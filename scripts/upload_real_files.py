#!/usr/bin/env python3
"""Download real files from the web and upload to test source types."""

import asyncio
import tempfile
from pathlib import Path

import httpx

from notebooklm import NotebookLMClient


# URLs to real files
TEST_FILES = {
    "png": {
        "url": "https://www.google.com/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png",
        "filename": "google_logo.png",
    },
    "jpg": {
        "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/280px-PNG_transparency_demonstration_1.png",
        "filename": "wiki_image.png",
    },
    "mp3": {
        # NASA public domain audio
        "url": "https://www.nasa.gov/wp-content/uploads/2015/01/590325main_ringtone_kennedy_702.mp3",
        "filename": "nasa_audio.mp3",
    },
}


async def download_file(url: str, path: Path) -> bool:
    """Download a file from URL."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http:
            response = await http.get(url)
            response.raise_for_status()
            path.write_bytes(response.content)
            return True
    except Exception as e:
        print(f"  Download error: {e}")
        return False


async def upload_and_wait(client, notebook_id: str, file_path: Path, label: str) -> dict:
    """Upload a file and wait for it to be processed."""
    print(f"\n{'='*60}")
    print(f"Uploading {label}: {file_path.name} ({file_path.stat().st_size} bytes)")
    print(f"{'='*60}")

    try:
        # Upload file
        source = await client.sources.add_file(notebook_id, file_path)
        print(f"  Source ID: {source.id}")
        print(f"  Title: {source.title}")

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
    """Download and upload test files."""
    notebook_id = "7d019668-04c9-4b77-ba6a-b7388a2c0abe"

    async with await NotebookLMClient.from_storage() as client:
        print("=" * 70)
        print("SOURCE TYPE INVESTIGATION - REAL FILES")
        print(f"Notebook: {notebook_id}")
        print("=" * 70)

        results = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Download and upload each file type
            for file_type, info in TEST_FILES.items():
                file_path = tmp_path / info["filename"]
                print(f"\nDownloading {file_type.upper()}: {info['url'][:60]}...")

                if await download_file(info["url"], file_path):
                    print(f"  Downloaded: {file_path.stat().st_size} bytes")
                    result = await upload_and_wait(client, notebook_id, file_path, file_type.upper())
                    results.append(result)
                else:
                    results.append({"label": file_type.upper(), "error": "Download failed"})

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
