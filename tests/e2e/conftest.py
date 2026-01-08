"""E2E test fixtures and configuration."""

import os
import warnings
import pytest
import httpx
from typing import AsyncGenerator

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, rely on shell environment

from notebooklm.auth import (
    load_auth_from_storage,
    extract_csrf_from_html,
    extract_session_id_from_html,
    DEFAULT_STORAGE_PATH,
    AuthTokens,
)
from notebooklm import NotebookLMClient


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
    reason=f"Requires authentication at {DEFAULT_STORAGE_PATH}",
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
def test_notebook_id():
    """Get notebook ID from NOTEBOOKLM_TEST_NOTEBOOK_ID env var.

    This env var is REQUIRED for E2E tests. You must create your own
    test notebook with sources and artifacts. See docs/contributing/testing.md.
    """
    notebook_id = os.environ.get("NOTEBOOKLM_TEST_NOTEBOOK_ID")
    if not notebook_id:
        pytest.exit(
            "\n\nERROR: NOTEBOOKLM_TEST_NOTEBOOK_ID environment variable is not set.\n\n"
            "E2E tests require YOUR OWN test notebook with content.\n\n"
            "Setup instructions:\n"
            "  1. Create a notebook at https://notebooklm.google.com\n"
            "  2. Add sources (text, URL, PDF, etc.)\n"
            "  3. Generate some artifacts (audio, quiz, etc.)\n"
            "  4. Copy notebook ID from URL and run:\n"
            "     export NOTEBOOKLM_TEST_NOTEBOOK_ID='your-notebook-id'\n\n"
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
                    warnings.warn(f"Failed to cleanup notebook {nb_id}: {e}")


@pytest.fixture
def created_sources():
    sources = []
    yield sources


@pytest.fixture
async def cleanup_sources(created_sources, test_notebook_id, auth_tokens):
    """Cleanup created sources after test."""
    yield
    if created_sources:
        async with NotebookLMClient(auth_tokens) as client:
            for src_id in created_sources:
                try:
                    await client.sources.delete(test_notebook_id, src_id)
                except Exception as e:
                    warnings.warn(f"Failed to cleanup source {src_id}: {e}")


@pytest.fixture
def created_artifacts():
    artifacts = []
    yield artifacts


@pytest.fixture
async def cleanup_artifacts(created_artifacts, test_notebook_id, auth_tokens):
    """Cleanup created artifacts after test."""
    yield
    if created_artifacts:
        async with NotebookLMClient(auth_tokens) as client:
            for art_id in created_artifacts:
                try:
                    await client.artifacts.delete(test_notebook_id, art_id)
                except Exception as e:
                    warnings.warn(f"Failed to cleanup artifact {art_id}: {e}")


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
# Test Infrastructure Fixtures (for tiered testing)
# =============================================================================


@pytest.fixture
async def generation_notebook(client, created_notebooks, cleanup_notebooks):
    """Notebook with content for generation tests.

    Creates a notebook with test content for artifact generation.
    Uses function scope to work with pytest-asyncio's default event loop scope.
    Automatically cleaned up after each test via cleanup_notebooks fixture.

    Use for: artifact generation (audio, video, quiz, etc.)
    Do NOT use for: CRUD tests (use temp_notebook instead)
    """
    import asyncio
    from uuid import uuid4

    notebook = await client.notebooks.create(f"GenTest-{uuid4().hex[:8]}")
    created_notebooks.append(notebook.id)

    # Add a text source so the notebook has content for operations
    await client.sources.add_text(
        notebook.id,
        title="Test Content",
        content=(
            "This is comprehensive test content for E2E testing. "
            "It covers various topics including artificial intelligence, "
            "machine learning, data science, and software engineering. "
            "The content is designed to be substantial enough for "
            "generating artifacts like audio overviews, quizzes, "
            "flashcards, reports, and other NotebookLM features."
        ),
    )

    # Delay to ensure source is processed
    await asyncio.sleep(SOURCE_PROCESSING_DELAY)

    return notebook


