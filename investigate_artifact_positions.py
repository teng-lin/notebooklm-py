#!/usr/bin/env python3
"""Investigation script to examine each artifact position in detail."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod


async def investigate_artifact_positions():
    """Examine each position in flashcard/quiz artifact data in detail."""

    notebook_id = "167481cd-23a3-4331-9a45-c8948900bf91"

    async with await NotebookLMClient.from_storage() as client:
        # Get raw artifacts data
        params = [[2], notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"']
        result = await client._core.rpc_call(
            RPCMethod.LIST_ARTIFACTS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        if not result:
            print("No artifacts found")
            return

        artifacts_data = result[0] if isinstance(result[0], list) else result

        # Find flashcard artifact
        flashcard_art = None
        quiz_art = None

        for art in artifacts_data:
            if isinstance(art, list) and len(art) > 2 and art[2] == 4:
                variant = None
                if len(art) > 9 and isinstance(art[9], list) and len(art[9]) > 1:
                    if isinstance(art[9][1], list) and len(art[9][1]) > 0:
                        variant = art[9][1][0]
                if variant == 1:
                    flashcard_art = art
                elif variant == 2:
                    quiz_art = art

        if flashcard_art:
            print("=" * 80)
            print("FLASHCARD ARTIFACT STRUCTURE")
            print("=" * 80)
            print(f"\nTitle: {flashcard_art[1]}")
            print(f"Total length: {len(flashcard_art)} positions")
            print()

            for i, item in enumerate(flashcard_art):
                print(f"\n[{i}] ", end="")
                if item is None:
                    print("None")
                elif isinstance(item, str):
                    if len(item) > 100:
                        print(f"String ({len(item)} chars): {item[:100]}...")
                    else:
                        print(f"String: {repr(item)}")
                elif isinstance(item, (int, float, bool)):
                    print(f"{type(item).__name__}: {item}")
                elif isinstance(item, list):
                    print(f"List[{len(item)}]:")
                    # Show nested structure
                    for j, sub in enumerate(item[:5]):  # First 5 items
                        if isinstance(sub, list):
                            print(f"    [{j}] List[{len(sub)}]: {str(sub)[:100]}...")
                        elif isinstance(sub, str):
                            print(f"    [{j}] String: {repr(sub[:50]) if len(sub) > 50 else repr(sub)}...")
                        else:
                            print(f"    [{j}] {type(sub).__name__}: {str(sub)[:100]}")
                    if len(item) > 5:
                        print(f"    ... and {len(item) - 5} more items")
                elif isinstance(item, dict):
                    print(f"Dict[{len(item)}]: {list(item.keys())[:5]}...")

        # Also check if there's a quiz with more data
        if quiz_art:
            print("\n" + "=" * 80)
            print("QUIZ ARTIFACT STRUCTURE")
            print("=" * 80)
            print(f"\nTitle: {quiz_art[1]}")

            # Look specifically at positions that might hold content
            interesting_positions = [6, 7, 8, 9, 10, 11, 12, 13]
            for i in interesting_positions:
                if i < len(quiz_art) and quiz_art[i] is not None:
                    item = quiz_art[i]
                    print(f"\n[{i}] ", end="")
                    if isinstance(item, list):
                        print(f"List[{len(item)}]:")
                        # Deeper exploration
                        print(f"    Full content: {json.dumps(item, indent=2, default=str)[:500]}...")
                    else:
                        print(f"{type(item).__name__}: {item}")


if __name__ == "__main__":
    asyncio.run(investigate_artifact_positions())
