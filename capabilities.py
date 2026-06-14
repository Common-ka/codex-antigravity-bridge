#!/usr/bin/env python
"""Capability detection for Google Antigravity CLI."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from agy_pty import clean_terminal_output, run_pty


CACHE_TTL_SECONDS = 300
_CACHE: dict[tuple[str, bool], tuple[float, dict[str, Any]]] = {}


def _run_subprocess(argv: list[str], cwd: Path, timeout_seconds: float) -> tuple[int | None, str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
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


def _parse_flags(help_output: str) -> list[str]:
    flags: set[str] = set()
    for match in re.finditer(r"(?m)^\s+(--[a-zA-Z0-9][a-zA-Z0-9-]*)\b", help_output):
        flags.add(match.group(1))
    return sorted(flags)


def _parse_subcommands(help_output: str) -> list[str]:
    subcommands: set[str] = set()
    in_section = False
    for line in help_output.splitlines():
        if line.strip() == "Available subcommands:":
            in_section = True
            continue
        if not in_section:
            continue
        match = re.match(r"^\s{2,}([a-zA-Z][a-zA-Z0-9-]*)\s{2,}", line)
        if match:
            subcommands.add(match.group(1))
    return sorted(subcommands)


def _probe_live(cwd: Path, timeout_seconds: int) -> dict[str, Any]:
    prompt = "Reply with exactly: CAPABILITY_OK"
    direct_exit, direct_raw = _run_subprocess(
        ["agy", "-p", prompt, "--print-timeout", "60s"],
        cwd,
        timeout_seconds,
    )
    direct_output = clean_terminal_output(direct_raw)

    try:
        pty_exit, pty_raw = run_pty(
            ["agy", "-p", prompt, "--print-timeout", "60s"],
            str(cwd),
            timeout_seconds,
        )
    except Exception as exc:  # pragma: no cover - defensive diagnostics.
        pty_exit, pty_raw = None, f"{type(exc).__name__}: {exc}"
    pty_output = clean_terminal_output(pty_raw)

    return {
        "direct_stdout": {
            "exit_status": direct_exit,
            "ok": direct_exit in (0, None) and direct_output == "CAPABILITY_OK",
            "output": direct_output,
        },
        "pty": {
            "exit_status": pty_exit,
            "ok": pty_exit in (0, None) and pty_output == "CAPABILITY_OK",
            "output": pty_output,
        },
    }


def detect_capabilities(
    cwd: str | Path,
    *,
    include_live_probe: bool = False,
    timeout_seconds: int = 90,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return detected Antigravity CLI capabilities.

    The default probe is free of LLM calls. Set include_live_probe=True only for
    smoke diagnostics; it sends a tiny prompt to Antigravity.
    """

    resolved_cwd = Path(cwd).resolve()
    cache_key = (str(resolved_cwd), include_live_probe)
    now = time.monotonic()
    if use_cache:
        cached = _CACHE.get(cache_key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    version_exit, version_raw = _run_subprocess(["agy", "--version"], resolved_cwd, 15)
    help_exit, help_raw = _run_subprocess(["agy", "--help"], resolved_cwd, 15)
    version = clean_terminal_output(version_raw).splitlines()[0] if version_raw.strip() else None
    help_output = clean_terminal_output(help_raw)
    flags = _parse_flags(help_output)
    subcommands = _parse_subcommands(help_output)

    supports = {
        "print_flag": "--print" in flags,
        "prompt_alias": "--prompt" in flags,
        "print_timeout_flag": "--print-timeout" in flags,
        "model_flag": "--model" in flags,
        "add_dir_flag": "--add-dir" in flags,
        "sandbox_flag": "--sandbox" in flags,
        "dangerously_skip_permissions_flag": "--dangerously-skip-permissions" in flags,
        "output_format_flag": "--output-format" in flags,
        "models_subcommand": "models" in subcommands,
        "run_subcommand": "run" in subcommands,
        "app_server_subcommand": "app-server" in subcommands,
    }

    backend_recommendation = "pty" if os.name == "nt" else "subprocess"
    warnings: list[str] = []
    if os.name == "nt":
        warnings.append(
            "Windows direct stdout for `agy -p` can be empty; prefer PTY backend."
        )
    if not supports["model_flag"]:
        warnings.append("`--model` is unavailable; bridge must use settings fallback.")
    if supports["run_subcommand"]:
        warnings.append("`agy run` detected; bridge can add a future run backend.")
    if supports["app_server_subcommand"]:
        warnings.append("`agy app-server` detected; verify before using as backend.")

    result: dict[str, Any] = {
        "agy_version": version,
        "version_exit_status": version_exit,
        "help_exit_status": help_exit,
        "flags": flags,
        "subcommands": subcommands,
        "supports": supports,
        "backend_recommendation": backend_recommendation,
        "live_probe_included": include_live_probe,
        "live_probe": None,
        "warnings": warnings,
    }

    if include_live_probe:
        result["live_probe"] = _probe_live(resolved_cwd, timeout_seconds)
        direct_ok = bool(result["live_probe"]["direct_stdout"]["ok"])
        pty_ok = bool(result["live_probe"]["pty"]["ok"])
        if os.name == "nt" and not direct_ok and pty_ok:
            result["backend_recommendation"] = "pty"
        elif direct_ok:
            result["backend_recommendation"] = "subprocess"

    _CACHE[cache_key] = (now, result)
    return result
