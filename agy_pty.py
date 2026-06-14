#!/usr/bin/env python
"""Run Antigravity CLI print mode through a Windows PTY.

The Windows agy.exe currently drops stdout when it is launched from a
non-interactive subprocess. Running it inside ConPTY makes agy behave as if it
has a real console, while this wrapper still returns capturable text.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time


ANSI_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)
MODEL_SETTINGS_LOCK = threading.Lock()
SETTINGS_LOCK_STALE_SECONDS = 3600
MODEL_OVERRIDE_MARKER_SUFFIX = ".codex-model-override.json"
DEFAULT_PTY_ROWS = 40
DEFAULT_PTY_COLS = 240


def clean_terminal_output(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def _pty_dimensions() -> tuple[int, int]:
    rows = _env_int("AGY_PTY_ROWS", DEFAULT_PTY_ROWS, 20, 200)
    cols = _env_int("AGY_PTY_COLS", DEFAULT_PTY_COLS, 80, 1000)
    return rows, cols


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return True

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        return error != 87  # ERROR_INVALID_PARAMETER means the PID does not exist.
    exit_code = wintypes.DWORD()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _lock_owner_is_dead(lock_path: Path) -> bool:
    try:
        raw_pid = lock_path.read_text(encoding="ascii", errors="ignore").strip()
        pid = int(raw_pid)
    except (OSError, ValueError):
        return False
    return not _pid_is_running(pid)


def _restore_model_field(settings_path: Path, original_model_present: bool, original_model):
    data = _read_json(settings_path, {})
    if original_model_present:
        data["model"] = original_model
    else:
        data.pop("model", None)
    _write_json(settings_path, data)


def recover_stale_model_override(settings_path: Path) -> bool:
    """Restore a previous model override if a killed process left it behind."""

    marker_path = settings_path.with_name(settings_path.name + MODEL_OVERRIDE_MARKER_SUFFIX)
    marker = _read_json(marker_path, None)
    if not marker:
        return False

    data = _read_json(settings_path, {})
    if data.get("model") == marker.get("override_model"):
        _restore_model_field(
            settings_path,
            bool(marker.get("original_model_present")),
            marker.get("original_model"),
        )
    with contextlib.suppress(FileNotFoundError):
        marker_path.unlink()
    return True


@contextlib.contextmanager
def settings_file_lock(settings_path: Path, timeout_seconds: float = 180.0):
    lock_path = settings_path.with_name(settings_path.name + ".codex-lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
        except FileExistsError:
            with contextlib.suppress(OSError):
                if _lock_owner_is_dead(lock_path):
                    lock_path.unlink()
                    continue
                age = time.time() - lock_path.stat().st_mtime
                if age > SETTINGS_LOCK_STALE_SECONDS:
                    lock_path.unlink()
                    continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for Antigravity settings lock: {lock_path}")
            time.sleep(0.1)
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                lock_path.unlink()


@contextlib.contextmanager
def temporary_model(model: str | None):
    if not model:
        yield
        return

    settings_path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"

    with MODEL_SETTINGS_LOCK:
        with settings_file_lock(settings_path):
            recover_stale_model_override(settings_path)
            current_model = _read_json(settings_path, {}).get("model")
    if current_model == model:
        yield
        return

    with MODEL_SETTINGS_LOCK:
        with settings_file_lock(settings_path):
            recover_stale_model_override(settings_path)
            data = _read_json(settings_path, {})
            original_model_present = "model" in data
            original_model = data.get("model")
            if original_model == model:
                yield
                return
            marker_path = settings_path.with_name(settings_path.name + MODEL_OVERRIDE_MARKER_SUFFIX)
            marker = {
                "pid": os.getpid(),
                "started_at": time.time(),
                "original_model_present": original_model_present,
                "original_model": original_model,
                "override_model": model,
            }
            _write_json(marker_path, marker)
            data["model"] = model
            _write_json(settings_path, data)
            try:
                yield
            finally:
                current = _read_json(settings_path, {})
                if current.get("model") == model:
                    _restore_model_field(settings_path, original_model_present, original_model)
                with contextlib.suppress(FileNotFoundError):
                    marker_path.unlink()


def run_conpty(argv: list[str], cwd: str, timeout_seconds: float) -> tuple[int | None, str]:
    try:
        from winpty import PtyProcess
    except ImportError as exc:  # pragma: no cover - exercised by the CLI user.
        raise RuntimeError(
            "Missing dependency: pywinpty. Install it with "
            "`python -m pip install --user pywinpty`."
        ) from exc

    proc = PtyProcess.spawn(argv, cwd=cwd, dimensions=_pty_dimensions())
    chunks: list[str] = []
    errors: list[str] = []

    def reader() -> None:
        while True:
            try:
                chunk = proc.read(4096)
            except EOFError:
                break
            except Exception as exc:  # pragma: no cover - defensive for PTY backend errors.
                errors.append(f"[pty read error: {exc!r}]")
                break
            if chunk:
                chunks.append(chunk)
            if not proc.isalive():
                # Try one more read on the next loop; ConPTY may still have buffered bytes.
                continue

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout_seconds
    while thread.is_alive() and time.monotonic() < deadline:
        thread.join(0.1)

    if thread.is_alive():
        with contextlib.suppress(Exception):
            proc.terminate()
        time.sleep(0.5)
        if proc.isalive():
            with contextlib.suppress(Exception):
                proc.kill()
        thread.join(2)

    if proc.isalive():
        with contextlib.suppress(Exception):
            proc.wait()

    output = "".join(chunks + errors)
    return proc.exitstatus, output


def run_subprocess(argv: list[str], cwd: str, timeout_seconds: float) -> tuple[int | None, str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return None, output + f"\n[subprocess timeout after {timeout_seconds}s]"
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def run_pty(argv: list[str], cwd: str, timeout_seconds: float) -> tuple[int | None, str]:
    if os.name == "nt":
        try:
            return run_conpty(argv, cwd, timeout_seconds)
        except Exception as exc:  # Fallback keeps MCP diagnostics alive if pywinpty breaks.
            exitstatus, output = run_subprocess(argv, cwd, timeout_seconds)
            diagnostic = f"[pty unavailable: {type(exc).__name__}: {exc}]"
            return exitstatus, "\n".join(part for part in [diagnostic, output] if part)
    return run_subprocess(argv, cwd, timeout_seconds)


def supports_model_flag(agy: str) -> bool:
    try:
        completed = subprocess.run(
            [agy, "--help"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "--model" in ((completed.stdout or "") + (completed.stderr or ""))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run `agy --print` through ConPTY and capture its output."
    )
    parser.add_argument("prompt", nargs="?", help="Prompt to send to Antigravity.")
    parser.add_argument("--prompt-file", help="Read the prompt from a UTF-8 text file.")
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for agy.")
    parser.add_argument("--agy", default="agy", help="Path to agy executable.")
    parser.add_argument("--model", help="Temporarily set Antigravity model label.")
    parser.add_argument("--timeout", default="5m0s", help="Antigravity print timeout.")
    parser.add_argument(
        "--wall-timeout",
        type=float,
        default=360.0,
        help="Wrapper timeout in seconds.",
    )
    parser.add_argument(
        "--add-dir",
        action="append",
        default=[],
        help="Add a directory to Antigravity workspace; repeatable.",
    )
    parser.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        help="Pass through agy's permission auto-approval flag.",
    )
    parser.add_argument("--raw", action="store_true", help="Print raw PTY output.")
    parser.add_argument("--json", action="store_true", help="Emit JSON result metadata.")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_arg_parser().parse_args()
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    elif args.prompt is not None:
        prompt = args.prompt
    else:
        raise SystemExit("Provide a prompt argument or --prompt-file.")

    use_cli_model_flag = bool(args.model and supports_model_flag(args.agy))
    agy_args = [args.agy]
    if use_cli_model_flag:
        agy_args.extend(["--model", args.model])
    if args.dangerously_skip_permissions:
        agy_args.append("--dangerously-skip-permissions")
    for directory in args.add_dir:
        agy_args.append(f"--add-dir={directory}")
    agy_args.extend([f"--print={prompt}", f"--print-timeout={args.timeout}"])

    model_context = (
        contextlib.nullcontext()
        if use_cli_model_flag
        else temporary_model(args.model)
    )
    with model_context:
        exitstatus, raw_output = run_pty(agy_args, args.cwd, args.wall_timeout)

    cleaned_output = raw_output if args.raw else clean_terminal_output(raw_output)
    if args.json:
        print(
            json.dumps(
                {
                    "exitstatus": exitstatus,
                    "output": cleaned_output,
                    "rawOutput": raw_output,
                    "model": args.model,
                    "usedCliModelFlag": use_cli_model_flag,
                    "usedSettingsModelFallback": bool(args.model and not use_cli_model_flag),
                    "cwd": str(Path(args.cwd).resolve()),
                },
                ensure_ascii=False,
            )
        )
    elif cleaned_output:
        print(cleaned_output)

    return 0 if exitstatus in (0, None) else int(exitstatus)


if __name__ == "__main__":
    raise SystemExit(main())
