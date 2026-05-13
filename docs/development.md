# Contributing Guide

**Status:** Active
**Last Updated:** 2026-03-12

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
├── _settings.py         # SettingsAPI implementation
├── _sharing.py          # SharingAPI implementation
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
│   cli/session.py, cli/notebook.py, cli/generate.py, etc.    │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                      Client Layer                           │
│  NotebookLMClient → NotebooksAPI, SourcesAPI, ArtifactsAPI  │
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

# E2E tests (requires auth + test notebook)
uv run pytest tests/e2e -m readonly        # Read-only tests only
uv run pytest tests/e2e -m "not variants"  # Skip parameter variants
uv run pytest tests/e2e --include-variants # All tests including variants

# Select a profile for E2E tests
uv run pytest tests/e2e -m e2e --profile work
```

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
├── unit/               # No network, fast, mock everything
├── integration/        # Mocked HTTP responses + VCR cassettes
│   ├── test_vcr_*.py   # Client-level VCR tests
│   └── cli_vcr/        # CLI integration tests with VCR
└── e2e/                # Real API calls (requires auth)
```

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

# Record new cassettes (sensitive data auto-scrubbed)
NOTEBOOKLM_VCR_RECORD=1 uv run pytest tests/integration/test_vcr_*.py -v
```

Sensitive data (cookies, tokens, emails) is automatically scrubbed from cassettes.

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

## CI/CD

### Workflows

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `test.yml` | Push/PR | Unit tests, linting, type checking |
| `nightly.yml` | Daily 6 AM UTC | E2E tests with real API |
| `rpc-health.yml` | Daily 7 AM UTC | RPC method ID monitoring (see [stability.md](stability.md#automated-rpc-health-check)) |
| `testpypi-publish.yml` | Manual dispatch | Publish to TestPyPI |
| `verify-package.yml` | Manual dispatch | Verify TestPyPI or PyPI install + E2E |
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
- Open an issue with captured request/response (sanitized)
