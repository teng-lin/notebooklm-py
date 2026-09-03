# API Stability and Versioning

**Status:** Active
**Last Updated:** 2026-09-03

This document describes the stability guarantees and versioning policy for `notebooklm-py`.

## Important Context

This library uses **undocumented Google APIs**. Unlike official Google APIs, there are:

- **No stability guarantees from Google**
- **No deprecation notices** before changes
- **No SLAs or support**

Google can change the underlying APIs at any time, which may break this library without warning.

## Versioning Policy

We follow [Semantic Versioning](https://semver.org/) with modifications for our unique situation:

### Version Format: `MAJOR.MINOR.PATCH`

| Change Type | Version Bump | Example |
|-------------|--------------|---------|
| RPC method ID fixes (Google changed something) | PATCH | 0.1.0 → 0.1.1 |
| Bug fixes | PATCH | 0.1.1 → 0.1.2 |
| New features (backward compatible) | MINOR | 0.1.2 → 0.2.0 |
| Public API breaking changes | MAJOR | 0.2.0 → 1.0.0 |

### Special Considerations

1. **RPC ID Changes = Patch Release**
   - When Google changes internal RPC method IDs, we release a patch
   - These are "bug fixes" from our perspective, not breaking changes
   - Users should always use the latest patch version

2. **Python API Stability**
   - Public API (items in `__all__`) is stable within a major version
   - Breaking changes require a major version bump
   - Deprecated APIs are marked with `DeprecationWarning` and documented

3. **0.x Pre-1.0 Semantics**
   - Per [SemVer §4](https://semver.org/#spec-item-4), the project is currently in 0.x and the public API is not yet considered stable.
   - **MINOR** releases (e.g. 0.4.0 → 0.5.0) **may remove** previously deprecated public APIs. Removal is preceded by at least one MINOR release of `DeprecationWarning` notice.
   - Once the project reaches 1.0.0, breaking changes will require a **MAJOR** bump as described above.

## Public API Surface

The following are considered **public API** and are subject to stability guarantees:

### Stable (Won't break without major version bump)

```python
# Version
__version__  # Package version string (read-only)

# Client
NotebookLMClient
NotebookLMClient.from_storage()
NotebookLMClient.backends
NotebookLMClient.notebooks
NotebookLMClient.sources
NotebookLMClient.artifacts
NotebookLMClient.chat
NotebookLMClient.research
NotebookLMClient.notes
NotebookLMClient.settings
NotebookLMClient.sharing
NotebookLMClient.labels
NotebookLMClient.mind_maps
NotebookLMClient.collections
NotebookLMClient.raw
WebRawAPI, AndroidRawAPI
GrpcUnaryMethod, GrpcUnaryStreamMethod, ReplayPolicy

# Types
Notebook, Source, Artifact, Note, Label, MindMap, Collection
GenerationState, GenerationStatus, AskResult  # incl. the .is_terminal predicate on both
NotebookDescription, ConversationTurn, ChatSession, PremiumFeatureInfo
ShareStatus, SharedUser, SourceFulltext, SourceGuide
NotebookMetadata, SourceSummary
AccountLimits, UserSettings
ChatReference, NextStepSuggestion, ReportSuggestion, PromptSuggestion, SuggestedTopic
MindMapKind, MindMapResult
ResearchStart, ResearchStatus, ResearchTask, ResearchSource, ResearchTerminationReason
ClientMetricsSnapshot, ConnectionLimits, RpcTelemetryEvent

# Exceptions (all inherit from NotebookLMError)
NotebookLMError  # Base exception
NotFoundError  # Cross-domain umbrella for *NotFoundError
WaitTimeoutError  # Cross-domain umbrella for wait/poll timeouts (also a built-in TimeoutError)
RPCError, AuthError, RateLimitError, RPCTimeoutError, RPCResponseTooLargeError, ServerError
NetworkError, DecodingError, UnknownRPCMethodError
ClientError, ConfigurationError, ValidationError, MissingDependencyError
NonIdempotentRetryError  # Raised by idempotent=True calls on a non-idempotent retry
# Domain-specific
# Note: *NotFoundError classes mix in RPCError (catchable as either RPCError
# or the domain base). v0.6.0 restored this symmetry across all three "not
# found" types — see docs/python-api.md#error-handling for migration prose.
# Note: *TimeoutError classes mix in WaitTimeoutError (and the built-in
# TimeoutError). v0.7.0 added the WaitTimeoutError umbrella so `except
# WaitTimeoutError` catches source/artifact/research wait timeouts uniformly,
# while `except TimeoutError` keeps working — see docs/python-api.md#waittimeouterror.
SourceError, SourceAddError, SourceProcessingError, SourceTimeoutError, SourceNotFoundError
# A post-registration add_file() failure keeps raising its own type (AuthError /
# RateLimitError / ServerError / NetworkError / ValidationError / bare
# SourceAddError), so existing `except` clauses around add_file() are unaffected.
# It additionally carries `source_id` and `stage` attributes identifying the
# source row the failure left behind; the library does not delete that row
# automatically. Read them with getattr(exc, "source_id", None) — they are absent
# on every other failure. See docs/python-api.md#partial-file-uploads.
NotebookError, NotebookNotFoundError
(
    ArtifactError,
    ArtifactDownloadError,
    ArtifactFeatureUnavailableError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
)
# Artifact download HTTP 401/403 responses raise AuthError directly so callers
# can trigger reauthentication. Other download transport, policy, content, and
# status failures continue to raise ArtifactDownloadError.
ArtifactTimeoutError, ArtifactPendingTimeoutError, ArtifactInProgressTimeoutError
(
    ResearchError,
    ResearchTimeoutError,
    ResearchTaskMismatchError,
    AmbiguousResearchTaskError,
    ResearchStartUnavailableError,
)
# Note: notes.get/update/delete and mind_maps.get/rename/delete now raise
# their domain *NotFoundError on a missing target; use get_or_none() for
# warning-free None-on-miss lookups.
NoteError, NoteNotFoundError
MindMapError, MindMapNotFoundError
LabelError, LabelNotFoundError
CollectionError, CollectionNotFoundError
ChatError, ChatResponseParseError

# Enums
AudioFormat, AudioLength
VideoFormat, VideoStyle
QuizQuantity, QuizDifficulty
InfographicOrientation, InfographicDetail, InfographicStyle
SlideDeckFormat, SlideDeckLength
ReportFormat
SourceType, ArtifactType, SourceStatus, DriveSourceStatus, DiscoveryMode
ShareAccess, SharePermission, ShareViewLevel
ChatGoal, ChatResponseLength, ChatMode, MagicArtifactType
DriveMimeType, ExportType
ArtifactStatus, artifact_status_to_str  # notebooklm.types.<X> only — NOT top-level (see below)

# Auth
AuthTokens  # also re-exported as notebooklm.auth.AuthTokens
notebooklm.paths.get_storage_path()

# Logging and Correlation
notebooklm.configure_logging
notebooklm.get_request_id
notebooklm.set_request_id
notebooklm.reset_request_id
notebooklm.correlation_id

# Citation and Research Helpers
notebooklm.utils.resolve_chat_reference_passage
notebooklm.research.select_cited_sources
notebooklm.research.normalize_url
notebooklm.research.extract_report_urls

# Helpers (cookies extra) - imported from notebooklm.auth
notebooklm.auth.convert_rookiepy_cookies_to_storage_state  # compatibility API name; requires the `rookie-cookies` package

# Cookie-domain tiers - imported from notebooklm.auth
notebooklm.auth.REQUIRED_COOKIE_DOMAINS
notebooklm.auth.OPTIONAL_COOKIE_DOMAINS
notebooklm.auth.OPTIONAL_COOKIE_DOMAINS_BY_LABEL

# Storage-writer failure - imported from notebooklm.auth
notebooklm.auth.LockUnavailableError  # canonical home: notebooklm.exceptions; also an OSError via TimeoutError (ADR-0029)
```

Backend selection is also public: `backend="web"` (the default) or
`backend="android"` on the client, and `--backend web|android` on the CLI.
Android is opt-in; see the installation and Android guides for its dependency,
credential, and undocumented-protocol requirements.

> **`ArtifactStatus` / `artifact_status_to_str` import path.** Unlike every other
> enum listed above, these two are **not** re-exported at top level — import them
> as `from notebooklm.types import ArtifactStatus`, never `from notebooklm import
> ArtifactStatus`. Their definitions live in the private transport-neutral
> `_types/enums.py` module; `notebooklm.rpc.types` remains a compatibility
> re-export. The `notebooklm.types` spelling is the blessed public one (see
> [deprecations.md](deprecations.md)). This implementation-home move preserves
> object identity at both existing import paths, but newly created pickle data
> records the private canonical module rather than the compatibility module.
>
> **Wire-value correction in the Unreleased line
> ([#2127](https://github.com/teng-lin/notebooklm-py/issues/2127)).** `ArtifactStatus`
> was added to this list *after* its member integers were corrected: codes 1 and
> 2 had been transposed relative to the backend, so the old values were simply
> wrong about the wire rather than a contract worth preserving. The stability
> promise applies from that correction forward. Note the general caveat that
> applies to every wire-derived value here — see
> [What Happens When Google Breaks Things](#what-happens-when-google-breaks-things).
>
> **What these two promise, precisely.** The guarantee is *"these codes keep
> these meanings"*, **not** *"this enum covers every code the backend emits"*.
> Three consequences worth writing down, because they are the ways a caller can
> be surprised without the promise being broken:
>
> 1. **The status-string set is open.** `artifact_status_to_str` went from five
>    strings to seven in #2127 and will widen again whenever the backend gains a
>    state. Treat an unrecognized return value as "unknown" — do **not** write an
>    exhaustive `if`/`elif` or `match` over it. The same applies to
>    `GenerationState`: it is stable in the sense that existing members keep
>    their values, not in the sense that the member list is frozen.
> 2. **The enum is fail-closed; the function is fail-open.** `ArtifactStatus(7)`
>    raises `ValueError`, while `artifact_status_to_str(7)` returns `"unknown"`.
>    That asymmetry is deliberate — the raising constructor is what surfaces
>    backend drift instead of silently swallowing it — but it means
>    `ArtifactStatus(...)` is the brittle way to parse a raw wire code. Prefer
>    `artifact_status_to_str`, or the `Artifact.status_str` / `.is_*` accessors,
>    for anything decoding live responses.
> 3. **`is_terminal` tracks the wire.** A state added later is non-terminal by
>    default, which is the safe direction. But if the backend ever ships a
>    genuinely terminal state, classifying it correctly will *flip* what
>    `is_terminal` returns for that state — the same kind of wire-tracking
>    correction as the #2127 value fix above, and not a break of this promise.

Every `notebooklm.auth.<name>` above is **exactly** the `__all__` of the
`notebooklm.auth` module: `test_auth_all_matches_documented_public_surface`
(`tests/_guardrails/test_public_surface.py`) parses this section and fails the
build if the module publishes a name this list does not, or vice versa. The rest
of `notebooklm.auth` — including the ~30 helpers `cli/` and `_app/` import across
the package boundary — is internal and may change without notice; those are
tracked as `AUTH_CROSS_BOUNDARY_NAMES` in the same test module, which grants
importability without any stability promise.

### Internal helpers exported for compatibility

The following symbols appear in `notebooklm/__all__` so that downstream code can
import them via `from notebooklm import ...`, but they are **not** covered by
the stability guarantee above. They support narrow integration use cases
(typed exception handling and warning filters) and may be
renamed, narrowed, or removed in a future minor release. Prefer the stable
surface when possible.

```python
CitedSourceSelection  # Chat citation payload — internal shape, exposed for typing
AuthExtractionError  # Specialized AuthError raised by browser-based login
NotebookLimitError  # Raised when account notebook quota is exhausted
UnknownTypeWarning  # Warning category emitted when .kind falls back to UNKNOWN
```

### Internal (May change without notice)

```python
# These are NOT part of the public API:
notebooklm.rpc.*          # RPC protocol internals, except documented power-user imports
notebooklm._*.py          # All underscore-prefixed modules
notebooklm.auth.*         # Auth internals (except the six documented names listed above: AuthTokens, cookie conversion, the cookie-domain constants, and LockUnavailableError)
```

For raw-RPC power-user calls, import the documented RPC helpers explicitly:
```python
from notebooklm.rpc import RPCMethod, resolve_rpc_id
```

### Adapter surfaces: MCP server and REST API (experimental)

The **MCP tool surface** (`mcp` extra, `notebooklm-mcp`) and the **single-tenant
REST API** (`server` extra, `notebooklm-server`) are transport adapters over the
same `_app/` business logic as the CLI. They are **experimental and not covered
by the semver guarantees above** — tool/route names, parameters, and response
shapes may change between releases without a major-version bump. The underlying
Python client API they call is still governed by the stability policy; only the
adapter surfaces are exempt. The remote-MCP connector (HTTP transport,
self-hosted OAuth, Docker/Cloudflare/Tailscale deployment) is likewise
experimental.

### Strict decoding (the only mode since v0.7.0)

Schema-drift helpers (notably the internal ``safe_index`` decode helper) **raise**
:class:`~notebooklm.exceptions.UnknownRPCMethodError` when Google's
batchexecute response shape does not match what the decoder expects. This is
now the only behavior: the legacy ``NOTEBOOKLM_STRICT_DECODE=0`` warn-and-
return-``None`` opt-out was retired in v0.7.0 (it had a one-release
``DeprecationWarning`` window through v0.5.0/v0.6.0). The env var is now
ignored.

Stability implications:

- **Exception type is stable.** ``UnknownRPCMethodError`` is a subclass of
  ``DecodingError`` and ``RPCError`` (both public-API exceptions). Code that
  already catches ``RPCError`` continues to handle drift correctly.
- **No silent shape changes.** Methods that previously returned ``None`` /
  empty values on drift now raise. Callers that treated ``None`` as a valid
  sentinel must add an ``except UnknownRPCMethodError`` branch.

See [`docs/configuration.md#decoder-strictness`](configuration.md#decoder-strictness)
for the env-var contract and
[`docs/adr/0011-schema-validation-policy.md`](adr/0011-schema-validation-policy.md)
for the design rationale behind the strict-decode policy.

## Deprecation Policy

1. **Deprecation Notice**: Deprecated features emit `DeprecationWarning`
2. **Documentation**: Deprecations are noted in docstrings and CHANGELOG
3. **Removal Timeline**: Deprecated features are removed in the next major version. While the project is in 0.x, removal may instead occur in the next MINOR release after at least one MINOR cycle of `DeprecationWarning` (see "0.x Pre-1.0 Semantics" above).
4. **Migration Guide**: Breaking changes include migration instructions

### Currently Deprecated

See [`docs/deprecations.md`](deprecations.md) for the canonical list of
currently-deprecated APIs and their scheduled removal versions, plus the
deprecations removed in v0.6.0, v0.7.0, and v0.8.0.

### Removed in v0.5.0

The following v0.3-era deprecations completed their removal cycle in v0.5.0:

| Removed | Replacement | Notes |
|---------|-------------|-------|
| `Source.source_type` | `Source.kind` | Returns `SourceType` str enum |
| `Artifact.artifact_type` | `Artifact.kind` | Returns `ArtifactType` str enum |
| `Artifact.variant` | `Artifact.kind` | Use `.is_quiz` / `.is_flashcards` |
| `SourceFulltext.source_type` | `SourceFulltext.kind` | Returns `SourceType` str enum |
| `notebooklm.StudioContentType` | `ArtifactType` | Str enum for user-facing code |
| `notebooklm.DEFAULT_STORAGE_PATH` | `notebooklm.paths.get_storage_path()` | Module-level constant replaced by helper |
| `notebooklm.rpc.types.StudioContentType` | `ArtifactType` | Internal raw code alias removed |
| `notebooklm.rpc.StudioContentType` | `ArtifactType` | Internal re-export removed |
| `notebooklm.rpc.RPCMethod.DISCOVER_SOURCES` | none | Unused raw RPC enum member, not exercised by client APIs |
| `notebooklm.rpc.RPCMethod.QUERY_ENDPOINT` | `notebooklm.rpc.get_query_url()` (internal) | Endpoint URL path moved out of the RPC method enum; `get_query_url()` is itself internal plumbing (`notebooklm.rpc.*` is internal — see above) with no blessed public replacement |
| `notebooklm.cli.language_cmd.save_config` | `_save_config` | Private low-level write primitive only |

### Deprecated for a future major release

| Deprecated | Replacement | Notes |
|------------|-------------|-------|
| `NotebookLMClient.rpc_call(...)` | Web: `client.raw.call(...)`; Android: `client.raw.unary(...)` / `unary_stream(...)`, or a separate Web-selected client's `raw.call(...)` | Deprecated in v0.9.0; warns once per client; scheduled for v1.0 removal. Android compatibility lazily creates a Web sidecar during v0.x. |
| `AuthTokens.from_storage(...)` | `async with NotebookLMClient.from_storage(...) as client:` and use `client.auth` | Deprecated in v0.8.1; emits `DeprecationWarning`; scheduled for v1.0 removal |
| `AuthTokens(..., storage_path=..., cookie_jar=None)` synchronous storage fallback | Managed `NotebookLMClient.from_storage(...)`, or an explicit `cookie_jar=` | Deprecated in v0.8.1; only the implicit synchronous-I/O branch warns; scheduled for v1.0 removal |
| Awaiting `NotebookLMClient.from_storage(...)` | `async with NotebookLMClient.from_storage(...) as client:` | Emits `DeprecationWarning`; scheduled for v1.0 removal |

### Permanent aliases

`RPCError.rpc_id` and `RPCError.code` are permanent backward-compatibility
aliases for `RPCError.method_id` and `RPCError.rpc_code`. Exception diagnostic
aliases are exempt from the standard deprecation cycle because removal can mask
the original exception inside `except` handlers. New code should prefer the
canonical attribute names, but existing exception handlers may keep using the
aliases.

## Migration Guides

### Migrating from v0.3.x to v0.4.0

Version 0.4.0 is backward compatible with v0.3.x. Notable additions:

- **Multi-account profiles** - Existing single-account setups continue to work as the implicit default profile. Your existing `~/.notebooklm/storage_state.json` is auto-detected — no manual migration is required. New accounts can be added via `notebooklm profile create <name>`.
- **`[cookies]` optional extra** - To reuse cookies from your existing browser, install with `pip install "notebooklm-py[cookies]"` (installs `rookie-cookies`; full extras matrix: [docs/installation.md#optional-extras-matrix](installation.md#optional-extras-matrix)).
- **Deprecation removal deferred** - The deprecated attributes originally scheduled for v0.4.0 (`Source.source_type`, `Artifact.artifact_type`, `Artifact.variant`, `SourceFulltext.source_type`, `StudioContentType`, `DEFAULT_STORAGE_PATH`) were deferred to v0.5.0. In v0.5.0 and later, use the replacements listed in [Removed in v0.5.0](#removed-in-v050).

### Migrating from v0.2.x to v0.3.0

Version 0.3.0 introduced attributes that were deprecated until their v0.5.0
removal. The historical migration examples below show the replacement surface.

#### 1. `Source.source_type` → `Source.kind`

**Before (removed in v0.5.0):**
```python
source = (await client.sources.list(notebook_id))[0]
if source.source_type == "pdf":
    print("This is a PDF")
```

**After (recommended):**
```python
from notebooklm import SourceType

source = (await client.sources.list(notebook_id))[0]

# Option 1: Use enum comparison (recommended)
if source.kind == SourceType.PDF:
    print("This is a PDF")

# Option 2: Use string comparison (str enum supports this)
if source.kind == "pdf":
    print("This is a PDF")
```

**Available `SourceType` values:**
`GOOGLE_DOCS`, `GOOGLE_SLIDES`, `GOOGLE_SPREADSHEET`, `PDF`, `PASTED_TEXT`, `WEB_PAGE`, `YOUTUBE`, `MARKDOWN`, `DOCX`, `CSV`, `IMAGE`, `MEDIA`, `UNKNOWN`

#### 2. `Artifact.artifact_type` → `Artifact.kind`

**Before (removed in v0.5.0):**
```python
artifact = (await client.artifacts.list(notebook_id))[0]
if artifact.artifact_type == 1:
    print("This is an audio artifact")
```

**After (recommended):**
```python
from notebooklm import ArtifactType

artifact = (await client.artifacts.list(notebook_id))[0]

# Option 1: Use enum comparison (recommended)
if artifact.kind == ArtifactType.AUDIO:
    print("This is an audio artifact")

# Option 2: Use string comparison (str enum supports this)
if artifact.kind == "audio":
    print("This is an audio artifact")
```

**Available `ArtifactType` values:**
`AUDIO`, `VIDEO`, `REPORT`, `QUIZ`, `FLASHCARDS`, `MIND_MAP`, `INFOGRAPHIC`,
`SLIDE_DECK`, `DATA_TABLE`, `FANTASY_MAP`, `FILE`, `UNKNOWN`

#### 3. `Artifact.variant` → `Artifact.kind` or helpers

**Before (removed in v0.5.0):**
```python
if artifact.artifact_type == 4 and artifact.variant == 2:
    print("This is a quiz")
```

**After (recommended):**
```python
# Option 1: Use .kind
if artifact.kind == ArtifactType.QUIZ:
    print("This is a quiz")

# Option 2: Use helper properties
if artifact.is_quiz:
    print("This is a quiz")
if artifact.is_flashcards:
    print("These are flashcards")
```

#### Why These Changes?

1. **Stability**: The `.kind` property abstracts internal integer codes that Google may change
2. **Usability**: String enums work in comparisons, logging, and serialization
3. **Future-proofing**: Unknown types return `UNKNOWN` with a warning instead of crashing

## What Happens When Google Breaks Things

When Google changes their internal APIs:

1. **Detection**: Automated RPC health check runs nightly for `main`; release
   candidates are checked by manually dispatching the workflow on protected
   `main` after the release PR is merged (see below)
2. **Investigation**: Identify changed method IDs using browser devtools
3. **Fix**: Update `rpc/types.py` with new method IDs
4. **Release**: Push patch release as soon as possible

### Automated RPC Health Check

A nightly GitHub Action (`rpc-health.yml`) monitors all 48 RPC methods for ID
changes on `main`. Release candidates use the same workflow through a manual
dispatch on protected `main` after their release PR is merged; non-`main` refs
are intentionally rejected.

**What it verifies:**
- The RPC method ID we send matches the ID returned in the response envelope
- Example: `LIST_NOTEBOOKS` sends `wXbhsf` → response must contain `wXbhsf`

**What it does NOT verify:**
- Response data correctness (E2E tests cover this)
- Response schema validation (too fragile across 35+ methods)
- Business logic (out of scope for monitoring)

**Why this design:**
- Google's breaking change pattern is silent ID changes, not schema changes
- Error responses still contain the method ID, so we detect mismatches even on API errors
- A mismatch means `rpc/types.py` needs updating, triggering a patch release

**On mismatch detection:**
- GitHub Issue auto-created with `bug`, `rpc-breakage`, and `automated` labels
- Report shows expected vs actual IDs and which `RPCMethod` entries need updating

**Manual trigger:** `gh workflow run rpc-health.yml --ref main -f account_rotation_base=auto`

### How to Report API Breakage

1. Check [GitHub Issues](https://github.com/teng-lin/notebooklm-py/issues) for existing reports
2. If not reported, open an issue with:
   - Error message (especially any RPC error codes)
   - Which operation failed
   - When it started failing
3. See [RPC Development Guide](rpc-development.md) for debugging

### Self-Recovery

If the library breaks before we release a fix:

1. Open browser devtools on NotebookLM
2. Perform the failing operation manually
3. Find the new RPC method ID in Network tab
4. Temporarily override the rotated ID without mutating the enum:
   ```bash
   export NOTEBOOKLM_RPC_OVERRIDES='{"LIST_NOTEBOOKS": "NewMethodId"}'
   ```

   The override key is the `RPCMethod` enum name. See
   [configuration.md#environment-variables](configuration.md#environment-variables)
   for validation and host-allowlist behavior.

## Upgrade Recommendations

### Stay Current

```bash
# Always use latest patch version
pip install --upgrade notebooklm-py
```

### Pin Appropriately

```toml
# pyproject.toml - recommended
dependencies = [
    "notebooklm-py>=0.1,<1.0",  # Accept patches and minors
]

# requirements.txt - for reproducibility
notebooklm-py==0.1.0  # Exact version (but update regularly!)
```

### Test Before Upgrading

```bash
# Test in development first
pip install notebooklm-py==X.Y.Z
pytest
```

## Questions?

- **Bug reports**: [GitHub Issues](https://github.com/teng-lin/notebooklm-py/issues)
- **Discussions**: [GitHub Discussions](https://github.com/teng-lin/notebooklm-py/discussions)
- **Security issues**: See [SECURITY.md](../SECURITY.md)
