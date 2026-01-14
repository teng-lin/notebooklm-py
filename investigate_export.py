#!/usr/bin/env python3
"""Investigation script to try EXPORT_ARTIFACT for quiz/flashcard content."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod, ExportType


async def investigate_export():
    """Try EXPORT_ARTIFACT RPC for quiz/flashcard content."""

    notebook_id = "167481cd-23a3-4331-9a45-c8948900bf91"
    flashcard_id = "173255d8-12b3-4c67-b925-a76ce6c71735"
    quiz_id = "a0e4dca6-3bb0-4ed0-aea1-dfa91cf89767"

    async with await NotebookLMClient.from_storage() as client:
        print("Testing EXPORT_ARTIFACT (Krh3pd) for flashcards...")

        # Try export to Docs
        params = [None, flashcard_id, None, "Flashcard Export", 1]  # ExportType.DOCS = 1
        print(f"\nParams: {params}")

        try:
            result = await client._core.rpc_call(
                RPCMethod.EXPORT_ARTIFACT,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )

            if result:
                print(f"Got result! Type: {type(result)}")
                print(f"Result: {json.dumps(result, indent=2, default=str)[:1000]}")
                with open("investigation_output/export_flashcard_result.json", "w") as f:
                    json.dump(result, f, indent=2, default=str)
            else:
                print("No result (None)")

        except Exception as e:
            print(f"Error: {e}")

        print("\n\nTesting EXPORT_ARTIFACT for quiz...")
        params = [None, quiz_id, None, "Quiz Export", 1]
        print(f"Params: {params}")

        try:
            result = await client._core.rpc_call(
                RPCMethod.EXPORT_ARTIFACT,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )

            if result:
                print(f"Got result! Type: {type(result)}")
                print(f"Result: {json.dumps(result, indent=2, default=str)[:1000]}")
                with open("investigation_output/export_quiz_result.json", "w") as f:
                    json.dump(result, f, indent=2, default=str)
            else:
                print("No result (None)")

        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(investigate_export())
