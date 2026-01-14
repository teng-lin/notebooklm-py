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
    python scripts/check_rpc_health.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

from notebooklm.auth import AuthTokens
from notebooklm.rpc import (
    BATCHEXECUTE_URL,
    RPCMethod,
    build_request_body,
    encode_rpc_request,
)
from notebooklm.rpc.decoder import (
    collect_rpc_ids,
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

# Methods that cannot be safely tested (destructive, create resources, or require setup)
SKIP_METHODS = {
    # Destructive operations
    RPCMethod.DELETE_NOTEBOOK,
    RPCMethod.DELETE_SOURCE,
    RPCMethod.DELETE_AUDIO,
    RPCMethod.DELETE_STUDIO,
    RPCMethod.DELETE_NOTE,
    # Resource-creating operations (would leak test data on each run)
    RPCMethod.CREATE_NOTEBOOK,
    RPCMethod.ADD_SOURCE,
    RPCMethod.CREATE_NOTE,
    RPCMethod.CREATE_AUDIO,
    RPCMethod.CREATE_VIDEO,
    RPCMethod.CREATE_ARTIFACT,
    RPCMethod.START_FAST_RESEARCH,
    RPCMethod.START_DEEP_RESEARCH,
    # Upload requires multipart file handling
    RPCMethod.ADD_SOURCE_FILE,
    # Query endpoint is not a batchexecute RPC
    RPCMethod.QUERY_ENDPOINT,
}

# Methods that are duplicates (same ID, different name)
DUPLICATE_METHODS = {
    RPCMethod.GENERATE_MIND_MAP,  # Same as ACT_ON_SOURCES (yyryJe)
    RPCMethod.LIST_ARTIFACTS,  # Same as POLL_STUDIO (gArtLc)
}


def load_cookies_from_env() -> dict[str, str]:
    """Load cookies from NOTEBOOKLM_AUTH_JSON environment variable."""
    auth_json = os.environ.get("NOTEBOOKLM_AUTH_JSON")
    if not auth_json:
        print("ERROR: NOTEBOOKLM_AUTH_JSON environment variable not set", file=sys.stderr)
        sys.exit(1)

    try:
        storage_state = json.loads(auth_json)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in NOTEBOOKLM_AUTH_JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract cookies
    return {cookie["name"]: cookie["value"] for cookie in storage_state.get("cookies", [])}


async def fetch_tokens(client: httpx.AsyncClient, cookies: dict[str, str]) -> AuthTokens:
    """Fetch CSRF token and session ID from NotebookLM homepage."""
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    response = await client.get(
        "https://notebooklm.google.com/",
        headers={"Cookie": cookie_header},
        follow_redirects=True,
    )
    html = response.text

    # Extract CSRF token (SNlM0e)
    csrf_match = re.search(r'"SNlM0e"\s*:\s*"([^"]+)"', html)
    if not csrf_match:
        print("ERROR: Could not extract CSRF token. Auth may be expired.", file=sys.stderr)
        sys.exit(1)
    csrf_token = csrf_match.group(1)

    # Extract session ID (FdrFJe)
    session_match = re.search(r'"FdrFJe"\s*:\s*"([^"]+)"', html)
    session_id = session_match.group(1) if session_match else ""

    return AuthTokens(cookies=cookies, csrf_token=csrf_token, session_id=session_id)


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
) -> CheckResult:
    """Check a single RPC method."""
    expected_id = method.value

    # Skip methods that can't be tested
    if method in SKIP_METHODS:
        return CheckResult(
            method=method,
            status=CheckStatus.SKIPPED,
            expected_id=expected_id,
            found_ids=[],
            error="Method skipped (destructive or requires setup)",
        )

    if method in DUPLICATE_METHODS:
        return CheckResult(
            method=method,
            status=CheckStatus.SKIPPED,
            expected_id=expected_id,
            found_ids=[],
            error="Duplicate method (same ID as another)",
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


async def run_health_check() -> list[CheckResult]:
    """Run health check on all RPC methods."""
    cookies = load_cookies_from_env()

    # Get notebook IDs
    read_only_id = os.environ.get("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID")
    generation_id = os.environ.get("NOTEBOOKLM_GENERATION_NOTEBOOK_ID")
    notebook_id = read_only_id or generation_id

    if not notebook_id:
        print("WARNING: No notebook ID provided. Some methods will be skipped.", file=sys.stderr)

    results: list[CheckResult] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Fetch auth tokens
        print("Fetching auth tokens...")
        auth = await fetch_tokens(client, cookies)
        print(f"Auth OK (CSRF token length: {len(auth.csrf_token)})")
        print()

        # Get all RPC methods
        methods = list(RPCMethod)
        total = len(methods)

        print(f"Checking {total} RPC methods...")
        print("=" * 60)

        for i, method in enumerate(methods, 1):
            result = await check_method(client, auth, method, notebook_id)
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

    # Return exit code
    if counts[CheckStatus.MISMATCH] > 0:
        print("RESULT: FAIL - RPC ID mismatches detected")
        return 1
    if counts[CheckStatus.ERROR] > counts[CheckStatus.OK]:
        print("RESULT: WARN - Many errors (possible auth issue)")
        return 1
    print("RESULT: PASS - All tested RPC methods OK")
    return 0


def main() -> int:
    """Main entry point."""
    print("RPC Health Check")
    print("=" * 60)
    print()

    results = asyncio.run(run_health_check())
    return print_summary(results)


if __name__ == "__main__":
    sys.exit(main())
