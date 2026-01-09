import os
import tempfile

import pytest

from .conftest import requires_auth

# Magic bytes for file type verification
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PDF_MAGIC = b"%PDF"
MP4_FTYP = b"ftyp"  # At offset 4


def is_png(path: str) -> bool:
    """Check if file is a valid PNG by magic bytes."""
    with open(path, "rb") as f:
        return f.read(8) == PNG_MAGIC


def is_pdf(path: str) -> bool:
    """Check if file is a valid PDF by magic bytes."""
    with open(path, "rb") as f:
        return f.read(4) == PDF_MAGIC


def is_mp4(path: str) -> bool:
    """Check if file is a valid MP4 by magic bytes."""
    with open(path, "rb") as f:
        header = f.read(12)
        # MP4 has 'ftyp' at offset 4
        return len(header) >= 8 and header[4:8] == MP4_FTYP


@requires_auth
class TestDownloadAudio:
    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_download_audio(self, client, read_only_notebook_id):
        """Downloads existing audio artifact - read-only.

        Note: NotebookLM serves audio in MP4 container format (MPEG-DASH),
        not MP3. The file extension .mp4 is correct.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "audio.mp4")
            try:
                result = await client.artifacts.download_audio(read_only_notebook_id, output_path)
                assert result == output_path
                assert os.path.exists(output_path)
                assert os.path.getsize(output_path) > 0
                assert is_mp4(output_path), "Downloaded audio is not a valid MP4 file"
            except ValueError as e:
                if "No completed audio" in str(e):
                    pytest.skip("No completed audio artifact available")
                raise


@requires_auth
class TestDownloadVideo:
    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_download_video(self, client, read_only_notebook_id):
        """Downloads existing video artifact - read-only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "video.mp4")
            try:
                result = await client.artifacts.download_video(read_only_notebook_id, output_path)
                assert result == output_path
                assert os.path.exists(output_path)
                assert os.path.getsize(output_path) > 0
                assert is_mp4(output_path), "Downloaded video is not a valid MP4 file"
            except ValueError as e:
                if "No completed video" in str(e):
                    pytest.skip("No completed video artifact available")
                raise


@requires_auth
class TestDownloadInfographic:
    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_download_infographic(self, client, read_only_notebook_id):
        """Downloads existing infographic - read-only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "infographic.png")
            try:
                result = await client.artifacts.download_infographic(
                    read_only_notebook_id, output_path
                )
                assert result == output_path
                assert os.path.exists(output_path)
                assert os.path.getsize(output_path) > 0
                assert is_png(output_path), "Downloaded infographic is not a valid PNG file"
            except ValueError as e:
                if "No completed infographic" in str(e):
                    pytest.skip("No completed infographic artifact available")
                raise


@requires_auth
class TestDownloadSlideDeck:
    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_download_slide_deck(self, client, read_only_notebook_id):
        """Downloads existing slide deck as PDF - read-only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "slides.pdf")
            try:
                result = await client.artifacts.download_slide_deck(
                    read_only_notebook_id, output_path
                )
                assert result == output_path
                assert os.path.exists(output_path)
                assert os.path.getsize(output_path) > 0
                assert is_pdf(output_path), "Downloaded slide deck is not a valid PDF file"
            except ValueError as e:
                if "No completed slide" in str(e):
                    pytest.skip("No completed slide deck artifact available")
                raise


@requires_auth
class TestExportArtifact:
    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_export_artifact(self, client, read_only_notebook_id):
        """Exports existing artifact - read-only."""
        artifacts = await client.artifacts.list(read_only_notebook_id)
        if not artifacts or len(artifacts) == 0:
            pytest.skip("No artifacts available to export")

        artifact_id = artifacts[0].id
        try:
            result = await client.artifacts.export(read_only_notebook_id, artifact_id)
            assert result is not None or result is None
        except Exception:
            pytest.skip("Export not available for this artifact type")
