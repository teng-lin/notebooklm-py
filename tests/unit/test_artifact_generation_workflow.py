"""Focused caller-budget regressions for private artifact generation workflow."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from notebooklm._artifact.generation_workflow import ArtifactGenerationWorkflow
from notebooklm._records import AudioGenerateResult, GenerationStatusRecord
from notebooklm.exceptions import RateLimitError


class _Clock:
    def __init__(self, now: float = 10.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class _Audio:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.deadlines: list[object] = []

    async def generate(self, _value: object, *, deadline: object) -> AudioGenerateResult:
        self.deadlines.append(deadline)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


class _Lifecycle:
    def __init__(self) -> None:
        self.deadlines: list[object] = []

    async def observe(
        self,
        _notebook_id: str,
        task_id: str,
        *,
        deadline: object,
    ) -> GenerationStatusRecord:
        self.deadlines.append(deadline)
        return GenerationStatusRecord(task_id, "completed")

    async def wait_for_completion(self, notebook_id: str, task_id: str, **kwargs: Any):
        self.deadlines.append(kwargs["deadline"])
        return await kwargs["poll_status"](notebook_id, task_id)


def _workflow(
    audio: _Audio,
    lifecycle: _Lifecycle,
    clock: _Clock,
) -> ArtifactGenerationWorkflow:
    return ArtifactGenerationWorkflow(
        audio=audio,  # type: ignore[arg-type]
        video=None,
        reports=None,
        interactive=None,
        visuals=None,
        data_tables=None,
        management=None,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


@pytest.mark.asyncio
async def test_retry_kickoff_and_wait_share_one_absolute_deadline() -> None:
    clock = _Clock()
    audio = _Audio(
        RateLimitError("quota", rpc_code="USER_DISPLAYABLE_ERROR"),
        AudioGenerateResult(GenerationStatusRecord("task", "pending")),
    )
    lifecycle = _Lifecycle()
    retries: list[float] = []
    wait_starts: list[str] = []
    context_events: list[tuple[str, str]] = []

    @asynccontextmanager
    async def wait_context(message: str, resume_hint: str):
        context_events.append((message, resume_hint))
        yield

    result = await _workflow(audio, lifecycle, clock).run(
        "generate_audio",
        "nb",
        {"source_ids": ["source"], "language": "en"},
        timeout=90.0,
        max_retries=1,
        wait=True,
        artifact_type="audio",
        wait_message="Waiting for audio generation (typically 2-5 min)...",
        initial_interval=3.0,
        on_retry=lambda event: retries.append(event.delay),
        on_wait_start=wait_starts.append,
        wait_context=wait_context,
    )

    assert result is not None and result.is_complete
    assert clock.sleeps == [60.0]
    assert retries == [60.0]
    assert wait_starts == ["task"]
    assert context_events == [
        (
            "Waiting for audio generation (typically 2-5 min)...",
            "notebooklm artifact poll task",
        )
    ]
    assert len(audio.deadlines) == 2
    assert len(lifecycle.deadlines) == 2
    assert all(
        deadline is audio.deadlines[0] for deadline in (*audio.deadlines, *lifecycle.deadlines)
    )


@pytest.mark.asyncio
async def test_retry_sleep_is_clamped_and_cannot_start_after_budget_expires() -> None:
    clock = _Clock()
    audio = _Audio(
        RateLimitError("quota", rpc_code="USER_DISPLAYABLE_ERROR"),
        AudioGenerateResult(GenerationStatusRecord("unexpected", "pending")),
    )

    with pytest.raises(TimeoutError, match="audio generation timed out after 30.0s"):
        await _workflow(audio, _Lifecycle(), clock).run(
            "generate_audio",
            "nb",
            {},
            timeout=30.0,
            max_retries=1,
            wait=False,
            artifact_type="audio",
            wait_message="unused",
        )

    assert clock.sleeps == [30.0]
    assert len(audio.deadlines) == 1
