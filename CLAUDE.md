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
Core Layer (_core.py + _core_*.py seam modules)
    ↓
RPC Layer (rpc/)
```

1. **RPC Layer** (`src/notebooklm/rpc/`):
   - `types.py`: All RPC method IDs and enums (source of truth)
   - `encoder.py`: Request encoding
   - `decoder.py`: Response parsing

2. **Core Layer** (`src/notebooklm/_core.py` + `_core_*.py` seam modules):
   - `_core.py`: `NotebookLMClient` orchestration
   - `_core_transport.py`, `_core_rpc.py`: HTTP client + RPC call abstraction
   - `_core_auth.py`, `_core_cookie_persistence.py`: Auth refresh + cookie storage
   - `_core_metrics.py`, `_core_drain.py`, `_core_reqid.py`: Telemetry, drain coordination, request-counter handling
   - `_core_cache.py`, `_core_polling.py`: Conversation cache + artifact polling helpers
   - `_core_constants.py`, `_core_helpers.py`, `_core_error_injection.py`: Module-level constants, helper utilities, synthetic-error transport
   - `_core_lifecycle.py`: Open/close lifecycle (loop-affinity guard + keepalive task)
   - `_capabilities.py`: Capability adapters for feature APIs

3. **Client Layer** (`src/notebooklm/client.py`, `_*.py`):
   - `NotebookLMClient`: Main async client with namespaced APIs
   - `_notebooks.py`, `_sources.py`, `_artifacts.py`, etc.: Domain APIs

4. **CLI Layer** (`src/notebooklm/cli/`):
   - Modular Click commands
   - `session.py`, `notebook.py`, `source.py`, `generate.py`, etc.

### Key Files

| File | Purpose |
|------|---------|
| `client.py` | Main `NotebookLMClient` class |
| `_core.py` | `ClientCore` orchestrator; HTTP client lifecycle; late-binding wrappers |
| `_core_constants.py` | `DEFAULT_*` knobs and module-level constants |
| `_core_helpers.py` | `is_auth_error`, `AUTH_ERROR_PATTERNS`, `_resolve_keepalive_interval` |
| `_core_error_injection.py` | `_SyntheticErrorTransport` + env-var guard for fault injection |
| `_core_metrics.py` | `ClientMetrics` — `ClientMetricsSnapshot` counters + `on_rpc_event` callback |
| `_core_drain.py` | `TransportDrainTracker` — in-flight transport counters + `_TransportOperationToken` |
| `_core_reqid.py` | `ReqidCounter` — monotonic `_reqid` for the chat backend |
| `_core_auth.py` | `AuthRefreshCoordinator` — refresh task + auth-snapshot lock |
| `_core_lifecycle.py` | `ClientLifecycle` — loop-affinity guard + keepalive task |
| `_core_rpc.py` | RPC dispatch executor with `DecodeResponse` + `RpcOwner` Protocols |
| `_core_transport.py` | Authed POST path, retry loops, `_AuthedTransportHost` Protocol |
| `_core_cache.py` | Per-instance LRU conversation cache for `ChatAPI` |
| `_core_polling.py` | Pending-poll registry for long-running artifact generations |
| `_core_cookie_persistence.py` | Cookie-jar persistence + `__Secure-1PSIDTS` rotation |
| `_capabilities.py` | Narrow capability Protocols + `ClientCoreCapabilities` adapter for sub-clients |
| `_notebooks.py` | `client.notebooks` API |
| `_sources.py` | `client.sources` API + `fetch_source_ids` module helper |
| `_artifacts.py` | `client.artifacts` API |
| `_chat.py` | `client.chat` API |
| `rpc/types.py` | RPC method IDs (source of truth) |
| `auth.py` | Authentication facade — re-exports + `_AuthFacadeModule` shim that forwards monkeypatches to `_auth/*` seams |
| `_auth/paths.py` | Storage paths and filesystem helpers |
| `_auth/extraction.py` | Cookie/token extraction from browser sessions |
| `_auth/headers.py` | HTTP header construction |
| `_auth/cookies.py` | Cookie map manipulation + `_update_cookie_input` |
| `_auth/cookie_policy.py` | Cookie-domain allowlist and policy decisions |
| `_auth/account.py` | Account profile + multi-account switching |
| `_auth/session.py` | Session-level dataclasses |
| `_auth/storage.py` | Profile/state persistence on disk |
| `_auth/keepalive.py` | Cookie keepalive + `__Secure-1PSIDTS` rotation loop |
| `_auth/refresh.py` | Token refresh: external `notebooklm login` driver + coalesced runs + redaction |
| `cli/` | CLI command modules |

### Repository Structure

```
src/notebooklm/
├── __init__.py                  # Public exports
├── client.py                    # NotebookLMClient
├── auth.py                      # Authentication facade — re-exports + _AuthFacadeModule shim
├── types.py                     # Dataclasses
├── _core.py                     # Core orchestration (NotebookLMClient internals)
├── _core_constants.py           # DEFAULT_* knobs + module-level constants
├── _core_helpers.py             # is_auth_error / AUTH_ERROR_PATTERNS / keepalive helpers
├── _core_error_injection.py     # _SyntheticErrorTransport + fault-injection env-var guard
├── _core_transport.py           # HTTP client + transport-layer concerns
├── _core_rpc.py                 # RPC call abstraction
├── _core_auth.py                # Auth refresh seam
├── _core_cookie_persistence.py  # Cookie storage seam
├── _core_metrics.py             # Telemetry / metrics seam
├── _core_drain.py               # In-flight drain coordinator
├── _core_reqid.py               # Request-counter / request-id helpers
├── _core_cache.py               # Conversation cache seam
├── _core_polling.py             # Artifact polling helpers
├── _core_lifecycle.py           # Open/close lifecycle seam (loop affinity + keepalive task)
├── _capabilities.py             # Capability adapters for feature APIs
├── _auth/                       # Auth subpackage (forwarded through auth.py facade)
│   ├── __init__.py
│   ├── paths.py                 # Storage paths and filesystem helpers
│   ├── extraction.py            # Cookie/token extraction from browser sessions
│   ├── headers.py               # HTTP header construction
│   ├── cookies.py               # Cookie maps + _update_cookie_input
│   ├── cookie_policy.py         # Domain allowlist and cookie policy
│   ├── account.py               # Account profile + multi-account switching
│   ├── session.py               # Session-level dataclasses
│   ├── storage.py               # Profile/state persistence on disk
│   ├── keepalive.py             # Cookie keepalive + __Secure-1PSIDTS rotation
│   └── refresh.py               # Token refresh driver (external login cmd, coalesced runs, redaction)
├── _notebooks.py                # NotebooksAPI
├── _sources.py                  # SourcesAPI
├── _artifacts.py                # ArtifactsAPI
├── _chat.py                     # ChatAPI
├── _research.py                 # ResearchAPI
├── _notes.py                    # NotesAPI
├── notebooklm_cli.py            # Entry-point assembler — imports + registers cli/ groups
├── rpc/                         # RPC protocol layer
│   ├── types.py                 # Method IDs and enums
│   ├── encoder.py               # Request encoding
│   └── decoder.py               # Response parsing
└── cli/                         # CLI implementation
    ├── __init__.py
    ├── helpers.py               # Shared utilities
    ├── session.py               # login, use, status, clear
    ├── notebook.py              # list, create, delete, rename
    ├── source.py                # source add, list, delete
    ├── artifact.py              # artifact commands
    ├── generate.py              # generate audio, video, etc.
    ├── download.py              # download commands
    ├── chat.py                  # ask, configure, history
    └── note.py                  # note commands
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
