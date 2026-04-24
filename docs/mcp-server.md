# NotebookLM MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that wraps
[notebooklm-py](../README.md) and exposes Google NotebookLM capabilities to Claude Desktop,
Cursor, Copilot, and any other MCP-compatible AI system.

---

## Features

| Category | Capability |
|----------|-----------|
| **Tools** | List / create / delete notebooks |
| | Add URL sources (web pages, YouTube) |
| | Add plain-text sources |
| | Ask questions (chat) |
| | Generate audio podcasts (multilingual, MP3) |
| | Generate quizzes (JSON) |
| | Generate mind maps (JSON) |
| **Resources** | `notebook://{id}/metadata` – notebook + sources list |
| | `notebook://{id}/sources/{source_id}` – full ingested text |
| **Prompts** | `notebooklm_deep_research` – structured multi-step research workflow |

---

## Installation

### 1. Install the package with MCP extras

```bash
pip install "notebooklm-py[browser,mcp]"
# or with uv
uv pip install "notebooklm-py[browser,mcp]"
```

### 2. Authenticate with NotebookLM (one-time setup)

```bash
notebooklm login
```

This opens a browser window. Sign in with your Google account, then close the browser.
Credentials are saved locally in `~/.notebooklm/storage_state.json`.

---

## Running the server

```bash
python -m notebooklm_mcp
```

Or via the installed script:

```bash
notebooklm-mcp
```

The server communicates over **stdio** (standard MCP transport). All logging goes to
**stderr** only so it never interferes with the protocol.

---

## Claude Desktop integration

Add the following to your `claude_desktop_config.json`
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "notebooklm": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "notebooklm-py[browser,mcp]",
        "python",
        "-m",
        "notebooklm_mcp"
      ]
    }
  }
}
```

If you cloned the repository locally and prefer to point directly at the source:

```json
{
  "mcpServers": {
    "notebooklm": {
      "command": "uv",
      "args": [
        "run",
        "--manifest-path",
        "/absolute/path/to/notebooklm-py/pyproject.toml",
        "python",
        "-m",
        "notebooklm_mcp"
      ]
    }
  }
}
```

Restart Claude Desktop after editing the config file.

---

## Tool reference

### `notebooklm_list_notebooks`

Returns a JSON array of all notebooks.

```
[]  (no arguments)
```

### `notebooklm_create_notebook`

Creates a notebook and returns its metadata.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `title` | string | ✅ | Display title |

### `notebooklm_delete_notebook`

Permanently deletes a notebook.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `notebook_id` | string | ✅ | Notebook ID |

### `notebooklm_add_source_url`

Ingests a web URL or YouTube video URL into a notebook. Waits for processing.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `notebook_id` | string | ✅ | Target notebook ID |
| `url` | string | ✅ | URL to ingest |

### `notebooklm_add_source_text`

Ingests plain text into a notebook as a source. Waits for processing.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `notebook_id` | string | ✅ | Target notebook ID |
| `title` | string | ✅ | Source display name |
| `text` | string | ✅ | Text body |

### `notebooklm_ask_chat`

Asks a question to a notebook and returns the AI answer.

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `notebook_id` | string | ✅ | Target notebook ID |
| `query` | string | ✅ | Question or prompt |

### `notebooklm_generate_audio_podcast`

Generates an Audio Overview (podcast) for a notebook. Supports multilingual generation.
Typically takes 2–5 minutes.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `notebook_id` | string | ✅ | — | Target notebook ID |
| `instructions` | string | ❌ | none | Custom instructions for hosts |
| `language` | string | ❌ | `"en"` | BCP-47 language code |
| `download_path` | string | ❌ | temp file | Where to save the audio |
| `timeout` | float | ❌ | `360.0` | Max wait seconds |

### `notebooklm_generate_quiz`

Generates a quiz and returns questions as JSON.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `notebook_id` | string | ✅ | — | Target notebook ID |
| `instructions` | string | ❌ | none | Custom instructions |
| `timeout` | float | ❌ | `180.0` | Max wait seconds |

### `notebooklm_generate_mind_map`

Generates a hierarchical mind map and returns the JSON structure.

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `notebook_id` | string | ✅ | — | Target notebook ID |
| `instructions` | string | ❌ | none | Custom instructions |
| `language` | string | ❌ | `"en"` | BCP-47 language code |

---

## Resource reference

### `notebook://{notebook_id}/metadata`

Returns the notebook's metadata combined with its list of sources as JSON.

### `notebook://{notebook_id}/sources/{source_id}`

Returns the full indexed text that NotebookLM extracted from a specific source.

---

## Prompt reference

### `notebooklm_deep_research`

An end-to-end deep research workflow prompt.

| Argument | Description |
|----------|-------------|
| `topic` | Research topic or question |
| `urls` | Comma-separated list of source URLs |

The prompt instructs the LLM to:
1. Create a new notebook for the topic
2. Ingest all provided URLs as sources
3. Generate an initial comprehensive summary
4. Ask structured follow-up questions (claims, evidence, gaps, recommendations)
5. Synthesise a Markdown research report

---

## Authentication details

`notebooklm-py` uses Google's internal RPC protocol with Playwright-captured browser
credentials. The `notebooklm login` command stores a Playwright storage state JSON file at:

| Platform | Default path |
|----------|-------------|
| macOS / Linux | `~/.notebooklm/storage_state.json` |
| Windows | `%APPDATA%\notebooklm\storage_state.json` |

The MCP server reads this file at startup via `NotebookLMClient.from_storage()`.
If the file is missing or auth has expired, every tool will return an actionable error
message instructing you to run `notebooklm login` again.

---

## Troubleshooting

**"NotebookLM authentication storage not found"**
→ Run `notebooklm login` and restart the MCP server.

**"Authentication expired"**
→ Credentials expire periodically. Re-run `notebooklm login`.

**Audio generation timeout**
→ Pass a larger `timeout` value (e.g. `600`). Generation can take up to 5 minutes
under heavy API load.

**MCP server not appearing in Claude Desktop**
→ Check `claude_desktop_config.json` syntax and restart Claude Desktop.
Check stderr logs for startup errors.
