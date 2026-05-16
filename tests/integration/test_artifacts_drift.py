"""Schema-drift tests for ``_parse_generation_result`` (PR).

These tests pin down the contract that PR establishes:

* ``_parse_generation_result`` accepts ``method_id`` as a keyword argument and
  threads it through ``safe_index`` so drift diagnostics know which RPC failed.
* Under the default soft-strict mode (``NOTEBOOKLM_STRICT_DECODE=0``) drift
  returns the legacy ``GenerationStatus(status="failed", task_id="")`` shape,
  preserving backward compatibility with callers that handle that sentinel.
* Under strict mode (``NOTEBOOKLM_STRICT_DECODE=1``) drift raises
  ``UnknownRPCMethodError`` carrying the supplied ``method_id`` so operators
  can detect that Google's response shape moved out from under us.

Real-shape happy-path coverage for the wire-level flow already exists in
``tests/integration/test_artifacts.py::TestParseGenerationResult`` (CREATE_ARTIFACT)
and elsewhere. Here we exercise the parser directly with constructed dicts
because the soft/strict mode toggle is a runtime concern that doesn't depend on
HTTP plumbing — a VCR cassette would only add ceremony without exercising the
new branch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc import RPCMethod

# mock-only drift tests; no HTTP, no cassette. Opt out of the
# tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


@pytest.fixture
def artifacts_api():
    """Build a minimal ArtifactsAPI for direct parser invocation."""
    mock_core = MagicMock()
    mock_core.rpc_call = AsyncMock()
    mock_notes = MagicMock()
    return ArtifactsAPI(mock_core, notes_api=mock_notes)


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
# Drift: soft mode (default) preserves legacy "failed" sentinel.
# ---------------------------------------------------------------------------


class TestParseGenerationResultSoftDrift:
    """Soft mode (``NOTEBOOKLM_STRICT_DECODE`` unset) returns legacy failure."""

    def test_none_result_returns_failed(self, artifacts_api, monkeypatch):
        monkeypatch.delenv("NOTEBOOKLM_STRICT_DECODE", raising=False)

        status = artifacts_api._parse_generation_result(
            None, method_id=RPCMethod.CREATE_ARTIFACT.value
        )

        assert status.status == "failed"
        assert status.task_id == ""
        assert status.error and "no artifact_id" in status.error.lower()

    def test_empty_list_returns_failed(self, artifacts_api, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "0")

        status = artifacts_api._parse_generation_result(
            [], method_id=RPCMethod.CREATE_ARTIFACT.value
        )

        assert status.status == "failed"
        assert status.task_id == ""

    def test_missing_inner_leaf_returns_failed(self, artifacts_api, monkeypatch):
        """Inner list is too short to expose [0][0] / [0][4] — soft mode swallows."""
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "0")

        # Inner list missing both task_id and status_code positions.
        status = artifacts_api._parse_generation_result(
            [[]], method_id=RPCMethod.REVISE_SLIDE.value
        )

        assert status.status == "failed"
        assert status.task_id == ""

    def test_status_code_missing_still_returns_pending(self, artifacts_api, monkeypatch):
        """Drift on the optional status_code leaf must NOT break a valid task_id.

        Under soft mode, ``safe_index`` returns ``None`` for the missing leaf,
        which the parser already handles by defaulting to ``"pending"``.
        """
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "0")

        # task_id present, status_code position absent.
        status = artifacts_api._parse_generation_result(
            [["task_short"]], method_id=RPCMethod.CREATE_ARTIFACT.value
        )

        assert status.task_id == "task_short"
        assert status.status == "pending"


# ---------------------------------------------------------------------------
# Drift: strict mode raises typed UnknownRPCMethodError.
# ---------------------------------------------------------------------------


class TestParseGenerationResultStrictDrift:
    """Strict mode (``NOTEBOOKLM_STRICT_DECODE=1``) raises typed errors."""

    def test_none_result_raises(self, artifacts_api, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")

        with pytest.raises(UnknownRPCMethodError) as exc_info:
            artifacts_api._parse_generation_result(None, method_id=RPCMethod.CREATE_ARTIFACT.value)

        err = exc_info.value
        assert err.method_id == RPCMethod.CREATE_ARTIFACT.value
        assert err.source == "_parse_generation_result"
        # Top-level descent: failing path is empty (we failed at the first index).
        assert err.path == ()

    def test_empty_list_raises_for_revise_slide(self, artifacts_api, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")

        with pytest.raises(UnknownRPCMethodError) as exc_info:
            artifacts_api._parse_generation_result([], method_id=RPCMethod.REVISE_SLIDE.value)

        err = exc_info.value
        assert err.method_id == RPCMethod.REVISE_SLIDE.value
        assert err.source == "_parse_generation_result"

    def test_inner_leaf_missing_raises(self, artifacts_api, monkeypatch):
        """Drift on the inner leaf reports a non-empty failing path."""
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")

        with pytest.raises(UnknownRPCMethodError) as exc_info:
            artifacts_api._parse_generation_result([[]], method_id=RPCMethod.CREATE_ARTIFACT.value)

        err = exc_info.value
        assert err.method_id == RPCMethod.CREATE_ARTIFACT.value
        # We descended into result[0], then failed on result[0][0].
        assert err.path == (0,)

    def test_status_code_missing_raises_in_strict_mode(self, artifacts_api, monkeypatch):
        """task_id present but status_code position absent must raise in strict mode.

        ``status_code`` is treated as a required leaf: in every captured real
        response it sits at ``result[0][4]``. If Google starts shipping a
        truncated shape like ``[["task_short"]]`` (task_id only, no
        status_code), we want to learn about it via a typed drift exception
        rather than silently falling back to ``"pending"``.
        """
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")

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
