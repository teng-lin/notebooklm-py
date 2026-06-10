# CLAUDE.md

Guidance for Claude Code working in this repo. Also follow the file/naming conventions in [CONTRIBUTING.md](CONTRIBUTING.md).

## Project Overview

`notebooklm-py` is an unofficial **async** Python client for Google NotebookLM. It drives Google's internal `batchexecute` RPC protocol to automate notebooks, sources, AI querying, and studio artifacts (podcasts, videos, quizzes, …).

**Critical constraint:** the obfuscated RPC method IDs in `src/notebooklm/rpc/types.py` are undocumented and can break whenever Google changes them — the #1 breakage class.

## Development Commands

```bash
# Canonical contributor install (respects uv.lock; full guide: docs/installation.md)
uv sync --frozen --extra browser --extra dev --extra markdown
source .venv/bin/activate
uv run playwright install chromium

uv run pytest                     # all tests (e2e excluded by default)
uv run pytest --cov               # with coverage
uv run pytest tests/e2e -m e2e    # e2e (requires auth)
uv run notebooklm --help          # CLI
```

## Before Pushing

The pre-commit hook runs ruff (format + lint) on staged files. Also run these manually — CI fails otherwise:

```bash
uv run mypy src/notebooklm --ignore-missing-imports
uv run pytest
```

## Architecture

`cli/` (Click) → `_app/` (transport-neutral business logic, reusable by MCP/HTTP adapters) → `client.py` + `_*.py` (client runtime) → `rpc/` (batchexecute encode/decode).

See **[docs/architecture.md](docs/architecture.md)** for the layered design, call flows, cross-cutting policies (loop affinity, idempotency, schema validation), the per-file index, and the full repository tree.

## Common Pitfalls

1. **RPC method IDs change** — re-capture network traffic and update `rpc/types.py`.
2. **Position-sensitive nested params** — copy the shape from an existing implementation; source-id nesting varies (`[id]` / `[[id]]` / `[[[id]]]` / `[[[[id]]]]`).
3. **CSRF tokens expire** — call `client.refresh_auth()` or re-run `notebooklm login`.
4. **Rate limiting** — add delays between bulk operations.
5. **Concurrency** — one `NotebookLMClient` is bound to its `open()`-time event loop: create one per thread, never reuse across event loops or `AuthTokens` tenants. See the [concurrency contract](docs/python-api.md#concurrency-contract).

## Usage

```python
async with await NotebookLMClient.from_storage() as client:
    notebooks = await client.notebooks.list()
    await client.sources.add_url(nb_id, url)
    answer = await client.chat.ask(nb_id, question)
    status = await client.artifacts.generate_audio(nb_id)
```

CLI: top-level commands (`login`, `use`, `status`, `list`, `ask`) plus grouped subcommands (`source add`, `artifact list`, `generate audio`, `download video`, `note create`, `mcp install <client>`, …). Full reference: [docs/cli-reference.md](docs/cli-reference.md).

An opt-in MCP server (`mcp` extra, console script `notebooklm-mcp`) exposes the same `_app/` business logic over the Model Context Protocol; `notebooklm mcp install <client>` wires it into Claude Desktop/Code, Cursor, or Windsurf, and `desktop-extension/` packages a one-click `.mcpb` bundle.

## Testing

Unit (`tests/unit/`, no network) · integration (`tests/integration/`, mocked HTTP) · e2e (`tests/e2e/`, real API, `@pytest.mark.e2e`). VCR cassettes match on `rpcids` + decoded body shape. Details: [docs/development.md](docs/development.md).

## Docs

`docs/`: installation · cli-reference · python-api · configuration · troubleshooting · development · architecture · mcp-guide · rpc-development · rpc-reference · adr/.

## Pull Request Workflow (required)

After opening a PR, drive it to merge:

1. Poll `gh pr checks <PR>` until all pass; investigate and fix any failures.
2. Address every review comment (especially `gemini-code-assist`): make the fix, push, then reply on the thread (`Addressed in <SHA>: …`). Unreplied threads block merge.
3. Not done until all checks pass, all threads addressed, and `mergeStateStatus` is `CLEAN`.

Claude review is **not** automatic — comment `@claude review` on the PR to trigger the `.github/workflows/claude.yml` workflow.
