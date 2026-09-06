"""Tests for the pure IMPORT_RESEARCH helpers added for #2187.

``_import_research_read_timeout`` and ``_is_import_research_failed_precondition``
live in ``_research_import.py``. Behavioral
coverage of how ``WebResearchAPI.import_sources_with_verification``
actually uses them (retry/raise/log decisions) lives in
``test_research_import_with_verification.py``; these tests cover the pure
functions in isolation.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from notebooklm._research_import import (
    _ANDROID_RESEARCH_IMPORT_POLICY,
    _WEB_RESEARCH_IMPORT_POLICY,
    _classify_research_import,
    _coerce_research_sources,
    _import_research_read_timeout,
    _imported_result,
    _is_import_research_failed_precondition,
    _validate_import_task_id,
)
from notebooklm._runtime.config import (
    DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT,
)
from notebooklm._types.research import ResearchSource
from notebooklm.exceptions import RPCError, ValidationError


def test_neutral_helper_module_owns_the_base_compatibility_seams() -> None:
    import notebooklm._research as research_base
    import notebooklm._research_import as research_import

    assert research_base._imported_result is research_import._imported_result
    assert (
        research_base._normalize_import_verification_url
        is research_import._normalize_import_verification_url
    )

    result = _imported_result([], [{"id": "existing", "title": "A", "url": "https://a"}])
    assert type(result).__module__ == "notebooklm._research_import"
    assert result.already_present == [{"id": "existing", "title": "A", "url": "https://a"}]


def test_neutral_source_coercion_preserves_order_and_typed_identity() -> None:
    typed = ResearchSource(url="https://a", title="A")

    assert _coerce_research_sources([typed, {"url": "https://b", "title": "B"}]) == [
        typed,
        ResearchSource(url="https://b", title="B"),
    ]


def test_import_policies_are_immutable_and_preserve_task_id_validation() -> None:
    assert _validate_import_task_id("opaque-task", _WEB_RESEARCH_IMPORT_POLICY) == "opaque-task"
    with pytest.raises(ValidationError, match="canonical UUID"):
        _validate_import_task_id("opaque-task", _ANDROID_RESEARCH_IMPORT_POLICY)
    with pytest.raises(FrozenInstanceError):
        _WEB_RESEARCH_IMPORT_POLICY.reports_first = False  # type: ignore[misc]


def test_import_classification_preserves_backend_report_order() -> None:
    inputs = [
        {"url": "https://example.com", "title": "Web"},
        {"title": "Report", "result_type": 5, "report_markdown": "# Report"},
    ]
    models = _coerce_research_sources(inputs)

    web = _classify_research_import(
        inputs,
        models,
        task_id="opaque-task",
        policy=_WEB_RESEARCH_IMPORT_POLICY,
    )
    android = _classify_research_import(
        inputs,
        models,
        task_id="00000000-0000-0000-0000-000000000001",
        policy=_ANDROID_RESEARCH_IMPORT_POLICY,
    )

    assert [item.kind for item in web.items] == ["report", "web"]
    assert [item.kind for item in android.items] == ["web", "report"]
    assert [item.source_input for item in android.items] == inputs


def test_import_classification_preserves_public_report_title_difference() -> None:
    inputs = [{"result_type": 5, "report_markdown": "# Report"}]
    models = _coerce_research_sources(inputs)

    web = _classify_research_import(
        inputs,
        models,
        task_id="opaque-task",
        policy=_WEB_RESEARCH_IMPORT_POLICY,
    )
    android = _classify_research_import(
        inputs,
        models,
        task_id="00000000-0000-0000-0000-000000000001",
        policy=_ANDROID_RESEARCH_IMPORT_POLICY,
    )

    assert web.items == ()
    assert web.skipped_count == 1
    assert [(item.kind, item.source.title) for item in android.items] == [("report", "Untitled")]


class TestImportResearchReadTimeout:
    def test_zero_sources_returns_base_floor(self):
        assert _import_research_read_timeout(0) == DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT

    def test_scales_linearly_with_source_count(self):
        # 3 sources: base + 3 * per-source increment, well under the ceiling.
        expected = (
            DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT + 3 * DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT
        )
        assert _import_research_read_timeout(3) == expected

    def test_notebook_source_cap_stays_under_ceiling(self):
        # 50 is the NotebookLM per-notebook source cap (#1919/#1926) — the
        # realistic worst case for one IMPORT_RESEARCH batch.
        result = _import_research_read_timeout(50)
        assert (
            result
            == DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT
            + 50 * DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT
        )
        assert result <= DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT

    def test_clamps_at_max_timeout_for_pathological_batch(self):
        assert _import_research_read_timeout(1000) == DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT


class TestIsImportResearchFailedPrecondition:
    def test_true_for_grpc_failed_precondition_code(self):
        exc = RPCError("The server rejected this request (failed precondition).", rpc_code=9)
        assert _is_import_research_failed_precondition(exc) is True

    def test_true_for_string_encoded_code(self):
        # ``rpc_code`` is typed ``str | int | None`` on the wire path.
        exc = RPCError("failed precondition", rpc_code="9")
        assert _is_import_research_failed_precondition(exc) is True

    @pytest.mark.parametrize("rpc_code", [16, 5, "USER_DISPLAYABLE_ERROR", None])
    def test_false_for_any_other_code(self, rpc_code):
        exc = RPCError("some other failure", rpc_code=rpc_code)
        assert _is_import_research_failed_precondition(exc) is False
