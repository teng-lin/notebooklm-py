#!/usr/bin/env python3
"""Investigation script to check GET_NOTEBOOK for embedded flashcard/quiz data."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod


def find_content(obj, depth=0, path="", max_depth=25):
    """Recursively search for flashcard/quiz-like content."""
    if depth > max_depth:
        return []

    results = []

    if isinstance(obj, str):
        lower = obj.lower()
        # Look for quiz/flashcard-related strings
        if any(kw in lower for kw in ["question", "answer", "correct", "option", "front", "back"]):
            if len(obj) > 20:  # Skip short strings
                results.append((path, obj[:200]))
        # Look for JSON content
        if obj.startswith("{") or obj.startswith("["):
            try:
                parsed = json.loads(obj)
                if isinstance(parsed, (dict, list)) and len(str(parsed)) > 100:
                    results.append((path + " (JSON)", str(parsed)[:300]))
            except:
                pass
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            results.extend(find_content(item, depth + 1, f"{path}[{i}]"))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            results.extend(find_content(v, depth + 1, f"{path}.{k}"))

    return results


async def investigate_notebook_detail():
    """Fetch notebook detail and look for quiz/flashcard content."""

    notebook_id = "167481cd-23a3-4331-9a45-c8948900bf91"

    async with await NotebookLMClient.from_storage() as client:
        print("Fetching GET_NOTEBOOK...")

        params = [[notebook_id]]
        result = await client._core.rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        if result:
            print(f"Got notebook data, length: {len(str(result))} chars")

            # Save full response
            with open("investigation_output/notebook_detail.json", "w") as f:
                json.dump(result, f, indent=2, default=str)
            print("Saved to investigation_output/notebook_detail.json")

            # Search for quiz/flashcard content
            print("\nSearching for quiz/flashcard content...")
            findings = find_content(result)
            if findings:
                print(f"\nFound {len(findings)} potential matches:")
                for path, content in findings[:30]:
                    print(f"\n  {path}:")
                    print(f"    {content}")
            else:
                print("No quiz/flashcard keywords found in notebook data")
        else:
            print("No result from GET_NOTEBOOK")

        # Also try to fetch conversation history - maybe quiz state is stored there
        print("\n\nFetching GET_CONVERSATION_HISTORY...")
        try:
            params = [notebook_id]
            result = await client._core.rpc_call(
                RPCMethod.GET_CONVERSATION_HISTORY,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )
            if result:
                print(f"Got conversation history, length: {len(str(result))} chars")
                with open("investigation_output/conversation_history.json", "w") as f:
                    json.dump(result, f, indent=2, default=str)
                print("Saved to investigation_output/conversation_history.json")
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(investigate_notebook_detail())
