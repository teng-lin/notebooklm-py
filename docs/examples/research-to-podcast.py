#!/usr/bin/env python3
"""Research to podcast workflow example.

This script demonstrates:
1. Create a notebook
2. Start deep research on a topic
3. Import discovered sources
4. Generate a podcast from the research

Prerequisites:
    pip install "notebooklm-py[browser]"
    notebooklm login

Usage:
    python research-to-podcast.py "Your research topic"
"""

import asyncio
import sys

from notebooklm import NotebookLMClient


async def main(topic: str):
    print(f"=== Research to Podcast: {topic} ===\n")

    async with await NotebookLMClient.from_storage() as client:
        # 1. Create a notebook
        print("Creating notebook...")
        nb = await client.notebooks.create(f"Research: {topic}")
        print(f"  Created: {nb.id}\n")

        # 2. Start deep research
        print("Starting deep research (this may take a while)...")
        research = await client.research.start(nb.id, topic, source="web", mode="deep")
        task_id = research.get("task_id") if research else None
        print(f"  Task ID: {task_id}\n")

        # 3. Poll for completion
        print("Waiting for research to complete...")
        max_polls = 30
        for i in range(max_polls):
            status = await client.research.poll(nb.id)
            state = status.get("status", "unknown")
            print(f"  Poll {i + 1}/{max_polls}: {state}")

            if state == "completed":
                sources = status.get("sources", [])
                print(f"  Found {len(sources)} sources!\n")
                break

            await asyncio.sleep(10)
        else:
            print("  Research timed out\n")
            return

        # 4. Import discovered sources
        if sources:
            print("Importing sources...")
            await client.research.import_sources(nb.id, task_id, sources[:10])  # Limit to 10
            print(f"  Imported {min(len(sources), 10)} sources\n")

        # 5. Generate podcast
        print("Generating podcast...")
        gen_status = await client.artifacts.generate_audio(
            nb.id, instructions=f"Create an engaging overview of {topic}"
        )

        print("Waiting for audio generation...")
        final = await client.artifacts.wait_for_completion(nb.id, gen_status.task_id, timeout=600)

        if final.is_complete:
            print(f"\n  Success! Audio URL: {final.url}")
            print("\n  Use 'notebooklm download audio' to save the file")
        else:
            print(f"\n  Generation ended with status: {final.status}")

        print(f"\n  Notebook ID: {nb.id}")
        print("  (Notebook kept for review - delete manually when done)")

    print("\n=== Done! ===")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python research-to-podcast.py 'Your research topic'")
        print("Example: python research-to-podcast.py 'renewable energy trends 2024'")
        sys.exit(1)

    topic = " ".join(sys.argv[1:])
    asyncio.run(main(topic))
