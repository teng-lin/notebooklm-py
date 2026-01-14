#!/usr/bin/env python3
"""Investigation script to try ACT_ON_SOURCES for quiz/flashcard content."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod


async def investigate_act_on_sources():
    """Try ACT_ON_SOURCES RPC with different action types."""

    notebook_id = "167481cd-23a3-4331-9a45-c8948900bf91"
    flashcard_id = "173255d8-12b3-4c67-b925-a76ce6c71735"

    # Get source IDs from the notebook
    async with await NotebookLMClient.from_storage() as client:
        source_ids = await client._core.get_source_ids(notebook_id)
        print(f"Notebook has {len(source_ids)} sources")
        source_ids_nested = [[[sid]] for sid in source_ids]

        # Actions to try - based on what we know works for mind maps
        actions_to_try = [
            "get_quiz",
            "get_flashcards",
            "quiz",
            "flashcard",
            "flashcards",
            "quiz_content",
            "flashcard_content",
            "get_artifact_content",
        ]

        for action in actions_to_try:
            print(f"\n{'='*60}")
            print(f"Trying action: {action}")

            params = [
                source_ids_nested,
                None,
                None,
                None,
                None,
                [action, [["[CONTEXT]", ""]], ""],
                None,
                [2, None, [1]],
            ]

            try:
                result = await client._core.rpc_call(
                    RPCMethod.ACT_ON_SOURCES,
                    params,
                    source_path=f"/notebook/{notebook_id}",
                    allow_null=True,
                )

                if result:
                    print(f"  SUCCESS! Type: {type(result)}")
                    print(f"  Preview: {str(result)[:500]}...")
                    with open(f"investigation_output/act_on_sources_{action}.json", "w") as f:
                        json.dump(result, f, indent=2, default=str)
                else:
                    print("  No result (None)")

            except Exception as e:
                print(f"  Error: {e}")

        # Also try with artifact ID included
        print(f"\n\n{'='*60}")
        print("Trying with artifact ID in params...")

        artifact_params = [
            source_ids_nested,
            flashcard_id,  # Include artifact ID
            None,
            None,
            None,
            ["get_content", [["[CONTEXT]", ""]], ""],
            None,
            [2, None, [1]],
        ]

        try:
            result = await client._core.rpc_call(
                RPCMethod.ACT_ON_SOURCES,
                artifact_params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )
            if result:
                print(f"  SUCCESS! {str(result)[:500]}...")
            else:
                print("  No result (None)")
        except Exception as e:
            print(f"  Error: {e}")


if __name__ == "__main__":
    asyncio.run(investigate_act_on_sources())
