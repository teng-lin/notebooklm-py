# API Stability and Versioning

**Status:** Active
**Last Updated:** 2026-01-14

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

# Types
Notebook, Source, Artifact, Note
GenerationStatus, AskResult
NotebookDescription, ConversationTurn

# Exceptions
SourceError, SourceProcessingError, SourceTimeoutError, SourceNotFoundError
RPCError

# Enums
AudioFormat, VideoFormat, StudioContentType, ExportType, etc.

# Auth
AuthTokens, DEFAULT_STORAGE_PATH
```

### Internal (May change without notice)

```python
# These are NOT part of the public API:
notebooklm.rpc.*          # RPC protocol internals
notebooklm._core.*        # Core infrastructure
notebooklm._*.py          # All underscore-prefixed modules
notebooklm.auth.*         # Auth internals (except AuthTokens)
```

To use internal APIs, import them explicitly:
```python
# Explicit import for power users (may break)
from notebooklm.rpc import RPCMethod, encode_rpc_request
```

## Deprecation Policy

1. **Deprecation Notice**: Deprecated features emit `DeprecationWarning`
2. **Documentation**: Deprecations are noted in docstrings and CHANGELOG
3. **Removal Timeline**: Deprecated features are removed in the next major version
4. **Migration Guide**: Breaking changes include migration instructions

### Currently Deprecated

None at this time.

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
