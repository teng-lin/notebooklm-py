"""Backend-neutral artifact operations API."""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _mind_map  # noqa: F401 -- private compatibility identity
from ._artifact import formatters as _artifact_formatters  # noqa: F401
from ._artifact import polling as _artifact_polling  # noqa: F401
from ._artifact import validation as _artifact_validation  # noqa: F401
from ._artifact.polling import ArtifactPollingService
from ._notebook_metadata import NotebookSourceIdProvider
from ._polling_registry import PollRegistry
from ._types.enums import (
    AudioFormat,
    AudioLength,
    ExportType,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
)
from ._types.research import MindMapResult
from .exceptions import ArtifactNotFoundError
from .types import Artifact, ArtifactType, GenerationStatus, ReportSuggestion

if TYPE_CHECKING:
    from ._runtime.lifecycle import ClientLifecycle
    from ._transport_drain import TransportDrainTracker


class ArtifactsAPI(ABC):
    """Backend-neutral operations on generated notebook artifacts."""

    def __init__(
        self,
        *,
        drain: TransportDrainTracker,
        lifecycle: ClientLifecycle,
        notebooks: NotebookSourceIdProvider,
        storage_path: Path | None = None,
    ) -> None:
        self._drain = drain
        self._lifecycle = lifecycle
        self._notebooks = notebooks
        self._storage_path = storage_path
        self._poll_registry = PollRegistry()
        self._polling = ArtifactPollingService(
            loop_guard=self._lifecycle,
            op_scope=self._drain,
            poll_registry=self._poll_registry,
        )
        self._drain.register_drain_hook("artifacts.polls", self._polling.drain)

    @abstractmethod
    async def _list_studio(self, notebook_id: str) -> builtins.list[Artifact]:
        """List decoded studio artifacts without note-backed mind maps."""

    @abstractmethod
    async def list(
        self, notebook_id: str, artifact_type: ArtifactType | None = None
    ) -> builtins.list[Artifact]:
        """List public artifacts, including note-backed mind maps."""

    async def get(self, notebook_id: str, artifact_id: str) -> Artifact:
        """Get an artifact by ID."""
        artifact = await self.get_or_none(notebook_id, artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError(artifact_id)
        return artifact

    async def get_or_none(self, notebook_id: str, artifact_id: str) -> Artifact | None:
        """Get an artifact by ID, or ``None`` when absent."""
        return next(
            (artifact for artifact in await self.list(notebook_id) if artifact.id == artifact_id),
            None,
        )

    _get_or_none = get_or_none

    @abstractmethod
    async def get_prompt(self, notebook_id: str, artifact_id: str) -> str | None:
        """Return the generation prompt for an artifact."""

    async def list_audio(self, notebook_id: str) -> builtins.list[Artifact]:
        return await self.list(notebook_id, ArtifactType.AUDIO)

    async def list_video(self, notebook_id: str) -> builtins.list[Artifact]:
        return await self.list(notebook_id, ArtifactType.VIDEO)

    async def list_reports(self, notebook_id: str) -> builtins.list[Artifact]:
        return await self.list(notebook_id, ArtifactType.REPORT)

    async def list_quizzes(self, notebook_id: str) -> builtins.list[Artifact]:
        return await self.list(notebook_id, ArtifactType.QUIZ)

    async def list_flashcards(self, notebook_id: str) -> builtins.list[Artifact]:
        return await self.list(notebook_id, ArtifactType.FLASHCARDS)

    async def list_infographics(self, notebook_id: str) -> builtins.list[Artifact]:
        return await self.list(notebook_id, ArtifactType.INFOGRAPHIC)

    async def list_slide_decks(self, notebook_id: str) -> builtins.list[Artifact]:
        return await self.list(notebook_id, ArtifactType.SLIDE_DECK)

    async def list_data_tables(self, notebook_id: str) -> builtins.list[Artifact]:
        return await self.list(notebook_id, ArtifactType.DATA_TABLE)

    @abstractmethod
    async def generate_audio(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
        audio_format: AudioFormat | None = None,
        audio_length: AudioLength | None = None,
    ) -> GenerationStatus: ...

    @abstractmethod
    async def generate_video(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
        video_format: VideoFormat | None = None,
        video_style: VideoStyle | None = None,
        style_prompt: str | None = None,
    ) -> GenerationStatus: ...

    @abstractmethod
    async def generate_cinematic_video(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> GenerationStatus: ...

    @abstractmethod
    async def generate_report(
        self,
        notebook_id: str,
        report_format: ReportFormat = ReportFormat.BRIEFING_DOC,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        custom_prompt: str | None = None,
        extra_instructions: str | None = None,
    ) -> GenerationStatus: ...

    @abstractmethod
    async def generate_study_guide(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        extra_instructions: str | None = None,
    ) -> GenerationStatus: ...

    @abstractmethod
    async def generate_quiz(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        instructions: str | None = None,
        quantity: QuizQuantity | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> GenerationStatus: ...

    @abstractmethod
    async def generate_flashcards(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        instructions: str | None = None,
        quantity: QuizQuantity | None = None,
        difficulty: QuizDifficulty | None = None,
    ) -> GenerationStatus: ...

    @abstractmethod
    async def generate_infographic(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
        orientation: InfographicOrientation | None = None,
        detail_level: InfographicDetail | None = None,
        style: InfographicStyle | None = None,
    ) -> GenerationStatus: ...

    @abstractmethod
    async def generate_slide_deck(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
        slide_format: SlideDeckFormat | None = None,
        slide_length: SlideDeckLength | None = None,
    ) -> GenerationStatus: ...

    @abstractmethod
    async def generate_data_table(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> GenerationStatus: ...

    @abstractmethod
    async def revise_slide(
        self, notebook_id: str, artifact_id: str, slide_index: int, prompt: str
    ) -> GenerationStatus: ...

    @abstractmethod
    async def retry_failed(self, notebook_id: str, artifact_id: str) -> GenerationStatus: ...

    @abstractmethod
    async def generate_mind_map(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> MindMapResult: ...

    @abstractmethod
    async def download_audio(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str: ...

    @abstractmethod
    async def download_video(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str: ...

    @abstractmethod
    async def download_infographic(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str: ...

    @abstractmethod
    async def download_slide_deck(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "pdf",
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str: ...

    @abstractmethod
    async def download_report(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str: ...

    @abstractmethod
    async def download_mind_map(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        mind_maps: builtins.list[Any] | None = None,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str: ...

    @abstractmethod
    async def download_data_table(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        artifacts_data: builtins.list[Any] | None = None,
    ) -> str: ...

    @abstractmethod
    async def download_quiz(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
        *,
        artifacts: builtins.list[Artifact] | None = None,
    ) -> str: ...

    @abstractmethod
    async def download_flashcards(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
        *,
        artifacts: builtins.list[Artifact] | None = None,
    ) -> str: ...

    @abstractmethod
    async def delete(self, notebook_id: str, artifact_id: str) -> None: ...

    @abstractmethod
    async def rename(
        self,
        notebook_id: str,
        artifact_id: str,
        new_title: str,
        *,
        return_object: bool = True,
    ) -> Artifact | None: ...

    async def poll_status(self, notebook_id: str, task_id: str) -> GenerationStatus:
        return await self._polling.poll_status(
            notebook_id,
            task_id,
            list_studio=self._list_studio,
        )

    async def wait_for_completion(
        self,
        notebook_id: str,
        task_id: str,
        initial_interval: float = 2.0,
        max_interval: float = 10.0,
        timeout: float = 300.0,
        max_not_found: int = 5,
        min_not_found_window: float = 10.0,
        on_status_change: Callable[[GenerationStatus], object] | None = None,
    ) -> GenerationStatus:
        return await self._polling.wait_for_completion(
            notebook_id,
            task_id,
            initial_interval=initial_interval,
            max_interval=max_interval,
            timeout=timeout,
            max_not_found=max_not_found,
            min_not_found_window=min_not_found_window,
            poll_status=self.poll_status,
            on_status_change=on_status_change,
        )

    @abstractmethod
    async def export_report(
        self,
        notebook_id: str,
        artifact_id: str,
        title: str = "Export",
        export_type: ExportType = ExportType.DOCS,
    ) -> Any: ...

    @abstractmethod
    async def export_data_table(
        self, notebook_id: str, artifact_id: str, title: str = "Export"
    ) -> Any: ...

    @abstractmethod
    async def export(
        self,
        notebook_id: str,
        artifact_id: str | None = None,
        title: str = "Export",
        export_type: ExportType = ExportType.DOCS,
        *,
        content: str | None = None,
    ) -> Any: ...

    @abstractmethod
    async def suggest_reports(self, notebook_id: str) -> builtins.list[ReportSuggestion]: ...


__all__ = ["ArtifactsAPI"]
