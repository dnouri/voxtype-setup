#!/usr/bin/env python3
"""Best-effort Klipper save/copy/restore hook; never an atomic history-only API.

Only supported, readable plain-text CLIPBOARD offers are saved. A detected newer
copy wins over restoration. Selection changes between checks remain possible;
crashes, SIGKILL, clipboard-manager policy and capacity cannot be repaired here.
No PRIMARY request, transcript file, clipboard database or paste key is used.
"""

import argparse
from contextlib import contextmanager
import fcntl
import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from urllib.parse import unquote


MAX_OUTPUT = 1024 * 1024
MAX_TEXT = MAX_OUTPUT - 1  # Leave room for qdbus6's single framing LF.
COMMAND_SECONDS = 0.250
WORK_SECONDS = 1.5
CLEANUP_SECONDS = 1.5
POLL_SECONDS = 0.025
HISTORY_ROWS = (0, 1, 2, 3)
LOCK_NAME = "voxtype-setup-clipboard-history.lock"
DIAGNOSTIC = "Clipboard history was not confirmed; keeping the original dictation.\n"
PLAIN_TYPES = (
    b"text/plain;charset=utf-8", b"text/plain", b"UTF8_STRING", b"TEXT", b"STRING",
)
QUOTES = str.maketrans({**dict.fromkeys("\u2018\u2019\u201b\u2032", "'"),
                       **dict.fromkeys("\u201c\u201d\u201f\u2033", '"')})


class Unavailable(Exception):
    """A content-free failure; command output must never enter diagnostics."""


def dispatched_text(original):
    """Mirror Voxtype 1.0.1 trim=false, then its output quote normalization."""
    if len(original) > MAX_TEXT:
        raise Unavailable()
    try:
        text = original.decode("utf-8").rstrip("\n").translate(QUOTES).encode("utf-8")
    except UnicodeError:
        raise Unavailable() from None
    if not text:
        raise Unavailable()
    return text


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Unavailable()
    return remaining


class Commands:
    """Bounded pipe I/O; wl-copy's background source never inherits our pipes."""

    def __init__(self, env):
        self.env = env

    @staticmethod
    def _stop(process):
        # Only our new process group, never the caller's session or compositor.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            pass

    def run(self, argv, deadline, data=None):
        deadline = min(deadline, time.monotonic() + COMMAND_SECONDS)
        _remaining(deadline)
        capture = data is None
        process = subprocess.Popen(
            argv, env=self.env, stdin=subprocess.DEVNULL if capture else subprocess.PIPE,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True, bufsize=0,
        )
        output = bytearray()
        offset = 0
        try:
            with selectors.DefaultSelector() as selector:
                pipe = process.stdout if capture else process.stdin
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ if capture else selectors.EVENT_WRITE)
                while selector.get_map():
                    events = selector.select(_remaining(deadline))
                    for key, _ in events:
                        if capture:
                            chunk = os.read(key.fd, min(65536, MAX_OUTPUT + 1 - len(output)))
                            if chunk:
                                output.extend(chunk)
                                if len(output) > MAX_OUTPUT:
                                    raise Unavailable()
                                continue
                        elif offset < len(data):
                            offset += os.write(key.fd, memoryview(data)[offset:offset + 65536])
                            if offset < len(data):
                                continue
                        selector.unregister(pipe)
                        pipe.close()
                if process.wait(timeout=_remaining(deadline)) != 0:
                    raise Unavailable()
            return bytes(output)
        except BaseException:
            self._stop(process)
            raise
        finally:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()


def _open_runtime(path):
    """Open every component without following symlinks; final directory is private."""
    if not path or not os.path.isabs(path) or ".." in path.split("/"):
        raise Unavailable()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for component in path.split("/"):
            if component:
                next_fd = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise Unavailable()
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def private_lock(runtime):
    """Do not queue dictations, follow links, reuse foreign files, or leak the lock."""
    directory = _open_runtime(runtime)
    lock = None
    try:
        lock = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
                       os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=directory)
        info = os.fstat(lock)
        current = os.stat(LOCK_NAME, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != 0
                or info.st_nlink != 1 or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise Unavailable()
        os.set_inheritable(lock, False)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def _local_socket(path):
    if not os.path.isabs(path):
        raise Unavailable()
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise Unavailable()


def checked_environment(environment):
    """Require explicit local sessions, never a display/bus autolaunch fallback."""
    env = dict(environment)
    runtime = env.get("XDG_RUNTIME_DIR", "")
    display = env.get("WAYLAND_DISPLAY", "")
    if (not display or env.get("XDG_SESSION_TYPE", "wayland") != "wayland"
            or "WAYLAND_SOCKET" in env):
        raise Unavailable()
    if not os.path.isabs(display):
        if "/" in display or display in (".", ".."):
            raise Unavailable()
        display = os.path.join(runtime, display)
    _local_socket(display)
    address = env.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not address.startswith("unix:") or ";" in address:
        raise Unavailable()
    fields = {}
    for field in address[5:].split(","):
        key, separator, value = field.partition("=")
        if not separator or key in fields or key not in ("path", "abstract", "guid"):
            raise Unavailable()
        fields[key] = unquote(value)
    if ("path" in fields) == ("abstract" in fields):
        raise Unavailable()
    if "path" in fields:
        _local_socket(fields["path"])
    elif not fields["abstract"]:
        raise Unavailable()
    # Neither old dictation context nor alternate inherited session hints are needed.
    for key in ("VOXTYPE_CONTEXT", "DBUS_STARTER_ADDRESS", "DBUS_STARTER_BUS_TYPE", "DISPLAY"):
        env.pop(key, None)
    return env


class DesktopBackend:
    """The small packaged-command boundary, also replaceable by an in-memory test."""

    def __init__(self, env):
        self.commands = Commands(env)
        self.tools = {}
        for name in ("wl-copy", "wl-paste", "qdbus6"):
            path = shutil.which(name, path=env.get("PATH", ""))
            if path is None or not os.path.isabs(path):
                raise Unavailable()
            self.tools[name] = path

    def _read(self, name, args, deadline):
        return self.commands.run([self.tools[name], *args], deadline)

    def snapshot(self, deadline):
        offered = self._read("wl-paste", ["--list-types"], deadline)
        types = offered.split(b"\n")
        if types[-1:] == [b""]:
            types.pop()
        if not types or any(mime not in PLAIN_TYPES for mime in types):
            raise Unavailable()
        mime = next(mime for mime in PLAIN_TYPES if mime in types)
        data = self._read("wl-paste", ["--no-newline", "--type", mime.decode("ascii")], deadline)
        if len(data) > MAX_TEXT:
            raise Unavailable()
        try:
            data.decode("utf-8")
        except UnicodeError:
            raise Unavailable() from None
        # Include offers in the pre-publication staleness check, not merely text.
        return frozenset(types), data

    def publish(self, data, deadline):
        self.commands.run([self.tools["wl-copy"], "--type", "text/plain;charset=utf-8"],
                          deadline, data=data)

    def history(self, index, deadline):
        reply = self._read("qdbus6", ["org.kde.klipper", "/klipper",
                                     "getClipboardHistoryItem", str(index)], deadline)
        # qdbus6 adds exactly one LF; do not strip any of the item's own bytes.
        if not reply.endswith(b"\n"):
            raise Unavailable()
        try:
            reply.decode("utf-8")
        except UnicodeError:
            raise Unavailable() from None
        return reply


def _pause(deadline):
    time.sleep(min(POLL_SECONDS, _remaining(deadline)))


def _captured(backend, text, deadline):
    while True:
        if backend.history(0, deadline) == text + b"\n":
            return
        _pause(deadline)


def _retained(backend, text, expected_head, deadline):
    while True:
        # A physical restore can precede Klipper's capture of that new offer.
        # Wait for the final clipboard item at the head before claiming T
        # survives it; otherwise a capacity-one history could pass briefly.
        if backend.history(0, deadline) == expected_head + b"\n":
            for index in HISTORY_ROWS:
                if backend.history(index, deadline) == text + b"\n":
                    return True
        _pause(deadline)


@contextmanager
def _uninterrupted_cleanup():
    # Defer a second signal while bounded recovery finishes. Use a caught
    # handler, not SIG_IGN: ignored dispositions survive exec and would make
    # wl-copy's background provider ignore service-stop signals too.
    def finish_cleanup(_signum, _frame):
        pass

    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, finish_cleanup)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _recover(backend, saved, text, captured, deadline):
    current = backend.snapshot(deadline)
    expected_head = current[1]
    if current[1] == text:
        if saved[1] != text:
            try:
                backend.publish(saved[1], deadline)
            except Exception:
                # A failed publisher may nonetheless have changed the selection.
                pass
            if backend.snapshot(deadline)[1] != saved[1]:
                return False
            expected_head = saved[1]
    # Otherwise leave the detected newer copy alone. There is still a TOCTOU
    # window between the check and a restore; this is not compare-and-swap.
    return captured and _retained(backend, text, expected_head, deadline)


def round_trip(backend, text):
    """Return true only after capture, guarded recovery and retained-history proof."""
    attempted = False
    captured = False
    operation_ok = False
    recovered = False
    saved = None
    try:
        deadline = time.monotonic() + WORK_SECONDS
        backend.history(0, deadline)  # Service/history-read preflight, before any copy.
        saved = backend.snapshot(deadline)
        if backend.snapshot(deadline) != saved:
            raise Unavailable()
        _remaining(deadline)
        attempted = True  # Must precede the write, which can fail after publication.
        backend.publish(text, deadline)
        _captured(backend, text, deadline)
        captured = operation_ok = True
    except Exception:
        pass
    finally:
        if attempted:
            with _uninterrupted_cleanup():
                try:
                    # A separate reserve, never the exhausted work deadline.
                    recovered = _recover(backend, saved, text, captured,
                                         time.monotonic() + CLEANUP_SECONDS)
                except Exception:
                    pass
    return operation_ok and recovered


def run_hook(stdin, stdout, stderr, environment):
    # Voxtype writes all stdin before it drains stdout. Buffer that input so a
    # large passthrough cannot deadlock the two pipes. The clipboard work itself
    # is capped; oversized input is echoed but never published or saved to disk.
    original = stdin.read()
    success = False
    try:
        text = dispatched_text(original)
        with private_lock(environment.get("XDG_RUNTIME_DIR", "")):
            backend = DesktopBackend(checked_environment(environment))
            success = round_trip(backend, text)
    except Exception:
        pass
    finally:
        stdout.write(original)
        stdout.flush()
    if not success:
        stderr.write(DIAGNOSTIC)
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    def interrupted(_signum, _frame):
        raise Unavailable()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    return run_hook(sys.stdin.buffer, sys.stdout.buffer, sys.stderr, os.environ)


if __name__ == "__main__":
    sys.exit(main())
