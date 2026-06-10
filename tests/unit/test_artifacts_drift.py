"""Schema-drift tests for ``_parse_generation_result``.

These tests pin down the strict-decoding contract:

* ``_parse_generation_result`` accepts ``method_id`` as a keyword argument and
  threads it through ``safe_index`` so drift diagnostics know which RPC failed.
* Drift raises ``UnknownRPCMethodError`` carrying the supplied ``method_id``
  so operators can detect that Google's response shape moved out from under us.
  Strict decoding is the only mode — the legacy
  ``NOTEBOOKLM_STRICT_DECODE=0`` soft-mode opt-out (which returned the
  ``GenerationStatus(status="failed", task_id="")`` sentinel) was retired in
  v0.7.0; see ADR-0011.

Real-shape happy-path coverage for the wire-level flow already exists in
``tests/integration/test_artifacts_integration.py::TestParseGenerationResult``
and elsewhere. Here we exercise the parser directly with constructed dicts
because the drift branch is a runtime concern that doesn't depend on HTTP
plumbing — a VCR cassette would only add ceremony without exercising it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc import RPCMethod


@pytest.fixture
def artifacts_api():
    """Build a minimal ArtifactsAPI for direct parser invocation."""
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService
    from tests._fixtures.fake_core import make_fake_core

    mock_core = make_fake_core(rpc_call=AsyncMock())
    return ArtifactsAPI(
        rpc=mock_core,
        drain=mock_core,
        lifecycle=mock_core,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )


# ---------------------------------------------------------------------------
# Happy-path: real response shape parses correctly for both call sites.
# ---------------------------------------------------------------------------


class TestParseGenerationResultHappyPath:
    """Real response shape parses successfully when ``method_id`` is supplied."""

    def test_create_artifact_real_shape(self, artifacts_api):
        """CREATE_ARTIFACT response: [[task_id, title, type_code, None, status]]."""
        result = [["task_abc", "Audio Overview", 1, None, 1]]

        status = artifacts_api._parse_generation_result(
            result, method_id=RPCMethod.CREATE_ARTIFACT.value
        )

        assert status.task_id == "task_abc"
        assert status.status == "in_progress"
        assert status.error is None

    def test_revise_slide_real_shape(self, artifacts_api):
        """REVISE_SLIDE shares the response shape with CREATE_ARTIFACT."""
        result = [["revised_xyz", "Slide Deck", 8, None, 3]]

        status = artifacts_api._parse_generation_result(
            result, method_id=RPCMethod.REVISE_SLIDE.value
        )

        assert status.task_id == "revised_xyz"
        assert status.status == "completed"
        assert status.error is None


# ---------------------------------------------------------------------------
# Drift: strict decoding raises typed UnknownRPCMethodError (the only mode).
# ---------------------------------------------------------------------------


class TestParseGenerationResultStrictDrift:
    """Strict decoding raises typed errors on drift (the only mode)."""

    def test_none_result_raises(self, artifacts_api):
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            artifacts_api._parse_generation_result(None, method_id=RPCMethod.CREATE_ARTIFACT.value)

        err = exc_info.value
        assert err.method_id == RPCMethod.CREATE_ARTIFACT.value
        assert err.source == "_parse_generation_result"
        # Top-level descent: failing path is empty (we failed at the first index).
        assert err.path == ()

    def test_none_result_raises_for_revise_slide(self, artifacts_api):
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            artifacts_api._parse_generation_result(None, method_id=RPCMethod.REVISE_SLIDE.value)

        err = exc_info.value
        assert err.method_id == RPCMethod.REVISE_SLIDE.value
        assert err.source == "_parse_generation_result"
        assert err.path == ()

    def test_empty_list_raises_for_revise_slide(self, artifacts_api):
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            artifacts_api._parse_generation_result([], method_id=RPCMethod.REVISE_SLIDE.value)

        err = exc_info.value
        assert err.method_id == RPCMethod.REVISE_SLIDE.value
        assert err.source == "_parse_generation_result"

    def test_inner_leaf_missing_raises(self, artifacts_api):
        """Drift on the inner leaf reports a non-empty failing path."""
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            artifacts_api._parse_generation_result([[]], method_id=RPCMethod.CREATE_ARTIFACT.value)

        err = exc_info.value
        assert err.method_id == RPCMethod.CREATE_ARTIFACT.value
        # We descended into result[0], then failed on result[0][0].
        assert err.path == (0,)

    def test_status_code_missing_raises(self, artifacts_api):
        """task_id present but status_code position absent must raise.

        ``status_code`` is treated as a required leaf: in every captured real
        response it sits at ``result[0][4]``. If Google starts shipping a
        truncated shape like ``[["task_short"]]`` (task_id only, no
        status_code), we want to learn about it via a typed drift exception
        rather than silently falling back to ``"pending"``.
        """
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            artifacts_api._parse_generation_result(
                [["task_short"]], method_id=RPCMethod.CREATE_ARTIFACT.value
            )

        err = exc_info.value
        assert err.method_id == RPCMethod.CREATE_ARTIFACT.value
        assert err.source == "_parse_generation_result"
        # We descended into result[0] (a list of length 1), then failed on
        # result[0][4] — so the failing path stops at (0,).
        assert err.path == (0,)
