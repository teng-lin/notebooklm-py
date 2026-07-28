# /// script
# requires-python = ">=3.10"
# dependencies = ["notebooklm-py>=0.8.0a3"]
# ///
"""Generate a studio artifact, wait for it to finish, and download it.

Wraps the generate -> wait -> download sequence for a single artifact TYPE in
one call, so an agent doesn't have to babysit three separate CLI/API calls.

Usage:
    uv run scripts/generate_artifact.py audio -n <notebook_id> "Focus on key trends"
    uv run scripts/generate_artifact.py quiz -n <notebook_id> --output quiz.json
    uv run scripts/generate_artifact.py mind-map -n <notebook_id> --language ja

    # Or, if notebooklm-py is already installed:
    python3 scripts/generate_artifact.py report -n <notebook_id> --output report.md

TYPE is one of: audio, video, cinematic-video, report, quiz, flashcards,
infographic, slide-deck, data-table, mind-map. PROMPT is optional free-text
instructions/description; `quiz` and `flashcards` do not support --language
(the API has no language parameter for those two types). `mind-map` generates
the synchronous note-backed mind map (no wait needed); the interactive,
polled kind is only available via `notebooklm generate mind-map --kind
interactive` on the CLI.

Prerequisites: `notebooklm login` must have been run at least once (see
../SKILL.md). Progress is printed to stderr; the final result is a single
JSON line on stdout.

Exit codes: 0 success, 1 user/auth error or generation failed, 2 timeout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from notebooklm import NotebookLMClient, ReportFormat

# type -> (download attribute name, default output extension, default timeout seconds)
_ARTIFACT_SPECS: dict[str, tuple[str, str, float]] = {
    "audio": ("download_audio", ".mp3", 1200.0),
    "video": ("download_video", ".mp4", 1800.0),
    "cinematic-video": ("download_video", ".mp4", 3600.0),
    "report": ("download_report", ".md", 300.0),
    "quiz": ("download_quiz", ".json", 300.0),
    "flashcards": ("download_flashcards", ".json", 300.0),
    "infographic": ("download_infographic", ".png", 300.0),
    "slide-deck": ("download_slide_deck", ".pdf", 300.0),
    "data-table": ("download_data_table", ".csv", 300.0),
    "mind-map": ("download_mind_map", ".json", 0.0),
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("type", choices=sorted(_ARTIFACT_SPECS), help="Artifact type to generate")
    parser.add_argument("-n", "--notebook", required=True, help="Notebook ID")
    parser.add_argument(
        "prompt", nargs="?", default=None, help="Instructions/description (optional)"
    )
    parser.add_argument("--output", default=None, help="Output path (default: <type><ext>)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Seconds to wait for generation (default varies by type; see docs).",
    )
    parser.add_argument(
        "--language", default="en", help="Output language (ignored for quiz/flashcards)."
    )
    return parser.parse_args(argv)


async def _generate(
    client, artifact_type: str, notebook_id: str, prompt: str | None, language: str
):
    """Dispatch to the right ``generate_*`` call for ``artifact_type``."""
    artifacts = client.artifacts
    if artifact_type == "audio":
        return await artifacts.generate_audio(notebook_id, language=language, instructions=prompt)
    if artifact_type == "video":
        return await artifacts.generate_video(notebook_id, language=language, instructions=prompt)
    if artifact_type == "cinematic-video":
        return await artifacts.generate_cinematic_video(
            notebook_id, language=language, instructions=prompt
        )
    if artifact_type == "infographic":
        return await artifacts.generate_infographic(
            notebook_id, language=language, instructions=prompt
        )
    if artifact_type == "slide-deck":
        return await artifacts.generate_slide_deck(
            notebook_id, language=language, instructions=prompt
        )
    if artifact_type == "data-table":
        return await artifacts.generate_data_table(
            notebook_id, language=language, instructions=prompt
        )
    if artifact_type == "quiz":
        return await artifacts.generate_quiz(notebook_id, instructions=prompt)
    if artifact_type == "flashcards":
        return await artifacts.generate_flashcards(notebook_id, instructions=prompt)
    if artifact_type == "report":
        if prompt:
            return await artifacts.generate_report(
                notebook_id,
                report_format=ReportFormat.CUSTOM,
                custom_prompt=prompt,
                language=language,
            )
        return await artifacts.generate_report(notebook_id, language=language)
    if artifact_type == "mind-map":
        return await artifacts.generate_mind_map(
            notebook_id, language=language, instructions=prompt
        )
    raise ValueError(
        f"Unknown artifact type: {artifact_type}"
    )  # pragma: no cover - guarded by argparse


async def run(args: argparse.Namespace) -> int:
    download_attr, default_ext, default_timeout = _ARTIFACT_SPECS[args.type]
    output = args.output or f"{args.type}{default_ext}"
    timeout = args.timeout if args.timeout is not None else default_timeout

    try:
        async with NotebookLMClient.from_storage() as client:
            print(f"Generating {args.type}...", file=sys.stderr)
            status = await _generate(client, args.type, args.notebook, args.prompt, args.language)

            if args.type == "mind-map":
                # generate_mind_map is synchronous (note-backed) - no polling needed.
                print(f"Downloading to {output}...", file=sys.stderr)
                download = getattr(client.artifacts, download_attr)
                output_path = await download(args.notebook, output, status.note_id)
                print(
                    json.dumps(
                        {
                            "notebook_id": args.notebook,
                            "note_id": status.note_id,
                            "output": output_path,
                        }
                    )
                )
                return 0

            print(f"Waiting for generation (timeout {timeout:.0f}s)...", file=sys.stderr)
            try:
                final = await client.artifacts.wait_for_completion(
                    args.notebook, status.task_id, timeout=timeout
                )
            except TimeoutError:
                print(f"Generation timed out after {timeout:.0f}s", file=sys.stderr)
                return 2

            if not final.is_complete:
                print(f"Generation ended with status: {final.status}", file=sys.stderr)
                return 1

            print(f"Downloading to {output}...", file=sys.stderr)
            download = getattr(client.artifacts, download_attr)
            output_path = await download(args.notebook, output, final.task_id)

            print(
                json.dumps(
                    {
                        "notebook_id": args.notebook,
                        "task_id": final.task_id,
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
