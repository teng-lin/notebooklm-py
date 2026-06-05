import os
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from notebooklm import SourceProcessingError, SourceType

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


def create_minimal_docx(path: Path) -> None:
    """Create a minimal valid DOCX file for testing."""
    import zipfile
    from io import BytesIO

    content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

    rels = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

    document = b"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body><w:p><w:r><w:t>Test DOCX content for NotebookLM upload testing.</w:t></w:r></w:p></w:body>
</w:document>"""

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)

    path.write_bytes(buffer.getvalue())


def create_minimal_jpg(path: Path) -> None:
    """Create a minimal valid JPEG file for testing."""
    # Minimal 1x1 red JPEG
    jpg_content = bytes(
        [
            0xFF,
            0xD8,
            0xFF,
            0xE0,
            0x00,
            0x10,
            0x4A,
            0x46,
            0x49,
            0x46,
            0x00,
            0x01,
            0x01,
            0x00,
            0x00,
            0x01,
            0x00,
            0x01,
            0x00,
            0x00,
            0xFF,
            0xDB,
            0x00,
            0x43,
            0x00,
            0x08,
            0x06,
            0x06,
            0x07,
            0x06,
            0x05,
            0x08,
            0x07,
            0x07,
            0x07,
            0x09,
            0x09,
            0x08,
            0x0A,
            0x0C,
            0x14,
            0x0D,
            0x0C,
            0x0B,
            0x0B,
            0x0C,
            0x19,
            0x12,
            0x13,
            0x0F,
            0x14,
            0x1D,
            0x1A,
            0x1F,
            0x1E,
            0x1D,
            0x1A,
            0x1C,
            0x1C,
            0x20,
            0x24,
            0x2E,
            0x27,
            0x20,
            0x22,
            0x2C,
            0x23,
            0x1C,
            0x1C,
            0x28,
            0x37,
            0x29,
            0x2C,
            0x30,
            0x31,
            0x34,
            0x34,
            0x34,
            0x1F,
            0x27,
            0x39,
            0x3D,
            0x38,
            0x32,
            0x3C,
            0x2E,
            0x33,
            0x34,
            0x32,
            0xFF,
            0xC0,
            0x00,
            0x0B,
            0x08,
            0x00,
            0x01,
            0x00,
            0x01,
            0x01,
            0x01,
            0x11,
            0x00,
            0xFF,
            0xC4,
            0x00,
            0x1F,
            0x00,
            0x00,
            0x01,
            0x05,
            0x01,
            0x01,
            0x01,
            0x01,
            0x01,
            0x01,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x01,
            0x02,
            0x03,
            0x04,
            0x05,
            0x06,
            0x07,
            0x08,
            0x09,
            0x0A,
            0x0B,
            0xFF,
            0xC4,
            0x00,
            0xB5,
            0x10,
            0x00,
            0x02,
            0x01,
            0x03,
            0x03,
            0x02,
            0x04,
            0x03,
            0x05,
            0x05,
            0x04,
            0x04,
            0x00,
            0x00,
            0x01,
            0x7D,
            0x01,
            0x02,
            0x03,
            0x00,
            0x04,
            0x11,
            0x05,
            0x12,
            0x21,
            0x31,
            0x41,
            0x06,
            0x13,
            0x51,
            0x61,
            0x07,
            0x22,
            0x71,
            0x14,
            0x32,
            0x81,
            0x91,
            0xA1,
            0x08,
            0x23,
            0x42,
            0xB1,
            0xC1,
            0x15,
            0x52,
            0xD1,
            0xF0,
            0x24,
            0x33,
            0x62,
            0x72,
            0x82,
            0x09,
            0x0A,
            0x16,
            0x17,
            0x18,
            0x19,
            0x1A,
            0x25,
            0x26,
            0x27,
            0x28,
            0x29,
            0x2A,
            0x34,
            0x35,
            0x36,
            0x37,
            0x38,
            0x39,
            0x3A,
            0x43,
            0x44,
            0x45,
            0x46,
            0x47,
            0x48,
            0x49,
            0x4A,
            0x53,
            0x54,
            0x55,
            0x56,
            0x57,
            0x58,
            0x59,
            0x5A,
            0x63,
            0x64,
            0x65,
            0x66,
            0x67,
            0x68,
            0x69,
            0x6A,
            0x73,
            0x74,
            0x75,
            0x76,
            0x77,
            0x78,
            0x79,
            0x7A,
            0x83,
            0x84,
            0x85,
            0x86,
            0x87,
            0x88,
            0x89,
            0x8A,
            0x92,
            0x93,
            0x94,
            0x95,
            0x96,
            0x97,
            0x98,
            0x99,
            0x9A,
            0xA2,
            0xA3,
            0xA4,
            0xA5,
            0xA6,
            0xA7,
            0xA8,
            0xA9,
            0xAA,
            0xB2,
            0xB3,
            0xB4,
            0xB5,
            0xB6,
            0xB7,
            0xB8,
            0xB9,
            0xBA,
            0xC2,
            0xC3,
            0xC4,
            0xC5,
            0xC6,
            0xC7,
            0xC8,
            0xC9,
            0xCA,
            0xD2,
            0xD3,
            0xD4,
            0xD5,
            0xD6,
            0xD7,
            0xD8,
            0xD9,
            0xDA,
            0xE1,
            0xE2,
            0xE3,
            0xE4,
            0xE5,
            0xE6,
            0xE7,
            0xE8,
            0xE9,
            0xEA,
            0xF1,
            0xF2,
            0xF3,
            0xF4,
            0xF5,
            0xF6,
            0xF7,
            0xF8,
            0xF9,
            0xFA,
            0xFF,
            0xDA,
            0x00,
            0x08,
            0x01,
            0x01,
            0x00,
            0x00,
            0x3F,
            0x00,
            0xFB,
            0xD5,
            0xDB,
            0x20,
            0xA8,
            0xF1,
            0x45,
            0x00,
            0x14,
            0x50,
            0x01,
            0x45,
            0x14,
            0x00,
            0xFF,
            0xD9,
        ]
    )
    path.write_bytes(jpg_content)


def create_minimal_epub(path: Path) -> None:
    """Create a minimal valid EPUB 3 file for testing."""
    mimetype = b"application/epub+zip"

    container_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

    content_opf = b"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:12345678-1234-1234-1234-123456789abc</dc:identifier>
    <dc:title>Test EPUB</dc:title>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  </manifest>
  <spine>
    <itemref idref="chapter1"/>
  </spine>
</package>"""

    chapter1_xhtml = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 1</title></head>
<body>
<h1>Chapter 1</h1>
<p>Test EPUB content for NotebookLM upload testing.</p>
</body>
</html>"""

    nav_xhtml = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Navigation</title></head>
<body>
<nav epub:type="toc"><ol><li><a href="chapter1.xhtml">Chapter 1</a></li></ol></nav>
</body>
</html>"""

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # mimetype must be first entry and stored uncompressed per EPUB spec
        zf.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("OEBPS/content.opf", content_opf)
        zf.writestr("OEBPS/chapter1.xhtml", chapter1_xhtml)
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml)

    path.write_bytes(buffer.getvalue())


async def assert_file_source(
    client,
    notebook_id,
    path,
    *,
    expected_kind,
    mime_type=None,
    expected_title=None,
    process=True,
    timeout=120.0,
):
    """Upload ``path``, then assert the client contract and (optionally) processing.

    Splits the two concerns so a failure is attributable (see #1204):

    - **Client contract** (always): ``add_file(wait=False)`` must return a source
      with an id — and ``expected_title`` when given. A client-side regression (bad
      MIME routing, malformed/corrupt upload) fails here, distinctly ours.
    - **Server processing** (``process=True``): wait for READY and assert
      ``kind == expected_kind``. A terminal ``SourceStatus.ERROR`` fails **loudly**
      with a triage note (server-side processing failure, not a client regression),
      chaining the original ``SourceProcessingError`` as the cause.
    - ``process=False`` (e.g. a dummy media file that can't transcribe):
      registration-only — assert the initial ``kind == expected_kind`` (usually
      ``SourceType.UNKNOWN``) without waiting.
    """
    source = await client.sources.add_file(notebook_id, path, mime_type=mime_type, wait=False)
    assert source is not None
    assert source.id is not None
    if expected_title is not None:
        assert source.title == expected_title

    if not process:
        assert source.kind == expected_kind
        return source

    try:
        ready = await client.sources.wait_until_ready(notebook_id, source.id, timeout=timeout)
    except SourceProcessingError as exc:
        # Re-raise (not pytest.fail) with ``from exc`` so the original
        # SourceProcessingError traceback is preserved as the failure cause.
        raise AssertionError(
            f"{Path(path).name} reached terminal SourceStatus.ERROR ({exc}). Upload "
            "and registration succeeded (asserted above), so this is NotebookLM's "
            "server-side processing failing — verify the service before treating it "
            "as a client upload/registration regression."
        ) from exc
    assert ready.kind == expected_kind
    return ready


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
        await assert_file_source(
            client,
            temp_notebook.id,
            test_pdf,
            mime_type="application/pdf",
            expected_title="test_upload.pdf",
            expected_kind=SourceType.PDF,
        )

    @pytest.mark.asyncio
    async def test_add_text_file(self, client, temp_notebook):
        """Test uploading a text file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("This is a test document for NotebookLM file upload.\n")
            f.write("It contains multiple lines of text.\n")
            f.write("The file upload should work with this content.")
            temp_path = f.name

        try:
            await assert_file_source(
                client,
                temp_notebook.id,
                temp_path,
                expected_kind=SourceType.PASTED_TEXT,
            )
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
            await assert_file_source(
                client,
                temp_notebook.id,
                temp_path,
                mime_type="text/markdown",
                expected_kind=SourceType.MARKDOWN,
            )
        finally:
            os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_add_csv_file(self, client, temp_notebook, tmp_path):
        """Test uploading a CSV file."""
        test_csv = tmp_path / "test_data.csv"
        test_csv.write_text("Header1,Header2\nValue1,Value2")
        await assert_file_source(
            client,
            temp_notebook.id,
            test_csv,
            mime_type="text/csv",
            expected_title="test_data.csv",
            expected_kind=SourceType.CSV,
        )

    @pytest.mark.asyncio
    async def test_add_mp3_file(self, client, temp_notebook, tmp_path):
        """Test uploading an MP3 file (registration only — a dummy MP3 won't transcribe)."""
        test_mp3 = tmp_path / "test_audio.mp3"
        # Minimal dummy MP3 (ID3 header): passes initial validation but won't process.
        test_mp3.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\n")
        await assert_file_source(
            client,
            temp_notebook.id,
            test_mp3,
            mime_type="audio/mpeg",
            expected_kind=SourceType.UNKNOWN,  # initial type before processing
            process=False,
        )

    @pytest.mark.asyncio
    async def test_add_mp4_file(self, client, temp_notebook, tmp_path):
        """Test uploading an MP4 file (registration only — a dummy MP4 won't process)."""
        test_mp4 = tmp_path / "test_video.mp4"
        # Minimal dummy MP4 ftyp atom: passes initial validation but won't process.
        test_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
        await assert_file_source(
            client,
            temp_notebook.id,
            test_mp4,
            mime_type="video/mp4",
            expected_kind=SourceType.UNKNOWN,  # initial type before processing
            process=False,
        )

    @pytest.mark.asyncio
    async def test_add_docx_file(self, client, temp_notebook, tmp_path):
        """Test uploading a DOCX file."""
        test_docx = tmp_path / "test_document.docx"
        create_minimal_docx(test_docx)
        await assert_file_source(
            client,
            temp_notebook.id,
            test_docx,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            expected_title="test_document.docx",
            expected_kind=SourceType.DOCX,
        )

    @pytest.mark.asyncio
    async def test_add_jpg_file(self, client, temp_notebook, tmp_path):
        """Test uploading a JPEG image file."""
        test_jpg = tmp_path / "test_image.jpg"
        create_minimal_jpg(test_jpg)
        await assert_file_source(
            client,
            temp_notebook.id,
            test_jpg,
            mime_type="image/jpeg",
            expected_title="test_image.jpg",
            expected_kind=SourceType.IMAGE,
        )

    @pytest.mark.asyncio
    async def test_add_epub_file(self, client, temp_notebook, tmp_path):
        """Test uploading an EPUB file.

        EPUB processing intermittently fails server-side (#1204); the helper keeps
        that failure loud + attributable (not ``xfail``'d) — see ``assert_file_source``.
        """
        test_epub = tmp_path / "test_book.epub"
        create_minimal_epub(test_epub)
        await assert_file_source(
            client,
            temp_notebook.id,
            test_epub,
            mime_type="application/epub+zip",
            expected_title="test_book.epub",
            expected_kind=SourceType.EPUB,
        )
