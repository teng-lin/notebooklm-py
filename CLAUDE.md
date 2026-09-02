# CLAUDE.md

Guidance for Claude Code working in this repo. Also follow the file/naming conventions in [CONTRIBUTING.md](CONTRIBUTING.md).

## Project Overview

`notebooklm-py` is an unofficial **async** Python client for Google NotebookLM. It drives Google's internal `batchexecute` RPC protocol to automate notebooks, sources, AI querying, and studio artifacts (podcasts, videos, quizzes, …).

**Critical constraint:** the obfuscated RPC method IDs in `src/notebooklm/rpc/types.py` are undocumented and can break whenever Google changes them — the #1 breakage class.

## Development Commands

```bash
# Canonical contributor install — matches the extras CI's test job installs.
# Full guide: docs/installation.md
uv sync --frozen --extra browser --extra dev --extra markdown \
        --extra mcp --extra server --extra impersonate
source .venv/bin/activate
uv run playwright install chromium

uv run pytest                     # all tests (e2e excluded by default)
uv run pytest --cov               # with coverage
uv run pytest tests/e2e -m e2e    # e2e (requires auth)
uv run notebooklm --help          # CLI
```

**Install the full extras set.** Omitting `mcp`/`server` does not fail — it
*skips*. `tests/unit/mcp/` and the server suites are `importorskip`-guarded, so
without `fastmcp`/`fastapi` they vanish silently and their source counts as
uncovered. Measured on one commit: the three-extra install runs **13,939** tests
at **84.39%** coverage and fails the 90% gate, while CI's set runs **15,478** at
**96.61%** and passes. That ~1,500-test blind spot hid a real MCP-adapter defect
through eight red CI jobs on #2198.

To approximate the nightly coverage gate (`.github/workflows/nightly.yml` — nightly actually splits coverage into a marker-filtered xdist pass plus a serial Playwright `--cov-append` pass and checks per-file floors via `scripts/check_coverage_thresholds.py`; PR cells run the same suite without coverage):

```bash
uv run pytest -n auto --dist loadgroup --cov=src/notebooklm \
  --cov-report=term-missing --cov-fail-under=90
```

`--dist loadgroup` matters: it honors `@pytest.mark.xdist_group`, and at least
one test fails under plain `-n auto` but passes under `loadgroup`. Check the
exit status directly rather than through a pipe — piping into `tail`/`grep`
reports the *pipeline's* status, which has masked real failures here before.

## Before Pushing

The pre-commit hook runs ruff (format + lint) on staged files. Also run these manually — CI fails otherwise:

```bash
uv run mypy src/notebooklm scripts/_live_auth_scenarios --ignore-missing-imports
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
async with NotebookLMClient.from_storage() as client:
    notebooks = await client.notebooks.list()
    await client.sources.add_url(nb_id, url)
    answer = await client.chat.ask(nb_id, question)
    status = await client.artifacts.generate_audio(nb_id)
```

CLI: top-level commands (`login`, `use`, `status`, `list`, `ask`) plus grouped subcommands (`source add`, `label list`, `artifact list`, `generate audio`, `download video`, `note create`, `mcp install <client>`, …). Full reference: [docs/cli-reference.md](docs/cli-reference.md).

An opt-in MCP server (`mcp` extra, console script `notebooklm-mcp`) exposes the same `_app/` business logic over the Model Context Protocol; `notebooklm mcp install <client>` wires it into Claude Desktop/Code, Cursor, or Windsurf, and `desktop-extension/` packages a one-click `.mcpb` bundle.

An opt-in single-tenant REST server (`server` extra, console script `notebooklm-server`) exposes guarded `/v1` FastAPI routes over the same `_app/` layer. It is experimental, loopback-bound by default, and requires `NOTEBOOKLM_SERVER_TOKEN`; see [docs/installation.md#rest-api-server](docs/installation.md#rest-api-server).

## Testing

Unit (`tests/unit/`, no network; includes `_app`, CLI, server, and guardrail tests) · integration (`tests/integration/`, VCR cassette replay) · e2e (`tests/e2e/`, real API, `@pytest.mark.e2e`). VCR cassettes match on the full tuple `["method", "scheme", "host", "port", "path", "rpcids", "freq"]` (`tests/vcr_config.py`) — note that `host` is part of the match, so replay is pinned to the host a cassette was recorded against, and `freq` compares the decoded `f.req` form body. Details: [docs/development.md](docs/development.md).

## Docs

`docs/`: installation · cli-reference · python-api · configuration · troubleshooting · development · architecture · mcp-guide · rpc-development · rpc-reference · stability · adr/.

## Pull Request Workflow (required)

After opening a PR, drive it to merge:

1. Poll `gh pr checks <PR>` until all pass; investigate and fix any failures.
2. Address every review comment (especially `gemini-code-assist`): make the fix, push, then reply on the thread (`Addressed in <SHA>: …`). Unreplied threads block merge.
3. Not done until all checks pass, all threads addressed, and `mergeStateStatus` is `CLEAN`.

Claude review is **not** automatic — comment `@claude review` on the PR to trigger the `.github/workflows/claude.yml` workflow. Treat `claude[bot]` as a first-class reviewer the merge gate **waits on**, alongside `gemini-code-assist` / `coderabbitai`:

- It posts inline review-thread comments **plus** a sticky summary comment ("**Claude finished … task**"). The action does **not** submit a formal GitHub review, so `claude[bot]` never appears in `gh pr view --json reviews` / `reviewDecision` and is **not** a required check — do not infer "claude reviewed" from those.
- `gh pr checks` may show a `claude` entry as **skipping**: every comment (incl. other bots') fires `claude.yml`, and runs not from a `teng-lin` `@claude` comment correctly skip via the job `if:` gate. That skip is **not** the review run — find the real one with `gh run list --workflow=claude.yml --json event,conclusion` (look for the `success` run) or just read the comment below.
- Before merging, confirm the review landed and address it. The two halves live on different endpoints: the sticky summary is an **issue** comment (`gh api /repos/<owner>/<repo>/issues/<PR>/comments`), and the inline findings are **pull-request review** comments on the diff (`gh api /repos/<owner>/<repo>/pulls/<PR>/comments`) — filter either with `--jq '.[]|select(.user.login=="claude[bot]").body'`. Resolve any inline `claude[bot]` threads like any other bot's.
