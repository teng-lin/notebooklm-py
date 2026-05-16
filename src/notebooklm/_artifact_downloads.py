"""Private artifact download service implementation."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .exceptions import ValidationError
from .rpc import ArtifactTypeCode
from .types import (
    ArtifactDownloadError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
)

logger = logging.getLogger(__name__)

_TRUSTED_DOWNLOAD_DOMAINS = (".google.com", ".googleusercontent.com", ".googleapis.com")


@dataclass(frozen=False)
class DownloadResult:
    """Outcome of a multi-URL download batch.

    Replaces the v0 silent-partial-failure behavior where `_download_urls_batch`
    returned only successful paths. Callers can now distinguish "all succeeded"
    from "partial" via the properties below.

    `succeeded`: paths that downloaded cleanly (matches existing list[str] shape).
    `failed`: (url, exception) tuples for transient httpx / ValueError failures.
    """

    succeeded: list[str] = field(default_factory=list)
    failed: list[tuple[str, Exception]] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        return not self.failed

    @property
    def partial(self) -> bool:
        return bool(self.succeeded) and bool(self.failed)


def _artifact_seams() -> Any:
    """Return the facade module that legacy tests patch.

    Download code deliberately resolves selected dependencies through
    ``notebooklm._artifacts`` at call time so existing private monkeypatch
    targets keep working after the extraction.
    """
    try:
        return sys.modules["notebooklm._artifacts"]
    except KeyError as e:
        raise RuntimeError("notebooklm._artifacts must be imported before downloads run") from e


def _load_httpx_cookies(storage_path: Any) -> Any:
    return _artifact_seams().load_httpx_cookies(path=storage_path)


def _is_trusted_download_host(netloc: str) -> bool:
    return any(
        netloc == domain.lstrip(".") or netloc.endswith(domain)
        for domain in _TRUSTED_DOWNLOAD_DOMAINS
    )


class ArtifactDownloadService:
    """Download operations extracted from :class:`ArtifactsAPI`."""

    def __init__(self, api: Any):
        self._api = api

    async def download_audio(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download an Audio Overview to a file."""
        api = self._api
        artifacts_data = await api._list_raw(notebook_id)

        audio_art = api._select_artifact(
            artifacts_data,
            artifact_id,
            "Audio",
            "audio",
            type_code=ArtifactTypeCode.AUDIO,
        )

        url = _artifact_seams()._extract_artifact_url(audio_art, ArtifactTypeCode.AUDIO.value)
        if not url:
            raise ArtifactParseError(
                "audio",
                artifact_id=artifact_id,
                details="Could not extract download URL from artifact metadata",
            )

        return await api._download_url(url, output_path)

    async def download_video(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download a Video Overview to a file."""
        api = self._api
        artifacts_data = await api._list_raw(notebook_id)

        # Note: distinct error keys preserved — specific-ID miss raises
        # "video" (from type_name="Video"); empty-list raises
        # "video_overview" (from type_name_lower).
        video_art = api._select_artifact(
            artifacts_data,
            artifact_id,
            "Video",
            "video_overview",
            type_code=ArtifactTypeCode.VIDEO,
        )

        url = _artifact_seams()._extract_artifact_url(video_art, ArtifactTypeCode.VIDEO.value)
        if not url:
            raise ArtifactParseError(
                "video_artifact",
                artifact_id=artifact_id,
                details="Could not extract download URL from artifact metadata",
            )

        return await api._download_url(url, output_path)

    async def download_infographic(
        self, notebook_id: str, output_path: str, artifact_id: str | None = None
    ) -> str:
        """Download an Infographic to a file."""
        api = self._api
        artifacts_data = await api._list_raw(notebook_id)

        info_art = api._select_artifact(
            artifacts_data,
            artifact_id,
            "Infographic",
            "infographic",
            type_code=ArtifactTypeCode.INFOGRAPHIC,
        )

        try:
            url = _artifact_seams()._extract_artifact_url(
                info_art, ArtifactTypeCode.INFOGRAPHIC.value
            )
            if not url:
                raise ArtifactParseError("infographic", details="Could not find metadata")
            return await api._download_url(url, output_path)

        except (IndexError, TypeError) as e:
            raise ArtifactParseError(
                "infographic", details=f"Failed to parse structure: {e}", cause=e
            ) from e

    async def download_slide_deck(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "pdf",
    ) -> str:
        """Download a slide deck as PDF or PPTX."""
        api = self._api
        if output_format not in ("pdf", "pptx"):
            raise ValidationError(f"Invalid format '{output_format}'. Must be 'pdf' or 'pptx'.")

        artifacts_data = await api._list_raw(notebook_id)

        slide_art = api._select_artifact(
            artifacts_data,
            artifact_id,
            "Slide deck",
            "slide_deck",
            type_code=ArtifactTypeCode.SLIDE_DECK,
        )

        # Extract download URL from metadata at index 16.
        # Structure: artifact[16] = [config, title, slides_list, pdf_url, pptx_url]
        try:
            if len(slide_art) <= 16:
                raise ArtifactParseError("slide_deck_artifact", details="Invalid structure")

            metadata = slide_art[16]
            if not isinstance(metadata, list):
                raise ArtifactParseError("slide_deck_metadata", details="Invalid structure")

            if output_format == "pptx":
                if len(metadata) < 5:
                    raise ArtifactDownloadError(
                        "slide_deck", details="PPTX URL not available in artifact data"
                    )
                url = metadata[4]
            else:
                if len(metadata) < 4:
                    raise ArtifactParseError("slide_deck_metadata", details="Invalid structure")
                url = metadata[3]

            if not isinstance(url, str) or not url.startswith("http"):
                raise ArtifactDownloadError(
                    "slide_deck",
                    details=f"Could not find {output_format.upper()} download URL",
                )

        except (IndexError, TypeError) as e:
            raise ArtifactParseError(
                "slide_deck", details=f"Failed to parse structure: {e}", cause=e
            ) from e

        return await api._download_url(url, output_path)

    async def download_interactive_artifact(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None,
        output_format: str,
        artifact_type: str,
    ) -> str:
        """Download quiz or flashcard artifact."""
        api = self._api
        valid_formats = ("json", "markdown", "html")
        if output_format not in valid_formats:
            raise ValidationError(
                f"Invalid output_format: {output_format!r}. Use one of: {', '.join(valid_formats)}"
            )

        is_quiz = artifact_type == "quiz"
        default_title = "Untitled Quiz" if is_quiz else "Untitled Flashcards"

        artifacts = (
            await api.list_quizzes(notebook_id)
            if is_quiz
            else await api.list_flashcards(notebook_id)
        )
        completed = [a for a in artifacts if a.is_completed]
        if not completed:
            raise ArtifactNotReadyError(artifact_type)

        completed.sort(key=lambda a: a.created_at.timestamp() if a.created_at else 0, reverse=True)

        if artifact_id:
            artifact = next((a for a in completed if a.id == artifact_id), None)
            if not artifact:
                raise ArtifactNotFoundError(artifact_id, artifact_type=artifact_type)
        else:
            artifact = completed[0]

        html_content = await api._get_artifact_content(notebook_id, artifact.id)
        if not html_content:
            raise ArtifactDownloadError(artifact_type, details="Failed to fetch content")

        json_module = _artifact_seams().json
        try:
            app_data = _artifact_seams()._extract_app_data(html_content)
        except (ValueError, json_module.JSONDecodeError) as e:
            raise ArtifactParseError(
                artifact_type, details=f"Failed to parse content: {e}", cause=e
            ) from e

        title = artifact.title or default_title
        content = api._format_interactive_content(
            app_data, title, output_format, html_content, is_quiz
        )

        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        def _write_file() -> None:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)

        await asyncio.to_thread(_write_file)
        return output_path

    async def download_report(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a report artifact as markdown."""
        api = self._api
        artifacts_data = await api._list_raw(notebook_id)

        report_art = api._select_artifact(
            artifacts_data,
            artifact_id,
            "Report",
            "report",
            type_code=ArtifactTypeCode.REPORT,
        )

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

            def _write_markdown() -> None:
                output.write_text(markdown_content, encoding="utf-8")

            await asyncio.to_thread(_write_markdown)
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
        """Download a mind map as JSON."""
        api = self._api
        mind_maps = await _artifact_seams()._mind_map.list_mind_maps(api._core, notebook_id)
        if not mind_maps:
            raise ArtifactNotReadyError("mind_map")

        if artifact_id:
            mind_map = next((mm for mm in mind_maps if mm[0] == artifact_id), None)
            if not mind_map:
                raise ArtifactNotFoundError(artifact_id, artifact_type="mind_map")
        else:
            mind_map = mind_maps[0]

        json_module = _artifact_seams().json
        try:
            json_string = _artifact_seams()._mind_map.extract_content(mind_map)
            if json_string is None:
                raise ArtifactParseError("mind_map_content", details="Invalid structure")

            json_data = json_module.loads(json_string)

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)

            def _write_json() -> None:
                with output.open("w", encoding="utf-8") as f:
                    _artifact_seams().json.dump(json_data, f, indent=2, ensure_ascii=False)

            await asyncio.to_thread(_write_json)
            return str(output)

        except (IndexError, TypeError, json_module.JSONDecodeError) as e:
            raise ArtifactParseError(
                "mind_map", details=f"Failed to parse structure: {e}", cause=e
            ) from e

    async def download_data_table(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
    ) -> str:
        """Download a data table as CSV."""
        api = self._api
        artifacts_data = await api._list_raw(notebook_id)

        table_art = api._select_artifact(
            artifacts_data,
            artifact_id,
            "Data table",
            # Unified to "data_table" so both empty-list and explicit-id-miss
            # paths raise ArtifactNotReadyError with the same artifact_type key.
            "data_table",
            type_code=ArtifactTypeCode.DATA_TABLE,
        )

        try:
            raw_data = table_art[18]
            headers, rows = _artifact_seams()._parse_data_table(raw_data)

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)

            def _write_csv() -> None:
                with output.open("w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
                    writer.writerows(rows)

            await asyncio.to_thread(_write_csv)

            return str(output)

        except (IndexError, TypeError, ValueError) as e:
            raise ArtifactParseError(
                "data_table", details=f"Failed to parse structure: {e}", cause=e
            ) from e

    async def download_quiz(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
    ) -> str:
        """Download quiz questions."""
        return await self.download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "quiz"
        )

    async def download_flashcards(
        self,
        notebook_id: str,
        output_path: str,
        artifact_id: str | None = None,
        output_format: str = "json",
    ) -> str:
        """Download flashcard deck."""
        return await self.download_interactive_artifact(
            notebook_id, output_path, artifact_id, output_format, "flashcards"
        )

    async def download_urls_batch(self, urls_and_paths: list[tuple[str, str]]) -> DownloadResult:
        """Download multiple files using httpx with proper cookie handling."""
        result = DownloadResult()

        cookies = await asyncio.to_thread(_load_httpx_cookies, self._api._storage_path)

        async with httpx.AsyncClient(
            cookies=cookies,
            follow_redirects=True,
            timeout=60.0,
        ) as client:
            for url, output_path in urls_and_paths:
                parsed_netloc = ""
                parsed_path = ""
                try:
                    parsed = urlparse(url)
                    parsed_netloc = parsed.netloc
                    parsed_path = parsed.path
                    if parsed.scheme != "https":
                        raise ArtifactDownloadError(
                            "media", details=f"Download URL must use HTTPS: {url[:80]}"
                        )
                    if not _is_trusted_download_host(parsed.netloc):
                        raise ArtifactDownloadError(
                            "media", details=f"Untrusted download domain: {parsed.netloc}"
                        )

                    response = await client.get(url)
                    if response.status_code in (401, 403):
                        raise ArtifactDownloadError(
                            "media",
                            details=(
                                f"Authentication failed (HTTP {response.status_code}) "
                                f"on {parsed.netloc}{parsed.path}"
                            ),
                        )
                    response.raise_for_status()

                    content_type = response.headers.get("content-type", "")
                    if "text/html" in content_type:
                        raise ArtifactDownloadError(
                            "media", details="Received HTML instead of media file"
                        )

                    output_file = Path(output_path)
                    output_file.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(output_file.write_bytes, response.content)
                    result.succeeded.append(output_path)
                    logger.debug(
                        "Downloaded %s%s (%d bytes)",
                        parsed.netloc,
                        parsed.path,
                        len(response.content),
                    )

                except (httpx.HTTPError, ValueError) as e:
                    if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                        reason = f"HTTP {e.response.status_code}"
                    else:
                        reason = e.__class__.__name__
                    logger.warning(
                        "Download failed for %s%s: %s",
                        parsed_netloc,
                        parsed_path,
                        reason,
                    )
                    result.failed.append((url, e))

        return result

    async def download_url(self, url: str, output_path: str) -> str:
        """Download a file from URL using streaming with proper cookie handling."""
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise ArtifactDownloadError("media", details=f"Download URL must use HTTPS: {url[:80]}")
        if not _is_trusted_download_host(parsed.netloc):
            raise ArtifactDownloadError(
                "media", details=f"Untrusted download domain: {parsed.netloc}"
            )

        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        fd, temp_path_str = tempfile.mkstemp(
            dir=output_file.parent,
            prefix=output_file.name + ".",
            suffix=".tmp",
        )
        os.close(fd)
        temp_file = Path(temp_path_str)

        try:
            cookies = await asyncio.to_thread(_load_httpx_cookies, self._api._storage_path)
            timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0)

            try:
                async with httpx.AsyncClient(  # noqa: SIM117
                    cookies=cookies,
                    follow_redirects=True,
                    timeout=timeout,
                ) as client:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()

                        content_type = response.headers.get("content-type", "")
                        if "text/html" in content_type:
                            raise ArtifactDownloadError(
                                "media",
                                details="Download failed: received HTML instead of media file. "
                                "Authentication may have expired. Run 'notebooklm login'.",
                            )

                        total_bytes = 0
                        with open(temp_file, "wb") as f:
                            async for chunk in response.aiter_bytes(chunk_size=65536):
                                write_task = asyncio.create_task(asyncio.to_thread(f.write, chunk))
                                try:
                                    await asyncio.shield(write_task)
                                except asyncio.CancelledError:
                                    with contextlib.suppress(asyncio.CancelledError, Exception):
                                        await write_task
                                    raise
                                total_bytes += len(chunk)

                        if total_bytes == 0:
                            raise ArtifactDownloadError(
                                "media",
                                details=(
                                    "Download produced 0 bytes -- the remote file may "
                                    "be missing or empty"
                                ),
                            )

                        os.replace(temp_file, output_file)
                        logger.debug(
                            "Downloaded %s%s (%d bytes)",
                            parsed.netloc,
                            parsed.path,
                            total_bytes,
                        )
                        return output_path
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    raise ArtifactDownloadError(
                        "media",
                        details=(
                            f"Authentication required for {parsed.netloc}{parsed.path}"
                            " -- try `notebooklm login`"
                        ),
                        cause=e,
                        status_code=e.response.status_code,
                    ) from e
                raise ArtifactDownloadError(
                    "media",
                    details=f"HTTP error downloading {parsed.netloc}{parsed.path}",
                    cause=e,
                    status_code=e.response.status_code,
                ) from e
            except httpx.RequestError as e:
                raise ArtifactDownloadError(
                    "media",
                    details=f"Network error downloading {parsed.netloc}{parsed.path}",
                    cause=e,
                ) from e
        except BaseException:
            temp_file.unlink(missing_ok=True)
            raise
