"""E2E test fixtures and configuration."""

import os
import warnings
import pytest
import httpx
from typing import AsyncGenerator

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

# Delay constants for rate limiting and polling
RATE_LIMIT_DELAY = 3.0  # Delay after tests to avoid rate limits
SOURCE_PROCESSING_DELAY = 2.0  # Delay for source processing
POLL_INTERVAL = 2.0  # Interval between poll attempts
POLL_TIMEOUT = 60.0  # Max time to wait for operations


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
# Auth Fixtures (session-scoped for efficiency)
# =============================================================================


@pytest.fixture(scope="session")
def auth_cookies() -> dict[str, str]:
    """Load auth cookies from storage (session-scoped)."""
    return load_auth_from_storage()


@pytest.fixture(scope="session")
async def auth_tokens(auth_cookies) -> AuthTokens:
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


@pytest.fixture
async def client(auth_tokens) -> AsyncGenerator[NotebookLMClient, None]:
    async with NotebookLMClient(auth_tokens) as c:
        yield c


@pytest.fixture
def test_notebook_id(golden_notebook_id):
    """Get notebook ID from env var or use golden notebook.

    Uses NOTEBOOKLM_TEST_NOTEBOOK_ID if set, otherwise falls back
    to the golden notebook (Google's shared demo notebook).
    """
    return os.environ.get("NOTEBOOKLM_TEST_NOTEBOOK_ID", golden_notebook_id)


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
# Golden Notebook Fixtures (for read-only and mutation tests)
# =============================================================================


# Google's shared demo notebook - stable, pre-seeded with content
DEFAULT_GOLDEN_NOTEBOOK_ID = "19bde485-a9c1-4809-8884-e872b2b67b44"


@pytest.fixture(scope="session")
def golden_notebook_id():
    """Get golden notebook ID.

    Defaults to Google's shared demo notebook which has pre-seeded:
    - Sources: Various content types
    - Artifacts: Audio, Video, Quiz, Flashcards, Slide Deck, Mind Map

    Override with NOTEBOOKLM_GOLDEN_NOTEBOOK_ID env var if needed.
    """
    return os.environ.get("NOTEBOOKLM_GOLDEN_NOTEBOOK_ID", DEFAULT_GOLDEN_NOTEBOOK_ID)


@pytest.fixture(scope="session")
async def golden_client(auth_tokens) -> AsyncGenerator[NotebookLMClient, None]:
    """Session-scoped client for golden notebook tests.

    Use this for read-only tests that don't modify state.
    """
    async with NotebookLMClient(auth_tokens) as c:
        yield c


@pytest.fixture
async def temp_notebook(client, created_notebooks, cleanup_notebooks):
    """Create a temporary notebook that auto-deletes after test.

    Use for CRUD tests that need isolated state.
    """
    from uuid import uuid4
    notebook = await client.notebooks.create(f"Test-{uuid4().hex[:8]}")
    created_notebooks.append(notebook.id)
    return notebook


@pytest.fixture(scope="session")
async def generation_notebook(auth_tokens) -> AsyncGenerator:
    """Session-scoped notebook for slow generation tests.

    Created once per test session with a source added.
    Cleaned up at session end.
    """
    import asyncio
    from uuid import uuid4

    async with NotebookLMClient(auth_tokens) as client:
        notebook = await client.notebooks.create(f"GenTest-{uuid4().hex[:8]}")
        # Add a source so generation works
        await client.sources.add_text(
            notebook.id,
            "This is test content for artifact generation. "
            "It contains enough text to generate various artifacts like "
            "audio overviews, quizzes, and summaries."
        )
        await asyncio.sleep(SOURCE_PROCESSING_DELAY)
        yield notebook
        # Cleanup
        try:
            await client.notebooks.delete(notebook.id)
        except Exception as e:
            warnings.warn(f"Failed to cleanup generation_notebook {notebook.id}: {e}")


# =============================================================================
# Test Infrastructure Fixtures (for tiered testing)
# =============================================================================


@pytest.fixture(scope="session")
async def test_workspace(auth_tokens) -> AsyncGenerator:
    """Session-scoped workspace notebook for all E2E tests.

    Creates a single notebook at session start with test content.
    All tests can share this workspace to avoid creating/deleting
    notebooks repeatedly. Cleaned up at session end.

    Note: Tests using this fixture MUST NOT modify the workspace state
    (sources, settings) as it's shared across all tests.
    """
    import asyncio
    from uuid import uuid4

    async with NotebookLMClient(auth_tokens) as client:
        notebook = await client.notebooks.create(f"E2E-Workspace-{uuid4().hex[:8]}")

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

        yield notebook

        # Cleanup at session end
        try:
            await client.notebooks.delete(notebook.id)
        except Exception as e:
            warnings.warn(f"Failed to cleanup test_workspace {notebook.id}: {e}")


@pytest.fixture
async def rate_limit_aware() -> AsyncGenerator[None, None]:
    """Add delay after test to avoid rate limiting.

    Use this fixture in generation tests to add breathing room
    between API calls. Yields before test, sleeps after.
    """
    import asyncio

    yield  # Run the test

    # Add delay after test to avoid rate limits
    await asyncio.sleep(RATE_LIMIT_DELAY)
