# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**IMPORTANT:** Follow documentation rules in [CONTRIBUTING.md](CONTRIBUTING.md) - especially the file creation and naming conventions.

## Project Overview

`notebooklm-py` is an unofficial Python client for Google NotebookLM that uses undocumented RPC APIs. The library enables programmatic automation of NotebookLM features including notebook management, source integration, AI querying, and studio artifact generation (podcasts, videos, quizzes, etc.).

**Critical constraint**: This uses Google's internal `batchexecute` RPC protocol with obfuscated method IDs that Google can change at any time. All RPC method IDs in `src/notebooklm/rpc/types.py` are undocumented and subject to breakage.

## Development Commands

```bash
# Canonical contributor install (respects uv.lock; full guide: docs/installation.md)
uv sync --frozen --extra browser --extra dev --extra markdown
source .venv/bin/activate
uv run playwright install chromium

# Run all tests (excluding e2e by default)
uv run pytest

# Run with coverage
uv run pytest --cov

# Run e2e tests (requires authentication)
uv run pytest tests/e2e -m e2e

# CLI testing
uv run notebooklm --help
```

## Pre-Commit Checks

The pre-commit hook (`.pre-commit-config.yaml`) runs ruff formatting + linting automatically on staged files.

Before pushing, also run mypy + pytest manually to avoid CI failures:
```bash
uv run mypy src/notebooklm --ignore-missing-imports
uv run pytest
```

## Architecture

### Layered Design

```
CLI Layer (cli/)
    ↓
Client Runtime Layer (client.py + _*.py APIs/collaborators)
    ↓
RPC Layer (rpc/)
```

1. **RPC Layer** (`src/notebooklm/rpc/`):
   - `types.py`: All RPC method IDs and enums (source of truth)
   - `encoder.py`: Request encoding
   - `decoder.py`: Response parsing

2. **Client Runtime Layer** (`src/notebooklm/client.py` + runtime collaborators):
   - `client.py`: `NotebookLMClient` composition root plus public surface
   - `_client_composed.py`, `_client_seams.py`, `_runtime_init.py`: composition holder, injectable seams, and collaborator construction
   - `_env.py`, `_runtime_config.py`, `_logging.py`, `_callbacks.py`: environment/config defaults, compatibility logger name, redaction/correlation logging, and callback invocation helpers
   - `_request_types.py`, `_transport_errors.py`, `_streaming_post.py`, `_runtime_transport.py`, `_rpc_executor.py`: request construction, transport errors, streaming HTTP, authed-POST transport wrapper, and RPC dispatch
   - `_runtime_auth.py`, `_cookie_persistence.py`: Auth refresh + cookie storage
   - `_client_metrics.py`, `_transport_drain.py`, `_deadline.py`, `_backoff.py`, `_reqid_counter.py`: Telemetry, drain coordination, aggregate deadlines, retry backoff, request-counter handling
   - `_conversation_cache.py`, `_polling_registry.py`: Conversation cache + artifact polling helpers
   - `_runtime_helpers.py`, `_error_injection.py`: Auth-error helpers and synthetic-error transport
   - `_runtime_lifecycle.py`: Open/close lifecycle (loop-affinity guard + keepalive task)
   - `_runtime_contracts.py`: Shared runtime Protocols consumed by feature APIs
   - `_middleware.py`, `_middleware_context.py`, `_middleware_chain.py`, `_middleware_chain_host.py`, `_middleware_*.py`: HTTP-shaped middleware envelope, context vocabulary, canonical chain builder/host, and chain links
   - `_idempotency.py`: Mutating-RPC retry taxonomy
   - `_atomic_io.py`: Crash-safe JSON writes and locked read-modify-write helpers shared by auth and CLI

3. **Client Layer** (`src/notebooklm/client.py`, `_*.py`):
   - `NotebookLMClient`: Main async client with namespaced APIs
   - `_notebooks.py`, `_sources.py`, `_artifacts.py`, etc.: Domain APIs
   - `_source_*.py`, `_artifact_*.py`: Feature-specific service logic
   - `_types/`, `_row_adapters*.py`: Dataclass implementations and typed views over positional RPC rows
   - `_research_task_parser.py`, `research.py`: Research task parsing plus public citation/report utilities

4. **CLI Layer** (`src/notebooklm/cli/`):
   - Modular Click commands
   - `cli/services/`: CLI-specific service layer

### Key Files

| File | Purpose |
|------|---------|
| `client.py` | Main `NotebookLMClient` class |
| `_client_composed.py` | Client-owned composition holder for transport, executor, chain host, middleware metadata, and runtime collaborator bundle. |
| `_client_seams.py` | Constructor-only injectable seams used by tests and collaborator construction. |
| `_runtime_init.py` | Constructor helpers that validate client runtime kwargs, build collaborators (returning a `RuntimeCollaborators` bundle), wire middleware, and bind `ClientComposed`. |
| `_kernel.py` | Concrete `Kernel` transport core (owns `httpx.AsyncClient` + cookie jar) |
| `_runtime_config.py` | `DEFAULT_*` knobs and module-level constants. `CORE_LOGGER_NAME = "notebooklm._core"` is intentionally preserved as a compatibility logging contract even though the `_core` module was deleted; renaming it silently breaks downstream `caplog`/logger filters. |
| `_env.py`, `config.py` | Runtime environment defaults and the public config re-export surface |
| `_logging.py`, `log.py` | Redaction/correlation logging internals and the public logging helper surface |
| `_callbacks.py` | Sync-or-async callback invocation helper used by telemetry/retry hooks |
| `_runtime_helpers.py` | `is_auth_error`, `AUTH_ERROR_PATTERNS`, `_resolve_keepalive_interval` |
| `_error_injection.py` | Synthetic-error env-var resolver + startup guard |
| `_client_metrics.py` | `ClientMetrics` — `ClientMetricsSnapshot` counters + `on_rpc_event` callback |
| `_transport_drain.py` | `TransportDrainTracker` — in-flight transport counters + `_TransportOperationToken` |
| `_deadline.py` | `RuntimeDeadline` helper shared by retry and polling loops so aggregate timeouts clamp sleep consistently |
| `_backoff.py` | Shared capped exponential-backoff calculation with deterministic test injection |
| `_reqid_counter.py` | `ReqidCounter` — monotonic `_reqid` for the chat backend |
| `_runtime_auth.py` | `AuthRefreshCoordinator` — refresh task + auth-snapshot lock |
| `_auth_refresh_retry.py` | Shared auth refresh-and-retry core for the two retry layers (HTTP-status `AuthRefreshMiddleware` + decoded-RPC `RpcExecutor`): the once-per-logical-call `RefreshBudget` token and the common `refresh_and_count` body (log/refresh/sleep/`rpc_auth_retries` metric). Unifies the previously-divergent copies per issue #1205; the two layers keep their distinct triggers and refresh-failure exception shapes. |
| `_runtime_lifecycle.py` | `ClientLifecycle` — loop-affinity guard + keepalive task |
| `_runtime_transport.py` | `RuntimeTransport` — authed-POST transport wrapper that drives the middleware chain and typed transport response handling |
| `_rpc_executor.py` | RPC dispatch executor. Takes its `Kernel`, `RuntimeTransport`, `AuthRefreshCoordinator`, and `ClientMetrics` collaborators directly via keyword-only constructor parameters (ADR-014 Rule 5). The `RpcOwner` Protocol that previously re-declared the former `Session` facade's private attribute surface was deleted in Wave 4 of session-decoupling (#1068); only the local `DecodeResponse` Protocol remains. |
| `_request_types.py` | Shared authed POST request construction types: `AuthSnapshot`, `BuildRequest`, `PostBody`, and materialization helpers. |
| `_transport_errors.py` | Transport exceptions, `Retry-After` parsing, and terminal `Kernel.post` error mapping for retry/auth middleware. |
| `_streaming_post.py` | Size-capped streaming POST helper used by `Kernel.post`. |
| `_middleware.py` | HTTP-shaped middleware request/response envelope, chain composition, and middleware Protocol |
| `_middleware_context.py` | Canonical per-request context-key vocabulary for middleware |
| `_middleware_chain_host.py` | Mutable owner for the live middleware chain slots and retry-budget tunables |
| `_conversation_cache.py` | Per-instance true-LRU conversation cache for `ChatAPI` (caps conversation count via `MAX_CONVERSATION_CACHE_SIZE` and per-conversation turns via `MAX_TURNS_PER_CONVERSATION`) |
| `_polling_registry.py` | Pending-poll registry for long-running artifact generations |
| `_cookie_persistence.py` | Cookie-jar persistence + `__Secure-1PSIDTS` rotation |
| `_runtime_contracts.py` | Shared runtime Protocols consumed by sub-clients |
| `_idempotency.py` | Mutating-RPC idempotency policy registry and probe-then-retry wrapper; ADR-005 is the taxonomy source |
| `_atomic_io.py`, `io.py` | Atomic JSON write/update internals and public I/O re-export surface for CLI boundary compliance |
| `exceptions.py` | Public exception hierarchy plus safe diagnostic preview/redaction helpers |
| `paths.py`, `migration.py` | Profile-aware path resolution and locked migration from the legacy flat layout |
| `_types/`, `types.py` | Dataclass implementation package and public type/re-export facade |
| `_row_adapters_artifacts.py` | `ArtifactRow` typed view over raw positional artifact RPC rows |
| `_row_adapters_notes.py` | `NoteRow` typed view over raw positional note and mind-map RPC rows |
| `_row_adapters_sources.py` | `SourceRow` / `SourceRowShape` typed views over raw positional source RPC rows |
| `artifacts.py`, `research.py`, `utils.py` | Public helper modules for artifact retry, research citation/report utilities, and common async helpers |
| `_research_task_parser.py` | Internal parser for research task result-type selection |
| `_notebooks.py` | `client.notebooks` API + source-id resolver |
| `_sources.py` | `client.sources` API |
| `_artifacts.py` | `client.artifacts` API — owns artifact generation orchestration directly (the former `_artifact_generation.py` service was folded into this facade in #1205, ADR-012 sibling fold) |
| `_chat.py` | `client.chat` API |
| `_research.py` | `client.research` API |
| `_notes.py` | `client.notes` API |
| `_sharing.py` | `client.sharing` API |
| `_settings.py` | `client.settings` API |
| `_note_service.py` | Service layer managing note CRUD, note-backed content generation, and sync |
| `_mind_map.py` | Specific adapter service representing mind-maps, backed by standard notes |
| `_artifact_downloads.py` | Asynchronous download coordinator for finished artifacts |
| `_artifact_formatters.py` | Markdown, HTML, and plain text formatters for artifacts |
| `_artifact_payloads.py` | Stable CREATE_ARTIFACT / GENERATE_MIND_MAP request payload builders |
| `_artifact_listing.py` | Listing and filtering operations for notebook artifacts |
| `_artifact_polling.py` | Poll coordination service for artifact generation tasks |
| `_source_add.py` | Core service layer for adding text, URL, or Google Drive sources |
| `_source_content.py` | Core service layer for fetching source HTML/markdown content |
| `_source_listing.py` | Core service layer for listing notebook sources |
| `_source_polling.py` | Poll coordination service for active source conversions |
| `_source_upload.py` | Concurrency-gated upload pipeline for source files |
| `_source_upload_payloads.py` | Stable source upload registration, rename, and resumable-upload request builders |
| `_notebook_metadata.py` | Metadata protocol schemas for sub-clients |
| `_url_utils.py`, `urls.py` | URL parsing/validation internals and the public URL helper facade |
| `_sharing_manager.py` | Direct sharing management logic |
| `_version_check.py` | Dynamic client-side version deprecation guard |
| `_chat_notes.py` | Chat-adjacent note saving workflow adapter |
| `_chat_wire.py` | Streamed-chat wire request construction + response parsing for the chat client |
| `_chat_transport.py` | Chat-specific error mapping over the shared transport pipeline |
| `_middleware_chain.py` | Constructs the middleware chain in the canonical ADR-009 order |
| `_middleware*.py` | Modular middleware implementations (drain, metrics, semaphore, retry, auth, error injection, tracing) |
| `rpc/types.py` | RPC method IDs (source of truth) |
| `auth.py` | Authentication facade — **almost pure re-exports** (the only remaining function body is `async def enumerate_accounts`, which binds `_poke_session` as a default dependency; ADR-003 records the optional-`async` audit command). Every other top-level name forwards from the relevant `_auth/*` module. The previous write-through (`_validate_required_cookies` copy-forwarding `MINIMUM_REQUIRED_COOKIES` / `_EXTRACTION_HINT` / `_has_valid_secondary_binding` into `_cookie_policy` and mirroring `_SECONDARY_BINDING_WARNED` back) was inverted in Wave 4 T2.2 (#1070); `auth._validate_required_cookies` is now identity-equal to `_auth.cookie_policy._validate_required_cookies`. `load_auth_from_storage` body was moved to `_auth/tokens.py` in Wave 3a (#1066). `AuthTokens` was moved to `_auth/tokens.py` in #1055. **ADR-003 flat-re-export goal closed by ADR-014** (session-decoupling Waves 3a + 4 T2.2 + 5). Tests that need to rebind policy names patch `_auth.cookie_policy.X` directly. |
| `_auth/paths.py` | Storage paths and filesystem helpers |
| `_auth/extraction.py` | Cookie/token extraction from browser sessions |
| `_auth/headers.py` | HTTP header construction |
| `_auth/cookies.py` | Cookie map manipulation + `_update_cookie_input` |
| `_auth/cookie_policy.py` | Cookie-domain allowlist and policy decisions |

### Repository Structure

```text
src/notebooklm/
├── __init__.py                  # Public exports
├── __main__.py                  # `python -m notebooklm` entry point
├── client.py                    # NotebookLMClient
├── auth.py                      # Authentication facade — almost pure re-exports (`enumerate_accounts` exception; ADR-003 flat-re-export goal closed by ADR-014; see file table above)
├── types.py                     # Dataclasses
├── artifacts.py                 # Public artifact-generation retry helpers
├── config.py                    # Public config facade over _env
├── exceptions.py                # Public exception hierarchy
├── io.py                        # Public atomic-I/O facade for CLI boundary compliance
├── log.py                       # Public logging helper facade
├── migration.py                 # Legacy flat-layout to profile migration
├── paths.py                     # Profile-aware path resolution
├── research.py                  # Public research citation/report helpers
├── urls.py                      # Public URL helper facade
├── utils.py                     # Public async utility helpers
├── _atomic_io.py                # Atomic JSON write/update helpers
├── _auth_refresh_retry.py       # Shared auth refresh-and-retry core (RefreshBudget + refresh_and_count) for both retry layers
├── _backoff.py                  # Shared retry backoff calculation
├── _callbacks.py                # Sync/async callback invocation helper
├── _client_composed.py          # Client-owned composition holder
├── _client_seams.py             # Constructor-only injectable seams
├── _deadline.py                 # RuntimeDeadline helper for aggregate timeouts
├── _env.py                      # Runtime environment/default endpoint helpers
├── _idempotency.py              # Mutating-RPC idempotency registry + wrappers
├── _kernel.py                   # Concrete Kernel transport core
├── _logging.py                  # Redaction + correlation logging internals
├── _loop_affinity.py            # Event-loop affinity guard helper
├── _row_adapters_artifacts.py   # Artifact row adapter
├── _row_adapters_notes.py       # Note and mind-map row adapter
├── _row_adapters_sources.py     # Source row adapter
├── _runtime_config.py           # DEFAULT_* knobs + module-level constants
├── _runtime_helpers.py          # is_auth_error / AUTH_ERROR_PATTERNS / keepalive helpers
├── _runtime_init.py             # Runtime collaborator construction + validation
├── _runtime_transport.py        # Middleware-chain transport wrapper
├── _error_injection.py          # Synthetic-error env-var resolver + startup guard
├── _request_types.py            # AuthSnapshot, BuildRequest, PostBody, request materialization helpers
├── _transport_errors.py         # Transport exceptions, Retry-After parsing, Kernel.post error mapping
├── _streaming_post.py           # Size-capped streaming POST helper
├── _rpc_executor.py             # RPC dispatch executor
├── _runtime_auth.py             # AuthRefreshCoordinator (refresh task + auth-snapshot lock)
├── _client_metrics.py           # Telemetry / metrics seam
├── _transport_drain.py          # In-flight transport drain coordinator
├── _reqid_counter.py            # Request-counter / request-id helpers
├── _conversation_cache.py       # Per-instance true-LRU conversation cache (bounded conversation count + per-conversation turns)
├── _polling_registry.py         # Artifact polling helpers
├── _cookie_persistence.py       # Cookie-jar persistence + __Secure-1PSIDTS rotation
├── _runtime_lifecycle.py        # Open/close lifecycle seam (loop affinity + keepalive task)
├── _runtime_contracts.py        # Shared runtime Protocols consumed by feature APIs
├── _note_service.py             # NoteService
├── _mind_map.py                 # NoteBackedMindMapService
├── _artifact_downloads.py       # Artifact download coordinator
├── _artifact_formatters.py      # Artifact formatting helpers
├── _artifact_payloads.py        # Stable artifact request payload builders
├── _artifact_listing.py         # Artifact listing helper
├── _artifact_polling.py         # Artifact polling coordinator
├── _source_add.py               # Source addition coordinator
├── _source_content.py           # Source content fetcher
├── _source_listing.py           # Source listing helper
├── _source_polling.py           # Source polling coordinator
├── _source_upload.py            # Gated source upload service
├── _source_upload_payloads.py   # Source upload request payload builders
├── _notebook_metadata.py        # Metadata protocols
├── _url_utils.py                # URL validation helpers
├── _sharing_manager.py          # Sharing management logic
├── _version_check.py            # Deprecation version guard
├── _chat_notes.py               # Note saving workflow adapter
├── _chat_wire.py                # Streamed-chat wire request/response parser
├── _chat_transport.py           # Chat error mapping
├── _research_task_parser.py     # Research task result-type parser
├── _middleware.py               # Middleware envelope + Protocol + chain composition primitive
├── _middleware_context.py       # Middleware context-key vocabulary
├── _middleware_chain.py         # Middleware chain builder
├── _middleware_chain_host.py    # Live middleware chain slots and retry tunables
├── _middleware_tracing.py       # Tracing middleware
├── _middleware_metrics.py       # Metrics middleware
├── _middleware_drain.py         # Drain middleware
├── _middleware_error_injection.py # Error injection middleware
├── _middleware_retry.py         # Retry middleware
├── _middleware_auth_refresh.py  # Auth refresh middleware
├── _middleware_semaphore.py     # Concurrency semaphore middleware
├── _auth/                       # Auth subpackage (forwarded through auth.py facade)
│   ├── __init__.py
│   ├── paths.py                 # Storage paths and filesystem helpers
│   ├── extraction.py            # Cookie/token extraction from browser sessions
│   ├── headers.py               # HTTP header construction
│   ├── cookies.py               # Cookie maps + _update_cookie_input
│   ├── cookie_policy.py         # Domain allowlist and cookie policy
│   ├── account.py               # Account profile + multi-account switching
│   ├── session.py               # Auth-session refresh implementation via `refresh_auth_session()` and explicit collaborators
│   ├── storage.py               # Profile/state persistence on disk
│   ├── keepalive.py             # Cookie keepalive + __Secure-1PSIDTS rotation
│   ├── psidts_recovery.py       # Inline PSIDTS recovery for cold-start (issue #865)
│   ├── refresh.py               # Token refresh driver (external login cmd, coalesced runs, redaction)
│   └── tokens.py                # AuthTokens container + load_auth_from_storage loader
├── _types/                      # Dataclass implementation package re-exported by types.py
│   ├── __init__.py
│   ├── artifacts.py
│   ├── chat.py
│   ├── common.py
│   ├── notebooks.py
│   ├── notes.py
│   ├── sharing.py
│   └── sources.py
├── _notebooks.py                # NotebooksAPI
├── _sources.py                  # SourcesAPI
├── _artifacts.py                # ArtifactsAPI
├── _chat.py                     # ChatAPI
├── _research.py                 # ResearchAPI
├── _notes.py                    # NotesAPI
├── _sharing.py                  # SharingAPI
├── _settings.py                 # SettingsAPI
├── notebooklm_cli.py            # Entry-point assembler — imports + registers cli/ groups
├── rpc/                         # RPC protocol layer
│   ├── types.py                 # Method IDs and enums
│   ├── encoder.py               # Request encoding
│   ├── decoder.py               # Response parsing
│   ├── _safe_index.py           # Strict bounds-checked positional access for decoded RPC payloads
│   └── overrides.py             # Runtime RPC ID override policy (env-driven)
└── cli/                         # CLI implementation
    ├── __init__.py              # Re-exports click groups under historical names from *_cmd modules
    ├── _chromium_profiles.py    # Multi-user-data-profile cookie extraction for Chromium browsers
    ├── _download_specs.py       # Registry data for `download <type>` leaf commands (P3.T2)
    ├── _encoding.py             # Encoding-safe CLI output helpers
    ├── _firefox_containers.py   # Container-aware Firefox cookie extraction
    ├── agent_cmd.py             # agent show commands (renamed in P3.T0)
    ├── agent_templates.py       # agent prompts and configurations
    ├── artifact_cmd.py          # artifact commands (renamed in P3.T0)
    ├── auth_runtime.py          # CLI authentication + command runtime helpers
    ├── chat_cmd.py              # ask, configure, history (renamed in P3.T0)
    ├── completion.py            # Best-effort shell-completion providers for live IDs
    ├── context.py              # CLI context persistence helpers
    ├── doctor_cmd.py            # diagnostic/repair tool (renamed in P3.T0)
    ├── download_cmd.py          # download commands (renamed in P3.T0)
    ├── download_helpers.py      # Helper functions for download commands
    ├── error_handler.py         # Centralized CLI error handling
    ├── generate_cmd.py          # generate audio, video, etc. (renamed in P3.T0)
    ├── grouped.py               # Custom Click group with sectioned help output
    ├── helpers.py               # Shared Click utilities
    ├── input.py                 # CLI prompt and stdin input helpers
    ├── language_cmd.py          # Language configuration CLI commands
    ├── notebook_cmd.py          # list, create, delete, rename (renamed in P3.T0)
    ├── note_cmd.py              # note commands (renamed in P3.T0)
    ├── options.py               # Shared CLI option decorators
    ├── polling_ui.py            # Command-layer UI helpers for long-running polling
    ├── profile_cmd.py           # Profile management CLI commands
    ├── rendering.py             # CLI rendering helpers
    ├── research_cmd.py          # Research management CLI commands
    ├── research_import.py       # Research import helpers shared by CLI commands
    ├── resolve.py               # CLI notebook/entity ID resolution helpers
    ├── runtime.py               # CLI runtime primitives
    ├── session_cmd.py           # login, use, status, clear (renamed in P3.T0)
    ├── share_cmd.py             # Sharing management CLI commands
    ├── skill_cmd.py             # Skill management commands
    ├── source_cmd.py            # source add, list, delete (renamed in P3.T0)
    └── services/                # CLI-specific service layer (ADR-008 Click-to-service extraction)
        ├── __init__.py
        ├── artifact_generation.py # `generate` artifact orchestration service
        ├── auth_diagnostics.py  # `auth check` diagnostic service
        ├── auth_source.py       # Single source of truth for the active CLI auth source
        ├── confirming_mutation.py # Shared confirmed-mutation pipeline for CLI resources
        ├── download.py          # Pure-logic download plan + executor
        ├── generate.py          # Service layer for `notebooklm generate` commands
        ├── listing.py           # Shared list-command pipeline for CLI resources
        ├── login/               # Browser-cookie login helper package (split in P3.T4)
        │   ├── __init__.py      # re-export-only patch surface
        │   ├── browser_accounts.py
        │   ├── chromium_accounts.py
        │   ├── cookie_domains.py
        │   ├── cookie_jar.py
        │   ├── cookie_writes.py
        │   ├── exceptions.py
        │   ├── firefox_accounts.py
        │   ├── outcomes.py
        │   ├── profile_targets.py
        │   ├── refresh.py
        │   └── rookiepy_errors.py
        ├── playwright_login.py  # Playwright-driven Google login service
        ├── polling.py           # Shared polling helpers for CLI wait commands
        ├── research.py          # Service layer for `research wait`
        ├── session_context.py   # Notebook-context services for `use`/`status`/`auth logout`
        ├── skill_install.py     # Service helpers for skill install result handling
        ├── source_add.py        # `source add` text/url/drive service
        ├── source_clean.py      # Source-content cleaning service
        ├── source_content.py    # Read-only source-content commands service
        ├── source_listing.py    # `source list` fetch + prepare service
        ├── source_mutations.py  # Source-mutation commands service
        ├── source_research.py   # `source add-research` start + wait + import service
        ├── source_serializers.py # Shared JSON serializers for source CLI output
        └── source_wait.py       # `source wait` source-readiness poll service
```

## API Patterns

### Client Usage

```python
# Correct pattern - uses namespaced APIs
async with await NotebookLMClient.from_storage() as client:
    notebooks = await client.notebooks.list()
    await client.sources.add_url(nb_id, url)
    result = await client.chat.ask(nb_id, question)
    status = await client.artifacts.generate_audio(nb_id)
```

### CLI Structure

Commands are organized as:
- **Top-level**: `login`, `use`, `status`, `clear`, `list`, `create`, `ask`
- **Grouped**: `source add`, `artifact list`, `generate audio`, `download video`, `note create`

## Testing Strategy

- **Unit tests** (`tests/unit/`): Test encoding/decoding, no network
- **Integration tests** (`tests/integration/`): Mock HTTP responses
- **E2E tests** (`tests/e2e/`): Real API, require auth, marked `@pytest.mark.e2e`

### E2E Test Status

- ✅ Notebook operations (list, create, rename, delete)
- ✅ Source operations (add URL/text/YouTube, rename)
- ✅ Download operations (audio, video, infographic, slides)
- ⚠️ Artifact generation may fail due to rate limiting

## Common Pitfalls

1. **RPC method IDs change**: Check network traffic and update `rpc/types.py`
2. **Nested list structures**: Params are position-sensitive. Check existing implementations.
3. **Source ID nesting**: Different methods need `[id]`, `[[id]]`, `[[[id]]]`, or `[[[[id]]]]`
4. **CSRF tokens expire**: Use `client.refresh_auth()` or re-run `notebooklm login`
5. **Rate limiting**: Add delays between bulk operations
6. **Concurrency**: One `NotebookLMClient` instance is bound to its open()-time event loop. See [Concurrency contract](docs/python-api.md#concurrency-contract). Common bugs:
   - Re-using a client across threads → not supported; create one per thread.
   - Re-using a client across event loops → raises `RuntimeError` on first authed POST.
   - Sharing across `AuthTokens` tenants → never (`ChatAPI._cache` is per-instance).

## Documentation

All docs use lowercase-kebab naming in `docs/`:
- `docs/installation.md` - Installation, extras matrix, platform notes (canonical install guide)
- `docs/cli-reference.md` - CLI commands
- `docs/python-api.md` - Python API reference
- `docs/configuration.md` - Storage and settings
- `docs/troubleshooting.md` - Known issues
- `docs/development.md` - Architecture, testing, releasing
- `docs/rpc-development.md` - RPC capture and debugging
- `docs/rpc-reference.md` - RPC payload structures

## When to Suggest CLI vs API

- **CLI**: Quick tasks, shell scripts, LLM agent automation
- **Python API**: Application integration, complex workflows, async operations

## Pull Request Workflow (REQUIRED)

After creating a PR, you MUST monitor and address feedback:

### 1. Monitor CI Status
```bash
# Check CI status (repeat until all pass)
gh pr checks <PR_NUMBER>
```

Wait for all checks to pass. If any fail, investigate and fix.

### 2. Check for Review Comments
```bash
# Get review comments
gh api repos/teng-lin/notebooklm-py/pulls/<PR_NUMBER>/comments \
  --jq '.[] | "File: \(.path):\(.line)\nComment: \(.body)\n---"'
```

### 3. Address Feedback
For each review comment (especially from `gemini-code-assist`):
1. Read and understand the feedback
2. Make the suggested fix if it improves the code
3. Commit with a descriptive message referencing the feedback
4. Push and re-check CI
5. **Reply to the review thread** confirming the fix:
   ```bash
   gh api repos/teng-lin/notebooklm-py/pulls/<PR>/comments/<COMMENT_ID>/replies \
     -f body="Addressed in commit <SHA>: <brief description>"
   ```

### 4. Verify Final State
```bash
# Ensure PR is ready to merge
gh pr view <PR_NUMBER> --json state,mergeStateStatus,mergeable
```

**Important**: Do NOT consider a PR complete until:
- All CI checks pass
- All review comments are addressed
- `mergeStateStatus` is `CLEAN`

### Requesting a Claude review on a PR

Automatic Claude review on every PR is disabled. To request a review, comment `@claude review` on the PR — the `.github/workflows/claude.yml` workflow will pick it up.
