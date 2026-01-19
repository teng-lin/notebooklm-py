#!/usr/bin/env python3
"""Create valid test files and upload them to test notebook."""

import asyncio
import struct
import tempfile
import zipfile
from pathlib import Path

from notebooklm import NotebookLMClient


def create_minimal_docx(path: Path) -> None:
    """Create a minimal valid DOCX file (it's a ZIP with XML)."""
    # DOCX is a ZIP file with specific XML structure
    with zipfile.ZipFile(path, 'w') as zf:
        # [Content_Types].xml
        content_types = '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''
        zf.writestr('[Content_Types].xml', content_types)

        # _rels/.rels
        rels = '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
        zf.writestr('_rels/.rels', rels)

        # word/document.xml
        document = '''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r>
        <w:t>This is a test DOCX document for source type investigation.</w:t>
      </w:r>
    </w:p>
  </w:body>
</w:document>'''
        zf.writestr('word/document.xml', document)


def create_valid_png(path: Path) -> None:
    """Create a minimal valid PNG file (8x8 red square)."""
    # PNG signature
    signature = b'\x89PNG\r\n\x1a\n'

    # IHDR chunk (image header)
    width = 8
    height = 8
    bit_depth = 8
    color_type = 2  # RGB
    ihdr_data = struct.pack('>IIBBBBB', width, height, bit_depth, color_type, 0, 0, 0)
    ihdr_crc = 0x3a8a26e7  # Pre-computed CRC for this data
    ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc)

    # IDAT chunk (image data) - simplified
    import zlib
    raw_data = b''
    for y in range(height):
        raw_data += b'\x00'  # filter byte
        for x in range(width):
            raw_data += b'\xff\x00\x00'  # Red pixel (RGB)
    compressed = zlib.compress(raw_data)
    idat_crc = zlib.crc32(b'IDAT' + compressed) & 0xffffffff
    idat = struct.pack('>I', len(compressed)) + b'IDAT' + compressed + struct.pack('>I', idat_crc)

    # IEND chunk
    iend_crc = 0xae426082  # Pre-computed CRC
    iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc)

    path.write_bytes(signature + ihdr + idat + iend)


def create_valid_mp3(path: Path) -> None:
    """Create a minimal valid MP3 file with ID3 tag and audio frames."""
    # ID3v2 header
    id3_header = b'ID3'
    id3_version = b'\x04\x00'  # ID3v2.4.0
    id3_flags = b'\x00'
    id3_size = b'\x00\x00\x00\x00'  # 0 bytes of ID3 content

    # MP3 frame header (MPEG Audio Layer 3)
    # 0xFF 0xFB = sync word + MPEG1 Layer3
    # 0x90 = 128kbps, 44.1kHz
    # 0x00 = padding, private, stereo
    mp3_frame = b'\xff\xfb\x90\x00'

    # Pad with silence (enough for a valid frame)
    frame_data = b'\x00' * 417  # ~1 frame at 128kbps

    # Write multiple frames to make it more realistic
    content = id3_header + id3_version + id3_flags + id3_size
    for _ in range(10):  # 10 frames
        content += mp3_frame + frame_data

    path.write_bytes(content)


def create_valid_pdf(path: Path) -> None:
    """Create a minimal valid PDF file."""
    pdf = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 84 >>
stream
BT
/F1 24 Tf
100 700 Td
(Test PDF for source type investigation) Tj
ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000248 00000 n
0000000384 00000 n
trailer
<< /Size 6 /Root 1 0 R >>
startxref
459
%%EOF"""
    path.write_bytes(pdf)


async def upload_and_wait(client, notebook_id: str, file_path: Path, label: str) -> dict:
    """Upload a file and wait for it to be processed."""
    print(f"\n{'='*60}")
    print(f"Uploading {label}: {file_path.name}")
    print(f"{'='*60}")

    try:
        # Upload file
        source = await client.sources.add_file(notebook_id, file_path)
        print(f"  Source ID: {source.id}")
        print(f"  Title: {source.title}")
        print(f"  Initial status: {source.status}")

        # Poll for completion
        print(f"  Waiting for processing...")
        max_polls = 30
        for i in range(max_polls):
            await asyncio.sleep(3)

            # Get fresh source list
            sources = await client.sources.list(notebook_id)
            current = next((s for s in sources if s.id == source.id), None)

            if current is None:
                print(f"  Poll {i+1}: Source not in list yet...")
                continue

            print(f"  Poll {i+1}: Status = {current.status}")

            if current.status == 2:  # READY
                # Get fulltext
                fulltext = await client.sources.get_fulltext(notebook_id, source.id)
                print(f"\n  ✓ PROCESSED!")
                print(f"  Type Code: {fulltext.source_type}")
                print(f"  Content: {len(fulltext.content)} chars")
                return {
                    "label": label,
                    "file": file_path.name,
                    "source_id": source.id,
                    "type_code": fulltext.source_type,
                    "content_len": len(fulltext.content),
                }
            elif current.status == 3:  # ERROR
                print(f"  ✗ Processing failed!")
                return {"label": label, "error": "Processing failed (status=3)"}

        print(f"  ✗ Timeout waiting for processing")
        return {"label": label, "error": "Timeout"}

    except Exception as e:
        print(f"  ✗ Error: {e}")
        return {"label": label, "error": str(e)}


async def main():
    """Upload test files and discover type codes."""
    notebook_id = "7d019668-04c9-4b77-ba6a-b7388a2c0abe"

    async with await NotebookLMClient.from_storage() as client:
        print("=" * 70)
        print("SOURCE TYPE INVESTIGATION - FILE UPLOADS")
        print(f"Notebook: {notebook_id}")
        print("=" * 70)

        results = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Create test files
            print("\nCreating test files...")

            docx_file = tmp_path / "test_source_type.docx"
            create_minimal_docx(docx_file)
            print(f"  Created: {docx_file.name} ({docx_file.stat().st_size} bytes)")

            png_file = tmp_path / "test_source_type.png"
            create_valid_png(png_file)
            print(f"  Created: {png_file.name} ({png_file.stat().st_size} bytes)")

            mp3_file = tmp_path / "test_source_type.mp3"
            create_valid_mp3(mp3_file)
            print(f"  Created: {mp3_file.name} ({mp3_file.stat().st_size} bytes)")

            pdf_file = tmp_path / "test_source_type.pdf"
            create_valid_pdf(pdf_file)
            print(f"  Created: {pdf_file.name} ({pdf_file.stat().st_size} bytes)")

            # Upload each file
            results.append(await upload_and_wait(client, notebook_id, docx_file, "DOCX"))
            results.append(await upload_and_wait(client, notebook_id, png_file, "PNG"))
            results.append(await upload_and_wait(client, notebook_id, mp3_file, "MP3"))
            results.append(await upload_and_wait(client, notebook_id, pdf_file, "PDF"))

        # Summary
        print("\n" + "=" * 70)
        print("RESULTS SUMMARY")
        print("=" * 70)

        for r in results:
            if "type_code" in r:
                print(f"\n{r['label']}:")
                print(f"  File: {r['file']}")
                print(f"  Type Code: {r['type_code']}")
            else:
                print(f"\n{r['label']}: ERROR - {r.get('error', 'Unknown')}")


if __name__ == "__main__":
    asyncio.run(main())
