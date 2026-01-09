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

        source = await client.sources.add_file(
            temp_notebook.id, test_pdf, mime_type="application/pdf"
        )
        assert source is not None
        assert source.id is not None
        assert source.title == "test_upload.pdf"

    @pytest.mark.asyncio
    async def test_add_text_file(self, client, temp_notebook):
        """Test uploading a text file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("This is a test document for NotebookLM file upload.\n")
            f.write("It contains multiple lines of text.\n")
            f.write("The file upload should work with this content.")
            temp_path = f.name

        try:
            source = await client.sources.add_file(temp_notebook.id, temp_path)
            assert source is not None
            assert source.id is not None
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
            source = await client.sources.add_file(
                temp_notebook.id, temp_path, mime_type="text/markdown"
            )
            assert source is not None
            assert source.id is not None
        finally:
            os.unlink(temp_path)
