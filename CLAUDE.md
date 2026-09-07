# CLAUDE.md

Guidance for Claude Code working in this repository. Follow conventions in [CONTRIBUTING.md](CONTRIBUTING.md).

## Project Overview

`notebooklm-py` is an unofficial **async** Python client for Google NotebookLM, driving Google's internal `batchexecute` RPC protocol to automate notebooks, sources, chat, and studio artifacts.

**Critical constraint:** Obfuscated RPC method IDs in `src/notebooklm/rpc/_identifiers.py` are undocumented and Google changes them periodically — the #1 breakage class. `rpc/types.py` re-exports them for backwards compatibility.

## Development Commands

See [docs/installation.md](docs/installation.md) for the full installation guide.

```bash
# Contributor install (matches CI test jobs):
uv sync --frozen --extra browser --extra dev --extra markdown \
        --extra mcp --extra server --extra impersonate
source .venv/bin/activate
uv run playwright install chromium

uv run pytest                     # all tests (e2e excluded by default)
uv run pytest --cov               # with coverage
uv run pytest tests/e2e -m e2e    # e2e (requires auth)
uv run notebooklm --help          # CLI
```

Always install the full extras set (`mcp`, `server`, `impersonate`); omitting them causes adapter suites to skip silently, masking errors and failing the 90% coverage threshold.

```bash
# Approximate nightly coverage gate:
uv run pytest -n auto --dist loadgroup --cov=src/notebooklm \
  --cov-report=term-missing --cov-fail-under=90
```

`--dist loadgroup` is required to honor `@pytest.mark.xdist_group`. Avoid piping test commands into `tail`/`grep` to prevent masking non-zero exit codes.

## Before Pushing

CI enforces formatting, linting, type checks, and tests:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/notebooklm scripts/_live_auth_scenarios --ignore-missing-imports
uv run pytest
```

## Architecture

Adapters (`cli/`, `mcp/`, `server/`) → `_app/` (business logic) → `client.py` + `_runtime/` → selected backend (`_web/` or `_android/`) → `raw.py` (`rpc/` is Web compatibility facade).

See [docs/architecture.md](docs/architecture.md) for layered design, call flows, cross-cutting policies (loop affinity, idempotency, schema validation), and the full file map.

## Common Pitfalls

1. **RPC method IDs change** — re-capture network traffic and update `rpc/_identifiers.py`.
2. **Position-sensitive nested params** — mirror existing shapes; source-id nesting varies (`[id]` / `[[id]]` / `[[[id]]]` / `[[[[id]]]]`).
3. **CSRF tokens expire** — call `client.refresh_auth()` or re-run `notebooklm login`.
4. **Rate limiting** — add delays between bulk operations.
5. **Concurrency** — one `NotebookLMClient` is bound to its `open()` event loop: one per thread, never reuse across loops or `AuthTokens` tenants (see [concurrency contract](docs/python-api.md#concurrency-contract)).

## Usage

```python
async with NotebookLMClient.from_storage() as client:
    notebooks = await client.notebooks.list()
    await client.sources.add_url(nb_id, url)
    answer = await client.chat.ask(nb_id, question)
    status = await client.artifacts.generate_audio(nb_id)
```

- **CLI:** Top-level commands (`login`, `use`, `status`, `list`, `ask`) and groups (`source`, `label`, `artifact`, `generate`, `download`, `note`, `mcp`, `research`). Reference: [docs/cli-reference.md](docs/cli-reference.md).
- **MCP Server:** Console script `notebooklm-mcp` exposes `_app/` logic over MCP. Install with `notebooklm mcp install <client>`. Reference: [docs/mcp-guide.md](docs/mcp-guide.md).
- **REST Server:** Console script `notebooklm-server` exposes `/v1` routes over `_app/`. Requires `NOTEBOOKLM_SERVER_TOKEN`. Reference: [docs/installation.md#rest-api-server](docs/installation.md#rest-api-server).

## Testing

- **Unit:** `tests/unit/` (offline; includes `_app`, CLI, server, guardrails).
- **Integration:** `tests/integration/` (VCR cassette replay matching method, scheme, host, port, path, rpcids, freq). Record with `NOTEBOOKLM_VCR_RECORD=1 uv run pytest tests/integration/ -v`.
- **E2E:** `tests/e2e/` (real API, `@pytest.mark.e2e`, requires auth).
- Details: [docs/development.md](docs/development.md).

## Documentation Map

Key references in `docs/`: [installation](docs/installation.md), [cli-reference](docs/cli-reference.md), [python-api](docs/python-api.md), [architecture](docs/architecture.md), [development](docs/development.md), [mcp-guide](docs/mcp-guide.md), [troubleshooting](docs/troubleshooting.md), and [adr/](docs/adr/).

## Pull Request Workflow (required)

Drive PRs to merge:

1. Poll `gh pr checks <PR>` until all pass; investigate and fix any failures.
2. Trigger Claude review by commenting `@claude review` on the PR.
3. Address every review comment (`gemini-code-assist`, `coderabbitai`, `claude[bot]`): fix, push, reply on the thread (`Addressed in <SHA>: …`), and resolve threads.
4. Merge only when all checks pass, all threads are resolved, and `mergeStateStatus` is `CLEAN`.

**Note on `@claude review`:**
- `claude[bot]` posts a sticky summary issue comment and inline diff comments (`gh api /repos/<owner>/<repo>/pulls/<PR>/comments`). It does not submit a formal GitHub `reviewDecision`, so check comments directly.
- Ensure all inline `claude[bot]` review threads are addressed and resolved before merging.
