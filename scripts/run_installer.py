"""whispr installer (recipient side).

Invoked by install.ps1 after the bundled embeddable Python environment is
bootstrapped and whispr's pinned dependencies are installed offline. Handles
everything easier to get right in Python than PowerShell: the output-directory
prompt, the audio-device picker, an atomic targeted patch of config.yaml, the
Startup-folder auto-start shortcut, and a final smoke test.

Safe to re-run: every write is atomic (temp file + replace) and every step is
driven by real on-disk/registry state, not an assumption that this is the
first run.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import subprocess
import sys
from pathlib import Path

INSTALL_ROOT = Path(__file__).resolve().parent.parent
# Running as `python.exe scripts\run_installer.py` only puts scripts\ on
# sys.path by default -- add the install root so `import whispr` resolves
# regardless of the current working directory.
sys.path.insert(0, str(INSTALL_ROOT))

LOG_FILENAME = "install.log"


def log(install_root: Path, message: str) -> None:
    line = f"[{_dt.datetime.now().isoformat(timespec='seconds')}] {message}"
    print(line)
    with open(install_root / LOG_FILENAME, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# -- output directory ------------------------------------------------------

def prompt_output_dir(install_root: Path) -> Path:
    default = Path.home() / "Documents" / "whispr-data"
    while True:
        raw = input(
            f"\nWhere should whispr save your transcripts/recordings/logs?\n"
            f"Press Enter for the default ({default}), or type a folder path: "
        ).strip().strip('"')
        chosen = Path(raw).expanduser() if raw else default
        try:
            chosen = chosen.resolve()
            for sub in ("transcripts", "recordings", "logs"):
                (chosen / sub).mkdir(parents=True, exist_ok=True)
            log(install_root, f"output directory: {chosen}")
            return chosen
        except OSError as exc:
            print(f"Could not use that folder ({exc}). Try another path.")


# -- device picker ----------------------------------------------------------

def _list_mic_candidates() -> list[str]:
    import sounddevice as sd

    names: list[str] = []
    for d in sd.query_devices():
        if d.get("max_input_channels", 0) > 0 and d["name"] not in names:
            names.append(d["name"])
    return names


def _default_mic_name(candidates: list[str]) -> str | None:
    import sounddevice as sd

    try:
        default_idx = sd.default.device[0]
        if default_idx is not None and default_idx >= 0:
            return sd.query_devices(default_idx)["name"]
    except Exception:
        pass
    return candidates[0] if candidates else None


def _list_loopback_candidates() -> list[str]:
    import soundcard as sc

    return [m.name for m in sc.all_microphones(include_loopback=True) if getattr(m, "isloopback", False)]


def _pick_from_list(kind: str, candidates: list[str], default_name: str | None) -> str:
    print(f"\nAvailable {kind} devices on this machine:")
    for i, name in enumerate(candidates, start=1):
        marker = "   <- default" if default_name and name == default_name else ""
        print(f"  {i}. {name}{marker}")
    while True:
        raw = input(f"Pick a {kind} device by number (Enter for default): ").strip()
        if not raw:
            if default_name:
                return default_name
            print("No default available - you must pick a number.")
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw) - 1]
        print("Not a valid choice, try again.")


def run_device_picker(install_root: Path) -> tuple[str | None, str | None]:
    """Return (mic_name, loopback_name); either may be None if this machine
    has no such device at all -- callers must NOT write an empty string in
    that case (an empty substring matches every device name in capture.py)."""
    mics = _list_mic_candidates()
    if not mics:
        log(install_root, "WARNING: no microphone (input) devices detected on this machine.")
        mic_choice = None
    else:
        mic_choice = _pick_from_list("microphone", mics, _default_mic_name(mics))
        log(install_root, f"mic device selected: {mic_choice!r}")

    loopbacks = _list_loopback_candidates()
    if not loopbacks:
        log(install_root, "WARNING: no speaker/loopback capture devices detected on this machine.")
        loop_choice = None
    else:
        loop_choice = _pick_from_list("speaker/loopback", loopbacks, loopbacks[0])
        log(install_root, f"loopback device selected: {loop_choice!r}")

    return mic_choice, loop_choice


# -- config.yaml patch (targeted, atomic, comment-preserving) --------------

def _quote_yaml(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _patch_scalar(text: str, key: str, value: str) -> str:
    """Replace the first `<indent>key: <old>` line with a new value, keeping
    every other line (including comments) byte-for-byte."""
    pattern = re.compile(rf"^(\s*){re.escape(key)}:.*$")
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = pattern.match(line.rstrip("\r\n"))
        if m:
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            lines[i] = f"{m.group(1)}{key}: {_quote_yaml(value)}{newline}"
            return "".join(lines)
    raise ValueError(f"key {key!r} not found in config.yaml -- refusing to patch blindly")


def _ensure_model_cache_dir(text: str, value: str = "model") -> str:
    if re.search(r"^\s*model_cache_dir:\s*", text, re.MULTILINE):
        return text  # already patched -- idempotent re-run
    lines = text.splitlines(keepends=True)
    out = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and re.match(r"^transcription:\s*$", line.rstrip("\r\n")):
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            out.append(f"  model_cache_dir: {value}{newline}")
            inserted = True
    if not inserted:
        raise ValueError("transcription: section not found in config.yaml")
    return "".join(out)


def patch_config(
    install_root: Path,
    output_dir: Path,
    mic_name: str | None,
    loopback_name: str | None,
) -> None:
    config_path = install_root / "config.yaml"
    text = config_path.read_text(encoding="utf-8")

    text = _patch_scalar(text, "transcripts", str(output_dir / "transcripts"))
    text = _patch_scalar(text, "recordings", str(output_dir / "recordings"))
    text = _patch_scalar(text, "logs", str(output_dir / "logs"))
    if mic_name:
        text = _patch_scalar(text, "mic_name_match", mic_name)
    if loopback_name:
        text = _patch_scalar(text, "loopback_name_hint", loopback_name)
    text = _ensure_model_cache_dir(text, "model")

    # Atomic write: never leave a half-patched config.yaml if this crashes.
    tmp_path = config_path.with_suffix(".yaml.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, config_path)
    log(install_root, f"config.yaml patched (output_dir={output_dir}, mic={mic_name!r}, loopback={loopback_name!r})")


# -- Teams presence check (best-effort, informational only) ----------------

def check_teams_installed(install_root: Path) -> None:
    import shutil

    local_appdata = Path(os.environ.get("LOCALAPPDATA", ""))
    new_teams = local_appdata / "Microsoft" / "WindowsApps" / "ms-teams.exe"
    classic_teams = local_appdata / "Microsoft" / "Teams" / "current" / "Teams.exe"

    if shutil.which("ms-teams.exe") or new_teams.exists():
        log(install_root, "Teams check: new Teams client (ms-teams.exe) found.")
        return
    if classic_teams.exists():
        msg = (
            "Only the CLASSIC Teams client was found on this machine. whispr's call "
            "detection targets the NEW Teams client (ms-teams.exe) and will not "
            "record on classic Teams. See README-INSTALL.md."
        )
    else:
        msg = (
            "Could not find Microsoft Teams installed on this machine. whispr only "
            "records Teams calls/meetings -- install Teams before relying on it."
        )
    print(f"\nWARNING: {msg}")
    log(install_root, f"WARNING: {msg}")


# -- Startup-folder auto-start shortcut --------------------------------------

def create_startup_shortcut(install_root: Path) -> bool:
    import win32com.client

    startup_dir = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    shortcut_path = startup_dir / "whispr.lnk"
    # Point at the run-whispr.cmd wrapper, not pythonw.exe directly -- the
    # wrapper sets PYTHONNOUSERSITE=1 so whispr's bundled Python stays
    # isolated from any unrelated Python install the recipient's machine
    # might already have (a shortcut object can't set environment variables
    # on its own).
    launcher = install_root / "run-whispr.cmd"

    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(shortcut_path))
    shortcut.TargetPath = str(launcher)
    shortcut.WorkingDirectory = str(install_root)
    shortcut.Description = "whispr - Teams call recorder (starts at logon)"
    shortcut.Save()

    ok = shortcut_path.exists()
    log(install_root, f"Startup shortcut {'created' if ok else 'FAILED to create'}: {shortcut_path}")
    return ok


# -- smoke test ---------------------------------------------------------------

def run_smoke_test(install_root: Path) -> None:
    python_exe = install_root / "python" / "python.exe"
    print("\nRunning a 3-second test recording (checks mic + speaker capture)...")
    env = dict(os.environ, PYTHONNOUSERSITE="1")
    try:
        result = subprocess.run(
            [str(python_exe), "-m", "whispr", "record-test", "3"],
            cwd=str(install_root), capture_output=True, text=True, timeout=30, env=env,
        )
    except Exception as exc:
        print(f"Could not run the test recording: {exc}")
        log(install_root, f"SMOKE TEST: failed to launch ({exc})")
        return

    log(install_root, f"smoke test stdout: {result.stdout.strip()}")
    if result.stderr.strip():
        log(install_root, f"smoke test stderr: {result.stderr.strip()}")

    if result.returncode != 0:
        print("Test recording FAILED - whispr may not capture audio correctly on this machine.")
        log(install_root, "SMOKE TEST: FAILED (nonzero exit)")
        return

    mic_frames = re.search(r"mic_frames=(\d+)", result.stdout)
    loop_frames = re.search(r"loopback_frames=(\d+)", result.stdout)
    mic_ok = bool(mic_frames and int(mic_frames.group(1)) > 0)
    loop_ok = bool(loop_frames and int(loop_frames.group(1)) > 0)

    print(f"  Microphone capture:       {'OK' if mic_ok else 'NOT DETECTED'}")
    print(f"  Speaker/loopback capture: {'OK' if loop_ok else 'NOT DETECTED'}")
    log(install_root, f"smoke test result: mic_ok={mic_ok} loop_ok={loop_ok}")


# -- main ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", default=str(INSTALL_ROOT))
    args = parser.parse_args()
    install_root = Path(args.install_root).resolve()

    log(install_root, "=== whispr installer starting ===")
    try:
        output_dir = prompt_output_dir(install_root)
        mic_name, loop_name = run_device_picker(install_root)
        patch_config(install_root, output_dir, mic_name, loop_name)
        check_teams_installed(install_root)

        shortcut_ok = create_startup_shortcut(install_root)
        if not shortcut_ok:
            print(
                "\nWARNING: could not set up auto-start at logon. You can still run "
                "whispr manually -- see README-INSTALL.md for the manual-launch steps."
            )

        run_smoke_test(install_root)
    except Exception as exc:
        log(install_root, f"INSTALL FAILED: {exc!r}")
        print(f"\nSetup failed: {exc}")
        print("See install.log for details. You can safely run install.cmd again.")
        return 1

    log(install_root, "=== whispr installer finished successfully ===")
    print("\nSetup complete. whispr will start automatically the next time you log in.")
    print("To start it right now instead of waiting for logon, double-click:")
    print(f'  "{install_root / "run-whispr.cmd"}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
