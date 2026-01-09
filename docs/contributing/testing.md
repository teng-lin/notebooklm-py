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

This opens a browser, logs into your Google account, and stores cookies in `~/.notebooklm/storage_state.json`.

Verify with:
```bash
notebooklm status
```

### 3. Create Your Read-Only Test Notebook (REQUIRED)

**You MUST create a personal test notebook** for read-only tests. Tests will exit with an error if not configured.

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
export NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID="your-notebook-id-here"
```

**Note:** The generation notebook is auto-created on first run and stored in `NOTEBOOKLM_HOME/generation_notebook_id` (default: `~/.notebooklm/generation_notebook_id`). You don't need to configure it manually.

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
| List/download existing artifacts | `read_only_notebook_id` | Your notebook with pre-made content |
| Add/delete sources or notes | `temp_notebook` | Isolated, auto-cleanup, has content |
| Generate audio/video/quiz | `generation_notebook_id` | Auto-created, has content, CI-aware cleanup |

### `read_only_notebook_id` (Your Test Notebook - REQUIRED)

Returns `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID` env var. **Tests will exit with an error if not set.**

Your notebook must have:
- Multiple sources (text, URL, etc.)
- Pre-generated artifacts (audio, quiz, etc.)

```python
@pytest.mark.readonly
async def test_list_artifacts(self, client, read_only_notebook_id):
    artifacts = await client.artifacts.list(read_only_notebook_id)
    assert isinstance(artifacts, list)
```

### `temp_notebook`

Fresh notebook per test. Automatically deleted after test completes (even on failure).

```python
async def test_add_source(self, client, temp_notebook):
    source = await client.sources.add_url(temp_notebook.id, "https://example.com")
    assert source.id is not None
```

### `generation_notebook_id`

Notebook for generation tests. Auto-created on first run if not configured via env var.

```python
async def test_generate_quiz(self, client, generation_notebook_id):
    result = await client.artifacts.generate_quiz(generation_notebook_id)
    assert result is not None
    assert result.task_id  # Generation returns immediately with task_id
```

**Lifecycle:**
- Auto-created if `NOTEBOOKLM_GENERATION_NOTEBOOK_ID` not set
- Artifacts/notes cleaned BEFORE tests (clean starting state)
- **In CI (CI=true):** Notebook deleted after tests to avoid orphans
- **Locally:** Notebook persists, ID stored in `NOTEBOOKLM_HOME/generation_notebook_id`

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
async def test_generate_audio_default(self, client, generation_notebook_id):
    result = await client.artifacts.generate_audio(generation_notebook_id)
    assert result.task_id, "Expected non-empty task_id"
    assert result.status in ("pending", "in_progress")
    assert result.error is None

# Skipped by default - tests parameter encoding
@pytest.mark.variants
async def test_generate_audio_brief(self, client, generation_notebook_id):
    result = await client.artifacts.generate_audio(
        generation_notebook_id,
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
    """Load cookies from ~/.notebooklm/storage_state.json"""

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
def read_only_notebook_id() -> str:
    """Returns NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID (required)"""

@pytest.fixture
async def temp_notebook(client) -> Notebook:
    """Create notebook with content, auto-delete after test"""

@pytest.fixture
async def generation_notebook_id(client) -> str:
    """Auto-created notebook for generation tests (deleted in CI)"""
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
# Required: Your read-only test notebook with sources and artifacts
NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID=your-notebook-id-here

# Optional: Explicit generation notebook ID (auto-created if not set)
# NOTEBOOKLM_GENERATION_NOTEBOOK_ID=your-generation-notebook-id
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
        ├── Read-only → read_only_notebook_id + @pytest.mark.readonly
        ├── CRUD → temp_notebook
        └── Generation → generation_notebook_id
            └── Parameter variant? → add @pytest.mark.variants
```

### Example: New Generation Test

Add generation tests to `tests/e2e/test_generation.py`:

```python
from .conftest import requires_auth, assert_generation_started

@requires_auth
class TestNewArtifact:
    @pytest.mark.asyncio
    async def test_generate_new_artifact_default(self, client, generation_notebook_id):
        result = await client.artifacts.generate_new(generation_notebook_id)
        # Use helper - skips test if rate limited, fails on other errors
        assert_generation_started(result, "NewArtifact")

    @pytest.mark.asyncio
    @pytest.mark.variants
    async def test_generate_new_artifact_with_options(self, client, generation_notebook_id):
        result = await client.artifacts.generate_new(
            generation_notebook_id,
            option=SomeOption.VALUE,
        )
        assert_generation_started(result, "NewArtifact")
```

Note: Generation tests only need `client` and `generation_notebook_id`. Cleanup is automatic.

### Rate Limiting

NotebookLM has undocumented rate limits. Running many generation tests in sequence can trigger rate limiting:

- Rate-limited tests are **skipped** (not failed) via `assert_generation_started()` helper
- You'll see `SKIPPED (Rate limited by API)` in test output
- Many skipped tests = you've hit rate limits, wait and retry

**Strategies:**
- **Run `readonly` tests first:** `pytest tests/e2e -m readonly` (minimal API calls)
- **Skip variants:** `pytest tests/e2e -m "not variants"` (fewer generation calls)
- **Wait between runs:** Rate limits reset after a few minutes

---

## Rate Limit Optimization

### Understanding Rate Limits

NotebookLM appears to have multiple rate limit mechanisms:

| Limit Type | Scope | Notes |
|------------|-------|-------|
| **Per-type daily quota** | Per artifact type | e.g., limited audio generations per day |
| **Burst rate limit** | Per time window | e.g., generations per 5 minutes |
| **Account-wide** | All operations | Undocumented overall limits |

**Key insight:** Rate limits primarily affect **artifact generation**, not notebook/source creation.

### Current Optimizations

The test suite is designed to minimize rate limit issues:

1. **Spread across artifact types** - Default tests cover 9 different artifact types (audio, video, quiz, flashcards, infographic, slide_deck, data_table, mind_map, study_guide). This distributes load across per-type quotas.

2. **Variants skipped by default** - Only ~10 generation tests run by default. The 20+ variant tests (testing parameter combinations) require `--include-variants`.

3. **Graceful rate limit handling** - `assert_generation_started()` detects rate limiting and skips tests instead of failing.

4. **Combined mutation tests** - `test_artifacts.py` combines poll/rename/wait operations into one test, and spreads across artifact types (flashcards for mutations, quiz for delete).

### Default Generation Tests (10 calls)

```
audio_default, audio_brief  → 2 audio calls
video_default               → 1 video call
quiz_default                → 1 quiz call
flashcards_default          → 1 flashcards call
infographic_default         → 1 infographic call
slide_deck_default          → 1 slide_deck call
data_table_default          → 1 data_table call
mind_map                    → 1 mind_map call (sync)
study_guide                 → 1 study_guide call
```

This spread is optimal for per-type rate limits.

### Generation Notebook Design

The `generation_notebook_id` fixture uses a hybrid approach to balance:
- **Local development**: Notebook persists for post-test verification
- **CI environments**: Auto-cleanup to avoid orphaned notebooks

**How it works:**

| Environment | Behavior |
|-------------|----------|
| `NOTEBOOKLM_GENERATION_NOTEBOOK_ID` set | Uses provided ID, never deleted |
| Local (no `CI` env var) | Auto-creates once, stores ID, persists across runs |
| CI (`CI=true`) | Auto-creates, deletes after tests complete |

Artifacts and notes are always cleaned BEFORE tests to ensure a clean starting state.

### Future Considerations

If rate limiting becomes more severe, consider:

1. **Add delays between generation tests:**
   ```python
   @pytest.fixture(autouse=True)
   async def rate_limit_delay():
       yield
       await asyncio.sleep(2)  # Delay after each test
   ```

2. **CI scheduling** - Spread test runs over time rather than running all at once.

3. **Tiered test runs** - Run readonly tests in CI, full suite nightly.

## CI/CD Setup

### GitHub Actions Workflows

The project has two CI workflows:

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `test.yml` | Push/PR to main | Unit tests, linting, type checking |
| `nightly.yml` | Daily at 6 AM UTC | E2E tests with real API |

### Automatic CI (test.yml)

Runs automatically on every push and PR:

1. **Quality Job** - Ruff linting + mypy type checking (runs once)
2. **Test Job** - Unit/integration tests on Ubuntu and macOS across Python 3.9-3.12

No setup required - this works out of the box.

### Nightly E2E Tests (nightly.yml)

Runs E2E tests daily against the real NotebookLM API. Requires repository secrets.

#### Step 1: Get Your Storage State

```bash
# Make sure you're logged in
notebooklm login

# Copy the storage state content
cat ~/.notebooklm/storage_state.json
```

#### Step 2: Add the Secrets to GitHub

1. Go to your repository on GitHub
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Add **two** repository secrets:

   **Secret 1: Auth JSON**
   - **Name:** `NOTEBOOKLM_AUTH_JSON`
   - **Value:** Paste the entire JSON content from step 1

   **Secret 2: Read-Only Notebook ID**
   - **Name:** `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID`
   - **Value:** Your test notebook ID (from URL: `notebooklm.google.com/notebook/YOUR_ID`)

#### Step 3: Test the Workflow

```bash
# Trigger manually to verify it works
gh workflow run nightly.yml

# Check the run status
gh run list --workflow=nightly.yml
```

### Maintaining CI Secrets

| Task | Frequency | Action |
|------|-----------|--------|
| Refresh credentials | Every 1-2 weeks | Run `notebooklm login`, update secret |
| Check nightly results | Daily | Review Actions tab for failures |
| Update secret after expiry | When E2E tests fail with auth errors | Repeat steps 1-2 |

### Security Best Practices

- Use a **dedicated test Google account** (not personal)
- The secret is encrypted and never exposed in logs
- Only the main repository can access secrets (forks cannot)
- Session cookies expire naturally, requiring periodic refresh

---

## Troubleshooting

### Tests skip with "no auth stored"

Run `notebooklm login` and complete browser authentication.

### Tests fail with permission errors

Your `NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID` may be invalid or you don't own it. Verify:
```bash
echo $NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID
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

### "NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID not set" error

This is expected - E2E tests require your own notebook. Follow Prerequisites step 3 to create one.

### Too many artifacts accumulating

- `temp_notebook`: Automatically deleted after each test
- `generation_notebook_id` in CI: Notebook deleted after tests
- `generation_notebook_id` locally: Artifacts cleaned before each run, notebook persists (ID stored in `NOTEBOOKLM_HOME/generation_notebook_id`)
- If tests are interrupted, orphaned notebooks may remain - clean up manually in the NotebookLM UI
