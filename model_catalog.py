#!/usr/bin/env python
"""Model catalog helpers for Antigravity CLI."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from agy_pty import clean_terminal_output, run_pty


CACHE_TTL_SECONDS = 300
_CLI_MODELS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _parse_model_lines(output: str) -> list[str]:
    models: list[str] = []
    seen: set[str] = set()
    ignored_patterns = [
        r"Fetching available models",
        r"^Usage:",
        r"^Flags:",
        r"^List available models$",
        r"^-h\b",
        r"^--help\b",
        r"^\[pty unavailable:",
        r"^(RuntimeError|ImportError|OSError|TimeoutError):",
    ]
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(re.search(pattern, line) for pattern in ignored_patterns):
            continue
        # Drop terminal spinner frames that can survive ANSI stripping.
        line = re.sub(r"^[\u2800-\u28ff]\s*", "", line).strip()
        if not line or any(re.search(pattern, line) for pattern in ignored_patterns):
            continue
        if len(line) > 120:
            continue
        if line not in seen:
            seen.add(line)
            models.append(line)
    return models


def list_models_from_cli(
    cwd: str | Path,
    *,
    timeout_seconds: int = 60,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return model labels from `agy models` using the PTY backend."""

    resolved_cwd = str(Path(cwd).resolve())
    now = time.monotonic()
    if use_cache:
        cached = _CLI_MODELS_CACHE.get(resolved_cwd)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    try:
        exitstatus, raw_output = run_pty(["agy", "models"], resolved_cwd, timeout_seconds)
    except Exception as exc:  # pragma: no cover - defensive diagnostics.
        exitstatus, raw_output = None, f"{type(exc).__name__}: {exc}"
    output = clean_terminal_output(raw_output)
    models = _parse_model_lines(output)
    warnings: list[str] = []
    if exitstatus not in (0, None):
        warnings.append(f"`agy models` exited with status {exitstatus}.")
    if not models:
        warnings.append("`agy models` returned no parseable model labels.")

    result = {
        "ok": exitstatus in (0, None) and bool(models),
        "models": models,
        "exit_status": exitstatus,
        "warnings": warnings,
    }
    _CLI_MODELS_CACHE[resolved_cwd] = (now, result)
    return result


def load_model_catalog(
    *,
    models_path: Path,
    settings_path: Path,
    cwd: str | Path,
    prefer_cli: bool = True,
) -> dict[str, Any]:
    """Load model labels from CLI first, with models.json/settings fallback."""

    fallback_data = _read_json(models_path, {"models": []})
    fallback_models = list(fallback_data.get("models", []))
    settings = _read_json(settings_path, {})
    current = settings.get("model")
    source = "models.json"
    warnings: list[str] = []

    models: list[dict[str, Any]] = []
    cli_result: dict[str, Any] | None = None
    if prefer_cli:
        cli_result = list_models_from_cli(cwd)
        warnings.extend(cli_result.get("warnings", []))
        if cli_result.get("ok"):
            source = "agy models"
            models = [
                {
                    "label": label,
                    "source": "agy models",
                    "notes": "Observed from local Antigravity CLI.",
                }
                for label in cli_result["models"]
            ]

    if not models:
        models = fallback_models

    if current and all(model.get("label") != current for model in models):
        models.insert(
            0,
            {
                "label": current,
                "source": "current-local-settings",
                "notes": "Read from Antigravity CLI settings.json",
            },
        )

    return {
        "models": models,
        "source": source,
        "models_path": str(models_path),
        "current_model": current,
        "cli_probe": cli_result,
        "warnings": warnings,
    }
