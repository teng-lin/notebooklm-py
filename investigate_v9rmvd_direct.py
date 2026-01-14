#!/usr/bin/env python3
"""Direct HTTP call to test v9rmvd RPC."""

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlencode, quote

sys.path.insert(0, str(Path(__file__).parent / "src"))

import httpx
from notebooklm.auth import AuthTokens
from notebooklm.rpc.decoder import decode_response

BATCHEXECUTE_URL = "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"


def encode_rpc_manual(rpc_id: str, params: list) -> str:
    """Manually encode RPC request."""
    params_json = json.dumps(params)
    inner = [rpc_id, params_json, None, "generic"]
    outer = [[inner]]
    encoded = json.dumps(outer, separators=(",", ":"))
    return f"f.req={quote(encoded)}"


async def test_v9rmvd():
    """Make direct HTTP call to v9rmvd RPC."""

    notebook_id = "167481cd-23a3-4331-9a45-c8948900bf91"
    quiz_id = "a0e4dca6-3bb0-4ed0-aea1-dfa91cf89767"
    flashcard_id = "173255d8-12b3-4c67-b925-a76ce6c71735"

    # Load auth
    auth = await AuthTokens.from_storage()

    # Build URL
    url_params = {
        "rpcids": "v9rmvd",
        "source-path": f"/notebook/{notebook_id}",
        "f.sid": auth.session_id,
        "hl": "en",
        "_reqid": "123456",
        "rt": "c",
    }
    url = f"{BATCHEXECUTE_URL}?{urlencode(url_params)}"

    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Same-Domain": "1",
        "Referer": "https://notebooklm.google.com/",
    }

    cookies = httpx.Cookies()
    for name, value in auth.cookies.items():
        cookies.set(name, value, domain="notebooklm.google.com")

    async with httpx.AsyncClient(cookies=cookies, follow_redirects=True) as client:
        print("="*60)
        print("Testing QUIZ content...")
        print(f"Quiz ID: {quiz_id}")

        # Build request body - params is [artifact_id]
        params = [quiz_id]
        body = encode_rpc_manual("v9rmvd", params)
        body += f"&at={auth.csrf_token}"

        print(f"URL: {url}")
        print(f"Body: {body[:200]}...")

        response = await client.post(url, content=body, headers=headers)
        print(f"Status: {response.status_code}")

        if response.status_code == 200:
            try:
                result = decode_response(response.text, "v9rmvd", allow_null=True)
                print(f"\n✓ SUCCESS!")
                print(f"Result type: {type(result)}")

                # Save full result
                with open("investigation_output/quiz_content_v9rmvd.json", "w") as f:
                    json.dump(result, f, indent=2, default=str, ensure_ascii=False)
                print("Saved to: investigation_output/quiz_content_v9rmvd.json")

                # Pretty print preview
                print(f"\nPreview:\n{json.dumps(result, indent=2, ensure_ascii=False)[:3000]}")

            except Exception as e:
                print(f"Decode error: {e}")
                print(f"Raw response: {response.text[:2000]}")
        else:
            print(f"Error: {response.text[:500]}")

        # Test flashcard
        print("\n" + "="*60)
        print("Testing FLASHCARD content...")
        print(f"Flashcard ID: {flashcard_id}")

        params = [flashcard_id]
        body = encode_rpc_manual("v9rmvd", params)
        body += f"&at={auth.csrf_token}"

        response = await client.post(url, content=body, headers=headers)
        print(f"Status: {response.status_code}")

        if response.status_code == 200:
            try:
                result = decode_response(response.text, "v9rmvd", allow_null=True)
                print(f"\n✓ SUCCESS!")

                with open("investigation_output/flashcard_content_v9rmvd.json", "w") as f:
                    json.dump(result, f, indent=2, default=str, ensure_ascii=False)
                print("Saved to: investigation_output/flashcard_content_v9rmvd.json")

                print(f"\nPreview:\n{json.dumps(result, indent=2, ensure_ascii=False)[:3000]}")

            except Exception as e:
                print(f"Decode error: {e}")
                print(f"Raw response: {response.text[:2000]}")


if __name__ == "__main__":
    asyncio.run(test_v9rmvd())
