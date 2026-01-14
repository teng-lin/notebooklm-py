#!/usr/bin/env python3
"""Quickstart example for notebooklm-py.

This script demonstrates a complete workflow:
1. Create a notebook
2. Add sources
3. Chat with content
4. Generate a podcast
5. Download the result

Prerequisites:
    pip install "notebooklm-py[browser]"
    playwright install chromium
    notebooklm login  # Authenticate first

Usage:
    python quickstart.py
"""

import asyncio

from notebooklm import NotebookLMClient


async def main():
    print("=== NotebookLM Quickstart ===\n")

    async with await NotebookLMClient.from_storage() as client:
        # 1. Create a notebook
        print("Creating notebook...")
        nb = await client.notebooks.create("Quickstart Demo")
        print(f"  Created: {nb.id} - {nb.title}\n")

        # 2. Add a source
        print("Adding source...")
        url = "https://en.wikipedia.org/wiki/Artificial_intelligence"
        source = await client.sources.add_url(nb.id, url)
        print(f"  Added: {source.title}\n")

        # 3. Chat with the content
        print("Asking a question...")
        result = await client.chat.ask(nb.id, "What are the main topics covered?")
        print(f"  Answer: {result.answer[:200]}...\n")

        # 4. Generate an audio overview
        print("Generating podcast (this may take a few minutes)...")
        status = await client.artifacts.generate_audio(
            nb.id, instructions="Focus on the history and key milestones"
        )
        print(f"  Started generation, task_id: {status.task_id}")

        # Wait for completion
        final = await client.artifacts.wait_for_completion(
            nb.id, status.task_id, timeout=300, poll_interval=10
        )

        if final.is_complete:
            print(f"  Complete! URL: {final.url}\n")

            # 5. Download (requires browser support)
            # output_path = await client.artifacts.download_audio(nb.id, "./podcast.mp3")
            # print(f"  Downloaded to: {output_path}")
        else:
            print(f"  Generation status: {final.status}\n")

        # Cleanup: Delete the demo notebook
        print("Cleaning up...")
        await client.notebooks.delete(nb.id)
        print("  Deleted demo notebook\n")

    print("=== Done! ===")


if __name__ == "__main__":
    asyncio.run(main())
