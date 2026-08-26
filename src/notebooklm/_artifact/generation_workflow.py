"""Caller-budgeted artifact generation workflow used by ``_app``.

The public ``ArtifactsAPI.generate_*`` methods remain one-shot compatibility
facades.  Application adapters enter through this private workflow so kickoff
retry and optional lifecycle polling consume one explicit absolute deadline.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, TypeVar, cast

from .._backend_compat import project_backend_call
from .._deadline import Monotonic, RuntimeDeadline, Sleep
from .._projectors import project_generation_status
from .._records import (
    ArtifactReviseSlideInput,
    AudioGenerateInput,
    DataTableGenerateInput,
    InfographicGenerateInput,
    InteractiveGenerateInput,
    ReportGenerateInput,
    SlideDeckGenerateInput,
    VideoGenerateInput,
)
from .._studio import (
    ArtifactLifecycleService,
    AudioFamilyService,
    DataTableFamilyService,
    DocumentOptionError,
    InteractiveFamilyService,
    ReportFamilyService,
    StudioManagementService,
    VideoFamilyService,
    VisualFamilyService,
)
from ..artifacts import RateLimitRetryEvent, _run_deadline_generation_workflow
from ..exceptions import ValidationError
from ..rpc import (
    AudioFormat,
    AudioLength,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    VideoFormat,
    VideoStyle,
)
from ..types import GenerationStatus
from .validation import coerce_report_format

RetryCallback = Callable[[RateLimitRetryEvent], object | Awaitable[object]]
WaitContext = Callable[[str, str], AbstractAsyncContextManager[None]]
_ServiceT = TypeVar("_ServiceT")

_SUPPORTED_METHODS = frozenset(
    {
        "generate_audio",
        "generate_cinematic_video",
        "generate_data_table",
        "generate_flashcards",
        "generate_infographic",
        "generate_quiz",
        "generate_report",
        "generate_slide_deck",
        "generate_video",
        "revise_slide",
    }
)


class ArtifactGenerationWorkflow:
    """Execute one application generation budget behind the artifact facade."""

    def __init__(
        self,
        *,
        audio: AudioFamilyService | None,
        video: VideoFamilyService | None,
        reports: ReportFamilyService | None,
        interactive: InteractiveFamilyService | None,
        visuals: VisualFamilyService | None,
        data_tables: DataTableFamilyService | None,
        management: StudioManagementService | None,
        lifecycle: ArtifactLifecycleService,
        sleep: Sleep | None = None,
        monotonic: Monotonic | None = None,
    ) -> None:
        self._audio = audio
        self._video = video
        self._reports = reports
        self._interactive = interactive
        self._visuals = visuals
        self._data_tables = data_tables
        self._management = management
        self._lifecycle = lifecycle
        self._sleep = sleep
        self._monotonic = monotonic

    async def run(
        self,
        method_name: str,
        notebook_id: str,
        call_kwargs: dict[str, Any],
        *,
        timeout: float,
        max_retries: int,
        wait: bool,
        artifact_type: str,
        wait_message: str,
        initial_interval: float | None = None,
        on_retry: RetryCallback | None = None,
        on_wait_start: Callable[[str], None] | None = None,
        wait_context: WaitContext | None = None,
    ) -> GenerationStatus | None:
        """Start, retry, and optionally wait under one caller deadline."""
        if method_name not in _SUPPORTED_METHODS:
            raise ValueError(f"unsupported application generation method: {method_name}")

        async def _generate(deadline: RuntimeDeadline) -> GenerationStatus:
            return await self.generate_once(
                method_name,
                notebook_id,
                call_kwargs,
                deadline=deadline,
            )

        async def _wait(
            resolved_notebook_id: str,
            task_id: str,
            deadline: RuntimeDeadline,
            caller_timeout: float,
            caller_interval: float | None,
        ) -> GenerationStatus:
            return await self._wait_for_completion(
                resolved_notebook_id,
                task_id,
                deadline=deadline,
                timeout=caller_timeout,
                initial_interval=caller_interval,
            )

        return await _run_deadline_generation_workflow(
            _generate,
            _wait,
            notebook_id=notebook_id,
            timeout=timeout,
            max_retries=max_retries,
            wait=wait,
            artifact_type=artifact_type,
            wait_message=wait_message,
            initial_interval=initial_interval,
            on_retry=on_retry,
            on_wait_start=on_wait_start,
            wait_context=wait_context,
            sleep=self._sleep,
            monotonic=self._monotonic,
        )

    async def _wait_for_completion(
        self,
        notebook_id: str,
        task_id: str,
        *,
        deadline: RuntimeDeadline,
        timeout: float,
        initial_interval: float | None,
    ) -> GenerationStatus:
        async def _poll_status(
            resolved_notebook_id: str, resolved_task_id: str
        ) -> GenerationStatus:
            return project_generation_status(
                await project_backend_call(
                    self._lifecycle.observe(
                        resolved_notebook_id,
                        resolved_task_id,
                        deadline=deadline,
                    )
                )
            )

        wait_kwargs: dict[str, Any] = {
            "initial_interval": 2.0 if initial_interval is None else initial_interval,
            "max_interval": 10.0,
            "timeout": timeout,
            "max_not_found": 5,
            "min_not_found_window": 10.0,
            "poll_status": _poll_status,
            "on_status_change": None,
            "deadline": deadline,
        }
        # ``_wait_for_completion`` is private: the service stays I1-neutral by
        # not naming the public ``GenerationStatus`` its callbacks carry, so
        # this caller — which already owns that type — restores it here.
        return cast(
            GenerationStatus,
            await self._lifecycle._wait_for_completion(notebook_id, task_id, **wait_kwargs),
        )

    async def generate_once(
        self,
        method_name: str,
        notebook_id: str,
        kwargs: dict[str, Any],
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> GenerationStatus:
        source_ids = kwargs.get("source_ids")
        sources = None if source_ids is None else tuple(source_ids)
        if method_name == "generate_audio":
            audio_service = _require(self._audio)
            audio_format = cast(AudioFormat | None, kwargs.get("audio_format"))
            audio_length = cast(AudioLength | None, kwargs.get("audio_length"))
            audio_result = await project_backend_call(
                audio_service.generate(
                    AudioGenerateInput(
                        notebook_id,
                        sources,
                        kwargs.get("language"),
                        kwargs.get("instructions"),
                        None if audio_format is None else audio_format.name.lower(),
                        None if audio_length is None else audio_length.name.lower(),
                    ),
                    deadline=deadline,
                )
            )
            return project_generation_status(audio_result.status)
        elif method_name in {"generate_video", "generate_cinematic_video"}:
            video_service = _require(self._video)
            video_format = cast(VideoFormat | None, kwargs.get("video_format"))
            video_style = cast(VideoStyle | None, kwargs.get("video_style"))
            try:
                video_result = await project_backend_call(
                    video_service.generate(
                        VideoGenerateInput(
                            notebook_id,
                            sources,
                            kwargs.get("language"),
                            kwargs.get("instructions"),
                            (
                                "cinematic"
                                if method_name == "generate_cinematic_video"
                                else None
                                if video_format is None
                                else video_format.name.lower()
                            ),
                            None if video_style is None else video_style.name.lower(),
                            kwargs.get("style_prompt"),
                            cinematic_route=method_name == "generate_cinematic_video",
                        ),
                        deadline=deadline,
                    )
                )
            except DocumentOptionError as error:
                raise ValidationError(str(error)) from None
            return project_generation_status(video_result.status)
        elif method_name == "generate_report":
            report_service = _require(self._reports)
            report_format = coerce_report_format(
                kwargs.get("report_format", ReportFormat.BRIEFING_DOC)
            )
            report_result = await project_backend_call(
                report_service.generate(
                    ReportGenerateInput(
                        notebook_id,
                        report_format.value,
                        sources,
                        kwargs.get("language"),
                        kwargs.get("custom_prompt"),
                        kwargs.get("extra_instructions"),
                    ),
                    deadline=deadline,
                )
            )
            return project_generation_status(report_result.status)
        elif method_name in {"generate_quiz", "generate_flashcards"}:
            interactive_service = _require(self._interactive)
            value = InteractiveGenerateInput(
                notebook_id,
                sources,
                kwargs.get("instructions"),
                _enum_name(kwargs.get("quantity"), QuizQuantity, "quantity"),
                _enum_name(kwargs.get("difficulty"), QuizDifficulty, "difficulty"),
            )
            call = (
                interactive_service.generate_quiz
                if method_name == "generate_quiz"
                else interactive_service.generate_flashcards
            )
            interactive_result = await project_backend_call(call(value, deadline=deadline))
            return project_generation_status(interactive_result.status)
        elif method_name == "generate_infographic":
            visual_service = _require(self._visuals)
            infographic_result = await project_backend_call(
                visual_service.generate_infographic(
                    InfographicGenerateInput(
                        notebook_id,
                        sources,
                        kwargs.get("language"),
                        kwargs.get("instructions"),
                        _optional_enum_name(kwargs.get("orientation")),
                        _optional_enum_name(kwargs.get("detail_level")),
                        _optional_enum_name(kwargs.get("style")),
                    ),
                    deadline=deadline,
                )
            )
            return project_generation_status(infographic_result.status)
        elif method_name == "generate_slide_deck":
            visual_service = _require(self._visuals)
            slide_deck_result = await project_backend_call(
                visual_service.generate_slide_deck(
                    SlideDeckGenerateInput(
                        notebook_id,
                        sources,
                        kwargs.get("language"),
                        kwargs.get("instructions"),
                        _optional_enum_name(kwargs.get("slide_format")),
                        _optional_enum_name(kwargs.get("slide_length")),
                    ),
                    deadline=deadline,
                )
            )
            return project_generation_status(slide_deck_result.status)
        elif method_name == "generate_data_table":
            data_table_service = _require(self._data_tables)
            data_table_result = await project_backend_call(
                data_table_service.generate(
                    DataTableGenerateInput(
                        notebook_id,
                        sources,
                        kwargs.get("language"),
                        kwargs.get("instructions"),
                    ),
                    deadline=deadline,
                )
            )
            return project_generation_status(data_table_result.status)
        else:
            management_service = _require(self._management)
            slide_index = kwargs["slide_index"]
            if slide_index < 0:
                raise ValidationError(f"slide_index must be >= 0, got {slide_index}")
            revision_result = await project_backend_call(
                management_service.revise_slide(
                    ArtifactReviseSlideInput(
                        notebook_id,
                        kwargs["artifact_id"],
                        slide_index,
                        kwargs["prompt"],
                    ),
                    deadline=deadline,
                )
            )
            return project_generation_status(revision_result.status)


def _require(service: _ServiceT | None) -> _ServiceT:
    if service is None:
        raise RuntimeError("ArtifactsAPI requires the client-assembled semantic backend")
    return service


def _optional_enum_name(value: Any) -> str | None:
    if value is None:
        return None
    return value.name.lower()


def _enum_name(value: Any, expected: type[Any], parameter: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, expected):
        raise ValidationError(
            f"{parameter} must be a {expected.__name__} member or None, got "
            f"{value!r} ({type(value).__name__})"
        )
    return value.name.lower()


__all__ = ["ArtifactGenerationWorkflow"]
