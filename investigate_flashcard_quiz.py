#!/usr/bin/env python3
"""Investigation script to analyze flashcard and quiz artifact structures.

This script:
1. Lists all type 4 (quiz/flashcard) artifacts in a notebook
2. Dumps the raw API response for analysis
3. Identifies where content/download URLs might be stored
"""

import asyncio
import json
import sys
from pathlib import Path

# Add src to path for development
sys.path.insert(0, str(Path(__file__).parent / "src"))

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod


def deep_search(obj, depth=0, path="", results=None):
    """Recursively search for string content that might be flashcard/quiz data."""
    if results is None:
        results = []

    max_depth = 20
    if depth > max_depth:
        return results

    if isinstance(obj, str):
        # Look for JSON-like content
        if len(obj) > 50 and ("{" in obj or "[" in obj):
            results.append((path, "potential_json", obj[:200] + "..." if len(obj) > 200 else obj))
        # Look for URLs
        elif obj.startswith("http"):
            results.append((path, "url", obj))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            deep_search(item, depth + 1, f"{path}[{i}]", results)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            deep_search(v, depth + 1, f"{path}.{k}", results)

    return results


async def investigate_notebook(notebook_id: str | None = None):
    """Investigate flashcard and quiz artifact structures in a notebook."""

    async with await NotebookLMClient.from_storage() as client:
        # Get list of notebooks if no ID provided
        if not notebook_id:
            notebooks = await client.notebooks.list()
            if not notebooks:
                print("No notebooks found")
                return

            print("Available notebooks:")
            for i, nb in enumerate(notebooks):
                print(f"  {i+1}. {nb.title} ({nb.id})")

            # Use first notebook
            notebook_id = notebooks[0].id
            print(f"\nUsing notebook: {notebooks[0].title}")

        # Get raw artifacts data
        params = [[2], notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"']
        result = await client._core.rpc_call(
            RPCMethod.LIST_ARTIFACTS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        if not result or not isinstance(result, list):
            print("No artifacts found")
            return

        artifacts_data = result[0] if isinstance(result[0], list) else result

        # Find type 4 (quiz/flashcard) artifacts
        type4_artifacts = [
            a for a in artifacts_data
            if isinstance(a, list) and len(a) > 2 and a[2] == 4
        ]

        if not type4_artifacts:
            print("No quiz/flashcard artifacts found (type 4)")
            print(f"\nFound {len(artifacts_data)} total artifacts")
            for art in artifacts_data:
                if isinstance(art, list) and len(art) > 2:
                    print(f"  - Type {art[2]}: {art[1] if len(art) > 1 else 'unknown'}")
            return

        print(f"\nFound {len(type4_artifacts)} quiz/flashcard artifacts:")

        for i, art in enumerate(type4_artifacts):
            # Determine variant
            variant = None
            if len(art) > 9 and isinstance(art[9], list) and len(art[9]) > 1:
                if isinstance(art[9][1], list) and len(art[9][1]) > 0:
                    variant = art[9][1][0]

            variant_name = "flashcards" if variant == 1 else "quiz" if variant == 2 else "unknown"
            title = art[1] if len(art) > 1 else "untitled"
            status = art[4] if len(art) > 4 else "unknown"

            print(f"\n{'='*60}")
            print(f"Artifact {i+1}: {title}")
            print(f"  Type: {variant_name} (variant={variant})")
            print(f"  Status: {status} (3=completed)")
            print(f"  ID: {art[0]}")
            print(f"  Array length: {len(art)}")

            # Dump structure overview
            print(f"\n  Structure overview:")
            for idx, item in enumerate(art):
                if item is None:
                    item_repr = "None"
                elif isinstance(item, str):
                    item_repr = f"str[{len(item)}]: {item[:50]}..." if len(item) > 50 else f"str: {item}"
                elif isinstance(item, list):
                    item_repr = f"list[{len(item)}]"
                elif isinstance(item, dict):
                    item_repr = f"dict[{len(item)}]"
                else:
                    item_repr = f"{type(item).__name__}: {item}"
                print(f"    [{idx}] {item_repr}")

            # Deep search for interesting content
            interesting = deep_search(art)
            if interesting:
                print(f"\n  Interesting content found:")
                for path, content_type, content in interesting[:20]:  # Limit output
                    print(f"    {path} ({content_type}): {content[:100]}...")

            # Save full artifact data
            output_dir = Path("investigation_output")
            output_dir.mkdir(exist_ok=True)

            filename = f"artifact_{variant_name}_{i+1}.json"
            with open(output_dir / filename, "w") as f:
                json.dump(art, f, indent=2, default=str)
            print(f"\n  Full data saved to: {output_dir / filename}")


async def main():
    """Main entry point."""
    notebook_id = sys.argv[1] if len(sys.argv) > 1 else None
    await investigate_notebook(notebook_id)


if __name__ == "__main__":
    asyncio.run(main())
