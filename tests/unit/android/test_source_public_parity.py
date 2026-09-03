"""Public-contract closure for Android source compatibility seams."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest

from notebooklm._android.session import AndroidSession
from notebooklm._android.sources import AndroidSourcesAPI
from notebooklm._android.upload import AndroidUploadPipeline
from notebooklm.types import Source, SourceStatus


class _DriveDownload:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.document_ids: list[str] = []

    @asynccontextmanager
    async def __call__(
        self,
        document_id: str,
    ) -> AsyncIterator[tuple[Path, str, str | None]]:
        self.document_ids.append(document_id)
        try:
            yield (
                self.path,
                "Drive document.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        finally:
            self.path.unlink(missing_ok=True)


class _ParitySources(AndroidSourcesAPI):
    def __init__(
        self,
        drive_download: _DriveDownload,
    ) -> None:
        super().__init__(
            cast(AndroidSession, object()),
            cast(AndroidUploadPipeline, object()),
            drive_download=drive_download,
        )
        self.uploads: list[tuple[str, Path, str | None, bool, float]] = []

    async def _send_upload(
        self,
        notebook_id: str,
        file_path: str | Path,
        mime_type: str | None,
        *,
        wait: bool,
        wait_timeout: float,
        title: str | None,
        on_progress: Callable[[int, int], object] | None,
    ) -> Source:
        del mime_type, on_progress
        path = Path(file_path)
        assert path.exists()
        self.uploads.append((notebook_id, path, title, wait, wait_timeout))
        return Source(id="source-id", title=title, status=SourceStatus.PROCESSING)


@pytest.mark.asyncio
async def test_add_drive_file_downloads_with_live_web_auth_then_uses_android_add_file(
    tmp_path: Path,
) -> None:
    downloaded = tmp_path / "downloaded.docx"
    downloaded.write_bytes(b"docx")
    drive_download = _DriveDownload(downloaded)

    api = _ParitySources(drive_download)
    result = await api.add_drive_file(
        "notebook-id",
        "abcdefghijklmnopqrstuvwxyz123456",
        wait=True,
        wait_timeout=9.0,
    )

    assert result.id == "source-id"
    assert drive_download.document_ids == ["abcdefghijklmnopqrstuvwxyz123456"]
    assert api.uploads == [("notebook-id", downloaded, "Drive document.docx", True, 9.0)]
    assert not downloaded.exists()
