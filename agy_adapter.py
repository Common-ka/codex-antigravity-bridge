#!/usr/bin/env python
"""Execution adapter for Antigravity CLI."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

from agy_pty import clean_terminal_output, run_pty, temporary_model
from capabilities import detect_capabilities


def _build_print_args(
    *,
    prompt: str,
    timeout: str,
    model: str | None,
    supports_model_flag: bool,
    add_dirs: list[Path],
    dangerously_skip_permissions: bool,
) -> list[str]:
    args = ["agy"]
    if model and supports_model_flag:
        args.extend(["--model", model])
    if dangerously_skip_permissions:
        args.append("--dangerously-skip-permissions")
    for directory in add_dirs:
        args.append(f"--add-dir={directory}")
    args.extend([f"--print={prompt}", f"--print-timeout={timeout}"])
    return args


def run_agy_print(
    *,
    prompt: str,
    cwd: Path,
    model: str | None,
    timeout: str,
    wall_timeout_seconds: float,
    add_dirs: list[Path],
    dangerously_skip_permissions: bool,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run `agy -p` through the selected backend and return captured output."""

    caps = capabilities or detect_capabilities(cwd)
    supports_model_flag = bool(caps.get("supports", {}).get("model_flag"))
    args = _build_print_args(
        prompt=prompt,
        timeout=timeout,
        model=model,
        supports_model_flag=supports_model_flag,
        add_dirs=add_dirs,
        dangerously_skip_permissions=dangerously_skip_permissions,
    )
    backend = "pty" if os.name == "nt" else "subprocess"
    used_cli_model_flag = bool(model and supports_model_flag)
    model_context = (
        contextlib.nullcontext()
        if used_cli_model_flag
        else temporary_model(model)
    )

    with model_context:
        exitstatus, raw_output = run_pty(args, str(cwd), wall_timeout_seconds)

    backend_warning = None
    output_source = raw_output
    if raw_output.startswith("[pty unavailable:"):
        first_line, _separator, remainder = raw_output.partition("\n")
        backend_warning = first_line.strip()
        output_source = remainder

    return {
        "exit_status": exitstatus,
        "raw_output": raw_output,
        "output": clean_terminal_output(output_source),
        "backend": backend,
        "backend_warning": backend_warning,
        "used_cli_model_flag": used_cli_model_flag,
        "used_settings_model_fallback": bool(model and not supports_model_flag),
        "args_shape": [
            arg if not arg.startswith("--print=") else "--print=<prompt>"
            for arg in args
        ],
    }
