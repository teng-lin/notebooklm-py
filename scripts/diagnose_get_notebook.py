#!/usr/bin/env python3
"""Diagnose GET_NOTEBOOK failures (Issue #114).

Compares LIST_NOTEBOOKS and GET_NOTEBOOK raw responses to help diagnose
why GET_NOTEBOOK returns "No result found" while LIST_NOTEBOOKS works.

This script makes the exact same HTTP requests as the main client and
prints the raw responses, parsed chunks, and found RPC IDs.

Usage:
    python scripts/diagnose_get_notebook.py <notebook_id>
    python scripts/diagnose_get_notebook.py              # uses first notebook from list

Environment variables:
    NOTEBOOKLM_AUTH_JSON - Playwright storage state JSON (for CI)
    Or run 'notebooklm login' first for local auth.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from urllib.parse import urlencode

import httpx

from notebooklm._auth.cookies import _load_storage_state
from notebooklm._auth.refresh import _fetch_tokens_with_jar
from notebooklm.auth import AuthTokens, build_cookie_jar, extract_cookies_with_domains
from notebooklm.rpc import (
    RPCMethod,
    build_request_body,
    encode_rpc_request,
    get_batchexecute_url,
)
from notebooklm.rpc.decoder import (
    collect_rpc_ids,
    decode_response,
    parse_chunked_response,
    strip_anti_xssi,
)


def load_auth() -> dict[tuple[str, str, str], str]:
    """Load auth cookies from storage with their domains intact.

    Deliberately **not** ``load_auth_from_storage()``: that returns a flat
    ``name -> value`` map, and ``AuthTokens`` then widens every entry to
    ``.google.com`` -- so the jar would broadcast the accounts-scoped ``LSID``
    and an arbitrary ``OSID`` to the app host, which is the defect this script
    exists to help diagnose (#2054).

    Also not ``build_httpx_cookies_from_storage()``: that fires PSIDTS
    recovery (a network POST and a disk write) exactly when auth is
    incomplete -- i.e. it would heal the state a diagnostic is trying to
    observe.
    """
    try:
        return extract_cookies_with_domains(_load_storage_state(None))
    except FileNotFoundError:
        print(
            "ERROR: No authentication found.\n"
            "Set NOTEBOOKLM_AUTH_JSON env var or run 'notebooklm login'",
            file=sys.stderr,
        )
        sys.exit(1)
    except ValueError as e:
        print(f"ERROR: Invalid authentication: {e}", file=sys.stderr)
        sys.exit(1)


async def make_rpc_request(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    method: RPCMethod,
    params: list,
    source_path: str = "/",
) -> str:
    """Make an RPC request and return raw response text."""
    query = urlencode(
        {
            "rpcids": method.value,
            "source-path": source_path,
            "f.sid": auth.session_id,
            "rt": "c",
        }
    )
    url = f"{get_batchexecute_url()}?{query}"

    rpc_request = encode_rpc_request(method, params)
    body = build_request_body(rpc_request, auth.csrf_token)

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    response = await client.post(url, content=body, headers=headers)
    response.raise_for_status()
    return response.text


def diagnose_response(label: str, raw: str, rpc_id: str) -> tuple[list, list, Any]:
    """Print diagnostic info about a raw RPC response.

    Returns:
        Tuple of (chunks, found_ids, decoded_result_or_None).
    """
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(f"  Raw response length: {len(raw)} bytes")
    print(f"  First 200 chars: {raw[:200]!r}")

    cleaned = strip_anti_xssi(raw)
    chunks = parse_chunked_response(cleaned)
    found_ids = collect_rpc_ids(chunks)

    print(f"  Chunks parsed: {len(chunks)}")
    print(f"  Found RPC IDs: {found_ids}")
    print(f"  Target RPC ID: {rpc_id}")
    print(f"  Target in found_ids: {rpc_id in found_ids}")

    # Show each chunk's structure
    for i, chunk in enumerate(chunks):
        if isinstance(chunk, list) and len(chunk) >= 2:
            tag = chunk[0] if isinstance(chunk[0], str) else "?"
            cid = chunk[1] if len(chunk) > 1 else "?"
            has_data = chunk[2] is not None if len(chunk) > 2 else False
            data_type = type(chunk[2]).__name__ if len(chunk) > 2 else "N/A"
            print(f"  Chunk {i}: tag={tag}, id={cid}, has_data={has_data}, data_type={data_type}")
        else:
            preview = repr(chunk)[:100]
            print(f"  Chunk {i}: {preview}")

    # Try full decode_response (uses the same parsing internally)
    decoded_result = None
    try:
        decoded_result = decode_response(raw, rpc_id)
        preview = repr(decoded_result)[:200]
        print(f"  decode_response: OK - {preview}")
    except Exception as e:
        print(f"  decode_response: raised {type(e).__name__}: {e}")

    return chunks, found_ids, decoded_result


async def run_diagnosis(notebook_id: str | None = None) -> None:
    """Run the diagnostic comparison."""
    cookies = load_auth()

    print("Fetching auth tokens...")
    # Fetch through a jar we keep, rather than `fetch_tokens(cookies)`. That
    # helper builds a jar internally and copies it back only when
    # NOTEBOOKLM_REFRESH_CMD ran, so on the ordinary path a Set-Cookie from the
    # sign-in redirect -- a rotated host-scoped OSID, say -- is discarded, and
    # the RPCs below would then be rejected despite token extraction having
    # succeeded. `poke=False` keeps this read-only: no RotateCookies POST.
    jar = build_cookie_jar(cookies=cookies)
    try:
        csrf_token, session_id = await _fetch_tokens_with_jar(jar, None, poke=False)
    except (ValueError, httpx.HTTPError) as e:
        print(f"ERROR: Failed to fetch auth tokens: {e}")
        print("Try running: notebooklm login")
        sys.exit(1)
    auth = AuthTokens(cookies=cookies, csrf_token=csrf_token, session_id=session_id, cookie_jar=jar)
    print(f"Auth OK (CSRF length: {len(auth.csrf_token)})")

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, read=60.0), cookies=auth.cookie_jar
    ) as client:
        # 1. LIST_NOTEBOOKS
        print("\n--- LIST_NOTEBOOKS ---")
        list_raw = await make_rpc_request(client, auth, RPCMethod.LIST_NOTEBOOKS, [])
        list_chunks, list_ids, list_result = diagnose_response(
            "LIST_NOTEBOOKS", list_raw, RPCMethod.LIST_NOTEBOOKS.value
        )

        # Extract notebook ID if not provided
        if not notebook_id and list_result:
            try:
                if isinstance(list_result, list) and len(list_result) > 0:
                    first_nb = list_result[0]
                    if isinstance(first_nb, list):
                        # Nested: [[notebook_data, ...], ...]
                        if isinstance(first_nb[0], list):
                            notebook_id = first_nb[0][2] if len(first_nb[0]) > 2 else None
                        else:
                            notebook_id = first_nb[2] if len(first_nb) > 2 else None
            except (IndexError, TypeError) as e:
                print(f"  Warning: Could not extract notebook ID from LIST_NOTEBOOKS result: {e}")

            if not notebook_id:
                print("\nERROR: Could not extract notebook ID from LIST_NOTEBOOKS.")
                print("Please provide a notebook ID as argument:")
                print(f"  python {sys.argv[0]} <notebook_id>")
                sys.exit(1)

            print(f"\nUsing first notebook: {notebook_id}")

        # 2. GET_NOTEBOOK
        print("\n--- GET_NOTEBOOK ---")
        get_raw = await make_rpc_request(
            client,
            auth,
            RPCMethod.GET_NOTEBOOK,
            [notebook_id, None, [2], None, 0],
            source_path=f"/notebook/{notebook_id}",
        )
        get_chunks, get_ids, _ = diagnose_response(
            "GET_NOTEBOOK", get_raw, RPCMethod.GET_NOTEBOOK.value
        )

        # 3. Side-by-side comparison
        print(f"\n{'=' * 60}")
        print("  COMPARISON")
        print(f"{'=' * 60}")
        print(
            f"  LIST_NOTEBOOKS: {len(list_raw)} bytes, {len(list_chunks)} chunks, IDs: {list_ids}"
        )
        print(f"  GET_NOTEBOOK:   {len(get_raw)} bytes, {len(get_chunks)} chunks, IDs: {get_ids}")

        list_ok = RPCMethod.LIST_NOTEBOOKS.value in list_ids
        get_ok = RPCMethod.GET_NOTEBOOK.value in get_ids
        print(f"\n  LIST_NOTEBOOKS working: {list_ok}")
        print(f"  GET_NOTEBOOK working:   {get_ok}")

        if list_ok and not get_ok:
            print("\n  DIAGNOSIS: LIST_NOTEBOOKS works but GET_NOTEBOOK does not.")
            print("  This matches Issue #114. The GET_NOTEBOOK RPC may be:")
            print("    - Returning null result data (server-side issue)")
            print("    - Requiring different parameters than expected")
            print("    - Affected by a server-side change not yet reflected in method IDs")
        elif list_ok and get_ok:
            print("\n  DIAGNOSIS: Both RPCs are working. Issue may be intermittent.")
        else:
            print("\n  DIAGNOSIS: Both RPCs are failing. Check authentication.")


def main() -> None:
    """Main entry point."""
    notebook_id = sys.argv[1] if len(sys.argv) > 1 else None

    print("GET_NOTEBOOK Diagnostic Tool (Issue #114)")
    print("=" * 60)

    asyncio.run(run_diagnosis(notebook_id))


if __name__ == "__main__":
    main()
