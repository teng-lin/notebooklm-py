# Repository Guidelines

**Status:** Active
**Last Updated:** 2026-09-07

## Structure & Modules

`src/notebooklm/` contains the async client (`client.py`) and typed exports (`__init__.py`).
- **Internal:** `_*.py` and `_*/` (`_app/`, `_auth/`, `_runtime/`, `_sources.py`, `_artifacts.py`).
- **Adapters & Wire:** `cli/` (Click), `mcp/` (FastMCP), `server/` (FastAPI), `rpc/` (batchexecute RPC facade), `_android/` (gRPC session & codecs).
- **Tests & Data:** `tests/unit/`, `tests/integration/` (VCR), `tests/server/`, `tests/e2e/`, `tests/_guardrails/`, `tests/_fault_server/`, `tests/cassettes/` (`web/`, `android/`). Examples in `examples/`, tools in `scripts/`.

## Development Commands

Canonical contributor install (full guide: [docs/installation.md](docs/installation.md)):

```bash
uv sync --frozen --extra browser --extra dev --extra markdown
source .venv/bin/activate
uv run playwright install chromium

uv run pytest
uv run pytest -n auto --dist loadgroup  # faster parallel run
uv run ruff check .
uv run ruff format --check .
uv run mypy src/notebooklm
```

For full adapter coverage, append `--extra mcp --extra server --extra impersonate`.

## Conventions & Testing

- **Style:** Python 3.10+, 4 spaces, double quotes, 100-char line limit (Ruff enforced).
- **Naming:** `snake_case` modules and tests (`test_<behavior>.py`). Preserve public/internal split (`_*.py`).
- **Testing:** Unit logic in `tests/unit/`, REST in `tests/server/`, VCR replay in `tests/integration/` (record: `NOTEBOOKLM_VCR_RECORD=1 uv run pytest tests/integration/ -v`), e2e in `tests/e2e/`. 90% coverage gate.
- **Commits/PRs:** Conventional commits (`feat(cli):`, `fix:`, `refactor:`, `style:`). Link issues and report test runs.

## Parallel & Autonomous Agents

- Prefer `--json` output and pass explicit notebook IDs instead of relying on stateful `notebooklm use`.
- Isolate concurrent runs with `NOTEBOOKLM_PROFILE=agent-<id>` (`~/.notebooklm/profiles/<name>/`) or `NOTEBOOKLM_HOME=/tmp/agent-<id>`.
- In headless environments, authenticate with `notebooklm login --browser-cookies <browser>` (requires `pip install "notebooklm-py[cookies]"`).

