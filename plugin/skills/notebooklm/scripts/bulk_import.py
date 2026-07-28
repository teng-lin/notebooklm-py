# /// script
# requires-python = ">=3.10"
# dependencies = ["notebooklm-py>=0.8.0a3"]
# ///
"""Bulk-import URLs and local files into a NotebookLM notebook.

Adds each SOURCE (a URL, or a path to a local file) to a notebook, optionally
waits for all of them to finish processing, and prints a JSON summary.

Usage:
    uv run scripts/bulk_import.py https://example.com/a https://example.com/b ./notes.pdf
    uv run scripts/bulk_import.py --from-file sources.txt --title "Research Collection"
    uv run scripts/bulk_import.py https://example.com -n <existing-notebook-id> --no-wait

    # Or, if notebooklm-py is already installed:
    python3 scripts/bulk_import.py https://example.com/a

`--from-file` reads one source per line (blank lines and lines starting with
`#` are skipped) and is combined with any SOURCE positional arguments.

Prerequisites: `notebooklm login` must have been run at least once (see
../SKILL.md). Progress is printed to stderr; the final result is a single
JSON line on stdout.

Exit codes: 0 success (at least one source added), 1 user/auth error or all
adds failed, 2 timeout waiting for sources to become ready.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from notebooklm import NotebookLMClient


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", help="URLs and/or local file paths to add")
    parser.add_argument(
        "--from-file",
        default=None,
        help="Path to a text file with one source (URL or file path) per line.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-n", "--notebook-id", default=None, help="Add sources to an existing notebook."
    )
    group.add_argument(
        "--title", default=None, help="Title for a new notebook (default: 'Bulk Import')."
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Do not wait for sources to finish processing before exiting.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Per-source seconds to wait for processing when waiting (default: 600).",
    )
    return parser.parse_args(argv)


def _collect_sources(args: argparse.Namespace) -> list[str]:
    sources = list(args.sources)
    if args.from_file:
        text = Path(args.from_file).read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                sources.append(stripped)
    return sources


async def run(args: argparse.Namespace) -> int:
    sources = _collect_sources(args)
    if not sources:
        print("No sources given (pass SOURCE args and/or --from-file).", file=sys.stderr)
        return 1

    try:
        async with NotebookLMClient.from_storage() as client:
            if args.notebook_id:
                notebook_id = args.notebook_id
                print(f"Using existing notebook {notebook_id}", file=sys.stderr)
            else:
                title = args.title or "Bulk Import"
                print(f"Creating notebook '{title}'...", file=sys.stderr)
                notebook = await client.notebooks.create(title)
                notebook_id = notebook.id
                print(f"  Created notebook {notebook_id}", file=sys.stderr)

            added: list[dict[str, str]] = []
            failed: list[dict[str, str]] = []

            for raw in sources:
                path = Path(raw)
                try:
                    if path.is_file():
                        print(f"Adding file: {raw}", file=sys.stderr)
                        source = await client.sources.add_file(notebook_id, path)
                    else:
                        print(f"Adding URL: {raw}", file=sys.stderr)
                        source = await client.sources.add_url(notebook_id, raw)
                    added.append({"input": raw, "id": source.id, "title": source.title or ""})
                except Exception as exc:  # noqa: BLE001 - report and continue with other sources
                    print(f"  Failed: {raw} - {exc}", file=sys.stderr)
                    failed.append({"input": raw, "error": str(exc)})

            result = {
                "notebook_id": notebook_id,
                "added": len(added),
                "failed": len(failed),
                "sources": added,
                "errors": failed,
            }

            if not added:
                print(json.dumps(result))
                return 1

            if not args.no_wait:
                print(f"Waiting for {len(added)} source(s) to be ready...", file=sys.stderr)
                try:
                    await client.sources.wait_for_sources(
                        notebook_id, [s["id"] for s in added], timeout=args.timeout
                    )
                    result["ready"] = True
                except TimeoutError:
                    print("Timed out waiting for sources to become ready.", file=sys.stderr)
                    result["ready"] = False
                    print(json.dumps(result))
                    return 2
                except Exception as exc:  # noqa: BLE001 - surface, don't crash the summary
                    print(f"Error waiting for sources: {exc}", file=sys.stderr)
                    result["ready"] = False

            print(json.dumps(result))
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
