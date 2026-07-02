# NotebookLM MCP — Claude Desktop extension (`.mcpb` / DXT)

This directory packages the [`notebooklm-py`](https://github.com/teng-lin/notebooklm-py)
MCP server as a one-click [Claude Desktop extension](https://www.anthropic.com/engineering/desktop-extensions)
(`.mcpb`, formerly DXT). Installing the bundle wires the NotebookLM tools into
Claude Desktop without any manual JSON editing.

## Contents

| File | Purpose |
|------|---------|
| `manifest.json` | The extension manifest (name, version, server command). |
| `run_server.py` | A resilient launcher that locates `uvx` and runs `uvx --from "notebooklm-py[mcp]" notebooklm-mcp`, forwarding stdin/stdout/stderr for clean JSON-RPC. |

The launcher resolves the server from PyPI on demand via `uvx`, so there is no
vendored Python environment in the bundle — only the two files above.

## Prerequisites

1. **`uv` / `uvx`** — the launcher shells out to `uvx`:
   ```bash
   # macOS / Linux
   curl -LsSf https://astral.sh/uv/install.sh | sh
   # Windows (PowerShell)
   powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
   `run_server.py` searches `PATH` plus the common install dirs
   (`~/.local/bin`, `~/.cargo/bin`, `/opt/homebrew/bin`, `/usr/local/bin`,
   `/snap/bin`, and the Windows equivalents), so a restricted host `PATH` is fine.

2. **Authenticate once** with your Google account:
   ```bash
   uvx --from "notebooklm-py[mcp]" notebooklm login
   ```
   This stores credentials under `~/.notebooklm/` for the active profile, which
   the server binds at startup.

## Build the bundle

The bundle is built with the official [`@anthropic-ai/mcpb`](https://github.com/anthropics/mcpb)
CLI (formerly `dxt`):

```bash
# From the repository root:
npx @anthropic-ai/mcpb pack desktop-extension notebooklm-mcp.mcpb
```

This produces `notebooklm-mcp.mcpb`, a zip of `manifest.json` + `run_server.py`.

> The CLI validates `manifest.json` against the DXT schema during `pack`. The
> repo's own `tests/unit/test_mcp_desktop_extension.py` asserts the manifest is
> valid JSON with the required keys and that `run_server.py` builds the correct
> `uvx` command, so the bundle stays shippable.

## Install

- **Claude Desktop:** double-click `notebooklm-mcp.mcpb`, or
  *Settings → Extensions → Install Extension…* and pick the file. Restart
  Claude Desktop; the NotebookLM tools (e.g. `notebook_list`, `chat_ask`,
  `studio_generate`) appear in the tool picker.

For other MCP clients (Claude Code, Cursor, Windsurf) that read a JSON config
instead of a `.mcpb`, use the CLI installer instead:

```bash
notebooklm mcp install claude-code   # or: cursor / windsurf / claude-desktop
```

## How the launcher passes through stdio

The MCP host talks to the server over **JSON-RPC on stdin/stdout**. `run_server.py`
therefore:

- prints **nothing** to stdout (diagnostics go to stderr only), and
- passes the host's `stdin`/`stdout`/`stderr` straight through to the child
  `uvx` process.

If `uvx` cannot be found, the launcher prints a clear install hint to **stderr**
and exits non-zero — it never contaminates the JSON-RPC channel.
