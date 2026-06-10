# MCP Server Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Each tool task follows @superpowers:test-driven-development (red → green → commit).

**Goal:** Ship a transport-neutral MCP server (`src/notebooklm/mcp/`) as a sibling adapter to `cli/` over the `_app/` layer — ~23 hybrid tools, name/partial-UUID resolution, both-mode confirmation, typed errors, and frictionless distribution.

**Architecture:** Each MCP tool is a thin adapter: parse args → build the `_app` `Request`/`Plan` → `await execute_<verb>(plan, client, progress=…)` → project the typed `Result` to the wire via `_app.serialize.to_jsonable`. No business logic, no `click`/`rich`/`cli` imports (AST-linted). FastMCP server, lifespan binds one async `NotebookLMClient`. stdio + loopback-HTTP.

**Tech Stack:** Python 3 (async), FastMCP (`mcp` extra), `_app/` cores, pytest + in-memory FastMCP `Client`, VCR cassettes (reused, never re-recorded).

**Design source of truth:** `docs/plans/2026-06-08-mcp-server-redesign-design.md`.
**Reference (do NOT copy wholesale):** the `feat/mcp-server` prototype (`git show feat/mcp-server:src/notebooklm/mcp/...`) shows tool shapes; rebuild them on `_app/`.

---

## Ground rules

- **TDD per tool**: write the failing unit test (in-memory FastMCP `Client` + mocked `NotebookLMClient`), see it fail, implement the adapter, see it pass, commit.
- **Read the `_app` contract first** for each domain: open `src/notebooklm/_app/<domain>.py` and use its exact `Request`/`Plan`/`Result` dataclasses and `execute_*`/`build_*_plan` signatures. Do **not** invent shapes.
- **Cassette invariance**: reuse existing cassettes (`rpcids` + body-shape matcher). Never run with `NOTEBOOKLM_VCR_RECORD=1`.
- **`--json` is not ours**: that's the CLI's contract; MCP owns its own wire shape via `to_jsonable`.
- **Commit frequently**, one tool/helper per commit.

---

## Phase 0 — Test-layout fix (Fix A + C) — do FIRST, standalone

This is a pre-existing latent bug (see design §6); fixing it first lets MCP tests live at the natural `tests/unit/mcp/`. **Validate in an env WITH the `mcp` extra** (that's where the shadowing bites).

### Task 0.1: Complete the `__init__.py` chain (Fix A)

**Files:**
- Create: `tests/__init__.py` (empty)
- Create: `tests/unit/__init__.py` (empty)

**Step 1:** Confirm the current top-level-package shadowing exists *before* the fix (documents the bug). With the `mcp` extra installed:
```bash
uv sync --frozen --extra dev --extra mcp   # adds fastmcp → installs top-level `mcp`
uv run python -c "import importlib.util as u; print(u.find_spec('mcp'))"   # the installed SDK
```
Note: a future `tests/unit/mcp/` would shadow this in `prepend` mode.

**Step 2:** Create both `__init__.py` files (empty).

**Step 3:** Run the FULL suite — the chain change is global:
```bash
uv run pytest -q -p no:cacheprovider
```
Expected: same pass count as baseline (9256 passed). Watch for: collection errors from duplicate module basenames, or conftest-scoping changes. If any dir under `tests/` has an inconsistent chain (some `__init__.py` present, parent missing), add the missing parent `__init__.py`.

**Step 4: Commit**
```bash
git add tests/__init__.py tests/unit/__init__.py
git commit -m "test: complete tests/ package chain so group dirs aren't top-level packages (Fix A)"
```

### Task 0.2: Guardrail against installed-package shadowing (Fix C)

**Files:**
- Create: `tests/_guardrails/test_test_dir_no_shadow.py`

**Step 1: Write the failing test** — fails if any test package dir (a dir under `tests/` containing `__init__.py`) has a name that resolves to an installed top-level distribution:
```python
"""Guardrail: no test package dir may share a name with an installed top-level package.

In pytest's default `prepend` import mode a packaged test dir becomes importable
under its own basename; if that basename matches an installed distribution (e.g.
the `mcp` SDK), it shadows it and breaks `import <name>` everywhere. See
docs/plans/2026-06-08-mcp-server-redesign-design.md §6.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]


def test_no_test_package_dir_shadows_installed_package() -> None:
    offenders = []
    for init in TESTS_ROOT.rglob("__init__.py"):
        pkg_dir = init.parent
        # Only dirs whose parent is NOT itself a package can become top-level.
        if (pkg_dir.parent / "__init__.py").exists():
            continue
        name = pkg_dir.name
        spec = importlib.util.find_spec(name)
        if spec is not None and "site-packages" in str(spec.origin or ""):
            offenders.append(f"{pkg_dir} shadows installed package '{name}' ({spec.origin})")
    assert not offenders, "Test dirs shadow installed packages:\n" + "\n".join(offenders)
```
Note: after Fix A, the only top-level test package is `tests` itself (no `tests` distribution installed), so this passes. Without Fix A it would flag `tests/unit/cli` etc. only if those names were installed — `mcp` is the real one.

**Step 2:** Run with the `mcp` extra installed → PASS (post Fix A).
```bash
uv run pytest tests/_guardrails/test_test_dir_no_shadow.py -q
```

**Step 3: Commit**
```bash
git add tests/_guardrails/test_test_dir_no_shadow.py
git commit -m "test: guardrail forbidding test dirs that shadow installed packages (Fix C)"
```

---

## Phase 1 — Foundation / scaffold

### Task 1.1: Add the `mcp` extra

**Files:** Modify `pyproject.toml`

**Steps:** Add an optional-dependency group `mcp = ["fastmcp>=2.14"]` (check the prototype's pin: `git show feat/mcp-server:pyproject.toml`). Add console script `notebooklm-mcp = "notebooklm.mcp.__main__:main"`. Keep fastmcp/pydantic **out** of the core deps. Run `uv sync --extra mcp` to confirm it resolves. Commit.

### Task 1.2: Serializer + error helper

**Files:**
- Create: `src/notebooklm/mcp/__init__.py`
- Create: `src/notebooklm/mcp/_errors.py`
- Test: `tests/unit/mcp/__init__.py`, `tests/unit/mcp/test_errors.py`

**`src/notebooklm/mcp/_errors.py`**: a `mcp_errors()` context manager / decorator that catches `NotebookLMError`, calls `_app.errors.classify(exc)`, and raises a FastMCP error carrying `{code, message, retriable, hint?}`. Build a `category → (code, hint)` table. Serialization is just `from ..._app.serialize import to_jsonable` (re-export from a `mcp/_serialize.py` shim if convenient).

**Tests:** for an exemplar of every `ErrorCategory`, assert the projected code + retriable + hint. This is also the **consistency gate** seed.

Commit.

### Task 1.3: Error-consistency gate

**Files:** Create `tests/_guardrails/test_mcp_classify_consistency.py` — mirror `tests/_guardrails/test_classify_error_handler_consistency.py`: for an exemplar of each `_app.errors.ErrorCategory`, assert the MCP code projection matches the category (so the two ladders can't drift). Commit.

### Task 1.4: Resolution helper

**Files:**
- Create: `src/notebooklm/mcp/_resolve.py`
- Test: `tests/unit/mcp/test_resolve.py`

**Behavior:** `async def resolve_notebook(client, ref) -> str` and `async def resolve_source(client, notebook_id, ref) -> str`. Each:
1. `from ..._app.resolve import resolve_ref, validate_id, AmbiguousIdError`.
2. If `ref` is a full/partial UUID-ish token → call `resolve_ref(ref, items, id_of=…, title_of=…)` over the listed items (full-UUID fast-path inside avoids needing the list — but source resolution needs the notebook's list anyway).
3. Else (non-hex token) → case-insensitive **exact title** match over the same items; 0 → `NotFoundError`-style; >1 → raise `AmbiguousIdError` with candidates.
Route hex-ish vs non-hex by a simple regex (`^[0-9a-fA-F-]+$`).

**Tests:** full UUID (no list call), exact id, unique prefix, name match, ambiguous prefix → candidates, ambiguous title → candidates, no match. Commit.

### Task 1.5: Confirmation + annotations helpers

**Files:**
- Create: `src/notebooklm/mcp/_confirm.py`
- Test: `tests/unit/mcp/test_confirm.py`

`needs_confirmation(preview: dict) -> dict` returns the `{status: "needs_confirmation", preview}` payload; a helper to assert/branch on `confirm`. Define annotation constants (`READ_ONLY`, `DESTRUCTIVE`) to attach to tool registration. Tests: preview-when-false, pass-through-when-true. Commit.

### Task 1.6: Server, lifespan, context, entrypoint

**Files:**
- Create: `src/notebooklm/mcp/_context.py` (AppState dataclass + `get_client(ctx)`)
- Create: `src/notebooklm/mcp/server.py` (`create_server(profile=None, client_factory=None) -> FastMCP`; lifespan binds one `NotebookLMClient.from_storage(profile)`; registers all tool modules)
- Create: `src/notebooklm/mcp/__main__.py` (`main()`: argparse `--profile/--transport/--host/--port/--log-level`; env `NOTEBOOKLM_PROFILE` etc.; **logs → stderr, stdout pure JSON-RPC**; loopback bind guard for http with `NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND` override)
- Test: `tests/unit/mcp/conftest.py` (in-memory `Client` + mocked `NotebookLMClient` fixture, per-namespace async mocks), `tests/unit/mcp/test_server.py` (server constructs; lifespan yields AppState; transport guard refuses non-loopback).

Reference the prototype's `server`/`__main__`/`_context` modules for the lifespan shape (`git show feat/mcp-server:src/notebooklm/mcp/server.py`). Commit.

### Task 1.7: Boundary guardrail

**Files:** Create `tests/_guardrails/test_mcp_boundary.py` — port the prototype's AST scan: every `.py` under `src/notebooklm/mcp/` must not import `click`, `rich`, `notebooklm.cli`, or `cli.*`. Run → PASS. Commit.

---

## Phase 2 — Tools by domain

### The tool-adapter recipe (apply to every tool)

```python
# src/notebooklm/mcp/tools/<domain>.py
from fastmcp import Context
from ..._app import <domain> as core            # the _app core
from ..._app.serialize import to_jsonable
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_notebook          # where applicable
from .._confirm import needs_confirmation, READ_ONLY, DESTRUCTIVE

def register(mcp):
    @mcp.tool(annotations=READ_ONLY)              # or DESTRUCTIVE
    async def <verb>(ctx: Context, notebook: str, ...) -> dict:
        """<concise agent-facing doc; say 'accepts name or ID'>."""
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            plan = core.build_<verb>_plan(<Request(...)>)        # if the core has a plan step
            result = await core.execute_<verb>(plan, client)     # exact sig per _app/<domain>.py
            return to_jsonable(result)
```

**TDD per tool (5 steps):** write the in-memory-Client test asserting `structured_content` for a mocked client → run, fail → implement the adapter per recipe → run, pass → commit. For destructive tools, add a test: `confirm=false` returns `needs_confirmation`; `confirm=true` calls the delete.

> For each domain, FIRST read `src/notebooklm/_app/<domain>.py` for the exact contract. The signatures below are the *tool* surface (design §2), not the `_app` signatures.

### Task 2.1: Notebooks (5)  — `tools/notebooks.py`, `tests/unit/mcp/test_notebooks.py`
`notebook_list` (READ) · `notebook_create(title)` · `notebook_describe(notebook)` (READ) · `notebook_rename(notebook, new_title)` · `notebook_delete(notebook, confirm)` (DESTRUCTIVE). `_app` core: `_app/notebooks.py`.

### Task 2.2: Sources (6)  — `tools/sources.py`, `tests/unit/mcp/test_sources.py`
`source_list(notebook)` (READ) · `source_get_content(notebook, source)` (READ) · `source_rename(notebook, source, new_title)` · `source_delete(notebook, source, confirm)` (DESTRUCTIVE) · `source_wait(notebook)` · ⊕ `source_add(notebook, type=url|text|file|drive|youtube, url?/text?/path?/document_id?, …)`. Cores: `_app/source_add.py`, `source_content.py`, `source_listing.py`, `source_mutations.py`, `source_wait.py`. Document the per-`type` required input in the docstring.

### Task 2.3: Chat (2)  — `tools/chat.py`, `tests/unit/mcp/test_chat.py`
`chat_ask(notebook, question, conversation_id?)` · `chat_configure(notebook, goal?, response_length?)`. Core: `_app/chat.py`. Pass a `ProgressSink` adapter that maps `_app` progress events to FastMCP `ctx` progress (drop Rich markup — emit plain text).

### Task 2.4: Artifacts (4)  — `tools/artifacts.py`, `tests/unit/mcp/test_artifacts.py`
`artifact_list(notebook)` (READ) · ⊕ `artifact_generate(notebook, type=audio|video|quiz|…, **opts)` · `artifact_status(notebook, task_id)` (READ, poll) · ⊕ `artifact_download(notebook, type, path, format?)`. Cores: `_app/artifacts.py`, `_app/generate*.py`, `_app/download.py`. Use the stateless `poll_status`-style path so agents can poll across calls (per design §architecture).

### Task 2.5: Research (3)  — `tools/research.py`, `tests/unit/mcp/test_research.py`
`research_start(query, source=web|drive, mode=fast|deep)` · `research_status(notebook)` (READ) · `research_import(notebook, task_id)`. Core: `_app/research.py`, `_app/source_research.py`.

### Task 2.6: Notes (4)  — `tools/notes.py`, `tests/unit/mcp/test_notes.py`
`note_create(notebook, title, content)` · `note_list(notebook)` (READ) · `note_update(notebook, note, content)` · `note_delete(notebook, note, confirm)` (DESTRUCTIVE). Core: `_app/notes.py`. (Split into verbs — do NOT use an `action` enum.)

### Task 2.7: Meta (1)  — `tools/meta.py`, `tests/unit/mcp/test_meta.py`
`server_info` (READ): version + auth-health (reuse `_app/auth_check.py`).

### Task 2.8: Manifest guardrail
Create `tests/unit/mcp/test_manifest.py`: pin the exact ~23-tool set + a ceiling; assert `DESTRUCTIVE` annotation on the 3 deletes, `READ_ONLY` on reads, and a `confirm` param on every destructive tool. Commit.

---

## Phase 3 — Distribution

### Task 3.1: uvx / console-script smoke
Verify `uvx --from "notebooklm-py[mcp]" notebooklm-mcp --help` works (or document the local-install equivalent). Add a unit test that `main(["--help"])` exits 0. Commit.

### Task 3.2: `.mcpb`/DXT desktop bundle
**Files:** Create `desktop-extension/manifest.json` + `desktop-extension/run_server.py` (resilient `uvx` locator that execs `uvx --from "notebooklm-py[mcp]" notebooklm-mcp`, clean stdio passthrough). Reference the competitor's `desktop-extension/` shape. Add a build/packaging note to `docs/`. Test: `run_server.py` locates a stub `uvx` on PATH. Commit.

### Task 3.3: `notebooklm mcp install <client>`
**Files:** Create `src/notebooklm/cli/mcp_cmd.py` — a `mcp` CLI group with `install <client>` that detects + writes the MCP server config block for `claude-desktop`, `claude-code`, `cursor`, `windsurf` (use each client's config path + JSON shape). Register it in the CLI assembler. TDD against a tmp config path. Commit. Update CLAUDE.md tree + architecture index.

---

## Phase 4 — Integration, E2E, docs

### Task 4.1: VCR integration
Create `tests/integration/test_mcp_notebook_list_vcr.py` — drive `notebook_list` through the in-memory Client over the **existing** `notebooks_list` cassette (reuse; no record). Assert end-to-end MCP tool → `_app` → client → recorded RPC. Commit.

### Task 4.2: E2E (`-m e2e`)
Create `tests/e2e/test_mcp.py` — real API: `notebook_list` (readonly), manifest presence + annotations, full `create→describe→rename→delete` lifecycle with cleanup, and **name-resolution against live data** (resolve a notebook by its title). Commit.

### Task 4.3: Docs + freshness
Update `CLAUDE.md` (new `mcp/` tree rows + the `mcp install` CLI command), `docs/architecture.md` index, and add a short `docs/mcp-guide.md` if desired (was descoped — confirm). Run `check_claude_md_freshness` + `module_size_ratchet` + full suite + mypy + ruff. Commit.

### Task 4.4: Final gate
```bash
uv run pytest -q -p no:cacheprovider          # full suite, 0 failed
uv run mypy src/notebooklm --ignore-missing-imports
uv run pytest tests/_guardrails -q            # boundary + manifest + consistency + shadow guardrails
```
Then open the PR via the project's PR workflow.

---

## Sequencing notes
- Phase 0 is independent and low-risk — land it first (it's a real bug fix).
- Phases 1→2 are the core; 2 tasks are parallelizable per domain once Phase 1 lands (disjoint `tools/*.py` + `tests/unit/mcp/test_*.py`; collide only on `server.py` registration + manifest — reconcile additively).
- Phase 3/4 after the tool surface is green.
- This work subsumes #1480; close it when the PR merges. #1481 is independent.
