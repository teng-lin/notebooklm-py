# Testing Guide

**Status:** Active
**Last Updated:** 2026-01-08

## Prerequisites

Before running ANY E2E tests, you must complete this setup:

### 1. Install Dependencies

```bash
# Using uv (recommended)
uv pip install -e ".[dev]"

# Or using pip
pip install -e ".[dev]"
```

### 2. Authenticate with NotebookLM

```bash
notebooklm login
```

This opens a browser, logs into your Google account, and stores cookies in `~/.notebooklm/storage.json`.

Verify with:
```bash
notebooklm status
```

### 3. Create Your Test Notebook (REQUIRED)

**You MUST create a personal test notebook** with content. Tests will exit with an error if not configured.

1. Go to [NotebookLM](https://notebooklm.google.com)
2. Create a new notebook (e.g., "E2E Test Notebook")
3. Add multiple sources:
   - At least one text/paste source
   - At least one URL source
   - Optionally: PDF, YouTube video
4. **Generate artifacts** (at least one of each type for full test coverage):
   - Audio overview (try different formats: Deep Dive, Brief)
   - Video overview
   - Quiz
   - Flashcards
   - Infographic
   - Slide deck
   - Report (Briefing Doc, Study Guide, or Blog Post)

   *Tip: Generate multiple artifacts of the same type with different customizations to test download selection.*

5. Copy the notebook ID from the URL: `notebooklm.google.com/notebook/YOUR_NOTEBOOK_ID`
6. Create your `.env` file:

```bash
cp .env.example .env
# Edit .env and set your notebook ID
```

Or set the environment variable directly:
```bash
export NOTEBOOKLM_TEST_NOTEBOOK_ID="your-notebook-id-here"
```

### 4. Verify Setup

```bash
# Should pass - unit tests don't need auth
pytest tests/unit/

# Should pass - uses your test notebook
pytest tests/e2e -m readonly -v
```

If tests skip with "no auth stored" or fail with permission errors, your setup is incomplete.

---

## ⚠️ Rate Limiting

NotebookLM has undocumented API rate limits. Running many tests in sequence (especially generation tests) can trigger rate limiting.

**How it works:**
- Generation tests use `assert_generation_started()` helper
- Rate-limited tests are **SKIPPED** (not failed) - you'll see `SKIPPED (Rate limited by API)`
- The API returns `USER_DISPLAYABLE_ERROR` when rate limited, detected via `result.is_rate_limited`

**Strategies:**
- Run `readonly` tests for quick validation (minimal API calls)
- Skip `variants` to reduce generation API calls
- Wait a few minutes between full test runs for rate limits to reset
- Check test output for skipped tests - many skips indicate you've hit rate limits

---

## Quick Reference

```bash
# Run unit + integration tests (default, no auth needed)
pytest

# Run E2E tests (requires setup above)
pytest tests/e2e -m readonly        # Read-only tests only (minimal API calls)
pytest tests/e2e -m "not variants"  # Skip generation parameter variants
pytest tests/e2e                    # All tests (variants skipped by default)
pytest tests/e2e --include-variants # ALL tests including parameter variants
```

## Test Structure

```
tests/
├── conftest.py              # RPC response builders
├── unit/                    # No network, fast
│   ├── cli/                 # CLI command tests
│   ├── test_decoder.py
│   ├── test_encoder.py
│   └── ...
├── integration/             # Mocked HTTP
│   ├── conftest.py          # Mock auth tokens
│   └── test_*.py
└── e2e/                     # Real API
    ├── conftest.py          # Auth, fixtures, cleanup
    ├── test_generation.py   # All artifact generation tests
    ├── test_artifacts.py    # Artifact CRUD/list operations
    ├── test_downloads.py    # Download operations
    ├── test_sources.py      # Source operations
    └── ...
```

## E2E Fixtures

### Which Fixture Do I Need?

| I want to... | Use | Why |
|--------------|-----|-----|
| List/download existing artifacts | `test_notebook_id` | Your notebook with pre-made content |
| Add/delete sources or notes | `temp_notebook` | Isolated, auto-cleanup, has content |
| Generate audio/video/quiz | `generation_notebook` | Writable, has content, auto-cleanup |

### `test_notebook_id` (Your Test Notebook - REQUIRED)

Returns `NOTEBOOKLM_TEST_NOTEBOOK_ID` env var. **Tests will exit with an error if not set.**

Your notebook must have:
- Multiple sources (text, URL, etc.)
- Pre-generated artifacts (audio, quiz, etc.)

```python
@pytest.mark.readonly
async def test_list_artifacts(self, client, test_notebook_id):
    artifacts = await client.artifacts.list(test_notebook_id)
    assert isinstance(artifacts, list)
```

### `temp_notebook`

Fresh notebook per test. Automatically deleted after test completes (even on failure).

```python
async def test_add_source(self, client, temp_notebook):
    source = await client.sources.add_url(temp_notebook.id, "https://example.com")
    assert source.id is not None
```

### `generation_notebook`

Notebook with content for generation tests. Automatically deleted after each test.

```python
async def test_generate_quiz(self, client, generation_notebook):
    result = await client.artifacts.generate_quiz(generation_notebook.id)
    assert result is not None
    assert result.task_id  # Generation returns immediately with task_id
```

**Why not readonly?** You can't generate on notebooks you don't own.
**Why not temp_notebook?** Both work similarly now - use `generation_notebook` for generation tests by convention.
**Cleanup:** Automatic - notebook deleted after each test.

## Test Markers

All markers defined in `pyproject.toml`:

| Marker | Purpose |
|--------|---------|
| `readonly` | Read-only tests against user's test notebook |
| `variants` | Generation parameter variants (skip to reduce API calls) |

### Understanding Generation Tests

Generation tests (audio, video, quiz, etc.) call the API and receive a `task_id` immediately - they do **not** wait for the artifact to complete. This means:

- Tests are fast (single API call each)
- The main concern is **rate limiting**, not execution time
- Running many generation tests in sequence triggers rate limits

### Variant Testing

Each artifact type has multiple parameter options (format, style, difficulty, etc.). To balance coverage and API quota:

- **Default test:** Tests with default parameters (always runs)
- **Variant tests:** Test other parameter combinations (skipped by default)

**Variants are skipped by default** to reduce API calls. Use `--include-variants` to run them:

```bash
pytest tests/e2e                    # Skips variants
pytest tests/e2e --include-variants # Includes variants
```

```python
# Runs by default - tests that generation works
async def test_generate_audio_default(self, client, generation_notebook):
    result = await client.artifacts.generate_audio(generation_notebook.id)
    assert result.task_id, "Expected non-empty task_id"
    assert result.status in ("pending", "in_progress")
    assert result.error is None

# Skipped by default - tests parameter encoding
@pytest.mark.variants
async def test_generate_audio_brief(self, client, generation_notebook):
    result = await client.artifacts.generate_audio(
        generation_notebook.id,
        audio_format=AudioFormat.BRIEF,
    )
    assert result.task_id, "Expected non-empty task_id"
```

## E2E Fixtures Reference

From `tests/e2e/conftest.py`:

### Authentication

```python
@pytest.fixture(scope="session")
def auth_cookies() -> dict[str, str]:
    """Load cookies from ~/.notebooklm/storage.json"""

@pytest.fixture(scope="session")
def auth_tokens(auth_cookies) -> AuthTokens:
    """Fetch CSRF + session ID from NotebookLM homepage"""

@pytest.fixture
async def client(auth_tokens) -> NotebookLMClient:
    """Fresh client per test"""
```

### Notebooks

```python
@pytest.fixture
def test_notebook_id() -> str:
    """Returns NOTEBOOKLM_TEST_NOTEBOOK_ID (required)"""

@pytest.fixture
async def temp_notebook(client) -> Notebook:
    """Create notebook with content, auto-delete after test"""

@pytest.fixture
async def generation_notebook(client) -> Notebook:
    """Notebook with content for generation tests, auto-delete after test"""
```

### Decorators

```python
@requires_auth  # Skip test if no auth stored
```

### Helpers

```python
from .conftest import assert_generation_started

# Use in generation tests - skips on rate limiting instead of failing
result = await client.artifacts.generate_audio(notebook_id)
assert_generation_started(result, "Audio")  # Skips if rate limited
```

## Environment Variables

Set via `.env` file (recommended) or shell export:

```bash
# Required: Your test notebook with sources and artifacts
NOTEBOOKLM_TEST_NOTEBOOK_ID=your-notebook-id-here
```

See `.env.example` for the template.

## Writing New Tests

### Decision Tree

```
Need network?
├── No → tests/unit/
├── Mocked → tests/integration/
└── Real API → tests/e2e/
    └── What notebook?
        ├── Read-only → test_notebook_id + @pytest.mark.readonly
        ├── CRUD → temp_notebook
        └── Generation → generation_notebook
            └── Parameter variant? → add @pytest.mark.variants
```

### Example: New Generation Test

Add generation tests to `tests/e2e/test_generation.py`:

```python
from .conftest import requires_auth, assert_generation_started

@requires_auth
class TestNewArtifact:
    @pytest.mark.asyncio
    async def test_generate_new_artifact_default(self, client, generation_notebook):
        result = await client.artifacts.generate_new(generation_notebook.id)
        # Use helper - skips test if rate limited, fails on other errors
        assert_generation_started(result, "NewArtifact")

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_new_artifact_with_options(self, client, generation_notebook):
        result = await client.artifacts.generate_new(
            generation_notebook.id,
            option=SomeOption.VALUE,
        )
        assert_generation_started(result, "NewArtifact")
```

Note: Generation tests only need `client` and `generation_notebook`. Cleanup is automatic.

### Rate Limiting

NotebookLM has undocumented rate limits. Running many generation tests in sequence can trigger rate limiting:

- Rate-limited tests are **skipped** (not failed) via `assert_generation_started()` helper
- You'll see `SKIPPED (Rate limited by API)` in test output
- Many skipped tests = you've hit rate limits, wait and retry

**Strategies:**
- **Run `readonly` tests first:** `pytest tests/e2e -m readonly` (minimal API calls)
- **Skip variants:** `pytest tests/e2e -m "not variants"` (fewer generation calls)
- **Wait between runs:** Rate limits reset after a few minutes

## Troubleshooting

### Tests skip with "no auth stored"

Run `notebooklm login` and complete browser authentication.

### Tests fail with permission errors

Your `NOTEBOOKLM_TEST_NOTEBOOK_ID` may be invalid or you don't own it. Verify:
```bash
echo $NOTEBOOKLM_TEST_NOTEBOOK_ID
notebooklm list  # Should show your notebooks
```

### Tests hang or timeout

- **CSRF token expired:** Run `notebooklm login` again.
- **Download tests:** May timeout if browser auth expired. Clear profile and re-login:
  ```bash
  rm -rf ~/.notebooklm/browser_profile && notebooklm login
  ```

### Rate limiting / Many tests skipped

NotebookLM has undocumented rate limits. If you see many tests `SKIPPED (Rate limited by API)`:

1. **This is expected behavior** - tests skip instead of fail when rate limited
2. **Wait and retry:** Rate limits reset after a few minutes
3. **Reduce API calls:** Use `pytest tests/e2e -m "not variants"` (fewer generation calls)
4. **Run single tests:** `pytest tests/e2e/test_generation.py::TestAudioGeneration::test_generate_audio_default`

Note: The `assert_generation_started()` helper detects rate limiting via `USER_DISPLAYABLE_ERROR` from the API and skips the test gracefully.

### "CSRF token invalid" or 403 errors

Your session expired. Re-authenticate:
```bash
notebooklm login
```

### "NOTEBOOKLM_TEST_NOTEBOOK_ID not set" error

This is expected - E2E tests require your own notebook. Follow Prerequisites step 3 to create one.

### Too many artifacts accumulating

Generation tests create artifacts in `generation_notebook` (deleted at session end) and `temp_notebook` (deleted per test). If you interrupt tests, orphaned notebooks may remain. Clean up manually in the NotebookLM UI.
