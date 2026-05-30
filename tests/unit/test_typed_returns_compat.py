"""Tests for the typed research/mind-map/guide returns + dict-compat bridge.

Covers issue #1209:

* ``ResearchStatus`` str-enum comparisons.
* Typed attribute access on the new return dataclasses.
* The :class:`MappingCompatMixin` dict-subscript backward-compat bridge
  (subscript warns; ``get``/``keys``/``in`` stay silent; legacy nested dict
  shape preserved).
* ``NOTEBOOKLM_QUIET_DEPRECATIONS`` suppression.
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
from notebooklm._deprecation import DEFAULT_REMOVAL


class TestResearchStatusEnum:
    def test_str_enum_compares_to_legacy_strings(self):
        assert ResearchStatus.IN_PROGRESS == "in_progress"
        assert ResearchStatus.COMPLETED == "completed"
        assert ResearchStatus.FAILED == "failed"
        assert ResearchStatus.NO_RESEARCH == "no_research"

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


class TestDictSubscriptCompat:
    def test_subscript_warns_and_returns_legacy_value(self):
        guide = SourceGuide(summary="hi", keywords=["a"])
        with pytest.warns(DeprecationWarning) as record:
            value = guide["summary"]
        assert value == "hi"
        msg = str(record[0].message)
        assert "dict-style access is deprecated" in msg
        assert f"v{DEFAULT_REMOVAL}" in msg
        assert ".summary" in msg
        assert "NOTEBOOKLM_QUIET_DEPRECATIONS" in msg

    def test_unknown_key_raises_keyerror(self):
        guide = SourceGuide(summary="hi", keywords=[])
        with pytest.raises(KeyError), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _ = guide["does_not_exist"]

    def test_read_mapping_surface_is_silent(self):
        guide = SourceGuide(summary="hi", keywords=["a", "b"])
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning fails the test
            assert guide.get("summary") == "hi"
            assert guide.get("missing", "default") == "default"
            assert "summary" in guide
            assert set(guide.keys()) == {"summary", "keywords"}
            assert list(guide.items()) == [("summary", "hi"), ("keywords", ["a", "b"])]
            assert list(guide.values()) == ["hi", ["a", "b"]]
            assert len(guide) == 2
            assert set(iter(guide)) == {"summary", "keywords"}

    def test_dict_constructor_round_trips_via_subscript(self):
        # ``dict(result)`` uses keys() + __getitem__, so it warns (per key) but
        # still reconstructs the legacy dict shape.
        guide = SourceGuide(summary="hi", keywords=["a", "b"])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert dict(guide) == {"summary": "hi", "keywords": ["a", "b"]}

    def test_research_task_nested_dict_shape_preserved(self):
        # Legacy callers do result["sources"][0]["url"] — the subscript must
        # yield the old dict-of-dicts shape, not the typed ResearchSource.
        src = ResearchSource(url="http://x", title="T", result_type=1)
        task = ResearchTask(task_id="t1", status=ResearchStatus.COMPLETED, sources=(src,))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sources = task["sources"]
        assert isinstance(sources, list)
        assert sources[0]["url"] == "http://x"

    def test_research_task_tasks_key_is_list(self):
        # ``result["tasks"]`` must be a list (the historical shape), not the
        # typed tuple.
        sub = ResearchTask(task_id="t1", status=ResearchStatus.COMPLETED)
        task = ResearchTask(task_id="t1", status=ResearchStatus.COMPLETED, tasks=(sub,))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tasks = task["tasks"]
        assert isinstance(tasks, list)
        assert tasks[0]["task_id"] == "t1"
        # Sub-task dicts do not carry a nested ``tasks`` key.
        assert "tasks" not in tasks[0]

    def test_mind_map_result_subscript(self):
        result = MindMapResult(mind_map={"name": "Root"}, note_id="n1")
        with pytest.warns(DeprecationWarning):
            assert result["mind_map"] == {"name": "Root"}
        with pytest.warns(DeprecationWarning):
            assert result["note_id"] == "n1"

    def test_research_start_subscript(self):
        start = ResearchStart(
            task_id="t1", report_id=None, notebook_id="nb", query="q", mode="fast"
        )
        with pytest.warns(DeprecationWarning):
            assert start["task_id"] == "t1"
        with pytest.warns(DeprecationWarning):
            assert start["report_id"] is None


class TestQuietDeprecations:
    def test_env_var_suppresses_subscript_warning(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
        guide = SourceGuide(summary="hi", keywords=[])
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # a warning would fail
            assert guide["summary"] == "hi"

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On"])
    def test_truthy_spellings_suppress(self, monkeypatch, value):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", value)
        guide = SourceGuide(summary="hi", keywords=[])
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert guide["summary"] == "hi"

    def test_falsy_value_keeps_warning(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "0")
        guide = SourceGuide(summary="hi", keywords=[])
        with pytest.warns(DeprecationWarning):
            _ = guide["summary"]


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
        from notebooklm._source_content import SourceContentRenderer

        class _Rpc:
            async def rpc_call(self, *a, **k):
                return [[[None, ["A summary"], [["kw1", "kw2"]], []]]]

        renderer = SourceContentRenderer(_Rpc())
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            guide = await renderer.get_guide("nb_1", "src_1")
        assert guide.summary == "A summary"
        assert guide.keywords == ("kw1", "kw2")
