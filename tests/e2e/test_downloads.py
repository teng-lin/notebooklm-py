import csv
import json
import os
import tempfile

import pytest

from notebooklm.exceptions import ArtifactNotReadyError

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
            except ArtifactNotReadyError:
                pytest.skip("No completed audio artifact available")


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
            except ArtifactNotReadyError:
                pytest.skip("No completed video artifact available")


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
            except ArtifactNotReadyError:
                pytest.skip("No completed infographic artifact available")


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
            except ArtifactNotReadyError:
                pytest.skip("No completed slide deck artifact available")


@requires_auth
class TestExportArtifact:
    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_export_artifact(self, client, read_only_notebook_id):
        """Exports existing artifact - read-only.

        Note: Only report and data_table artifacts support export.
        Reports export to Google Docs, data tables export to Google Sheets.
        """
        from notebooklm import ArtifactType

        artifacts = await client.artifacts.list(read_only_notebook_id)
        if not artifacts:
            pytest.skip("No artifacts available")

        # Filter for exportable artifacts (only report and data_table support export)
        exportable = [
            a for a in artifacts if a.kind in (ArtifactType.REPORT, ArtifactType.DATA_TABLE)
        ]
        if not exportable:
            pytest.skip("No exportable artifacts (report or data_table) available")

        artifact = exportable[0]
        result = await client.artifacts.export(read_only_notebook_id, artifact.id)

        # Export returns a list with Google Docs/Sheets URL(s) on success
        assert result is not None, "Export should return a result"
        assert isinstance(result, list), "Export result should be a list"
        assert len(result) > 0, "Successful export should return at least one URL"

        # Reports go to Docs, data tables go to Sheets
        url = result[0]
        assert "docs.google.com" in url or "sheets.google.com" in url, (
            f"Export URL should be Google Docs or Sheets, got: {url}"
        )


def is_valid_markdown(path: str) -> bool:
    """Check if file is valid markdown (starts with # or has content)."""
    with open(path, encoding="utf-8") as f:
        content = f.read()
        return len(content) > 0 and (
            content.startswith("#") or "\n#" in content or len(content) > 100
        )


def is_valid_json(path: str) -> bool:
    """Check if file is valid JSON."""
    try:
        with open(path, encoding="utf-8") as f:
            json.load(f)
        return True
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def is_valid_csv(path: str) -> bool:
    """Check if file is valid CSV with headers."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
            return len(rows) > 0 and len(rows[0]) > 0
    except (csv.Error, OSError, UnicodeDecodeError):
        return False


@requires_auth
class TestDownloadReport:
    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_download_report(self, client, read_only_notebook_id):
        """Downloads existing report as markdown - read-only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "report.md")
            try:
                result = await client.artifacts.download_report(read_only_notebook_id, output_path)
                assert result == output_path
                assert os.path.exists(output_path)
                assert os.path.getsize(output_path) > 0
                assert is_valid_markdown(output_path), "Downloaded report is not valid markdown"
            except ArtifactNotReadyError:
                pytest.skip("No completed report artifact available")


@requires_auth
class TestDownloadMindMap:
    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_download_mind_map(self, client, read_only_notebook_id):
        """Downloads existing mind map as JSON - read-only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "mindmap.json")
            try:
                result = await client.artifacts.download_mind_map(
                    read_only_notebook_id, output_path
                )
                assert result == output_path
                assert os.path.exists(output_path)
                assert os.path.getsize(output_path) > 0
                assert is_valid_json(output_path), "Downloaded mind map is not valid JSON"

                # Verify structure
                with open(output_path, encoding="utf-8") as f:
                    data = json.load(f)
                assert "name" in data, "Mind map JSON should have 'name' field"
            except ArtifactNotReadyError:
                pytest.skip("No mind map artifact available")


@requires_auth
class TestDownloadDataTable:
    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_download_data_table(self, client, read_only_notebook_id):
        """Downloads existing data table as CSV - read-only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "data.csv")
            try:
                result = await client.artifacts.download_data_table(
                    read_only_notebook_id, output_path
                )
                assert result == output_path
                assert os.path.exists(output_path)
                assert os.path.getsize(output_path) > 0
                assert is_valid_csv(output_path), "Downloaded data table is not valid CSV"
            except ArtifactNotReadyError:
                pytest.skip("No completed data table artifact available")
