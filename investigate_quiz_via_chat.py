#!/usr/bin/env python3
"""Investigation script to try fetching quiz/flashcard content via chat-like requests."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod


async def investigate_quiz_via_chat():
    """Try to fetch quiz/flashcard content through various approaches."""

    notebook_id = "167481cd-23a3-4331-9a45-c8948900bf91"
    flashcard_id = "173255d8-12b3-4c67-b925-a76ce6c71735"
    quiz_id = "a0e4dca6-3bb0-4ed0-aea1-dfa91cf89767"

    async with await NotebookLMClient.from_storage() as client:
        # First let's check if the chat API can retrieve quiz content
        print("Attempting to retrieve quiz content via chat...\n")

        # Try using an artifact-related prompt through chat.ask()
        try:
            print("Testing: Ask about flashcard content")
            result = await client.chat.ask(
                notebook_id,
                "Show me the flashcards from this notebook"
            )
            print(f"Response: {result.answer[:500]}...")
            print()
        except Exception as e:
            print(f"Error: {e}\n")

        # Try to see if there's a specific RPC for getting artifact content
        # Let's look at what methods are available in CREATE_ARTIFACT / GET_ARTIFACT style
        print("\nTrying CREATE_VIDEO RPC with artifact ID to get content...")

        # The xpWGLf RPC is CREATE_ARTIFACT - maybe it has a "get" mode
        try:
            params = [
                [2],
                notebook_id,
                [
                    flashcard_id,  # Try passing existing artifact ID
                    None,
                    4,  # Type 4 = quiz/flashcard
                ]
            ]
            result = await client._core.rpc_call(
                RPCMethod.CREATE_ARTIFACT,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )
            if result:
                print(f"CREATE_ARTIFACT result: {json.dumps(result, indent=2, default=str)[:500]}")
            else:
                print("No result")
        except Exception as e:
            print(f"Error: {e}")

        # Check the streaming endpoint format - maybe quiz content comes through there
        print("\n\nChecking if quiz/flashcard has special rendering requirements...")

        # Get all artifacts and look more carefully at type 4
        params = [[2], notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"']
        result = await client._core.rpc_call(
            RPCMethod.LIST_ARTIFACTS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        if result:
            artifacts_data = result[0] if isinstance(result[0], list) else result

            for art in artifacts_data:
                if isinstance(art, list) and len(art) > 2 and art[2] == 4:
                    print(f"\nType 4 artifact: {art[1]}")
                    print(f"  Full raw data ({len(art)} elements):")
                    # Save complete data for offline analysis
                    with open(f"investigation_output/type4_full_{art[0][:8]}.json", "w") as f:
                        json.dump(art, f, indent=2, default=str)
                    print(f"  Saved to investigation_output/type4_full_{art[0][:8]}.json")


if __name__ == "__main__":
    asyncio.run(investigate_quiz_via_chat())
