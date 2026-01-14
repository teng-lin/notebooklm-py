#!/usr/bin/env python3
"""Bulk import sources example.

This script demonstrates:
1. Create a notebook
2. Add multiple sources of different types
3. Handle errors gracefully
4. Report import status

Prerequisites:
    pip install "notebooklm-py[browser]"
    notebooklm login

Usage:
    python bulk-import.py
"""

import asyncio

from notebooklm import NotebookLMClient

# Example sources to import
SOURCES = {
    "urls": [
        "https://en.wikipedia.org/wiki/Machine_learning",
        "https://en.wikipedia.org/wiki/Deep_learning",
    ],
    "youtube": [
        "https://www.youtube.com/watch?v=aircAruvnKk",  # 3Blue1Brown neural networks
    ],
    "text": [
        {
            "title": "Project Notes",
            "content": """
            Key points for our ML research project:
            - Focus on transformer architectures
            - Compare with traditional RNN approaches
            - Benchmark on standard datasets
            """,
        },
    ],
}


async def main():
    print("=== Bulk Import Example ===\n")

    async with await NotebookLMClient.from_storage() as client:
        # 1. Create a notebook
        print("Creating notebook...")
        nb = await client.notebooks.create("Bulk Import Demo")
        print(f"  Created: {nb.id}\n")

        results = {"success": [], "failed": []}

        # 2. Import URLs
        print("Importing URLs...")
        for url in SOURCES["urls"]:
            try:
                source = await client.sources.add_url(nb.id, url)
                results["success"].append(f"URL: {source.title}")
                print(f"  + {source.title}")
            except Exception as e:
                results["failed"].append(f"URL: {url} - {e}")
                print(f"  - Failed: {url}")

        # 3. Import YouTube videos (add_url auto-detects YouTube)
        print("\nImporting YouTube videos...")
        for url in SOURCES["youtube"]:
            try:
                source = await client.sources.add_url(nb.id, url)
                results["success"].append(f"YouTube: {source.title}")
                print(f"  + {source.title}")
            except Exception as e:
                results["failed"].append(f"YouTube: {url} - {e}")
                print(f"  - Failed: {url}")

        # 4. Import text content
        print("\nImporting text content...")
        for item in SOURCES["text"]:
            try:
                source = await client.sources.add_text(nb.id, item["title"], item["content"])
                results["success"].append(f"Text: {source.title}")
                print(f"  + {source.title}")
            except Exception as e:
                results["failed"].append(f"Text: {item['title']} - {e}")
                print(f"  - Failed: {item['title']}")

        # 5. Report results
        print("\n" + "=" * 40)
        print("Import complete!")
        print(f"  Successful: {len(results['success'])}")
        print(f"  Failed: {len(results['failed'])}")

        if results["failed"]:
            print("\nFailed imports:")
            for item in results["failed"]:
                print(f"  - {item}")

        print(f"\n  Notebook ID: {nb.id}")
        print("  (Notebook kept for review - delete manually when done)")

    print("\n=== Done! ===")


if __name__ == "__main__":
    asyncio.run(main())
