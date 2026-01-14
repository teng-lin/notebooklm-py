#!/usr/bin/env python3
"""Investigation script to try POLL_STUDIO for detailed artifact data."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod


async def investigate_poll_studio():
    """Try POLL_STUDIO RPC with different parameter formats."""

    notebook_id = "167481cd-23a3-4331-9a45-c8948900bf91"
    flashcard_id = "173255d8-12b3-4c67-b925-a76ce6c71735"
    quiz_id = "a0e4dca6-3bb0-4ed0-aea1-dfa91cf89767"

    async with await NotebookLMClient.from_storage() as client:
        print("Testing POLL_STUDIO RPC (gArtLc) for flashcards...")

        # Standard format from existing code
        params = [flashcard_id, notebook_id, [2]]
        print(f"\nParams: {params}")

        try:
            result = await client._core.rpc_call(
                RPCMethod.POLL_STUDIO,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )

            if result:
                print(f"Got result! Type: {type(result)}, Length: {len(result) if isinstance(result, (list, dict, str)) else 'N/A'}")
                with open("investigation_output/poll_studio_flashcard.json", "w") as f:
                    json.dump(result, f, indent=2, default=str)
                print("Saved to poll_studio_flashcard.json")
                print(f"\nResult preview: {str(result)[:500]}...")
            else:
                print("No result (None)")

        except Exception as e:
            print(f"Error: {e}")

        print("\n\nTesting POLL_STUDIO RPC for quiz...")
        params = [quiz_id, notebook_id, [2]]
        print(f"Params: {params}")

        try:
            result = await client._core.rpc_call(
                RPCMethod.POLL_STUDIO,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )

            if result:
                print(f"Got result! Type: {type(result)}, Length: {len(result) if isinstance(result, (list, dict, str)) else 'N/A'}")
                with open("investigation_output/poll_studio_quiz.json", "w") as f:
                    json.dump(result, f, indent=2, default=str)
                print("Saved to poll_studio_quiz.json")
                print(f"\nResult preview: {str(result)[:500]}...")
            else:
                print("No result (None)")

        except Exception as e:
            print(f"Error: {e}")

        # Try with different modes/flags
        print("\n\nTrying with extended params...")
        extended_params = [
            [flashcard_id, notebook_id, [2], None, True],
            [flashcard_id, notebook_id, [2], 1],
            [[2], flashcard_id, notebook_id],
        ]

        for params in extended_params:
            print(f"\nParams: {params}")
            try:
                result = await client._core.rpc_call(
                    RPCMethod.POLL_STUDIO,
                    params,
                    source_path=f"/notebook/{notebook_id}",
                    allow_null=True,
                )
                if result:
                    print(f"  SUCCESS! {str(result)[:200]}...")
                else:
                    print("  No result")
            except Exception as e:
                print(f"  Error: {e}")


if __name__ == "__main__":
    asyncio.run(investigate_poll_studio())
