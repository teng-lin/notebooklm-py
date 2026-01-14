# Contributing Guide

**Status:** Active
**Last Updated:** 2026-01-13

This guide covers everything you need to contribute to `notebooklm-py`: architecture overview, testing, and releasing.

---

## Architecture

### Package Structure

```
src/notebooklm/
├── __init__.py          # Public exports
├── client.py            # NotebookLMClient main class
├── auth.py              # Authentication handling
├── types.py             # Dataclasses and type definitions
├── _core.py             # Core HTTP/RPC infrastructure
├── _notebooks.py        # NotebooksAPI implementation
├── _sources.py          # SourcesAPI implementation
├── _artifacts.py        # ArtifactsAPI implementation
├── _chat.py             # ChatAPI implementation
├── _research.py         # ResearchAPI implementation
├── _notes.py            # NotesAPI implementation
├── rpc/                 # RPC protocol layer
│   ├── __init__.py
│   ├── types.py         # RPCMethod enum and constants
│   ├── encoder.py       # Request encoding
│   └── decoder.py       # Response parsing
└── cli/                 # CLI implementation
    ├── __init__.py      # CLI package exports
    ├── helpers.py       # Shared utilities
    ├── session.py       # login, use, status, clear
    ├── notebook.py      # list, create, delete, rename
    ├── source.py        # source add, list, delete
    ├── artifact.py      # artifact list, get, delete
    ├── generate.py      # generate audio, video, etc.
    ├── download.py      # download audio, video, etc.
    ├── chat.py          # ask, configure, history
    └── ...
```

### Layered Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         CLI Layer                           │
│   cli/session.py, cli/notebook.py, cli/generate.py, etc.   │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                      Client Layer                           │
│  NotebookLMClient → NotebooksAPI, SourcesAPI, ArtifactsAPI │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                       Core Layer                            │
│              ClientCore → _rpc_call(), HTTP client          │
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
| **CLI** | `cli/*.py` | User commands, input validation, Rich output |
| **Client** | `client.py`, `_*.py` | High-level Python API, returns typed dataclasses |
| **Core** | `_core.py` | HTTP client, request counter, RPC abstraction |
| **RPC** | `rpc/*.py` | Protocol encoding/decoding, method IDs |

### Key Design Decisions

**Why underscore prefixes?** Files like `_notebooks.py` are internal implementation. Public API stays clean (`from notebooklm import NotebookLMClient`).

**Why namespaced APIs?** `client.notebooks.list()` instead of `client.list_notebooks()` - better organization, scales well, tab-completion friendly.

**Why async?** Google's API can be slow. Async enables concurrent operations and non-blocking downloads.

### Adding New Features

**New RPC Method:**
1. Capture traffic (see [RPC Development Guide](rpc-development.md))
2. Add to `rpc/types.py`: `NEW_METHOD = "AbCdEf"`
3. Implement in appropriate `_*.py` API class
4. Add dataclass to `types.py` if needed
5. Add CLI command if user-facing

**New API Class:**
1. Create `_newfeature.py` with `NewFeatureAPI` class
2. Add to `client.py`: `self.newfeature = NewFeatureAPI(self._core)`
3. Export types from `__init__.py`

---

## Testing

### Prerequisites

1. **Install dependencies:**
   ```bash
   uv pip install -e ".[dev]"
   ```

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
pytest

# E2E tests (requires auth + test notebook)
pytest tests/e2e -m readonly        # Read-only tests only
pytest tests/e2e -m "not variants"  # Skip parameter variants
pytest tests/e2e --include-variants # All tests including variants
```

### Test Structure

```
tests/
├── unit/           # No network, fast, mock everything
├── integration/    # Mocked HTTP responses + VCR cassettes
└── e2e/            # Real API calls (requires auth)
```

### E2E Fixtures

| Fixture | Use Case |
|---------|----------|
| `read_only_notebook_id` | List/download existing artifacts |
| `temp_notebook` | Add/delete sources (auto-cleanup) |
| `generation_notebook_id` | Generate artifacts (CI-aware cleanup) |

### Rate Limiting

NotebookLM has undocumented rate limits. Generation tests may be skipped when rate limited:
- Use `pytest tests/e2e -m readonly` for quick validation
- Wait a few minutes between full test runs
- `SKIPPED (Rate limited by API)` is expected behavior, not failure

### VCR Testing (Recorded HTTP)

Record HTTP interactions for offline/deterministic replay:

```bash
# Record cassettes (not committed to repo)
NOTEBOOKLM_VCR_RECORD=1 pytest tests/integration/test_vcr_*.py -v

# Run with recorded responses
pytest tests/integration/test_vcr_*.py
```

Sensitive data (cookies, tokens, emails) is automatically scrubbed.

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

## Releasing

### Pre-release Checklist

- [ ] All tests pass: `pytest`
- [ ] E2E readonly tests pass: `pytest tests/e2e -m readonly`
- [ ] No uncommitted changes: `git status`
- [ ] On `main` branch with latest changes
- [ ] Version updated in `src/notebooklm/__init__.py`
- [ ] CHANGELOG.md updated

### Release Steps

1. **Update version** in `src/notebooklm/__init__.py`:
   ```python
   __version__ = "X.Y.Z"
   ```

2. **Update CHANGELOG.md** with release notes

3. **Commit and push:**
   ```bash
   git add -A
   git commit -m "chore: release vX.Y.Z"
   git push
   ```

4. **Test on TestPyPI:**
   ```bash
   python -m build
   twine upload --repository testpypi dist/*
   pip install --index-url https://test.pypi.org/simple/ \
       --extra-index-url https://pypi.org/simple notebooklm-py
   notebooklm --version
   ```

5. **Create release tag:**
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

   This triggers GitHub Actions to publish to PyPI automatically.

### Versioning Policy

| Change Type | Bump | Example |
|-------------|------|---------|
| RPC method ID fixes | PATCH | 0.1.0 → 0.1.1 |
| Bug fixes | PATCH | 0.1.1 → 0.1.2 |
| New features (backward compatible) | MINOR | 0.1.2 → 0.2.0 |
| Breaking API changes | MAJOR | 0.2.0 → 1.0.0 |

See [stability.md](stability.md) for full versioning policy.

---

## CI/CD

### Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `test.yml` | Push/PR | Unit tests, linting, type checking |
| `nightly.yml` | Daily 6 AM UTC | E2E tests with real API |
| `rpc-health.yml` | Daily 7 AM UTC | RPC method ID monitoring (see [stability.md](stability.md#automated-rpc-health-check)) |
| `publish.yml` | Tag push | Publish to PyPI |

### Setting Up Nightly E2E Tests

1. Get storage state: `cat ~/.notebooklm/storage_state.json`
2. Add GitHub secrets:
   - `NOTEBOOKLM_AUTH_JSON`: Storage state JSON
   - `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID`: Your test notebook ID

### Maintaining Secrets

| Task | Frequency |
|------|-----------|
| Refresh credentials | Every 1-2 weeks |
| Check nightly results | Daily |

---

## Getting Help

- Check existing implementations in `_*.py` files
- Look at test files for expected structures
- See [RPC Development Guide](rpc-development.md) for protocol details
- Open an issue with captured request/response (sanitized)
