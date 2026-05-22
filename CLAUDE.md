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
Client Layer (client.py, _*.py APIs)
    ↓
Session Layer (_session.py + session/kernel collaborator modules)
    ↓
RPC Layer (rpc/)
```

1. **RPC Layer** (`src/notebooklm/rpc/`):
   - `types.py`: All RPC method IDs and enums (source of truth)
   - `encoder.py`: Request encoding
   - `decoder.py`: Response parsing

2. **Session Layer** (`src/notebooklm/_session.py` + session/kernel collaborators):
   - `_session.py`: concrete `Session` orchestration
   - `_authed_transport.py`, `_rpc_executor.py`: HTTP client + RPC call abstraction
   - `_session_auth.py`, `_cookie_persistence.py`: Auth refresh + cookie storage
   - `_client_metrics.py`, `_transport_drain.py`, `_reqid_counter.py`: Telemetry, drain coordination, request-counter handling
   - `_conversation_cache.py`, `_polling_registry.py`: Conversation cache + artifact polling helpers
   - `_session_config.py`, `_session_helpers.py`, `_error_injection.py`: Module-level constants, helper utilities, synthetic-error transport
   - `_session_lifecycle.py`: Open/close lifecycle (loop-affinity guard + keepalive task)
   - `_session_contracts.py`: Shared session Protocols consumed by feature APIs

3. **Client Layer** (`src/notebooklm/client.py`, `_*.py`):
   - `NotebookLMClient`: Main async client with namespaced APIs
   - `_notebooks.py`, `_sources.py`, `_artifacts.py`, etc.: Domain APIs
   - `_source_*.py`, `_artifact_*.py`: Feature-specific service logic

4. **CLI Layer** (`src/notebooklm/cli/`):
   - Modular Click commands
   - `cli/services/`: CLI-specific service layer

### Key Files

| File | Purpose |
|------|---------|
| `client.py` | Main `NotebookLMClient` class |
| `_session.py` | Concrete `Session` orchestrator; HTTP client lifecycle; late-binding wrappers |
| `_kernel.py` | Concrete `Kernel` transport core (owns `httpx.AsyncClient` + cookie jar) |
| `_session_config.py` | `DEFAULT_*` knobs and module-level constants |
| `_session_helpers.py` | `is_auth_error`, `AUTH_ERROR_PATTERNS`, `_resolve_keepalive_interval` |
| `_error_injection.py` | Synthetic-error env-var resolver + startup guard |
| `_client_metrics.py` | `ClientMetrics` — `ClientMetricsSnapshot` counters + `on_rpc_event` callback |
| `_transport_drain.py` | `TransportDrainTracker` — in-flight transport counters + `_TransportOperationToken` |
| `_reqid_counter.py` | `ReqidCounter` — monotonic `_reqid` for the chat backend |
| `_session_auth.py` | `AuthRefreshCoordinator` — refresh task + auth-snapshot lock |
| `_session_lifecycle.py` | `ClientLifecycle` — loop-affinity guard + keepalive task |
| `_rpc_executor.py` | RPC dispatch executor with `DecodeResponse` + `RpcOwner` Protocols |
| `_authed_transport.py` | Authed POST leaf raising `TransportRateLimited` / `TransportServerError` / `TransportAuthExpired` for `RetryMiddleware` (in `_middleware_retry.py`) to act on; hosts the `_AuthedTransportHost` Protocol. |
| `_conversation_cache.py` | Per-instance LRU conversation cache for `ChatAPI` |
| `_polling_registry.py` | Pending-poll registry for long-running artifact generations |
| `_cookie_persistence.py` | Cookie-jar persistence + `__Secure-1PSIDTS` rotation |
| `_session_contracts.py` | Shared session Protocols consumed by sub-clients |
| `_notebooks.py` | `client.notebooks` API + source-id resolver |
| `_sources.py` | `client.sources` API |
| `_artifacts.py` | `client.artifacts` API |
| `_chat.py` | `client.chat` API |
| `_research.py` | `client.research` API |
| `_notes.py` | `client.notes` API |
| `_sharing.py` | `client.sharing` API |
| `_settings.py` | `client.settings` API |
| `_note_service.py` | Service layer managing note CRUD, note-backed content generation, and sync |
| `_mind_map.py` | Specific adapter service representing mind-maps, backed by standard notes |
| `_artifact_downloads.py` | Asynchronous download coordinator for finished artifacts |
| `_artifact_formatters.py` | Markdown, HTML, and plain text formatters for artifacts |
| `_artifact_generation.py` | Extracted artifact generation payload-building service |
| `_artifact_listing.py` | Listing and filtering operations for notebook artifacts |
| `_artifact_polling.py` | Poll coordination service for artifact generation tasks |
| `_source_add.py` | Core service layer for adding text, URL, or Google Drive sources |
| `_source_content.py` | Core service layer for fetching source HTML/markdown content |
| `_source_listing.py` | Core service layer for listing notebook sources |
| `_source_polling.py` | Poll coordination service for active source conversions |
| `_source_upload.py` | Concurrency-gated upload pipeline for source files |
| `_notebook_metadata.py` | Metadata protocol schemas for sub-clients |
| `_url_utils.py` | URL parsing and validation helpers |
| `_sharing_manager.py` | Direct sharing management logic |
| `_version_check.py` | Dynamic client-side version deprecation guard |
| `_chat_notes.py` | Chat-adjacent note saving workflow adapter |
| `_chat_protocol.py` | Internal types and interfaces for the chat client |
| `_chat_transport.py` | Chat-specific error mapping over `AuthedTransport` |
| `_middleware_chain.py` | Constructs the middleware chain in the canonical ADR-009 order |
| `_middleware*.py` | Modular middleware implementations (drain, metrics, semaphore, retry, auth, error injection, tracing) |
| `rpc/types.py` | RPC method IDs (source of truth) |
| `auth.py` | Authentication facade and host for retained surface. Still owns `AuthTokens`, `load_auth_from_storage()`, and the `_validate_required_cookies()` write-through that propagates `auth.py`-level policy rebindings into `_auth.cookie_policy` (and mirrors `_SECONDARY_BINDING_WARNED` back). Tests still pin `notebooklm.auth.<name>` monkeypatch behavior (see `tests/unit/test_public_shims.py`); `_AuthFacadeModule` itself was retired in [arch-d1-auth-side](https://github.com/teng-lin/notebooklm-py/pull/834) (#834), but the flat re-export goal in ADR-003 is **deferred** — full retirement of `AuthTokens` / `load_auth_from_storage` to `_auth/` has not happened. |
| `_auth/paths.py` | Storage paths and filesystem helpers |
| `_auth/extraction.py` | Cookie/token extraction from browser sessions |
| `_auth/headers.py` | HTTP header construction |
| `_auth/cookies.py` | Cookie map manipulation + `_update_cookie_input` |
| `_auth/cookie_policy.py` | Cookie-domain allowlist and policy decisions |

### Repository Structure

```text
src/notebooklm/
├── __init__.py                  # Public exports
├── client.py                    # NotebookLMClient
├── auth.py                      # Authentication facade hosting `AuthTokens`, `load_auth_from_storage()`, and `_validate_required_cookies()` write-through into `_auth.cookie_policy` (ADR-003 flat re-export goal deferred; `_AuthFacadeModule` retired in #834)
├── types.py                     # Dataclasses
├── _session.py                  # Concrete Session orchestration (NotebookLMClient internals)
├── _kernel.py                   # Concrete Kernel transport core
├── _session_config.py           # DEFAULT_* knobs + module-level constants
├── _session_helpers.py          # is_auth_error / AUTH_ERROR_PATTERNS / keepalive helpers
├── _error_injection.py          # Synthetic-error env-var resolver + startup guard
├── _authed_transport.py         # Authed POST leaf — raises TransportRateLimited / TransportServerError / TransportAuthExpired for RetryMiddleware (_middleware_retry.py) to act on
├── _rpc_executor.py             # RPC dispatch executor
├── _session_auth.py             # AuthRefreshCoordinator (refresh task + auth-snapshot lock)
├── _client_metrics.py           # Telemetry / metrics seam
├── _transport_drain.py          # In-flight transport drain coordinator
├── _reqid_counter.py            # Request-counter / request-id helpers
├── _conversation_cache.py       # Per-instance LRU conversation cache
├── _polling_registry.py         # Artifact polling helpers
├── _cookie_persistence.py       # Cookie-jar persistence + __Secure-1PSIDTS rotation
├── _session_lifecycle.py        # Open/close lifecycle seam (loop affinity + keepalive task)
├── _session_contracts.py        # Shared session Protocols consumed by feature APIs
├── _note_service.py             # NoteService
├── _mind_map.py                 # NoteBackedMindMapService
├── _artifact_downloads.py       # Artifact download coordinator
├── _artifact_formatters.py      # Artifact formatting helpers
├── _artifact_generation.py      # Artifact generation payload builder
├── _artifact_listing.py         # Artifact listing helper
├── _artifact_polling.py         # Artifact polling coordinator
├── _source_add.py               # Source addition coordinator
├── _source_content.py           # Source content fetcher
├── _source_listing.py           # Source listing helper
├── _source_polling.py           # Source polling coordinator
├── _source_upload.py            # Gated source upload service
├── _notebook_metadata.py        # Metadata protocols
├── _url_utils.py                # URL validation helpers
├── _sharing_manager.py          # Sharing management logic
├── _version_check.py            # Deprecation version guard
├── _chat_notes.py               # Note saving workflow adapter
├── _chat_protocol.py            # Internal chat types
├── _chat_transport.py           # Chat error mapping
├── _middleware_chain.py         # Middleware chain builder
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
│   ├── session.py               # Auth-session refresh implementation (`RefreshAuthCore` Protocol + `refresh_auth_session()`)
│   ├── storage.py               # Profile/state persistence on disk
│   ├── keepalive.py             # Cookie keepalive + __Secure-1PSIDTS rotation
│   ├── psidts_recovery.py       # Inline PSIDTS recovery for cold-start (issue #865)
│   └── refresh.py               # Token refresh driver (external login cmd, coalesced runs, redaction)
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
│   └── decoder.py               # Response parsing
└── cli/                         # CLI implementation
    ├── __init__.py              # Re-exports click groups under historical names from *_cmd modules
    ├── helpers.py               # Shared Click utilities
    ├── session_cmd.py           # login, use, status, clear (renamed in P3.T0)
    ├── notebook_cmd.py          # list, create, delete, rename (renamed in P3.T0)
    ├── source_cmd.py            # source add, list, delete (renamed in P3.T0)
    ├── artifact_cmd.py          # artifact commands (renamed in P3.T0)
    ├── generate_cmd.py          # generate audio, video, etc. (renamed in P3.T0)
    ├── download_cmd.py          # download commands (renamed in P3.T0)
    ├── chat_cmd.py              # ask, configure, history (renamed in P3.T0)
    ├── note_cmd.py              # note commands (renamed in P3.T0)
    ├── agent_cmd.py             # agent show commands (renamed in P3.T0)
    ├── agent_templates.py       # agent prompts and configurations
    ├── doctor_cmd.py            # diagnostic/repair tool (renamed in P3.T0)
    └── services/                # CLI-specific service layer (ADR-008 Click-to-service extraction)
        ├── __init__.py
        ├── artifact_generation.py
        ├── login/                # split into a package in P3.T4 (leaf-ward DAG)
        │   ├── __init__.py       # re-export-only patch surface
        │   ├── browser_accounts.py
        │   ├── chromium_accounts.py
        │   ├── cookie_domains.py
        │   ├── cookie_jar.py
        │   ├── cookie_writes.py
        │   ├── firefox_accounts.py
        │   ├── profile_targets.py
        │   ├── refresh.py
        │   └── rookiepy_errors.py
        ├── source_add.py
        └── source_clean.py
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
