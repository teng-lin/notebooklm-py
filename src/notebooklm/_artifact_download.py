"""Artifact download operations.

Provides the ArtifactDownloader class for downloading AI-generated artifacts
including Audio Overviews, Video Overviews, Reports, Quizzes, Flashcards,
Infographics, Slide Decks, Data Tables, and Mind Maps.
"""

import asyncio
import builtins
import csv
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ._artifact_helpers import (
    _extract_app_data,
    _format_flashcards_markdown,
    _format_quiz_markdown,
    _parse_data_table,
)
from ._core import ClientCore
from .auth import load_httpx_cookies
from .exceptions import ValidationError
from .rpc import ArtifactStatus, ArtifactTypeCode, RPCMethod
from .types import (
    Artifact,
    ArtifactDownloadError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
)

if TYPE_CHECKING:
    from ._notes import NotesAPI

logger = logging.getLogger(__name__)


def _filter_completed_artifacts(
    artifacts_data: builtins.list[Any],
    type_code: ArtifactTypeCode,
    min_length: int = 5,
) -> builtins.list[Any]:
    """Filter artifacts by type and completed status.

    Args:
        artifacts_data: Raw artifact data from API.
        type_code: The artifact type to filter for.
        min_length: Minimum required list length for valid artifacts.

    Returns:
        List of matching artifact data.
    """
    return [
        a
        for a in artifacts_data
        if isinstance(a, list)
        and len(a) > min_length
        and a[2] == type_code
        and a[4] == ArtifactStatus.COMPLETED
    ]


def _select_by_id_or_first(
    candidates: builtins.list[Any],
    artifact_id: str | None,
    artifact_type: str,
) -> Any:
    """Select an artifact by ID or return the first available.

    Args:
        candidates: List of candidate artifacts.
        artifact_id: Specific artifact ID to select, or None for first.
        artifact_type: Type name for error messages.

    Returns:
        Selected artifact data.

    Raises:
        ArtifactNotReadyError: If artifact not found or no candidates.
    """
    if artifact_id:
        artifact = next((a for a in candidates if a[0] == artifact_id), None)
        if not artifact:
            raise ArtifactNotReadyError(artifact_type, artifact_id=artifact_id)
        return artifact

    if not candidates:
        raise ArtifactNotReadyError(artifact_type)

    return candidates[0]


class ArtifactDownloader:
    """Handles artifact download operations.

    This class encapsulates all artifact download logic. It uses callback
    injection for methods that need to access the facade (list_quizzes,
    list_flashcards, _list_raw) to avoid circular imports.

    Usage:
        downloader = ArtifactDownloader(
            core=core,
            notes_api=notes_api,
            list_raw_fn=artifacts_api._list_raw,
            list_quizzes_fn=artifacts_api.list_quizzes,
            list_flashcards_fn=artifacts_api.list_flashcards,
        )
        path = await downloader.download_audio(notebook_id, "output.mp4")
    """

    def __init__(
        self,
        core: ClientCore,
        notes_api: "NotesAPI",
        list_raw_fn: Callable[[str], Awaitable[builtins.list[Any]]],
        list_quizzes_fn: Callable[[str], Awaitable[builtins.list[Artifact]]],
        list_flashcards_fn: Callable[[str], Awaitable[builtins.list[Artifact]]],
    ):
        """Initialize the artifact downloader.

        Args:
            core: The core client infrastructure for RPC calls.
            notes_api: The notes API for mind map downloads.
            list_raw_fn: Callback to get raw artifact list data.
            list_quizzes_fn: Callback to list quiz artifacts.
            list_flashcards_fn: Callback to list flashcard artifacts.
        """
        self._core = core
        self._notes = notes_api
        self._list_raw = list_raw_fn
        self._list_quizzes = list_quizzes_fn
        self._list_flashcards = list_flashcards_fn

    # =========================================================================
    # Media Downloads (Audio, Video, Infographic, Slides)
    # =========================================================================

    async def download_audio(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download an Audio Overview to a file.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the audio file (MP4/MP3).
            artifact_id: Specific artifact ID, or uses first completed audio.

        Returns:
            The output path.
        """
        artifacts_data = await self._list_raw(notebook_id)
        candidates = _filter_completed_artifacts(artifacts_data, ArtifactTypeCode.AUDIO)
        audio_art = _select_by_id_or_first(candidates, artifact_id, "audio")

        # Extract URL from metadata[6][5]
        try:
            metadata = audio_art[6]
            if not isinstance(metadata, list) or len(metadata) <= 5:
                raise ArtifactParseError(
                    "audio",
                    artifact_id=artifact_id,
                    details="Invalid audio metadata structure",
                )

            media_list = metadata[5]
            if not isinstance(media_list, list) or len(media_list) == 0:
                raise ArtifactParseError(
                    "audio",
                    artifact_id=artifact_id,
                    details="No media URLs found",
                )

            url = None
            for item in media_list:
                if isinstance(item, list) and len(item) > 2 and item[2] == "audio/mp4":
                    url = item[0]
                    break

            if not url and len(media_list) > 0 and isinstance(media_list[0], list):
                url = media_list[0][0]

            if not url:
                raise ArtifactDownloadError(
                    "audio",
                    artifact_id=artifact_id,
                    details="Could not extract download URL",
                )

            return await self._download_url(url, output_path)

        except (IndexError, TypeError) as e:
            raise ArtifactParseError(
                "audio",
                artifact_id=artifact_id,
                details=f"Failed to parse audio artifact structure: {e}",
                cause=e,
            ) from e

    async def download_video(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download a Video Overview to a file.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the video file (MP4).
            artifact_id: Specific artifact ID, or uses first completed video.

        Returns:
            The output path.
        """
        artifacts_data = await self._list_raw(notebook_id)
        candidates = _filter_completed_artifacts(artifacts_data, ArtifactTypeCode.VIDEO)
        video_art = _select_by_id_or_first(candidates, artifact_id, "video")

        # Extract URL from metadata[8]
        try:
            if len(video_art) <= 8:
                raise ArtifactParseError("video_artifact", details="Invalid structure")

            metadata = video_art[8]
            if not isinstance(metadata, list):
                raise ArtifactParseError("video_metadata", details="Invalid structure")

            media_list = None
            for item in metadata:
                if (
                    isinstance(item, list)
                    and len(item) > 0
                    and isinstance(item[0], list)
                    and len(item[0]) > 0
                    and isinstance(item[0][0], str)
                    and item[0][0].startswith("http")
                ):
                    media_list = item
                    break

            if not media_list:
                raise ArtifactParseError("media", details="No media URLs found")

            url = None
            for item in media_list:
                if isinstance(item, list) and len(item) > 2 and item[2] == "video/mp4":
                    url = item[0]
                    if item[1] == 4:
                        break

            if not url and len(media_list) > 0:
                url = media_list[0][0]

            if not url:
                raise ArtifactDownloadError("media", details="Could not extract download URL")

            return await self._download_url(url, output_path)

        except (IndexError, TypeError) as e:
            raise ArtifactParseError(
                "video_artifact", details=f"Failed to parse structure: {e}", cause=e
            ) from e

    async def download_infographic(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download an Infographic to a file.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the image file (PNG).
            artifact_id: Specific artifact ID, or uses first completed infographic.

        Returns:
            The output path.
        """
        artifacts_data = await self._list_raw(notebook_id)
        candidates = _filter_completed_artifacts(artifacts_data, ArtifactTypeCode.INFOGRAPHIC)
        info_art = _select_by_id_or_first(candidates, artifact_id, "infographic")

        # Extract URL from metadata
        try:
            metadata = None
            for item in reversed(info_art):
                if isinstance(item, list) and len(item) > 0 and isinstance(item[0], list):
                    if len(item) > 2 and isinstance(item[2], list) and len(item[2]) > 0:
                        content_list = item[2]
                        if isinstance(content_list[0], list) and len(content_list[0]) > 1:
                            img_data = content_list[0][1]
                            if (
                                isinstance(img_data, list)
                                and len(img_data) > 0
                                and isinstance(img_data[0], str)
                                and img_data[0].startswith("http")
                            ):
                                metadata = item
                                break

            if not metadata:
                raise ArtifactParseError("infographic", details="Could not find metadata")

            url = metadata[2][0][1][0]
            return await self._download_url(url, output_path)

        except (IndexError, TypeError) as e:
            raise ArtifactParseError(
                "infographic", details=f"Failed to parse structure: {e}", cause=e
            ) from e

    async def download_slide_deck(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download a slide deck as a PDF file.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the PDF file.
            artifact_id: Specific artifact ID, or uses first completed slide deck.

        Returns:
            The output path.
        """
        artifacts_data = await self._list_raw(notebook_id)
        candidates = _filter_completed_artifacts(artifacts_data, ArtifactTypeCode.SLIDE_DECK)
        slide_art = _select_by_id_or_first(candidates, artifact_id, "slide_deck")

        # Extract PDF URL from metadata at index 16, position 3
        # Structure: artifact[16] = [config, title, slides_list, pdf_url]
        try:
            if len(slide_art) <= 16:
                raise ArtifactParseError("slide_deck_artifact", details="Invalid structure")

            metadata = slide_art[16]
            if not isinstance(metadata, list) or len(metadata) < 4:
                raise ArtifactParseError("slide_deck_metadata", details="Invalid structure")

            pdf_url = metadata[3]
            if not isinstance(pdf_url, str) or not pdf_url.startswith("http"):
                raise ArtifactDownloadError("slide_deck", details="Could not find PDF download URL")

            return await self._download_url(pdf_url, output_path)

        except (IndexError, TypeError) as e:
            raise ArtifactParseError(
                "slide_deck", details=f"Failed to parse structure: {e}", cause=e
            ) from e

    # =========================================================================
    # Document Downloads (Report, Data Table, Mind Map)
    # =========================================================================

    async def download_report(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a report artifact as markdown.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the markdown file.
            artifact_id: Specific artifact ID, or uses first completed report.

        Returns:
            The output path where the file was saved.
        """
        artifacts_data = await self._list_raw(notebook_id)
        candidates = _filter_completed_artifacts(
            artifacts_data, ArtifactTypeCode.REPORT, min_length=7
        )
        report_art = _select_by_id_or_first(candidates, artifact_id, "report")

        try:
            content_wrapper = report_art[7]
            markdown_content = (
                content_wrapper[0]
                if isinstance(content_wrapper, list) and content_wrapper
                else content_wrapper
            )

            if not isinstance(markdown_content, str):
                raise ArtifactParseError("report_content", details="Invalid structure")

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(markdown_content, encoding="utf-8")
            return str(output)

        except (IndexError, TypeError) as e:
            raise ArtifactParseError(
                "report", details=f"Failed to parse structure: {e}", cause=e
            ) from e

    async def download_mind_map(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a mind map as JSON.

        Mind maps are stored in the notes system, not the regular artifacts list.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the JSON file.
            artifact_id: Specific mind map ID (note ID), or uses first available.

        Returns:
            The output path where the file was saved.
        """
        mind_maps = await self._notes.list_mind_maps(notebook_id)
        if not mind_maps:
            raise ArtifactNotReadyError("mind_map")

        if artifact_id:
            mind_map = next((mm for mm in mind_maps if mm[0] == artifact_id), None)
            if not mind_map:
                raise ArtifactNotFoundError(artifact_id, artifact_type="mind_map")
        else:
            mind_map = mind_maps[0]

        try:
            json_string = mind_map[1][1]
            if not isinstance(json_string, str):
                raise ArtifactParseError("mind_map_content", details="Invalid structure")

            json_data = json.loads(json_string)

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")
            return str(output)

        except (IndexError, TypeError, json.JSONDecodeError) as e:
            raise ArtifactParseError(
                "mind_map", details=f"Failed to parse structure: {e}", cause=e
            ) from e

    async def download_data_table(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a data table as CSV.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the CSV file.
            artifact_id: Specific artifact ID, or uses first completed data table.

        Returns:
            The output path where the file was saved.
        """
        artifacts_data = await self._list_raw(notebook_id)
        candidates = _filter_completed_artifacts(
            artifacts_data, ArtifactTypeCode.DATA_TABLE, min_length=18
        )
        table_art = _select_by_id_or_first(candidates, artifact_id, "data_table")

        try:
            raw_data = table_art[18]
            headers, rows = _parse_data_table(raw_data)

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)

            with output.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)

            return str(output)

        except (IndexError, TypeError, ValueError) as e:
            raise ArtifactParseError(
                "data_table", details=f"Failed to parse structure: {e}", cause=e
            ) from e

    # =========================================================================
    # Interactive Downloads (Quiz, Flashcards)
    # =========================================================================

    async def download_quiz(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
    ) -> str:
        """Download quiz questions.

        Args:
            notebook_id: Notebook ID.
            output_path: Output file path.
            artifact_id: Specific quiz artifact ID (optional).
            output_format: Output format - json, markdown, or html.

        Returns:
            Path to downloaded file.

        Raises:
            ValueError: If no completed quiz artifact found.
        """
        return await self._download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "quiz"
        )

    async def download_flashcards(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
    ) -> str:
        """Download flashcard deck.

        Args:
            notebook_id: Notebook ID.
            output_path: Output file path.
            artifact_id: Specific flashcard artifact ID (optional).
            output_format: Output format - json, markdown, or html.

        Returns:
            Path to downloaded file.

        Raises:
            ValueError: If no completed flashcard artifact found.
        """
        return await self._download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "flashcards"
        )

    # =========================================================================
    # Private Helpers
    # =========================================================================

    async def _get_artifact_content(self, notebook_id: str, artifact_id: str) -> str | None:
        """Fetch artifact HTML content for quiz/flashcard types."""
        result = await self._core.rpc_call(
            RPCMethod.GET_INTERACTIVE_HTML,
            [artifact_id],
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        # Response is wrapped: result[0] contains the artifact data
        if result and isinstance(result, list) and len(result) > 0:
            data = result[0]
            if isinstance(data, list) and len(data) > 9 and data[9]:
                return data[9][0]  # HTML content
        return None

    async def _download_interactive_artifact(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None,
        output_format: str,
        artifact_type: str,
    ) -> str:
        """Download quiz or flashcard artifact.

        Args:
            notebook_id: Notebook ID.
            output_path: Output file path.
            artifact_id: Specific artifact ID (optional).
            output_format: Output format - json, markdown, or html.
            artifact_type: Either "quiz" or "flashcards".

        Returns:
            Path to downloaded file.

        Raises:
            ValueError: If no completed artifact found or invalid output_format.
        """
        # Validate output format
        valid_formats = ("json", "markdown", "html")
        if output_format not in valid_formats:
            raise ValidationError(
                f"Invalid output_format: {output_format!r}. Use one of: {', '.join(valid_formats)}"
            )

        # Type-specific configuration
        is_quiz = artifact_type == "quiz"
        default_title = "Untitled Quiz" if is_quiz else "Untitled Flashcards"

        # Fetch and filter artifacts using callbacks
        artifacts = (
            await self._list_quizzes(notebook_id)
            if is_quiz
            else await self._list_flashcards(notebook_id)
        )
        completed = [a for a in artifacts if a.is_completed]
        if not completed:
            raise ArtifactNotReadyError(artifact_type)

        # Sort by creation date to ensure we get the latest by default
        completed.sort(key=lambda a: a.created_at.timestamp() if a.created_at else 0, reverse=True)

        # Select artifact
        if artifact_id:
            artifact = next((a for a in completed if a.id == artifact_id), None)
            if not artifact:
                raise ArtifactNotFoundError(artifact_id, artifact_type=artifact_type)
        else:
            artifact = completed[0]

        # Fetch and parse HTML content
        html_content = await self._get_artifact_content(notebook_id, artifact.id)
        if not html_content:
            raise ArtifactDownloadError(artifact_type, details="Failed to fetch content")

        try:
            app_data = _extract_app_data(html_content)
        except (ValueError, json.JSONDecodeError) as e:
            raise ArtifactParseError(
                artifact_type, details=f"Failed to parse content: {e}", cause=e
            ) from e

        # Format output
        title = artifact.title or default_title
        content = self._format_interactive_content(
            app_data, title, output_format, html_content, is_quiz
        )

        # Create parent directories and write file
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        def _write_file() -> None:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)

        await asyncio.to_thread(_write_file)
        return output_path

    def _format_interactive_content(
        self,
        app_data: dict,
        title: str,
        output_format: str,
        html_content: str,
        is_quiz: bool,
    ) -> str:
        """Format quiz or flashcard content for output.

        Args:
            app_data: Parsed data from HTML.
            title: Artifact title.
            output_format: Output format - json, markdown, or html.
            html_content: Original HTML content.
            is_quiz: True for quiz, False for flashcards.

        Returns:
            Formatted content string.
        """
        if output_format == "html":
            return html_content

        if is_quiz:
            questions = app_data.get("quiz", [])
            if output_format == "markdown":
                return _format_quiz_markdown(title, questions)
            return json.dumps({"title": title, "questions": questions}, indent=2)

        cards = app_data.get("flashcards", [])
        if output_format == "markdown":
            return _format_flashcards_markdown(title, cards)
        normalized = [{"front": c.get("f", ""), "back": c.get("b", "")} for c in cards]
        return json.dumps({"title": title, "cards": normalized}, indent=2)

    async def _download_url(self, url: str, output_path: str) -> str:
        """Download a file from URL using httpx with proper cookie handling.

        Args:
            url: URL to download from.
            output_path: Path to save the file.

        Returns:
            The output path on success.

        Raises:
            ValueError: If download fails or authentication expired.
        """
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Load cookies with domain info for cross-domain redirect handling
        cookies = load_httpx_cookies()

        async with httpx.AsyncClient(
            cookies=cookies,
            follow_redirects=True,
            timeout=httpx.Timeout(10.0, read=60.0),
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type:
                raise ArtifactDownloadError(
                    "media",
                    details="Download failed: received HTML instead of media file. "
                    "Authentication may have expired. Run 'notebooklm login'.",
                )

            output_file.write_bytes(response.content)
            logger.debug("Downloaded %s (%d bytes)", url[:60], len(response.content))
            return output_path

    async def _download_urls_batch(
        self, urls_and_paths: builtins.list[tuple[str, str]]
    ) -> builtins.list[str]:
        """Download multiple files concurrently using httpx with proper cookie handling.

        Args:
            urls_and_paths: List of (url, output_path) tuples.

        Returns:
            List of successfully downloaded output paths.
        """
        cookies = load_httpx_cookies()

        async def _download_one(
            client: httpx.AsyncClient, url: str, output_path: str
        ) -> str | None:
            """Helper to download a single file and handle errors."""
            try:
                response = await client.get(url)
                response.raise_for_status()

                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type:
                    raise ArtifactDownloadError(
                        "media", details="Received HTML instead of media file"
                    )

                output_file = Path(output_path)
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_bytes(response.content)
                logger.debug("Downloaded %s (%d bytes)", url[:60], len(response.content))
                return output_path
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("Download failed for %s: %s", url[:60], e)
                return None

        async with httpx.AsyncClient(
            cookies=cookies,
            follow_redirects=True,
            timeout=httpx.Timeout(10.0, read=60.0),
        ) as client:
            tasks = [_download_one(client, url, path) for url, path in urls_and_paths]
            results = await asyncio.gather(*tasks)

        return [path for path in results if path is not None]
