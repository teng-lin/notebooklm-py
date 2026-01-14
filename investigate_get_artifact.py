#!/usr/bin/env python3
"""Investigation script to try fetching individual artifact content.

Tests the GET_ARTIFACT RPC (BnLyuf) to see if it returns quiz/flashcard questions.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod


async def investigate_get_artifact():
    """Try GET_ARTIFACT RPC to fetch flashcard/quiz content."""

    notebook_id = "167481cd-23a3-4331-9a45-c8948900bf91"  # High School notebook
    flashcard_id = "173255d8-12b3-4c67-b925-a76ce6c71735"  # Agent Flashcards
    quiz_id = "a0e4dca6-3bb0-4ed0-aea1-dfa91cf89767"  # Agent Quiz

    async with await NotebookLMClient.from_storage() as client:
        print("Testing GET_ARTIFACT RPC (BnLyuf)...")

        # Try different parameter formats
        param_formats = [
            ("Format 1: [id]", [flashcard_id]),
            ("Format 2: [[id]]", [[flashcard_id]]),
            ("Format 3: [nb_id, id]", [notebook_id, flashcard_id]),
            ("Format 4: [id, nb_id]", [flashcard_id, notebook_id]),
            ("Format 5: [[2], id]", [[2], flashcard_id]),
            ("Format 6: [[2], nb_id, id]", [[2], notebook_id, flashcard_id]),
        ]

        for desc, params in param_formats:
            print(f"\n{desc}: {params}")
            try:
                result = await client._core.rpc_call(
                    RPCMethod.GET_ARTIFACT,
                    params,
                    source_path=f"/notebook/{notebook_id}",
                    allow_null=True,
                )
                if result:
                    print(f"  SUCCESS! Result type: {type(result)}")
                    if isinstance(result, list):
                        print(f"  Length: {len(result)}")
                        # Save result
                        with open("investigation_output/get_artifact_result.json", "w") as f:
                            json.dump(result, f, indent=2, default=str)
                        print("  Saved to get_artifact_result.json")
                        return result
                    else:
                        print(f"  Result: {result}")
                else:
                    print("  No result (None)")
            except Exception as e:
                print(f"  Error: {e}")

        # Also try LIST_ARTIFACTS_ALT
        print("\n\nTesting LIST_ARTIFACTS_ALT RPC (LfTXoe)...")
        alt_param_formats = [
            ("Format 1: [nb_id]", [notebook_id]),
            ("Format 2: [[2], nb_id]", [[2], notebook_id]),
            ("Format 3: [nb_id, id]", [notebook_id, flashcard_id]),
        ]

        for desc, params in alt_param_formats:
            print(f"\n{desc}: {params}")
            try:
                result = await client._core.rpc_call(
                    RPCMethod.LIST_ARTIFACTS_ALT,
                    params,
                    source_path=f"/notebook/{notebook_id}",
                    allow_null=True,
                )
                if result:
                    print(f"  SUCCESS! Result type: {type(result)}")
                    if isinstance(result, list):
                        print(f"  Length: {len(result)}")
                        with open("investigation_output/list_artifacts_alt_result.json", "w") as f:
                            json.dump(result, f, indent=2, default=str)
                        print("  Saved to list_artifacts_alt_result.json")
                    else:
                        print(f"  Result: {result}")
                else:
                    print("  No result (None)")
            except Exception as e:
                print(f"  Error: {e}")


if __name__ == "__main__":
    asyncio.run(investigate_get_artifact())
