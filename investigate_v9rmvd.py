#!/usr/bin/env python3
"""Investigation script to test the v9rmvd RPC for quiz/flashcard content."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from notebooklm import NotebookLMClient


async def investigate_v9rmvd():
    """Test the v9rmvd RPC to fetch quiz/flashcard content."""

    notebook_id = "167481cd-23a3-4331-9a45-c8948900bf91"
    quiz_id = "a0e4dca6-3bb0-4ed0-aea1-dfa91cf89767"  # Agent Quiz
    flashcard_id = "173255d8-12b3-4c67-b925-a76ce6c71735"  # Agent Flashcards

    async with await NotebookLMClient.from_storage() as client:
        print("Testing v9rmvd RPC for quiz content...")
        print(f"Quiz ID: {quiz_id}")

        # Based on the curl, the params are just [artifact_id]
        params = [quiz_id]

        try:
            result = await client._core.rpc_call(
                "v9rmvd",  # New RPC ID we discovered!
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )

            if result:
                print(f"\n✓ SUCCESS! Got quiz content!")
                print(f"Result type: {type(result)}")
                if isinstance(result, list):
                    print(f"Result length: {len(result)}")

                # Save full result
                with open("investigation_output/quiz_content_v9rmvd.json", "w") as f:
                    json.dump(result, f, indent=2, default=str, ensure_ascii=False)
                print("\nFull result saved to: investigation_output/quiz_content_v9rmvd.json")

                # Preview the content
                print(f"\nResult preview:\n{json.dumps(result, indent=2, default=str, ensure_ascii=False)[:2000]}")
            else:
                print("No result (None)")

        except Exception as e:
            print(f"Error: {e}")

        print("\n" + "="*60)
        print("Testing v9rmvd RPC for flashcard content...")
        print(f"Flashcard ID: {flashcard_id}")

        params = [flashcard_id]

        try:
            result = await client._core.rpc_call(
                "v9rmvd",
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )

            if result:
                print(f"\n✓ SUCCESS! Got flashcard content!")
                print(f"Result type: {type(result)}")

                # Save full result
                with open("investigation_output/flashcard_content_v9rmvd.json", "w") as f:
                    json.dump(result, f, indent=2, default=str, ensure_ascii=False)
                print("\nFull result saved to: investigation_output/flashcard_content_v9rmvd.json")

                # Preview
                print(f"\nResult preview:\n{json.dumps(result, indent=2, default=str, ensure_ascii=False)[:2000]}")
            else:
                print("No result (None)")

        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(investigate_v9rmvd())
