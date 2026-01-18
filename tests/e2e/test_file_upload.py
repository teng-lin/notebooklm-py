import os
import tempfile
from pathlib import Path

import pytest

from .conftest import requires_auth


def create_minimal_pdf(path: Path) -> None:
    """Create a minimal valid PDF file for testing."""
    pdf_content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>
endobj
4 0 obj
<< /Length 44 >>
stream
BT
/F1 12 Tf
100 700 Td
(Test PDF) Tj
ET
endstream
endobj
xref
0 5
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000206 00000 n
trailer
<< /Size 5 /Root 1 0 R >>
startxref
300
%%EOF"""
    path.write_bytes(pdf_content)


def create_minimal_image(path: Path) -> None:
    """Create a minimal valid PNG file for testing."""
    # 1x1 pixel transparent PNG
    png_content = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    path.write_bytes(png_content)


@requires_auth
class TestFileUpload:
    """File upload tests.

    These tests verify the 3-step resumable upload protocol works correctly.
    Uses temp_notebook since file upload creates sources (CRUD operation).
    """

    @pytest.mark.asyncio
    async def test_add_pdf_file(self, client, temp_notebook, tmp_path):
        """Test uploading a PDF file."""
        test_pdf = tmp_path / "test_upload.pdf"
        create_minimal_pdf(test_pdf)

        # wait=True ensures we get the processed source type
        source = await client.sources.add_file(
            temp_notebook.id,
            test_pdf,
            mime_type="application/pdf",
            wait=True,
            wait_timeout=120,
        )
        assert source is not None
        assert source.id is not None
        assert source.title == "test_upload.pdf"
        assert source.source_type == "pdf"
        assert source.source_type_code == 3

    @pytest.mark.asyncio
    async def test_add_text_file(self, client, temp_notebook):
        """Test uploading a text file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("This is a test document for NotebookLM file upload.\n")
            f.write("It contains multiple lines of text.\n")
            f.write("The file upload should work with this content.")
            temp_path = f.name

        try:
            # wait=True ensures we get the processed source type
            source = await client.sources.add_file(
                temp_notebook.id,
                temp_path,
                wait=True,
                wait_timeout=120,
            )
            assert source is not None
            assert source.id is not None
            assert source.source_type == "text"
            assert source.source_type_code == 11
        finally:
            os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_add_markdown_file(self, client, temp_notebook):
        """Test uploading a markdown file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Test Markdown Document\n\n")
            f.write("## Section 1\n\n")
            f.write("This is a test markdown file.\n\n")
            f.write("- Item 1\n")
            f.write("- Item 2\n")
            temp_path = f.name

        try:
            # wait=True ensures we get the processed source type
            source = await client.sources.add_file(
                temp_notebook.id,
                temp_path,
                mime_type="text/markdown",
                wait=True,
                wait_timeout=120,
            )
            assert source is not None
            assert source.id is not None
            # Markdown uploads are treated as TEXT type (11)
            assert source.source_type == "text"
            assert source.source_type_code == 11
        finally:
            os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_add_csv_file(self, client, temp_notebook, tmp_path):
        """Test uploading a CSV file."""
        test_csv = tmp_path / "test_data.csv"
        test_csv.write_text("Header1,Header2\nValue1,Value2")

        # wait=True ensures we get the processed source type
        source = await client.sources.add_file(
            temp_notebook.id,
            test_csv,
            mime_type="text/csv",
            wait=True,
            wait_timeout=120,
        )
        assert source is not None
        assert source.id is not None
        assert source.title == "test_data.csv"
        # CSVs are type 14 (SPREADSHEET)
        assert source.source_type == "spreadsheet"
        assert source.source_type_code == 14

    @pytest.mark.asyncio
    async def test_add_mp3_file(self, client, temp_notebook, tmp_path):
        """Test uploading an MP3 file."""
        test_mp3 = tmp_path / "test_audio.mp3"
        # Minimal dummy MP3 content (ID3 header) to pass initial validation
        # In real E2E, this might fail "processing" step if not valid audio,
        # but verifies the upload type mapping.
        test_mp3.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\n")

        source = await client.sources.add_file(
            temp_notebook.id,
            test_mp3,
            mime_type="audio/mpeg",
            wait=False,  # Don't wait for processing as dummy file might fail transcription
        )
        assert source is not None
        assert source.id is not None
        assert source.source_type == "upload"  # Initial type

    @pytest.mark.asyncio
    async def test_add_mp4_file(self, client, temp_notebook, tmp_path):
        """Test uploading an MP4 file."""
        test_mp4 = tmp_path / "test_video.mp4"
        # Minimal dummy MP4 ftyp atom
        test_mp4.write_bytes(
            b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
        )

        source = await client.sources.add_file(
            temp_notebook.id,
            test_mp4,
            mime_type="video/mp4",
            wait=False,  # Don't wait for processing as dummy file might fail
        )
        assert source is not None
        assert source.id is not None
        assert source.source_type == "upload"  # Initial type
