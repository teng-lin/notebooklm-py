"""Studio representation selection, serialization, and trusted byte retrieval."""

from __future__ import annotations

import json
from collections.abc import Sequence

from .._artifact.formatters import _extract_app_data, _format_interactive_content
from .._semantic.backend import BackendAdapter
from .._semantic.records import (
    ARTIFACT_DOWNLOAD_DEF,
    ArtifactDownloadInput,
    ArtifactParseFailureKind,
    ArtifactParseFailureRecord,
    ArtifactRecord,
    ArtifactRepresentationRecord,
    MindMapRepresentationRecord,
)
from ..exceptions import (
    ArtifactDownloadError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    UnknownRPCMethodError,
    ValidationError,
)
from .downloads import DownloadResult, StudioDownloadClient
from .serialization import StudioSerializationClient


class ArtifactRepresentationService:
    """Select and materialize public artifact download representations."""

    def __init__(
        self,
        backend: BackendAdapter | None,
        *,
        remote: StudioDownloadClient,
        serialization: StudioSerializationClient | None = None,
    ) -> None:
        self._backend = backend
        self._remote = remote
        self._serialization = serialization or StudioSerializationClient()

    @staticmethod
    def _project_parse_failure(record: ArtifactParseFailureRecord) -> Exception:
        """Rebuild a bounded public cause without retaining raw wire payloads."""

        if record.kind is ArtifactParseFailureKind.UNKNOWN_RPC_METHOD:
            return UnknownRPCMethodError(
                record.message,
                method_id=record.method_id,
                path=record.path,
                source=record.source,
                found_ids=list(record.found_ids) or None,
                raw_response=record.raw_response,
                data_at_failure=record.data_at_failure,
                rpc_code=record.rpc_code,
            )
        builtin_by_kind: dict[ArtifactParseFailureKind, type[Exception]] = {
            ArtifactParseFailureKind.INDEX: IndexError,
            ArtifactParseFailureKind.KEY: KeyError,
            ArtifactParseFailureKind.TYPE: TypeError,
            ArtifactParseFailureKind.VALUE: ValueError,
        }
        failure_type = builtin_by_kind.get(record.kind)
        if failure_type is None:
            raise RuntimeError(f"unsupported artifact parse failure kind: {record.kind.value}")
        return failure_type(record.message)

    @classmethod
    def _raise_parse_error(
        cls,
        artifact_type: str,
        *,
        artifact_id: str | None,
        details: str,
        failure: ArtifactParseFailureRecord | None,
    ) -> None:
        cause = None if failure is None else cls._project_parse_failure(failure)
        error = ArtifactParseError(
            artifact_type,
            artifact_id=artifact_id,
            details=details,
            cause=cause,
        )
        if cause is None:
            raise error
        raise error from cause

    async def _list_representations(
        self,
        notebook_id: str,
    ) -> tuple[ArtifactRepresentationRecord, ...]:
        if self._backend is None:
            raise RuntimeError("Artifact representation lookup requires a semantic backend")
        result = await self._backend.invoke(
            ARTIFACT_DOWNLOAD_DEF,
            ArtifactDownloadInput(notebook_id, "catalog"),
            deadline=None,
        )
        return result.representations

    async def _list_mind_maps(
        self,
        notebook_id: str,
    ) -> tuple[MindMapRepresentationRecord, ...]:
        if self._backend is None:
            raise RuntimeError("Mind-map representation lookup requires a semantic backend")
        result = await self._backend.invoke(
            ARTIFACT_DOWNLOAD_DEF,
            ArtifactDownloadInput(notebook_id, "mind_maps"),
            deadline=None,
        )
        return result.mind_maps

    async def _get_content(
        self,
        notebook_id: str,
        artifact_id: str,
        action: str,
    ) -> str | None:
        if self._backend is None:
            raise RuntimeError("Interactive representation lookup requires a semantic backend")
        result = await self._backend.invoke(
            ARTIFACT_DOWNLOAD_DEF,
            ArtifactDownloadInput(notebook_id, action, artifact_id),
            deadline=None,
        )
        return result.content

    @staticmethod
    def _select(
        candidates: Sequence[ArtifactRepresentationRecord],
        artifact_id: str | None,
        family: str,
        type_name: str,
        no_result_error_key: str,
    ) -> ArtifactRepresentationRecord:
        completed = [
            item
            for item in candidates
            if item.artifact.family == family and item.artifact.status == "completed"
        ]
        if artifact_id:
            match = next((item for item in completed if item.artifact.id == artifact_id), None)
            if match is None:
                raise ArtifactNotReadyError(
                    type_name.lower().replace(" ", "_"),
                    artifact_id=artifact_id,
                )
            return match
        if not completed:
            raise ArtifactNotReadyError(no_result_error_key)
        completed.sort(
            key=lambda item: (
                item.artifact.created_at.timestamp() if item.artifact.created_at else 0
            ),
            reverse=True,
        )
        selected, *_ = completed
        return selected

    async def download_audio(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        representations: Sequence[ArtifactRepresentationRecord] | None = None,
    ) -> str:
        candidates = (
            representations
            if representations is not None
            else await self._list_representations(notebook_id)
        )
        selected = self._select(candidates, artifact_id, "audio", "Audio", "audio")
        if selected.parse_error is not None:
            self._raise_parse_error(
                "audio",
                artifact_id=artifact_id,
                details=f"Failed to parse structure: {selected.parse_error}",
                failure=selected.parse_failure,
            )
        if not selected.audio_url:
            raise ArtifactParseError(
                "audio",
                artifact_id=artifact_id,
                details="Could not extract download URL from artifact metadata",
            )
        return await self.download_url(selected.audio_url, output_path)

    async def download_video(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        representations: Sequence[ArtifactRepresentationRecord] | None = None,
    ) -> str:
        candidates = (
            representations
            if representations is not None
            else await self._list_representations(notebook_id)
        )
        selected = self._select(
            candidates,
            artifact_id,
            "video",
            "Video",
            "video_overview",
        )
        if selected.parse_error is not None:
            self._raise_parse_error(
                "video_artifact",
                artifact_id=artifact_id,
                details=f"Failed to parse structure: {selected.parse_error}",
                failure=selected.parse_failure,
            )
        if not selected.video_url:
            raise ArtifactParseError(
                "video_artifact",
                artifact_id=artifact_id,
                details="Could not extract download URL from artifact metadata",
            )
        return await self.download_url(selected.video_url, output_path)

    async def download_infographic(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        representations: Sequence[ArtifactRepresentationRecord] | None = None,
    ) -> str:
        candidates = (
            representations
            if representations is not None
            else await self._list_representations(notebook_id)
        )
        selected = self._select(
            candidates,
            artifact_id,
            "infographic",
            "Infographic",
            "infographic",
        )
        if selected.parse_error is not None:
            self._raise_parse_error(
                "infographic",
                artifact_id=artifact_id,
                details=f"Failed to parse structure: {selected.parse_error}",
                failure=selected.parse_failure,
            )
        if not selected.infographic_url:
            raise ArtifactParseError(
                "infographic",
                artifact_id=artifact_id,
                details="Could not find metadata",
            )
        return await self.download_url(selected.infographic_url, output_path)

    async def download_slide_deck(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "pdf",
        *,
        representations: Sequence[ArtifactRepresentationRecord] | None = None,
    ) -> str:
        if output_format not in {"pdf", "pptx"}:
            raise ValidationError(f"Invalid format '{output_format}'. Must be 'pdf' or 'pptx'.")
        candidates = (
            representations
            if representations is not None
            else await self._list_representations(notebook_id)
        )
        selected = self._select(
            candidates,
            artifact_id,
            "slide_deck",
            "Slide deck",
            "slide_deck",
        )
        if selected.parse_error is not None:
            self._raise_parse_error(
                "slide_deck",
                artifact_id=artifact_id,
                details=f"Failed to parse structure: {selected.parse_error}",
                failure=selected.parse_failure,
            )
        url = (
            selected.slide_deck_pptx_url if output_format == "pptx" else selected.slide_deck_pdf_url
        )
        if not url:
            details = (
                "PPTX URL not available in artifact data"
                if output_format == "pptx"
                else f"Could not find {output_format.upper()} download URL"
            )
            raise ArtifactDownloadError("slide_deck", details=details)
        return await self.download_url(url, output_path)

    async def download_report(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        representations: Sequence[ArtifactRepresentationRecord] | None = None,
    ) -> str:
        candidates = (
            representations
            if representations is not None
            else await self._list_representations(notebook_id)
        )
        selected = self._select(candidates, artifact_id, "report", "Report", "report")
        if selected.parse_error is not None:
            self._raise_parse_error(
                "report",
                artifact_id=artifact_id,
                details=f"Failed to parse structure: {selected.parse_error}",
                failure=selected.parse_failure,
            )
        if not isinstance(selected.report_markdown, str):
            raise ArtifactParseError(
                "report_content",
                artifact_id=artifact_id,
                details="Invalid structure",
            )
        return await self._serialization.write_text(output_path, selected.report_markdown)

    async def download_data_table(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        representations: Sequence[ArtifactRepresentationRecord] | None = None,
    ) -> str:
        candidates = (
            representations
            if representations is not None
            else await self._list_representations(notebook_id)
        )
        selected = self._select(
            candidates,
            artifact_id,
            "data_table",
            "Data table",
            "data_table",
        )
        if selected.parse_error is not None:
            self._raise_parse_error(
                "data_table",
                artifact_id=artifact_id,
                details=f"Failed to parse structure: {selected.parse_error}",
                failure=selected.parse_failure,
            )
        if selected.data_table_error is not None:
            self._raise_parse_error(
                "data_table",
                artifact_id=artifact_id,
                details=selected.data_table_error,
                failure=selected.data_table_failure,
            )
        return await self._serialization.write_csv(
            output_path,
            list(selected.data_table_headers),
            [list(row) for row in selected.data_table_rows],
        )

    async def download_interactive(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None,
        output_format: str,
        family: str,
        *,
        artifacts: Sequence[ArtifactRecord] | None = None,
    ) -> str:
        valid_formats = ("json", "markdown", "html")
        if output_format not in valid_formats:
            raise ValidationError(
                f"Invalid output_format: {output_format!r}. Use one of: {', '.join(valid_formats)}"
            )
        records = (
            tuple(artifacts)
            if artifacts is not None
            else tuple(item.artifact for item in await self._list_representations(notebook_id))
        )
        completed = [
            record for record in records if record.family == family and record.status == "completed"
        ]
        if not completed:
            raise ArtifactNotReadyError(family)
        completed.sort(
            key=lambda item: item.created_at.timestamp() if item.created_at else 0,
            reverse=True,
        )
        if artifact_id:
            artifact = next((item for item in completed if item.id == artifact_id), None)
            if artifact is None:
                raise ArtifactNotFoundError(artifact_id, artifact_type=family)
        else:
            artifact = next(iter(completed))
        html_content = await self._get_content(
            notebook_id,
            artifact.id,
            "interactive_html",
        )
        if not html_content:
            raise ArtifactDownloadError(family, details="Failed to fetch content")
        try:
            app_data = _extract_app_data(html_content)
        except json.JSONDecodeError as exc:
            raise ArtifactParseError(
                family,
                details=f"Failed to parse content: {exc}",
                cause=exc,
            ) from exc
        title = artifact.title or ("Untitled Quiz" if family == "quiz" else "Untitled Flashcards")
        content = _format_interactive_content(
            app_data,
            title,
            output_format,
            html_content,
            family == "quiz",
        )
        return await self._serialization.write_text(output_path, content)

    async def download_mind_map(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        *,
        mind_maps: Sequence[MindMapRepresentationRecord] | None = None,
        representations: Sequence[ArtifactRepresentationRecord] | None = None,
    ) -> str:
        note_maps = (
            tuple(mind_maps) if mind_maps is not None else await self._list_mind_maps(notebook_id)
        )
        json_string: str | None = None
        if artifact_id:
            note_map = next((item for item in note_maps if item.id == artifact_id), None)
            if note_map is not None:
                json_string = note_map.content
            else:
                studio = (
                    representations
                    if representations is not None
                    else await self._list_representations(notebook_id)
                )
                interactive = next(
                    (
                        item
                        for item in studio
                        if item.artifact.id == artifact_id
                        and item.artifact.variant == "interactive_mind_map"
                    ),
                    None,
                )
                if interactive is not None:
                    json_string = await self._get_content(
                        notebook_id,
                        artifact_id,
                        "mind_map_tree",
                    )
                    if json_string is None:
                        raise ArtifactNotReadyError("mind_map")
                elif not note_maps:
                    raise ArtifactNotReadyError("mind_map")
                else:
                    raise ArtifactNotFoundError(artifact_id, artifact_type="mind_map")
        else:
            if not note_maps:
                raise ArtifactNotReadyError("mind_map")
            json_string = next(iter(note_maps)).content
        if json_string is None:
            raise ArtifactParseError("mind_map_content", details="Invalid structure")
        try:
            return await self._serialization.write_json_string(output_path, json_string)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ArtifactParseError(
                "mind_map",
                details=f"Failed to parse structure: {exc}",
                cause=exc,
            ) from exc

    async def download_batch(self, values: list[tuple[str, str]]) -> DownloadResult:
        return await self._remote.download_batch(values)

    async def download_url(self, url: str, output_path: str) -> str:
        return await self._remote.download(url, output_path)


__all__ = ["ArtifactRepresentationService"]
