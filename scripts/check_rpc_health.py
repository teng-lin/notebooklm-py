#!/usr/bin/env python3
"""RPC Health Check - Verify NotebookLM RPC method IDs are still valid.

This script makes minimal API calls to exercise RPC methods and verify
that the method IDs in rpc/types.py still match what the API returns.

Exit codes:
    0 - All RPC methods OK (or only transient errors: rate-limits / ReadTimeouts)
    1 - One or more RPC methods have mismatched IDs
    2 - Authentication or infrastructure failure (not an RPC problem)
    3 - One or more RPC methods returned a non-transient ERROR
        (timeouts, parse failures, unexpected HTTP errors)
    4 - Studio-customization cohort flip: the ``sqTeoe`` tripwire
        (GetArtifactCustomizationChoices) returned non-null, meaning our
        account migrated to the new customization surface and the VideoStyle /
        format codes must be re-captured. GATED-null is the expected steady
        state and is NOT a failure.

Priority order when multiple statuses are present:
    MISMATCH (1) > AUTH (2) > non-transient ERROR (3) > cohort MIGRATED (4) > OK (0)

Transient errors that still exit 0 are limited to rate-limit signals
(HTTP 429, gRPC ``RESOURCE_EXHAUSTED``, and the decoder's user-displayable
``API rate limit`` / quota messages raised as ``RateLimitError``) plus
``httpx.ReadTimeout`` against Google's RPC endpoints — those are almost
always server-side slowness, not an RPC contract change, and they
consistently pass on retry (see #1004). Everything else (parse failures,
unexpected HTTP status codes, schema mismatches) is still treated as a
real failure so the nightly canary can flag silent breakage.

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
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import httpx

from notebooklm._artifact.payloads import build_retry_artifact_params
from notebooklm._chat.wire import (
    build_streaming_chat_request,
    parse_streaming_chat_response,
)
from notebooklm._env import get_default_language
from notebooklm._logging import scrub_secrets
from notebooklm._notebooks import build_create_notebook_params
from notebooklm.auth import (
    AuthTokens,
    fetch_tokens,
    get_account_email_for_storage,
    get_authuser_for_storage,
    load_auth_from_storage,
)
from notebooklm.exceptions import ChatError, ChatResponseParseError, DecodingError
from notebooklm.paths import get_storage_path
from notebooklm.rpc import (
    RPCError,
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
from notebooklm.rpc.types import _QUERY_ENDPOINT_PATH


class CheckStatus(str, Enum):
    """Result status for an RPC check."""

    OK = "OK"
    MISMATCH = "MISMATCH"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class CohortStatus(str, Enum):
    """Result of the ``sqTeoe`` studio-customization cohort tripwire.

    ``GetArtifactCustomizationChoices`` (rpc id ``sqTeoe``) is gated off for our
    consumer cohort and returns ``null`` unconditionally today (``GATED`` — the
    expected steady state, NOT a failure). The day it starts returning a
    non-null payload our account has been migrated to the new studio-customization
    surface (``MIGRATED`` — a loud signal: the VideoStyle/format codes must be
    re-captured). ``UNKNOWN`` covers a transient/transport failure on the probe
    itself (no cohort conclusion drawn — never an alarm).
    """

    GATED = "GATED"
    MIGRATED = "MIGRATED"
    UNKNOWN = "UNKNOWN"


# Studio-customization cohort tripwire RPC. NOT in ``RPCMethod`` (it is gated off
# for our consumer cohort, so there is no typed/public path), called by raw id.
_CUSTOMIZATION_CHOICES_RPC_ID = "sqTeoe"


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
# Currently empty - no duplicate method IDs in use
DUPLICATE_METHODS: set[RPCMethod] = set()

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
    RPCMethod.DELETE_CONVERSATION,  # Destructive; needs a real conversation to delete
}

# Methods always skipped (even in full mode)
ALWAYS_SKIP_METHODS = {
    # Takes too long
    RPCMethod.START_DEEP_RESEARCH,
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
    if isinstance(data, str | int):
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
        # API responses have varying nesting depths (e.g., [[['id']]], [['id']], ['id']).
        # Recursively drill down to find the actual string/int ID.
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
        sys.exit(2)
    except ValueError as e:
        print(f"ERROR: Invalid authentication: {e}", file=sys.stderr)
        sys.exit(2)
    return cookies


def resolve_storage_path() -> Path | None:
    """Return the file-based auth path, or None when NOTEBOOKLM_AUTH_JSON is used."""
    if "NOTEBOOKLM_AUTH_JSON" in os.environ:
        return None
    return get_storage_path()


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
    query_params = {
        "rpcids": method.value,
        "source-path": source_path,
        "f.sid": auth.session_id,
        "hl": get_default_language(),
        "rt": "c",
    }
    if auth.account_email or auth.authuser:
        query_params["authuser"] = auth.account_route
    url = f"{get_batchexecute_url()}?{urlencode(query_params)}"
    rpc_request = encode_rpc_request(method, params)
    body = build_request_body(rpc_request, auth.csrf_token)

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": auth.cookie_header,
    }

    try:
        response = await client.post(url, content=body, headers=headers)
        response.raise_for_status()
        return response.text, None
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}"
    except httpx.RequestError as e:
        # ``httpx.ReadTimeout`` can stringify to ``""``; fall back to the
        # class name so callers don't mislabel it as an empty response (#864).
        return None, str(e) or type(e).__name__


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
    if error is not None:
        return [], error
    if response_text is None:
        return [], "Empty response from server"

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
        error=error if error is not None else "RPC ID not found in response",
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
    if error is not None:
        return CheckResult(
            method=method,
            status=CheckStatus.ERROR,
            expected_id=expected_id,
            found_ids=[],
            error=error,
        ), None
    if response_text is None:
        return CheckResult(
            method=method,
            status=CheckStatus.ERROR,
            expected_id=expected_id,
            found_ids=[],
            error="Empty response from server",
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

    # GET_USER_TIER: read subscription tier from homepage context (no notebook required)
    # Params mirror build_get_user_tier_params() in src/notebooklm/_settings.py.
    if method == RPCMethod.GET_USER_TIER:
        return [
            [
                [
                    [None, "1", 627],
                    [None, None, None, None, None, None, None, None, None, [None, None, 2]],
                    1,
                ]
            ]
        ]

    # Methods that require a notebook ID
    if not notebook_id:
        return None

    # Methods that take [notebook_id] as the only param
    if method in (
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.GET_SOURCE_GUIDE,
        RPCMethod.GET_SHARE_STATUS,
        RPCMethod.REMOVE_RECENTLY_VIEWED,
    ):
        return [notebook_id]

    # GET_SUGGESTED_REPORTS has special params: [[2], notebook_id].
    # Suggestions only exist once a notebook has indexed sources, so the
    # freshly-created temp notebook used in --full mode returns an empty
    # body and trips the empty-response guard. When a stable read-only
    # notebook is available, route this method there instead so the
    # canary keeps drift-checking the RPC ID.
    if method == RPCMethod.GET_SUGGESTED_REPORTS:
        stable_id = (
            os.environ.get("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID")
            or os.environ.get("NOTEBOOKLM_GENERATION_NOTEBOOK_ID")
            or notebook_id
        )
        return [[2], stable_id]

    # Methods that take [[notebook_id]] as the only param.
    if method == RPCMethod.GET_NOTES_AND_MIND_MAPS:
        return [[notebook_id]]

    # GET_LAST_CONVERSATION_ID: returns most recent conversation ID
    if method == RPCMethod.GET_LAST_CONVERSATION_ID:
        return [[], None, notebook_id, 1]

    # GET_CONVERSATION_TURNS: placeholder conv ID - API echoes RPC ID even in error response
    if method == RPCMethod.GET_CONVERSATION_TURNS:
        return [[], None, None, "placeholder_conv_id", 2]

    # SUGGEST_PROMPTS (GeneratePromptSuggestions): like GET_SUGGESTED_REPORTS,
    # suggestions only exist once a notebook has indexed sources, so route to a
    # stable read-only notebook when one is configured. The required mode enum
    # (1..9) is set to the default 4; an empty source-id list scopes to all of
    # the notebook's sources server-side. The canary only verifies the RPC ID
    # echoes back, so empty source ids are sufficient.
    if method == RPCMethod.SUGGEST_PROMPTS:
        stable_id = (
            os.environ.get("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID")
            or os.environ.get("NOTEBOOKLM_GENERATION_NOTEBOOK_ID")
            or notebook_id
        )
        return [
            [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
            stable_id,
            [],
            4,
            None,
            None,
        ]

    # LIST_ARTIFACTS has special params
    if method == RPCMethod.LIST_ARTIFACTS:
        return [[2], notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"']

    # LIST_LABELS: list a notebook's source labels (read-only; OPTS wrapper + nb id)
    if method == RPCMethod.LIST_LABELS:
        return [
            [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
            notebook_id,
        ]

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
    if method == RPCMethod.GET_INTERACTIVE_HTML:
        return [[notebook_id], "placeholder"]

    if method == RPCMethod.RENAME_ARTIFACT:
        return [[notebook_id], "placeholder", "New Name"]

    if method == RPCMethod.EXPORT_ARTIFACT:
        return [[notebook_id], "placeholder", 1]

    if method == RPCMethod.REVISE_SLIDE:
        # Params: [[2], artifact_id, [[[slide_index, prompt]]]]
        # Will fail with placeholder artifact_id but still echoes method ID in error response
        return [[2], "placeholder_artifact_id", [[[0, "RPC health check test"]]]]

    if method == RPCMethod.RETRY_ARTIFACT:
        # Params: [retry_options, artifact_id]. The placeholder artifact_id
        # matches no real artifact, so this is a safe liveness probe (no
        # actual retry is kicked off) that still echoes the method ID in the
        # error response — same posture as REVISE_SLIDE/RENAME_ARTIFACT above.
        return build_retry_artifact_params("placeholder_artifact_id")

    # Research operations (read-only - poll/import only)
    if method == RPCMethod.POLL_RESEARCH:
        return [[notebook_id], "placeholder_task_id"]

    if method == RPCMethod.IMPORT_RESEARCH:
        return [[notebook_id], "placeholder_research_id"]

    # Note operations (read-only - update only)
    if method == RPCMethod.UPDATE_NOTE:
        return [[notebook_id], "placeholder", "Updated", "Updated content"]

    # Mind map operation (read-only)
    if method == RPCMethod.GENERATE_MIND_MAP:
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

    if error is not None:
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


@dataclass
class _ChatQueryProbe:
    """Method-like sentinel for the ``GenerateFreeFormStreamed`` chat probe.

    The streamed-chat orchestration endpoint is NOT a batchexecute RPC ID — it
    is a ``PATH_NOT_METHOD`` ``v1`` URL segment (see ``_QUERY_ENDPOINT_PATH`` in
    ``rpc/types.py``), so it has no :class:`RPCMethod` enum member and cannot be
    probed by ``make_rpc_call``/``check_method`` like the obfuscated method IDs.
    This ducktype gives the probe's :class:`CheckResult` the ``.name`` /
    ``.value`` attributes that ``print_summary`` / ``format_check_output`` /
    ``partition_errors`` read, so the chat result flows through the existing
    summary + exit-code machinery unchanged (an ``ERROR`` here is classified
    and reported as drift exactly like a method-ID probe).
    """

    name: str = "GENERATE_FREE_FORM_STREAMED"
    value: str = _QUERY_ENDPOINT_PATH


# Singleton sentinel reused for every chat-probe CheckResult.
CHAT_QUERY_PROBE = _ChatQueryProbe()


async def check_chat_query(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    notebook_id: str | None,
) -> CheckResult:
    """Probe the streamed-chat ``GenerateFreeFormStreamed`` orchestration RPC.

    This is the chat surface's drift canary. Unlike the batchexecute method-ID
    probes, the chat endpoint is a hardcoded ``v1`` path with no obfuscated RPC
    ID to echo back, so liveness is asserted on the *wire shape* instead: issue
    one minimal request and require an HTTP 200 plus a recognizable stream frame
    (a ``wrb.fr`` envelope or a server ``"er"`` frame).

    Classification mirrors the method-ID probes:

    * Parseable stream (an answer, or a server-side ``ChatError`` frame the
      parser recognizes — including a rate-limit / rejected frame) -> ``OK``:
      the wire contract is intact, the server merely declined this request.
    * Zero parseable chunks (:class:`ChatResponseParseError`) -> ``ERROR``: the
      response body was empty or the wire format drifted. This is the schema
      drift the canary exists to catch.
    * Non-200 / transport failure -> ``ERROR`` (rate-limit / ReadTimeout text is
      still classified as transient downstream by ``is_transient_error``).

    Skipped when no notebook ID is configured (the request needs one for
    server-side conversation persistence).
    """
    if not notebook_id:
        return CheckResult(
            method=CHAT_QUERY_PROBE,  # type: ignore[arg-type]
            status=CheckStatus.SKIPPED,
            expected_id=CHAT_QUERY_PROBE.value,
            found_ids=[],
            error="No notebook ID provided (chat probe needs one)",
        )

    url, body, extra_headers = build_streaming_chat_request(
        snapshot=auth,
        notebook_id=notebook_id,
        question="RPC health check ping.",
        source_ids=[],
        conversation_history=None,
        conversation_id=None,
        reqid=int(uuid4().int % 1_000_000),
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": auth.cookie_header,
        **extra_headers,
    }

    try:
        response = await client.post(url, content=body, headers=headers)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return CheckResult(
            method=CHAT_QUERY_PROBE,  # type: ignore[arg-type]
            status=CheckStatus.ERROR,
            expected_id=CHAT_QUERY_PROBE.value,
            found_ids=[],
            error=f"HTTP {e.response.status_code}",
        )
    except httpx.RequestError as e:
        # ``httpx.ReadTimeout`` can stringify to ``""``; fall back to the class
        # name so the downstream transient classifier (and the operator) sees a
        # usable signal — same posture as ``make_rpc_request`` (#864).
        return CheckResult(
            method=CHAT_QUERY_PROBE,  # type: ignore[arg-type]
            status=CheckStatus.ERROR,
            expected_id=CHAT_QUERY_PROBE.value,
            found_ids=[],
            error=str(e) or type(e).__name__,
        )

    try:
        parse_streaming_chat_response(response.text)
    except (ChatResponseParseError, DecodingError, json.JSONDecodeError) as e:
        # Zero parseable chunks (empty/drifted body), a structurally malformed
        # JSON body (``json.JSONDecodeError`` — a ``ValueError`` subclass we
        # catch specifically so a bare ``ValueError`` from a real bug still
        # propagates), OR strict positional drift raising DecodingError /
        # UnknownRPCMethodError from the wire decoder: all are the schema-drift
        # signal the canary exists to surface. Catch them here so a drifted
        # response returns a clean ERROR result rather than crashing the canary.
        return CheckResult(
            method=CHAT_QUERY_PROBE,  # type: ignore[arg-type]
            status=CheckStatus.ERROR,
            expected_id=CHAT_QUERY_PROBE.value,
            found_ids=[],
            error=f"Parse error: {e}",
        )
    except ChatError as e:
        # A recognized server-side ``"er"`` / rate-limit frame: the wire shape
        # is INTACT (the parser identified the frame), the server simply
        # declined this request. Treat as OK — the contract is healthy. The
        # message is preserved for the operator (and so a rate-limit frame
        # stays visible), but it does not fail the canary.
        return CheckResult(
            method=CHAT_QUERY_PROBE,  # type: ignore[arg-type]
            status=CheckStatus.OK,
            expected_id=CHAT_QUERY_PROBE.value,
            found_ids=[CHAT_QUERY_PROBE.value],
            error=f"Server declined (recognized frame): {e}",
        )

    return CheckResult(
        method=CHAT_QUERY_PROBE,  # type: ignore[arg-type]
        status=CheckStatus.OK,
        expected_id=CHAT_QUERY_PROBE.value,
        found_ids=[CHAT_QUERY_PROBE.value],
    )


def build_customization_choices_params(notebook_id: str) -> list[Any]:
    """Build ``sqTeoe`` (GetArtifactCustomizationChoices) params for ``notebook_id``.

    Reverse-engineered (live-verified) request shape: ``[nbctx, None, artifact_type]``
    where ``nbctx`` is the standard notebook-context options wrapper and
    ``artifact_type`` is ``3`` (video) — the surface whose VideoStyle/format codes
    we most want an early migration signal for. Extracted as a helper so the wire
    shape is testable without a live call.
    """
    nbctx = [
        2,
        None,
        None,
        [1, None, None, None, None, None, None, None, None, None, [1]],
        [notebook_id],
    ]
    artifact_type = 3  # video
    return [nbctx, None, artifact_type]


async def make_raw_rpc_request(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    rpc_id: str,
    params: list[Any],
    source_path: str = "/",
) -> tuple[str | None, str | None]:
    """Issue a batchexecute call by *raw* rpc id (for methods not in ``RPCMethod``).

    The cohort tripwire probes ``sqTeoe``, which has no ``RPCMethod`` member (it
    is gated for our cohort), so it cannot go through ``make_rpc_request``. This
    mirrors that function's wire assembly but threads the id straight into both
    the ``rpcids=`` query param and ``encode_rpc_request``'s ``rpc_id_override``
    (the two MUST stay in sync). It calls the executor directly — there is no
    idempotency registry in this script, so no retries to disable.
    """
    query_params = {
        "rpcids": rpc_id,
        "source-path": source_path,
        "f.sid": auth.session_id,
        "hl": get_default_language(),
        "rt": "c",
    }
    if auth.account_email or auth.authuser:
        query_params["authuser"] = auth.account_route
    url = f"{get_batchexecute_url()}?{urlencode(query_params)}"
    # Any RPCMethod member works as the first arg — ``rpc_id_override`` is what
    # actually lands in the body, kept identical to the ``rpcids=`` query param.
    rpc_request = encode_rpc_request(RPCMethod.LIST_NOTEBOOKS, params, rpc_id_override=rpc_id)
    body = build_request_body(rpc_request, auth.csrf_token)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": auth.cookie_header,
    }
    try:
        response = await client.post(url, content=body, headers=headers)
        response.raise_for_status()
        return response.text, None
    except httpx.HTTPStatusError as e:
        return None, f"HTTP {e.response.status_code}"
    except httpx.RequestError as e:
        # httpx.RequestError subclasses can stringify the failed request URL,
        # which carries f.sid; scrub before it reaches a live print site.
        return None, scrub_secrets(str(e) or type(e).__name__)


async def check_customization_cohort(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    notebook_id: str | None,
) -> tuple[CohortStatus, str]:
    """Probe ``sqTeoe`` to detect a studio-customization cohort flip.

    ``GetArtifactCustomizationChoices`` returns ``null`` for our gated consumer
    cohort. We decode with ``allow_null=True`` so a genuine null payload is
    ``GATED`` (the steady state, not a failure) — but the *absent-id* drift guard
    inside ``decode_response`` still fires if the id stopped being echoed (that
    surfaces as a parse error here and is reported as ``UNKNOWN``, not silently
    swallowed). A non-null payload means our account was migrated to the new
    customization surface (``MIGRATED`` — re-capture VideoStyle codes).

    Returns ``(status, detail)``. ``UNKNOWN`` is drawn for any transport/parse
    failure so the tripwire never alarms on a transient flake; only a clean,
    decoded non-null result is ``MIGRATED``.
    """
    if not notebook_id:
        return CohortStatus.UNKNOWN, "No notebook ID provided (cohort tripwire needs one)"

    response_text, error = await make_raw_rpc_request(
        client,
        auth,
        _CUSTOMIZATION_CHOICES_RPC_ID,
        build_customization_choices_params(notebook_id),
        source_path=f"/notebook/{notebook_id}",
    )
    if error is not None:
        return CohortStatus.UNKNOWN, error
    if response_text is None:
        return CohortStatus.UNKNOWN, "Empty response from server"

    # RPCError (incl. UnknownRPCMethodError from the absent-id drift guard) must
    # propagate so the tripwire LOUDLY surfaces an id rotation / protocol break
    # rather than degrading it to a quiet UNKNOWN. Bare ValueError likewise
    # propagates — it signals a code bug (e.g. an invalid int() conversion), not
    # API shape drift. Only a genuinely unreadable/malformed JSON root is caught
    # and reported as UNKNOWN here.
    try:
        data = decode_response(response_text, _CUSTOMIZATION_CHOICES_RPC_ID, allow_null=True)
    except (json.JSONDecodeError, TypeError) as e:
        return CohortStatus.UNKNOWN, f"Parse error: {scrub_secrets(e)}"

    if data is None:
        return CohortStatus.GATED, "null (gated — expected steady state)"
    return CohortStatus.MIGRATED, "non-null payload (cohort migrated)"


async def setup_temp_resources(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    results: list[CheckResult],
) -> TempResources:
    """Create temporary resources for full mode testing.

    Tests CREATE_NOTEBOOK, ADD_SOURCE, ADD_SOURCE_FILE, START_FAST_RESEARCH,
    CREATE_NOTE, and CREATE_ARTIFACT RPC methods.
    Gives artifact generation a short grace period before testing DELETE_ARTIFACT
    in cleanup.
    """
    temp = TempResources()

    # Test CREATE_NOTEBOOK - extract notebook_id from response[0]
    title = f"RPC-Health-Check-{uuid4().hex[:8]}"
    result, data = await test_rpc_method_with_data(
        client,
        auth,
        RPCMethod.CREATE_NOTEBOOK,
        build_create_notebook_params(title),
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
            # Decoded response may carry residual credential-shaped substrings
            # (cookies/CSRF tokens echoed in error payloads, etc.). Scrub the
            # FULL repr before slicing — slicing first risks chopping a
            # secret-shaped substring (e.g. ``cookie: SID=ab|cd``) at the
            # 200-char boundary, leaving the prefix outside the scrub
            # patterns. Scrub-then-truncate keeps the redaction intact even
            # if the bytes after position 200 carried the matching anchor.
            preview = scrub_secrets(repr(data))[:200]
            print(f"  WARNING: ADD_SOURCE ID extraction failed. Response: {preview}")

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
                # Same scrub-then-truncate ordering as the ADD_SOURCE
                # failure site upstream — slicing first risks chopping a
                # cookie / CSRF token at the 200-char boundary and
                # missing the scrub-pattern anchor.
                preview = scrub_secrets(repr(data))[:200]
                print(f"  WARNING: CREATE_ARTIFACT ID extraction failed. Response: {preview}")

        # Probe LIST_ARTIFACTS briefly so artifact generation gets a grace
        # period before DELETE_ARTIFACT cleanup.
        if temp.artifact_id:
            # Probe for up to 30 seconds before cleanup.
            max_polls = 15
            poll_interval = 2.0
            artifact_list_live = False
            polls_done = 0

            for _ in range(max_polls):
                await asyncio.sleep(poll_interval)
                polls_done += 1
                # LIST_ARTIFACTS is the available liveness probe; there is no
                # poll-by-ID RPC here.
                poll_result = await test_rpc_method(
                    client,
                    auth,
                    RPCMethod.LIST_ARTIFACTS,
                    [[2], temp.notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"'],
                    source_path=f"/notebook/{temp.notebook_id}",
                )
                # We only need the method to succeed; poll results are omitted
                # to keep output focused on the primary checks.
                if poll_result.status == CheckStatus.OK:
                    artifact_list_live = True
                    break

            if artifact_list_live:
                print(f"  Artifact list live after {polls_done * poll_interval:.0f}s polling")
            else:
                print(
                    f"  Artifact list not live after {max_polls * poll_interval:.0f}s "
                    "(continuing anyway)"
                )
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
    Always attempts to delete the notebook, even if other cleanup operations fail.
    """
    if not temp.notebook_id:
        return

    # Test DELETE_NOTE if we have a note (best effort - don't block notebook deletion)
    if temp.note_id:
        try:
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
        except Exception as e:
            print(f"ERROR    DELETE_NOTE - {e}")

    # Test DELETE_SOURCE if we have a source (best effort)
    if temp.source_id:
        try:
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
        except Exception as e:
            print(f"ERROR    DELETE_SOURCE - {e}")

    # Test DELETE_ARTIFACT if we have an artifact (best effort)
    if temp.artifact_id:
        try:
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
        except Exception as e:
            print(f"ERROR    DELETE_ARTIFACT - {e}")

    # ALWAYS delete notebook - this is critical to avoid orphaned notebooks
    try:
        await asyncio.sleep(CALL_DELAY)
        result = await test_rpc_method(client, auth, RPCMethod.DELETE_NOTEBOOK, [temp.notebook_id])
        results.append(result)
        print(format_check_with_success(result, "temp notebook deleted"))
    except Exception as e:
        print(f"ERROR    DELETE_NOTEBOOK - {e}")
        print(f"WARNING: Notebook {temp.notebook_id} may need manual cleanup", file=sys.stderr)


async def run_health_check(full_mode: bool = False) -> tuple[list[CheckResult], CohortStatus]:
    """Run health check on all RPC methods.

    Returns ``(results, cohort_status)``: the per-method ``CheckResult`` list plus
    the ``sqTeoe`` studio-customization cohort tripwire verdict (reported and
    exit-coded separately from RPC drift; see :class:`CohortStatus`).
    """
    storage_path = resolve_storage_path()
    cookies = load_auth()

    notebook_id = os.environ.get("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID") or os.environ.get(
        "NOTEBOOKLM_GENERATION_NOTEBOOK_ID"
    )

    if not notebook_id and not full_mode:
        print("WARNING: No notebook ID provided. Some methods will be skipped.", file=sys.stderr)

    results: list[CheckResult] = []
    temp_resources = TempResources()
    cohort_status = CohortStatus.UNKNOWN

    print("Fetching auth tokens...")
    try:
        csrf_token, session_id = await fetch_tokens(cookies, storage_path=storage_path)
    except ValueError as e:
        print(f"ERROR: {scrub_secrets(e)}", file=sys.stderr)
        sys.exit(2)
    except httpx.HTTPError as e:
        # ``httpx`` exception strings can echo full request URLs including
        # ``f.sid=<session_id>`` query params, so scrub before logging.
        print(
            f"ERROR: Network error while fetching auth tokens: {scrub_secrets(e)}",
            file=sys.stderr,
        )
        sys.exit(2)
    auth = AuthTokens(
        cookies=cookies,
        csrf_token=csrf_token,
        session_id=session_id,
        storage_path=storage_path,
        authuser=get_authuser_for_storage(storage_path),
        account_email=get_account_email_for_storage(storage_path),
    )
    print(f"Auth OK (CSRF token length: {len(auth.csrf_token)})")
    print()

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            if full_mode:
                print("Creating temp resources for full testing...")
                temp_resources = await setup_temp_resources(client, auth, results)
                if temp_resources.notebook_id:
                    notebook_id = temp_resources.notebook_id
                print()

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
                    # Per-method live print of the error string runs BEFORE
                    # the summary scrub at the end of the script and BEFORE
                    # the workflow-level scrub on health-report.txt. Scrub
                    # at the live site too so the Actions log doesn't carry
                    # an unredacted copy in the streamed output.
                    line += f" - {scrub_secrets(result.error)}"
                print(line)

                if i < total and result.status != CheckStatus.SKIPPED:
                    await asyncio.sleep(CALL_DELAY)

            # Probe the streamed-chat orchestration RPC. It is not an
            # ``RPCMethod`` (it is a ``PATH_NOT_METHOD`` ``v1`` URL segment),
            # so it is checked separately from the method-ID loop above — but
            # its ``CheckResult`` flows through the same summary + exit-code
            # machinery so chat drift fails the canary like any other probe.
            chat_result = await check_chat_query(client, auth, notebook_id)
            results.append(chat_result)
            chat_icon = STATUS_ICONS[chat_result.status]
            chat_line = f"{chat_icon:8} {chat_result.method.name} ({chat_result.expected_id})"
            if chat_result.error and chat_result.status != CheckStatus.OK:
                chat_line += f" - {scrub_secrets(chat_result.error)}"
            print(chat_line)

            # Studio-customization cohort tripwire. Distinct from the RPC-drift
            # probes: GATED-null is the expected steady state (NOT a failure),
            # MIGRATED is a loud signal that the customization surface flipped and
            # the VideoStyle/format codes must be re-captured.
            cohort_status, cohort_detail = await check_customization_cohort(
                client, auth, notebook_id
            )
            print(
                "COHORT   sqTeoe customization choices: "
                f"{cohort_status.value} - {scrub_secrets(cohort_detail)}"
            )

        finally:
            if full_mode and temp_resources.notebook_id:
                print()
                print("Testing DELETE operations during cleanup...")
                await cleanup_temp_resources(client, auth, temp_resources, results)

    return results, cohort_status


# Substrings that mark an ERROR as a transient signal. Keep this list
# narrow on purpose: broadening it would mask real RPC drift.
#
# Currently classified as transient:
#   * ``HTTP 429`` and gRPC ``RESOURCE_EXHAUSTED`` — explicit rate-limit
#     signals from the backend.
#   * ``API rate limit`` — catches the decoder's user-displayable messages
#     raised as ``RateLimitError`` ("API rate limit exceeded..." and
#     "API rate limit or quota exceeded..."). These reach the canary via
#     the ``except RPCError`` parse-error branch in
#     ``test_rpc_method_with_data`` and were previously misclassified.
#   * ``ReadTimeout`` — ``httpx.ReadTimeout`` against Google's RPC
#     endpoints is almost always server-side slowness, not an RPC
#     contract change. It consistently passes on retry (see #1004 and
#     prior occurrences on 2026-05-20). Only the ``httpx.ReadTimeout``
#     class name is treated as transient — ``ConnectTimeout`` /
#     ``WriteTimeout`` / ``PoolTimeout`` stay non-transient because they
#     point at client- or network-side problems worth surfacing.
#
# Everything else (other timeouts, parse failures, unexpected HTTP
# status codes, schema mismatches) is still treated as a real failure
# so the nightly canary can flag silent breakage.
TRANSIENT_ERROR_MARKERS: tuple[str, ...] = (
    "HTTP 429",
    "RESOURCE_EXHAUSTED",
    "API rate limit",
    "ReadTimeout",
)


def is_transient_error(error_message: str | None) -> bool:
    """Return True if an ERROR result is a transient signal.

    Transient signals (filtered out of the auto-open path):
      * Rate-limit responses (HTTP 429, gRPC ``RESOURCE_EXHAUSTED``,
        decoder ``RateLimitError`` messages).
      * ``httpx.ReadTimeout`` against Google's RPC endpoints — these
        are server-side flakes that pass on retry (issue #1004).

    Anything else — other timeouts, parse failures, unexpected HTTP
    status codes — is treated as a real failure that warrants
    investigation.
    """
    if not error_message:
        return False
    return any(marker in error_message for marker in TRANSIENT_ERROR_MARKERS)


def partition_errors(
    results: list[CheckResult],
) -> tuple[list[CheckResult], list[CheckResult]]:
    """Split ERROR results into (non-transient, transient) lists."""
    non_transient: list[CheckResult] = []
    transient: list[CheckResult] = []
    for r in results:
        if r.status != CheckStatus.ERROR:
            continue
        if is_transient_error(r.error):
            transient.append(r)
        else:
            non_transient.append(r)
    return non_transient, transient


def compute_exit_code(
    counts: Counter[CheckStatus],
    non_transient_errors: list[CheckResult],
    cohort_status: CohortStatus = CohortStatus.UNKNOWN,
) -> int:
    """Compute the script exit code from result counts.

    Priority order (highest wins):
        1. MISMATCH  -> 1
        2. AUTH      -> 2 (signaled by the caller via sys.exit, never reached here)
        3. non-transient ERROR -> 3
        4. cohort MIGRATED -> 4 (sqTeoe tripwire flipped; loud but not RPC drift)
        5. OK        -> 0

    The cohort flip sits *below* the RPC-drift codes so a run that has both real
    drift AND a cohort flip still surfaces the higher-severity drift exit; the
    flip is reported in the summary either way.
    """
    if counts[CheckStatus.MISMATCH] > 0:
        return 1
    if non_transient_errors:
        return 3
    if cohort_status == CohortStatus.MIGRATED:
        return 4
    return 0


def print_summary(
    results: list[CheckResult],
    cohort_status: CohortStatus = CohortStatus.UNKNOWN,
) -> int:
    """Print summary and return exit code."""
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    counts = Counter(r.status for r in results)
    total = len(results)
    tested = total - counts[CheckStatus.SKIPPED]

    non_transient_errors, transient_errors = partition_errors(results)
    non_transient_count = len(non_transient_errors)
    transient_count = len(transient_errors)

    print(f"TESTED:   {tested}/{total} methods")
    print(f"OK:       {counts[CheckStatus.OK]}/{tested}")
    print(f"MISMATCH: {counts[CheckStatus.MISMATCH]}/{tested}")
    print(
        f"ERROR:    {counts[CheckStatus.ERROR]}/{tested} "
        f"(non-transient: {non_transient_count}, transient: {transient_count})"
    )

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

    # Print details for errors, split into non-transient (real failures)
    # and transient (rate-limit) buckets so the cron output is actionable.
    if non_transient_errors or transient_errors:
        print()
        print("ERROR DETAILS:")
        print("-" * 40)
        for r in non_transient_errors:
            # ``r.error`` is a free-form error string produced by the RPC
            # call paths; if the upstream library ever quotes a request
            # URL or cookie jar in its message, the workflow's later
            # file scrub catches it but the live Actions log would not.
            # Belt-and-braces: scrub at the print site too.
            print(f"  [non-transient] {r.method.name} ({r.expected_id}): {scrub_secrets(r.error)}")
        for r in transient_errors:
            print(f"  [transient]     {r.method.name} ({r.expected_id}): {scrub_secrets(r.error)}")
        print()

    # Cohort tripwire line. GATED-null is the expected steady state (a PASS, not
    # a failure); MIGRATED is the loud flip the canary exists to surface.
    print(f"COHORT:   sqTeoe customization choices = {cohort_status.value}")

    # Return exit code.
    # Priority: MISMATCH (1) > non-transient ERROR (3) > cohort MIGRATED (4) > OK (0).
    # AUTH (2) is signaled earlier via sys.exit(2) and never reaches here.
    exit_code = compute_exit_code(counts, non_transient_errors, cohort_status)

    if exit_code == 1:
        print("RESULT: FAIL - RPC ID mismatches detected")
        return 1
    if exit_code == 3:
        affected = ", ".join(r.method.name for r in non_transient_errors)
        print(f"RESULT: FAIL - non-transient ERROR detected in methods: {affected}")
        print("       These are real failures (not rate-limit transients).")
        return 3
    if exit_code == 4:
        print("RESULT: COHORT FLIP - sqTeoe returned non-null (studio customization migrated)")
        print("       Re-capture VideoStyle / format codes in src/notebooklm/rpc/types.py.")
        return 4
    if transient_errors:
        print("RESULT: PASS - Only transient errors observed (rate-limits / ReadTimeouts)")
        print("       Review ERROR DETAILS above for affected methods.")
        return 0
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

    results, cohort_status = asyncio.run(run_health_check(full_mode=args.full))
    return print_summary(results, cohort_status)


if __name__ == "__main__":
    sys.exit(main())
