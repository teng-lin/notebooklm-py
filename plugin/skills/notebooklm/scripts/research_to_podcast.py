# /// script
# requires-python = ">=3.10"
# dependencies = ["notebooklm-py>=0.8.0a3"]
# ///
"""Research a topic and turn the findings into a podcast.

Creates (or reuses) a notebook, runs NotebookLM web research on TOPIC,
imports the discovered sources, generates an Audio Overview, and downloads
the finished podcast.

Usage:
    uv run scripts/research_to_podcast.py "renewable energy trends 2024"
    uv run scripts/research_to_podcast.py "AI safety" --mode deep --max-sources 15

    # Or, if notebooklm-py is already installed:
    python3 scripts/research_to_podcast.py "renewable energy trends 2024"

Prerequisites: `notebooklm login` must have been run at least once (see
../SKILL.md). Progress is printed to stderr; the final result is a single
JSON line on stdout.

Exit codes: 0 success, 1 user/auth error, 2 timeout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from notebooklm import NotebookLMClient


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic", nargs="+", help="Research topic (quoting optional)")
    parser.add_argument(
        "--mode",
        choices=["fast", "deep"],
        default="fast",
        help="Research depth: fast (seconds, ~5-10 sources) or deep (15-30+ min, 20+ sources). "
        "Default: fast.",
    )
    parser.add_argument(
        "--max-sources",
        type=int,
        default=10,
        help="Maximum number of discovered sources to import (default: 10).",
    )
    parser.add_argument(
        "--research-timeout",
        type=float,
        default=None,
        help="Seconds to wait for research to finish (default: 180 for fast, 1800 for deep).",
    )
    parser.add_argument(
        "--audio-timeout",
        type=float,
        default=1200.0,
        help="Seconds to wait for audio generation to finish (default: 1200).",
    )
    parser.add_argument(
        "--output",
        default="podcast.mp3",
        help="Output path for the downloaded podcast (default: podcast.mp3).",
    )
    parser.add_argument(
        "--notebook-id",
        default=None,
        help="Reuse an existing notebook instead of creating a new one.",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    topic = " ".join(args.topic)
    research_timeout = args.research_timeout
    if research_timeout is None:
        research_timeout = 1800.0 if args.mode == "deep" else 180.0

    try:
        async with NotebookLMClient.from_storage() as client:
            if args.notebook_id:
                notebook_id = args.notebook_id
                print(f"Using existing notebook {notebook_id}", file=sys.stderr)
            else:
                print(f"Creating notebook for '{topic}'...", file=sys.stderr)
                notebook = await client.notebooks.create(f"Research: {topic}")
                notebook_id = notebook.id
                print(f"  Created notebook {notebook_id}", file=sys.stderr)

            print(
                f"Starting {args.mode} research (timeout {research_timeout:.0f}s)...",
                file=sys.stderr,
            )
            start = await client.research.start(notebook_id, topic, source="web", mode=args.mode)

            print("Waiting for research to complete...", file=sys.stderr)
            try:
                task = await client.research.wait_for_completion(
                    notebook_id, start.task_id, timeout=research_timeout
                )
            except TimeoutError:
                print(f"Research timed out after {research_timeout:.0f}s", file=sys.stderr)
                return 2

            if task.status != "completed":
                print(f"Research ended with status: {task.status}", file=sys.stderr)
                return 1

            sources = list(task.sources)[: args.max_sources]
            print(
                f"Found {len(task.sources)} sources; importing {len(sources)}...",
                file=sys.stderr,
            )
            imported = 0
            if sources:
                imported_rows = await client.research.import_sources(
                    notebook_id, task.task_id, sources
                )
                imported = len(imported_rows)

            print("Generating podcast...", file=sys.stderr)
            generation = await client.artifacts.generate_audio(
                notebook_id, instructions=f"Create an engaging overview of {topic}"
            )

            print(
                f"Waiting for audio generation (timeout {args.audio_timeout:.0f}s)...",
                file=sys.stderr,
            )
            try:
                final = await client.artifacts.wait_for_completion(
                    notebook_id, generation.task_id, timeout=args.audio_timeout
                )
            except TimeoutError:
                print(
                    f"Audio generation timed out after {args.audio_timeout:.0f}s", file=sys.stderr
                )
                return 2

            if not final.is_complete:
                print(f"Generation ended with status: {final.status}", file=sys.stderr)
                return 1

            print(f"Downloading podcast to {args.output}...", file=sys.stderr)
            output_path = await client.artifacts.download_audio(
                notebook_id, args.output, generation.task_id
            )

            print(
                json.dumps(
                    {
                        "notebook_id": notebook_id,
                        "task_id": generation.task_id,
                        "sources_found": len(task.sources),
                        "sources_imported": imported,
                        "output": output_path,
                    }
                )
            )
            return 0
    except (FileNotFoundError, ValueError) as exc:
        print(f"Authentication error: {exc}", file=sys.stderr)
        print("Run `notebooklm login` first.", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
