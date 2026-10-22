"""Offline tests: isolated HOME/XDG, fake-only PATH and an invalid session bus."""

import contextlib
import errno
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import pty
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).absolute().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import doctor
import install


FAKE = r'''
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["TEST_COMMAND_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "qdbus6":
    assert args == ["org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel.action", "268435488"], args
    sys.stdout.write(os.environ.get("TEST_SHORTCUT", ""))
    sys.exit(int(os.environ.get("TEST_QDBUS_EXIT", "0")))
if name == "kbuildsycoca6":
    assert not args, args
    sys.exit(int(os.environ.get("TEST_REFRESH_EXIT", "0")))
if name == "systemctl":
    assert args == ["--user", "show", "voxtype.service", "--no-pager", "--property=LoadState,ActiveState,SubState,FragmentPath"], args
    print(os.environ.get("TEST_SERVICE", "LoadState=loaded\nActiveState=active\nSubState=running\nFragmentPath=/usr/lib/systemd/user/voxtype.service"))
    sys.exit(int(os.environ.get("TEST_SYSTEMCTL_EXIT", "0")))
if name == "voxtype":
    assert len(args) == 5 and args[0] == "--config" and args[2] == "info" and args[4] == "--json", args
    defaults = {
        "engines": [{"name": "whisper", "compiled": True, "active": True}],
        "models": {"engines": {"whisper": {"default": "base.en", "models": [{"name": "base.en", "installed": True}]}}},
        "variants": {"active_variant": "whisper-vulkan", "running_variant": "whisper-vulkan", "binary_path": "/private/user/path"},
        "accel": {"state": "gpu", "backend": "vulkan", "evidence": ["PRIVATE_TRANSCRIPT_MUST_NOT_PRINT"]},
    }
    assert args[3] in defaults, args
    print(os.environ.get("TEST_INFO_" + args[3].upper(), json.dumps(defaults[args[3]])))
    sys.exit(int(os.environ.get("TEST_INFO_EXIT", "0")))
# Presence of these tools can be checked, but invoking them is always a bug.
print("UNSAFE TOOL INVOCATION", file=sys.stderr)
sys.exit(99)
'''


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voxtype-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        self.env = {
            "HOME": str(self.home), "PATH": str(self.bin),
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_DATA_HOME": str(self.home / "data"),
            "XDG_STATE_HOME": str(self.home / "state"),
            "XDG_CACHE_HOME": str(self.home / "cache"),
            "XDG_RUNTIME_DIR": str(self.home / "runtime"),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.root}/nonexistent-session-bus",
            "TEST_COMMAND_LOG": str(self.log), "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": "C.UTF-8",
        }
        for name in ("qdbus6", "kbuildsycoca6", "systemctl", "voxtype", "voxtype-osd", "voxtype-osd-gtk4",
                     "ydotool", "wtype", "eitype", "dotool", "wl-copy", "wl-paste", "xclip", "arecord"):
            self.fake_command(name)
        self.config = self.home / "config/voxtype/config.toml"
        self.desktop = self.home / "data/applications/voxtype-record-toggle.desktop"
        self.helper = self.home / "data/voxtype-setup/clipboard_history.py"
        self.asset_paths = (self.helper, self.config, self.desktop)
        self.backups = self.home / "state/voxtype-setup/backups"

    def fake_command(self, name, source=FAKE):
        fake = self.bin / name
        fake.write_text(f"#!{sys.executable}\n" + source)
        fake.chmod(0o700)
        return fake

    def run_script(self, script, *args, env=None):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args],
                              env=env or self.env, input="", capture_output=True, text=True,
                              cwd=self.root, timeout=20)

    def installer(self, *args, **kwargs):
        return self.run_script("install.py", *args, **kwargs)

    def install_assets(self):
        for source, destination, mode in (
            (ROOT / "scripts/clipboard_history.py", self.helper, 0o600),
            (ROOT / "config/voxtype.toml", self.config, 0o600),
            (ROOT / "desktop/voxtype-record-toggle.desktop", self.desktop, 0o644),
        ):
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            destination.chmod(mode)

    def backup_for(self, directory, destination):
        return next(directory.glob(f"??-{destination.name}"))

    def disable_history(self):
        text = self.config.read_text()
        self.config.write_text(re.sub(r"\n\[output\.post_process\]\n.*?(?=\n\[)", "\n", text, flags=re.DOTALL))

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def snapshot(self):
        return {str(path.relative_to(self.home)): (path.lstat().st_mode, path.lstat().st_mtime_ns,
                                                  path.read_bytes() if path.is_file() else None)
                for path in self.home.rglob("*")}

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_error(self, result, text):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(text, result.stdout + result.stderr)


class InstallerTests(IsolatedTest):
    def test_dry_run_has_no_filesystem_effects(self):
        before = self.snapshot()
        result = self.installer()
        self.assert_ok(result)
        self.assertIn("DRY RUN", result.stdout)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.commands(), [install.SHORTCUT_QUERY])

    def test_first_install_uses_only_three_assets_helper_before_config(self):
        result = self.installer("--apply")
        self.assert_ok(result)
        self.assertEqual(self.config.read_bytes(), (ROOT / "config/voxtype.toml").read_bytes())
        self.assertEqual(self.desktop.read_bytes(), (ROOT / "desktop/voxtype-record-toggle.desktop").read_bytes())
        self.assertEqual(self.helper.read_bytes(), (ROOT / "scripts/clipboard_history.py").read_bytes())
        self.assertEqual(stat.S_IMODE(self.helper.stat().st_mode), 0o600)
        self.assertLess(result.stdout.index(f"INSTALLED: {self.helper}"), result.stdout.index(f"INSTALLED: {self.config}"))
        self.assertIn("APPLIED: three assets installed", result.stdout)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.desktop.stat().st_mode), 0o644)
        self.assertFalse(self.backups.exists())
        files = [p for p in self.home.rglob("*") if p.is_file()]
        self.assertEqual(set(files), set(self.asset_paths))
        self.assertEqual(self.commands(), [install.SHORTCUT_QUERY])

    def test_identical_apply_is_a_true_noop(self):
        self.install_assets()
        before = self.snapshot()
        result = self.installer("--apply", "--replace")
        self.assert_ok(result)
        self.assertIn("NO-OP", result.stdout)
        self.assertEqual(before, self.snapshot())

    def test_conflict_preflights_all_three_files_before_writing(self):
        self.desktop.parent.mkdir(parents=True)
        self.desktop.write_text("[Desktop Entry]\nName=My existing launcher\n")
        before = self.snapshot()
        result = self.installer("--apply")
        self.assert_error(result, "require --replace")
        self.assertIn("--- ", result.stdout)
        self.assertIn("+++ ", result.stdout)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.helper.exists())
        self.assertEqual(before, self.snapshot())

    def test_dry_run_conflict_shows_diff_but_never_creates_backup(self):
        self.install_assets()
        self.config.write_text("# my current choices\n")
        before = self.snapshot()
        result = self.installer("--replace")
        self.assert_ok(result)
        self.assertIn("-# my current choices", result.stdout)
        self.assertEqual(before, self.snapshot())

    def test_replacements_have_unique_private_backups_and_safe_modes(self):
        self.install_assets()
        originals = {self.helper.name: b"# original helper\n", self.config.name: b"# original config\n",
                     self.desktop.name: b"# original desktop\n"}
        for path in self.asset_paths:
            path.write_bytes(originals[path.name])
            path.chmod(0o777)
        result = self.installer("--apply", "--replace")
        self.assert_ok(result)
        first = next(self.backups.iterdir())
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o700)
        self.assertEqual(len(list(first.iterdir())), 3)
        for path in first.iterdir():
            self.assertEqual(path.read_bytes(), originals[path.name.partition("-")[2]])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.helper.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.desktop.stat().st_mode), 0o644)
        self.config.write_text("# second original\n")
        self.assert_ok(self.installer("--apply", "--replace"))
        directories = list(self.backups.iterdir())
        self.assertEqual(len(directories), 2)
        second = next(path for path in directories if path != first)
        self.assertEqual(self.backup_for(second, self.config).read_text(), "# second original\n")
        self.assertEqual(self.backup_for(first, self.config).read_bytes(), originals[self.config.name])
        self.assertFalse(list(self.home.rglob(".voxtype-setup-*")))

    def test_insecure_helper_directory_is_rejected_before_any_asset_write(self):
        self.helper.parent.mkdir(parents=True)
        self.helper.parent.chmod(0o777)
        before = self.snapshot()
        result = self.installer("--apply")
        self.assert_error(result, "history helper directory must be user-owned and not writable by others")
        self.assertEqual(before, self.snapshot())
        self.assertFalse(any(path.exists() for path in self.asset_paths))
        self.assertFalse(self.backups.exists())

    def test_helper_mode_only_change_requires_permission_and_private_backup(self):
        self.install_assets()
        original = self.helper.read_bytes()
        self.helper.chmod(0o700)
        before = self.snapshot()
        result = self.installer("--apply")
        self.assert_error(result, "require --replace")
        self.assertIn("MODE: 0700 -> 0600", result.stdout)
        self.assertEqual(before, self.snapshot())
        self.assert_ok(self.installer("--apply", "--replace"))
        self.assertEqual(stat.S_IMODE(self.helper.stat().st_mode), 0o600)
        backup = self.backup_for(next(self.backups.iterdir()), self.helper)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(before[str(self.config.relative_to(self.home))], self.snapshot()[str(self.config.relative_to(self.home))])
        self.assertEqual(before[str(self.desktop.relative_to(self.home))], self.snapshot()[str(self.desktop.relative_to(self.home))])

    def test_every_destination_is_preflighted_before_any_asset_write(self):
        target = self.root / "outside-asset"
        target.write_text("outside unchanged")
        for destination in self.asset_paths:
            for kind in ("symlink", "directory"):
                with self.subTest(destination=destination.name, kind=kind):
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if kind == "symlink":
                        destination.symlink_to(target)
                    else:
                        destination.mkdir()
                    result = self.installer("--apply", "--replace")
                    self.assert_error(result, "refusing symlink" if kind == "symlink" else "not a regular file")
                    for other in self.asset_paths:
                        if other != destination:
                            self.assertFalse(other.exists())
                    self.assertFalse(self.backups.exists())
                    self.assertEqual(target.read_text(), "outside unchanged")
                    destination.unlink() if kind == "symlink" else destination.rmdir()

    def test_all_originals_are_backed_up_before_the_first_asset_write(self):
        self.install_assets()
        originals = {path: f"original {path.name}".encode() for path in self.asset_paths}
        for path, data in originals.items():
            path.write_bytes(data)
        original_write = install.atomic_write
        written = []

        def checked_write(plan):
            backup_dir, = self.backups.iterdir()
            self.assertEqual(len(list(backup_dir.iterdir())), 3)
            for path, data in originals.items():
                self.assertEqual(self.backup_for(backup_dir, path).read_bytes(), data)
                self.assertEqual(stat.S_IMODE(self.backup_for(backup_dir, path).stat().st_mode), 0o600)
            written.append(plan.destination)
            original_write(plan)

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(install, "atomic_write", side_effect=checked_write), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(install.main(["--apply", "--replace", "--skip-shortcut-check"]), 0)
        self.assertEqual(written, list(self.asset_paths))

    def test_failed_backup_never_starts_asset_writes(self):
        self.install_assets()
        originals = {path: f"original {path.name}".encode() for path in self.asset_paths}
        for path, data in originals.items():
            path.write_bytes(data)
        original_open = Path.open

        def failing_open(path, *args, **kwargs):
            if path.is_relative_to(self.backups) and args and args[0] == "xb" and path.name.startswith("03-"):
                raise OSError("synthetic backup failure")
            return original_open(path, *args, **kwargs)

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(Path, "open", failing_open), \
                mock.patch.object(install, "atomic_write") as write_asset, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(install.main(["--apply", "--replace", "--skip-shortcut-check"]), 1)
        write_asset.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in self.asset_paths}, originals)
        self.assertEqual(self.commands(), [])

    def test_partial_write_failure_reports_exact_three_asset_progress_and_keeps_backups(self):
        original_write = install.atomic_write
        for fail_at in range(3):
            with self.subTest(fail_at=fail_at):
                self.install_assets()
                originals = {path: f"original {fail_at} {path.name}".encode() for path in self.asset_paths}
                for path, data in originals.items():
                    path.write_bytes(data)
                previous_backups = set(self.backups.iterdir()) if self.backups.exists() else set()
                attempted = []

                def failing_write(plan):
                    attempted.append(plan.destination)
                    if len(attempted) == fail_at + 1:
                        raise OSError("synthetic asset failure")
                    original_write(plan)

                with mock.patch.dict(os.environ, self.env, clear=True), \
                        mock.patch.object(install, "atomic_write", side_effect=failing_write), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(install.main(["--apply", "--replace", "--skip-shortcut-check"]), 1)
                self.assertIn(f"{fail_at} file(s) already installed", errors.getvalue())
                self.assertEqual(attempted, list(self.asset_paths[:fail_at + 1]))
                backup_dir, = set(self.backups.iterdir()) - previous_backups
                for index, path in enumerate(self.asset_paths):
                    self.assertEqual(self.backup_for(backup_dir, path).read_bytes(), originals[path])
                    expected = (ROOT / {self.helper: "scripts/clipboard_history.py", self.config: "config/voxtype.toml",
                                        self.desktop: "desktop/voxtype-record-toggle.desktop"}[path]).read_bytes()
                    self.assertEqual(path.read_bytes(), expected if index < fail_at else originals[path])
        self.assertEqual(self.commands(), [])

    def test_preflight_rechecks_all_three_asset_contents_and_modes(self):
        self.install_assets()
        for destination in self.asset_paths:
            for change in ("contents", "mode"):
                with self.subTest(destination=destination.name, change=change):
                    self.install_assets()
                    with mock.patch.dict(os.environ, self.env, clear=True):
                        plans = install.plan_files(install.xdg_paths())
                        if change == "contents":
                            destination.write_text("concurrent edit")
                        else:
                            destination.chmod(0o400)
                        before = self.snapshot()
                        with mock.patch.object(install, "atomic_write") as write_asset:
                            with self.assertRaisesRegex(install.SetupError, "changed after preflight"):
                                install.apply_files(plans, self.backups)
                    write_asset.assert_not_called()
                    self.assertEqual(before, self.snapshot())
                    destination.chmod(0o600)

    def test_safe_existing_readonly_permissions_are_preserved(self):
        self.install_assets()
        self.desktop.write_text("old desktop")
        self.desktop.chmod(0o440)
        self.assert_ok(self.installer("--apply", "--replace"))
        self.assertEqual(stat.S_IMODE(self.desktop.stat().st_mode), 0o440)

    def test_file_symlink_and_dangling_symlink_are_refused(self):
        self.config.parent.mkdir(parents=True)
        target = self.root / "outside-config"
        for exists in (True, False):
            with self.subTest(existing_target=exists):
                if exists:
                    target.write_text("outside unchanged")
                elif target.exists():
                    target.unlink()
                self.config.symlink_to(target)
                result = self.installer("--apply", "--replace")
                self.assert_error(result, "refusing symlink")
                self.assertFalse(self.desktop.exists())
                self.assertTrue(self.config.is_symlink())
                if exists:
                    self.assertEqual(target.read_text(), "outside unchanged")
                else:
                    self.assertFalse(target.exists())
                self.config.unlink()

    def test_symlink_parent_is_refused_for_each_destination(self):
        for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            with self.subTest(variable=variable):
                outside = self.root / f"outside-{variable}"
                outside.mkdir()
                link = self.root / f"link-{variable}"
                link.symlink_to(outside, target_is_directory=True)
                env = dict(self.env, **{variable: str(link)})
                result = self.installer("--apply", env=env)
                self.assert_error(result, "refusing symlink")
                self.assertFalse(list(outside.iterdir()))
                self.assertFalse(self.config.exists())
                self.assertFalse(self.desktop.exists())

    def test_nested_destination_parent_symlink_is_refused(self):
        self.config.parent.parent.mkdir(parents=True)
        outside = self.root / "outside-nested-config"
        outside.mkdir()
        self.config.parent.symlink_to(outside, target_is_directory=True)
        self.assert_error(self.installer("--apply"), "refusing symlink")
        self.assertFalse(list(outside.iterdir()))
        self.assertFalse(self.desktop.exists())

    def test_backup_symlink_parent_is_refused_before_replacements(self):
        self.install_assets()
        self.config.write_text("old config")
        outside = self.root / "outside-state"
        outside.mkdir()
        state = Path(self.env["XDG_STATE_HOME"])
        state.symlink_to(outside, target_is_directory=True)
        result = self.installer("--apply", "--replace")
        self.assert_error(result, "refusing symlink")
        self.assertEqual(self.config.read_text(), "old config")
        self.assertFalse(list(outside.iterdir()))

    def test_backup_directory_symlink_itself_is_refused(self):
        self.install_assets()
        self.config.write_text("old config")
        outside = self.root / "outside-backups"
        outside.mkdir()
        self.backups.parent.mkdir(parents=True)
        self.backups.symlink_to(outside, target_is_directory=True)
        self.assert_error(self.installer("--apply", "--replace"), "refusing symlink")
        self.assertEqual(self.config.read_text(), "old config")
        self.assertFalse(list(outside.iterdir()))

    def test_nonprivate_backup_directory_is_refused_before_writes(self):
        self.install_assets()
        self.config.write_text("old config")
        self.backups.mkdir(parents=True)
        self.backups.chmod(0o755)
        before = self.snapshot()
        self.assert_error(self.installer("--apply", "--replace"), "private (0700)")
        self.assertEqual(before, self.snapshot())

    def test_nonregular_file_and_parent_are_refused(self):
        self.config.mkdir(parents=True)
        self.assert_error(self.installer("--apply"), "not a regular file")
        self.config.rmdir()
        self.config.parent.rmdir()
        self.config.parent.write_text("not a directory")
        self.assert_error(self.installer("--apply"), "not a directory")
        self.assertFalse(self.desktop.exists())

    def test_each_missing_source_is_preflighted_before_any_asset_write(self):
        repo = self.root / "fake-repo"
        assets = ("scripts/clipboard_history.py", "config/voxtype.toml", "desktop/voxtype-record-toggle.desktop")
        for missing in assets:
            with self.subTest(missing=missing):
                for name in assets:
                    target = repo / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((ROOT / name).read_bytes())
                (repo / missing).unlink()
                with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(install, "REPO", repo):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(install.main(["--apply"]), 1)
                self.assertEqual(self.snapshot(), {})

    def test_relative_xdg_directories_are_rejected(self):
        for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
            with self.subTest(variable=variable):
                env = dict(self.env, **{variable: "relative/path"})
                self.assert_error(self.installer("--apply", env=env), "must be an absolute")
        self.assertEqual(self.snapshot(), {})

    def test_history_command_expansion_and_installation_respect_spaced_xdg_paths(self):
        data_home = self.home / "data with spaces and $literal"
        env = dict(self.env, XDG_DATA_HOME=str(data_home))
        self.assert_ok(self.installer("--apply", env=env))
        expected = data_home / "voxtype-setup/clipboard_history.py"
        self.assertEqual(expected.read_bytes(), (ROOT / "scripts/clipboard_history.py").read_bytes())
        self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o600)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(install.xdg_paths()["history_helper"], expected)
        # Expand the command's quoted argument using a harmless shell builtin,
        # never invoke the helper or even Python from the configured command.
        self.assertTrue(install.HISTORY_COMMAND.startswith("exec /usr/bin/python3 "))
        shell = install.HISTORY_COMMAND.replace("exec /usr/bin/python3 ", "printf '%s\\n' ", 1)
        cases = ((env, expected),
                 (dict(env, XDG_DATA_HOME=""), self.home / ".local/share/voxtype-setup/clipboard_history.py"),
                 ({key: value for key, value in env.items() if key != "XDG_DATA_HOME"},
                  self.home / ".local/share/voxtype-setup/clipboard_history.py"))
        for case_env, path in cases:
            with self.subTest(data_home=case_env.get("XDG_DATA_HOME")):
                result = subprocess.run(["/bin/sh", "-c", shell], env=case_env, input="", capture_output=True, text=True, timeout=5)
                self.assert_ok(result)
                self.assertEqual(result.stdout, str(path) + "\n")
        self.assertEqual(self.commands(), [install.SHORTCUT_QUERY])

    def test_home_fallbacks_are_used_without_xdg_variables(self):
        env = {key: value for key, value in self.env.items() if key not in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")}
        self.assert_ok(self.installer("--apply", env=env))
        self.assertTrue((self.home / ".config/voxtype/config.toml").is_file())
        self.assertTrue((self.home / ".local/share/applications/voxtype-record-toggle.desktop").is_file())
        self.assertTrue((self.home / ".local/share/voxtype-setup/clipboard_history.py").is_file())

    def test_relative_home_fallback_is_rejected(self):
        env = dict(self.env, HOME="relative", XDG_CONFIG_HOME="")
        self.assert_error(self.installer("--apply", env=env), "HOME is not absolute")
        self.assertEqual(self.snapshot(), {})

    def test_shortcut_owner_ours_accepts_four_line_response(self):
        for action in ("action_launch", "_launch"):
            with self.subTest(action=action):
                self.env["TEST_SHORTCUT"] = f"voxtype-record-toggle.desktop\n{action}\nVoxtype Record Toggle\nLaunch\n"
                result = self.installer("--apply")
                self.assert_ok(result)
                self.assertIn("SHORTCUT OURS", result.stdout)

    def test_shortcut_empty_response_is_free(self):
        for response in ("", "\n\n\n\n"):
            with self.subTest(response=repr(response)):
                self.env["TEST_SHORTCUT"] = response
                result = self.installer("--apply")
                self.assert_ok(result)
                self.assertIn("SHORTCUT FREE", result.stdout)
                self.assertIn("not a binding guarantee", result.stdout)

    def test_other_component_or_other_action_is_a_collision(self):
        for component, action in (("org.kde.plasmashell", "activate widget"), ("voxtype-record-toggle.desktop", "other_action")):
            with self.subTest(component=component, action=action):
                self.env["TEST_SHORTCUT"] = f"{component}\n{action}\nFriendly name\nFriendly action\n"
                result = self.installer("--apply")
                self.assert_error(result, "shortcut collision")
                self.assertIn("SHORTCUT OTHER", result.stdout)
                self.assertEqual(self.snapshot(), {})

    def test_unknown_shortcut_never_claims_free_and_requires_explicit_skip(self):
        for response, code in (("malformed\n", "0"), ("", "1")):
            with self.subTest(response=response, code=code):
                self.env.update(TEST_SHORTCUT=response, TEST_QDBUS_EXIT=code)
                plan = self.installer()
                self.assert_ok(plan)
                self.assertIn("SHORTCUT UNKNOWN", plan.stdout)
                self.assertNotIn("SHORTCUT FREE", plan.stdout)
                self.assert_error(self.installer("--apply"), "--skip-shortcut-check")
                self.assertEqual(self.snapshot(), {})
        result = self.installer("--apply", "--skip-shortcut-check")
        self.assert_ok(result)
        self.assertIn("not a forced assignment", result.stdout)
        self.assertIn("custom assignments are left intact", result.stdout)
        self.assertTrue(self.config.exists())

    def test_no_bus_or_qdbus_is_unknown_not_failure_of_dry_run(self):
        env = dict(self.env)
        env.pop("DBUS_SESSION_BUS_ADDRESS")
        result = self.installer(env=env)
        self.assert_ok(result)
        self.assertIn("SHORTCUT UNKNOWN", result.stdout)
        self.assertEqual(self.commands(), [])
        (self.bin / "qdbus6").unlink()
        self.assert_error(self.installer("--apply"), "--skip-shortcut-check")
        self.assertEqual(self.snapshot(), {})

    def test_refresh_requires_apply_and_available_tool_before_writes(self):
        self.assert_error(self.installer("--refresh-desktop"), "requires --apply")
        (self.bin / "kbuildsycoca6").unlink()
        self.assert_error(self.installer("--apply", "--refresh-desktop"), "needs kbuildsycoca6")
        self.assertEqual(self.snapshot(), {})
        self.assertFalse(any(command[0] == "kbuildsycoca6" for command in self.commands()))

    def test_refresh_is_explicit_and_runs_after_apply(self):
        result = self.installer("--apply", "--refresh-desktop")
        self.assert_ok(result)
        self.assertIn("REFRESHED", result.stdout)
        self.assertIn("may discover shortcut hints automatically", result.stdout)
        self.assertIn("no explicit D-Bus registration or service restart", result.stdout)
        self.assertNotIn("no D-Bus registration", result.stdout)
        self.assertEqual(self.commands(), [install.SHORTCUT_QUERY, ["kbuildsycoca6"]])
        self.assertTrue(self.config.exists())
        self.assertFalse((self.home / "config/kglobalshortcutsrc").exists())

    def test_refresh_failure_reports_files_and_backups_remain(self):
        self.install_assets()
        self.config.write_text("original before refresh")
        self.env["TEST_REFRESH_EXIT"] = "3"
        result = self.installer("--apply", "--replace", "--refresh-desktop")
        self.assert_error(result, "AFTER apply")
        self.assertIn("no rollback", result.stderr)
        self.assertEqual(self.config.read_bytes(), (ROOT / "config/voxtype.toml").read_bytes())
        backup = self.backup_for(next(self.backups.iterdir()), self.config)
        self.assertEqual(backup.read_text(), "original before refresh")


class DoctorTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        # Installer tests keep the default unassigned response. Doctor readiness
        # requires an actual binding, not merely an available shortcut.
        self.env["TEST_SHORTCUT"] = "voxtype-record-toggle.desktop\n_launch\nVoxtype Record Toggle\nLaunch\n"

    def test_environment_overrides_are_warned_not_treated_as_toml(self):
        self.install_assets()
        for name, value in (("VOXTYPE_OUTPUT_MODE", "paste"),
                            ("VOXTYPE_FALLBACK_TO_CLIPBOARD", "true"),
                            ("VOXTYPE_AUTO_SUBMIT", "true")):
            with self.subTest(name=name):
                env = dict(self.env, **{name: value})
                result = self.run_script("doctor.py", env=env)
                self.assert_ok(result)
                self.assertIn("TOML on disk, not effective runtime settings", result.stdout)
                self.assertIn("WARN: environment overrides", result.stdout)
                self.assertIn(name, result.stdout)
                self.assertIn("on-disk typing-driver policy", result.stdout)
        self.assertFalse(any(command[0] in ("wl-copy", "wl-paste", "xclip", "ydotool")
                             for command in self.commands()))

    def test_environment_warning_never_discloses_values(self):
        self.install_assets()
        env = dict(self.env, VOXTYPE_APPEND_TEXT="PRIVATE_ENVIRONMENT_VALUE_MUST_NOT_PRINT")
        result = self.run_script("doctor.py", env=env)
        self.assert_ok(result)
        self.assertIn("VOXTYPE_APPEND_TEXT", result.stdout)
        self.assertNotIn("PRIVATE_ENVIRONMENT_VALUE_MUST_NOT_PRINT", result.stdout + result.stderr)
        self.assertIn("values and daemon environment are not inspected", result.stdout)

    def test_healthy_installed_config_is_readonly_and_reports_no_contents(self):
        self.install_assets()
        with self.config.open("a") as config:
            config.write('\n# PRIVATE_USER_CONFIG_MUST_NOT_PRINT\n')
        before = self.snapshot()
        result = self.run_script("doctor.py")
        self.assert_ok(result)
        self.assertIn("0 error(s)", result.stdout)
        self.assertIn("microphone: system default", result.stdout)
        self.assertIn("finite u32 ceiling", result.stdout)
        self.assertIn("model base.en: installed", result.stdout)
        self.assertIn("backend=vulkan", result.stdout)
        self.assertIn("installed desktop entry: recipe Type, Exec and Terminal verified (not launched)", result.stdout)
        self.assertIn("OK: KDE shortcut ours", result.stdout)
        self.assertNotIn("PRIVATE_", result.stdout + result.stderr)
        self.assertNotIn("/private/user/path", result.stdout)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.commands(), [
            *(["voxtype", "--config", str(self.config), "info", kind, "--json"]
              for kind in ("engines", "models", "variants", "accel")),
            ["systemctl", "--user", "show", "voxtype.service", "--no-pager",
             "--property=LoadState,ActiveState,SubState,FragmentPath"],
            install.SHORTCUT_QUERY,
        ])

    def test_missing_installed_desktop_entry_is_a_readonly_failure(self):
        self.install_assets()
        self.desktop.unlink()
        before = self.snapshot()
        result = self.run_script("doctor.py")
        self.assert_error(result, "installed desktop entry missing")
        self.assertIn("installer plan (not launched)", result.stdout)
        self.assertEqual(before, self.snapshot())

    def test_invalid_or_mismatched_desktop_entry_never_launches_or_prints_it(self):
        self.install_assets()
        baseline = self.desktop.read_text()
        self.fake_command("private-desktop-launcher")
        entries = {
            "missing Exec": baseline.replace("Exec=voxtype record toggle\n", ""),
            "colon Exec": baseline.replace("Exec=voxtype record toggle", "Exec:voxtype record toggle"),
            "colon Type": baseline.replace("Type=Application", "Type:Application"),
            "colon Terminal": baseline.replace("Terminal=false", "Terminal:false"),
            "changed Exec": baseline.replace("Exec=voxtype record toggle", "Exec=private-desktop-launcher PRIVATE_EXEC_ARGUMENT %F"),
            "wrong Type": baseline.replace("Type=Application", "Type=Link"),
            "terminal": baseline.replace("Terminal=false", "Terminal=true"),
            "missing Terminal": baseline.replace("Terminal=false\n", ""),
            "missing group": baseline.replace("[Desktop Entry]", "[Other Group]"),
            "malformed": "PRIVATE_DESKTOP_CONTENT_MUST_NOT_PRINT\n",
            "duplicate key": baseline + "Exec=PRIVATE_DUPLICATE_EXEC\n",
            "INI defaults": "[DEFAULT]\nType=Application\nExec=voxtype record toggle\nTerminal=false\n[Desktop Entry]\nName=Not the recipe\n",
            "invalid encoding": b"\xffPRIVATE_DESKTOP_CONTENT_MUST_NOT_PRINT\n",
        }
        for kind, entry in entries.items():
            with self.subTest(kind=kind):
                self.desktop.write_bytes(entry.encode() if isinstance(entry, str) else entry)
                before = self.snapshot()
                result = self.run_script("doctor.py")
                self.assert_error(result, "ERROR: installed desktop entry")
                self.assertIn("not launched", result.stdout)
                self.assertNotIn("PRIVATE_", result.stdout + result.stderr)
                self.assertNotIn("private-desktop-launcher", result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(before, self.snapshot())
        self.assertEqual({command[0] for command in self.commands()}, {"voxtype", "systemctl", "qdbus6"})

    def test_desktop_entry_check_allows_unrelated_presentation_changes(self):
        self.install_assets()
        self.desktop.write_text(self.desktop.read_text().replace("Name=Voxtype Record Toggle", "Name=My dictation toggle")
                                + "\n# PRIVATE_DESKTOP_COMMENT_MUST_NOT_PRINT\n")
        result = self.run_script("doctor.py")
        self.assert_ok(result)
        self.assertIn("installed desktop entry: recipe Type, Exec and Terminal verified", result.stdout)
        self.assertNotIn("PRIVATE_", result.stdout + result.stderr)
        self.assertNotIn("My dictation toggle", result.stdout)

    def test_unsafe_desktop_paths_are_refused_without_reading_or_running_tools(self):
        self.install_assets()
        original = self.desktop.read_bytes()
        outside = self.root / "outside-applications"
        outside.mkdir()
        target = outside / self.desktop.name
        target.write_bytes(original)
        self.desktop.unlink()
        for kind in ("file symlink", "parent symlink", "directory"):
            with self.subTest(kind=kind):
                if kind == "file symlink":
                    self.desktop.symlink_to(target)
                elif kind == "parent symlink":
                    self.desktop.parent.rmdir()
                    self.desktop.parent.symlink_to(outside, target_is_directory=True)
                else:
                    self.desktop.mkdir()
                with mock.patch.dict(os.environ, self.env, clear=True), \
                        mock.patch.object(doctor.Path, "open", side_effect=AssertionError("unsafe entry was opened")), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    report = doctor.Reporter()
                    doctor.desktop_entry(report)
                self.assertEqual(report.errors, 1, output.getvalue())
                self.assertIn("installed desktop entry missing, unsafe", output.getvalue())
                self.assertEqual(self.commands(), [])
                if kind == "file symlink":
                    self.desktop.unlink()
                elif kind == "parent symlink":
                    self.desktop.parent.unlink()
                    self.desktop.parent.mkdir()
                else:
                    self.desktop.rmdir()
        self.assertEqual(target.read_bytes(), original)

    def test_known_history_hook_requires_each_clipboard_dependency_without_invoking_it(self):
        self.install_assets()
        for command in ("wl-copy", "wl-paste", "qdbus6"):
            with self.subTest(command=command):
                tool = self.bin / command
                original = tool.read_bytes()
                tool.unlink()
                before = self.snapshot()
                result = self.run_script("doctor.py")
                self.assert_error(result, f"prerequisite: {command} missing")
                self.assertIn("typing: configured eitype available", result.stdout)
                self.assertEqual(before, self.snapshot())
                tool.write_bytes(original)
                tool.chmod(0o700)
        self.assertEqual({command[0] for command in self.commands()}, {"voxtype", "systemctl", "qdbus6"})
        self.assertTrue(all(command == install.SHORTCUT_QUERY for command in self.commands() if command[0] == "qdbus6"))

    def test_missing_installed_history_helper_is_readonly_failure_but_typing_is_still_checked(self):
        self.install_assets()
        self.helper.unlink()
        before = self.snapshot()
        result = self.run_script("doctor.py")
        self.assert_error(result, "installed history helper missing, unsafe")
        self.assertIn("typing: configured eitype available", result.stdout)
        self.assertIn("not read or executed", result.stdout)
        self.assertEqual(before, self.snapshot())
        self.assertEqual({command[0] for command in self.commands()}, {"voxtype", "systemctl", "qdbus6"})

    def test_helper_check_inspects_metadata_only_not_program_contents(self):
        self.install_assets()
        self.helper.write_text("raise RuntimeError('PRIVATE_HELPER_CONTENT_MUST_NOT_PRINT')\n")
        before = self.snapshot()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(Path, "open", side_effect=AssertionError("helper contents must not be read")), \
                mock.patch.object(doctor, "run_readonly", side_effect=AssertionError("no helper execution")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            report = doctor.Reporter()
            doctor.installed_history_helper(report)
        self.assertEqual(report.errors, 0, output.getvalue())
        self.assertIn("private regular file verified (not read or executed)", output.getvalue())
        self.assertNotIn("PRIVATE_", output.getvalue())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.commands(), [])

    def test_unsafe_installed_history_helper_is_refused_without_reading(self):
        self.install_assets()
        original = self.helper.read_bytes()
        outside = self.root / "outside-helper"
        outside.write_bytes(original)
        self.helper.unlink()
        for kind in ("symlink", "directory", "fifo", "public", "executable", "foreign owner", "writable directory", "parent symlink"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.helper.symlink_to(outside)
                elif kind == "directory":
                    self.helper.mkdir()
                elif kind == "fifo":
                    os.mkfifo(self.helper, 0o600)
                elif kind == "parent symlink":
                    self.helper.parent.rmdir()
                    other_parent = self.root / "outside-helper-directory"
                    other_parent.mkdir(mode=0o700)
                    (other_parent / self.helper.name).write_bytes(original)
                    (other_parent / self.helper.name).chmod(0o600)
                    self.helper.parent.symlink_to(other_parent, target_is_directory=True)
                else:
                    self.helper.write_bytes(original)
                    self.helper.chmod(0o644 if kind == "public" else 0o700 if kind == "executable" else 0o600)
                    if kind == "writable directory":
                        self.helper.parent.chmod(0o770)
                uid = os.getuid() + 1 if kind == "foreign owner" else os.getuid()
                with mock.patch.dict(os.environ, self.env, clear=True), \
                        mock.patch.object(doctor.os, "getuid", return_value=uid), \
                        mock.patch.object(Path, "open", side_effect=AssertionError("unsafe helper must not be read")), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    report = doctor.Reporter()
                    doctor.installed_history_helper(report)
                self.assertEqual(report.errors, 1, output.getvalue())
                self.assertIn("installed history helper missing, unsafe", output.getvalue())
                if kind == "directory":
                    self.helper.rmdir()
                elif kind == "parent symlink":
                    self.helper.parent.unlink()
                    self.helper.parent.mkdir(mode=0o700)
                else:
                    self.helper.unlink()
                    self.helper.parent.chmod(0o700)
        self.assertEqual(outside.read_bytes(), original)
        self.assertEqual(self.commands(), [])

    def test_custom_postprocessor_is_qualified_not_executed_or_given_history_dependencies(self):
        self.install_assets()
        self.config.write_text(self.config.read_text().replace(install.HISTORY_COMMAND, "private-postprocessor PRIVATE_ARGUMENT"))
        self.fake_command("private-postprocessor")
        self.helper.unlink()
        for name in ("wl-copy", "wl-paste", "qdbus6"):
            (self.bin / name).unlink()
        before = self.snapshot()
        result = self.run_script("doctor.py")
        self.assert_ok(result)
        self.assertIn("post-processor: custom command; unknown side effects", result.stdout)
        self.assertIn("typing-driver protection does not cover this hook", result.stdout)
        self.assertIn("typing-only driver chain and fallback disabled; these output drivers do not write the clipboard", result.stdout)
        self.assertNotIn("pure direct typing; no clipboard writes", result.stdout)
        self.assertNotIn("preserve the active clipboard", result.stdout)
        self.assertNotIn("PRIVATE_", result.stdout + result.stderr)
        self.assertNotIn("private-postprocessor", result.stdout + result.stderr)
        self.assertNotIn("prerequisite: wl-", result.stdout)
        self.assertNotIn("installed history helper", result.stdout)
        self.assertEqual(before, self.snapshot())
        self.assertEqual({command[0] for command in self.commands()}, {"voxtype", "systemctl"})
        config = tomllib.loads(self.config.read_text())
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(doctor.shutil, "which", wraps=doctor.shutil.which) as which, \
                contextlib.redirect_stdout(io.StringIO()):
            doctor.prerequisites(config, doctor.Reporter())
        self.assertFalse({call.args[0] for call in which.call_args_list} & {"wl-copy", "wl-paste", "qdbus6", "xclip"})

    def test_opted_out_protected_typing_needs_no_clipboard_tools_or_helper(self):
        self.install_assets()
        self.disable_history()
        self.helper.unlink()
        for name in ("wl-copy", "wl-paste", "xclip", "wtype"):
            (self.bin / name).unlink()
        before = self.snapshot()
        result = self.run_script("doctor.py")
        self.assert_ok(result)
        self.assertIn("OK: output: direct typing baseline", result.stdout)
        self.assertIn("pure direct typing; no clipboard writes or history insertion", result.stdout)
        self.assertNotIn("profile", result.stdout)
        self.assertNotIn("clipboard tool", result.stdout)
        self.assertNotIn("clipboard fallback tool", result.stdout)
        self.assertIn("0 error(s), 0 warning(s)", result.stdout)
        self.assertEqual(before, self.snapshot())
        self.assertEqual({command[0] for command in self.commands()}, {"voxtype", "systemctl", "qdbus6"})

    def test_opted_out_protected_typing_does_not_even_probe_clipboard_tools(self):
        self.install_assets()
        self.disable_history()
        config = tomllib.loads(self.config.read_text())
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(doctor.shutil, "which", wraps=doctor.shutil.which) as which:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                report = doctor.Reporter()
                self.assertTrue(doctor.prerequisites(config, report))
        self.assertEqual(report.errors, 0, output.getvalue())
        self.assertFalse({call.args[0] for call in which.call_args_list} & {"wl-copy", "wl-paste", "xclip", "qdbus6"})
        self.assertEqual(self.commands(), [])

    def test_no_session_environment_is_warning_not_false_failure(self):
        self.install_assets()
        env = dict(self.env)
        env.pop("DBUS_SESSION_BUS_ADDRESS")
        result = self.run_script("doctor.py", env=env)
        self.assert_ok(result)
        self.assertIn("vendor service: unknown", result.stdout)
        self.assertIn("KDE shortcut unknown", result.stdout)
        self.assertNotIn("KDE shortcut free", result.stdout)
        self.assertTrue(all(command[0] == "voxtype" for command in self.commands()))

    def test_invalid_bus_failure_is_unknown_not_free(self):
        self.install_assets()
        self.env.update(TEST_QDBUS_EXIT="1", TEST_SYSTEMCTL_EXIT="1")
        result = self.run_script("doctor.py")
        self.assert_ok(result)
        self.assertIn("vendor service: unknown", result.stdout)
        self.assertIn("KDE shortcut unknown", result.stdout)

    def test_zero_duration_is_a_clear_failure(self):
        self.install_assets()
        self.config.write_text(self.config.read_text().replace("max_duration_secs = 4294967295", "max_duration_secs = 0"))
        self.assert_error(self.run_script("doctor.py"), "NOT unlimited")

    def test_missing_config_invalid_toml_and_wrong_table_are_failures(self):
        self.assert_error(self.run_script("doctor.py"), "installed config missing")
        self.install_assets()
        self.config.write_text("not valid TOML at all")
        self.assert_error(self.run_script("doctor.py"), "invalid TOML")
        self.config.write_text("audio = false\n")
        result = self.run_script("doctor.py")
        self.assert_error(result, "audio must be a TOML table")
        self.assertNotIn("Traceback", result.stderr)

    def test_wayland_paste_and_clipboard_require_wl_copy_even_with_xclip(self):
        self.install_assets()
        self.env["WAYLAND_DISPLAY"] = "wayland-fake"
        xclip = self.bin / "xclip"
        xclip.write_bytes((self.bin / "wl-copy").read_bytes())
        xclip.chmod(0o700)
        (self.bin / "wl-copy").unlink()
        baseline = self.config.read_text()
        for mode in ("paste", "clipboard"):
            with self.subTest(mode=mode):
                self.config.write_text(baseline.replace('mode = "type"', f'mode = "{mode}"'))
                self.assert_error(self.run_script("doctor.py"), "missing wl-copy (Wayland recipe)")
        self.assertFalse(any(command[0] in ("xclip", "wl-copy") for command in self.commands()))

    def test_wayland_type_fallback_warns_when_only_xclip_is_present(self):
        self.install_assets()
        self.disable_history()
        self.config.write_text(self.config.read_text().replace("fallback_to_clipboard = false", "fallback_to_clipboard = true"))
        self.env["WAYLAND_DISPLAY"] = "wayland-fake"
        (self.bin / "wl-copy").unlink()
        result = self.run_script("doctor.py")
        self.assert_ok(result)
        self.assertIn("WARN: output: clipboard fallback enabled", result.stdout)
        self.assertIn("typing failure can overwrite the current clipboard", result.stdout)
        self.assertIn("WARN: clipboard fallback tool missing", result.stdout)
        self.assertNotIn("preserve the active clipboard", result.stdout)
        self.assertFalse(any(command[0] in ("xclip", "wl-copy") for command in self.commands()))

    def test_explicit_fallback_or_clipboard_driver_warns_even_with_tools_present(self):
        self.install_assets()
        baseline = self.config.read_text()
        for change in (lambda text: text.replace("fallback_to_clipboard = false", "fallback_to_clipboard = true"),
                       lambda text: text.replace('driver_order = ["eitype", "dotool", "ydotool"]',
                                                 'driver_order = ["eitype", "clipboard"]'),
                       lambda text: text.replace('driver_order = ["eitype", "dotool", "ydotool"]',
                                                 'driver_order = ["eitype", "xclip"]')):
            with self.subTest(config=change(baseline)):
                self.config.write_text(change(baseline))
                result = self.run_script("doctor.py")
                self.assert_ok(result)
                self.assertIn("can overwrite the current clipboard", result.stdout)
                self.assertNotIn("preserve the active clipboard", result.stdout)
                self.assertNotIn("tool missing", result.stdout)
        self.assertFalse(any(command[0] in ("xclip", "wl-copy", "wl-paste") for command in self.commands()))

    def test_missing_required_commands_is_a_failure_without_execution(self):
        self.install_assets()
        self.disable_history()
        for name in ("voxtype", "voxtype-osd-gtk4", "wtype", "eitype", "dotool", "ydotool", "wl-copy"):
            (self.bin / name).unlink()
        result = self.run_script("doctor.py")
        self.assert_error(result, "prerequisite: voxtype missing")
        self.assertIn("voxtype-osd-gtk4 missing", result.stdout)
        self.assertIn("no configured KDE-compatible typing tool available", result.stdout)
        self.assertIn("no usable fallback", result.stdout)
        self.assertNotIn("missing wl-copy", result.stdout)
        self.assertTrue(all(command[0] in ("systemctl", "qdbus6") for command in self.commands()))

    def test_frontend_is_discovered_beside_resolved_packaged_binary(self):
        self.install_assets()
        packaged = self.root / "fake-package/usr/lib/voxtype"
        packaged.mkdir(parents=True)
        (self.bin / "voxtype").rename(packaged / "voxtype")
        (self.bin / "voxtype").symlink_to(packaged / "voxtype")
        (self.bin / "voxtype-osd-gtk4").rename(packaged / "voxtype-osd-gtk4")
        before = self.snapshot()
        result = self.run_script("doctor.py")
        self.assert_ok(result)
        self.assertIn("prerequisite: voxtype-osd-gtk4 available", result.stdout)
        self.assertEqual(before, self.snapshot())
        self.assertEqual({command[0] for command in self.commands()}, {"voxtype", "systemctl", "qdbus6"})

    def test_frontend_on_path_is_used_before_packaged_sibling_lookup(self):
        self.install_assets()
        config = tomllib.loads(self.config.read_text())
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(doctor.Path, "resolve", side_effect=AssertionError("unexpected sibling lookup")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                report = doctor.Reporter()
                self.assertTrue(doctor.prerequisites(config, report))
        self.assertEqual(report.errors, 0, output.getvalue())
        self.assertIn("prerequisite: voxtype-osd-gtk4 available", output.getvalue())
        self.assertEqual(self.commands(), [])

    def test_missing_frontend_does_not_fall_back_to_host_paths(self):
        self.install_assets()
        (self.bin / "voxtype-osd-gtk4").unlink()
        result = self.run_script("doctor.py")
        self.assert_error(result, "prerequisite: voxtype-osd-gtk4 missing")
        self.assertFalse(any(command[0].startswith("voxtype-osd") for command in self.commands()))

    def test_packaged_frontend_must_be_an_executable_file(self):
        self.install_assets()
        frontend = self.bin / "voxtype-osd-gtk4"
        frontend.chmod(0o600)
        with self.subTest(kind="non-executable file"):
            self.assert_error(self.run_script("doctor.py"), "prerequisite: voxtype-osd-gtk4 missing")
        frontend.unlink()
        frontend.mkdir()
        with self.subTest(kind="directory"):
            self.assert_error(self.run_script("doctor.py"), "prerequisite: voxtype-osd-gtk4 missing")
        self.assertFalse(any(command[0].startswith("voxtype-osd") for command in self.commands()))

    def test_inactive_or_missing_vendor_service_is_a_failure(self):
        self.install_assets()
        for status in ("LoadState=loaded\nActiveState=inactive", "LoadState=not-found\nActiveState=inactive"):
            with self.subTest(status=status):
                self.env["TEST_SERVICE"] = status
                self.assert_error(self.run_script("doctor.py"), "ERROR: vendor service")

    def test_unavailable_selected_model_or_engine_is_a_failure(self):
        self.install_assets()
        self.env["TEST_INFO_MODELS"] = json.dumps({"engines": {"whisper": {"models": [{"name": "base.en", "installed": False, "problems": ["PRIVATE_MODEL_PATH"]}]}}})
        result = self.run_script("doctor.py")
        self.assert_error(result, "missing or damaged")
        self.assertNotIn("PRIVATE_MODEL_PATH", result.stdout)
        self.env.pop("TEST_INFO_MODELS")
        self.env["TEST_INFO_ENGINES"] = '[{"name":"whisper","compiled":false}]'
        self.assert_error(self.run_script("doctor.py"), "not compiled")

    def test_unavailable_packaged_info_is_warning(self):
        self.install_assets()
        self.env["TEST_INFO_EXIT"] = "2"
        result = self.run_script("doctor.py")
        self.assert_ok(result)
        self.assertIn("unavailable/unsupported; unknown", result.stdout)

    def test_unassigned_shortcut_is_a_doctor_failure_with_settings_guidance(self):
        self.install_assets()
        before = self.snapshot()
        for response in ("", "\n\n\n\n"):
            with self.subTest(response=repr(response)):
                self.env["TEST_SHORTCUT"] = response
                result = self.run_script("doctor.py")
                self.assert_error(result, "ERROR: KDE shortcut free")
                self.assertIn("System Settings > Keyboard > Shortcuts", result.stdout)
                self.assertIn("Voxtype Record Toggle", result.stdout)
                self.assertIn("Add New > Application", result.stdout)
                self.assertIn("assign Meta+Space and Apply", result.stdout)
                self.assertNotIn("Command or Script", result.stdout)
                self.assertEqual(before, self.snapshot())

    def test_shortcut_ours_is_ready_for_each_supported_launch_action(self):
        self.install_assets()
        for action in ("_launch", "action_launch"):
            with self.subTest(action=action):
                self.env["TEST_SHORTCUT"] = f"voxtype-record-toggle.desktop\n{action}\nPRIVATE_OWNER_NAME\nPRIVATE_ACTION_NAME\n"
                result = self.run_script("doctor.py")
                self.assert_ok(result)
                self.assertIn("OK: KDE shortcut ours", result.stdout)
                self.assertNotIn("PRIVATE_", result.stdout + result.stderr)

    def test_shortcut_collision_is_a_doctor_failure(self):
        self.install_assets()
        self.env["TEST_SHORTCUT"] = "other.desktop\naction\nPRIVATE_OWNER_NAME\nPRIVATE_ACTION_NAME\n"
        result = self.run_script("doctor.py")
        self.assert_error(result, "KDE shortcut other")
        self.assertNotIn("PRIVATE_", result.stdout)

    def test_type_is_final_output_with_best_effort_history_not_live_fragments(self):
        self.install_assets()
        result = self.run_script("doctor.py")
        self.assert_ok(result)
        self.assertIn("OK: output: direct typing baseline", result.stdout)
        self.assertIn("one complete final-on-stop dictation, not live fragments", result.stdout)
        self.assertIn("typing-only driver chain and fallback disabled; these output drivers do not write the clipboard", result.stdout)
        self.assertIn("history hook: known save/capture/guarded-restore helper selected; never invoked by doctor", result.stdout)
        self.assertIn("WARN: history recovery: best-effort plain-text save/capture/guarded restore, not atomic", result.stdout)
        self.assertIn("no supported Klipper history-only insertion API", result.stdout)
        self.assertIn("Rich, absent or unreadable offers are skipped", result.stdout)
        self.assertIn("detected newer copies and restoration can limit recovery; undetected races remain", result.stdout)
        self.assertNotIn("recovery is not provided", result.stdout)
        self.assertIn("0 error(s), 1 warning(s)", result.stdout)
        self.assertNotIn("paste baseline", result.stdout)
        self.assertNotIn("copied dictation retained", result.stdout)
        self.assertFalse(any(command[0] in ("wl-copy", "wl-paste", "xclip", "ydotool", "wtype", "eitype", "dotool") for command in self.commands()))

    def test_each_configured_kde_typing_tool_is_accepted_alone(self):
        self.install_assets()
        self.disable_history()
        for name in ("wtype", "eitype", "ydotool", "dotool", "wl-copy", "wl-paste", "xclip"):
            (self.bin / name).unlink()
        for driver in ("eitype", "dotool", "ydotool"):
            with self.subTest(driver=driver):
                fake = self.fake_command(driver)
                result = self.run_script("doctor.py")
                self.assert_ok(result)
                self.assertIn(f"typing: configured {driver} available", result.stdout)
                self.assertIn("permissions/socket/focus not tested (never invoked)", result.stdout)
                fake.unlink()
        self.assertFalse(any(command[0] in ("eitype", "dotool", "ydotool") for command in self.commands()))

    def test_type_rejects_wtype_on_kde_and_ignores_unconfigured_typing_tools(self):
        self.install_assets()
        baseline = self.config.read_text()
        for drivers in ('["wtype"]', '["eitype"]', '[]'):
            with self.subTest(drivers=drivers):
                self.config.write_text(baseline.replace('driver_order = ["eitype", "dotool", "ydotool"]',
                                                        f'driver_order = {drivers}'))
                (self.bin / "eitype").unlink(missing_ok=True)
                result = self.run_script("doctor.py")
                self.assert_error(result, "no configured KDE-compatible typing tool available")
                self.assertIn("no usable fallback", result.stdout)
                if drivers == '["wtype"]':
                    self.assertIn("wtype is unsupported on KDE Plasma Wayland", result.stdout)
        self.assertFalse(any(command[0] in ("wtype", "dotool", "ydotool", "wl-copy", "wl-paste", "xclip") for command in self.commands()))

    def test_paste_is_a_recognized_alternative_that_writes_the_current_clipboard(self):
        self.install_assets()
        self.config.write_text(self.config.read_text().replace('mode = "type"', 'mode = "paste"'))
        result = self.run_script("doctor.py")
        self.assert_ok(result)
        self.assertIn("WARN: output: alternative paste mode writes the current clipboard", result.stdout)
        self.assertIn("not a history-only write or archive", result.stdout)
        self.assertNotIn("preserve the active clipboard", result.stdout)
        self.assertNotIn("paste: shortcut differs", result.stdout)
        self.assertFalse(any(command[0] in ("wl-copy", "wl-paste", "eitype", "ydotool") for command in self.commands()))

    def test_paste_restoration_is_not_reported_as_lossless_preservation(self):
        self.install_assets()
        self.config.write_text(self.config.read_text().replace('mode = "type"', 'mode = "paste"')
                               .replace("auto_submit = false", "auto_submit = false\nrestore_clipboard = true"))
        result = self.run_script("doctor.py")
        self.assert_ok(result)
        self.assertIn("alternative paste mode writes the current clipboard", result.stdout)
        self.assertIn("best-effort restoration is not lossless clipboard preservation", result.stdout)
        self.assertIn("newline/MIME changes, races and failure paths", result.stdout)
        self.assertNotIn("preserve the active clipboard", result.stdout)
        self.assertFalse(any(command[0] in ("wl-copy", "wl-paste", "xclip") for command in self.commands()))

    def test_paste_accepts_each_kde_keystroke_tool_alone(self):
        self.install_assets()
        self.config.write_text(self.config.read_text().replace('mode = "type"', 'mode = "paste"'))
        for name in ("wtype", "eitype", "ydotool", "dotool"):
            (self.bin / name).unlink()
        for driver in ("eitype", "ydotool"):
            with self.subTest(driver=driver):
                fake = self.fake_command(driver)
                result = self.run_script("doctor.py")
                self.assert_ok(result)
                self.assertIn(f"paste keystroke tool: {driver} available", result.stdout)
                self.assertIn("permissions/socket/focus not tested (never invoked)", result.stdout)
                fake.unlink()
        self.assertFalse(any(command[0] in ("eitype", "ydotool", "wl-copy", "wl-paste") for command in self.commands()))

    def test_paste_rejects_wtype_or_dotool_as_the_only_keystroke_tool(self):
        self.install_assets()
        self.config.write_text(self.config.read_text().replace('mode = "type"', 'mode = "paste"'))
        for name in ("wtype", "eitype", "ydotool", "dotool"):
            (self.bin / name).unlink()
        for driver in ("wtype", "dotool"):
            with self.subTest(driver=driver):
                fake = self.fake_command(driver)
                result = self.run_script("doctor.py")
                self.assert_error(result, "paste keystroke tool: missing eitype or ydotool")
                self.assertIn("wtype alone is not supported by KDE Plasma Wayland", result.stdout)
                fake.unlink()
        self.assertFalse(any(command[0] in ("wtype", "dotool", "wl-copy", "wl-paste") for command in self.commands()))

    def test_cpu_only_backend_is_a_supported_operator_choice(self):
        self.install_assets()
        self.env["TEST_INFO_ACCEL"] = '{"state":"cpu-only","backend":"cpu"}'
        result = self.run_script("doctor.py")
        self.assert_ok(result)
        self.assertIn("OK: runtime acceleration: cpu-only, backend=cpu", result.stdout)
        self.assertIn("0 error(s), 1 warning(s)", result.stdout)
        self.assertNotIn("WARN: runtime acceleration", result.stdout)


class ConfigSemanticsTests(unittest.TestCase):
    def config(self):
        return tomllib.loads((ROOT / "config/voxtype.toml").read_text())

    def test_global_protected_typing_baseline_has_no_paste_profile_or_auto_submission(self):
        config = self.config()
        self.assertEqual(config["engine"], "whisper")
        self.assertEqual(config["whisper"]["model"], "base.en")
        self.assertEqual(config["whisper"]["language"], "en")
        self.assertFalse(config["whisper"]["eager_processing"])
        self.assertFalse(config["whisper"]["on_demand_loading"])
        self.assertEqual(config["audio"]["device"], "default")
        self.assertEqual(config["audio"]["sample_rate"], 16000)
        self.assertEqual(config["audio"]["max_duration_secs"], 4294967295)
        self.assertFalse(config["hotkey"]["enabled"])
        self.assertEqual(config["hotkey"]["mode"], "toggle")
        self.assertEqual(config["output"]["mode"], "type")
        self.assertEqual(config["output"]["driver_order"], ["eitype", "dotool", "ydotool"])
        self.assertNotIn("clipboard", config["output"]["driver_order"])
        self.assertNotIn("xclip", config["output"]["driver_order"])
        self.assertIs(config["output"]["fallback_to_clipboard"], False)
        self.assertNotIn("paste_keys", config["output"])
        self.assertNotIn("restore_clipboard", config["output"])
        self.assertNotIn("restore_clipboard_delay_ms", config["output"])
        self.assertIs(config["output"]["auto_submit"], False)
        self.assertIs(config["text"]["smart_auto_submit"], False)
        self.assertNotIn("profiles", config)
        self.assertTrue(config["osd"]["enabled"])
        self.assertEqual(config["osd"]["frontend"], "gtk4")
        self.assertFalse(config["vad"]["enabled"])
        results = doctor.config_semantics(config)
        self.assertFalse(any(level == "ERROR" for level, _ in results))
        warnings = [text for level, text in results if level == "WARN"]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith("history recovery:"))

    def test_stock_postprocessor_contract_matches_installer_and_has_cleanup_budget(self):
        text = (ROOT / "config/voxtype.toml").read_text()
        self.assertIn("command = '" + install.HISTORY_COMMAND + "'", text)
        hook = self.config()["output"]["post_process"]
        self.assertEqual(hook, {"command": install.HISTORY_COMMAND, "timeout_ms": 5000,
                                "trim": False, "fallback_on_empty": True})
        self.assertEqual(doctor.history_hook(self.config()["output"]), "known")

    def test_known_history_hook_rejects_unsafe_settings_without_exposing_values(self):
        for field, value in (("trim", True), ("trim", "PRIVATE_TRIM"), ("trim", None),
                             ("timeout_ms", 0), ("timeout_ms", 4999), ("timeout_ms", True),
                             ("timeout_ms", "PRIVATE_TIMEOUT"), ("timeout_ms", None),
                             ("fallback_on_empty", False), ("fallback_on_empty", "PRIVATE_FALLBACK"),
                             ("fallback_on_empty", None)):
            with self.subTest(field=field, value=value):
                config = self.config()
                hook = config["output"]["post_process"]
                if value is None:
                    del hook[field]
                else:
                    hook[field] = value
                before = dict(hook)
                results = doctor.config_semantics(config)
                self.assertTrue(any(level == "ERROR" and ("post_process." + field) in text for level, text in results))
                self.assertNotIn("PRIVATE_", repr(results))
                self.assertEqual(hook, before)
        config = self.config()
        config["output"]["post_process"]["timeout_ms"] = 6000
        self.assertFalse(any(level == "ERROR" for level, _ in doctor.config_semantics(config)))

    def test_absent_custom_and_invalid_postprocessors_do_not_make_blanket_safety_promises(self):
        config = self.config()
        del config["output"]["post_process"]
        results = doctor.config_semantics(config)
        self.assertFalse(any(level in ("ERROR", "WARN") for level, _ in results))
        self.assertTrue(any("pure direct typing; no clipboard writes" in text for _, text in results))
        for value in (False, [], {}, {"command": 5}, {"command": ""}, {"command": "  "}):
            with self.subTest(value=value):
                config["output"]["post_process"] = value
                results = doctor.config_semantics(config)
                self.assertIn(("ERROR", "config: output.post_process must be a table with a nonempty command string"), results)
                self.assertFalse(any("pure direct typing; no clipboard writes" in text for _, text in results))
        config["output"]["post_process"] = {"command": "PRIVATE_CUSTOM_COMMAND"}
        results = doctor.config_semantics(config)
        self.assertTrue(any(level == "WARN" and "unknown side effects" in text for level, text in results))
        self.assertNotIn("PRIVATE_", repr(results))
        self.assertFalse(any("pure direct typing; no clipboard writes" in text for _, text in results))
        self.assertFalse(any("known save/capture" in text for _, text in results))

    def test_invalid_durations_and_short_finite_ceiling(self):
        for duration in (0, -1, 4294967296, True, "unlimited"):
            with self.subTest(duration=duration):
                config = self.config()
                config["audio"]["max_duration_secs"] = duration
                self.assertTrue(any(level == "ERROR" and text.startswith("duration:") for level, text in doctor.config_semantics(config)))
        config["audio"]["max_duration_secs"] = 120
        self.assertIn(("WARN", "duration: a shorter finite timeout is configured; RAM still grows while recording"), doctor.config_semantics(config))

    def test_wrong_setting_types_are_clear_errors(self):
        for section, field, value in (("hotkey", "enabled", "false"), ("audio", "device", 7),
                                       ("osd", "enabled", "true"), ("output", "restore_clipboard", "false"),
                                       ("output", "auto_submit", "false"), ("text", "smart_auto_submit", "false"),
                                       ("output", "mode", []), ("output", "fallback_to_clipboard", "false"),
                                       ("output", "paste_keys", False)):
            with self.subTest(section=section, field=field):
                config = self.config()
                config[section][field] = value
                self.assertIn(("ERROR", f"config: {section}.{field} has the wrong value type"), doctor.config_semantics(config))

    def test_invalid_driver_orders_are_errors_without_dumping_values(self):
        for drivers in (False, "eitype", [7], [{}], ["PRIVATE_UNKNOWN_DRIVER"], ["Eitype"]):
            with self.subTest(drivers=drivers):
                config = self.config()
                config["output"]["driver_order"] = drivers
                results = doctor.config_semantics(config)
                self.assertIn(("ERROR", "config: output.driver_order must be an array of supported driver names"), results)
                self.assertNotIn("PRIVATE_UNKNOWN_DRIVER", repr(results))
                self.assertFalse(any("preserve the active clipboard" in text for _, text in results))

    def test_clipboard_drivers_and_fallback_warn_without_changing_user_choices(self):
        for changes in ({"fallback_to_clipboard": True}, {"driver_order": ["eitype", "clipboard"]},
                        {"driver_order": ["eitype", "xclip"]}, {"driver_order": None}):
            with self.subTest(changes=changes):
                config = self.config()
                config["output"].update(changes)
                if config["output"]["driver_order"] is None:
                    # Voxtype's implicit default chain includes clipboard and xclip;
                    # disabling the separate fallback flag does not remove them.
                    config["output"].pop("driver_order")
                before = dict(config["output"])
                results = doctor.config_semantics(config)
                self.assertTrue(any(level == "WARN" and "can overwrite the current clipboard" in text
                                    for level, text in results))
                self.assertFalse(any("preserve the active clipboard" in text for _, text in results))
                self.assertEqual(config["output"], before)

    def test_alternative_modes_remain_recognized_without_clipboard_safety_claims(self):
        for mode in ("paste", "clipboard", "file"):
            with self.subTest(mode=mode):
                config = self.config()
                config["output"]["mode"] = mode
                results = doctor.config_semantics(config)
                self.assertFalse(any(level == "ERROR" for level, _ in results))
                self.assertTrue(any(level == "WARN" and text.startswith("output: alternative") for level, text in results))
                self.assertFalse(any("preserve the active clipboard" in text for _, text in results))
                self.assertEqual(config["output"]["mode"], mode)

    def test_auto_submission_warns_if_either_setting_is_enabled(self):
        for section, field in (("output", "auto_submit"), ("text", "smart_auto_submit")):
            with self.subTest(setting=f"{section}.{field}"):
                config = self.config()
                config[section][field] = True
                self.assertTrue(any(level == "WARN" and text.startswith("auto-submit: enabled")
                                    for level, text in doctor.config_semantics(config)))
                self.assertIs(config[section][field], True)

    def test_deviations_are_reported_without_resetting_choices(self):
        config = self.config()
        config["audio"]["device"] = "PRIVATE_DEVICE_NAME"
        config["hotkey"]["enabled"] = True
        config["osd"]["enabled"] = False
        config["output"].update(mode="paste", restore_clipboard=True)
        config["whisper"]["eager_processing"] = True
        results = doctor.config_semantics(config)
        self.assertTrue(any("restoration is not lossless clipboard preservation" in text for _, text in results))
        self.assertTrue(any("unified streaming" in text for _, text in results))
        self.assertTrue(any("OSD: disabled" in text for _, text in results))
        self.assertNotIn("PRIVATE_DEVICE_NAME", repr(results))
        self.assertEqual(config["audio"]["device"], "PRIVATE_DEVICE_NAME")


SAMPLE_FAKE = r'''
import json
import os
from pathlib import Path
import sys
import tempfile

name = Path(sys.argv[0]).name
args = sys.argv[1:]
repo = Path(os.environ["TEST_SAMPLE_REPO"])
with open(os.environ["TEST_COMMAND_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "dirname":
    assert args == ["--", str(repo / "scripts/record-sample.sh")], args
    print(repo / "scripts")
elif name == "mkdir":
    assert args == ["-p", "--", str(repo / "tmp/samples")], args
    (repo / "tmp/samples").mkdir(parents=True)
elif name == "mktemp":
    assert args == ["-d", str(repo / "tmp/samples/sample-XXXXXXXX")], args
    print(tempfile.mkdtemp(prefix="sample-", dir=repo / "tmp/samples"))
elif name == "chmod":
    assert len(args) == 2 and args[0] == "700", args
    path = Path(args[1])
    assert path.parent == repo / "tmp/samples", args
    path.chmod(0o700)
elif name == "arecord":
    assert args[:-1] == ["-D", "default", "-t", "wav", "-f", "S16_LE", "-r", "16000", "-c", "1"], args
    path = Path(args[-1])
    assert path.name == "sample.wav" and path.parent.parent == repo / "tmp/samples", args
    path.write_bytes(b"FAKE AUDIO: no microphone was opened")
else:
    raise AssertionError((name, args))
'''


class SampleTests(IsolatedTest):
    def sample(self, *args):
        # Pipes, never a PTY. Even a regression cannot reach a real arecord:
        # PATH contains only the fake that rejects all invocations.
        return subprocess.run(["/bin/bash", str(ROOT / "scripts/record-sample.sh"), *args],
                              env=self.env, input="", capture_output=True, text=True, timeout=10)

    def test_help_never_opens_microphone_or_creates_files(self):
        before = self.snapshot()
        result = self.sample("--help")
        self.assert_ok(result)
        self.assertIn("Ctrl+C", result.stdout)
        self.assertIn("Nothing is played", result.stdout)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.commands(), [])

    def test_noninteractive_invocation_is_refused(self):
        result = self.sample()
        self.assert_error(result, "Refusing noninteractive recording")
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.snapshot(), {})

    def test_unknown_argument_is_not_a_recording_trigger(self):
        result = self.sample("--yes")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.commands(), [])

    def interactive_sample(self, confirmation):
        # A copied script and fake recorder/filesystem commands keep even the
        # consented path inside the temporary test tree, without real audio.
        repo = self.root / "sample repo"
        (repo / "scripts").mkdir(parents=True)
        script = repo / "scripts/record-sample.sh"
        script.write_bytes((ROOT / "scripts/record-sample.sh").read_bytes())
        (repo / "config").mkdir()
        (repo / "config/voxtype.toml").write_bytes((ROOT / "config/voxtype.toml").read_bytes())
        for name in ("dirname", "mkdir", "mktemp", "chmod", "arecord"):
            self.fake_command(name, SAMPLE_FAKE)
        env = dict(self.env, TEST_SAMPLE_REPO=str(repo))
        master, slave = pty.openpty()
        try:
            with subprocess.Popen(["/bin/bash", str(script)], stdin=slave, stdout=slave,
                                  stderr=slave, env=env, cwd=self.root) as process:
                os.close(slave)
                slave = None
                os.write(master, confirmation.encode())
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
                chunks = []
                while True:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                result = subprocess.CompletedProcess(process.args, process.returncode,
                                                     b"".join(chunks).decode(), "")
        finally:
            if slave is not None:
                os.close(slave)
            os.close(master)
        return repo, result

    def test_tty_confirmation_captures_only_fake_audio_privately_and_prints_one_command(self):
        repo, result = self.interactive_sample("\n")
        self.assert_ok(result)
        audio, = (repo / "tmp/samples").glob("sample-*/sample.wav")
        self.assertEqual(stat.S_IMODE(audio.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(audio.stat().st_mode), 0o600)
        self.assertEqual(audio.read_bytes(), b"FAKE AUDIO: no microphone was opened")
        examples = [line.strip() for line in result.stdout.splitlines() if line.startswith("  voxtype ")]
        self.assertEqual(len(examples), 1, result.stdout)
        self.assertEqual(shlex.split(examples[0]), ["voxtype", "--config", str(repo / "config/voxtype.toml"),
                                                  "transcribe", str(audio), ">", str(audio.parent / "transcript.txt")])
        self.assertFalse((audio.parent / "transcript.txt").exists())
        self.assertEqual([command[0] for command in self.commands()],
                         ["dirname", "mkdir", "mktemp", "chmod", "arecord"])
        self.assertNotIn("parakeet", result.stdout.lower())
        self.assertNotIn("avx", result.stdout.lower())

    def test_tty_other_input_cancels_without_audio_or_sample_directory(self):
        repo, result = self.interactive_sample("no\n")
        self.assert_ok(result)
        self.assertIn("Cancelled; microphone was not opened", result.stdout)
        self.assertFalse((repo / "tmp").exists())
        self.assertEqual([command[0] for command in self.commands()], ["dirname"])


class DocumentReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value is not None:
                if name in ("href", "src"):
                    self.links.append(value)
                elif name == "id":
                    self.ids.add(value)


class PublicAssetsTests(unittest.TestCase):
    def test_public_assets_are_generic_and_have_no_retired_profile_or_private_references(self):
        assets = [ROOT / name for name in ("README.md", "AGENTS.md", "LICENSE", "Makefile")]
        for directory in ("config", "desktop", "docs", ".github", "scripts"):
            assets.extend(path for path in (ROOT / directory).rglob("*")
                          if path.is_file() and "__pycache__" not in path.parts)
        forbidden = re.compile(r"/home/|/Users/|file://|\.worktrees/|"
                               r"emacs-paste|benchmark-summary\.json|parakeet-batch\.toml|"
                               r"live-config-backup|live-verification|during this task|on this machine|this machine's",
                               re.IGNORECASE)
        hardware = re.compile(r"\b(?:AMD|Ryzen|AVX[-_ ]?512)\b", re.IGNORECASE)
        for path in assets:
            with self.subTest(asset=str(path.relative_to(ROOT))):
                text = path.read_text()
                self.assertIsNone(forbidden.search(text), str(path.relative_to(ROOT)))
                # Contributor policy may name the capabilities it forbids assuming.
                if path.name != "AGENTS.md":
                    self.assertIsNone(hardware.search(text), str(path.relative_to(ROOT)))
        self.assertFalse((ROOT / "config/parakeet-batch.toml").exists())
        self.assertFalse((ROOT / "docs/benchmark-summary.json").exists())

    def test_public_document_links_stay_in_the_repo_and_resolve(self):
        documents = [ROOT / "README.md", *(ROOT / "docs").rglob("*.html"),
                     *(ROOT / "docs").rglob("*.svg")]
        for document in documents:
            text = document.read_text()
            parsed = DocumentReferences()
            if document.suffix == ".md":
                links = re.findall(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)", text)
            else:
                parsed.feed(text)
                links = parsed.links
            for link in links:
                with self.subTest(document=str(document.relative_to(ROOT)), link=link):
                    url = urlsplit(link)
                    self.assertIn(url.scheme, ("", "https", "http", "mailto"))
                    if url.scheme:
                        continue  # External sources are allowed; no network checks in offline tests.
                    self.assertFalse(url.netloc, "Local documentation must not use protocol-relative URLs")
                    target = (document.parent / unquote(url.path)).resolve() if url.path else document.resolve()
                    self.assertTrue(target.is_relative_to(ROOT.resolve()), "Link escapes the repository")
                    self.assertTrue(target.exists(), "Local document/asset does not exist")
                    if url.fragment and target.suffix in (".html", ".svg"):
                        destination = DocumentReferences()
                        destination.feed(target.read_text())
                        self.assertIn(unquote(url.fragment), destination.ids, "Local anchor does not exist")

    def test_desktop_toggle_has_no_profile_or_machine_specific_arguments(self):
        text = (ROOT / "desktop/voxtype-record-toggle.desktop").read_text()
        self.assertIn("\nType=Application\n", text)
        self.assertIn("\nName=Voxtype Record Toggle\n", text)
        self.assertIn("\nExec=voxtype record toggle\n", text)
        self.assertIn("\nTerminal=false\n", text)
        self.assertIn("\nNoDisplay=false\n", text)
        self.assertIn("\nCategories=Utility;\n", text)
        self.assertNotIn("--profile", text)
        self.assertIn("\nX-KDE-GlobalAccel-CommandShortcut=true\n", text)
        self.assertIn("\nX-KDE-Shortcuts=Meta+Space\n", text)


if __name__ == "__main__":
    unittest.main()
