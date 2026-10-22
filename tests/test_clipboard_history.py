"""Offline-only clipboard tests: memory backends or private fake executables.

All HOME/XDG paths and socket names are temporary. Socket files have no listener:
there is deliberately no compositor or session bus behind these fake commands.
"""

import base64
import io
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).absolute().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import clipboard_history as history


PLAIN = frozenset((b"text/plain;charset=utf-8", b"text/plain", b"UTF8_STRING"))
OLD = "Original café\n\n".encode()
TEXT = "Current complete dictation 世界".encode()
NEW = b"Newer independent copy"


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class MemoryBackend:
    def __init__(self, old=OLD, capacity=10):
        self.current = (PLAIN, old)
        self.items = [old]
        self.capacity = capacity
        self.writes = []
        self.reads = []
        self.snapshots = 0
        self.snapshot_changes = {}
        self.snapshot_errors = set()
        self.publish_failures = set()
        self.publish_no_effect = set()
        self.capture = True
        self.history_error = False
        self.after_first_publish = None
        self.delayed_ack = 0
        self.pending = None

    def snapshot(self, deadline):
        history._remaining(deadline)
        self.snapshots += 1
        if self.snapshots in self.snapshot_errors:
            raise history.Unavailable()
        if self.snapshots in self.snapshot_changes:
            self.current = self.snapshot_changes[self.snapshots]
        return self.current

    def insert(self, data):
        self.items = [data] + [item for item in self.items if item != data]
        self.items = self.items[:self.capacity]

    def publish(self, data, deadline):
        history._remaining(deadline)
        self.writes.append(data)
        number = len(self.writes)
        if number not in self.publish_no_effect:
            self.current = (PLAIN, data)
            if number != 1 or self.capture:
                if number == 1 and self.delayed_ack:
                    self.pending = data
                else:
                    self.insert(data)
            if number == 1 and self.after_first_publish is not None:
                self.current = (PLAIN, self.after_first_publish)
                self.insert(self.after_first_publish)
        if number in self.publish_failures:
            raise history.Unavailable()

    def history(self, index, deadline):
        history._remaining(deadline)
        self.reads.append(index)
        if self.history_error:
            raise history.Unavailable()
        if self.pending is not None:
            if self.delayed_ack <= 0:
                self.insert(self.pending)
                self.pending = None
            else:
                self.delayed_ack -= 1
        return (self.items[index] if index < len(self.items) else b"") + b"\n"


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.time = mock.patch.multiple(history.time, monotonic=self.clock.monotonic,
                                        sleep=self.clock.sleep)
        self.time.start()
        self.addCleanup(self.time.stop)
        self.backend = MemoryBackend()

    def trip(self, text=TEXT):
        return history.round_trip(self.backend, text)

    def test_success_restores_original_exact_unicode_and_newlines(self):
        self.assertTrue(self.trip())
        self.assertEqual(self.backend.writes, [TEXT, OLD])
        self.assertEqual(self.backend.current[1], OLD)
        self.assertEqual(self.backend.items[:2], [OLD, TEXT])
        self.assertIn(1, self.backend.reads)

    def test_already_retained_duplicate_is_allowed(self):
        self.backend.items = [TEXT, OLD]
        self.assertTrue(self.trip())
        self.assertEqual(self.backend.current[1], OLD)
        self.assertEqual(self.backend.items[:2], [OLD, TEXT])

    def test_same_old_and_transcript_needs_no_restore_write(self):
        self.backend = MemoryBackend(old=TEXT, capacity=1)
        self.assertTrue(self.trip())
        self.assertEqual(self.backend.writes, [TEXT])
        self.assertEqual(self.backend.current[1], TEXT)

    def test_zero_byte_valid_offer_is_restored_not_guessed_nil(self):
        self.backend = MemoryBackend(old=b"")
        self.assertTrue(self.trip())
        self.assertEqual(self.backend.writes, [TEXT, b""])
        self.assertEqual(self.backend.current[1], b"")

    def test_service_history_preflight_failure_never_writes(self):
        self.backend.history_error = True
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(self.backend.snapshots, 0)

    def test_initial_unreadable_snapshot_never_writes(self):
        self.backend.snapshot_errors.add(1)
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.writes, [])

    def test_stale_bytes_or_offers_before_publish_skip(self):
        for changed in ((PLAIN, NEW), (frozenset((b"text/plain",)), OLD)):
            with self.subTest(changed=changed):
                self.backend = MemoryBackend()
                self.backend.snapshot_changes[2] = changed
                self.assertFalse(self.trip())
                self.assertEqual(self.backend.writes, [])

    def test_second_snapshot_failure_never_writes(self):
        self.backend.snapshot_errors.add(2)
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.writes, [])

    def test_capture_waits_for_exact_ack_not_fixed_sleep(self):
        self.backend.delayed_ack = 3
        self.assertTrue(self.trip())
        self.assertGreaterEqual(self.backend.reads.count(0), 5)
        self.assertGreater(self.clock.now, 100)

    def test_capture_timeout_still_has_reserved_cleanup_budget(self):
        self.backend.capture = False
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.writes, [TEXT, OLD])
        self.assertEqual(self.backend.current[1], OLD)
        self.assertGreaterEqual(self.clock.now, 100 + history.WORK_SECONDS)
        self.assertLess(self.clock.now, 100 + history.WORK_SECONDS + 0.1)

    def test_no_broad_strip_in_capture_ack(self):
        self.backend.capture = False
        self.backend.items = [TEXT + b"\n"]
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.current[1], OLD)

    def test_partial_failed_publication_is_guardedly_restored(self):
        self.backend.publish_failures.add(1)
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.writes, [TEXT, OLD])
        self.assertEqual(self.backend.current[1], OLD)

    def test_failed_publication_without_effect_does_not_overwrite(self):
        self.backend.publish_failures.add(1)
        self.backend.publish_no_effect.add(1)
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.writes, [TEXT])
        self.assertEqual(self.backend.current[1], OLD)

    def test_failed_restore_is_not_success(self):
        self.backend.publish_failures.add(2)
        self.backend.publish_no_effect.add(2)
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.current[1], TEXT)

    def test_restore_reported_failure_but_exact_recovery_can_be_confirmed(self):
        self.backend.publish_failures.add(2)
        self.assertTrue(self.trip())
        self.assertEqual(self.backend.current[1], OLD)

    def test_cleanup_unreadable_offer_is_not_overwritten(self):
        self.backend.snapshot_errors.add(3)
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.writes, [TEXT])

    def test_cleanup_verification_failure_is_not_success(self):
        self.backend.snapshot_errors.add(4)
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.writes, [TEXT, OLD])

    def test_newer_copy_after_publish_wins(self):
        self.backend.after_first_publish = NEW
        # A foreign row 0 prevents exact capture acknowledgement: fail, but keep C.
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.writes, [TEXT])
        self.assertEqual(self.backend.current[1], NEW)

    def test_newer_copy_after_capture_wins_and_retention_is_confirmed(self):
        self.backend.snapshot_changes[3] = (PLAIN, NEW)
        original_history = self.backend.history

        def captured_foreign_copy(index, deadline):
            if self.backend.snapshots >= 3 and self.backend.items[0] != NEW:
                self.backend.insert(NEW)
            return original_history(index, deadline)

        self.backend.history = captured_foreign_copy
        self.assertTrue(self.trip())
        self.assertEqual(self.backend.current[1], NEW)
        self.assertEqual(self.backend.writes, [TEXT])

    def test_newer_copy_during_restore_verification_not_overwritten_again(self):
        self.backend.snapshot_changes[4] = (PLAIN, NEW)
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.current[1], NEW)
        self.assertEqual(self.backend.writes, [TEXT, OLD])

    def test_history_capacity_one_cannot_claim_both_retained(self):
        self.backend.capacity = 1
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.current[1], OLD)
        self.assertEqual(self.backend.items, [OLD])
        self.assertLessEqual(self.clock.now, 100 + history.CLEANUP_SECONDS + 0.001)

    def test_delayed_restoration_capture_does_not_hide_capacity_one_eviction(self):
        self.backend.capacity = 1
        original_publish = self.backend.publish
        original_history = self.backend.history
        restore_head_reads = 0

        def deferred_restore(data, deadline):
            original_publish(data, deadline)
            if len(self.backend.writes) == 2:
                # A is already the physical clipboard, but its capture is queued.
                self.backend.items = [TEXT]

        def delayed_capture(index, deadline):
            nonlocal restore_head_reads
            if len(self.backend.writes) == 2 and index == 0:
                restore_head_reads += 1
                if restore_head_reads >= 3:
                    self.backend.items = [OLD]
            return original_history(index, deadline)

        self.backend.publish = deferred_restore
        self.backend.history = delayed_capture
        self.assertFalse(self.trip())
        self.assertGreaterEqual(restore_head_reads, 3)
        self.assertEqual(self.backend.current[1], OLD)
        self.assertEqual(self.backend.items, [OLD])

    def test_bounded_retention_probes_allow_row_three_not_four(self):
        for index, expected in ((3, True), (4, False)):
            with self.subTest(index=index):
                self.backend = MemoryBackend()
                original_snapshot = self.backend.snapshot

                def snapshot(deadline):
                    result = original_snapshot(deadline)
                    if self.backend.snapshots == 4:
                        self.backend.items = [OLD] * index + [TEXT]
                    return result

                self.backend.snapshot = snapshot
                self.assertEqual(self.trip(), expected)
                self.assertLessEqual(max(self.backend.reads), 3)

    def test_unexpected_ordinary_exception_still_cleans_up(self):
        original_publish = self.backend.publish

        def publish(data, deadline):
            original_publish(data, deadline)
            if len(self.backend.writes) == 1:
                raise RuntimeError("SENSITIVE CONTENT MUST NOT BE LOGGED")

        self.backend.publish = publish
        self.assertFalse(self.trip())
        self.assertEqual(self.backend.current[1], OLD)

    def test_ordinary_signal_during_publication_still_cleans_up(self):
        original_publish = self.backend.publish
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handler = signal.getsignal(signum)

            def interrupted(_number, _frame):
                raise history.Unavailable()

            def publish(data, deadline):
                original_publish(data, deadline)
                if len(self.backend.writes) == 1:
                    os.kill(os.getpid(), signum)

            with self.subTest(signum=signum):
                self.backend = MemoryBackend()
                original_publish = self.backend.publish
                self.backend.publish = publish
                signal.signal(signum, interrupted)
                try:
                    self.assertFalse(self.trip())
                    self.assertEqual(self.backend.current[1], OLD)
                    self.assertIs(signal.getsignal(signum), interrupted)
                finally:
                    signal.signal(signum, old_handler)


class NormalizationTests(unittest.TestCase):
    def test_exact_eight_quotes_and_only_trailing_lf_removed(self):
        original = "  ‘a’‛′ “b”‟″\r\nembedded\n世界\t \n\n".encode()
        self.assertEqual(history.dispatched_text(original),
                         "  'a''' \"b\"\"\"\r\nembedded\n世界\t ".encode())

    def test_unrelated_unicode_and_carriage_return_stay(self):
        text = "‚„«»‵‶ café 🦆\u00a0\r".encode()
        self.assertEqual(history.dispatched_text(text), text)

    def test_invalid_utf8_empty_only_lf_or_oversize_skip(self):
        for original in (b"\xff", b"", b"\n\n", b"x" * (history.MAX_TEXT + 1)):
            with self.subTest(length=len(original)):
                with self.assertRaises(history.Unavailable):
                    history.dispatched_text(original)

    def test_full_transcript_up_to_limit_not_fragment(self):
        text = b"first\n" + b"x" * (history.MAX_TEXT - 11) + b"\nlast"
        self.assertEqual(len(text), history.MAX_TEXT)
        self.assertEqual(history.dispatched_text(text), text)


FAKE = r'''
import base64
import json
import os
from pathlib import Path
import signal
import sys
import time

state_path = Path(os.environ["TEST_STATE"])
state = json.loads(state_path.read_text())
name = Path(sys.argv[0]).name
args = sys.argv[1:]
record = {"name": name, "args": args,
          "context": "VOXTYPE_CONTEXT" in os.environ,
          "stdout": os.readlink("/proc/self/fd/1"),
          "stderr": os.readlink("/proc/self/fd/2"), "lock_inherited": False}
for fd in Path("/proc/self/fd").iterdir():
    try:
        if "voxtype-setup-clipboard-history.lock" in os.readlink(fd):
            record["lock_inherited"] = True
    except FileNotFoundError:
        pass
with open(os.environ["TEST_LOG"], "a") as log:
    log.write(json.dumps(record) + "\n")

def decode(value):
    return base64.b64decode(value)

def encode(value):
    return base64.b64encode(value).decode()

def save():
    state_path.write_text(json.dumps(state))

def insert(value):
    state["history"] = [value] + [item for item in state["history"] if item != value]
    state["history"] = state["history"][:state.get("capacity", 10)]

if state.get("stderr"):
    sys.stderr.write(state["stderr"])
if state.get("hang") == name:
    time.sleep(10)
if state.get("huge_read") == name:
    sys.stdout.buffer.write(b"x" * (1024 * 1024 + 100))
    sys.exit(0)
if name == "wl-paste":
    if args == ["--list-types"]:
        if state.get("no_offer"):
            sys.exit(1)
        sys.stdout.buffer.write(b"".join(mime.encode() + b"\n" for mime in state["types"]))
    else:
        assert len(args) == 3 and args[:2] == ["--no-newline", "--type"], args
        assert args[2] in state["types"], args
        if state.get("read_failure"):
            sys.exit(1)
        sys.stdout.buffer.write(decode(state["current"]))
    sys.exit(0)
if name == "qdbus6":
    assert args[:3] == ["org.kde.klipper", "/klipper", "getClipboardHistoryItem"], args
    index = int(args[3])
    if state.get("history_failure"):
        sys.exit(1)
    if len(state["writes"]) == 1 and state.get("signal"):
        os.kill(os.getppid(), state["signal"])
        time.sleep(0.1)
    value = decode(state["history"][index]) if index < len(state["history"]) else b""
    sys.stdout.buffer.write(value + state.get("framing", "\n").encode())
    sys.exit(0)
if name == "wl-copy":
    assert args == ["--type", "text/plain;charset=utf-8"], args
    value = encode(sys.stdin.buffer.read())
    state["current"] = value
    state["types"] = ["text/plain;charset=utf-8", "text/plain", "UTF8_STRING"]
    state["writes"].append(value)
    insert(value)
    save()
    if len(state["writes"]) == 1 and state.get("partial_failure"):
        sys.exit(1)
    if len(state["writes"]) == 1 and state.get("partial_hang"):
        time.sleep(10)
    if state.get("background"):
        if os.fork() == 0:
            time.sleep(0.2)
            os._exit(0)
    sys.exit(0)
raise AssertionError("unexpected fake command")
'''


def b64(value):
    return base64.b64encode(value).decode("ascii")


class IsolatedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voxtype-clipboard-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.runtime = self.home / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.bin = self.root / "fake-bin"
        self.bin.mkdir(mode=0o700)
        self.log = self.root / "commands.jsonl"
        self.state_path = self.root / "synthetic-state.json"
        self.state = {"current": b64(OLD), "history": [b64(OLD)],
                      "types": [mime.decode() for mime in PLAIN], "writes": []}
        self.save_state()
        for name in ("wl-paste", "wl-copy", "qdbus6"):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + FAKE)
            path.chmod(0o700)
        # Deliberately dead, private socket files. Never contact a real desktop.
        for path in (self.runtime / "fake-wayland", self.runtime / "not-a-session-bus"):
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(path))
        self.env = {
            "HOME": str(self.home), "PATH": str(self.bin),
            "XDG_RUNTIME_DIR": str(self.runtime), "XDG_SESSION_TYPE": "wayland",
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_DATA_HOME": str(self.home / "data"),
            "XDG_STATE_HOME": str(self.home / "state"),
            "XDG_CACHE_HOME": str(self.home / "cache"),
            "WAYLAND_DISPLAY": "fake-wayland",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.runtime}/not-a-session-bus",
            "TEST_STATE": str(self.state_path), "TEST_LOG": str(self.log),
            "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C.UTF-8",
            "VOXTYPE_CONTEXT": "PRIVATE_PRIOR_DICTATION_DO_NOT_USE",
        }

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state))

    def load_state(self):
        return json.loads(self.state_path.read_text())

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def cli(self, original=TEXT, *args, env=None):
        return subprocess.run([sys.executable, str(ROOT / "scripts/clipboard_history.py"), *args],
                              input=original, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=self.env if env is None else env, timeout=6, cwd=self.root)

    def assert_skipped(self, result, original=TEXT, no_writes=True):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, original)
        self.assertEqual(result.stderr, history.DIAGNOSTIC.encode())
        if no_writes:
            self.assertEqual(self.load_state()["writes"], [])

    def test_cli_success_exact_stdout_full_normalized_history_no_context(self):
        original = "  ‘First complete line’\n第二行\nlast ′ ″ \n\n".encode()
        result = self.cli(original)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, original)
        self.assertEqual(result.stderr, b"")
        state = self.load_state()
        expected = history.dispatched_text(original)
        self.assertEqual(state["writes"], [b64(expected), b64(OLD)])
        self.assertEqual(state["current"], b64(OLD))
        self.assertIn(b64(expected), state["history"])
        for record in self.records():
            self.assertFalse(record["context"])
            self.assertFalse(record["lock_inherited"])
            self.assertNotIn(original.decode(), json.dumps(record))
            if record["name"] == "wl-copy":
                self.assertEqual(record["stdout"], "/dev/null")
                self.assertEqual(record["stderr"], "/dev/null")
        self.assertEqual(sorted(path.name for path in self.runtime.iterdir()),
                         sorted(["fake-wayland", "not-a-session-bus", history.LOCK_NAME]))
        lock = self.runtime / history.LOCK_NAME
        self.assertEqual(lock.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_cli_large_complete_input_survives_both_pipes(self):
        original = ("first\n" + "世界 long dictation.\n" * 10000 + "last\n\n").encode()
        self.assertGreater(len(original), 65536)
        result = self.cli(original)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, original)
        self.assertEqual(self.load_state()["history"][:2],
                         [b64(OLD), b64(history.dispatched_text(original))])

    def test_cli_valid_zero_byte_offer_is_not_nil(self):
        self.state["current"] = b64(b"")
        self.state["history"] = []
        self.save_state()
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.load_state()["current"], b64(b""))

    def test_cli_invalid_input_or_vast_transcript_echoes_without_commands(self):
        for original in (b"\xff\x00\n", b"", b"\n\n", b"x" * (2 * history.MAX_OUTPUT)):
            with self.subTest(length=len(original)):
                self.assert_skipped(self.cli(original), original)
                self.assertEqual(self.records(), [])

    def test_rich_unknown_metadata_and_nil_offers_are_skipped(self):
        for types in (["text/plain", "text/html"], ["text/uri-list"], ["image/png"],
                      ["text/plain", "application/x-kde-onlyReplaceEmpty"],
                      ["unknown"], []):
            with self.subTest(types=types):
                self.state["types"] = types
                self.save_state()
                self.assert_skipped(self.cli())
        self.state["types"] = ["text/plain"]
        self.state["no_offer"] = True
        self.save_state()
        self.assert_skipped(self.cli())

    def test_supported_plain_mime_and_legacy_text_aliases(self):
        for mime in history.PLAIN_TYPES:
            with self.subTest(mime=mime):
                self.state.update(current=b64(OLD), history=[b64(OLD)], writes=[],
                                  types=[mime.decode()])
                self.save_state()
                result = self.cli()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.load_state()["current"], b64(OLD))

    def test_unreadable_and_invalid_utf8_snapshot_are_skipped(self):
        for changes in ({"read_failure": True}, {"current": b64(b"\xff")}):
            with self.subTest(changes=changes):
                original = self.state.copy()
                self.state.update(changes)
                self.save_state()
                self.assert_skipped(self.cli())
                self.state = original

    def test_snapshot_oversize_and_unbounded_command_output_are_capped(self):
        for changes in ({"current": b64(b"x" * history.MAX_OUTPUT)},
                        {"huge_read": "wl-paste"}, {"huge_read": "qdbus6"}):
            with self.subTest(keys=tuple(changes)):
                original = self.state.copy()
                self.state.update(changes)
                self.save_state()
                self.assert_skipped(self.cli())
                self.state = original

    def test_history_preflight_failure_or_bad_cli_framing_skips(self):
        for changes in ({"history_failure": True},
                        {"framing": "", "history": [b64(b"unframed item")]}):
            with self.subTest(changes=changes):
                original = self.state.copy()
                self.state.update(changes)
                self.save_state()
                self.assert_skipped(self.cli())
                self.state = original

    def test_read_timeout_and_raw_stderr_are_content_free(self):
        self.state.update(hang="wl-paste", stderr="PRIVATE_TRANSCRIPT_FROM_COMMAND")
        self.save_state()
        self.assert_skipped(self.cli())

    def test_missing_dependency_skips_before_any_command(self):
        (self.bin / "qdbus6").unlink()
        self.assert_skipped(self.cli())
        self.assertEqual(self.records(), [])

    def test_partial_write_failure_or_timeout_still_restores(self):
        for flag in ("partial_failure", "partial_hang"):
            with self.subTest(flag=flag):
                self.state.update(current=b64(OLD), history=[b64(OLD)], writes=[])
                self.state.pop("partial_failure", None)
                self.state.pop("partial_hang", None)
                self.state[flag] = True
                self.save_state()
                self.assert_skipped(self.cli(), no_writes=False)
                state = self.load_state()
                self.assertEqual(state["writes"], [b64(TEXT), b64(OLD)])
                self.assertEqual(state["current"], b64(OLD))

    def test_cli_signal_after_publication_attempt_restores(self):
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signum=signum):
                self.state.update(current=b64(OLD), history=[b64(OLD)], writes=[], signal=signum)
                self.save_state()
                self.assert_skipped(self.cli(), no_writes=False)
                self.assertEqual(self.load_state()["current"], b64(OLD))

    def test_wl_copy_normal_background_source_has_no_output_or_lock_pipes(self):
        self.state["background"] = True
        self.save_state()
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        for record in self.records():
            if record["name"] == "wl-copy":
                self.assertEqual(record["stdout"], "/dev/null")
                self.assertEqual(record["stderr"], "/dev/null")
                self.assertFalse(record["lock_inherited"])

    def test_cleanup_children_do_not_inherit_ignored_termination_signals(self):
        # POSIX preserves SIG_IGN across exec. A callable deferral handler must
        # instead leave clipboard providers ordinarily stoppable after exec.
        code = ("import json, signal; print(json.dumps(["
                "signal.getsignal(signal.SIGINT) == signal.SIG_IGN,"
                "signal.getsignal(signal.SIGTERM) == signal.SIG_IGN]))")
        with history._uninterrupted_cleanup():
            result = history.Commands(self.env).run(
                [sys.executable, "-c", code], history.time.monotonic() + 2)
        self.assertEqual(json.loads(result), [False, False])

    def test_safe_help_does_not_read_stdin_or_touch_desktop(self):
        result = self.cli(b"", "--help", env={"HOME": str(self.home), "PATH": str(self.bin)})
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"save/copy/restore", result.stdout)
        self.assertEqual(self.records(), [])
        self.assertFalse((self.runtime / history.LOCK_NAME).exists())

    def test_missing_nonlocal_and_implicit_session_guards(self):
        cases = ({"WAYLAND_DISPLAY": ""}, {"WAYLAND_DISPLAY": "../fake-wayland"},
                 {"WAYLAND_DISPLAY": "missing-wayland"}, {"WAYLAND_SOCKET": "0"},
                 {"XDG_SESSION_TYPE": "x11"}, {"DBUS_SESSION_BUS_ADDRESS": ""},
                 {"DBUS_SESSION_BUS_ADDRESS": "autolaunch:"},
                 {"DBUS_SESSION_BUS_ADDRESS": "tcp:host=example.invalid,port=1"},
                 {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/missing/bus"},
                 {"DBUS_SESSION_BUS_ADDRESS": "unix:abstract=a;autolaunch:"},
                 {"DBUS_SESSION_BUS_ADDRESS": "unix:path=relative"},
                 {"DBUS_SESSION_BUS_ADDRESS": "unix:abstract="},
                 {"DBUS_SESSION_BUS_ADDRESS": "unix:abstract=a,path=/missing"},
                 {"DBUS_SESSION_BUS_ADDRESS": "unix:abstract=a,abstract=b"})
        for changes in cases:
            with self.subTest(changes=changes):
                self.assert_skipped(self.cli(env={**self.env, **changes}))
                self.assertEqual(self.records(), [])

    def test_socket_symlinks_or_regular_files_are_rejected(self):
        link = self.runtime / "socket-link"
        link.symlink_to(self.runtime / "fake-wayland")
        regular = self.runtime / "not-socket"
        regular.write_text("")
        for path in (link, regular):
            with self.subTest(path=path.name):
                self.assert_skipped(self.cli(env={**self.env, "WAYLAND_DISPLAY": str(path)}))
                self.assertEqual(self.records(), [])

    def test_runtime_missing_relative_public_or_symlink_paths_are_rejected(self):
        public = self.home / "public"
        public.mkdir(mode=0o755)
        link = self.home / "runtime-link"
        link.symlink_to(self.runtime, target_is_directory=True)
        ancestor = self.root / "home-link"
        ancestor.symlink_to(self.home, target_is_directory=True)
        for path in ("", "relative", str(self.home / "missing"), str(public), str(link),
                     str(ancestor / "runtime"), str(self.runtime / ".." / "runtime")):
            with self.subTest(path=path):
                self.assert_skipped(self.cli(env={**self.env, "XDG_RUNTIME_DIR": path}))
                self.assertEqual(self.records(), [])

    def test_unsafe_lock_symlink_nonregular_nonempty_public_hardlink(self):
        lock = self.runtime / history.LOCK_NAME
        target = self.root / "target"
        target.write_bytes(b"DO NOT MODIFY")
        hardlink_target = self.root / "empty-private-target"
        hardlink_target.touch(mode=0o600)
        constructors = (
            lambda: lock.symlink_to(target),
            lambda: lock.mkdir(mode=0o700),
            lambda: os.mkfifo(lock, 0o600),
            lambda: (lock.touch(mode=0o600), lock.write_bytes(b"DO NOT MODIFY")),
            lambda: (lock.touch(mode=0o644), lock.chmod(0o644)),
            lambda: os.link(hardlink_target, lock),
        )
        for constructor in constructors:
            with self.subTest(constructor=constructor):
                constructor()
                self.assert_skipped(self.cli())
                self.assertEqual(self.records(), [])
                if lock.is_dir():
                    lock.rmdir()
                else:
                    lock.unlink()
        self.assertEqual(target.read_bytes(), b"DO NOT MODIFY")

    def test_runtime_and_lock_foreign_owners_are_rejected_offline(self):
        # Metadata injection avoids chown, elevated privileges or any foreign paths.
        real_fstat = os.fstat
        for foreign in ("runtime", "lock"):
            with self.subTest(foreign=foreign):
                def fstat(fd):
                    info = real_fstat(fd)
                    is_runtime = stat.S_ISDIR(info.st_mode)
                    if (foreign == "runtime") == is_runtime:
                        fields = list(info)
                        fields[4] = os.getuid() + 1
                        return os.stat_result(fields)
                    return info

                with mock.patch.object(history.os, "fstat", side_effect=fstat):
                    with self.assertRaises(history.Unavailable):
                        with history.private_lock(str(self.runtime)):
                            self.fail("foreign owner accepted")

    def test_busy_private_lock_is_nonblocking_and_not_queued(self):
        with history.private_lock(str(self.runtime)):
            self.assert_skipped(self.cli())
            self.assertEqual(self.records(), [])
        # Releasing it leaves an empty reusable file, not a stale PID protocol.
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_passthrough_even_if_backend_raises_sensitive_exception(self):
        original = b"CURRENT_SECRET\n\n"
        stdout, stderr = io.BytesIO(), io.StringIO()
        with mock.patch.object(history, "DesktopBackend", side_effect=RuntimeError("PRIVATE_ERROR")):
            result = history.run_hook(io.BytesIO(original), stdout, stderr, self.env)
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), original)
        self.assertEqual(stderr.getvalue(), history.DIAGNOSTIC)

    def test_in_memory_hook_success_and_normalization_keep_stdout_unchanged(self):
        original = "‘Current’\nnot context\n\n".encode()
        backend = MemoryBackend()
        stdout, stderr = io.BytesIO(), io.StringIO()
        with mock.patch.object(history, "DesktopBackend", return_value=backend):
            result = history.run_hook(io.BytesIO(original), stdout, stderr, self.env)
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), original)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(backend.writes, [history.dispatched_text(original), OLD])


if __name__ == "__main__":
    unittest.main()
