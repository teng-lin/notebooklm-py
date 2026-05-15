"""Empty-result VCR cassettes — parser edge cases for empty state (T8.E5).

Three cassettes exercise the empty-collection code paths in:

* :mod:`notebooklm._sources` — ``SourcesAPI.list`` on a brand-new notebook
  with zero sources must return ``[]``, not ``None`` and not raise.
* :mod:`notebooklm._artifacts` — ``ArtifactsAPI.list`` on a notebook with no
  generated artifacts must return ``[]`` and must not propagate any error
  from the mind-map sidecar.
* :mod:`notebooklm._research` — ``ResearchAPI.poll`` against a notebook that
  has never started research must return the well-formed sentinel dict
  ``{"status": "no_research", "tasks": []}`` (the empty-state contract
  documented on ``poll``'s docstring).

Empty results are a VALID state, not an error condition. Each test asserts
the parser yields an empty collection (the exact empty type the API returns,
NOT ``None`` and NOT an exception) so future drift toward "treat empty as
error" is caught at replay time.

Recording (LIVE):

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \\
        tests/integration/test_empty_results_vcr.py -v -s

Recording creates an ephemeral scratch notebook, runs the three reads
against it, then deletes the notebook. The notebook ID is therefore
incidental to replay — the cassettes are matched by RPC method + body,
not by notebook UUID.
"""

import sys
from pathlib import Path

import pytest

# tests/ is not a package (no __init__.py); insert both dirs onto sys.path so
# the ``conftest`` and ``vcr_config`` siblings resolve in any invocation mode
# (direct pytest, ``-k`` filter, IDE runner).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from conftest import get_vcr_auth, skip_no_cassettes  # noqa: E402
from notebooklm import NotebookLMClient  # noqa: E402
from vcr_config import notebooklm_vcr  # noqa: E402

# Skip all tests in this module if cassettes are not available (mirrors the
# pattern in tests/integration/test_vcr_comprehensive.py).
pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestEmptyResults:
    """Replay tests for empty-state RPC responses.

    All three tests share the same shape: open a VCR-decorated client, call
    one collection-returning API, and assert the returned value is an empty
    collection of the documented type. No notebook ID needs to be passed —
    during replay VCR ignores the URL slug; during recording the orchestrator
    passes a freshly-created scratch notebook ID (see this module's docstring).
    """

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebook_zero_sources.yaml")
    async def test_zero_sources_returns_empty_list(self):
        """``SourcesAPI.list`` on a sources-less notebook returns ``[]``.

        Exercises the empty-state path through :func:`SourcesAPI.list`.
        Observed during recording: ``GET_NOTEBOOK`` against a brand-new
        notebook elides the sources slot (returns ``None`` instead of an
        empty list), which the parser folds into ``[]`` via the non-strict
        ``_handle_malformed_list_response`` branch — so a fresh notebook's
        empty-source state is the SAME observable as a structurally
        malformed response, and both must yield ``[]``. The default
        ``strict=False`` path is the relevant contract for empty-state
        callers; ``strict=True`` is exercised elsewhere.
        """
        notebook_id = _get_scratch_notebook_id()
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            sources = await client.sources.list(notebook_id)

        # Empty list is a VALID state — not None, not an exception.
        assert sources == []
        assert isinstance(sources, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_list_empty.yaml")
    async def test_empty_artifacts_returns_empty_list(self):
        """``ArtifactsAPI.list`` on a notebook with zero artifacts returns ``[]``.

        Covers both wings of the unified ``list`` implementation: the studio
        ``LIST_ARTIFACTS`` RPC (audio/video/reports/quizzes/...) AND the
        mind-map sidecar via :func:`notebooklm._artifacts._mind_map.list_mind_maps`.
        A brand-new notebook has neither, so the merged return must be ``[]``.
        """
        notebook_id = _get_scratch_notebook_id()
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            artifacts = await client.artifacts.list(notebook_id)

        assert artifacts == []
        assert isinstance(artifacts, list)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("research_poll_empty.yaml")
    async def test_research_poll_no_tasks_returns_sentinel(self):
        """``ResearchAPI.poll`` with no research in flight returns the empty sentinel.

        The empty-poll contract is documented on :meth:`ResearchAPI.poll`:
        when ``POLL_RESEARCH`` returns an empty envelope, the API surface
        yields ``{"status": "no_research", "tasks": []}`` — a stable dict
        rather than ``None`` or an exception, so callers can poll in a loop
        without special-casing "haven't started yet".
        """
        notebook_id = _get_scratch_notebook_id()
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            result = await client.research.poll(notebook_id)

        # Empty-state sentinel, not None and not an exception.
        assert isinstance(result, dict)
        assert result["status"] == "no_research"
        assert result["tasks"] == []


# =============================================================================
# Scratch notebook plumbing
# =============================================================================


def _get_scratch_notebook_id() -> str:
    """Return the notebook ID to pass through the three API calls.

    During RECORDING the test orchestrator sets
    ``NOTEBOOKLM_EMPTY_SCRATCH_NOTEBOOK_ID`` to the freshly-minted scratch
    notebook UUID (see this module's docstring). During REPLAY no scratch
    notebook exists — VCR replays the recorded responses regardless of
    notebook ID, so we just return a placeholder UUID that lets the request
    builder run without raising.
    """
    import os

    return os.environ.get(
        "NOTEBOOKLM_EMPTY_SCRATCH_NOTEBOOK_ID",
        "00000000-0000-0000-0000-000000000000",
    )
