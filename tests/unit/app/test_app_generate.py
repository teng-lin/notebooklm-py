"""Unit tests for the transport-neutral ``notebooklm._app.generate`` executor.

These pin the relocated generate *executor* business logic at the ``_app``
boundary (independent of the Click adapter):

* :func:`execute_generation` dispatch to the right ``client.artifacts.<method>``
  per ``kind``;
* the per-kind call-kwargs builder (``_build_call_kwargs``): ``source_ids`` /
  ``language`` / ``instructions`` threading, the ``revise-slide`` and
  ``data-table`` / ``report`` / ``cinematic-video`` bespoke shapes;
* the injected ``notebook_resolver`` / ``source_resolver`` seams;
* the mind-map routing (interactive → ``client.mind_maps.generate``;
  note-backed → ``generate_mind_map``).

No Click / ``CliRunner`` — every test calls ``execute_generation`` directly
with a ``MagicMock`` client + injected resolvers. The CLI ``--json`` / console
rendering assertions stay in ``tests/unit/cli/test_generate.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.generate import (
    GenerationExecutionResult,
    _build_call_kwargs,
    build_generation_plan,
    execute_generation,
)
from notebooklm.types import GenerationStatus, MindMapKind


def _notebook_resolver(resolved: str = "nb_resolved") -> AsyncMock:
    """Resolver matching the CLI signature (client, nb_id, *, json_output)."""
    return AsyncMock(return_value=resolved)


def _source_resolver(resolved=None) -> AsyncMock:
    """Resolver matching the CLI signature (client, nb_id, ids, *, json_output)."""
    return AsyncMock(return_value=resolved if resolved is not None else ["s1"])


def _make_client(method_name: str, return_value) -> MagicMock:
    client = MagicMock()
    client.artifacts = MagicMock()
    setattr(client.artifacts, method_name, AsyncMock(return_value=return_value))
    return client


def _audio_plan(**overrides):
    args = {"notebook_id": "nb_partial", "audio_format": "deep-dive", "audio_length": "default"}
    args.update(overrides)
    return build_generation_plan("audio", args)


# ---------------------------------------------------------------------------
# _build_call_kwargs — per-kind call shapes (pure).
# ---------------------------------------------------------------------------


class TestBuildCallKwargs:
    def test_audio_passes_instructions_and_language(self):
        plan = build_generation_plan(
            "audio",
            {
                "notebook_id": "nb_1",
                "audio_format": "deep-dive",
                "audio_length": "default",
                "description": "focus",
                "language": "fr",
            },
            language_resolver=lambda lang: lang or "en",
        )
        kwargs = _build_call_kwargs(plan, notebook_id="nb_1", sources=["s1"])
        assert kwargs["source_ids"] == ["s1"]
        assert kwargs["language"] == "fr"
        assert kwargs["instructions"] == "focus"

    def test_audio_empty_description_becomes_none_instructions(self):
        plan = _audio_plan(description="")
        kwargs = _build_call_kwargs(plan, notebook_id="nb_1", sources=[])
        assert kwargs["instructions"] is None

    def test_revise_slide_bespoke_shape(self):
        plan = build_generation_plan(
            "revise-slide",
            {
                "notebook_id": "nb_1",
                "description": "move title",
                "artifact_id": "art_1",
                "slide_index": "3",
            },
        )
        kwargs = _build_call_kwargs(plan, notebook_id="nb_1", sources=None)
        assert kwargs == {"artifact_id": "art_1", "slide_index": 3, "prompt": "move title"}

    def test_report_packs_report_params(self):
        plan = build_generation_plan(
            "report",
            {"notebook_id": "nb_1", "report_format": "study-guide", "description": "x"},
        )
        kwargs = _build_call_kwargs(plan, notebook_id="nb_1", sources=["s1"])
        assert "report_format" in kwargs
        assert "custom_prompt" in kwargs
        assert "extra_instructions" in kwargs
        # report never carries ``instructions``.
        assert "instructions" not in kwargs

    def test_data_table_passes_description_as_instructions(self):
        plan = build_generation_plan(
            "data-table",
            {"notebook_id": "nb_1", "description": "Compare A and B"},
        )
        kwargs = _build_call_kwargs(plan, notebook_id="nb_1", sources=["s1"])
        assert kwargs["instructions"] == "Compare A and B"

    def test_cinematic_video_uses_description_as_instructions(self):
        plan = build_generation_plan(
            "cinematic-video",
            {"notebook_id": "nb_1", "description": "epic scene"},
        )
        kwargs = _build_call_kwargs(plan, notebook_id="nb_1", sources=["s1"])
        assert kwargs == {
            "source_ids": ["s1"],
            "language": plan.language,
            "instructions": "epic scene",
        }


# ---------------------------------------------------------------------------
# execute_generation — dispatch + resolver injection.
# ---------------------------------------------------------------------------


class TestExecuteGeneration:
    @pytest.mark.asyncio
    async def test_dispatches_to_generate_audio(self):
        status = GenerationStatus(task_id="t1", status="pending", error=None, error_code=None)
        client = _make_client("generate_audio", status)
        plan = _audio_plan()

        result = await execute_generation(
            plan,
            client,
            notebook_resolver=_notebook_resolver("nb_resolved"),
            source_resolver=_source_resolver(["s1"]),
        )
        assert isinstance(result, GenerationExecutionResult)
        assert result.kind == "audio"
        assert result.generation is not None
        assert result.generation.status == "pending"
        client.artifacts.generate_audio.assert_awaited_once()
        # The resolved notebook id is the one used for the API call.
        call_args = client.artifacts.generate_audio.await_args
        assert call_args.args[0] == "nb_resolved"

    @pytest.mark.asyncio
    async def test_notebook_resolver_invoked_with_json_output_flag(self):
        status = GenerationStatus(task_id="t1", status="pending", error=None, error_code=None)
        client = _make_client("generate_audio", status)
        plan = _audio_plan(json_output=True)
        resolver = _notebook_resolver("nb_resolved")

        await execute_generation(
            plan,
            client,
            notebook_resolver=resolver,
            source_resolver=_source_resolver(),
        )
        _args, kwargs = resolver.await_args
        assert kwargs["json_output"] is True

    @pytest.mark.asyncio
    async def test_revise_slide_skips_source_resolution(self):
        status = GenerationStatus(task_id="t1", status="pending", error=None, error_code=None)
        client = _make_client("revise_slide", status)
        plan = build_generation_plan(
            "revise-slide",
            {
                "notebook_id": "nb_1",
                "description": "fix",
                "artifact_id": "art_1",
                "slide_index": "0",
            },
        )
        source_resolver = _source_resolver()

        await execute_generation(
            plan,
            client,
            notebook_resolver=_notebook_resolver(),
            source_resolver=source_resolver,
        )
        source_resolver.assert_not_awaited()
        client.artifacts.revise_slide.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_status_maps_to_failed_outcome(self):
        status = GenerationStatus(task_id="t1", status="failed", error="boom", error_code="X")
        client = _make_client("generate_audio", status)
        result = await execute_generation(
            _audio_plan(),
            client,
            notebook_resolver=_notebook_resolver(),
            source_resolver=_source_resolver(),
        )
        assert result.generation.status == "failed"
        assert result.generation.error == "boom"

    @pytest.mark.asyncio
    async def test_none_result_is_failed(self):
        client = _make_client("generate_audio", None)
        result = await execute_generation(
            _audio_plan(),
            client,
            notebook_resolver=_notebook_resolver(),
            source_resolver=_source_resolver(),
        )
        assert result.generation.status == "failed"


# ---------------------------------------------------------------------------
# Mind-map routing.
# ---------------------------------------------------------------------------


class TestExecuteGenerationMindMap:
    @pytest.mark.asyncio
    async def test_interactive_routes_through_mind_maps_api(self):
        client = MagicMock()
        client.artifacts = MagicMock()
        mind_map_obj = MagicMock()
        client.mind_maps = MagicMock()
        client.mind_maps.generate = AsyncMock(return_value=mind_map_obj)
        plan = build_generation_plan(
            "mind-map",
            {
                "notebook_id": "nb_1",
                "map_kind": "interactive",
                "source_ids": ["s1"],
                "instructions": "focus on the astronauts",
            },
        )
        result = await execute_generation(
            plan,
            client,
            notebook_resolver=_notebook_resolver("nb_resolved"),
            source_resolver=_source_resolver(["s1"]),
        )
        assert result.kind == "mind-map"
        assert result.mind_map is mind_map_obj
        assert result.generation is None
        client.mind_maps.generate.assert_awaited_once()
        _args, kwargs = client.mind_maps.generate.await_args
        assert kwargs["kind"] == MindMapKind.INTERACTIVE
        # The interactive path must forward the custom prompt (server applies it).
        assert kwargs["instructions"] == "focus on the astronauts"

    @pytest.mark.asyncio
    async def test_note_backed_routes_through_generate_mind_map(self):
        client = MagicMock()
        client.artifacts = MagicMock()
        payload = {"note_id": "n1", "mind_map": {"name": "Root", "children": []}}
        client.artifacts.generate_mind_map = AsyncMock(return_value=payload)
        client.mind_maps = MagicMock()
        client.mind_maps.generate = AsyncMock()
        plan = build_generation_plan(
            "mind-map",
            {"notebook_id": "nb_1", "map_kind": "note-backed", "source_ids": ["s1"]},
        )
        result = await execute_generation(
            plan,
            client,
            notebook_resolver=_notebook_resolver(),
            source_resolver=_source_resolver(["s1"]),
        )
        assert result.mind_map == payload
        client.artifacts.generate_mind_map.assert_awaited_once()
        client.mind_maps.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_interactive_json_output_skips_mind_map_context(self):
        """Under ``--json`` the mind-map context span is bypassed (no spinner)."""
        client = MagicMock()
        client.artifacts = MagicMock()
        client.mind_maps = MagicMock()
        client.mind_maps.generate = AsyncMock(return_value=MagicMock())
        context_entered = {"flag": False}

        class _Ctx:
            async def __aenter__(self):
                context_entered["flag"] = True

            async def __aexit__(self, *exc):
                return False

        plan = build_generation_plan(
            "mind-map",
            {"notebook_id": "nb_1", "source_ids": ["s1"], "json_output": True},
        )
        await execute_generation(
            plan,
            client,
            notebook_resolver=_notebook_resolver(),
            source_resolver=_source_resolver(["s1"]),
            mind_map_context=lambda: _Ctx(),
        )
        assert context_entered["flag"] is False
