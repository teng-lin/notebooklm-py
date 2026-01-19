#!/usr/bin/env python3
"""Systematic investigation of all NotebookLM source types.

This script creates test files for each supported type, uploads them,
and records the actual source_type integer codes returned by the API.
"""

import asyncio
import tempfile
from pathlib import Path

from notebooklm import NotebookLMClient


# Test data for each file type
TEST_CONTENT = {
    "txt": "This is a test text file.\nIt has multiple lines.\nFor testing source type detection.",
    "md": "# Test Markdown\n\n## Section 1\n\nThis is a markdown file for testing.\n\n- Item 1\n- Item 2",
    "pdf": b"""%PDF-1.4
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
%%EOF""",
    # Simple MP3 file (minimal valid MP3 header + ID3 tag)
    "mp3": b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + (b"\x00" * 100),
    # Simple PNG file (1x1 transparent pixel)
    "png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82",
    # Simple JPEG file (1x1 red pixel)
    "jpg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfe\xaa\xff\xd9",
}


async def create_test_files(tmp_dir: Path) -> dict[str, Path]:
    """Create test files for each type."""
    files = {}

    # Text file
    txt_file = tmp_dir / "test.txt"
    txt_file.write_text(TEST_CONTENT["txt"])
    files["txt"] = txt_file

    # Markdown file
    md_file = tmp_dir / "test.md"
    md_file.write_text(TEST_CONTENT["md"])
    files["md"] = md_file

    # PDF file
    pdf_file = tmp_dir / "test.pdf"
    pdf_file.write_bytes(TEST_CONTENT["pdf"])
    files["pdf"] = pdf_file

    # MP3 file
    mp3_file = tmp_dir / "test.mp3"
    mp3_file.write_bytes(TEST_CONTENT["mp3"])
    files["mp3"] = mp3_file

    # PNG file
    png_file = tmp_dir / "test.png"
    png_file.write_bytes(TEST_CONTENT["png"])
    files["png"] = png_file

    # JPG file
    jpg_file = tmp_dir / "test.jpg"
    jpg_file.write_bytes(TEST_CONTENT["jpg"])
    files["jpg"] = jpg_file

    # WEBP file (simple valid WEBP)
    webp_file = tmp_dir / "test.webp"
    webp_file.write_bytes(b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x000\x01\x00\x9d\x01*\x01\x00\x01\x00\x01@%\xa4\x00\x03p\x00\xfe\xfb\x94\x00\x00")
    files["webp"] = webp_file

    return files


async def main():
    """Systematically test all source types."""
    async with await NotebookLMClient.from_storage() as client:
        print("=" * 80)
        print("SYSTEMATIC SOURCE TYPE INVESTIGATION")
        print("=" * 80)

        # Create a test notebook
        print("\n[1/5] Creating test notebook...")
        notebook = await client.notebooks.create("Source Type Investigation")
        nb_id = notebook.id
        print(f"✓ Created notebook: {nb_id}")

        results = {}

        try:
            # Create test files
            print("\n[2/5] Creating test files...")
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                files = await create_test_files(tmp_path)
                print(f"✓ Created {len(files)} test files")

                # Test file uploads
                print("\n[3/5] Testing file uploads...")
                print("-" * 80)

                for file_type, file_path in files.items():
                    try:
                        print(f"\nUploading {file_type.upper()} file: {file_path.name}")
                        source = await client.sources.add_file(
                            nb_id,
                            file_path,
                            wait=False,  # Don't wait in add_file, we'll poll manually
                        )

                        print(f"  Source ID: {source.id}")
                        print(f"  Waiting for processing...")

                        # Wait for source to be ready
                        await client.sources.wait_for_sources(
                            nb_id,
                            [source.id],
                            timeout=120.0
                        )

                        # Get fulltext to see type code
                        fulltext = await client.sources.get_fulltext(nb_id, source.id)
                        type_code = fulltext.source_type

                        print(f"  ✓ Processed! Type Code: {type_code}")

                        results[file_type] = {
                            "type_code": type_code,
                            "source_id": source.id,
                            "title": source.title,
                        }

                    except Exception as e:
                        print(f"  ✗ Error uploading {file_type}: {e}")
                        results[file_type] = {"error": str(e)}

            # Test other source types
            print("\n[4/5] Testing other source types...")
            print("-" * 80)

            # Pasted text
            print("\nAdding pasted text...")
            try:
                source = await client.sources.add_text(
                    nb_id,
                    "Pasted Text Test",
                    "This is pasted text content for testing."
                )
                fulltext = await client.sources.get_fulltext(nb_id, source.id)
                print(f"  Type Code: {fulltext.source_type}")
                results["pasted_text"] = {"type_code": fulltext.source_type}
            except Exception as e:
                print(f"  ✗ Error: {e}")
                results["pasted_text"] = {"error": str(e)}

            # Web URL
            print("\nAdding web URL...")
            try:
                source = await client.sources.add_url(
                    nb_id,
                    "https://en.wikipedia.org/wiki/Python_(programming_language)",
                    wait=True,
                    wait_timeout=60
                )
                fulltext = await client.sources.get_fulltext(nb_id, source.id)
                print(f"  Type Code: {fulltext.source_type}")
                results["web_url"] = {"type_code": fulltext.source_type}
            except Exception as e:
                print(f"  ✗ Error: {e}")
                results["web_url"] = {"error": str(e)}

            # YouTube
            print("\nAdding YouTube video...")
            try:
                source = await client.sources.add_url(
                    nb_id,
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    wait=True,
                    wait_timeout=60
                )
                fulltext = await client.sources.get_fulltext(nb_id, source.id)
                print(f"  Type Code: {fulltext.source_type}")
                results["youtube"] = {"type_code": fulltext.source_type}
            except Exception as e:
                print(f"  ✗ Error: {e}")
                results["youtube"] = {"error": str(e)}

            # Summary
            print("\n[5/5] RESULTS SUMMARY")
            print("=" * 80)
            print("\nSource Type Code Mapping:")
            print("-" * 80)

            type_code_map = {}
            for source_type, data in results.items():
                if "type_code" in data:
                    code = data["type_code"]
                    if code not in type_code_map:
                        type_code_map[code] = []
                    type_code_map[code].append(source_type)

            for code in sorted(type_code_map.keys()):
                sources = ", ".join(type_code_map[code])
                print(f"Type Code {code:2d}: {sources}")

            print("\n" + "=" * 80)
            print("DETAILED RESULTS:")
            print("=" * 80)
            for source_type, data in sorted(results.items()):
                print(f"\n{source_type.upper()}:")
                for key, value in data.items():
                    print(f"  {key}: {value}")

        finally:
            # Cleanup
            print("\n\nCleaning up test notebook...")
            try:
                await client.notebooks.delete(nb_id)
                print("✓ Deleted test notebook")
            except Exception as e:
                print(f"✗ Error deleting notebook: {e}")
                print(f"  Please manually delete notebook: {nb_id}")


if __name__ == "__main__":
    asyncio.run(main())
