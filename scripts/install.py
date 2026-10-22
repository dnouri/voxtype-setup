#!/usr/bin/env python3
"""Plan or install three packaged-Voxtype setup assets (Python 3.11+)."""

import argparse
from dataclasses import dataclass
import difflib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

REPO = Path(__file__).absolute().parent.parent
HISTORY_COMMAND = 'exec /usr/bin/python3 "${XDG_DATA_HOME:-$HOME/.local/share}/voxtype-setup/clipboard_history.py"'
SHORTCUT_QUERY = ["qdbus6", "org.kde.kglobalaccel", "/kglobalaccel",
                  "org.kde.KGlobalAccel.action", "268435488"]


class SetupError(Exception):
    pass


def xdg_paths():
    """Reject relative environment paths rather than accidentally using cwd."""
    def directory(variable, fallback):
        value = os.environ.get(variable)
        if not value:
            home = os.environ.get("HOME", "")
            if not home or not Path(home).is_absolute():
                raise SetupError(f"{variable} is unset and HOME is not absolute")
            value = str(Path(home) / fallback)
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise SetupError(f"{variable} must be an absolute directory without '..'")
        return path

    return {
        "config": directory("XDG_CONFIG_HOME", ".config") / "voxtype/config.toml",
        "desktop": directory("XDG_DATA_HOME", ".local/share") / "applications/voxtype-record-toggle.desktop",
        "history_helper": directory("XDG_DATA_HOME", ".local/share") / "voxtype-setup/clipboard_history.py",
        "backups": directory("XDG_STATE_HOME", ".local/state") / "voxtype-setup/backups",
    }


def check_path(path, *, directory=False):
    """lstat every existing component, including dangling links; never resolve links."""
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise SetupError(f"refusing symlink: {part}")
        if part != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise SetupError(f"not a directory: {part}")
        elif not stat.S_ISREG(info.st_mode):
            raise SetupError(f"not a regular file: {part}")


def check_writable_parent(path):
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    if not os.access(parent, os.W_OK | os.X_OK):
        raise SetupError(f"parent directory is not writable: {parent}")


def make_directories(path):
    """Create missing directories privately, checking each component again."""
    check_path(path, directory=True)
    for part in reversed((path, *path.parents)):
        try:
            part.mkdir(mode=0o700)
        except FileExistsError:
            pass
        check_path(part, directory=True)


def run_readonly(command):
    """No raw command output is logged: it can contain personal information."""
    return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=10, check=False)


@dataclass(frozen=True)
class Shortcut:
    state: str
    detail: str


def inspect_shortcut():
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return Shortcut("unknown", "no session bus environment; check inside Plasma")
    if not shutil.which("qdbus6"):
        return Shortcut("unknown", "qdbus6 is unavailable")
    try:
        result = run_readonly(SHORTCUT_QUERY)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return Shortcut("unknown", "KDE ownership query could not complete")
    if result.returncode:
        return Shortcut("unknown", "KDE ownership query failed (session/service unavailable)")
    lines = result.stdout.splitlines()
    if not any(line.strip() for line in lines):
        return Shortcut("free", "Meta+Space has no reported owner")
    if len(lines) != 4 or not lines[0] or not lines[1]:
        return Shortcut("unknown", "unexpected KDE action response (expected four QStringList lines)")
    if lines[:2] == ["voxtype-record-toggle.desktop", "_launch"]:
        return Shortcut("ours", "Meta+Space already belongs to this desktop entry")
    # KDE versions normally spell the launch action '_launch'. Accept the
    # alternate action_launch spelling used by some desktop-entry callers too.
    if lines[:2] == ["voxtype-record-toggle.desktop", "action_launch"]:
        return Shortcut("ours", "Meta+Space already belongs to this desktop entry")
    return Shortcut("other", "Meta+Space belongs to another component/action; review KDE Shortcuts")


@dataclass
class Plan:
    source: Path
    destination: Path
    data: bytes
    previous: bytes | None
    mode: int
    previous_mode: int | None = None

    @property
    def action(self):
        if self.previous == self.data and self.previous_mode == self.mode:
            return "unchanged"
        return "create" if self.previous is None else "replace"


def plan_files(paths):
    plans = []
    # Publish the helper first: a new configuration must not refer to an asset
    # that this installation has not yet written. Preflight still covers all.
    for source, destination, mode in (
        (REPO / "scripts/clipboard_history.py", paths["history_helper"], 0o600),
        (REPO / "config/voxtype.toml", paths["config"], 0o600),
        (REPO / "desktop/voxtype-record-toggle.desktop", paths["desktop"], 0o644),
    ):
        check_path(source)
        check_path(destination)
        if destination == paths["history_helper"] and destination.parent.exists():
            directory = destination.parent.stat()
            if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o022:
                raise SetupError("history helper directory must be user-owned and not writable by others: "
                                 + str(destination.parent))
        data = source.read_bytes()
        previous = destination.read_bytes() if destination.exists() else None
        previous_mode = stat.S_IMODE(destination.stat().st_mode) if previous is not None else None
        if previous is not None and destination != paths["history_helper"]:
            # Keep safe existing read/write permissions, never execute, set-id,
            # or group/other write bits. The interpreter-read helper is 0600.
            mode = previous_mode & 0o644
        plan = Plan(source, destination, data, previous, mode, previous_mode)
        if plan.action != "unchanged":
            check_writable_parent(destination)
        plans.append(plan)
    return plans


def show_plan(plans):
    for plan in plans:
        print(f"{plan.action.upper()}: {plan.destination}")
        if plan.previous_mode is not None and plan.previous_mode != plan.mode:
            print(f"MODE: {plan.previous_mode:04o} -> {plan.mode:04o}")
        if plan.action == "replace":
            diff = difflib.unified_diff(
                plan.previous.decode("utf-8", errors="replace").splitlines(keepends=True),
                plan.data.decode("utf-8", errors="replace").splitlines(keepends=True),
                fromfile=str(plan.destination), tofile=str(plan.source))
            print("".join(diff), end="\n")


def preflight_backups(root):
    check_path(root, directory=True)
    check_writable_parent(root / "new-backup")
    if root.exists():
        info = root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise SetupError(f"backup directory must be owned by you and private (0700): {root}")


def atomic_write(plan):
    check_path(plan.destination)
    make_directories(plan.destination.parent)
    fd, name = tempfile.mkstemp(prefix=".voxtype-setup-", dir=plan.destination.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), plan.mode)
            output.write(plan.data)
            output.flush()
            os.fsync(output.fileno())
        check_path(plan.destination)
        os.replace(name, plan.destination)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def apply_files(plans, backup_root):
    changed = [plan for plan in plans if plan.action != "unchanged"]
    # Recheck the entire plan before creating anything, including no-op files.
    for plan in plans:
        check_path(plan.destination)
        current = plan.destination.read_bytes() if plan.destination.exists() else None
        mode = stat.S_IMODE(plan.destination.stat().st_mode) if current is not None else None
        if current != plan.previous or mode != plan.previous_mode:
            raise SetupError(f"file changed after preflight; rerun the plan: {plan.destination}")
    replaced = [plan for plan in changed if plan.action == "replace"]
    if replaced:
        preflight_backups(backup_root)
        make_directories(backup_root)
        backup_dir = Path(tempfile.mkdtemp(prefix="replacement-", dir=backup_root))
        for index, plan in enumerate(replaced, 1):
            # Destination basenames need not be unique in future recipes.
            backup = backup_dir / f"{index:02d}-{plan.destination.name}"
            with backup.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(plan.previous)
                output.flush()
                os.fsync(output.fileno())
        print(f"BACKUP: {backup_dir} (private; all originals saved before replacements)")
    installed = []
    try:
        for plan in changed:
            atomic_write(plan)
            installed.append(plan.destination)
            print(f"INSTALLED: {plan.destination}")
    except (OSError, SetupError):
        print(f"ERROR: installation stopped; {len(installed)} file(s) already installed; "
              "any backups remain. Review the plan before retrying.", file=sys.stderr)
        raise
    return bool(changed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the three planned files (default: read-only plan)")
    parser.add_argument("--replace", action="store_true", help="allow differing files to be replaced after private backups")
    parser.add_argument("--refresh-desktop", action="store_true", help="run kbuildsycoca6 after applying; requires --apply")
    parser.add_argument("--skip-shortcut-check", action="store_true", help="require manual KDE shortcut review; does not force an assignment")
    args = parser.parse_args(argv)
    if args.refresh_desktop and not args.apply:
        parser.error("--refresh-desktop requires --apply")
    try:
        paths = xdg_paths()
        plans = plan_files(paths)
        show_plan(plans)
        if any(plan.action == "replace" for plan in plans):
            preflight_backups(paths["backups"])
            if args.apply and not args.replace:
                raise SetupError("differing files require --replace; nothing was written")
            if not args.replace:
                print("NOTE: applying these replacements requires --replace and private backups.")
        if args.refresh_desktop and not shutil.which("kbuildsycoca6"):
            raise SetupError("--refresh-desktop needs kbuildsycoca6; nothing was written")
        if args.skip_shortcut_check:
            print("WARN: shortcut inspection skipped; manually review KDE Shortcuts. This is not a forced assignment.")
        else:
            shortcut = inspect_shortcut()
            print(f"SHORTCUT {shortcut.state.upper()}: {shortcut.detail}")
            if shortcut.state == "other":
                raise SetupError("shortcut collision; nothing was written")
            if shortcut.state == "unknown" and args.apply:
                raise SetupError("ownership is unknown; review KDE Shortcuts, then explicitly use --skip-shortcut-check")
        print("NOTE: desktop shortcut hint only, not a binding guarantee; existing KDE custom assignments are left intact.")
        if not args.apply:
            print("DRY RUN: no files written; use --apply to install.")
            return 0
        changed = apply_files(plans, paths["backups"])
        print("APPLIED: three assets installed." if changed else "NO-OP: all three assets are identical; no files written.")
        if args.refresh_desktop:
            try:
                result = run_readonly(["kbuildsycoca6"])
                success = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired, UnicodeError):
                success = False
            if not success:
                raise SetupError("desktop refresh failed AFTER apply; installed files and backups remain. "
                                 "Review KDE Shortcuts and retry kbuildsycoca6 manually; no rollback was attempted")
            print("REFRESHED: KDE desktop cache; KDE may discover shortcut hints automatically "
                  "(no explicit D-Bus registration or service restart).")
        else:
            print("NOTE: no desktop refresh requested; check KDE Shortcuts manually.")
        return 0
    except (SetupError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
