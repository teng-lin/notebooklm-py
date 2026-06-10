# MCP Server Redesign — Design

**Date:** 2026-06-08
**Status:** Design approved (brainstorming complete); implementation not started.
**Supersedes:** the `feat/mcp-server` prototype (clean rewrite, salvaging proven parts).

## Context & goal

We have an MCP server prototype on `feat/mcp-server` (FastMCP, 19 tools, stdio-only, built
against `cli/services/`). The maintainer is dissatisfied with it. We studied a competitor —
`jacob-bd/notebooklm-mcp-cli` (v0.7, 37 tools, FastMCP, stdio+HTTP+SSE, `.mcpb`/DXT
distribution) — and our own prototype, and decided to do a **new implementation** that keeps
the prototype's genuinely good engineering while fixing its weak points.

`_app/` (the transport-neutral business-logic layer, ADR-0021) has now landed on `main`, so
the new server builds on `main` directly — no branch dependency. This rewrite *subsumes* the
deferred "rebase MCP onto `_app`" work (issue #1480).

### Priorities (maintainer-selected)
1. **Agent UX / ergonomics** — name/ID resolution, legible (non-opaque) tools, confirmation
   previews, actionable error hints.
2. **`_app`-native architecture** — built directly on the `_app/` cores, as a sibling adapter
   to `cli/`.
3. **Distribution & transports** — easy install, stdio + local HTTP.

### Explicitly out of scope (YAGNI)
- **Power features**: batch, cross-notebook query, pipelines, tags, auto-labeling. (Competitor
  has them; we deliberately don't.)
- **Sharing** domain — not exposed at all.
- **Remote / multi-tenant HTTP** — single-user, profile-bound only.
- **Embedded `instructions` string + `mcp-guide.md` + agent skill file** — dropped for now
  (a minimal instructions string is near-free if reconsidered later).
- **Prefix/name resolution is IN** (see Resolution); only multi-tenant auth and the docs/skill
  bundle are out.

## Locked decisions

| Axis | Decision |
|---|---|
| Tool model | **Hybrid** — explicit verb-per-op where schemas differ; enum discriminator only where variants share a schema; names accepted everywhere |
| Transports | **stdio + streamable-HTTP on 127.0.0.1** (loopback guard); single-user, profile bound at startup |
| Relationship to prototype | **Clean rewrite on `_app/`**, salvage proven parts |
| Reference types | **ID + partial-UUID + name** (reuse `_app/resolve.py`, add title matching) |
| Destructive-op safety | **Both** — MCP annotations + 2-step `confirm` with titled preview |
| Distribution | **uvx/`mcp` extra + `.mcpb`/DXT bundle + `mcp install <client>` auto-config** |
| Test layout fix | **Fix A** (complete `__init__.py` chain) **+ Fix C** (guardrail); tests at `tests/unit/mcp/` |

## 1. Architecture & layering

Sibling adapter to `cli/`, both over `_app/`:

```
client.* (public API) ─┐
                        ├─→ _app/ (neutral cores) ─┬─→ cli/  (Click)
                        ┘                          └─→ mcp/  (FastMCP)   ← new
```

- **Tool = thin adapter** (same 4 steps as the CLI adapter): parse args → build the `_app`
  `Request`/`Plan` → `await execute_<verb>(plan, client, progress=…)` → project the typed
  `Result` to the wire via `_app.serialize.to_jsonable` (already proven byte-identical to the
  prototype's copy). No business logic in `mcp/`; no `click`/`rich`/`cli` imports.
- **Server & lifespan**: FastMCP instance; lifespan binds **one async `NotebookLMClient`**
  (`from_storage(profile)`) for the process — async-native, keepalive/cookie-rotation free, no
  thread-pool/singleton/locks (the competitor's sync workaround we don't need).
- **Transports**: stdio (default) + streamable-HTTP on `127.0.0.1` with a bind guard (refuse
  non-loopback without an explicit override). Logs → stderr; stdout stays pure JSON-RPC.
- **Auth/config**: profile bound at startup via `--profile` / `NOTEBOOKLM_PROFILE`; reuses
  `notebooklm login`. No new credential surface.

## 2. Tool surface (~23 tools, hybrid)

`⊕` = enum discriminator (variants share a schema). Every `notebook`/`source` arg accepts
name or ID.

**Notebooks (5)** — verbs
`notebook_list` · `notebook_create(title)` · `notebook_describe(notebook)` ·
`notebook_rename(notebook, new_title)` · `notebook_delete(notebook, confirm)`

**Sources (6)**
`source_list(notebook)` · `source_get_content(source)` · `source_rename` ·
`source_delete(confirm)` · `source_wait(notebook)` (poll conversion) ·
⊕ `source_add(notebook, type=url|text|file|drive|youtube, …)` — unified: one operation,
mutually-exclusive *named* inputs (not hidden action-dispatch), schema stays legible.

**Chat (2)**
`chat_ask(notebook, question, conversation_id?)` · `chat_configure(notebook, goal?, response_length?)`

**Artifacts / Studio (4)**
`artifact_list(notebook)` · ⊕ `artifact_generate(notebook, type=audio|video|quiz|…)` ·
`artifact_status(notebook, task_id)` (poll) · ⊕ `artifact_download(notebook, type, path, format?)`

**Research (3)**
`research_start(query, source=web|drive, mode=fast|deep)` · `research_status(notebook)` ·
`research_import(notebook, task_id)`

**Meta (1)**
`server_info` — version + auth-health.

Deliberate trade: ~23 (not ~17). Notes is split into verbs — the de-opaquing win over the
prototype's `note(action=…)`:

**Notes (4)** — verbs
`note_create` · `note_list` · `note_update` · `note_delete(confirm)`

(No Sharing domain.)

## 3. Name ↔ ID resolution

Reuse `_app/resolve.py:resolve_ref` (the CLI's resolver). Per argument, in order:
1. Empty → `ValidationError`.
2. **Full 36-char UUID** → fast-path, no list call.
3. Case-insensitive **exact ID** match.
4. Case-insensitive **unique ID prefix** (partial UUID).
5. **Ambiguous prefix** → `AmbiguousIdError` carrying candidate IDs+titles (= fail-with-candidates).
6. No match → `ValidationError`.

**New on top:** case-insensitive **exact title (name) matching**. Non-hex tokens route to the
title path, hex-ish tokens to the ID/prefix path — no collision (titles aren't hex). Duplicate
titles fail with candidates, identical to prefix ambiguity. Sources resolve within the resolved
notebook's source list. Cost: name/prefix resolution adds one `list` RPC; the full-ID fast-path
avoids it (docstrings say "accepts name or ID").

**Ambiguity policy:** always **fail with candidates** — never pick-first (the next call could be
`notebook_delete`).

## 4. Error contract & destructive-op safety

**Errors**: reuse `_app.errors.classify(exc) → ClassifiedError` (category + `retriable`) — the
single source the CLI also projects from. MCP projects the category onto its own thin code
vocabulary + an actionable **`hint`** per category (`AUTH → "run notebooklm login"`,
`RATE_LIMITED → "wait and retry"`, `AMBIGUOUS → "retry with one of these IDs: …"`). Agent sees
structured `{code, message, retriable, hint?}`. Messages redaction-capped, but **code +
retriable always preserved** (improves on the prototype's blunt 300-char truncation). A
consistency gate pins MCP codes ↔ `classify` categories (mirrors the CLI gate) so they can't
drift.

**Destructive ops** (3: `notebook_delete`, `source_delete`, `note_delete`) — **both** mechanisms:
- MCP **annotations** (`destructiveHint` on deletes, `readOnlyHint` on reads so hosts can
  auto-allow safe calls).
- **2-step confirm**: first call returns a `needs_confirmation` preview (what's deleted, by
  title); agent re-calls with `confirm=true`. Belt-and-suspenders: host gating where it exists,
  protocol-level confirm where it doesn't.

## 5. Distribution & onboarding

1. **PyPI `mcp` extra + uvx (baseline)** — server behind an `mcp` extra (FastMCP/pydantic
   opt-in, off the core); `notebooklm-mcp` console script; `uvx --from "notebooklm-py[mcp]"
   notebooklm-mcp`.
2. **One-click `.mcpb`/DXT bundle** — `desktop-extension/` with `manifest.json` + a resilient
   `run_server.py` launcher that locates `uvx` across common paths and execs it (clean stdio
   passthrough). Built + attached to GitHub releases.
3. **`notebooklm mcp install <client>`** — auto-detects and writes the MCP config block for
   Claude Desktop, Claude Code, Cursor, Windsurf. Removes the hand-edited-JSON failure mode.

## 6. Testing & guardrails

- **Unit** (`tests/unit/mcp/`): in-memory FastMCP `Client` + mocked `NotebookLMClient` per
  namespace; assert serialized `structured_content`. Dedicated suites for the **resolution
  ladder** (full/exact/partial-UUID/name/ambiguous→candidates/no-match) and the **confirm flow**
  (preview when `confirm=false`, executes when `true`).
- **Boundary guardrail**: AST scan of `mcp/` for banned imports (`click`/`rich`/`cli`) — port
  the prototype's `test_mcp_boundary.py`.
- **Manifest guardrail**: pin the exact tool set + ceiling; assert `destructiveHint` on the 3
  deletes, `readOnlyHint` on reads, `confirm` present on deletes.
- **Error-consistency gate**: MCP code ↔ `_app.errors.classify` category.
- **Serialization**: `mcp/` imports `_app.serialize.to_jsonable` directly (already golden-tested).
- **Integration (VCR)**: reuse existing cassettes (`rpcids` + body-shape matcher) end-to-end:
  MCP tool → `_app` → client → recorded RPC. **No re-recording** (cassette-invariance rule).
- **E2E** (`-m e2e`): real API — `create→describe→rename→delete` lifecycle, manifest presence,
  name-resolution against live data.

### Test-layout fix (pre-existing latent bug; fix regardless of MCP)

Root cause: pytest's default **`prepend`** import mode + a broken `__init__.py` chain
(`tests/` and `tests/unit/` have no `__init__.py`, but each group dir does) makes every group
dir a **top-level package named after the directory** (`cli`, `app`, would-be `mcp`). `cli`/`app`
are harmless (no installed distribution), but `mcp` collides with the installed MCP SDK
(fastmcp's dep) → `import mcp` resolves to the test dir and breaks. The prototype dodged this by
renaming to `mcp_server/` (workaround, leaves the footgun armed).

- **Fix A (chosen)**: add `tests/__init__.py` + `tests/unit/__init__.py` to complete the chain.
  Tests then import as fully-qualified `tests.unit.mcp.test_x`; no group dir is top-level →
  nothing shadows installed `mcp`. Keeps the natural `tests/unit/mcp/` name; inoculates all
  future group dirs. Global import-path change → **validate with a full-suite run** (watch for
  inconsistent chains under `tests/`).
- **Fix C (chosen)**: a guardrail test that fails if any test package dir name matches an
  installed top-level distribution. Cheap regression insurance.

## Sequencing / follow-ups

- `_app/` is on `main` → build the new `mcp/` on `main`.
- This rewrite subsumes #1480 (MCP onto `_app`); #1481 (de-monkeypatch / collapse wrappers) is
  independent.
- Do the test-layout fix (A+C) early so MCP tests land at `tests/unit/mcp/` from the start.
- Implementation: isolated worktree + a detailed implementation plan (next step).
