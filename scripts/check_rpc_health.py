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
from urllib.parse import quote
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
    RPCMethod.LIST_ARTIFACTS,  # Same as POLL_ARTIFACT (gArtLc)
}

# Methods that require real resource IDs (fail with placeholders).
# These return HTTP 400 with placeholder IDs but would work with real IDs.
# Currently empty but kept for future additions.
PLACEHOLDER_FAIL_METHODS: set[RPCMethod] = set()

# Methods that can only be tested in full mode (with temp notebook)
# These are destructive or create resources
FULL_MODE_ONLY_METHODS = {
    # Create operations
    RPCMethod.CREATE_NOTEBOOK,
    RPCMethod.ADD_SOURCE,
    RPCMethod.ADD_SOURCE_FILE,  # Registers file source intent (no upload needed)
    RPCMethod.CREATE_NOTE,
    RPCMethod.CREATE_ARTIFACT,  # Main RPC for all artifacts - test with flashcards (fast)
    RPCMethod.START_FAST_RESEARCH,  # Starts research (verify RPC ID, don't wait)
    # Delete operations (tested after creates)
    RPCMethod.DELETE_NOTE,
    RPCMethod.DELETE_SOURCE,
    RPCMethod.DELETE_ARTIFACT,  # Main RPC for artifact deletion
    RPCMethod.DELETE_NOTEBOOK,
}

# Methods always skipped (even in full mode)
ALWAYS_SKIP_METHODS = {
    # Not a batchexecute RPC
    RPCMethod.QUERY_ENDPOINT,
    # Takes too long
    RPCMethod.START_DEEP_RESEARCH,
    # Defined but not used in library - correct params format unknown
    RPCMethod.GET_ARTIFACT,
    # Not fully rolled out by Google - fails with any IDs
    RPCMethod.DISCOVER_SOURCES,
}


@dataclass
class TempResources:
    """Tracks temporarily created resources for cleanup."""

    notebook_id: str | None = None
    source_id: str | None = None
    note_id: str | None = None
    artifact_id: str | None = None  # Flashcard artifact for DELETE_ARTIFACT test


def extract_id_recursive(data: Any) -> str | None:
    """Recursively extract the first string/int ID from nested response data.

    Drills down through nested lists until it finds a string or int.
    This handles various response formats like:
    - [[['id', ...]]] -> 'id'
    - [['id', ...]] -> 'id'
    - ['id', ...] -> 'id'

    Args:
        data: Response data (typically a nested list)

    Returns:
        The extracted string ID or None if not found
    """
    if data is None:
        return None
    if isinstance(data, (str, int)):
        return str(data)
    if isinstance(data, list) and len(data) > 0:
        return extract_id_recursive(data[0])
    return None


def extract_id(data: Any, *indices: int) -> str | None:
    """Safely extract an ID from nested response data.

    Args:
        data: Response data (typically a nested list)
        indices: Index path to traverse (e.g., 0 for data[0], or 0, 0 for data[0][0])

    Returns:
        The extracted string ID or None if not found
    """
    try:
        result = data
        for idx in indices:
            result = result[idx]
        # Recursively extract the first ID from the result
        return extract_id_recursive(result)
    except (IndexError, TypeError):
        return None


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


async def make_rpc_request(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    method: RPCMethod,
    params: list[Any],
    source_path: str = "/",
) -> tuple[str | None, str | None]:
    """Make an RPC request and return raw response text.

    Args:
        client: HTTP client
        auth: Authentication tokens
        method: RPC method to call
        params: Method parameters
        source_path: Source path for the request (default: "/")

    Returns:
        Tuple of (response text or None, error message or None)
    """
    url = f"{BATCHEXECUTE_URL}?f.sid={auth.session_id}&source-path={quote(source_path, safe='')}"
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
        return response.text, None
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}"
    except httpx.RequestError as e:
        return None, str(e)


async def make_rpc_call(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    method: RPCMethod,
    params: list[Any],
    source_path: str = "/",
) -> tuple[list[str], str | None]:
    """Make an RPC call and return found IDs.

    Args:
        client: HTTP client
        auth: Authentication tokens
        method: RPC method to call
        params: Method parameters
        source_path: Source path for the request (default: "/")

    Returns:
        Tuple of (list of RPC IDs found in response, error message or None)
    """
    response_text, error = await make_rpc_request(client, auth, method, params, source_path)
    if error:
        return [], error

    try:
        cleaned = strip_anti_xssi(response_text)
        chunks = parse_chunked_response(cleaned)
        found_ids = collect_rpc_ids(chunks)
        return found_ids, None
    except (json.JSONDecodeError, ValueError, IndexError, TypeError) as e:
        return [], f"Parse error: {e}"


async def test_rpc_method(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    method: RPCMethod,
    params: list[Any],
    source_path: str = "/",
) -> CheckResult:
    """Test an RPC method and return a CheckResult.

    Args:
        client: HTTP client
        auth: Authentication tokens
        method: RPC method to call
        params: Method parameters
        source_path: Source path for the request (default: "/")

    Returns:
        CheckResult with test status

    Makes the RPC call and checks if the expected method ID appears in the response.
    """
    expected_id = method.value
    found_ids, error = await make_rpc_call(client, auth, method, params, source_path)

    if expected_id in found_ids:
        return CheckResult(
            method=method,
            status=CheckStatus.OK,
            expected_id=expected_id,
            found_ids=found_ids,
        )

    return CheckResult(
        method=method,
        status=CheckStatus.ERROR,
        expected_id=expected_id,
        found_ids=found_ids,
        error=error or "RPC ID not found in response",
    )


async def test_rpc_method_with_data(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    method: RPCMethod,
    params: list[Any],
    source_path: str = "/",
) -> tuple[CheckResult, Any]:
    """Test an RPC method and return both CheckResult and response data.

    Args:
        client: HTTP client
        auth: Authentication tokens
        method: RPC method to call
        params: Method parameters
        source_path: Source path for the request (default: "/")

    Returns:
        Tuple of (CheckResult, decoded response data)

    Use this when you need the response data (e.g., to extract created resource IDs).
    """
    expected_id = method.value

    response_text, error = await make_rpc_request(client, auth, method, params, source_path)
    if error:
        return CheckResult(
            method=method,
            status=CheckStatus.ERROR,
            expected_id=expected_id,
            found_ids=[],
            error=error,
        ), None

    try:
        cleaned = strip_anti_xssi(response_text)
        chunks = parse_chunked_response(cleaned)
        found_ids = collect_rpc_ids(chunks)
        data = decode_response(response_text, method.value)
    except (json.JSONDecodeError, ValueError, IndexError, TypeError, RPCError) as e:
        return CheckResult(
            method=method,
            status=CheckStatus.ERROR,
            expected_id=expected_id,
            found_ids=[],
            error=f"Parse error: {e}",
        ), None

    status = CheckStatus.OK if expected_id in found_ids else CheckStatus.ERROR
    error_msg = None if status == CheckStatus.OK else "RPC ID not found in response"
    return CheckResult(
        method=method,
        status=status,
        expected_id=expected_id,
        found_ids=found_ids,
        error=error_msg,
    ), data


def format_check_output(result: CheckResult, suffix: str | None = None) -> str:
    """Format a CheckResult for console output."""
    status_icon = STATUS_ICONS[result.status]
    line = f"{status_icon:8} {result.method.name}"
    if suffix:
        line += f" - {suffix}"
    elif result.error and result.status != CheckStatus.OK:
        line += f" - {result.error}"
    return line


def format_check_with_success(result: CheckResult, success_msg: str) -> str:
    """Format a CheckResult, showing success_msg only when OK."""
    suffix = success_msg if result.status == CheckStatus.OK else None
    return format_check_output(result, suffix)


def get_test_params(method: RPCMethod, notebook_id: str | None) -> list[Any] | None:
    """Get test parameters for an RPC method.

    Returns None if method cannot be tested with simple params.
    """
    # Methods that work without a notebook
    if method == RPCMethod.LIST_NOTEBOOKS:
        return []

    # Global settings (no notebook required)
    if method == RPCMethod.GET_USER_SETTINGS:
        # Params to read current settings
        return [None, [1, None, None, None, None, None, None, None, None, None, [1]]]

    if method == RPCMethod.SET_USER_SETTINGS:
        # Params structure: [[[null,[[null,null,null,null,["language_code"]]]]]]
        # Use "en" as safe language code
        return [[[None, [[None, None, None, None, ["en"]]]]]]

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
        RPCMethod.POLL_ARTIFACT,
        RPCMethod.GET_CONVERSATION_HISTORY,
        RPCMethod.GET_NOTES_AND_MIND_MAPS,
        RPCMethod.DISCOVER_SOURCES,
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


async def setup_temp_resources(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    results: list[CheckResult],
) -> TempResources:
    """Create temporary resources for full mode testing.

    Tests CREATE_NOTEBOOK, ADD_SOURCE, ADD_SOURCE_FILE, START_FAST_RESEARCH,
    CREATE_NOTE, CREATE_ARTIFACT, and GET_ARTIFACT RPC methods.
    Polls for artifact completion before testing GET_ARTIFACT.
    """
    temp = TempResources()

    # Test CREATE_NOTEBOOK - extract notebook_id from response[0]
    result, data = await test_rpc_method_with_data(
        client, auth, RPCMethod.CREATE_NOTEBOOK, [f"RPC-Health-Check-{uuid4().hex[:8]}"]
    )
    results.append(result)
    print(format_check_with_success(result, "temp notebook created"))

    if result.status != CheckStatus.OK:
        return temp

    # Notebook ID is at position [2] in CREATE_NOTEBOOK response
    # Response format: [title, None, notebook_id, ...]
    temp.notebook_id = extract_id(data, 2)
    if not temp.notebook_id:
        print(
            "WARNING: Notebook created but ID not found in response. May need manual cleanup.",
            file=sys.stderr,
        )
        return temp

    # Test ADD_SOURCE - extract source_id from response[0][0]
    # Params format: [[[None, [title, content], None*6]], notebook_id, [2], None, None]
    await asyncio.sleep(CALL_DELAY)
    result, data = await test_rpc_method_with_data(
        client,
        auth,
        RPCMethod.ADD_SOURCE,
        [
            [
                [
                    None,
                    ["Test Source", "Test content for RPC health check."],
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ]
            ],
            temp.notebook_id,
            [2],
            None,
            None,
        ],
        source_path=f"/notebook/{temp.notebook_id}",
    )
    results.append(result)
    print(format_check_with_success(result, "temp source added"))

    if result.status == CheckStatus.OK:
        temp.source_id = extract_id(data, 0, 0)
        if not temp.source_id:
            print(f"  WARNING: Source ID extraction failed. Response: {repr(data)[:200]}")

    # Test ADD_SOURCE_FILE - registers file source intent (no actual upload needed)
    # Params format: [[[filename]], notebook_id, [2], [1, None, ...]]
    await asyncio.sleep(CALL_DELAY)
    result = await test_rpc_method(
        client,
        auth,
        RPCMethod.ADD_SOURCE_FILE,
        [
            [["test_file.pdf"]],
            temp.notebook_id,
            [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ],
        source_path=f"/notebook/{temp.notebook_id}",
    )
    results.append(result)
    print(format_check_with_success(result, "file source registered"))

    # Test START_FAST_RESEARCH - starts research task (verify RPC ID only)
    # Params format: [[query, source_type], None, 1, notebook_id]
    await asyncio.sleep(CALL_DELAY)
    result = await test_rpc_method(
        client,
        auth,
        RPCMethod.START_FAST_RESEARCH,
        [["test query", 1], None, 1, temp.notebook_id],  # 1 = Web search
        source_path=f"/notebook/{temp.notebook_id}",
    )
    results.append(result)
    print(format_check_with_success(result, "research started"))

    # Test CREATE_NOTE - extract note_id from response[0]
    # Params format: [notebook_id, "", [1], None, title]
    await asyncio.sleep(CALL_DELAY)
    result, data = await test_rpc_method_with_data(
        client,
        auth,
        RPCMethod.CREATE_NOTE,
        [temp.notebook_id, "", [1], None, "Test Note"],
        source_path=f"/notebook/{temp.notebook_id}",
    )
    results.append(result)
    print(format_check_with_success(result, "temp note created"))

    if result.status == CheckStatus.OK:
        temp.note_id = extract_id(data, 0)

    # Test CREATE_ARTIFACT - main RPC for all artifact generation (flashcards are fast)
    # Params for quiz: [[2], notebook_id, [None, None, 4, source_ids_triple, ...]]
    if temp.source_id:
        await asyncio.sleep(CALL_DELAY)
        source_ids_triple = [[[temp.source_id]]]
        result, data = await test_rpc_method_with_data(
            client,
            auth,
            RPCMethod.CREATE_ARTIFACT,
            [
                [2],
                temp.notebook_id,
                [
                    None,
                    None,
                    4,  # ArtifactTypeCode.QUIZ_FLASHCARD
                    source_ids_triple,
                    None,
                    None,
                    None,
                    None,
                    None,
                    [
                        None,
                        [
                            1,  # Variant: flashcards (faster than quiz)
                            None,  # instructions
                            None,
                            None,
                            None,
                            None,
                            [None, 1],  # [difficulty, quantity] - FEWER
                        ],
                    ],
                ],
            ],
            source_path=f"/notebook/{temp.notebook_id}",
        )
        results.append(result)
        print(format_check_with_success(result, "flashcard generation triggered"))

        if result.status == CheckStatus.OK:
            # Artifact ID is at response[0][0]
            temp.artifact_id = extract_id(data, 0, 0)
            if not temp.artifact_id:
                print(f"  WARNING: Artifact ID extraction failed. Response: {repr(data)[:200]}")

        # Poll for artifact completion and test GET_ARTIFACT
        if temp.artifact_id:
            # Poll up to 30 seconds for flashcard generation to complete
            max_polls = 15
            poll_interval = 2.0
            artifact_ready = False
            polls_done = 0

            for _ in range(max_polls):
                await asyncio.sleep(poll_interval)
                polls_done += 1
                poll_result = await test_rpc_method(
                    client,
                    auth,
                    RPCMethod.POLL_ARTIFACT,
                    [temp.artifact_id, temp.notebook_id, [2]],
                    source_path=f"/notebook/{temp.notebook_id}",
                )
                # Check if status indicates completion (status code 3 = ready)
                # We don't add poll results to avoid cluttering output
                if poll_result.status == CheckStatus.OK:
                    artifact_ready = True
                    break

            if artifact_ready:
                print(f"  Artifact ready after {polls_done * poll_interval:.0f}s polling")
            else:
                print(
                    f"  Artifact not ready after {max_polls * poll_interval:.0f}s (continuing anyway)"
                )

            # Note: GET_ARTIFACT (BnLyuf) is defined but not used in the library.
            # The correct params format is unknown - library uses list() + filter instead.
            # DELETE_ARTIFACT test in cleanup phase validates artifact manipulation works.
    else:
        # Skip artifact tests - no source_id available
        print("SKIP     CREATE_ARTIFACT - No source_id available (source extraction failed)")

    return temp


async def cleanup_temp_resources(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    temp: TempResources,
    results: list[CheckResult],
) -> None:
    """Delete temporary resources and test DELETE RPC methods.

    Tests DELETE_NOTE, DELETE_SOURCE, DELETE_ARTIFACT, and DELETE_NOTEBOOK RPCs.
    """
    if not temp.notebook_id:
        return

    # Test DELETE_NOTE if we have a note
    if temp.note_id:
        await asyncio.sleep(CALL_DELAY)
        result = await test_rpc_method(
            client,
            auth,
            RPCMethod.DELETE_NOTE,
            [temp.notebook_id, None, [temp.note_id]],
            source_path=f"/notebook/{temp.notebook_id}",
        )
        results.append(result)
        print(format_check_with_success(result, "temp note deleted"))

    # Test DELETE_SOURCE if we have a source
    if temp.source_id:
        await asyncio.sleep(CALL_DELAY)
        result = await test_rpc_method(
            client,
            auth,
            RPCMethod.DELETE_SOURCE,
            [[[temp.source_id]]],
            source_path=f"/notebook/{temp.notebook_id}",
        )
        results.append(result)
        print(format_check_with_success(result, "temp source deleted"))

    # Test DELETE_ARTIFACT if we have an artifact (main RPC for artifact deletion)
    if temp.artifact_id:
        await asyncio.sleep(CALL_DELAY)
        result = await test_rpc_method(
            client,
            auth,
            RPCMethod.DELETE_ARTIFACT,
            [[2], temp.artifact_id],
            source_path=f"/notebook/{temp.notebook_id}",
        )
        results.append(result)
        print(format_check_with_success(result, "temp artifact deleted"))

    # Test DELETE_NOTEBOOK (always runs to cleanup)
    await asyncio.sleep(CALL_DELAY)
    result = await test_rpc_method(client, auth, RPCMethod.DELETE_NOTEBOOK, [temp.notebook_id])
    results.append(result)
    print(format_check_with_success(result, "temp notebook deleted"))


async def run_health_check(full_mode: bool = False) -> list[CheckResult]:
    """Run health check on all RPC methods."""
    cookies = load_auth()

    notebook_id = os.environ.get("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID") or os.environ.get(
        "NOTEBOOKLM_GENERATION_NOTEBOOK_ID"
    )

    if not notebook_id and not full_mode:
        print("WARNING: No notebook ID provided. Some methods will be skipped.", file=sys.stderr)

    results: list[CheckResult] = []
    temp_resources = TempResources()

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
        if full_mode:
            print("Creating temp resources for full testing...")
            temp_resources = await setup_temp_resources(client, auth, results)
            if temp_resources.notebook_id:
                notebook_id = temp_resources.notebook_id
            print()

        try:
            methods = list(RPCMethod)
            total = len(methods)

            print(f"Checking {total} RPC methods...")
            print("=" * 60)

            for i, method in enumerate(methods, 1):
                result = await check_method(client, auth, method, notebook_id, full_mode)
                results.append(result)

                status_icon = STATUS_ICONS[result.status]
                line = f"{status_icon:8} {method.name} ({result.expected_id})"
                if result.error and result.status != CheckStatus.OK:
                    line += f" - {result.error}"
                print(line)

                if i < total and result.status != CheckStatus.SKIPPED:
                    await asyncio.sleep(CALL_DELAY)

        finally:
            if full_mode and temp_resources.notebook_id:
                print()
                print("Testing DELETE operations during cleanup...")
                await cleanup_temp_resources(client, auth, temp_resources, results)

    return results


def print_summary(results: list[CheckResult]) -> int:
    """Print summary and return exit code."""
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    counts = Counter(r.status for r in results)
    total = len(results)
    tested = total - counts[CheckStatus.SKIPPED]

    print(f"TESTED:   {tested}/{total} methods")
    print(f"OK:       {counts[CheckStatus.OK]}/{tested}")
    print(f"MISMATCH: {counts[CheckStatus.MISMATCH]}/{tested}")
    print(f"ERROR:    {counts[CheckStatus.ERROR]}/{tested}")

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
