"""E2E test fixtures and configuration."""

import os
import warnings
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, rely on shell environment

from notebooklm import NotebookLMClient
from notebooklm.auth import (
    AuthTokens,
    extract_csrf_from_html,
    extract_session_id_from_html,
    load_auth_from_storage,
)
from notebooklm.paths import get_home_dir

# =============================================================================
# Constants
# =============================================================================

# Delay constants for polling
SOURCE_PROCESSING_DELAY = 2.0  # Delay for source processing
POLL_INTERVAL = 2.0  # Interval between poll attempts
POLL_TIMEOUT = 60.0  # Max time to wait for operations


def assert_generation_started(result, artifact_type: str = "Artifact") -> None:
    """Assert that artifact generation started successfully.

    Skips the test if rate limited by the API instead of failing.

    Args:
        result: GenerationStatus from a generate_* method
        artifact_type: Name of artifact type for error messages

    Raises:
        pytest.skip: If rate limited by API
        AssertionError: If generation failed for other reasons
    """
    assert result is not None, f"{artifact_type} generation returned None"

    if result.is_rate_limited:
        pytest.skip("Rate limited by API")

    assert result.task_id, f"{artifact_type} generation failed: {result.error}"
    assert result.status in ("pending", "in_progress"), (
        f"Unexpected {artifact_type.lower()} status: {result.status}"
    )


def has_auth() -> bool:
    try:
        load_auth_from_storage()
        return True
    except (FileNotFoundError, ValueError):
        return False


requires_auth = pytest.mark.skipif(
    not has_auth(),
    reason="Requires NotebookLM authentication (run 'notebooklm login')",
)


# =============================================================================
# Pytest Hooks
# =============================================================================


def pytest_addoption(parser):
    """Add --include-variants option for e2e tests."""
    parser.addoption(
        "--include-variants",
        action="store_true",
        default=False,
        help="Include variant tests (skipped by default to save API quota)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip variant tests by default unless --include-variants is passed."""
    if config.getoption("--include-variants"):
        return

    skip_variants = pytest.mark.skip(
        reason="Variant tests skipped by default. Use --include-variants to run."
    )
    for item in items:
        if "variants" in [m.name for m in item.iter_markers()]:
            item.add_marker(skip_variants)


# =============================================================================
# Auth Fixtures (session-scoped for efficiency)
# =============================================================================


@pytest.fixture(scope="session")
def auth_cookies() -> dict[str, str]:
    """Load auth cookies from storage (session-scoped)."""
    return load_auth_from_storage()


@pytest.fixture(scope="session")
def auth_tokens(auth_cookies) -> AuthTokens:
    """Fetch auth tokens synchronously (session-scoped)."""
    import asyncio

    async def _fetch_tokens():
        cookie_header = "; ".join(f"{k}={v}" for k, v in auth_cookies.items())
        async with httpx.AsyncClient() as http:
            resp = await http.get(
                "https://notebooklm.google.com/",
                headers={"Cookie": cookie_header},
                follow_redirects=True,
            )
            resp.raise_for_status()
            csrf = extract_csrf_from_html(resp.text)
            session_id = extract_session_id_from_html(resp.text)
        return AuthTokens(cookies=auth_cookies, csrf_token=csrf, session_id=session_id)

    return asyncio.run(_fetch_tokens())


@pytest.fixture
async def client(auth_tokens) -> AsyncGenerator[NotebookLMClient, None]:
    async with NotebookLMClient(auth_tokens) as c:
        yield c


@pytest.fixture
def read_only_notebook_id():
    """Get notebook ID from NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID env var.

    This env var is REQUIRED for E2E tests. You must create your own
    read-only test notebook with sources and artifacts.

    This fixture provides a notebook ID for READ-ONLY tests - tests that
    list, get, or query but do NOT modify the notebook. Do not use this
    fixture for tests that create, update, or delete resources.

    See docs/contributing/testing.md for setup instructions.
    """
    notebook_id = os.environ.get("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID")
    if not notebook_id:
        pytest.exit(
            "\n\nERROR: NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID environment variable is not set.\n\n"
            "E2E tests require YOUR OWN test notebook with content.\n\n"
            "Setup instructions:\n"
            "  1. Create a notebook at https://notebooklm.google.com\n"
            "  2. Add sources (text, URL, PDF, etc.)\n"
            "  3. Generate some artifacts (audio, quiz, etc.)\n"
            "  4. Copy notebook ID from URL and run:\n"
            "     export NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID='your-notebook-id'\n\n"
            "See docs/contributing/testing.md for details.\n",
            returncode=1,
        )
    return notebook_id


@pytest.fixture
def created_notebooks():
    notebooks = []
    yield notebooks


@pytest.fixture
async def cleanup_notebooks(created_notebooks, auth_tokens):
    """Cleanup created notebooks after test."""
    yield
    if created_notebooks:
        async with NotebookLMClient(auth_tokens) as client:
            for nb_id in created_notebooks:
                try:
                    await client.notebooks.delete(nb_id)
                except Exception as e:
                    warnings.warn(f"Failed to cleanup notebook {nb_id}: {e}", stacklevel=2)


# =============================================================================
# Notebook Fixtures
# =============================================================================


@pytest.fixture
async def temp_notebook(client, created_notebooks, cleanup_notebooks):
    """Create a temporary notebook with content that auto-deletes after test.

    Use for CRUD tests that need isolated state. Includes a text source
    so artifact generation operations have content to work with.
    """
    import asyncio
    from uuid import uuid4

    notebook = await client.notebooks.create(f"Test-{uuid4().hex[:8]}")
    created_notebooks.append(notebook.id)

    # Add a text source so artifact operations have content to work with
    await client.sources.add_text(
        notebook.id,
        title="Test Content",
        content=(
            "This is test content for E2E testing. "
            "It covers topics including artificial intelligence, "
            "machine learning, and software engineering principles."
        ),
    )

    # Delay to ensure source is processed
    await asyncio.sleep(SOURCE_PROCESSING_DELAY)

    return notebook


# =============================================================================
# Generation Notebook Fixtures
# =============================================================================

# File to store auto-created generation notebook ID
GENERATION_NOTEBOOK_ID_FILE = "generation_notebook_id"

# Module-level state to ensure cleanup only runs once per session
_generation_cleanup_done = False


def _get_generation_notebook_id_path() -> Path:
    """Get the path to the generation notebook ID file."""
    return get_home_dir() / GENERATION_NOTEBOOK_ID_FILE


def _load_stored_generation_notebook_id() -> str | None:
    """Load generation notebook ID from stored file."""
    path = _get_generation_notebook_id_path()
    if path.exists():
        try:
            return path.read_text().strip()
        except Exception:
            return None
    return None


def _save_generation_notebook_id(notebook_id: str) -> None:
    """Save generation notebook ID to file for future runs."""
    path = _get_generation_notebook_id_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(notebook_id)


async def _create_generation_notebook(client: NotebookLMClient) -> str:
    """Create a new generation notebook with content.

    Returns the notebook ID.
    """
    import asyncio
    from uuid import uuid4

    notebook = await client.notebooks.create(f"E2E-Generation-{uuid4().hex[:8]}")

    # Add a text source so the notebook has content for operations
    # Content must be substantial enough for all artifact types including infographics
    await client.sources.add_text(
        notebook.id,
        title="Machine Learning Fundamentals",
        content=(
            "# Introduction to Machine Learning\n\n"
            "Machine learning is a subset of artificial intelligence that enables "
            "systems to learn and improve from experience without being explicitly programmed.\n\n"
            "## Key Concepts\n\n"
            "### Supervised Learning\n"
            "Uses labeled data to train models. Common algorithms include:\n"
            "- Linear Regression: Predicts continuous values\n"
            "- Decision Trees: Makes decisions based on feature values\n"
            "- Neural Networks: Mimics human brain structure\n\n"
            "### Unsupervised Learning\n"
            "Finds patterns in unlabeled data. Examples:\n"
            "- Clustering: Groups similar data points (K-means, DBSCAN)\n"
            "- Dimensionality Reduction: Reduces feature space (PCA, t-SNE)\n\n"
            "### Reinforcement Learning\n"
            "Agents learn through trial and error with rewards and penalties.\n\n"
            "## Applications\n\n"
            "| Domain | Use Case | Impact |\n"
            "|--------|----------|--------|\n"
            "| Healthcare | Disease diagnosis | 95% accuracy in some cancers |\n"
            "| Finance | Fraud detection | $20B saved annually |\n"
            "| Transportation | Autonomous vehicles | 40% fewer accidents |\n"
            "| Retail | Recommendation systems | 35% increase in sales |\n\n"
            "## Model Evaluation Metrics\n\n"
            "1. **Accuracy**: Correct predictions / Total predictions\n"
            "2. **Precision**: True positives / (True positives + False positives)\n"
            "3. **Recall**: True positives / (True positives + False negatives)\n"
            "4. **F1 Score**: Harmonic mean of precision and recall\n\n"
            "## Best Practices\n\n"
            "- Always split data into training, validation, and test sets\n"
            "- Use cross-validation to avoid overfitting\n"
            "- Normalize features for better model performance\n"
            "- Monitor for data drift in production systems\n"
        ),
    )

    # Delay to ensure source is processed
    await asyncio.sleep(SOURCE_PROCESSING_DELAY)

    return notebook.id


async def _cleanup_generation_notebook(client: NotebookLMClient, notebook_id: str) -> None:
    """Clean up existing artifacts and notes from generation notebook.

    This runs BEFORE tests to ensure a clean starting state.
    """
    # Delete all artifacts
    try:
        artifacts = await client.artifacts.list(notebook_id)
        for artifact in artifacts:
            try:
                await client.artifacts.delete(notebook_id, artifact.id)
            except Exception:
                pass  # Ignore individual delete failures
    except Exception:
        pass  # Ignore list failures

    # Delete all notes (except pinned system notes)
    try:
        notes = await client.notes.list(notebook_id)
        for note in notes:
            # Skip if no id or if it's a pinned system note
            if note.id and not getattr(note, "pinned", False):
                try:
                    await client.notes.delete(notebook_id, note.id)
                except Exception:
                    pass  # Ignore individual delete failures
    except Exception:
        pass  # Ignore list failures


def _is_ci_environment() -> bool:
    """Check if running in CI environment.

    Detects common CI systems: GitHub Actions, GitLab CI, CircleCI, Travis CI,
    Azure Pipelines, and others that set CI=true/1/yes.
    """
    ci_value = os.environ.get("CI", "").lower()
    return ci_value in ("true", "1", "yes")


def _delete_stored_generation_notebook_id() -> None:
    """Delete the stored generation notebook ID file."""
    path = _get_generation_notebook_id_path()
    if path.exists():
        try:
            path.unlink()
        except Exception:
            pass


async def _verify_notebook_exists(client, notebook_id: str) -> bool:
    """Verify a notebook exists and is accessible."""
    try:
        nb = await client.notebooks.get(notebook_id)
        return nb is not None
    except Exception:
        return False


@pytest.fixture
async def generation_notebook_id(client):
    """Get or create a notebook for generation tests.

    This fixture uses a hybrid approach:
    1. Check NOTEBOOKLM_GENERATION_NOTEBOOK_ID env var
    2. If not set, check for stored ID in NOTEBOOKLM_HOME/generation_notebook_id
    3. If not found, auto-create a notebook and store its ID

    All notebook IDs (env var or stored) are verified to exist before use.

    In CI environments (CI=true/1/yes), auto-created notebooks are deleted after tests.
    In local environments, the notebook persists across runs for verification.

    Artifacts and notes are cleaned up BEFORE tests to ensure clean state.
    Sources are NOT cleaned (generation tests need them).

    Use for: artifact generation tests (audio, video, quiz, etc.)
    Do NOT use for: CRUD tests (use temp_notebook instead)
    """
    auto_created = False
    source = None  # Track where notebook ID came from for debugging

    # Priority 1: Environment variable
    notebook_id = os.environ.get("NOTEBOOKLM_GENERATION_NOTEBOOK_ID")
    if notebook_id:
        source = "env var"

    # Priority 2: Stored ID file
    if not notebook_id:
        notebook_id = _load_stored_generation_notebook_id()
        if notebook_id:
            source = "stored file"

    # Verify notebook exists (for both env var AND stored IDs)
    if notebook_id:
        if not await _verify_notebook_exists(client, notebook_id):
            warnings.warn(
                f"Generation notebook {notebook_id} from {source} no longer exists, "
                "creating new one",
                stacklevel=2,
            )
            notebook_id = None

    # Priority 3: Auto-create
    if not notebook_id:
        notebook_id = await _create_generation_notebook(client)
        _save_generation_notebook_id(notebook_id)
        auto_created = True

    # Clean up artifacts and notes before tests (only once per session)
    global _generation_cleanup_done
    if not _generation_cleanup_done:
        await _cleanup_generation_notebook(client, notebook_id)
        _generation_cleanup_done = True

    yield notebook_id

    # Cleanup: In CI, delete auto-created notebooks to avoid orphans
    if auto_created and _is_ci_environment():
        # Delete stored file first (idempotent), then attempt notebook delete (best effort)
        _delete_stored_generation_notebook_id()
        try:
            await client.notebooks.delete(notebook_id)
        except Exception as e:
            warnings.warn(f"Failed to delete generation notebook {notebook_id}: {e}", stacklevel=2)


# =============================================================================
# Multi-Source Notebook Fixtures
# =============================================================================

# File to store auto-created multi-source notebook ID
MULTI_SOURCE_NOTEBOOK_ID_FILE = "multi_source_notebook_id"

# Module-level state to ensure cleanup only runs once per session
_multi_source_cleanup_done = False


def _get_multi_source_notebook_id_path() -> Path:
    """Get the path to the multi-source notebook ID file."""
    return get_home_dir() / MULTI_SOURCE_NOTEBOOK_ID_FILE


def _load_stored_multi_source_notebook_id() -> str | None:
    """Load multi-source notebook ID from stored file."""
    path = _get_multi_source_notebook_id_path()
    if path.exists():
        try:
            return path.read_text().strip()
        except Exception:
            return None
    return None


def _save_multi_source_notebook_id(notebook_id: str) -> None:
    """Save multi-source notebook ID to file for future runs."""
    path = _get_multi_source_notebook_id_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(notebook_id)


def _delete_stored_multi_source_notebook_id() -> None:
    """Delete the stored multi-source notebook ID file."""
    path = _get_multi_source_notebook_id_path()
    if path.exists():
        try:
            path.unlink()
        except Exception:
            pass


async def _create_multi_source_notebook(client: NotebookLMClient) -> str:
    """Create a notebook with multiple sources for testing source selection.

    Returns the notebook ID.
    """
    import asyncio
    from uuid import uuid4

    notebook = await client.notebooks.create(f"E2E-MultiSource-{uuid4().hex[:8]}")

    # Add 3 distinct text sources with different content
    sources_content = [
        (
            "Python Programming",
            (
                "# Python Programming Fundamentals\n\n"
                "Python is a high-level, interpreted programming language known for "
                "its clear syntax and readability. Created by Guido van Rossum in 1991.\n\n"
                "## Key Features\n"
                "- Dynamic typing\n"
                "- Automatic memory management\n"
                "- Extensive standard library\n"
                "- Multi-paradigm support\n\n"
                "## Data Types\n"
                "- int, float, complex for numbers\n"
                "- str for text\n"
                "- list, tuple, set, dict for collections\n"
            ),
        ),
        (
            "Machine Learning Basics",
            (
                "# Machine Learning Overview\n\n"
                "Machine learning enables computers to learn from data without "
                "explicit programming. It's a subset of artificial intelligence.\n\n"
                "## Types of ML\n"
                "- Supervised Learning: Uses labeled data\n"
                "- Unsupervised Learning: Finds patterns in unlabeled data\n"
                "- Reinforcement Learning: Learns through rewards\n\n"
                "## Common Algorithms\n"
                "- Linear Regression\n"
                "- Decision Trees\n"
                "- Neural Networks\n"
                "- K-Means Clustering\n"
            ),
        ),
        (
            "Web Development",
            (
                "# Web Development Essentials\n\n"
                "Web development involves creating websites and web applications.\n\n"
                "## Frontend Technologies\n"
                "- HTML: Structure\n"
                "- CSS: Styling\n"
                "- JavaScript: Interactivity\n\n"
                "## Backend Technologies\n"
                "- Node.js, Python, Ruby, Go\n"
                "- Databases: PostgreSQL, MongoDB\n"
                "- APIs: REST, GraphQL\n\n"
                "## Modern Frameworks\n"
                "- React, Vue, Angular for frontend\n"
                "- Django, FastAPI, Express for backend\n"
            ),
        ),
    ]

    for title, content in sources_content:
        await client.sources.add_text(notebook.id, title=title, content=content)

    # Delay to ensure all sources are processed
    await asyncio.sleep(SOURCE_PROCESSING_DELAY * 2)

    return notebook.id


async def _cleanup_multi_source_notebook(client: NotebookLMClient, notebook_id: str) -> None:
    """Clean up existing artifacts from multi-source notebook.

    This runs BEFORE tests to ensure a clean starting state.
    Sources are NOT cleaned (tests need them).
    """
    try:
        artifacts = await client.artifacts.list(notebook_id)
        for artifact in artifacts:
            try:
                await client.artifacts.delete(notebook_id, artifact.id)
            except Exception:
                pass
    except Exception:
        pass


@pytest.fixture
async def multi_source_notebook_id(client):
    """Get or create a notebook with multiple sources for source selection tests.

    This fixture uses a hybrid approach similar to generation_notebook_id:
    1. Check NOTEBOOKLM_MULTI_SOURCE_NOTEBOOK_ID env var
    2. If not set, check for stored ID
    3. If not found, auto-create a notebook with 3 sources

    All IDs are verified to exist before use.
    Artifacts are cleaned before tests. Sources are preserved.
    """
    auto_created = False
    source = None

    # Priority 1: Environment variable
    notebook_id = os.environ.get("NOTEBOOKLM_MULTI_SOURCE_NOTEBOOK_ID")
    if notebook_id:
        source = "env var"

    # Priority 2: Stored ID file
    if not notebook_id:
        notebook_id = _load_stored_multi_source_notebook_id()
        if notebook_id:
            source = "stored file"

    # Verify notebook exists
    if notebook_id:
        if not await _verify_notebook_exists(client, notebook_id):
            warnings.warn(
                f"Multi-source notebook {notebook_id} from {source} no longer exists, "
                "creating new one",
                stacklevel=2,
            )
            notebook_id = None

    # Priority 3: Auto-create
    if not notebook_id:
        notebook_id = await _create_multi_source_notebook(client)
        _save_multi_source_notebook_id(notebook_id)
        auto_created = True

    # Clean up artifacts before tests (only once per session)
    global _multi_source_cleanup_done
    if not _multi_source_cleanup_done:
        await _cleanup_multi_source_notebook(client, notebook_id)
        _multi_source_cleanup_done = True

    yield notebook_id

    # Cleanup: In CI, delete auto-created notebooks
    if auto_created and _is_ci_environment():
        _delete_stored_multi_source_notebook_id()
        try:
            await client.notebooks.delete(notebook_id)
        except Exception as e:
            warnings.warn(
                f"Failed to delete multi-source notebook {notebook_id}: {e}", stacklevel=2
            )
