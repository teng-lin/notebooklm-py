# Skill: NotebookLM MCP Server

Expose Google NotebookLM capabilities to MCP-compatible AI systems (Claude Desktop, Cursor, Copilot, etc.) via the Model Context Protocol.

## Purpose
This skill provides a standard Model Context Protocol (MCP) server for interacting with NotebookLM. It enables AI agents to list/create/delete notebooks, add sources, ask questions, and generate specialized artifacts (audio, video, quizzes, mind maps) within a managed environment.

## Deployment & Setup
To ensure persistent connectivity and avoid authentication failures, follow these environment isolation rules:

### 1. Create a dedicated virtual environment
Always run the MCP server in an isolated `venv` to prevent dependency conflicts.
```bash
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate  # Windows
```

### 2. Install with exhaustive dependencies
Install the package with the `[all]` extra to ensure all media downloaders and cookie extractors are available.
```bash
pip install git+https://github.com/djmahe4/notebooklm-py.git ".[all]"
```

### 3. Initialize Playwright & Chromium
Authentication requires Playwright's Chromium binary.
```bash
playwright install chromium
```

### 4. Authenticate
Run the interactive login command **within the venv**. This only needs to be done once per profile.
```bash
notebooklm login
```

### 5. Start the Server
Run the server using the module entry point.
```bash
# stdio transport (default for most MCP clients)
python -m notebooklm_mcp --transport stdio

# SSE transport (for web-based clients)
python -m notebooklm_mcp --transport sse --host 127.0.0.1 --port 8000
```

## Tool Usage & Best Practices

### Resource Identifiers
Tools use `notebook_id` and `source_id` (UUIDs). Use `notebooklm_list_notebooks` first to discover available IDs.

### Long-Running Generations
Media generation (audio/video) can take several minutes.
- Tools like `notebooklm_generate_audio_podcast` return a `task_id`.
- If a timeout occurs, the tool returns a JSON object with the `task_id`.
- **Action:** AI agents should capture this `task_id` and use it later to poll status if the immediate generation timed out.

### Error Diagnosis
If a tool call fails, always use the `notebooklm_troubleshoot` tool first.
- Pass the raw `error_message` and the `operation` name.
- It will provide specific advice for X.com scraping, auth expiry, and quota limits.

## Troubleshooting Common Scenarios

| Scenario | Recommendation |
|----------|----------------|
| **Auth Expiry** | Run `notebooklm auth check --test`. If it fails, run `notebooklm login`. |
| **X.com / Twitter Ingest** | Do not add URLs directly. Pre-fetch using `bird read <URL> > file.md` then add the file. |
| **macOS Keychain Prompts** | Click "Always Allow" in the browser-cookies prompt or switch to Firefox. |
| **Daily Quota Reached** | Generation tools may return "unavailable". Wait 24 hours or try a different profile. |

## When to Use
- When providing an AI agent with access to a user's NotebookLM knowledge base.
- When automating research workflows that involve YouTube, web, or document sources.
- When batch-generating study materials (quizzes, flashcards) or concepts (mind maps).

## Limitations
- **Interactive Login:** Initial authentication (`login`) cannot be performed purely via MCP; it requires a local terminal session to open a browser.
- **Bot Detection:** Aggressive scraping of certain sites (like X.com) may still fail; use the pre-fetch workaround mentioned above.
- **Media Download:** The server handles extraction, but large audio/video files may exceed some MCP transport limits if sent as raw bytes (prefer downloading to local paths).
