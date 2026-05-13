# API Stability and Versioning

**Status:** Active
**Last Updated:** 2026-05-09

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
# Client
NotebookLMClient
NotebookLMClient.from_storage()
NotebookLMClient.notebooks
NotebookLMClient.sources
NotebookLMClient.artifacts
NotebookLMClient.chat
NotebookLMClient.research
NotebookLMClient.notes
NotebookLMClient.settings
NotebookLMClient.sharing

# Types
Notebook, Source, Artifact, Note
GenerationStatus, AskResult
NotebookDescription, ConversationTurn
ShareStatus, SharedUser, SourceFulltext
NotebookMetadata, SourceSummary
AccountLimits, AccountTier
ChatReference, ReportSuggestion, SuggestedTopic

# Exceptions (all inherit from NotebookLMError)
NotebookLMError                    # Base exception
RPCError, AuthError, RateLimitError, RPCTimeoutError, ServerError
NetworkError, DecodingError, UnknownRPCMethodError
ClientError, ConfigurationError, ValidationError
# Domain-specific
SourceError, SourceAddError, SourceProcessingError, SourceTimeoutError, SourceNotFoundError
NotebookError, NotebookNotFoundError
ArtifactError, ArtifactDownloadError, ArtifactNotFoundError, ArtifactNotReadyError, ArtifactParseError
ChatError

# Enums
AudioFormat, AudioLength
VideoFormat, VideoStyle
QuizQuantity, QuizDifficulty
InfographicOrientation, InfographicDetail, InfographicStyle
SlideDeckFormat, SlideDeckLength
ReportFormat
SourceType, ArtifactType, SourceStatus
ShareAccess, SharePermission, ShareViewLevel
ChatGoal, ChatResponseLength, ChatMode
DriveMimeType, ExportType

# Auth
AuthTokens
# (DEFAULT_STORAGE_PATH is deprecated; use notebooklm.paths.get_storage_path())

# Helpers (cookies extra) - imported from notebooklm.auth
notebooklm.auth.convert_rookiepy_cookies_to_storage_state  # requires `pip install "notebooklm-py[cookies]"`
```

### Internal (May change without notice)

```python
# These are NOT part of the public API:
notebooklm.rpc.*          # RPC protocol internals
notebooklm._core.*        # Core infrastructure
notebooklm._*.py          # All underscore-prefixed modules
notebooklm.auth.*         # Auth internals (except AuthTokens and convert_rookiepy_cookies_to_storage_state)
```

To use internal APIs, import them explicitly:
```python
# Explicit import for power users (may break)
from notebooklm.rpc import RPCMethod, encode_rpc_request
```

## Deprecation Policy

1. **Deprecation Notice**: Deprecated features emit `DeprecationWarning`
2. **Documentation**: Deprecations are noted in docstrings and CHANGELOG
3. **Removal Timeline**: Deprecated features are removed in the next major version. While the project is in 0.x, removal may instead occur in the next MINOR release after at least one MINOR cycle of `DeprecationWarning` (see "0.x Pre-1.0 Semantics" above).
4. **Migration Guide**: Breaking changes include migration instructions

### Currently Deprecated

The following are deprecated and will be removed in **v0.5.0**:

| Deprecated | Replacement | Notes |
|------------|-------------|-------|
| `Source.source_type` | `Source.kind` | Returns `SourceType` str enum |
| `Artifact.artifact_type` | `Artifact.kind` | Returns `ArtifactType` str enum |
| `Artifact.variant` | `Artifact.kind` | Use `.is_quiz` / `.is_flashcards` |
| `SourceFulltext.source_type` | `SourceFulltext.kind` | Returns `SourceType` str enum |
| `StudioContentType` | `ArtifactType` | Str enum for user-facing code |
| `DEFAULT_STORAGE_PATH` | `notebooklm.paths.get_storage_path()` | Module-level constant replaced by helper |

> **Note:** These were originally targeted for removal in v0.4.0. The removal was deferred one release to give downstream users more time to migrate. They continue to emit `DeprecationWarning` and will be removed in v0.5.0.

## Migration Guides

### Migrating from v0.3.x to v0.4.0

Version 0.4.0 is backward compatible with v0.3.x. Notable additions:

- **Multi-account profiles** - Existing single-account setups continue to work as the implicit default profile. Your existing `~/.notebooklm/storage_state.json` is auto-detected — no manual migration is required. New accounts can be added via `notebooklm profile create <name>`.
- **`[cookies]` optional extra** - To reuse cookies from your existing browser, install with `pip install "notebooklm-py[cookies]"` (requires `rookiepy`).
- **Deprecation removal deferred** - The deprecated attributes originally scheduled for v0.4.0 (`Source.source_type`, `Artifact.artifact_type`, `Artifact.variant`, `SourceFulltext.source_type`, `StudioContentType`, `DEFAULT_STORAGE_PATH`) will now be removed in v0.5.0. They still emit `DeprecationWarning` — please migrate before v0.5.0.

### Migrating from v0.2.x to v0.3.0

Version 0.3.0 introduces **deprecated** attributes that emit `DeprecationWarning` when accessed.
These will be removed in v0.5.0. Update your code now to avoid breakage.

#### 1. `Source.source_type` → `Source.kind`

**Before (deprecated):**
```python
source = await client.sources.list(notebook_id)[0]
if source.source_type == "pdf":  # ⚠️ Emits DeprecationWarning
    print("This is a PDF")
```

**After (recommended):**
```python
from notebooklm import SourceType

source = await client.sources.list(notebook_id)[0]

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

**Before (deprecated):**
```python
from notebooklm import StudioContentType  # ⚠️ Emits DeprecationWarning

artifact = await client.artifacts.list(notebook_id)[0]
if artifact.artifact_type == StudioContentType.AUDIO:  # ⚠️ Emits DeprecationWarning
    print("This is an audio artifact")
```

**After (recommended):**
```python
from notebooklm import ArtifactType

artifact = await client.artifacts.list(notebook_id)[0]

# Option 1: Use enum comparison (recommended)
if artifact.kind == ArtifactType.AUDIO:
    print("This is an audio artifact")

# Option 2: Use string comparison (str enum supports this)
if artifact.kind == "audio":
    print("This is an audio artifact")
```

**Available `ArtifactType` values:**
`AUDIO`, `VIDEO`, `REPORT`, `QUIZ`, `FLASHCARDS`, `MIND_MAP`, `INFOGRAPHIC`, `SLIDE_DECK`, `DATA_TABLE`, `UNKNOWN`

#### 3. `Artifact.variant` → `Artifact.kind` or helpers

**Before (deprecated):**
```python
if artifact.artifact_type == 4 and artifact.variant == 2:  # ⚠️ Deprecated
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

1. **Detection**: Automated RPC health check runs nightly (see below)
2. **Investigation**: Identify changed method IDs using browser devtools
3. **Fix**: Update `rpc/types.py` with new method IDs
4. **Release**: Push patch release as soon as possible

### Automated RPC Health Check

A nightly GitHub Action (`rpc-health.yml`) monitors all 35+ RPC methods for ID changes.

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

**Configuration:**
- `NOTEBOOKLM_RPC_DELAY`: Delay between RPC calls in seconds (default: 1.0)

**Manual trigger:** `gh workflow run rpc-health.yml`

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
4. Temporarily patch your local copy:
   ```python
   # In your code, before using the library
   from notebooklm.rpc.types import RPCMethod
   RPCMethod.SOME_METHOD._value_ = "NewMethodId"
   ```

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
