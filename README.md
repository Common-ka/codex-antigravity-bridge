# Codex-Antigravity Bridge

This bridge allows Google Codex (or other MCP-compatible clients) to delegate tasks to the Google Antigravity CLI and receive responses back.

On Windows, `agy --print` may exit successfully but return empty stdout when executed from a non-interactive subprocess. `agy_pty.py` bypasses this limitation using ConPTY (`pywinpty`). It spawns `agy` inside a pseudo-terminal (PTY), captures the terminal output, and returns the cleaned text to the caller.

## Workflow

```text
Codex -> MCP server -> agy_pty.py -> ConPTY -> agy.exe -> Antigravity
```

The bridge detects the capabilities of the installed `agy` CLI using a capability probe. On Windows, the PTY backend is used by default because direct stdout capture for `agy -p` can return empty output even if the model responds successfully.

## Installation

```powershell
python -m pip install --user -r requirements.txt
```

Additionally, ensure you have the Antigravity CLI installed:

```powershell
where.exe agy
agy --version
agy update
```

## Quick Verification

To verify the low-level wrapper:

```powershell
python agy_pty.py "What is 2+2? Answer with one digit only." --timeout=60s --json
```

Expected `output`:

```json
"4"
```

To verify the MCP server via stdio:

```powershell
python mcp_server.py
```

When run in a standard console, this command will wait for an MCP client. For actual validation, register it in your Codex MCP config or run a test MCP client.

## Model Selection

Antigravity CLI 1.0.5+ supports the official `--model` flag:

```powershell
agy --model "Gemini 3.5 Flash (High)" -p "Reply with exactly: OK"
```

The bridge uses this flag automatically if the capability probe detects support for `--model` (default for `agy 1.0.6+`).

For older CLI versions, the bridge falls back to temporarily modifying:

```text
%USERPROFILE%\.gemini\antigravity-cli\settings.json
```

The fallback sequence is:

1. The bridge acquires a lock file near `settings.json`.
2. It backs up original settings.
3. Temporarily sets `"model": "<label>"`.
4. Runs `agy`.
5. Restores original configuration (reverting only the `model` field without overwriting other settings).

If the process is hard-killed during fallback model override, the next bridge invocation will detect the crash marker near `settings.json` and restore the original model value (if the current setting still matches the left override).

On `agy 1.0.6+`, `settings.json` is not modified for model selection.

Diagnostic example:

```powershell
python agy_pty.py "Reply with exactly: OK" --model "Gemini 3.5 Flash (High)"
```

The bridge fetches the list of models by running `agy models` via PTY. If the CLI is unavailable, it falls back to:

```text
models.json
```

## MCP Server

Codex config:

```toml
[mcp_servers.antigravity]
command = "python"
args = ["C:\\path\\to\\codex-antigravity-bridge\\mcp_server.py"]
enabled = true
required = false
startup_timeout_sec = 60
tool_timeout_sec = 600
```

Tools:

- `antigravity_delegate`: Sends a prompt to Antigravity and returns the response.
- `antigravity_delegate_async`: Starts delegation in the background and returns a `job_id`.
- `antigravity_run_status`: Checks the status of an async job.
- `antigravity_get_summary`: Gets the summary of an async job.
- `antigravity_get_result_path`: Gets the file path to the full result of an async job.
- `antigravity_smoke_test`: Verifies the installation path, expects `MCP_OK`.
- `antigravity_capabilities`: Displays `agy` version, flags/subcommands, and recommended backend.
- `antigravity_current_settings`: Displays non-sensitive bridge settings and trusted roots.
- `antigravity_list_models`: Lists model labels retrieved from `agy models` with fallback to `models.json`.

`antigravity_delegate` supports `return_mode`:

| Value | Returned to Codex |
|---|---|
| `full` | Complete response inline + writes `result.md` |
| `file_summary` | Brief summary, metadata, and path to `result.md` (full response is kept in the file) |

To save Codex tokens, use `file_summary`:

```json
{
  "return_mode": "file_summary",
  "summary_max_bullets": 5
}
```

Run artifacts are written to:

```text
<cwd>/.agentboard/antigravity/runs/
```

This directory should not be committed. The bridge automatically retains only the last 50 runs in each workspace.

Capability probe:

```json
{
  "include_live_probe": false
}
```

`include_live_probe: false` does not consume LLM quota (the bridge only reads `agy --version` and `agy --help`). `include_live_probe: true` sends a small smoke prompt and verifies direct stdout against the PTY backend.

Prompt guardrails:

- Hard limit: 12,000 characters (after adding `mode: "advise"` wrapper text).
- Warning: triggered after 8,000 characters.
- If the hard limit is exceeded, the request is not sent to Antigravity, and only a truncated preview is written to the run directory.

## Migration to Another Project

There are two migration modes: project-local and global.

### Project-Local

In this mode, the bridge resides within each project.

1. Copy the repository files into your project folder, e.g.:

```text
tools/antigravity-bridge/
```

2. Install dependencies in the new project:

```powershell
python -m pip install --user -r tools\antigravity-bridge\requirements.txt
```

3. Verify Antigravity CLI:

```powershell
where.exe agy
agy --version
```

4. Add local run folders to `.gitignore` of the new project:

```gitignore
.antigravitycli/
.agentboard/
__pycache__/
```

5. Run a smoke-test once from the root of the new project:

```powershell
python tools\antigravity-bridge\agy_pty.py "Reply with exactly: OK" --add-dir "<ABSOLUTE_PROJECT_PATH>" --timeout=60s
```

6. Add the MCP server to `%USERPROFILE%\.codex\config.toml`, substituting the project path:

```toml
[mcp_servers.antigravity]
command = "python"
args = ["<ABSOLUTE_PROJECT_PATH>\\tools\\antigravity-bridge\\mcp_server.py"]
enabled = true
required = false
startup_timeout_sec = 60
tool_timeout_sec = 600
```

7. Restart Codex to reload the MCP config.

8. Call `antigravity_smoke_test` in the new project.

### Global MCP

In this mode, the bridge is placed in a single global location, e.g.:

```text
C:\Users\username\.codex\tools\codex-antigravity-bridge\
```

The active project path is passed via `cwd`/`add_dirs` in each MCP tool call. To let the bridge know which workspaces are trusted, add environment variables to the MCP config:

```toml
[mcp_servers.antigravity]
command = "python"
args = ["C:\\Users\\username\\.codex\\tools\\codex-antigravity-bridge\\mcp_server.py"]
enabled = true
required = false
startup_timeout_sec = 60
tool_timeout_sec = 600

[mcp_servers.antigravity.env]
ANTIGRAVITY_BRIDGE_WORKSPACE_ROOT = "C:\\path\\to\\your\\project"
ANTIGRAVITY_BRIDGE_TRUSTED_ROOTS = "C:\\path\\to\\your\\project"
```

`ANTIGRAVITY_BRIDGE_WORKSPACE_ROOT` defines the default `cwd` if the tool call doesn't specify one. `ANTIGRAVITY_BRIDGE_TRUSTED_ROOTS` defines additional allowed roots; multiple paths are separated by the OS path separator (`;` on Windows).

If environment variables are not set, the fallback remains project-local:

```text
BRIDGE_DIR.parent.parent
```

Run artifacts are always written to the active workspace (i.e. `<resolved_cwd>/.agentboard/`), not next to the global bridge scripts.

## Troubleshooting

If `agy` throws a symlink error:

```text
A required privilege is not held by the client
```

Enable Windows Developer Mode and restart your terminal/Codex. Antigravity uses `.antigravitycli/` as a local project link to its settings in `%USERPROFILE%\.gemini\config\projects`.

If `antigravity_delegate` returns an empty response:

- Do not call `agy --print` directly;
- Run `agy_pty.py` smoke-test;
- Verify `pywinpty` is installed;
- Verify the Antigravity CLI is authenticated.
- If `pywinpty` is broken or missing, the bridge returns a diagnostic warning and falls back to standard subprocess execution (which may not capture stdout on Windows).

If the prompt is rejected due to size:

- Do not paste entire files;
- Pass file paths;
- Instruct Antigravity to read only specific files or sections.

If you need a different model:

- Call `antigravity_list_models`;
- Pass the exact label in `model`;
- If `agy models` fails, update the fallback `models.json`.
