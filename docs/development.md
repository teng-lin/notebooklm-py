# Contributing Guide

**Status:** Active
**Last Updated:** 2026-06-11

This guide covers everything you need to contribute to `notebooklm-py`: architecture overview, testing, and releasing.

> **New contributor?** Start with [CONTRIBUTING.md](../CONTRIBUTING.md) at the
> repo root for the install/lint/test workflow and PR conventions, then come
> back here for architectural context once you're ready to write code.

---

## Architecture

> **Canonical post-refactor map:** see [`docs/architecture.md`](./architecture.md)
> for the current adapter/app/client/runtime/RPC graph and
> capability-protocol model. This section
> remains as the contributor on-ramp (package layout + adding-features
> guidance) and links out to the architecture doc rather than duplicating it.

### Package Structure

```
src/notebooklm/
├── __init__.py          # Public exports
├── client.py            # NotebookLMClient main class
├── auth.py              # Public auth facade
├── types.py             # Dataclasses and type definitions
├── _app/                # Transport-neutral business logic shared by adapters
├── _client_composed.py  # Client-owned composition holder
├── _runtime/            # Runtime contracts, config, lifecycle, auth, transport
├── _notebooks.py        # NotebooksAPI implementation
├── _notebook_metadata.py # Private notebook metadata composition service
├── _sources.py          # SourcesAPI implementation
├── _source/             # Private source services
├── _artifacts.py        # ArtifactsAPI implementation
├── _artifact/           # Private artifact services
├── _chat/               # ChatAPI implementation (facade + chat helpers)
├── _research.py         # ResearchAPI implementation
├── _notes.py            # NotesAPI implementation
├── _mind_map.py         # Private note-backed mind-map service
├── _mind_maps_api.py    # MindMapsAPI implementation
├── _labels.py           # LabelsAPI implementation
├── _settings.py         # SettingsAPI implementation
├── _sharing.py          # SharingAPI implementation
├── _sharing_manager.py  # Private legacy notebook share-link service
├── rpc/                 # RPC protocol layer
│   ├── __init__.py
│   ├── types.py         # RPCMethod enum and constants
│   ├── encoder.py       # Request encoding
│   └── decoder.py       # Response parsing
├── cli/                 # Click adapter (`*_cmd.py`) plus `cli/services/`
├── mcp/                 # FastMCP adapter (optional `mcp` extra)
└── server/              # FastAPI REST adapter (optional `server` extra)
```

### Layered Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Adapter Layer                          │
│        cli/ (Click), mcp/ (FastMCP), server/ (FastAPI)       │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                  App Core Layer (`_app/`)                    │
│        transport-neutral request/plan/result workflows       │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                      Client Layer                           │
│  NotebookLMClient → NotebooksAPI, SourcesAPI, ArtifactsAPI  │
│       private services compose cross-facade behavior         │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                      Runtime Layer                          │
│          RpcExecutor, RuntimeTransport, Kernel, lifecycle    │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                        RPC Layer                            │
│        encoder.py, decoder.py, types.py (RPCMethod)         │
└─────────────────────────────────────────────────────────────┘
```

### Layer Responsibilities

| Layer | Files | Responsibility |
|-------|-------|----------------|
| **Adapters** | `cli/`, `mcp/`, `server/` | User commands/tools/routes, transport-specific input/output, auth envelopes |
| **App core** | `_app/*.py` | Transport-neutral workflows reused by adapters |
| **Client** | `client.py`, `_*.py` | High-level Python API, returns typed dataclasses |
| **Runtime** | `client.py`, `_client_composed.py`, `_runtime/init.py`, `_kernel.py`, runtime collaborators | `NotebookLMClient` composition root plus seam-module helpers (HTTP client lifecycle, RPC dispatch, metrics, drain bookkeeping, request-id counter, auth refresh, conversation cache, polling registry, cookie persistence) |
| **RPC** | `rpc/*.py` | Protocol encoding/decoding, method IDs |

#### Runtime seam modules

The client runtime is split across `NotebookLMClient` (composition root),
`ClientComposed` (holder), `_runtime/init.py` (construction helpers),
`_kernel.py` (HTTP client owner), and single-responsibility collaborator
modules. (The legacy `_core.py` compatibility shim was deleted in v0.5.0;
callers import directly from the canonical modules.) Each helper exposes
a narrow Protocol surface so it can be unit-tested against a stub:

| Module | Class | Responsibility |
|---|---|---|
| `_client_composed.py` | `ClientComposed` | Client-owned holder for transport, executor, chain host, middleware metadata, and session collaborator bundle. |
| `_runtime/init.py` | `RuntimeCollaborators` helpers | Validates constructor args, builds collaborators, wires middleware, and binds `ClientComposed`. |
| `_client_metrics.py` | `ClientMetrics` | `ClientMetricsSnapshot` counters, queue-wait recorders, `on_rpc_event` async callback. |
| `_transport_drain.py` | `TransportDrainTracker` | In-flight transport counters, `_TransportOperationToken`, lazy `asyncio.Condition` powering `client.drain(...)`. |
| `_reqid_counter.py` | `ReqidCounter` | Monotonic `_reqid` counter for chat backend (baseline 100000, step 100000). |
| `_runtime/auth.py` | `AuthRefreshCoordinator` | Refresh-task lifecycle, refresh lock, `AuthSnapshot` rotation. |
| `_runtime/contracts.py` | Runtime Protocols | Shared capability Protocols: `Kernel`, `RpcCaller`, and `LoopGuard`. Single-consumer capabilities stay local to their owner modules. |
| `_runtime/lifecycle.py` | `ClientLifecycle` | Loop-affinity guard, `aclose` plumbing, keepalive task wiring. |
| `_runtime/transport.py` | `RuntimeTransport` | Authenticated transport leg used by `RpcExecutor` and the middleware chain terminal. |
| `_rpc_executor.py` | `RpcExecutor` | RPC dispatch executor with direct collaborator dependencies. |
| `_request_types.py` | `AuthSnapshot`, `BuildRequest`, request materialization | Shared request construction Interface. |
| `_transport_errors.py` | transport exceptions, `parse_retry_after`, `raise_mapped_post_error` | Terminal `Kernel.post` error mapping for middleware retry/auth behavior. |
| `_streaming_post.py` | `stream_post_with_size_cap` | Low-level POST streaming and response-size guard. |
| `_conversation_cache.py` | `ConversationCache` | Per-instance true-LRU conversation cache for `ChatAPI` continuity. Caps the conversation count (`MAX_CONVERSATION_CACHE_SIZE`) and the turns retained per conversation (`MAX_TURNS_PER_CONVERSATION`). |
| `_polling_registry.py` | `PollRegistry` | Pending-poll registry shared by long-running artifact generations. |
| `_cookie_persistence.py` | `CookiePersistence` | Cookie-jar → storage-state serialization, `__Secure-1PSIDTS` rotation. |

The feature-facing surface is the set of **capability Protocols** in
`notebooklm._runtime.contracts` — `Kernel`, `RpcCaller`, and
`LoopGuard`. Single-consumer capability shapes stay in the owning
feature module (`AuthMetadata` in `_source/upload.py`,
`OperationScopeProvider` in `_artifact/polling.py`), and the unused
`AsyncWorkRuntime` composite was deleted. The broad `Session` Protocol
that previously bundled these together was deleted in the final phase
of the capability refactor (see [`docs/refactor-history.md`](refactor-history.md)
and ADR-0013); each feature now depends on the narrowest slice it needs
and takes those collaborators by keyword-only constructor argument. The
feature-local composite-runtime Protocols (`ChatRuntime`,
`ArtifactsRuntime`, `UploadRuntime`) and their adapter dataclasses that
previously bundled three capability Protocols apiece were retired once
it was clear they only hid stable collaborators with one production
satisfier; see ADR-0013 for the promotion criterion (at least two
production consumers) that still gates adding any new shared Protocol.

Private service modules sit inside the client layer but below the public
facades. They own cross-facade composition without importing sibling facades:
`_notebook_metadata.py` composes notebook metadata through a narrow source
lister, `_sharing_manager.py` owns legacy `SHARE_ARTIFACT` link behavior, and
`_mind_map.py` owns note-backed mind-map rows shared by notes and artifacts.
Facade modules keep the public method surface stable and delegate to these
services.

### Boundary Guardrails

These are the same family as the *Architecture & invariant gates* (`tests/_guardrails/`)
described below. The **pure** ones (e.g. `test_cli_boundary.py`) have been
consolidated into `tests/_guardrails/`; the **hybrids** that pair a gate with
behavioral tests (e.g. `test_public_shims.py`) keep their behavioral half in
`tests/unit/` and split the gate half into a dedicated `tests/_guardrails/`
file.

The architecture tests encode the current layer contract:

- `tests/_guardrails/test_public_surface_manifest.py` has a documented public
  import manifest. When a docs change adds or removes a supported import path,
  update the manifest in the same PR so public API drift is intentional and
  reviewable. The behavioral half of the public-shim suite (the
  `select_cited_sources` / `ResearchAPI` back-compat delegations, the
  `UnknownTypeWarning` filter behaviour, and `NotebookLMClient.rpc_call`
  forwarding) stays in `tests/unit/test_public_shims.py`.
- `tests/_guardrails/test_cli_boundary.py` parses `src/notebooklm/cli/**/*.py`
  and rejects CLI imports from `notebooklm._*`, `notebooklm.rpc.*`, or
  `_private` names exposed by public modules. Promote needed symbols through a
  public facade (`notebooklm.types`, `notebooklm.auth`, `notebooklm.research`,
  etc.) before using them from the CLI.
- Auth internals may move under `notebooklm._auth` during architecture work,
  but first-party callers continue to import through `notebooklm.auth`. The
  compatibility manifest in `tests/_guardrails/test_public_surface_manifest.py`
  enforces the current first-party surface for that move; it is not a broader
  public API decision, and removing a listed name needs a separate deprecation
  plan.
- `tests/_guardrails/test_no_facade_reach_in.py` holds the AST reach-in /
  runtime-import boundary gates: notebook metadata services must not import or
  construct `SourcesAPI`; artifact/source/notebook composition services must
  not runtime-import facade APIs. Add new private services to those guard
  lists when they take ownership of cross-facade behavior. The construction /
  init-order behaviour tests — `NotebookLMClient` constructs `SourcesAPI`
  before `NotebooksAPI` and passes it through the legacy `sources_api=` slot,
  plus the mind-map decoupling flows — stay in
  `tests/unit/test_init_order.py`.

### Key Design Decisions

**Why underscore prefixes?** Files like `_notebooks.py` are internal implementation. Public API stays clean (`from notebooklm import NotebookLMClient`).

**Why namespaced APIs?** `client.notebooks.list()` instead of `client.list_notebooks()` - better organization, scales well, tab-completion friendly.

**Why async?** Google's API can be slow. Async enables concurrent operations and non-blocking downloads.

**Naming conventions.** See [`docs/conventions.md`](./conventions.md) for the
canonical tiebreakers on waiting/polling verbs (`poll_X` / `wait_for_X` /
`wait_until_X` / `await_X` / `_wait_for_X`), RPC-callable Protocol names
(`NextCall` / `RpcCallback` / `RpcCaller`), and
metrics method verbs (`record_X` vs `emit_X`). New code should pick names
from those catalogues rather than introducing parallel patterns.

### Adding New Features

**New RPC Method:**
1. Capture traffic (see [RPC Development Guide](rpc-development.md))
2. Add to `rpc/types.py`: `NEW_METHOD = "AbCdEf"`
3. Implement in appropriate `_*.py` API class
4. Add dataclass to `types.py` if needed
5. Add CLI command if user-facing

**New API Class:**
1. Create `_newfeature.py` with `NewFeatureAPI` class.
2. Type each constructor parameter against the **narrowest shared
   capability Protocol** it actually uses (`RpcCaller`, `LoopGuard`,
   `Kernel` — see
   [`docs/architecture.md`](./architecture.md) for the protocol
   catalog). If the capability has only one consumer, define the
   Protocol locally beside that consumer instead of promoting it to
   `_runtime/contracts.py`. Pass each collaborator by keyword-only
   argument; do not bundle them into a feature-local composite-runtime
   Protocol unless a second production consumer materialises. **Do NOT
   depend on a broad runtime facade for type annotations** — there is no
   concrete `Session` class (the broad `Session` Protocol was deleted;
   see ADR-0013).
3. Add the wiring in `_client_assembly.py::_assemble_client(...)`, not
   directly in `client.py`. The assembly seam is shared by
   `NotebookLMClient.__init__` and the canonical test factory; set every
   constructor-time attribute there and thread concrete collaborators
   from `compose_client_internals(...)`.
4. **Tests** should inject the narrow collaborator the feature actually
   needs. `tests/_fixtures/fake_core.py:FakeSession` remains available
   for legacy broad-fixture tests, but new direct feature tests should
   prefer `MagicMock(spec=RpcCaller, rpc_call=AsyncMock(...))`-style
   fakes or local protocol fakes.
5. Export types from `__init__.py`.

---

## Concurrency Model

Multiple `notebooklm` processes (parallel CLI runs, an in-process keepalive
beside a cron-driven `notebooklm auth refresh`, container start-up races,
`xargs -P` fan-outs) can target the same `NOTEBOOKLM_HOME` simultaneously.
The library coordinates with **cross-process file locks** (POSIX `flock` /
Windows `LockFileEx`, via the [`filelock`](https://pypi.org/project/filelock/)
package) so reads and writes against shared on-disk state never tear or
clobber a sibling's update.

All locks are sibling files next to the resource they guard (zero-byte,
left on disk after release — `filelock` reuses them).

| Lock file | Owner | Scope | Acquisition |
|---|---|---|---|
| `<profile>/storage_state.json.lock` | `_auth/storage.py::save_cookies_to_storage` | Read-merge-write of `storage_state.json` (cookie sync after a rotation or 302) | Blocking exclusive |
| `<profile>/.storage_state.json.rotate.lock` | `_auth/keepalive.py::_poke_session` | Cross-process dedup of the `accounts.google.com/RotateCookies` keepalive POST | Non-blocking exclusive (`LOCK_NB`); skip on contention |
| `<home>/.migration.lock` | `migration.py::migrate_to_profiles` | One-shot legacy→profile layout migration on startup | Blocking exclusive, 30s timeout (raises `MigrationLockTimeoutError`) |
| `<profile>/context.json.lock` | `_atomic_io.py::atomic_update_json` through CLI context helpers | Read-modify-write of the active-notebook/account-routing context for a profile | Blocking exclusive, 10s timeout |

Design notes:

- **Two layered storage locks (not one).** The `.lock` and `.rotate.lock`
  files protect the *same* `storage_state.json` but serve different access
  patterns: a long-running save must not block — or be blocked by — a
  best-effort rotation poke. Keeping them separate prevents the keepalive
  from queueing behind a slow cookie write (and vice-versa).
- **Fail-open on lock infrastructure failure.** When the lock file itself
  cannot be created (read-only home dir, NFS without `flock`, permission
  denied), `_poke_session` proceeds *without* coordination rather than
  wedging forever. A duplicate rotation across processes is bounded and
  harmless; a permanently-suppressed rotation is not.
- **Locks are sibling files, never the resource itself.** `filelock` reuses
  the sentinel across invocations, so cleanup is not required — and a
  TOCTOU race between unlink and reacquire is avoided.
- **In-process serializers complement, not replace, file locks.**
  `_auth/keepalive.py::_poke_session` also takes an `asyncio.Lock` keyed on
  `(event_loop, profile)` to dedupe an `asyncio.gather` fan-out before
  reaching the cross-process flock — the file lock only sees one
  contender per process per rate-limit window.

Path resolution for all locked resources flows through `paths.py`
(`get_storage_path`, `get_context_path`, `get_home_dir`), so a `--storage`
override or a different `NOTEBOOKLM_PROFILE` automatically yields a distinct
lock sibling and the two invocations never contend.

---

## Testing

### Prerequisites

1. **Install dependencies** (canonical contributor flow — see [docs/installation.md#e-contributor](installation.md#e-contributor) for details):
   ```bash
   uv sync --frozen --extra browser --extra dev --extra markdown
   uv run playwright install chromium
   uv run pre-commit install
   ```

   The `browser` extra is required for the default `uv run pytest` suite because
   several unit tests import and patch `playwright.sync_api`. The command
   `uv sync --frozen --extra dev` installs the test tools, but not Playwright.

   CI runs the same lint gate with `uv run pre-commit run --all-files`, so local hook results should match the `quality` job.

2. **Authenticate:**
   ```bash
   notebooklm login
   ```

3. **Create read-only test notebook** (required for E2E tests):
   - Create notebook at [NotebookLM](https://notebooklm.google.com)
   - Add multiple sources (text, URL, etc.)
   - Generate artifacts (audio, quiz, etc.)
   - Set env var: `export NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID="your-id"`

### Quick Reference

```bash
# Unit + integration tests (no auth needed)
uv run pytest

# Fast local loop — skip repo-wide audit / release-gate checks (~40s saved).
# CI still runs these; the marker just lets you iterate quickly.
uv run pytest tests/unit tests/integration -m "not repo_lint"

# E2E tests (requires auth + test notebook)
uv run pytest tests/e2e -m readonly        # Read-only tests only
uv run pytest tests/e2e -m "not variants"  # Skip parameter variants
uv run pytest tests/e2e --include-variants # All tests including variants

# Select a profile for E2E tests
uv run pytest tests/e2e -m e2e --profile work
```

The `repo_lint` marker tags cassette-shape lint, public-surface scans,
docstring/install-doc drift guards, version-sync, and CI-script audits.
These are valuable release/CI guardrails but cost ~30–45s locally. See
[`CONTRIBUTING.md`](../CONTRIBUTING.md#fast-local-loop-skip-repo-wide-audit-checks)
for the canonical fast-loop guidance.

### Selecting a profile for E2E tests

The E2E suite picks up the active NotebookLM profile from (highest precedence first):

1. `--profile <name>` pytest flag
2. `NOTEBOOKLM_PROFILE` environment variable
3. `default_profile` from `~/.notebooklm/config.json`
4. `default`

The auto-created notebook ID cache files
(`generation_notebook_id`, `multi_source_notebook_id`) are written under the
active profile directory (`~/.notebooklm/profiles/<name>/`), so each profile
keeps its own cache and never reuses notebook IDs from another Google account.

#### Notebook ID env vars are profile-agnostic

The notebook ID env vars (`NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID`,
`NOTEBOOKLM_GENERATION_NOTEBOOK_ID`, `NOTEBOOKLM_MULTI_SOURCE_NOTEBOOK_ID`)
are **not** profile-scoped — they're read as-is regardless of which profile
is active. If you set them in `.env` and switch profiles, the test will try
to access notebooks that don't exist in the other Google account.

**Recommendation:** leave the generation/multi-source env vars unset and let
the per-profile cache files handle it. Only `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID`
needs to be set; if you switch profiles often, override it inline:

```bash
NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID=<work-nb-id> \
  uv run pytest tests/e2e -m e2e --profile work
```

### Test Structure

```
tests/
├── unit/                            # No network, fast, mock everything
├── _guardrails/                     # Architecture/invariant gates (custom AST + filesystem lint)
├── _baselines/                      # Regenerable-baseline registry (ADR-0022): derive/store/compare
├── fixtures/
│   └── baselines/                   # Committed derived baselines (types_all.json, ungated_surface.json)
├── integration/                     # Mocked HTTP responses + VCR cassettes
│   ├── test_artifacts_integration.py # ArtifactsAPI integration
│   ├── test_artifacts_drift.py      # CREATE_ARTIFACT payload drift guard
│   ├── test_auth_refresh_vcr.py     # Auth refresh token VCR test
│   ├── test_auto_refresh.py         # Keepalive/refresh integration
│   ├── test_chat_delete_conversation_vcr.py
│   ├── test_chat_multi_source_vcr.py
│   ├── test_chat_passage_resolver.py
│   ├── test_cli_session_local.py
│   ├── test_download_multi_artifact.py
│   ├── test_error_paths_vcr.py      # Synthetic and VCR error paths
│   ├── test_get_summary_drift.py    # GET_NOTEBOOK_SUMMARY drift guard
│   ├── test_notebooks_integration.py # NotebooksAPI integration
│   ├── test_notes_integration.py     # NotesAPI integration
│   ├── test_notes_idempotency.py
│   ├── test_polling_vcr.py
│   ├── test_research_deep_poll_vcr.py
│   ├── test_research_idempotency.py
│   ├── test_save_chat_as_note_integration.py
│   ├── test_session_integration.py  # Client init + RPC plumbing
│   ├── test_settings_integration.py  # SettingsAPI integration
│   ├── test_settings_vcr.py
│   ├── test_sharing_integration.py   # SharingAPI integration
│   ├── test_sharing_vcr.py
│   ├── test_skill_packaging.py      # Packaging smoke (skills, entry-points)
│   ├── test_sources_integration.py   # SourcesAPI integration
│   ├── test_vcr_comprehensive.py    # End-to-end VCR walkthrough
│   ├── test_vcr_example.py          # VCR pattern reference
│   ├── test_vcr_real_api.py         # VCR against real-API cassettes
│   ├── cli_vcr/                     # CLI → Client → RPC VCR tests
│   └── concurrency/                 # Cross-process / asyncio races
└── e2e/                             # Real API calls (requires auth)
```

The `*_drift.py` tests are payload-shape canaries: they decode a recorded
RPC response (or assemble a synthetic one) and assert the live decoder still
produces the expected dataclass. They fail loudly when Google changes a
payload field, so the failure shows up here before users hit it.

### Architecture & invariant gates (`tests/_guardrails/`)

`tests/_guardrails/` holds the project's **custom lint gates** — pytest tests that
enforce architectural decisions a general-purpose linter can't express. They are
not style checks; each file encodes one project-specific invariant, usually the
executable half of an ADR ("enforce, don't document" — un-enforced consistency
is the failure mode this directory exists to prevent).

**What belongs here vs `tests/unit/`.** This directory is the home for a *pure*
gate — a file whose whole purpose is enforcing a repo-wide invariant, with no
module-under-test. A unit test that only *embeds* a boundary assertion among
behavioral checks stays in `tests/unit/` (see *Boundary Guardrails* above). Pure
architecture gates — e.g. `test_cli_boundary.py`, `test_cassette_shapes.py`,
`test_public_surface.py` — have been consolidated into this directory; the gate
halves of former hybrids live alongside them (e.g.
`test_public_surface_manifest.py`, `test_no_facade_reach_in.py`).

**How they differ from ruff / mypy.** Ruff and mypy run in the `quality` job and
enforce *generic* rules (style, unused imports, types) from a fixed catalogue.
The `tests/_guardrails/` gates are collected by the normal `uv run pytest` run and
enforce *bespoke* rules by doing their own analysis: most parse the source with
`ast.parse` (or scan files with regex / `rglob`), and some **import the module and reflect on
the live object** — something a purely-static linter cannot do.

A representative slice (run `ls tests/_guardrails/` for the full set):

| Gate | Enforces |
|---|---|
| `test_no_raw_positional_rpc_indexing.py` | No chained positional indexing (`x[0][9][3]`) of `batchexecute` payloads outside the sanctioned `_row_adapters/` — the project's #1 fragility class |
| `test_rpc_method_ids_only_in_types.py` | Obfuscated RPC IDs live only in `rpc/types.py` (the source of truth) |
| `test_no_forbidden_monkeypatches.py` | The forbidden monkeypatch shapes under `tests/` (ADR-0007) |
| `test_no_inline_deprecation_warnings.py` | No inline `warnings.warn(..., DeprecationWarning)` outside `_deprecation.py` (ADR-0018) |
| `test_cli_rpc_envelope.py` | Every *RPC-touching* Click leaf command (call graph reaches `NotebookLMClient`) routes its errors into the JSON envelope |
| `test_module_size_ratchet.py` | No module grows past the size budget (ADR-0008) — a burn-down ratchet |
| `test_v080_release_gate.py` | The v0.8.0 breaking-change set flips in lockstep at the version bump |
| `test_adr_reference_format.py` | ADR references are 4-digit and resolve to a real `docs/adr/NNNN-*.md` |
| `test_cli_boundary.py` | CLI modules import only public `notebooklm` surface — no `notebooklm._*` / `notebooklm.rpc.*` / `_private` reach-in |
| `test_no_facade_reach_in.py` | Feature APIs and service modules don't reach into Session internals or runtime-import facade APIs |
| `test_public_surface_manifest.py` | The documented public-import manifest + re-export identity pins for `notebooklm` / `auth` / `types` / shims stay intact |

**Conventions when adding a gate:**

- **One invariant per file**, with a module docstring that states the rule, *why*
  it matters (cite the ADR), and how a violation is fixed. The assertion message
  is the contributor's first — and often only — explanation, so make it
  actionable.
- **Make the detector a pure function and self-test it** against known good/bad
  inputs in the same file, so the gate can't silently become vacuous (a regex
  that matches nothing must fail its own self-test, not pass everything).
- **Shrink-only allowlists.** A gate that would fail on pre-existing violations
  may grandfather them in an allowlist — but it must be a *one-way ratchet* that
  only shrinks (e.g. `test_module_size_ratchet.py`,
  `tests/scripts/check_method_coverage.py`). The rule lands without a giant
  cleanup PR, and the gate fails when an allowlisted entry becomes clean so it
  gets removed.
- **Scan yourself too.** A gate that shows the *wrong* form in its examples
  should use placeholders (or build them at runtime) rather than excluding its
  own file, so it still polices its own references
  (`test_adr_reference_format.py`).

Most gates are fast and run in the normal loop; the slow repo-wide cassette scan
(`test_cassettes_clean.py`) carries the `repo_lint` marker (see
[Quick Reference](#quick-reference)).

**Trade-off.** Because some gates import internals and reflect on them, they
couple more tightly to implementation than a static linter — a
behavior-preserving refactor can still trip one. That coupling is deliberate: it
catches architecture drift that ruff and mypy structurally cannot see.

### Updating baselines

Some gates freeze a *snapshot* of a value the code already derives, so a public
surface change is a deliberate, diff-visible act. These **regenerable baselines**
(ADR-0022) are registered in `tests/_baselines/registry.py` and committed under
`tests/fixtures/baselines/` (plus the CLI contract at
`tests/fixtures/cli_contract_baseline.json`):

| Baseline | Derives from | Committed file |
|---|---|---|
| `types_all` | `notebooklm.types.__all__` | `tests/fixtures/baselines/types_all.json` |
| `ungated_surface` | collected `__all__` of each ungated public module | `tests/fixtures/baselines/ungated_surface.json` |
| `cli_contract` | `build_cli_contract()` | `tests/fixtures/cli_contract_baseline.json` |

The freeze test `test_baseline_matches_committed_file` (in
`tests/_guardrails/test_public_surface_manifest.py`) asserts each committed file
equals `derive()`. When you intentionally change a public surface — e.g. add an
export to `notebooklm.types.__all__` or a CLI option — that test fails. Regenerate
the committed files in the **same PR**:

```bash
python scripts/regen_baselines.py
git diff tests/fixtures/baselines tests/fixtures/cli_contract_baseline.json
```

Review the diff — each changed line is the deliberate acknowledgement of the
surface change. Regen is **dev-only**: it shells `pytest … --update-baselines`,
which both the wrapper and the `update_baselines` fixture refuse to run when a
`CI` environment is detected. **CI never regenerates — it only diffs.**

### VCR Testing (Recorded HTTP)

VCR tests record HTTP interactions for offline, deterministic replay. We have two levels:

**Client-level VCR tests** (`tests/integration/test_vcr_*.py`):
- Test Python API methods directly
- Verify RPC encoding/decoding with real responses

**CLI VCR tests** (`tests/integration/cli_vcr/`):
- Test the full CLI → Client → RPC path
- Use Click's CliRunner with VCR cassettes
- Verify CLI commands work end-to-end without mocking the client

```bash
# Run all VCR tests
uv run pytest tests/integration/

# Run only CLI VCR tests
uv run pytest tests/integration/cli_vcr/
```

Sensitive data (cookies, tokens, emails) is automatically scrubbed from cassettes.

### Cassette recording

Maintainers re-record cassettes against the live API when an RPC payload
shape changes. Recording is opt-in (`NOTEBOOKLM_VCR_RECORD=1`) and requires
a valid `notebooklm login` session.

Two notebook env vars steer which notebook the recording session targets.
**Neither UUID is committed** — both are per-maintainer secrets (notebook IDs
are linkable to a Google account):

| Env var | Used by | Notebook role |
|---------|---------|---------------|
| `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID` | read-heavy cassettes (`list`, `download`, `get`) | A maintainer-owned notebook pre-populated with sources + artifacts. Tests only READ from it. |
| `NOTEBOOKLM_GENERATION_NOTEBOOK_ID` | mutation/generation cassettes (`add source`, `generate`, `delete`) | A **separate** maintainer-owned notebook used only for destructive/generation flows, so the read-only notebook stays pristine. |

#### One-time setup — generation notebook

Run the setup script once per Google account that records cassettes:

```bash
uv run python tests/scripts/setup-generation-notebook.py
```

The script is idempotent: it reuses the notebook whose title matches
`GENERATION_NOTEBOOK_TITLE` (defined in `tests/scripts/setup-generation-notebook.py`)
if one already exists, otherwise creates it.
It prints the notebook UUID and an `export` line. Copy the export line into
your maintainer environment (e.g. `~/.zshrc` or a profile-specific `.env`
file you do NOT commit):

```bash
export NOTEBOOKLM_GENERATION_NOTEBOOK_ID=<printed-uuid>
```

The script is a manual maintainer helper — CI never runs it.

#### Recording a cassette

```bash
# Re-record (or record-new) cassettes; sensitive data auto-scrubbed
NOTEBOOKLM_VCR_RECORD=1 uv run pytest tests/integration/test_vcr_*.py -v
```

> **Recording reads your real `~/.notebooklm` profile.** Normally the suite
> pins `NOTEBOOKLM_HOME` at a throwaway tmp dir (autouse `_isolate_notebooklm_home`
> in `tests/conftest.py`) so runs are reproducible and never touch your real
> profile. Under `NOTEBOOKLM_VCR_RECORD=1`, `@pytest.mark.vcr` tests instead read
> the real profile — so `get_vcr_auth()` (via `AuthTokens.from_storage()`) and
> the CLI auth path resolve live credentials to record against. CLI-VCR tests
> additionally skip their `mock_auth_for_vcr` patch in record mode for the same
> reason. Replay runs and non-VCR tests stay isolated (a stray
> `NOTEBOOKLM_VCR_RECORD` on a normal run never un-isolates a non-VCR test). The
> test must carry the `vcr` marker — most do via a module-level `pytestmark`.
> Before #1263 this deferral did not exist and cassettes could only be recorded
> with a standalone script.
>
> **Limitation:** a few cli_vcr tests (`settings` / `profile` / `doctor`)
> re-pin `NOTEBOOKLM_HOME` at their own tmp dir to isolate config/profile
> writes. They override this deferral and so are not auto-recordable through
> pytest — re-record those with a standalone script (or, as a future
> enhancement, inject `NOTEBOOKLM_AUTH_JSON` from the real storage so auth is
> resolved independently of `NOTEBOOKLM_HOME`).

The scrubbing pipeline (`tests/vcr_config.py`) redacts cookies, CSRF tokens,
emails, and other sensitive patterns before the cassette hits disk. Verify
the result with the cassette guard before committing:

```bash
# Verify recorded cassettes are clean of credentials
uv run python tests/scripts/check_cassettes_clean.py
```

#### Long-running recordings (deep research, multi-minute polling)

Recording a cassette that polls a multi-minute server-side operation — the Deep
Research lifecycle (`test_research_deep_poll_vcr.py`) is the canonical example —
hits a few non-obvious snags. Lessons from the v0.8 full-lifecycle re-record
(PR #1566):

- **`httpx.PoolTimeout` after ~15–20 min of idle polling.** The default
  `ConnectionLimits(keepalive_expiry=30.0)` keeps an idle pooled connection
  around long enough to be silently dropped server-side, and the next acquire
  stalls. In **record mode only**, build the client with a shorter keepalive and
  a generous read timeout:
  `NotebookLMClient(auth, timeout=60.0, limits=ConnectionLimits(keepalive_expiry=10.0))`.
  Note `async_client_factory` is **not** a public constructor kwarg — use the
  public `timeout=` / `limits=` seams.
- **`pytest-timeout` kills the run.** The global per-test timeout aborts a
  ~30-min recording. Mark the recording test `@pytest.mark.timeout(3600)`.
- **`start()` task_id ≠ the poll-reported task_id.** Deep Research's kickoff id
  is not the id `POLL_RESEARCH` echoes back, so a filtered
  `research.poll(task_id=…)` returns `NOT_FOUND` every poll. The record loop must
  mirror `wait_for_completion`: first poll unfiltered, then pin the
  *poll-reported* id.
- **Trim with a byte-exact text slice, not `yaml.safe_dump`.** Long deep-research
  poll bodies accumulate large markdown, so trim redundant middle `in_progress`
  polls to stay under the cassette size cap. Re-serializing via `yaml.safe_dump`
  re-wraps long scalars and breaks Windows YAML parsing (CI catches it) — slice
  the VCR-native YAML text instead.
- **Cleanliness is necessary-not-sufficient.** After recording, run the cassette
  guard (above) *and* manually grep the new file for live cookie/token/email
  shapes (`SID` / `HSID` / `SAPISIDHASH` / Bearer / the account email) — the
  name-anchored scrubber can miss credentials in un-allowlisted fields.

#### Synthetic error cassettes

> [!WARNING]
> **Error cassettes generated through this plumbing are SYNTHETIC.** They
> validate the client's exception-mapping branches (`RateLimitError`,
> `ServerError`, the auth-refresh path), NOT Google's actual error response
> shapes. If you need to validate a real-world error shape, capture a live
> recording instead — these synthetic shapes are intentionally minimal.

The `NOTEBOOKLM_VCR_RECORD_ERRORS` env var opts a recording session into
substituting the next outgoing batchexecute RPC with a synthetic error
response. Three modes are supported:

| Mode            | HTTP status | Maps to                                         |
|-----------------|-------------|-------------------------------------------------|
| `429`           | 429         | `RateLimitError` (after retry budget exhausted) |
| `5xx`           | 500         | `ServerError`   (after retry budget exhausted) |
| `expired_csrf`  | 400         | auth-refresh path (NotebookLM uses 400, not 401)|

The plumbing has three opt-in layers:

1. **Env var**: `NOTEBOOKLM_VCR_RECORD_ERRORS=<mode>` activates the
   `ErrorInjectionMiddleware` in the middleware chain (the env var is
   consulted when the client opens).
2. **Pytest marker**: `@pytest.mark.synthetic_error("<mode>")` sets the env
   var for the duration of a single test (auto-reverted on teardown). Note
   that the `synthetic_error` marker is registered dynamically in
   `tests/conftest.py:149` (rather than statically listed in `pyproject.toml`).
3. **Filename prefix**: cassettes recorded under this mode MUST be named
   `error_synthetic_<mode>_<slug>.yaml` — use
   `tests.cassette_patterns.synthetic_error_cassette_name(mode, slug)` to
   build the filename so reviewers can tell synthetic shapes apart from
   real recordings at a glance.

Example recording session (this is the workflow a maintainer uses to
record the actual error cassettes — the transport-wrapper module itself
ships only the plumbing):

```bash
NOTEBOOKLM_VCR_RECORD=1 \
NOTEBOOKLM_VCR_RECORD_ERRORS=429 \
  uv run pytest tests/integration/test_error_paths_vcr.py
```

Production behavior is unchanged when `NOTEBOOKLM_VCR_RECORD_ERRORS` is
unset — the transport wrapper is only constructed when the env var resolves
to a recognized mode, and a typo'd value resolves to `None` (the recording
session continues without substitution).

### Per-method RPC coverage gate

`tests/scripts/check_method_coverage.py` enforces, on every PR, that each
member of `RPCMethod` has **both**:

1. **A test reference** — at least one file under `tests/` (excluding the
   gate script itself) mentions the enum member by its qualified name
   (`RPCMethod.LIST_NOTEBOOKS`) OR by its raw RPC id string value
   (`"wXbhsf"`).
2. **A cassette covering the RPC id** — at least one cassette YAML under
   `tests/cassettes/` contains the RPC id string in its body.

The gate is a pure-text static check (no pytest, no network) and runs in the
`quality` job of `test.yml`.

**Adding a new `RPCMethod`?** Ship it with:
- a unit or integration test that imports the enum member (or asserts on its
  raw id), AND
- at least one cassette whose recorded request/response body contains the
  RPC id.

**Pre-existing gaps.** A small `PREEXISTING_GAPS` set inside the script can
grandfather methods that lacked coverage when the gate first landed. It is
currently empty. The set is a **one-way ratchet** — it must not grow. When
you backfill coverage for a grandfathered method, delete its entry from
`PREEXISTING_GAPS` in the same PR. The gate fails when a stale
`PREEXISTING_GAPS` entry has acquired full coverage so maintainers remove it.

```bash
# Run locally before pushing changes that touch RPCMethod
uv run python tests/scripts/check_method_coverage.py
```

### E2E Fixtures

| Fixture | Use Case |
|---------|----------|
| `read_only_notebook_id` | List/download existing artifacts |
| `temp_notebook` | Add/delete sources (auto-cleanup) |
| `generation_notebook_id` | Generate artifacts (CI-aware cleanup) |

### Rate Limiting

NotebookLM has undocumented rate limits. Generation tests may be skipped when rate limited:
- Use `uv run pytest tests/e2e -m readonly` for quick validation
- Wait a few minutes between full test runs
- `SKIPPED (Rate limited by API)` is expected behavior, not failure

### Writing New Tests

```
Need network?
├── No → tests/unit/
├── Mocked → tests/integration/
└── Real API → tests/e2e/
    └── What notebook?
        ├── Read-only → read_only_notebook_id + @pytest.mark.readonly
        ├── CRUD → temp_notebook
        └── Generation → generation_notebook_id
            └── Parameter variant? → add @pytest.mark.variants
```

---

## Logging and observability

### Levels — when to emit what

- **WARNING** — data loss, protocol drift, schema mismatch, unexpected non-2xx that isn't auth-recoverable. Actionable.
- **INFO** — coarse-grained lifecycle events (login complete, profile switched). Rare in library code; CLI uses INFO for user-facing progress.
- **DEBUG** — expected fallbacks, hot-path parser branches, polling status, request/response metadata. Off by default; enable via `NOTEBOOKLM_LOG_LEVEL=DEBUG` or `notebooklm -vv`.
- **Silent + comment** — best-effort discovery loops (browser cookie scan, alternative profile locations). `except` body is `pass` or `continue` with a single-line `# best-effort: <what we tried>` comment.

### Credential redaction

The package handler installed by `configure_logging()` has a `RedactingFilter` attached. It runs for every record reaching the handler, including records originating in child loggers (`notebooklm._rpc_executor`, `notebooklm._transport_errors`, `notebooklm._chat`, etc.) via Python logging's default propagation. The filter scrubs:

- CSRF tokens (`at=...`)
- Session IDs (`f.sid=...`)
- Google session cookies (`SAPISID`, `SID`, `HSID`, `SSID`, `__Secure-1PSID`, `__Secure-3PSID`)
- `Authorization: Bearer <token>` headers
- `Cookie: <jar>` headers

The filter pre-renders `record.exc_info` traceback into a scrubbed `record.exc_text` while preserving `record.exc_info` itself. The live exception object is not mutated.

To add a new secret pattern: edit `_REDACT_PATTERNS` in `src/notebooklm/_logging.py` and add a unit test in `tests/unit/test_logging.py` before merging.

### Attaching your own handler

`notebooklm` propagates to root by default, so `caplog`, `basicConfig`, and similar workflows work without configuration. To capture notebooklm logs in a dedicated handler:

```python
import logging
from notebooklm._logging import apply_redaction

handler = logging.handlers.SysLogHandler(...)
apply_redaction(handler)
logging.getLogger("notebooklm").addHandler(handler)
```

`apply_redaction()` attaches the `RedactingFilter` and wraps the formatter so your handler also benefits from credential scrubbing.

### Style — always lazy formatting

Use `%`-style log calls, not f-strings:

```python
logger.warning("Failed for %s in %.2fs", name, elapsed)  # OK
logger.warning(f"Failed for {name} in {elapsed:.2f}s")    # BAD
```

f-string eager evaluation defeats lazy formatting and (although the filter would still scrub via `record.getMessage()`) makes profile-time cost unconditional.

### Third-party loggers

`httpx`, `urllib3`, and `asyncio` can emit at DEBUG with full URLs and headers containing notebooklm-py credentials.

For `httpx` and `urllib3`, `configure_logging()` (run automatically at package import) attaches a *logger-level* `RedactingFilter` to each. That filter runs before records propagate to ancestor loggers, so a library consumer who enables those loggers via `logging.basicConfig(level=logging.DEBUG)` gets credential-scrubbed request URLs and headers with no extra setup — and without notebooklm-py adding any handler of its own to those loggers.

If you also want those loggers to *emit* through notebooklm-py's default handler (the CLI does this when `-vv` is set), call `install_redaction`, which adds both the filter and a default StreamHandler:

```python
from notebooklm.log import install_redaction
install_redaction("httpx", "urllib3")
```

To cover additional third-party loggers (e.g. `asyncio`) or libraries that set `propagate=False` on internal loggers (rare), pass the names explicitly:

```python
install_redaction("asyncio")
install_redaction("httpx._client", "urllib3.connectionpool")
```

### Trade-offs

The `RedactingFilter` preserves `record.exc_info` (the live exception object) so handlers like Sentry can still access it. However:

- Standard `logging.Formatter` uses `record.exc_text` (scrubbed by our filter) and does NOT re-render from `exc_info`. Safe.
- Custom formatters that ignore `exc_text` and read `exc_info` directly may render an unredacted traceback. **Mitigation**: wrap such handlers with `apply_redaction()` so the formatter is decorated and post-scrubs the final output regardless of which exception attribute it reads.
- Records propagate to root by default (`notebooklm.propagate = True`) so `caplog` and `basicConfig` work without changes. Our filter mutates the record before propagation, so downstream handlers (including root's) see the scrubbed version. **Caveat**: if a user attaches an unredacted handler directly to a child logger (`notebooklm._rpc_executor`), that handler fires *before* propagation reaches our parent handler. Mitigation: `apply_redaction(child_handler)`.
- Applications that want notebooklm logs *isolated* from root can set `logging.getLogger('notebooklm').propagate = False` themselves.

---

## CI/CD

### Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `test.yml` | Push/PR | Unit tests, linting, type checking |
| `nightly.yml` | Daily 6 AM UTC (`main`), manual dispatch for `release/*` | E2E tests with real API |
| `rpc-health.yml` | Daily 7 AM UTC (`main`), manual dispatch for `release/*` | RPC method ID monitoring (see [stability.md](stability.md#automated-rpc-health-check)) |
| `testpypi-publish.yml` | Manual dispatch | Publish to TestPyPI |
| `verify-package.yml` | Manual dispatch | Verify TestPyPI or PyPI install + E2E |
| `publish.yml` | Tag push | Publish to PyPI |

### Setting Up Nightly E2E Tests

1. Get storage state: `cat ~/.notebooklm/storage_state.json`
2. Add GitHub secrets:
   - `NOTEBOOKLM_AUTH_JSON`: Storage state JSON
   - `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID`: Your test notebook ID

Scheduled canaries target `main` only. Release canaries are manual: dispatch
`nightly.yml` or `rpc-health.yml` with `custom_branch=release/vX.Y.Z`.

### Maintaining Secrets

| Task | Frequency |
|------|-----------|
| Refresh credentials | Every 1-2 weeks |
| Check nightly results | Daily |

### Workflow secret gates

Every workflow that consumes user-provided secrets (`secrets.NOTEBOOKLM_AUTH_JSON`,
`secrets.NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID`, `secrets.CLAUDE_CODE_OAUTH_TOKEN`, …)
is wrapped in at least one of three gates so that a non-maintainer cannot exfiltrate
credentials by dispatching a workflow on a feature branch:

| Gate | Where | Mechanism |
|------|-------|-----------|
| `environment: protected-readonly` | Job-level | GitHub Environment hosting the canonical secret values. Bind it **unconditionally** (`environment: protected-readonly`) so every trigger — scheduled `cron` and `workflow_dispatch` alike — sees the same secret. **Note:** the earlier conditional form (`${{ github.event_name == 'workflow_dispatch' && 'protected-readonly' \|\| '' }}`) silently broke scheduled crons once the secrets stopped existing at repo level (issue #1009); the same env binding is now the single source of truth. If you want to block `workflow_dispatch` behind manual approval, add a **required reviewers** rule on the environment — but be aware scheduled runs will then queue at the same gate. |
| `needs.<job>.outputs.is_standard == 'true'` | Job/step-level `if:` | Pin secret-using jobs or steps to standard branches (`main` / `release/*` / scheduled cron). Non-standard branches skip outright — no secret values land in the runner env. |
| `github.event.sender.login == 'teng-lin'` | Job-level `if:` | Pin webhook-triggered workflows (e.g. `claude.yml`) to a specific maintainer actor. Any other actor's trigger never reaches the secret-bearing steps. |

`scripts/check_workflow_secret_gates.py` (wired into the `test.yml` quality job)
asserts every workflow file in `.github/workflows/` satisfies at least one of
the above gates for every `secrets.*` reference (except `secrets.GITHUB_TOKEN`,
which is covered separately by `scripts/check_workflow_permissions.py`).

The checker also **rejects the conditional `environment:` shape** outright:

```yaml
# REJECTED (silently broke #1009 once the secret was migrated env-only)
environment: ${{ github.event_name == 'workflow_dispatch' && 'protected-readonly' || '' }}

# REQUIRED — unconditional binding
environment: protected-readonly
```

The empty-string fallback in the expression form means "no environment", so
secrets that live only in environments resolve to empty under that branch.
Binding the environment unconditionally is the single source of truth.

Additionally, **every job that consumes `NOTEBOOKLM_AUTH_JSON` runs a
fail-fast preflight step** (`if [ -z "$NOTEBOOKLM_AUTH_JSON" ]; then exit 1`)
before the test/script step. Without the preflight, an empty secret would
let pytest skip every auth-requiring test silently and the job would land
green with 0 tests run (issue #1009). The preflight surfaces an `::error::`
annotation linked to the secret-config misconfig so the failure is visible
in the GitHub UI rather than hidden behind "0 passed".

#### One-time GitHub Environment setup

The `protected-readonly` environment must be configured in the GitHub repository
settings before any workflow that references it can run **with an approval gate**.

> **Important — silent auto-creation**: GitHub Actions silently creates a
> referenced environment that doesn't exist, with **no protection rules**, the
> first time a workflow references it. A typo in the environment name (e.g.
> `protectd-readonly`) or a never-configured environment would therefore
> bypass maintainer approval at runtime even though the workflow YAML appears
> to gate on it. The static checker `scripts/check_workflow_secret_gates.py`
> pins the accepted environment names to an explicit allow-list
> (`_APPROVED_ENVIRONMENTS`) to prevent typos from passing CI — but the
> *runtime* gate still depends on the manual setup below being done correctly.
> Verify by triggering a `workflow_dispatch` and confirming the run pauses at
> "Waiting for review" before any secret is exposed.

This is a manual UI/API step — Pull Requests cannot create environments on
their own.

1. Open the repository on GitHub and navigate to
   **Settings → Environments → New environment**.
2. Name the environment **`protected-readonly`** (exact spelling — the workflow
   YAML files match this string verbatim, and the checker enforces the same
   spelling).
3. Under **Deployment protection rules**, enable **Required reviewers** and add
   the maintainer GitHub account (e.g. `teng-lin`) to the reviewer list.
4. Leave **Wait timer** at `0` minutes (manual approval is the gate; we don't
   need a cool-down).
5. Save. The environment is now ready; the next `workflow_dispatch` against
   `verify-package.yml`, `verify-artifacts.yml`, `rpc-health.yml`, or
   `nightly.yml` will pause at the maintainer-approval prompt before any
   secret resolves.
6. **Smoke-test the gate.** Dispatch one of the workflows above from a
   non-maintainer account (or from the maintainer account if no second
   account is available — the approval prompt should still fire) and
   confirm the run pauses at "Waiting for review" instead of immediately
   acquiring secrets. If the run does not pause, the environment was not
   configured correctly; do not rely on the gate until this smoke-test
   passes.

For automation-driven setup (e.g. infrastructure-as-code), the same configuration
can be applied via the GitHub REST API:

```bash
gh api -X PUT \
  /repos/teng-lin/notebooklm-py/environments/protected-readonly \
  -f 'wait_timer=0' \
  -f 'reviewers[][type]=User' \
  -F 'reviewers[][id]=<github-user-id-for-teng-lin>'
```

#### Adding a new secret-bearing workflow

When introducing a workflow that touches `secrets.*`:

1. Pick the gate shape that matches the trigger surface:
   - `workflow_dispatch` only → job-level `environment: protected-readonly`.
   - `workflow_dispatch` + `schedule` → also job-level `environment: protected-readonly`
     (unconditional — issue #1009). Pair with an upstream `is_standard`
     gate so a non-maintainer's feature-branch dispatch can't reach the
     secret-bearing job at all.
   - Webhook-triggered (`issue_comment`, etc.) → job-level `if:` pinning
     `sender.login` to the maintainer.
   - Multi-branch CI (`push`, `pull_request`, nightly) → step-level `if:`
     referencing an upstream `is_standard` output.
2. Run `python scripts/check_workflow_secret_gates.py` locally to verify the
   gate is recognised.
3. If the new workflow references the `protected-readonly` environment for
   the first time, **double-check the Environment exists** (see "One-time
   GitHub Environment setup" above). GitHub Actions will **silently
   auto-create** a referenced environment that doesn't exist, **with no
   protection rules**, so a never-configured `protected-readonly`
   environment would let the workflow run without any approval gate —
   exactly the opposite of what the YAML implies. The static checker
   rejects unapproved *names* via `_APPROVED_ENVIRONMENTS`, but it cannot
   verify that GitHub-side configuration has actually been applied; that
   verification is the maintainer's responsibility per the smoke-test
   step in "One-time GitHub Environment setup".

### Troubleshooting CI/CD Auth

**First step:** Run `notebooklm auth check --json` in your workflow to diagnose issues.

#### "NOTEBOOKLM_AUTH_JSON environment variable is set but empty"

**Cause:** The `NOTEBOOKLM_AUTH_JSON` env var is set to an empty string.

**Solution:**
- Ensure the GitHub secret is properly configured
- Check the secret isn't empty or whitespace-only
- Verify the workflow syntax: `${{ secrets.NOTEBOOKLM_AUTH_JSON }}`

#### "must contain valid Playwright storage state with a 'cookies' key"

**Cause:** The JSON in `NOTEBOOKLM_AUTH_JSON` is missing the required structure.

**Solution:** Ensure your secret contains valid Playwright storage state JSON:
```json
{
  "cookies": [
    {"name": "SID", "value": "...", "domain": ".google.com", ...},
    ...
  ],
  "origins": []
}
```

#### "Cannot run 'login' when NOTEBOOKLM_AUTH_JSON is set"

**Cause:** You're trying to run `notebooklm login` in CI/CD where `NOTEBOOKLM_AUTH_JSON` is set.

**Why:** The `login` command saves to a file, which conflicts with environment-based auth.

**Solution:**
- Don't run `login` in CI/CD - use the env var for auth instead
- If you need to refresh auth, do it locally and update the secret

#### Session expired in CI/CD

**Cause:** Google sessions expire periodically (typically every 1-2 weeks).

**Solution:**
1. Re-run `notebooklm login` locally
2. Copy the contents of `~/.notebooklm/storage_state.json`
3. Update your GitHub secret

#### Multiple accounts in CI/CD

Use separate secrets and set `NOTEBOOKLM_AUTH_JSON` per job:

```yaml
jobs:
  account-1:
    env:
      NOTEBOOKLM_AUTH_JSON: ${{ secrets.NOTEBOOKLM_AUTH_ACCOUNT1 }}
    steps:
      - run: notebooklm list

  account-2:
    env:
      NOTEBOOKLM_AUTH_JSON: ${{ secrets.NOTEBOOKLM_AUTH_ACCOUNT2 }}
    steps:
      - run: notebooklm list
```

#### Debugging CI/CD auth issues

Add diagnostic steps to your workflow:

```yaml
- name: Debug auth
  run: |
    # Comprehensive auth check (preferred)
    notebooklm auth check --json

    # Check if env var is set (without revealing content)
    if [ -n "$NOTEBOOKLM_AUTH_JSON" ]; then
      echo "NOTEBOOKLM_AUTH_JSON is set (length: ${#NOTEBOOKLM_AUTH_JSON})"
    else
      echo "NOTEBOOKLM_AUTH_JSON is not set"
    fi
```

The `auth check --json` output shows:
- Whether storage/env var is being used
- Which cookies are present
- Cookie domains (important for regional users)
- Any validation errors

---

## Getting Help

- Check existing implementations in `_*.py` files
- Look at test files for expected structures
- See [RPC Development Guide](rpc-development.md) for protocol details
- See [CONTRIBUTING.md](../CONTRIBUTING.md) for install, lint, and PR workflow
- Open an issue with captured request/response (sanitized)
