#!/usr/bin/env python3
"""One-time maintainer setup for the VCR generation notebook.

This is a **manual, local-only** helper. It is NOT run by CI and must NOT be
called from any test or workflow. Run it once per Google account that records
VCR cassettes for mutation/generation endpoints:

    uv run python tests/scripts/setup-generation-notebook.py

The script is idempotent: if a notebook titled
"VCR Generation Notebook (Tier 8)" already exists on the authenticated
account, its UUID is reported instead of creating a duplicate.

After running, export the printed UUID in your maintainer environment (e.g.
`~/.zshrc` or a per-profile env file):

    export NOTEBOOKLM_GENERATION_NOTEBOOK_ID=<printed-uuid>

The UUID is per-maintainer and MUST NOT be committed to the repository —
notebook IDs are linkable to a Google account and leak account identity. See
`docs/development.md` -> "Cassette recording" for the full env-var workflow.
"""

from __future__ import annotations

import asyncio
import sys

from notebooklm.client import NotebookLMClient

GENERATION_NOTEBOOK_TITLE = "VCR Generation Notebook (Tier 8)"


async def main() -> int:
    """Ensure a generation notebook exists; print its UUID and export line."""
    async with await NotebookLMClient.from_storage() as client:
        print(f"Checking for existing notebook titled {GENERATION_NOTEBOOK_TITLE!r}...")
        notebooks = await client.notebooks.list()
        existing = next(
            (nb for nb in notebooks if nb.title == GENERATION_NOTEBOOK_TITLE),
            None,
        )

        if existing is not None:
            notebook_id = existing.id
            print(f"Found existing notebook: {notebook_id}")
        else:
            print(f"Creating notebook {GENERATION_NOTEBOOK_TITLE!r}...")
            created = await client.notebooks.create(GENERATION_NOTEBOOK_TITLE)
            notebook_id = created.id
            print(f"Created notebook: {notebook_id}")

    separator = "=" * 60
    print()
    print(separator)
    print("Generation notebook is ready.")
    print(f"  Notebook ID: {notebook_id}")
    print(separator)
    print("Export this in your maintainer environment (NOT in the repo):")
    print()
    print(f"  export NOTEBOOKLM_GENERATION_NOTEBOOK_ID={notebook_id}")
    print()
    print("See docs/development.md -> 'Cassette recording' for usage.")
    print(separator)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 — top-level CLI surface
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
