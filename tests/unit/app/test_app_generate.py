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

import asyncio
import gc
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

import notebooklm.artifacts as artifact_helpers
from notebooklm._app.generate import (
    GenerationExecutionResult,
    _build_call_kwargs,
    build_generation_plan,
    execute_generation,
)
from notebooklm.exceptions import (
    ArtifactInProgressTimeoutError,
    ArtifactPendingTimeoutError,
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
    client.artifacts.wait_for_completion = AsyncMock()
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
        client.artifacts.wait_for_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_public_kickoff_and_wait_seams_receive_workflow_inputs(self):
        started = GenerationStatus(task_id="t1", status="pending", error=None, error_code=None)
        completed = GenerationStatus(task_id="t1", status="completed", error=None, error_code=None)
        client = _make_client("generate_audio", started)
        client.artifacts.wait_for_completion.return_value = completed
        plan = _audio_plan(wait=True, timeout=60.0, interval=5.0)

        result = await execute_generation(
            plan,
            client,
            notebook_resolver=_notebook_resolver("nb_resolved"),
            source_resolver=_source_resolver(["s1"]),
        )

        assert result.generation is not None
        assert result.generation.status == "completed"
        client.artifacts.generate_audio.assert_awaited_once()
        client.artifacts.wait_for_completion.assert_awaited_once()
        wait_args = client.artifacts.wait_for_completion.await_args
        assert wait_args.args == ("nb_resolved", "t1")
        assert wait_args.kwargs["initial_interval"] == 5.0
        assert 0.0 < wait_args.kwargs["timeout"] <= 60.0

    @pytest.mark.asyncio
    async def test_wait_receives_remaining_budget_and_preserves_typed_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _ControlledDeadline:
            timeout = 60.0

            def __init__(self) -> None:
                self.remaining_values = iter((60.0, 17.0))

            def expired(self) -> bool:
                return False

            def remaining(self) -> float:
                return next(self.remaining_values)

            def timeout_message(self, operation: str) -> str:
                return f"{operation} timed out after 60.0s"

        deadline = _ControlledDeadline()

        class _ControlledDeadlineFactory:
            @staticmethod
            def start(timeout: float, *, monotonic: object) -> _ControlledDeadline:
                assert timeout == 60.0
                return deadline

        monkeypatch.setattr(artifact_helpers, "RuntimeDeadline", _ControlledDeadlineFactory)

        started = GenerationStatus(task_id="t1", status="pending", error=None, error_code=None)
        timeout_error = ArtifactPendingTimeoutError("nb_resolved", "t1", 17.0)
        client = _make_client("generate_audio", started)
        client.artifacts.wait_for_completion.side_effect = timeout_error

        with pytest.raises(ArtifactPendingTimeoutError) as exc_info:
            await execute_generation(
                _audio_plan(wait=True, timeout=60.0, interval=5.0),
                client,
                notebook_resolver=_notebook_resolver("nb_resolved"),
                source_resolver=_source_resolver(["s1"]),
            )

        assert exc_info.value is timeout_error
        client.artifacts.wait_for_completion.assert_awaited_once_with(
            "nb_resolved",
            "t1",
            timeout=17.0,
            initial_interval=5.0,
            on_status_change=ANY,
        )

    @pytest.mark.asyncio
    async def test_caller_budget_preempts_a_slower_shared_wait_with_typed_timeout(self) -> None:
        started = GenerationStatus(task_id="t1", status="pending", error=None, error_code=None)
        leader_timeout = ArtifactPendingTimeoutError("nb_resolved", "t1", 1.0)
        client = _make_client("generate_audio", started)

        async def leader_poll() -> object:
            await asyncio.sleep(0.2)
            raise leader_timeout

        leader_task = asyncio.create_task(leader_poll())

        async def slower_shared_wait(*_args: object, **_kwargs: object) -> object:
            return await asyncio.shield(leader_task)

        client.artifacts.wait_for_completion.side_effect = slower_shared_wait

        try:
            with pytest.raises(ArtifactPendingTimeoutError) as exc_info:
                await execute_generation(
                    _audio_plan(wait=True, timeout=0.05, interval=0.01),
                    client,
                    notebook_resolver=_notebook_resolver("nb_resolved"),
                    source_resolver=_source_resolver(["s1"]),
                )

            assert exc_info.value is not leader_timeout
            assert exc_info.value.notebook_id == "nb_resolved"
            assert exc_info.value.task_id == "t1"
            assert exc_info.value.timeout == 0.05
            assert exc_info.value.last_status == "pending"
            assert exc_info.value.status_history == ("pending",)
            assert not leader_task.done()
        finally:
            leader_task.cancel()
            await asyncio.gather(leader_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_caller_timeout_uses_observed_in_progress_transition(self) -> None:
        started = GenerationStatus(task_id="t1", status="pending", error=None, error_code=None)
        client = _make_client("generate_audio", started)

        async def in_progress_wait(*_args: object, **kwargs: object) -> object:
            callback = kwargs["on_status_change"]
            assert callable(callback)
            callback(GenerationStatus(task_id="t1", status="in_progress"))
            await asyncio.sleep(0.2)
            return GenerationStatus(task_id="t1", status="completed")

        client.artifacts.wait_for_completion.side_effect = in_progress_wait

        with pytest.raises(ArtifactInProgressTimeoutError) as exc_info:
            await execute_generation(
                _audio_plan(wait=True, timeout=0.05, interval=0.01),
                client,
                notebook_resolver=_notebook_resolver("nb_resolved"),
                source_resolver=_source_resolver(["s1"]),
            )

        assert exc_info.value.last_status == "in_progress"
        assert exc_info.value.status_history == ("in_progress",)
        assert tuple(status.status for status in exc_info.value.status_transitions) == (
            "in_progress",
        )

    @pytest.mark.asyncio
    async def test_caller_timer_does_not_depend_on_deadline_expired_clock_edge(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _ClockEdgeDeadline:
            timeout = 60.0

            def __init__(self) -> None:
                self.remaining_values = iter((60.0, 0.01))

            def expired(self) -> bool:
                return False

            def remaining(self) -> float:
                return next(self.remaining_values)

            def timeout_message(self, operation: str) -> str:
                return f"{operation} timed out after 60.0s"

        deadline = _ClockEdgeDeadline()

        class _ClockEdgeDeadlineFactory:
            @staticmethod
            def start(timeout: float, *, monotonic: object) -> _ClockEdgeDeadline:
                assert timeout == 60.0
                return deadline

        monkeypatch.setattr(artifact_helpers, "RuntimeDeadline", _ClockEdgeDeadlineFactory)
        started = GenerationStatus(task_id="t1", status="pending", error=None, error_code=None)
        client = _make_client("generate_audio", started)

        async def in_progress_wait(*_args: object, **kwargs: object) -> object:
            callback = kwargs["on_status_change"]
            assert callable(callback)
            callback(GenerationStatus(task_id="t1", status="in_progress"))
            await asyncio.Event().wait()

        client.artifacts.wait_for_completion.side_effect = in_progress_wait

        with pytest.raises(ArtifactInProgressTimeoutError) as exc_info:
            await execute_generation(
                _audio_plan(wait=True, timeout=60.0),
                client,
                notebook_resolver=_notebook_resolver("nb_resolved"),
                source_resolver=_source_resolver(["s1"]),
            )

        assert exc_info.value.last_status == "in_progress"
        assert exc_info.value.status_history == ("in_progress",)

    @pytest.mark.asyncio
    async def test_caller_timer_does_not_await_a_cancellation_suppressing_child(self) -> None:
        release = asyncio.Event()
        cancellation_observed = asyncio.Event()
        loop_errors: list[dict[str, object]] = []

        async def suppress_cancellation() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_observed.set()
                await release.wait()

        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        original_tasks = set(artifact_helpers._DETACHED_TIMEOUT_TASKS)
        try:
            started_at = loop.time()
            completed, result = await artifact_helpers._await_before_timeout(
                suppress_cancellation(),
                0.01,
            )
            elapsed = loop.time() - started_at

            assert (completed, result) == (False, None)
            assert elapsed < 0.05
            await asyncio.wait_for(cancellation_observed.wait(), timeout=0.1)
            assert len(artifact_helpers._DETACHED_TIMEOUT_TASKS - original_tasks) == 1

            gc.collect()
            await asyncio.sleep(0)
            assert not any("destroyed" in str(error.get("message", "")) for error in loop_errors)
        finally:
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            loop.set_exception_handler(previous_handler)

        assert original_tasks == artifact_helpers._DETACHED_TIMEOUT_TASKS

    @pytest.mark.asyncio
    async def test_inner_bare_timeout_before_caller_deadline_propagates(self) -> None:
        started = GenerationStatus(task_id="t1", status="pending", error=None, error_code=None)
        inner_timeout = TimeoutError("inner waiter timeout")
        client = _make_client("generate_audio", started)
        client.artifacts.wait_for_completion.side_effect = inner_timeout

        with pytest.raises(TimeoutError) as exc_info:
            await execute_generation(
                _audio_plan(wait=True, timeout=60.0),
                client,
                notebook_resolver=_notebook_resolver("nb_resolved"),
                source_resolver=_source_resolver(["s1"]),
            )

        assert exc_info.value is inner_timeout

    @pytest.mark.asyncio
    async def test_inner_bare_kickoff_timeout_before_caller_deadline_propagates(self) -> None:
        inner_timeout = TimeoutError("inner kickoff timeout")
        client = _make_client("generate_audio", None)
        client.artifacts.generate_audio.side_effect = inner_timeout

        with pytest.raises(TimeoutError) as exc_info:
            await execute_generation(
                _audio_plan(wait=False, timeout=60.0),
                client,
                notebook_resolver=_notebook_resolver("nb_resolved"),
                source_resolver=_source_resolver(["s1"]),
            )

        assert exc_info.value is inner_timeout
        client.artifacts.wait_for_completion.assert_not_awaited()

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
