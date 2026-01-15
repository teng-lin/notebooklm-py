#!/usr/bin/env python3
"""RPC Health Check - Verify NotebookLM RPC method IDs are still valid.

This script makes minimal API calls to exercise RPC methods and verify
that the method IDs in rpc/types.py still match what the API returns.

Exit codes:
    0 - All RPC methods OK
    1 - One or more RPC methods have mismatched IDs

Environment variables:
    NOTEBOOKLM_AUTH_JSON - Playwright storage state JSON (required)
    NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID - Notebook ID for read operations
    NOTEBOOKLM_GENERATION_NOTEBOOK_ID - Notebook ID for write operations
    NOTEBOOKLM_RPC_DELAY - Delay between RPC calls in seconds (default: 1.0)

Usage:
    python scripts/check_rpc_health.py          # Quick mode (skip destructive)
    python scripts/check_rpc_health.py --full   # Full mode (create temp notebook)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import uuid4

import httpx

from notebooklm.auth import AuthTokens, fetch_tokens, load_auth_from_storage
from notebooklm.rpc import (
    BATCHEXECUTE_URL,
    RPCError,
    RPCMethod,
    build_request_body,
    encode_rpc_request,
)
from notebooklm.rpc.decoder import (
    collect_rpc_ids,
    decode_response,
    parse_chunked_response,
    strip_anti_xssi,
)


class CheckStatus(str, Enum):
    """Result status for an RPC check."""

    OK = "OK"
    MISMATCH = "MISMATCH"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


@dataclass
class CheckResult:
    """Result of checking a single RPC method."""

    method: RPCMethod
    status: CheckStatus
    expected_id: str
    found_ids: list[str]
    error: str | None = None


# Delay between RPC calls to avoid rate limiting (seconds)
# Can be overridden via NOTEBOOKLM_RPC_DELAY env var
CALL_DELAY = float(os.environ.get("NOTEBOOKLM_RPC_DELAY", "1.0"))

# Status display icons
STATUS_ICONS = {
    CheckStatus.OK: "OK",
    CheckStatus.MISMATCH: "MISMATCH",
    CheckStatus.ERROR: "ERROR",
    CheckStatus.SKIPPED: "SKIP",
}

# Methods that are duplicates (same ID, different name)
DUPLICATE_METHODS = {
    RPCMethod.GENERATE_MIND_MAP,  # Same as ACT_ON_SOURCES (yyryJe)
    RPCMethod.LIST_ARTIFACTS,  # Same as POLL_STUDIO (gArtLc)
}

# Methods that require real resource IDs (fail with placeholders)
# These return HTTP 400 with placeholder IDs but would work with real IDs
PLACEHOLDER_FAIL_METHODS = {
    RPCMethod.DISCOVER_SOURCES,  # Needs valid source discovery params
    RPCMethod.GET_ARTIFACT,  # Needs real artifact ID
    RPCMethod.LIST_ARTIFACTS_ALT,  # Needs specific notebook state
}

# Methods that can only be tested in full mode (with temp notebook)
# These are destructive or create resources
FULL_MODE_ONLY_METHODS = {
    # Create operations
    RPCMethod.CREATE_NOTEBOOK,
    RPCMethod.ADD_SOURCE,
    RPCMethod.CREATE_NOTE,
    # Delete operations (tested after creates)
    RPCMethod.DELETE_NOTE,
    RPCMethod.DELETE_SOURCE,
    RPCMethod.DELETE_NOTEBOOK,
}

# Methods always skipped (even in full mode)
ALWAYS_SKIP_METHODS = {
    # These require complex setup or have side effects we can't easily undo
    RPCMethod.CREATE_AUDIO,  # Takes too long, uses quota
    RPCMethod.CREATE_VIDEO,  # Takes too long, uses quota
    RPCMethod.CREATE_ARTIFACT,  # Takes too long, uses quota
    RPCMethod.DELETE_AUDIO,  # Need audio first
    RPCMethod.DELETE_STUDIO,  # Need studio first
    RPCMethod.ADD_SOURCE_FILE,  # Requires multipart upload
    RPCMethod.QUERY_ENDPOINT,  # Not a batchexecute RPC
    RPCMethod.START_FAST_RESEARCH,  # Takes too long
    RPCMethod.START_DEEP_RESEARCH,  # Takes too long
}


@dataclass
class TempResources:
    """Tracks temporarily created resources for cleanup."""

    notebook_id: str | None = None
    source_id: str | None = None
    note_id: str | None = None


def load_auth() -> dict[str, str]:
    """Load auth from environment or storage file.

    Uses the library's load_auth_from_storage() which handles:
    - NOTEBOOKLM_AUTH_JSON env var (for CI)
    - ~/.notebooklm/storage_state.json file (for local dev)
    - Proper cookie domain filtering
    """
    try:
        cookies = load_auth_from_storage()
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
    return cookies


async def make_rpc_call(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    method: RPCMethod,
    params: list[Any],
) -> tuple[list[str], str | None]:
    """Make an RPC call and return found IDs.

    Returns:
        Tuple of (list of RPC IDs found in response, error message or None)
    """
    # Build URL
    url = f"{BATCHEXECUTE_URL}?f.sid={auth.session_id}&source-path=%2F"

    # Encode request
    rpc_request = encode_rpc_request(method, params)
    body = build_request_body(rpc_request, auth.csrf_token)

    # Make request
    cookie_header = "; ".join(f"{k}={v}" for k, v in auth.cookies.items())
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookie_header,
    }

    try:
        response = await client.post(url, content=body, headers=headers)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return [], f"HTTP {e.response.status_code}"
    except httpx.RequestError as e:
        return [], str(e)

    # Parse response and extract IDs
    try:
        cleaned = strip_anti_xssi(response.text)
        chunks = parse_chunked_response(cleaned)
        found_ids = collect_rpc_ids(chunks)
        return found_ids, None
    except (json.JSONDecodeError, ValueError, IndexError, TypeError) as e:
        return [], f"Parse error: {e}"


async def make_rpc_call_with_data(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    method: RPCMethod,
    params: list[Any],
) -> tuple[Any, str | None]:
    """Make an RPC call and return decoded response data.

    Returns:
        Tuple of (decoded response data or None, error message or None)
    """
    url = f"{BATCHEXECUTE_URL}?f.sid={auth.session_id}&source-path=%2F"
    rpc_request = encode_rpc_request(method, params)
    body = build_request_body(rpc_request, auth.csrf_token)

    cookie_header = "; ".join(f"{k}={v}" for k, v in auth.cookies.items())
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookie_header,
    }

    try:
        response = await client.post(url, content=body, headers=headers)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}"
    except httpx.RequestError as e:
        return None, str(e)

    try:
        data = decode_response(response.text, method.value)
        return data, None
    except (json.JSONDecodeError, ValueError, IndexError, TypeError, RPCError) as e:
        return None, f"Parse error: {e}"


async def create_temp_notebook(
    client: httpx.AsyncClient, auth: AuthTokens
) -> tuple[str | None, str | None]:
    """Create a temporary notebook for testing.

    Returns:
        Tuple of (notebook_id or None, error message or None)
    """
    name = f"RPC-Health-Check-{uuid4().hex[:8]}"
    data, error = await make_rpc_call_with_data(client, auth, RPCMethod.CREATE_NOTEBOOK, [name])
    if error:
        return None, error
    try:
        # Response structure: [notebook_id, name, ...]
        notebook_id = data[0]
        return notebook_id, None
    except (IndexError, TypeError) as e:
        return None, f"Failed to extract notebook ID: {e}"


async def add_temp_source(
    client: httpx.AsyncClient, auth: AuthTokens, notebook_id: str
) -> tuple[str | None, str | None]:
    """Add a text source to the temp notebook.

    Returns:
        Tuple of (source_id or None, error message or None)
    """
    # ADD_SOURCE params: [notebook_id, title, content, type=0 (text)]
    params = [notebook_id, "Test Source", "Test content for RPC health check.", 0]
    data, error = await make_rpc_call_with_data(client, auth, RPCMethod.ADD_SOURCE, params)
    if error:
        return None, error
    try:
        # Response structure: [[source_id, ...], ...]
        source_id = data[0][0]
        return source_id, None
    except (IndexError, TypeError) as e:
        return None, f"Failed to extract source ID: {e}"


async def create_temp_note(
    client: httpx.AsyncClient, auth: AuthTokens, notebook_id: str
) -> tuple[str | None, str | None]:
    """Create a note in the temp notebook.

    Returns:
        Tuple of (note_id or None, error message or None)
    """
    # CREATE_NOTE params: [[notebook_id], title, content]
    params = [[notebook_id], "Test Note", "Test note content"]
    data, error = await make_rpc_call_with_data(client, auth, RPCMethod.CREATE_NOTE, params)
    if error:
        return None, error
    try:
        # Response structure: [note_id, ...]
        note_id = data[0]
        return note_id, None
    except (IndexError, TypeError) as e:
        return None, f"Failed to extract note ID: {e}"


async def delete_temp_notebook(
    client: httpx.AsyncClient, auth: AuthTokens, notebook_id: str
) -> str | None:
    """Delete the temp notebook (cleanup).

    Returns:
        Error message or None if successful
    """
    _, error = await make_rpc_call(client, auth, RPCMethod.DELETE_NOTEBOOK, [notebook_id])
    return error


def get_test_params(method: RPCMethod, notebook_id: str | None) -> list[Any] | None:
    """Get test parameters for an RPC method.

    Returns None if method cannot be tested with simple params.
    """
    # Methods that work without a notebook
    if method == RPCMethod.LIST_NOTEBOOKS:
        return []

    # Methods that require a notebook ID
    if not notebook_id:
        return None

    # Methods that take [notebook_id] as the only param
    if method in (
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.GET_SOURCE_GUIDE,
        RPCMethod.GET_SUGGESTED_REPORTS,
        RPCMethod.GET_SHARE_STATUS,
        RPCMethod.REMOVE_RECENTLY_VIEWED,
    ):
        return [notebook_id]

    # Methods that take [[notebook_id]] as the only param
    if method in (
        RPCMethod.LIST_ARTIFACTS,
        RPCMethod.LIST_ARTIFACTS_ALT,
        RPCMethod.POLL_STUDIO,
        RPCMethod.GET_CONVERSATION_HISTORY,
        RPCMethod.GET_NOTES_AND_MIND_MAPS,
        RPCMethod.DISCOVER_SOURCES,
        RPCMethod.GET_AUDIO,
    ):
        return [[notebook_id]]

    # Notebook operations (read-only - rename to same name is a no-op)
    if method == RPCMethod.RENAME_NOTEBOOK:
        return [notebook_id, "RPC Health Check Test", None, None, None]

    # Source operations (read-only - use placeholder IDs)
    if method == RPCMethod.GET_SOURCE:
        return [[notebook_id], ["placeholder_source_id"]]

    if method in (RPCMethod.REFRESH_SOURCE, RPCMethod.CHECK_SOURCE_FRESHNESS):
        return [[notebook_id], [["placeholder"]]]

    if method == RPCMethod.UPDATE_SOURCE:
        return [[notebook_id], "placeholder", "New Title"]

    # Summary operations (read-only)
    if method == RPCMethod.SUMMARIZE:
        return [[notebook_id], [], "Summarize the content"]

    # Artifact operations (read-only - use placeholder IDs)
    if method in (RPCMethod.GET_ARTIFACT, RPCMethod.GET_INTERACTIVE_HTML):
        return [[notebook_id], "placeholder"]

    if method == RPCMethod.RENAME_ARTIFACT:
        return [[notebook_id], "placeholder", "New Name"]

    if method == RPCMethod.EXPORT_ARTIFACT:
        return [[notebook_id], "placeholder", 1]

    # Research operations (read-only - poll/import only)
    if method == RPCMethod.POLL_RESEARCH:
        return [[notebook_id], "placeholder_task_id"]

    if method == RPCMethod.IMPORT_RESEARCH:
        return [[notebook_id], "placeholder_research_id"]

    # Note operations (read-only - update only)
    if method == RPCMethod.UPDATE_NOTE:
        return [[notebook_id], "placeholder", "Updated", "Updated content"]

    # Mind map operation (read-only)
    if method == RPCMethod.ACT_ON_SOURCES:
        return [[notebook_id], [], 5]  # Mind map type

    # Sharing operations (read-only checks)
    if method == RPCMethod.SHARE_ARTIFACT:
        return [[notebook_id], "placeholder", True]

    if method == RPCMethod.SHARE_NOTEBOOK:
        return [notebook_id, 1]  # Restricted

    return None


async def check_method(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    method: RPCMethod,
    notebook_id: str | None,
    full_mode: bool = False,
    temp_resources: TempResources | None = None,
) -> CheckResult:
    """Check a single RPC method."""
    expected_id = method.value

    # Always skip certain methods
    if method in ALWAYS_SKIP_METHODS:
        return CheckResult(
            method=method,
            status=CheckStatus.SKIPPED,
            expected_id=expected_id,
            found_ids=[],
            error="Method always skipped (complex setup or quota)",
        )

    if method in DUPLICATE_METHODS:
        return CheckResult(
            method=method,
            status=CheckStatus.SKIPPED,
            expected_id=expected_id,
            found_ids=[],
            error="Duplicate method (same ID as another)",
        )

    if method in PLACEHOLDER_FAIL_METHODS:
        return CheckResult(
            method=method,
            status=CheckStatus.SKIPPED,
            expected_id=expected_id,
            found_ids=[],
            error="Requires real resource IDs (placeholder fails)",
        )

    # Skip full-mode-only methods - they're handled in setup/cleanup phases
    if method in FULL_MODE_ONLY_METHODS:
        skip_reason = (
            "Tested in setup/cleanup phases"
            if full_mode
            else "Requires --full mode (creates/deletes resources)"
        )
        return CheckResult(
            method=method,
            status=CheckStatus.SKIPPED,
            expected_id=expected_id,
            found_ids=[],
            error=skip_reason,
        )

    # Get test params
    params = get_test_params(method, notebook_id)
    if params is None:
        return CheckResult(
            method=method,
            status=CheckStatus.SKIPPED,
            expected_id=expected_id,
            found_ids=[],
            error="No test parameters available",
        )

    # Make the call
    found_ids, error = await make_rpc_call(client, auth, method, params)

    if error:
        # Check if error response still contains our expected ID
        if expected_id in found_ids:
            return CheckResult(
                method=method,
                status=CheckStatus.OK,
                expected_id=expected_id,
                found_ids=found_ids,
                error=f"Call failed but ID found: {error}",
            )
        return CheckResult(
            method=method,
            status=CheckStatus.ERROR,
            expected_id=expected_id,
            found_ids=found_ids,
            error=error,
        )

    # Check if expected ID is in response
    status = CheckStatus.OK if expected_id in found_ids else CheckStatus.MISMATCH
    error_msg = None if status == CheckStatus.OK else f"Expected '{expected_id}' not in response"
    return CheckResult(
        method=method,
        status=status,
        expected_id=expected_id,
        found_ids=found_ids,
        error=error_msg,
    )


async def run_health_check(full_mode: bool = False) -> list[CheckResult]:
    """Run health check on all RPC methods."""
    # Load cookies from env/storage using library's domain-filtered loader
    cookies = load_auth()

    # Get notebook IDs
    read_only_id = os.environ.get("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID")
    generation_id = os.environ.get("NOTEBOOKLM_GENERATION_NOTEBOOK_ID")
    notebook_id = read_only_id or generation_id

    if not notebook_id and not full_mode:
        print("WARNING: No notebook ID provided. Some methods will be skipped.", file=sys.stderr)

    results: list[CheckResult] = []
    temp_resources = TempResources()

    # Fetch auth tokens using library's token fetcher
    print("Fetching auth tokens...")
    try:
        csrf_token, session_id = await fetch_tokens(cookies)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPError as e:
        print(f"ERROR: Network error while fetching auth tokens: {e}", file=sys.stderr)
        sys.exit(1)
    auth = AuthTokens(cookies=cookies, csrf_token=csrf_token, session_id=session_id)
    print(f"Auth OK (CSRF token length: {len(auth.csrf_token)})")
    print()

    async with httpx.AsyncClient(timeout=30.0) as client:
        # In full mode, create temp notebook first and track as test results
        if full_mode:
            print("Creating temp resources for full testing...")

            # Test CREATE_NOTEBOOK
            found_ids, error = await make_rpc_call(
                client, auth, RPCMethod.CREATE_NOTEBOOK, [f"RPC-Health-Check-{uuid4().hex[:8]}"]
            )
            if RPCMethod.CREATE_NOTEBOOK.value in found_ids:
                print("OK       CREATE_NOTEBOOK - temp notebook created")
                results.append(
                    CheckResult(
                        method=RPCMethod.CREATE_NOTEBOOK,
                        status=CheckStatus.OK,
                        expected_id=RPCMethod.CREATE_NOTEBOOK.value,
                        found_ids=found_ids,
                    )
                )
                # Extract notebook ID from response (it's typically the first string in found data)
                # For now, list notebooks and find the one we just created
                await asyncio.sleep(CALL_DELAY)
                list_ids, _ = await make_rpc_call(client, auth, RPCMethod.LIST_NOTEBOOKS, [])
                # Get fresh notebook list to find our temp notebook
                try:
                    data = decode_response(
                        (
                            await client.post(
                                f"{BATCHEXECUTE_URL}?f.sid={auth.session_id}&source-path=%2F",
                                content=build_request_body(
                                    encode_rpc_request(RPCMethod.LIST_NOTEBOOKS, []),
                                    auth.csrf_token,
                                ),
                                headers={
                                    "Content-Type": "application/x-www-form-urlencoded",
                                    "Cookie": "; ".join(
                                        f"{k}={v}" for k, v in auth.cookies.items()
                                    ),
                                },
                            )
                        ).text,
                        RPCMethod.LIST_NOTEBOOKS.value,
                    )
                    # Find notebook starting with RPC-Health-Check-
                    for nb in data or []:
                        if isinstance(nb, list) and len(nb) > 1:
                            if isinstance(nb[1], str) and nb[1].startswith("RPC-Health-Check-"):
                                temp_resources.notebook_id = nb[0]
                                break
                except Exception:
                    pass
            else:
                print(f"ERROR    CREATE_NOTEBOOK - {error or 'ID not in response'}")
                results.append(
                    CheckResult(
                        method=RPCMethod.CREATE_NOTEBOOK,
                        status=CheckStatus.ERROR,
                        expected_id=RPCMethod.CREATE_NOTEBOOK.value,
                        found_ids=found_ids,
                        error=error or "RPC ID not found in response",
                    )
                )

            if temp_resources.notebook_id:
                notebook_id = temp_resources.notebook_id

                # Test ADD_SOURCE
                await asyncio.sleep(CALL_DELAY)
                found_ids, error = await make_rpc_call(
                    client,
                    auth,
                    RPCMethod.ADD_SOURCE,
                    [notebook_id, "Test Source", "Test content for RPC health check.", 0],
                )
                if RPCMethod.ADD_SOURCE.value in found_ids:
                    print("OK       ADD_SOURCE - temp source added")
                    results.append(
                        CheckResult(
                            method=RPCMethod.ADD_SOURCE,
                            status=CheckStatus.OK,
                            expected_id=RPCMethod.ADD_SOURCE.value,
                            found_ids=found_ids,
                        )
                    )
                    # Extract source_id for DELETE_SOURCE test
                    try:
                        data, _ = await make_rpc_call_with_data(
                            client, auth, RPCMethod.GET_NOTEBOOK, [notebook_id]
                        )
                        # Sources are in data[3] as list of [source_id, ...]
                        if data and len(data) > 3 and data[3]:
                            temp_resources.source_id = data[3][0][0]
                    except Exception:
                        pass
                else:
                    print(f"ERROR    ADD_SOURCE - {error or 'ID not in response'}")
                    results.append(
                        CheckResult(
                            method=RPCMethod.ADD_SOURCE,
                            status=CheckStatus.ERROR,
                            expected_id=RPCMethod.ADD_SOURCE.value,
                            found_ids=found_ids,
                            error=error or "RPC ID not found in response",
                        )
                    )

                # Test CREATE_NOTE
                await asyncio.sleep(CALL_DELAY)
                found_ids, error = await make_rpc_call(
                    client,
                    auth,
                    RPCMethod.CREATE_NOTE,
                    [[notebook_id], "Test Note", "Test note content"],
                )
                if RPCMethod.CREATE_NOTE.value in found_ids:
                    print("OK       CREATE_NOTE - temp note created")
                    results.append(
                        CheckResult(
                            method=RPCMethod.CREATE_NOTE,
                            status=CheckStatus.OK,
                            expected_id=RPCMethod.CREATE_NOTE.value,
                            found_ids=found_ids,
                        )
                    )
                    # Extract note_id for DELETE_NOTE test
                    try:
                        data, _ = await make_rpc_call_with_data(
                            client, auth, RPCMethod.GET_NOTES_AND_MIND_MAPS, [[notebook_id]]
                        )
                        # Notes are returned as list of [note_id, ...]
                        if data and len(data) > 0 and data[0]:
                            temp_resources.note_id = data[0][0][0]
                    except Exception:
                        pass
                else:
                    print(f"ERROR    CREATE_NOTE - {error or 'ID not in response'}")
                    results.append(
                        CheckResult(
                            method=RPCMethod.CREATE_NOTE,
                            status=CheckStatus.ERROR,
                            expected_id=RPCMethod.CREATE_NOTE.value,
                            found_ids=found_ids,
                            error=error or "RPC ID not found in response",
                        )
                    )

            print()

        try:
            # Get all RPC methods
            methods = list(RPCMethod)
            total = len(methods)

            print(f"Checking {total} RPC methods...")
            print("=" * 60)

            for i, method in enumerate(methods, 1):
                result = await check_method(
                    client, auth, method, notebook_id, full_mode, temp_resources
                )
                results.append(result)

                # Print result
                status_icon = STATUS_ICONS[result.status]
                line = f"{status_icon:8} {method.name} ({result.expected_id})"
                if result.error and result.status != CheckStatus.OK:
                    line += f" - {result.error}"
                print(line)

                # Delay between calls
                if i < total and result.status not in (CheckStatus.SKIPPED,):
                    await asyncio.sleep(CALL_DELAY)

        finally:
            # Cleanup temp notebook in full mode - test DELETE operations
            if full_mode and temp_resources.notebook_id:
                print()
                print("Testing DELETE operations during cleanup...")

                # Test DELETE_NOTE if we have a note
                if temp_resources.note_id:
                    await asyncio.sleep(CALL_DELAY)
                    found_ids, error = await make_rpc_call(
                        client,
                        auth,
                        RPCMethod.DELETE_NOTE,
                        [[temp_resources.notebook_id], temp_resources.note_id],
                    )
                    if RPCMethod.DELETE_NOTE.value in found_ids:
                        print("OK       DELETE_NOTE - temp note deleted")
                        results.append(
                            CheckResult(
                                method=RPCMethod.DELETE_NOTE,
                                status=CheckStatus.OK,
                                expected_id=RPCMethod.DELETE_NOTE.value,
                                found_ids=found_ids,
                            )
                        )
                    else:
                        print(f"ERROR    DELETE_NOTE - {error or 'ID not in response'}")
                        results.append(
                            CheckResult(
                                method=RPCMethod.DELETE_NOTE,
                                status=CheckStatus.ERROR,
                                expected_id=RPCMethod.DELETE_NOTE.value,
                                found_ids=found_ids,
                                error=error or "RPC ID not found in response",
                            )
                        )

                # Test DELETE_SOURCE if we have a source
                if temp_resources.source_id:
                    await asyncio.sleep(CALL_DELAY)
                    found_ids, error = await make_rpc_call(
                        client,
                        auth,
                        RPCMethod.DELETE_SOURCE,
                        [[temp_resources.notebook_id], [[temp_resources.source_id]]],
                    )
                    if RPCMethod.DELETE_SOURCE.value in found_ids:
                        print("OK       DELETE_SOURCE - temp source deleted")
                        results.append(
                            CheckResult(
                                method=RPCMethod.DELETE_SOURCE,
                                status=CheckStatus.OK,
                                expected_id=RPCMethod.DELETE_SOURCE.value,
                                found_ids=found_ids,
                            )
                        )
                    else:
                        print(f"ERROR    DELETE_SOURCE - {error or 'ID not in response'}")
                        results.append(
                            CheckResult(
                                method=RPCMethod.DELETE_SOURCE,
                                status=CheckStatus.ERROR,
                                expected_id=RPCMethod.DELETE_SOURCE.value,
                                found_ids=found_ids,
                                error=error or "RPC ID not found in response",
                            )
                        )

                # Test DELETE_NOTEBOOK (always runs to cleanup)
                await asyncio.sleep(CALL_DELAY)
                found_ids, error = await make_rpc_call(
                    client, auth, RPCMethod.DELETE_NOTEBOOK, [temp_resources.notebook_id]
                )
                if RPCMethod.DELETE_NOTEBOOK.value in found_ids:
                    print("OK       DELETE_NOTEBOOK - temp notebook deleted")
                    results.append(
                        CheckResult(
                            method=RPCMethod.DELETE_NOTEBOOK,
                            status=CheckStatus.OK,
                            expected_id=RPCMethod.DELETE_NOTEBOOK.value,
                            found_ids=found_ids,
                        )
                    )
                else:
                    print(f"ERROR    DELETE_NOTEBOOK - {error or 'ID not in response'}")
                    results.append(
                        CheckResult(
                            method=RPCMethod.DELETE_NOTEBOOK,
                            status=CheckStatus.ERROR,
                            expected_id=RPCMethod.DELETE_NOTEBOOK.value,
                            found_ids=found_ids,
                            error=error or "RPC ID not found in response",
                        )
                    )

    return results


def print_summary(results: list[CheckResult]) -> int:
    """Print summary and return exit code."""
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    counts = Counter(r.status for r in results)
    total = len(results)

    print(f"OK:       {counts[CheckStatus.OK]}/{total}")
    print(f"MISMATCH: {counts[CheckStatus.MISMATCH]}/{total}")
    print(f"ERROR:    {counts[CheckStatus.ERROR]}/{total}")
    print(f"SKIPPED:  {counts[CheckStatus.SKIPPED]}/{total}")

    # Print details for mismatches
    mismatches = [r for r in results if r.status == CheckStatus.MISMATCH]
    if mismatches:
        print()
        print("MISMATCH DETAILS:")
        print("-" * 40)
        for r in mismatches:
            print(f"  {r.method.name}:")
            print(f"    Expected: '{r.expected_id}'")
            print(f"    Found:    {r.found_ids}")
            print(f"    Action:   Update RPCMethod.{r.method.name} in src/notebooklm/rpc/types.py")
            print()

    # Print details for errors
    errors = [r for r in results if r.status == CheckStatus.ERROR]
    if errors:
        print()
        print("ERROR DETAILS:")
        print("-" * 40)
        for r in errors:
            print(f"  {r.method.name} ({r.expected_id}): {r.error}")
        print()

    # Return exit code
    # Only fail on MISMATCH (RPC ID changed) - this is what we care about
    # ERROR could be transient (rate limiting, network issues) - don't fail on these
    if counts[CheckStatus.MISMATCH] > 0:
        print("RESULT: FAIL - RPC ID mismatches detected")
        return 1
    if counts[CheckStatus.ERROR] > 0:
        print("RESULT: WARN - Some methods had errors (may be transient)")
        print("       Review ERROR DETAILS above for potential issues")
        return 0  # Don't fail - could be rate limiting or network issues
    print("RESULT: PASS - All tested RPC methods OK")
    return 0


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="RPC Health Check - Verify NotebookLM RPC method IDs"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Full mode: create temp notebook to test create/delete operations",
    )
    args = parser.parse_args()

    mode_str = "FULL" if args.full else "QUICK"
    print(f"RPC Health Check ({mode_str} mode)")
    print("=" * 60)
    print()

    results = asyncio.run(run_health_check(full_mode=args.full))
    return print_summary(results)


if __name__ == "__main__":
    sys.exit(main())
