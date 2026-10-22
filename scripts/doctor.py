#!/usr/bin/env python3
"""Read-only checks for the installed recipe; never records, types, or repairs."""

import argparse
import configparser
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tomllib

# Running diagnostics must not even leave an import cache in the checkout.
sys.dont_write_bytecode = True

from install import HISTORY_COMMAND, SetupError, check_path, inspect_shortcut, run_readonly, xdg_paths

U32_MAX = 4294967295
# Packaged Voxtype 1.0.1 defaults include clipboard drivers even when the
# separate fallback flag is false. Only an explicit typing-only chain is safe.
DEFAULT_DRIVER_ORDER = ("wtype", "eitype", "dotool", "ydotool", "clipboard", "xclip")
KDE_TYPING_DRIVERS = ("eitype", "dotool", "ydotool")
CLIPBOARD_DRIVERS = ("clipboard", "xclip")
ENGINES = {"whisper", "parakeet", "moonshine", "sensevoice", "paraformer",
           "dolphin", "omnilingual", "cohere", "soniox"}


def table(config, name):
    value = config.get(name, {})
    return value if isinstance(value, dict) else {}


def output_drivers(output):
    drivers = output.get("driver_order", list(DEFAULT_DRIVER_ORDER))
    if not isinstance(drivers, list) or any(not isinstance(driver, str) or driver not in DEFAULT_DRIVER_ORDER
                                           for driver in drivers):
        return None
    return drivers


def history_hook(output):
    """Recognize only the kit's exact command, without interpreting shell text."""
    if "post_process" not in output:
        return "disabled"
    hook = output["post_process"]
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str) or not hook["command"].strip():
        return "invalid"
    return "known" if hook["command"] == HISTORY_COMMAND else "custom"


def history_semantics(output):
    hook = history_hook(output)
    if hook == "disabled":
        return [("OK", "history recovery: post-processing hook absent; no history insertion configured")]
    if hook == "invalid":
        return [("ERROR", "config: output.post_process must be a table with a nonempty command string")]
    checks = []
    settings = output["post_process"]
    for field, expected in (("timeout_ms", int), ("trim", bool), ("fallback_on_empty", bool)):
        if field in settings and type(settings[field]) is not expected:
            checks.append(("ERROR", f"config: output.post_process.{field} has the wrong value type"))
    if hook == "custom":
        checks.append(("WARN", "post-processor: custom command; unknown side effects, not inspected or executed; typing-driver protection does not cover this hook"))
        return checks
    if settings.get("trim") is not False:
        checks.append(("ERROR", "history hook: output.post_process.trim must be false for the known helper"))
    timeout = settings.get("timeout_ms")
    if type(timeout) is not int or timeout < 5000:
        checks.append(("ERROR", "history hook: output.post_process.timeout_ms must be an integer of at least 5000 to leave time for guarded cleanup"))
    if settings.get("fallback_on_empty") is not True:
        checks.append(("ERROR", "history hook: output.post_process.fallback_on_empty must be true to retain the original dictation on empty output"))
    checks.append(("OK", "history hook: known save/capture/guarded-restore helper selected; never invoked by doctor"))
    checks.append(("WARN", "history recovery: best-effort plain-text save/capture/guarded restore, not atomic; Plasma 6.3.6 has no supported Klipper history-only insertion API. Rich, absent or unreadable offers are skipped; dependencies, capture, detected newer copies and restoration can limit recovery; undetected races remain"))
    return checks


def config_semantics(config):
    """Only describe settings relevant to this kit; never dump user config."""
    checks = []
    for name in ("audio", "hotkey", "output", "osd", "whisper", "text", "profiles"):
        if name in config and not isinstance(config[name], dict):
            checks.append(("ERROR", f"config: {name} must be a TOML table"))
    audio, hotkey = table(config, "audio"), table(config, "hotkey")
    output, osd = table(config, "output"), table(config, "osd")
    for name, fields in (
        ("audio", {"device": str, "sample_rate": int}),
        ("hotkey", {"enabled": bool, "mode": str}),
        ("output", {"mode": str, "paste_keys": str, "restore_clipboard": bool,
                    "fallback_to_clipboard": bool, "auto_submit": bool}),
        ("text", {"smart_auto_submit": bool}),
        ("osd", {"enabled": bool, "frontend": str}),
    ):
        section = table(config, name)
        for field, expected in fields.items():
            if field in section and type(section[field]) is not expected:
                checks.append(("ERROR", f"config: {name}.{field} has the wrong value type"))
    default_mic = audio.get("device", "default") == "default"
    checks.append(("OK" if default_mic else "WARN", "microphone: system default, re-resolved per recording" if default_mic
                   else "microphone: fixed/custom device configured; not changed or opened"))
    manual = hotkey.get("enabled", True) is False and hotkey.get("mode", "push-to-talk") == "toggle"
    checks.append(("OK" if manual else "WARN", "hotkey: KDE-owned manual toggle; evdev hotkey disabled" if manual
                   else "hotkey: differs from KDE-only manual toggle; check for duplicate listeners"))
    duration = audio.get("max_duration_secs", 60)
    if type(duration) is not int or not 0 <= duration <= U32_MAX:
        checks.append(("ERROR", "duration: expected an integer in the u32 range"))
    elif duration == 0:
        checks.append(("ERROR", "duration: zero times out immediately in Voxtype 1.0.1; it is NOT unlimited"))
    elif duration == U32_MAX:
        checks.append(("OK", "duration: finite u32 ceiling (~136 years), practical manual stop; RAM grows ~230 MB/hour plus processing"))
    else:
        checks.append(("WARN", "duration: a shorter finite timeout is configured; RAM still grows while recording"))
    drivers = output_drivers(output)
    if drivers is None:
        checks.append(("ERROR", "config: output.driver_order must be an array of supported driver names"))
    mode = output.get("mode", "type")
    if mode == "type":
        checks.append(("OK", "output: direct typing baseline; one complete final-on-stop dictation, not live fragments"))
        if drivers is not None and any(driver in CLIPBOARD_DRIVERS for driver in drivers):
            checks.append(("WARN", "output: clipboard driver in typing chain can overwrite the current clipboard, even with fallback_to_clipboard=false"))
        if output.get("fallback_to_clipboard", True) is True:
            checks.append(("WARN", "output: clipboard fallback enabled; typing failure can overwrite the current clipboard"))
        elif output.get("fallback_to_clipboard") is False and drivers and not any(driver in CLIPBOARD_DRIVERS for driver in drivers):
            checks.append(("OK", "on-disk typing-driver policy: typing-only driver chain and fallback disabled; these output drivers do not write the clipboard"))
            if history_hook(output) == "disabled":
                checks.append(("OK", "on-disk clipboard policy: pure direct typing; no clipboard writes or history insertion in the configured output pipeline"))
    elif mode == "paste":
        checks.append(("WARN", "output: alternative paste mode writes the current clipboard, then pastes one complete final-on-stop dictation; not a history-only write or archive"))
        if output.get("restore_clipboard", False) is True:
            checks.append(("WARN", "paste: best-effort restoration is not lossless clipboard preservation (newline/MIME changes, races and failure paths)"))
    elif mode == "clipboard":
        checks.append(("WARN", "output: alternative clipboard mode overwrites the current clipboard; not history-only insertion"))
    elif mode == "file":
        checks.append(("WARN", "output: alternative file mode configured; no simultaneous typing or Klipper history insertion"))
        if not output.get("file_path"):
            checks.append(("WARN", "file: no output.file_path; Voxtype falls back to writing the current clipboard"))
    else:
        checks.append(("ERROR", "output: unsupported mode"))
    checks.extend(history_semantics(output))
    if output.get("auto_submit", False) or table(config, "text").get("smart_auto_submit", False):
        checks.append(("WARN", "auto-submit: enabled; dictation may press Enter instead of leaving submission to you"))
    if osd.get("enabled", False):
        frontend = osd.get("frontend", "gtk4")
        checks.append(("OK" if frontend == "gtk4" else "WARN", "OSD: GTK4 enabled" if frontend == "gtk4"
                       else "OSD: non-GTK4 frontend configured; preserved"))
    else:
        checks.append(("WARN", "OSD: disabled; no recording overlay expected"))
    if table(config, "whisper").get("eager_processing", False):
        checks.append(("WARN", "eager processing is enabled; this kit does not promise unified streaming output"))
    return checks


class Reporter:
    def __init__(self):
        self.errors = 0
        self.warnings = 0

    def say(self, level, text):
        self.errors += level == "ERROR"
        self.warnings += level == "WARN"
        print(f"{level}: {text}")


def installed_history_helper(report):
    """Inspect metadata only: never import, read, or run the installed helper."""
    try:
        path = xdg_paths()["history_helper"]
        check_path(path)
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SetupError("helper must be user-owned and 0600")
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022:
            raise SetupError("helper directory must be user-owned and not writable by others")
    except (SetupError, OSError):
        report.say("ERROR", "installed history helper missing, unsafe, or not private (user-owned 0600); run the installer plan (not read or executed)")
        return
    report.say("OK", "installed history helper: private regular file verified (not read or executed)")


def prerequisites(config, report):
    def required(command, packaged_binary=None):
        found = shutil.which(command)
        if found is None and packaged_binary is not None:
            # Debian keeps frontends beside the resolved binary, outside PATH.
            candidate = Path(packaged_binary).resolve().parent / command
            if candidate.is_file() and os.access(candidate, os.X_OK):
                found = str(candidate)
        report.say("OK" if found else "ERROR", f"prerequisite: {command} {'available' if found else 'missing'}")
        return found

    voxtype = required("voxtype")
    osd, output = table(config, "osd"), table(config, "output")
    if osd.get("enabled", False):
        required("voxtype-osd")
        frontend = osd.get("frontend", "gtk4")
        if frontend in ("gtk4", "native", "quickshell"):
            required(f"voxtype-osd-{frontend}", packaged_binary=voxtype)
    mode = output.get("mode", "type")
    drivers = output_drivers(output) or []
    if mode == "type":
        if "wtype" in drivers:
            report.say("WARN", "typing: wtype is unsupported on KDE Plasma Wayland; not counted as a usable driver")
        typing = next((driver for driver in drivers if driver in KDE_TYPING_DRIVERS and shutil.which(driver)), None)
        clipboard_enabled = "clipboard" in drivers or output.get("fallback_to_clipboard", True) is True
        clipboard_risk = clipboard_enabled or "xclip" in drivers
        # Typing drivers need no clipboard tools on the protected path. The
        # separately selected history hook has its own dependency checks below.
        # This recipe targets Wayland: xclip is not a substitute for wl-copy.
        clipboard = bool(shutil.which("wl-copy")) if clipboard_risk else False
        if typing:
            report.say("OK", f"typing: configured {typing} available; permissions/socket/focus not tested (never invoked)")
        else:
            can_fallback = clipboard and clipboard_enabled
            report.say("WARN" if can_fallback else "ERROR", "typing: no configured KDE-compatible typing tool available (eitype, dotool or ydotool)" +
                       ("; clipboard fallback only, overwrites the current clipboard" if can_fallback else "; no usable fallback"))
        if clipboard_risk and not clipboard:
            report.say("WARN", "clipboard fallback tool missing wl-copy (Wayland recipe); configured clipboard output can overwrite the current clipboard if available (never invoked)")
    elif mode in ("paste", "clipboard"):
        clipboard = bool(shutil.which("wl-copy"))
        report.say("OK" if clipboard else "ERROR", "clipboard tool: available (never invoked)" if clipboard
                   else "clipboard tool: missing wl-copy (Wayland recipe)")
        if mode == "paste":
            paste_driver = next((driver for driver in ("eitype", "ydotool") if shutil.which(driver)), None)
            report.say("OK" if paste_driver else "ERROR",
                       f"paste keystroke tool: {paste_driver} available; permissions/socket/focus not tested (never invoked)" if paste_driver
                       else "paste keystroke tool: missing eitype or ydotool; wtype alone is not supported by KDE Plasma Wayland")
    if history_hook(output) == "known":
        for command in ("wl-copy", "wl-paste", "qdbus6"):
            required(command)
        python = Path("/usr/bin/python3")
        available = python.is_file() and os.access(python, os.X_OK)
        report.say("OK" if available else "ERROR", "history interpreter: /usr/bin/python3 " +
                   ("available (stdlib only; not invoked)" if available else "missing or not executable"))
        installed_history_helper(report)
    return voxtype is not None


def desktop_entry(report):
    """Check this recipe's launch fields without executing or printing them."""
    try:
        path = xdg_paths()["desktop"]
        check_path(path)
        entry = configparser.ConfigParser(interpolation=None, delimiters=("=",))
        entry.optionxform = str  # Desktop-entry keys are case-sensitive.
        with path.open(encoding="utf-8") as source:
            entry.read_file(source)
        expected = {"Type": "Application", "Exec": "voxtype record toggle", "Terminal": "false"}
        # KDE does not inherit INI defaults. Require the recipe fields in the
        # actual Desktop Entry group; leave unrelated presentation keys alone.
        if entry.defaults() or any(entry["Desktop Entry"].get(key) != value for key, value in expected.items()):
            report.say("ERROR", "installed desktop entry: Type, Exec or Terminal differs from this kit; review the installer plan (not launched)")
            return
    except (SetupError, OSError, UnicodeError, configparser.Error, KeyError):
        report.say("ERROR", "installed desktop entry missing, unsafe, unreadable, or invalid; run the installer plan (not launched)")
        return
    report.say("OK", "installed desktop entry: recipe Type, Exec and Terminal verified (not launched)")


def service_status(report):
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") or not shutil.which("systemctl"):
        report.say("WARN", "vendor service: unknown (no user session bus or systemctl); nothing started")
        return
    command = ["systemctl", "--user", "show", "voxtype.service", "--no-pager",
               "--property=LoadState,ActiveState,SubState,FragmentPath"]
    try:
        result = run_readonly(command)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        report.say("WARN", "vendor service: inspection unavailable; nothing started")
        return
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("LoadState") == "not-found":
        report.say("ERROR", "vendor service: voxtype.service is not installed")
    elif result.returncode or not values.get("LoadState"):
        report.say("WARN", "vendor service: unknown (user manager unavailable)")
    elif values.get("LoadState") != "loaded" or values.get("ActiveState") in ("failed", "inactive"):
        report.say("ERROR", "vendor service: not loaded/active; dictation daemon is not ready")
    elif values.get("ActiveState") == "active":
        report.say("OK", "vendor service: voxtype.service active")
    else:
        report.say("WARN", "vendor service: transitional/unknown state")
    fragment = values.get("FragmentPath", "")
    if fragment and not fragment.startswith(("/usr/lib/systemd/user/", "/lib/systemd/user/", "/usr/share/systemd/user/")):
        report.say("WARN", "service: a custom unit is in use; left untouched")


def safe_name(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.+-]{1,80}", value) else "custom/unknown"


def runtime_info(config, config_path, report):
    def info(kind):
        try:
            result = run_readonly(["voxtype", "--config", str(config_path), "info", kind, "--json"])
            if result.returncode == 0:
                return json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
            pass
        report.say("WARN", f"{kind}: read-only packaged info unavailable/unsupported; unknown")
        return None

    engine = config.get("engine", "whisper")
    if not isinstance(engine, str) or engine not in ENGINES:
        report.say("ERROR", "engine: unrecognized; cannot verify model/backend")
        return
    engines = info("engines")
    if isinstance(engines, list):
        selected = next((row for row in engines if isinstance(row, dict) and row.get("name") == engine), None)
        if selected is not None and type(selected.get("compiled")) is bool:
            compiled = selected["compiled"]
            report.say("OK" if compiled else "ERROR", f"engine {engine}: {'compiled in' if compiled else 'not compiled into the active binary'}")
        else:
            report.say("WARN", "engine: info response did not identify the selected engine")
    models = info("models")
    catalog = models.get("engines", {}).get(engine, {}) if isinstance(models, dict) and isinstance(models.get("engines"), dict) else {}
    catalog = catalog if isinstance(catalog, dict) else {}
    model = table(config, engine).get("model", catalog.get("default"))
    entries = catalog.get("models", [])
    entries = entries if isinstance(entries, list) else []
    selected = next((row for row in entries if isinstance(row, dict) and row.get("name") == model), None)
    if selected is not None and type(selected.get("installed")) is bool:
        installed = selected["installed"]
        report.say("OK" if installed else "ERROR", f"model {safe_name(model)}: {'installed (cheap integrity check, not a full hash)' if installed else 'missing or damaged; no download attempted'}")
    else:
        report.say("WARN", "model: availability unknown/custom; no download attempted")
    variants = info("variants")
    if isinstance(variants, dict):
        report.say("OK", f"binary variant: next={safe_name(variants.get('active_variant'))}, running={safe_name(variants.get('running_variant'))}; no switching")
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        report.say("WARN", "runtime acceleration: unknown outside the user session")
        return
    accel = info("accel")
    if isinstance(accel, dict):
        state = accel.get("state")
        if state in ("gpu", "cpu-only", "cpu-fallback", "not-running", "unknown"):
            report.say("OK" if state in ("gpu", "cpu-only") else "WARN", f"runtime acceleration: {state}, backend={safe_name(accel.get('backend'))}; no journal contents printed")
        else:
            report.say("WARN", "runtime acceleration: unknown response")


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    report = Reporter()
    print("Read-only doctor: no audio, clipboard contents, transcripts, repairs, or service changes.")
    print("Configuration checks describe TOML on disk, not effective runtime settings; "
          "environment/CLI overrides and the running daemon's startup state can differ.")
    overrides = sorted(name for name in os.environ if name.startswith("VOXTYPE_"))
    if overrides:
        report.say("WARN", "environment overrides may supersede the on-disk configuration: "
                   + ", ".join(overrides) + "; values and daemon environment are not inspected")
    try:
        path = xdg_paths()["config"]
        check_path(path)
        with path.open("rb") as source:
            config = tomllib.load(source)
    except (SetupError, OSError, tomllib.TOMLDecodeError):
        report.say("ERROR", "installed config missing, unsafe, unreadable, or invalid TOML; run the installer plan")
        config = None
    if config is not None:
        for level, text in config_semantics(config):
            report.say(level, text)
        if prerequisites(config, report):
            runtime_info(config, path, report)
    else:
        report.say("OK" if shutil.which("voxtype") else "ERROR", "packaged voxtype: available" if shutil.which("voxtype") else "packaged voxtype: missing")
    desktop_entry(report)
    service_status(report)
    shortcut = inspect_shortcut()
    level = "ERROR" if shortcut.state in ("free", "other") else "WARN" if shortcut.state == "unknown" else "OK"
    detail = shortcut.detail
    if shortcut.state == "free":
        detail += ("; in System Settings > Keyboard > Shortcuts, find Voxtype Record Toggle "
                   "(or Add New > Application), assign Meta+Space and Apply")
    report.say(level, f"KDE shortcut {shortcut.state}: {detail}")
    print(f"Summary: {report.errors} error(s), {report.warnings} warning(s). Unknown is not a clean bill of health.")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
