"""Tests for the typed research/mind-map/guide returns (attribute-only).

Covers issue #1209 (typed returns) and issue #1251 (the dict-subscript
back-compat bridge dropped in v0.8.0):

* ``ResearchStatus`` str-enum comparisons.
* Typed attribute access on the new return dataclasses.
* The dataclasses are pure attribute-only frozen dataclasses: subscript raises
  :class:`TypeError`; ``get``/``keys``/``items``/``values`` raise
  :class:`AttributeError`; ``in``/``iter``/``len`` raise :class:`TypeError`.
* The library never self-warns on its own internal attribute access.
"""

from __future__ import annotations

import warnings

import pytest

from notebooklm import (
    MindMapResult,
    ResearchSource,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
    SourceGuide,
)


class TestResearchStatusEnum:
    def test_str_enum_compares_to_legacy_strings(self):
        assert ResearchStatus.IN_PROGRESS == "in_progress"
        assert ResearchStatus.COMPLETED == "completed"
        assert ResearchStatus.FAILED == "failed"
        assert ResearchStatus.NO_RESEARCH == "no_research"
        assert ResearchStatus.NOT_FOUND == "not_found"

    def test_not_found_is_distinct_from_no_research(self):
        # The poll-observed absence of a specific task (NOT_FOUND) is a
        # different lifecycle state from "nothing in flight" (NO_RESEARCH).
        assert ResearchStatus.NOT_FOUND is not ResearchStatus.NO_RESEARCH
        assert ResearchStatus.NOT_FOUND.value != ResearchStatus.NO_RESEARCH.value

    def test_is_a_str_subclass(self):
        assert isinstance(ResearchStatus.COMPLETED, str)

    def test_membership_in_string_tuple(self):
        # The pattern internal code uses: ``status in ("completed", "failed")``.
        assert ResearchStatus.COMPLETED in ("completed", "failed")
        assert ResearchStatus.IN_PROGRESS not in ("completed", "failed")

    def test_str_renders_value(self):
        assert str(ResearchStatus.COMPLETED) == "completed"


class TestTypedAttributeAccess:
    def test_source_guide_attributes(self):
        # A list is accepted for ergonomics but stored as an immutable tuple.
        guide = SourceGuide(summary="hi", keywords=["a", "b"])
        assert guide.summary == "hi"
        assert guide.keywords == ("a", "b")
        assert isinstance(guide.keywords, tuple)
        # The legacy dict shape keeps keywords as a list.
        assert guide.to_public_dict()["keywords"] == ["a", "b"]

    def test_mind_map_result_attributes(self):
        result = MindMapResult(mind_map={"name": "Root"}, note_id="note_1")
        assert result.mind_map == {"name": "Root"}
        assert result.note_id == "note_1"

    def test_research_start_attributes(self):
        start = ResearchStart(
            task_id="t1", report_id="r1", notebook_id="nb", query="q", mode="deep"
        )
        assert start.task_id == "t1"
        assert start.report_id == "r1"
        assert start.mode == "deep"

    def test_research_task_attributes(self):
        src = ResearchSource(url="http://x", title="T", result_type=1)
        task = ResearchTask(
            task_id="t1",
            status=ResearchStatus.COMPLETED,
            query="q",
            sources=(src,),
            summary="s",
            report="r",
        )
        assert task.task_id == "t1"
        assert task.status == "completed"
        assert task.sources[0].url == "http://x"
        assert task.summary == "s"

    def test_research_task_empty_sentinel(self):
        empty = ResearchTask.empty()
        assert empty.status == ResearchStatus.NO_RESEARCH
        assert empty.tasks == ()
        assert empty.to_public_dict() == {"status": "no_research", "tasks": []}

    def test_research_task_not_found_sentinel(self):
        # The pinned-but-absent placeholder carries the requested task id and
        # the NOT_FOUND status; its dict shape uses the per-task layout (since
        # task_id is set), keeping it distinct from the no_research shape.
        not_found = ResearchTask.not_found("task_missing")
        assert not_found.status == ResearchStatus.NOT_FOUND
        assert not_found.task_id == "task_missing"
        assert not_found.tasks == ()
        assert not_found.to_public_dict() == {
            "task_id": "task_missing",
            "status": "not_found",
            "query": "",
            "sources": [],
            "summary": "",
            "report": "",
            "tasks": [],
        }


class TestAttributeOnlyNoDictAccess:
    """The dict-subscript bridge was dropped in v0.8.0 (#1251).

    Each typed return is now a pure attribute-only frozen dataclass: subscript
    raises :class:`TypeError`; the method-style mapping shims
    (``get``/``keys``/``items``/``values``) raise :class:`AttributeError`; and
    ``in``/``iter``/``len`` raise :class:`TypeError`. No ``DeprecationWarning``
    is emitted on any of these paths — they are plain attribute-error / type-
    error failures, exactly as a bare dataclass produces.
    """

    @pytest.mark.parametrize(
        "obj",
        [
            SourceGuide(summary="hi", keywords=["a", "b"]),
            MindMapResult(mind_map={"name": "Root"}, note_id="n1"),
            ResearchStart(task_id="t1", report_id=None, notebook_id="nb", query="q", mode="fast"),
            ResearchTask(task_id="t1", status=ResearchStatus.COMPLETED),
            ResearchSource(url="http://x", title="T", result_type=1),
        ],
    )
    def test_subscript_raises_typeerror_not_subscriptable(self, obj):
        with pytest.raises(TypeError, match="not subscriptable"):
            obj["summary"]  # type: ignore[index]

    def test_subscript_never_warns(self):
        # The dropped bridge no longer emits a DeprecationWarning; the failure
        # is a plain TypeError (a warning would fail under simplefilter("error")).
        guide = SourceGuide(summary="hi", keywords=["a"])
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with pytest.raises(TypeError):
                guide["summary"]  # type: ignore[index]

    def test_method_style_shims_raise_attributeerror(self):
        guide = SourceGuide(summary="hi", keywords=["a", "b"])
        for name in ("get", "keys", "items", "values"):
            with pytest.raises(AttributeError):
                getattr(guide, name)

    def test_membership_iter_and_len_raise_typeerror(self):
        guide = SourceGuide(summary="hi", keywords=["a", "b"])
        with pytest.raises(TypeError):
            "summary" in guide  # type: ignore[operator]  # noqa: B015
        with pytest.raises(TypeError):
            iter(guide)  # type: ignore[call-overload]
        with pytest.raises(TypeError):
            len(guide)  # type: ignore[arg-type]

    def test_to_public_dict_still_works(self):
        # to_public_dict() is NOT part of the dropped bridge — it survives and
        # builds the historical JSON shape for CLI output.
        guide = SourceGuide(summary="hi", keywords=["a", "b"])
        assert guide.to_public_dict() == {"summary": "hi", "keywords": ["a", "b"]}
        result = MindMapResult(mind_map={"name": "Root"}, note_id="n1")
        assert result.to_public_dict() == {"mind_map": {"name": "Root"}, "note_id": "n1"}
        start = ResearchStart(
            task_id="t1", report_id=None, notebook_id="nb", query="q", mode="fast"
        )
        assert start.to_public_dict()["task_id"] == "t1"


class TestNoInternalSelfWarn:
    """The library must use attribute access internally — never subscript.

    Run representative internal flows with ``DeprecationWarning`` promoted to an
    error: any self-inflicted dict-subscript warning fails the test.
    """

    @pytest.mark.asyncio
    async def test_poll_does_not_self_warn(self):
        from notebooklm._research import ResearchAPI

        class _Rpc:
            async def rpc_call(self, *a, **k):
                # Empty POLL_RESEARCH envelope -> ResearchTask.empty().
                return []

        api = ResearchAPI(_Rpc())
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await api.poll("nb_1")
        assert result.status == "no_research"

    @pytest.mark.asyncio
    async def test_get_guide_service_does_not_self_warn(self):
        from notebooklm._source.content import SourceContentRenderer

        class _Rpc:
            async def rpc_call(self, *a, **k):
                return [[[None, ["A summary"], [["kw1", "kw2"]], []]]]

        renderer = SourceContentRenderer(_Rpc())
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            guide = await renderer.get_guide("nb_1", "src_1")
        assert guide.summary == "A summary"
        assert guide.keywords == ("kw1", "kw2")
