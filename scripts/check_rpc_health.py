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
    5 - Stale frontend build label: the ``bl`` value pinned in
        ``_env.DEFAULT_BL`` trails the label the app shell actually serves by
        more than ``_env.BUILD_LABEL_STALE_AFTER_DAYS``. Ordinary week-to-week
        drift is a PASS; this fires only once the pin has gone unattended, and
        clears as soon as it is bumped (#2073).

Priority order when multiple statuses are present:
    MISMATCH (1) > AUTH (2) > non-transient ERROR (3) > cohort MIGRATED (4)
    > build label STALE (5) > OK (0)

Transient errors that still exit 0 are limited to rate-limit signals
(HTTP 429, gRPC ``RESOURCE_EXHAUSTED``, and the decoder's user-displayable
``API rate limit`` / quota messages raised as ``RateLimitError``) plus
``httpx.ReadTimeout`` against Google's RPC endpoints — those are almost
always server-side slowness, not an RPC contract change, and they
consistently pass on retry (see #1004). Everything else (parse failures,
unexpected HTTP status codes, schema mismatches) is still treated as a
real failure so the nightly canary can flag silent breakage.

Rebrand-host lane (``notebook.google.com``):
    A SEPARATE reporting lane answers "does the post-rebrand host serve RPC?".
    It NEVER contributes to the exit codes above and never lands in the
    ``CheckResult`` list — see :func:`probe_rebrand_host`. That separation is
    load-bearing: the main lane's non-transient-ERROR issue is deduped by
    title alone, so a rebrand probe folded into it would open one issue on the
    first failing night and then SUPPRESS every main-lane degradation issue after
    it. The lane reports a *state change* (for example, ``PRESENT -> ABSENT``)
    against the previous run's recorded state, under its own issue title.

    The lane answers two of the three migration sub-questions — batchexecute and
    ``GenerateFreeFormStreamed``. The third, ``/upload/_/`` (Scotty), is NOT
    probed here: this script exercises ``ADD_SOURCE_FILE`` as an RPC only and
    issues no upload POST anywhere, so answering it needs a manual authenticated
    capture. That is stated in the report output too, so nobody reads a silent
    lane as a green one.

Environment variables:
    NOTEBOOKLM_AUTH_JSON - Playwright storage state JSON (optional; the
        profile's storage_state.json is used when unset, which is what CI does)
    NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID - Notebook ID for read operations
    NOTEBOOKLM_GENERATION_NOTEBOOK_ID - Notebook ID for write operations
    NOTEBOOKLM_RPC_DELAY - Delay between RPC calls in seconds (default: 1.0)
    NOTEBOOKLM_BASE_URL - Base host override (``--base-url`` takes precedence)

Usage:
    python scripts/check_rpc_health.py          # Quick mode (skip destructive)
    python scripts/check_rpc_health.py --full   # Full mode (create temp notebook)
    # Point the WHOLE run at the rebrand host (manual investigation only — the
    # nightly stays on the default host so the main signal exercises that host):
    python scripts/check_rpc_health.py --base-url https://notebook.google.com
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
from urllib.parse import urlencode, urlparse
from uuid import uuid4

import httpx

from notebooklm._artifact.payloads import build_retry_artifact_params
from notebooklm._auth.tokens import LoadPolicy, _load_stored_auth
from notebooklm._chat.wire import (
    build_streaming_chat_request,
    parse_streaming_chat_response,
)
from notebooklm._env import (
    BUILD_LABEL_STALE_AFTER_DAYS,
    DEFAULT_BL,
    PERSONAL_APP_HOSTS,
    PERSONAL_BASE_HOST,
    build_label_days_behind,
    extract_build_label,
    get_base_url,
    get_default_bl,
    get_default_language,
)
from notebooklm._logging import scrub_secrets
from notebooklm._notebooks import build_create_notebook_params
from notebooklm.auth import AuthTokens
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


class RebrandProbeStatus(str, Enum):
    """Verdict for one rebrand-host (``notebook.google.com``) capability probe.

    ``PRESENT`` / ``ABSENT`` are *recorded* states — they are what the state
    file remembers and what a transition is computed against. Every other
    member is explicitly NOT recorded (see :data:`RECORDED_REBRAND_STATUSES`):
    it carries the previous state forward, so a flake can never manufacture a
    state change, and never suppress a real one later.

    ``UNKNOWN`` is server-side or transport noise (rate limit, 5xx, connection
    failure). ``UNAUTHENTICATED`` is the answer "we were not signed in for that
    request" — an HTTP 401/403 or a redirect into Google's sign-in flow. That
    is a statement about this run's credentials, NOT about the endpoint, so it
    must never overwrite a recorded PRESENT: measured 2026-08-04 across five
    live profiles, the session is gated by the accounts-scoped ``LSID`` family
    (domain-correct for both hosts), so an auth failure here means a broken or
    partial profile, never a missing endpoint. ``NOT_PROBED`` is the
    ``/upload/_/`` sub-question, which needs a manual capture.
    """

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    UNKNOWN = "UNKNOWN"
    NOT_PROBED = "NOT_PROBED"


# The only verdicts written to the state file and compared for transitions.
# Everything else carries the previous value forward untouched.
RECORDED_REBRAND_STATUSES: frozenset[RebrandProbeStatus] = frozenset(
    {RebrandProbeStatus.PRESENT, RebrandProbeStatus.ABSENT}
)


# Studio-customization cohort tripwire RPC. NOT in ``RPCMethod`` (it is gated off
# for our consumer cohort, so there is no typed/public path), called by raw id.
_CUSTOMIZATION_CHOICES_RPC_ID = "sqTeoe"


class BuildLabelStatus(str, Enum):
    """Verdict for the pinned-vs-served frontend build label (``bl``).

    ``CURRENT`` — the app shell serves exactly what ``_env.DEFAULT_BL`` pins.
    ``DRIFTED`` — it serves something else, but the pin is inside the freshness
    window (``_env.BUILD_LABEL_STALE_AFTER_DAYS``). This is the ordinary steady
    state: Google ships a new build roughly weekly and the pin is not expected to
    chase every one, so DRIFTED is a PASS.
    ``STALE`` — the pin trails the served label by more than that window. The
    constant has been left unattended (#2073: it went five months and 154 label-days
    without a re-check) and should be bumped.
    ``UNKNOWN`` — no label could be read (not signed in, transport failure,
    unrecognized shell). No conclusion is drawn and nothing is alarmed.
    """

    CURRENT = "CURRENT"
    DRIFTED = "DRIFTED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


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


def describe_auth_source(storage_path: Path | None) -> str:
    """Name where credentials were loaded from, for diagnostics."""
    return "NOTEBOOKLM_AUTH_JSON env var" if storage_path is None else str(storage_path)


def describe_cookie_scopes(auth: AuthTokens) -> str:
    """Summarise the jar as ``name@domain`` pairs — names and domains only.

    #2019 was a cookie-*scoping* failure that produced a generic "authentication
    expired" line, so the nightly log carried nothing to diagnose it with. Cookie
    names and domains are not secrets (values are never printed), and having them
    in the report makes the next scoping regression readable from the log alone.
    """
    jar = auth.cookie_jar
    if jar is None:
        return "<no jar>"
    return ", ".join(sorted(f"{cookie.name}@{cookie.domain}" for cookie in jar.jar))


async def load_auth(storage_path: Path | None) -> AuthTokens:
    """Load auth from environment or storage file, preserving cookie domains.

    Routes through the storage owner underlying the managed client loader,
    which handles:

    - the profile storage file (what CI and local dev both use) and the
      short-lived ``NOTEBOOKLM_AUTH_JSON`` env var, via ``storage_path``
      resolved by :func:`resolve_storage_path`
    - cookie-domain filtering *and* per-cookie domain preservation
    - authuser / account-email routing and the CSRF + session token fetch

    Domain preservation is load-bearing (issue #2019). The previous
    ``load_auth_from_storage()`` + ``fetch_tokens()`` pairing flattened the jar
    to ``name -> value``; under ``NOTEBOOKLM_AUTH_JSON`` there is no file to
    reload from, so ``build_cookie_jar`` re-pinned every cookie to
    ``.google.com``. Host-scoped cookies (``OSID`` / ``__Secure-OSID`` on the
    NotebookLM host, ``LSID`` on ``accounts.google.com``) were then broadcast to
    every ``*.google.com`` host. After Google's ``notebook.google.com`` cutover
    that collides with the freshly minted host cookie, dead-ends the sign-in
    redirect chain on ``accounts.google.com/CookieMismatch``, and made this
    script report "Authentication expired" against perfectly valid credentials.

    Only the three failure classes below are mapped to the AUTH exit code.
    ``RuntimeError`` (a failed ``NOTEBOOKLM_REFRESH_CMD``) and non-missing-file
    ``OSError`` still propagate as a traceback, exactly as they did through the
    old ``fetch_tokens`` call.
    """
    try:
        loaded = await _load_stored_auth(
            path=storage_path,
            profile=None,
            policy=LoadPolicy(),
            auth_type=AuthTokens,
        )
        return loaded.auth
    except FileNotFoundError as e:
        # ``e`` names the storage path that was actually checked — under
        # profiles that is the only way to see which one the script resolved.
        print(
            f"ERROR: No authentication found: {scrub_secrets(e)}\n"
            "Set NOTEBOOKLM_AUTH_JSON env var or run 'notebooklm login'",
            file=sys.stderr,
        )
        sys.exit(2)
    except ValueError as e:
        # Name the auth source. The file branch of ``_load_storage_state``
        # re-raises a bare ``json`` error carrying no context at all, so a
        # corrupt storage_state.json would otherwise print only
        # "Expecting value: line 1 column 1 (char 0)".
        print(
            f"ERROR: {scrub_secrets(e)}\nAuth source: {describe_auth_source(storage_path)}",
            file=sys.stderr,
        )
        sys.exit(2)
    except httpx.HTTPError as e:
        # ``httpx`` exception strings can echo full request URLs including
        # ``f.sid=<session_id>`` query params, so scrub before logging.
        print(
            f"ERROR: Network error while fetching auth tokens: {scrub_secrets(e)}",
            file=sys.stderr,
        )
        sys.exit(2)


def resolve_storage_path() -> Path | None:
    """Return the file-based auth path, or None when NOTEBOOKLM_AUTH_JSON is used."""
    if "NOTEBOOKLM_AUTH_JSON" in os.environ:
        return None
    return get_storage_path()


def build_probe_client(auth: AuthTokens) -> httpx.AsyncClient:
    """Build the HTTP client every probe shares, with domain-scoped cookies.

    Extracted as a named seam so a test can assert that the *shipped* client
    carries the jar. Each probe used to hand-build a ``Cookie:`` header from
    ``auth.cookie_header``, which collapses every domain into one name→value
    slot: the accounts-scoped ``LSID`` reached the app host on every request,
    and when a name existed on two hosts the survivor was arbitrary (issue
    #2054). Letting httpx select per RFC 6265 from ``auth.cookie_jar`` fixes
    both.

    Note ``httpx`` **copies** the jar rather than sharing it, so cookies the
    service rotates during a run (``SIDCC``, ``__Secure-1PSIDCC``, …) land on
    the client's private copy and are discarded at exit. Not a regression — the
    script previously sent a fixed header and kept no jar at all — but it does
    mean a probe run cannot persist a rotation.
    """
    return httpx.AsyncClient(timeout=30.0, cookies=auth.cookie_jar)


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

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

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
    # (1..10) is set to the default 4; an empty source-id list scopes to all of
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
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
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


# ---------------------------------------------------------------------------
# Rebrand-host lane (notebook.google.com) — reported separately, exit-code-free
# ---------------------------------------------------------------------------

# Capability keys. Stable strings: they are the state file's schema and the
# dedup-relevant text in the state-change line, so renaming one resets history.
REBRAND_BATCHEXECUTE = "batchexecute"
REBRAND_CHAT = "chat"
REBRAND_UPLOAD = "upload"

# The last availability evidence acknowledged in the repository, used as the
# previous state on the first run (or whenever the cached state is unavailable).
# Keep each capability independent: the authenticated nightly probe at 36221e0
# observed LIST_NOTEBOOKS over batchexecute (#2077) and parsed a streamed-chat
# response (#2078) on the rebrand host. The entries remain independent so later
# evidence can advance one capability without changing the other.
REBRAND_BASELINE_STATE: dict[str, str] = {
    REBRAND_BATCHEXECUTE: RebrandProbeStatus.PRESENT.value,
    REBRAND_CHAT: RebrandProbeStatus.PRESENT.value,
}

# Bumped when the file shape changes or the checked-in evidence baseline
# advances. A mismatch is treated as "no usable previous state" and falls back
# to REBRAND_BASELINE_STATE; version 2 prevents a pre-#2077/#2078 cached ABSENT
# document from overriding the newly acknowledged PRESENT observations.
REBRAND_STATE_VERSION = 2


@dataclass(frozen=True)
class RebrandProbe:
    """One rebrand-host capability observation."""

    capability: str
    status: RebrandProbeStatus
    detail: str


def rebrand_probe_host() -> str:
    """Return the host the rebrand lane probes.

    Always the post-rebrand host, regardless of what ``--base-url`` /
    ``NOTEBOOKLM_BASE_URL`` selected for the main lane: the lane's question is
    specifically "does *that* host serve RPC?".

    .. note::

       Since #2067 moved the default onto this same host, the lane duplicates
       the main lane instead of adding a signal. That is deliberate for one
       release: re-aiming it at :data:`~notebooklm._env.PERSONAL_LEGACY_HOST`
       turns it into a *rollback-availability* monitor (ADR-0028, as amended),
       which is the useful question post-flip -- but the nightly compares
       against cached state (``rpc-health.yml``), so flipping the target
       without bumping the state namespace would read the old host's history
       as this host's and fire a spurious transition alert. Tracked as the
       follow-up to this flip; kept redundant-but-honest until then.
    """
    return PERSONAL_BASE_HOST


def retarget_url(url: str, host: str) -> str:
    """Return ``url`` with its host replaced by ``host``, path/query intact.

    Derived from the library's own URL builders rather than re-spelling the
    endpoint paths here, so a path change in ``rpc/types.py`` cannot leave this
    lane probing a stale endpoint and reporting a false ABSENT.
    """
    return str(httpx.URL(url).copy_with(host=host))


# HTTP statuses that answer "you are not signed in", not "the endpoint is gone".
_REBRAND_AUTH_STATUS_CODES = frozenset({401, 403, 407})

# A ``Location`` landing on one of these is Google's sign-in flow. Same reading
# as a 401: the host bounced an unauthenticated request, which says nothing
# about whether it serves the RPC endpoint to a signed-in one.
_LOGIN_REDIRECT_HOSTS = frozenset({"accounts.google.com", "accounts.youtube.com"})
# ``login`` subsumes the older ``servicelogin`` entry and additionally catches the
# app's own bounce, ``notebook.google.com/login?continue=…``, which is what an
# unauthenticated fetch of the app shell actually receives (measured 2026-08-04).
# Without it that bounce read as ABSENT — "the endpoint is gone" — on a lane whose
# whole point (#2062) is that a signed-out run must not overwrite a recorded state.
_LOGIN_REDIRECT_PATH_MARKERS = ("login", "signin", "accountchooser", "oauth")


def is_login_redirect(location: str | None) -> bool:
    """Return whether a redirect ``Location`` points into Google's sign-in flow.

    Handles the relative form (``/ServiceLogin?...``) as well as the absolute
    one, since either is a legal ``Location`` per RFC 7231.
    """
    if not location:
        return False
    try:
        url = httpx.URL(location)
    except (httpx.InvalidURL, ValueError, TypeError):
        return False
    if url.host in _LOGIN_REDIRECT_HOSTS:
        return True
    path = url.path.lower()
    return any(marker in path for marker in _LOGIN_REDIRECT_PATH_MARKERS)


def classify_rebrand_status(
    status_code: int, location: str | None = None
) -> tuple[RebrandProbeStatus, str] | None:
    """Map a non-200 HTTP status to a lane verdict, or None to keep inspecting.

    Rate limits and 5xx are ``UNKNOWN`` (server-side noise — never a state
    change). 401/403 and redirects into the sign-in flow are
    ``UNAUTHENTICATED``: they establish that *this request* was not
    authenticated, which is not evidence the endpoint is absent, so they must
    not overwrite a recorded PRESENT (#2062). Everything else non-200 is
    ``ABSENT``: the host answered, and what it answered was not an RPC
    response.
    """
    if status_code == 200:
        return None
    if status_code == 429 or status_code >= 500:
        return RebrandProbeStatus.UNKNOWN, f"HTTP {status_code} (transient — no conclusion drawn)"
    if status_code in _REBRAND_AUTH_STATUS_CODES:
        return (
            RebrandProbeStatus.UNAUTHENTICATED,
            f"HTTP {status_code} (not authenticated for this host — no conclusion drawn)",
        )
    if 300 <= status_code < 400:
        if is_login_redirect(location):
            return (
                RebrandProbeStatus.UNAUTHENTICATED,
                f"HTTP {status_code} redirect to sign-in — no conclusion drawn",
            )
        return RebrandProbeStatus.ABSENT, f"HTTP {status_code} (redirect, not an RPC response)"
    return RebrandProbeStatus.ABSENT, f"HTTP {status_code}"


async def probe_rebrand_batchexecute(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    host: str,
) -> RebrandProbe:
    """Ask whether ``host`` serves ``/_/LabsTailwindUi/data/batchexecute``.

    Issues one read-only ``LIST_NOTEBOOKS`` call and requires the response to
    echo the method's own RPC id back — the same liveness assertion the main
    method-ID loop makes. A 200 that is not a batchexecute envelope (the app
    shell, a sign-in interstitial) is ``ABSENT``, not a parse error.
    """
    method = RPCMethod.LIST_NOTEBOOKS
    query_params = {
        "rpcids": method.value,
        "source-path": "/",
        "f.sid": auth.session_id,
        "hl": get_default_language(),
        "rt": "c",
    }
    if auth.account_email or auth.authuser:
        query_params["authuser"] = auth.account_route
    url = f"{retarget_url(get_batchexecute_url(), host)}?{urlencode(query_params)}"
    body = build_request_body(encode_rpc_request(method, []), auth.csrf_token)
    # No ``Cookie`` header: the shared client from ``build_probe_client`` carries
    # ``auth.cookie_jar``, so httpx selects per RFC 6265 for whichever host this
    # probe is retargeted at (#2054).
    #
    # That means a profile whose ``OSID``/``__Secure-OSID`` are scoped to the
    # legacy host sends *no* host-scoped binding to the rebrand host. Measured
    # 2026-08-04 across five live profiles (two of them pre-cutover, binding on
    # the legacy host only): ``LIST_NOTEBOOKS`` succeeds on both hosts anyway.
    # The credential that actually gates the session is the accounts-scoped
    # ``LSID`` family, which is domain-correct for both. So an ABSENT verdict
    # here means the endpoint did not answer — not that routing starved it.
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        response = await client.post(url, content=body, headers=headers)
    except httpx.RequestError as e:
        return RebrandProbe(
            REBRAND_BATCHEXECUTE,
            RebrandProbeStatus.UNKNOWN,
            scrub_secrets(str(e) or type(e).__name__),
        )

    verdict = classify_rebrand_status(response.status_code, response.headers.get("location"))
    if verdict is not None:
        return RebrandProbe(REBRAND_BATCHEXECUTE, verdict[0], verdict[1])

    try:
        found_ids = collect_rpc_ids(parse_chunked_response(strip_anti_xssi(response.text)))
    except (RPCError, json.JSONDecodeError, ValueError, IndexError, TypeError):
        # ``RPCError`` is what the decoder raises for a body it cannot read as
        # batchexecute at all (the HTML app shell hits exactly this). In THIS
        # lane that is the answer — "the host served something else" — not the
        # parse/drift signal it would be on the exit-coded main lane.
        return RebrandProbe(
            REBRAND_BATCHEXECUTE,
            RebrandProbeStatus.ABSENT,
            "HTTP 200 but the body is not a batchexecute envelope",
        )
    if method.value in found_ids:
        return RebrandProbe(
            REBRAND_BATCHEXECUTE,
            RebrandProbeStatus.PRESENT,
            f"HTTP 200, {method.name} ({method.value}) echoed back",
        )
    return RebrandProbe(
        REBRAND_BATCHEXECUTE,
        RebrandProbeStatus.ABSENT,
        f"HTTP 200 but {method.value} was not echoed (ids: {found_ids})",
    )


async def probe_rebrand_chat(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    host: str,
    notebook_id: str | None,
) -> RebrandProbe:
    """Ask whether ``host`` serves ``GenerateFreeFormStreamed``.

    Mirrors :func:`check_chat_query`'s classification — a recognized frame
    (answer *or* server ``"er"`` frame) proves the endpoint is live — but lands
    in the rebrand lane instead of the exit-coded ``CheckResult`` list.
    """
    if not notebook_id:
        return RebrandProbe(
            REBRAND_CHAT,
            RebrandProbeStatus.UNKNOWN,
            "No notebook ID provided (chat probe needs one)",
        )

    url, body, extra_headers = build_streaming_chat_request(
        snapshot=auth,
        notebook_id=notebook_id,
        question="RPC health check ping (rebrand host).",
        source_ids=[],
        conversation_history=None,
        conversation_id=None,
        reqid=int(uuid4().int % 1_000_000),
    )
    # No ``Cookie`` header — the shared client carries the jar and httpx routes
    # per RFC 6265. See the rebrand batchexecute probe above for why routed
    # selection does not starve this probe (#2054).
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        **extra_headers,
    }

    try:
        response = await client.post(retarget_url(url, host), content=body, headers=headers)
    except httpx.RequestError as e:
        return RebrandProbe(
            REBRAND_CHAT,
            RebrandProbeStatus.UNKNOWN,
            scrub_secrets(str(e) or type(e).__name__),
        )

    verdict = classify_rebrand_status(response.status_code, response.headers.get("location"))
    if verdict is not None:
        return RebrandProbe(REBRAND_CHAT, verdict[0], verdict[1])

    try:
        parse_streaming_chat_response(response.text)
    except (ChatResponseParseError, DecodingError, json.JSONDecodeError):
        # Order matters: ``ChatResponseParseError`` is a ``ChatError`` subclass,
        # so the "unreadable body" arm MUST precede the "recognized frame" arm
        # below — the same ordering ``check_chat_query`` uses. Reversed, every
        # unparseable body would be scored PRESENT.
        return RebrandProbe(
            REBRAND_CHAT,
            RebrandProbeStatus.ABSENT,
            "HTTP 200 but the body is not a streamed-chat response",
        )
    except ChatError as e:
        return RebrandProbe(
            REBRAND_CHAT,
            RebrandProbeStatus.PRESENT,
            f"HTTP 200, recognized server frame: {scrub_secrets(e)}",
        )
    return RebrandProbe(REBRAND_CHAT, RebrandProbeStatus.PRESENT, "HTTP 200, stream parsed")


def rebrand_upload_probe() -> RebrandProbe:
    """Record the ``/upload/_/`` sub-question as unanswered — deliberately.

    This script exercises ``ADD_SOURCE_FILE`` as an RPC only ("registers file
    source intent (no actual upload needed)") and issues no Scotty POST
    anywhere, so there is nothing here to derive an upload verdict from.
    Emitting ``NOT_PROBED`` keeps the gap visible in every report instead of
    letting a silent lane read as a green one.
    """
    return RebrandProbe(
        REBRAND_UPLOAD,
        RebrandProbeStatus.NOT_PROBED,
        "no upload POST is issued by this script — needs a manual authenticated capture",
    )


async def probe_rebrand_host(
    client: httpx.AsyncClient,
    auth: AuthTokens,
    notebook_id: str | None,
    host: str | None = None,
) -> list[RebrandProbe]:
    """Run the rebrand-host lane and return its probes.

    Deliberately returns ``RebrandProbe`` objects, NOT ``CheckResult`` objects:
    nothing here may reach ``partition_errors`` / ``compute_exit_code``, because
    the main lane's issue is deduped by title alone and a permanently-failing
    rebrand probe folded into it would suppress every subsequent
    main-lane degradation issue.

    Runs LAST in the check, and paced like the method loop, so it cannot push
    the account into a rate limit that would then be attributed to a main-lane
    probe. When this lane was introduced, it deliberately presented the
    project's CI credentials to the rebrand host for the first time; both hosts
    are Google's and are origins of the same app.
    """
    target = host or rebrand_probe_host()
    await asyncio.sleep(CALL_DELAY)
    batchexecute = await probe_rebrand_batchexecute(client, auth, target)
    await asyncio.sleep(CALL_DELAY)
    chat = await probe_rebrand_chat(client, auth, target, notebook_id)
    return [batchexecute, chat, rebrand_upload_probe()]


def load_rebrand_state(path: Path | None) -> dict[str, str]:
    """Return the previously recorded state or checked-in acknowledged baseline.

    Any problem — no path, no file, unreadable, wrong schema version — degrades
    to :data:`REBRAND_BASELINE_STATE`. That checked-in fallback records the last
    acknowledged observation per capability. A missing cache therefore does not
    replay a transition acknowledged by this checkout, while a contrary live
    observation is still reported as a new transition.
    """
    if path is None or not path.exists():
        return dict(REBRAND_BASELINE_STATE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(REBRAND_BASELINE_STATE)
    if not isinstance(raw, dict) or raw.get("version") != REBRAND_STATE_VERSION:
        return dict(REBRAND_BASELINE_STATE)
    recorded = raw.get("state")
    if not isinstance(recorded, dict):
        return dict(REBRAND_BASELINE_STATE)
    merged = dict(REBRAND_BASELINE_STATE)
    recorded_statuses = {status.value for status in RECORDED_REBRAND_STATUSES}
    for key in REBRAND_BASELINE_STATE:
        value = recorded.get(key)
        if isinstance(value, str) and value in recorded_statuses:
            merged[key] = value
    return merged


def build_rebrand_state(
    host: str,
    probes: list[RebrandProbe],
    previous: dict[str, str],
    run_id: str | None = None,
) -> dict[str, Any]:
    """Fold ``probes`` onto ``previous`` and describe the transitions.

    Only PRESENT/ABSENT observations update the recorded state; every other
    verdict (UNKNOWN, UNAUTHENTICATED, NOT_PROBED) carries the previous value
    forward, so a rate-limited or signed-out night neither fires an alarm nor
    erases yesterday's answer.

    ``run_id`` stamps the document with the identity of the run that produced
    it. Consumers (the workflow's "Detect rebrand-host state change" step) match
    it against their own run id, so a state file restored from cache but never
    rewritten — the health script exiting early, say — can never be read as this
    run's result. See ``.github/workflows/rpc-health.yml`` (#2062).
    """
    state = dict(previous)
    transitions: list[dict[str, str]] = []
    for probe in probes:
        if probe.status not in RECORDED_REBRAND_STATUSES:
            continue
        before = state.get(probe.capability)
        if before != probe.status.value:
            transitions.append(
                {
                    "capability": probe.capability,
                    "from": before or RebrandProbeStatus.UNKNOWN.value,
                    "to": probe.status.value,
                    "detail": probe.detail,
                }
            )
        state[probe.capability] = probe.status.value
    return {
        "version": REBRAND_STATE_VERSION,
        "host": host,
        "run_id": run_id,
        "state": state,
        "observed": {p.capability: p.status.value for p in probes},
        "transitions": transitions,
        "changed": bool(transitions),
    }


def write_rebrand_state(path: Path | None, state: dict[str, Any]) -> None:
    """Persist the rebrand state, tolerating an unwritable path.

    The lane is advisory: a failed write must not take the canary down, so the
    failure is reported on stdout and the run continues.
    """
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as e:
        print(f"REBRAND  WARNING: could not write state file: {scrub_secrets(e)}")


def format_rebrand_lane(state: dict[str, Any], probes: list[RebrandProbe]) -> list[str]:
    """Render the rebrand lane's report block as printable lines."""
    lines = [f"REBRAND  host: {state['host']} (own lane — never affects the exit code)"]
    for probe in probes:
        lines.append(
            f"REBRAND  {probe.capability:13} {probe.status.value:15} - {scrub_secrets(probe.detail)}"
        )
    for transition in state["transitions"]:
        lines.append(
            f"REBRAND  STATE CHANGE: {transition['capability']}: "
            f"{transition['from']}->{transition['to']}"
        )
    if not state["changed"]:
        lines.append("REBRAND  no state change since the previous recorded run")
    if any(p.status is RebrandProbeStatus.UNAUTHENTICATED for p in probes):
        lines.append(
            "REBRAND  NOTE: an UNAUTHENTICATED probe says this run's credentials "
            "were rejected, NOT that the endpoint is gone — the previous recorded "
            "state was carried forward. Check the CI profile."
        )
    lines.append(
        "REBRAND  NOTE: an ABSENT verdict means 'not observed from this run's "
        "credentials', not 'unsupported'."
    )
    return lines


# ---------------------------------------------------------------------------
# Build-label lane — is ``_env.DEFAULT_BL`` still close to what Google serves?
# ---------------------------------------------------------------------------

# How many hops the app-shell fetch will follow. Measured 2026-08-04: the default
# host (``notebook.google.com`` since #2067) answers 200 directly, and the legacy
# host answers 302 to it — so a run pointed at the rollback host takes exactly one
# hop. Two is one spare; anything more is a redirect loop, not a shell.
_SHELL_MAX_HOPS = 2


@dataclass(frozen=True)
class BuildLabelProbe:
    """One pinned-vs-served build-label observation."""

    status: BuildLabelStatus
    detail: str
    pinned: str
    served: str | None = None
    days_behind: int | None = None


async def fetch_app_shell(client: httpx.AsyncClient, url: str) -> tuple[str | None, str]:
    """GET the app shell at ``url``, following only app-host hops.

    Returns ``(html, detail)``; ``html`` is ``None`` when no shell was reached,
    and ``detail`` then says why.

    Redirects are followed by hand rather than with ``follow_redirects=True`` so
    this lane never carries the session jar somewhere it did not intend to go: a
    hop is followed only when it lands on a personal app host at the site root.
    A sign-in bounce (including ``notebook.google.com/login?continue=…``, which an
    unauthenticated fetch gets) therefore ends the walk with no observation —
    "this run was not signed in" is not evidence about the build label.
    """
    for _ in range(_SHELL_MAX_HOPS + 1):
        try:
            response = await client.get(url)
        except httpx.RequestError as e:
            return None, scrub_secrets(str(e) or type(e).__name__)
        if response.status_code == 200:
            return response.text, ""
        if not 300 <= response.status_code < 400:
            return None, f"HTTP {response.status_code}"
        location = response.headers.get("location")
        if is_login_redirect(location):
            return None, f"HTTP {response.status_code} redirect to sign-in (not signed in)"
        if not location:
            return None, f"HTTP {response.status_code} with no Location header"
        target = httpx.URL(url).join(location)
        # ``https`` is checked alongside host and path: a downgrade to ``http``
        # would put this run's jar on the wire in cleartext for every cookie not
        # marked Secure. Refusing to follow is the only safe reading of it, and
        # costs nothing — the app has never served the shell over cleartext.
        if (
            target.scheme != "https"
            or target.host not in PERSONAL_APP_HOSTS
            or target.path not in ("", "/")
        ):
            return None, (
                f"HTTP {response.status_code} redirect to "
                f"{scrub_secrets(str(target.copy_with(query=None)))} (not the app shell)"
            )
        url = str(target)
    return None, f"more than {_SHELL_MAX_HOPS} redirects"


def classify_build_label(served: str | None, detail: str) -> BuildLabelProbe:
    """Score the served label against the pinned :data:`DEFAULT_BL`.

    Pure, so the staleness policy is testable without a live shell. ``served``
    is ``None`` when nothing was read; ``detail`` carries the reason.

    Note what this does NOT do: it never consults the wall clock. The verdict is
    a delta between two label dates, so it depends only on what Google served —
    a replayed or delayed run cannot age into an alarm on its own.
    """
    if served is None:
        return BuildLabelProbe(
            BuildLabelStatus.UNKNOWN,
            detail or "no build label found in the response",
            DEFAULT_BL,
        )
    if served == DEFAULT_BL:
        return BuildLabelProbe(
            BuildLabelStatus.CURRENT, "pin matches served", DEFAULT_BL, served, 0
        )

    days = build_label_days_behind(DEFAULT_BL, served)
    if days is None:
        return BuildLabelProbe(
            BuildLabelStatus.UNKNOWN,
            f"served {served} but one of the labels is not datable",
            DEFAULT_BL,
            served,
        )
    if days > BUILD_LABEL_STALE_AFTER_DAYS:
        return BuildLabelProbe(
            BuildLabelStatus.STALE,
            f"pin is {days} label-days behind {served} (limit {BUILD_LABEL_STALE_AFTER_DAYS})",
            DEFAULT_BL,
            served,
            days,
        )
    # Negative days means the pin is *ahead* of the served shell — an older
    # cohort, not staleness — so it gets its own wording rather than being
    # reported as "N days behind, within the window".
    if days < 0:
        drift = f"pin is {-days} label-days AHEAD of {served} (older cohort — not stale)"
    else:
        drift = (
            f"pin is {days} label-days behind {served} "
            f"(within the {BUILD_LABEL_STALE_AFTER_DAYS}-day window)"
        )
    return BuildLabelProbe(BuildLabelStatus.DRIFTED, drift, DEFAULT_BL, served, days)


async def check_build_label(client: httpx.AsyncClient) -> BuildLabelProbe:
    """Read the served build label off the app shell and score the pin against it.

    Exists because ``bl`` is a pinned constant with no other guard: cassettes
    replay whatever was recorded, so the entire offline suite passes no matter how
    old the pin gets, and the streaming endpoint accepts arbitrary labels — a live
    A/B on 2026-08-04 got a complete cited answer from the pinned label, the served
    label, and a fabricated 1970 one alike. Nothing fails until Google decides
    otherwise, which is exactly why the gap needs a lane of its own (#2073).

    **Never raises.** ``fetch_app_shell`` already absorbs transport errors, but
    this lane runs immediately before the rebrand-host lane, so anything it let
    escape would skip that lane and lose the night's rebrand observation
    entirely. The lowest-severity lane in the script must not be able to do that
    to a higher-severity one, so an unexpected failure degrades to UNKNOWN —
    which alarms nothing — rather than propagating.
    """
    try:
        html, detail = await fetch_app_shell(client, f"{get_base_url()}/")
        if html is None:
            return classify_build_label(None, detail)
        return classify_build_label(extract_build_label(html), detail)
    except Exception as e:  # noqa: BLE001 - see "Never raises" above
        return classify_build_label(None, f"lane failed: {scrub_secrets(e) or type(e).__name__}")


def format_build_label_lane(probe: BuildLabelProbe) -> list[str]:
    """Render the build-label lane's report block as printable lines."""
    lines = [
        f"BUILDLBL {probe.status.value:8} - {scrub_secrets(probe.detail)}",
        f"BUILDLBL pinned:   {probe.pinned} (src/notebooklm/_env.py)",
        f"BUILDLBL served:   {probe.served or '(not observed)'}",
    ]
    active = get_default_bl()
    if active != probe.pinned:
        lines.append(
            f"BUILDLBL NOTE: this run sent NOTEBOOKLM_BL={active} — the verdict "
            "above is about the committed pin, not the override."
        )
    if probe.status is BuildLabelStatus.STALE:
        lines.append(
            "BUILDLBL ACTION: re-capture the served label and bump DEFAULT_BL in "
            "src/notebooklm/_env.py."
        )
    return lines


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


async def run_health_check(
    full_mode: bool = False,
    *,
    base_url_source: str = "default",
    rebrand_state_path: Path | None = None,
    rebrand_previous_state_path: Path | None = None,
    rebrand_run_id: str | None = None,
) -> tuple[list[CheckResult], CohortStatus, dict[str, Any], BuildLabelProbe]:
    """Run health check on all RPC methods.

    Returns ``(results, cohort_status, rebrand_state, build_label)``: the
    per-method ``CheckResult`` list, the ``sqTeoe`` studio-customization cohort
    tripwire verdict (reported and exit-coded separately from RPC drift; see
    :class:`CohortStatus`), the rebrand-host lane's state document (reported
    separately and NEVER exit-coded; see :func:`probe_rebrand_host`), and the
    pinned-vs-served build-label verdict (its own exit code; see
    :func:`check_build_label`).

    ``base_url_source`` names where the selected base host came from
    (``default`` / ``NOTEBOOKLM_BASE_URL`` / ``--base-url``) so the report says
    which host was probed and why. ``rebrand_state_path`` is where the rebrand
    lane writes this run's state; ``rebrand_previous_state_path`` is where it
    reads the last run's from (defaulting to the same file, which is what a
    local invocation wants). CI keeps them apart so the file the workflow reads
    back can only be one this run wrote — see the "Detect rebrand-host state
    change" step. ``None`` compares against the checked-in acknowledged baseline
    and persists nothing. ``rebrand_run_id`` stamps the written document; see
    :func:`build_rebrand_state`.
    """
    notebook_id = os.environ.get("NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID") or os.environ.get(
        "NOTEBOOKLM_GENERATION_NOTEBOOK_ID"
    )

    if not notebook_id and not full_mode:
        print("WARNING: No notebook ID provided. Some methods will be skipped.", file=sys.stderr)

    results: list[CheckResult] = []
    temp_resources = TempResources()
    cohort_status = CohortStatus.UNKNOWN
    # A no-observation verdict, so an early failure yields a well-formed lane that
    # alarms about nothing rather than a missing attribute in the summary.
    build_label = classify_build_label(None, "lane did not run")
    # A no-observation document, so an early failure still yields a
    # well-formed (and change-free) rebrand lane rather than a KeyError.
    rebrand_state: dict[str, Any] = build_rebrand_state(
        rebrand_probe_host(), [], dict(REBRAND_BASELINE_STATE), rebrand_run_id
    )
    previous_state_path = rebrand_previous_state_path or rebrand_state_path

    storage_path = resolve_storage_path()
    print(f"Fetching auth tokens... (source: {describe_auth_source(storage_path)})")
    auth = await load_auth(storage_path)
    print(f"Auth OK (CSRF token length: {len(auth.csrf_token)})")
    print(f"  base host:     {httpx.URL(get_base_url()).host} (from: {base_url_source})")
    print(f"  account route: {auth.account_route}")
    print(f"  cookie scopes: {describe_cookie_scopes(auth)}")
    print(f"  rebrand probe: {rebrand_probe_host()} (separate lane)")
    print()

    async with build_probe_client(auth) as client:
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

            # Build-label lane. One plain GET of the app shell, scored against
            # the pinned ``_env.DEFAULT_BL``. CURRENT/DRIFTED/UNKNOWN are passes;
            # only STALE is exit-coded, and it clears itself the moment the
            # constant is bumped.
            await asyncio.sleep(CALL_DELAY)
            build_label = await check_build_label(client)
            for line in format_build_label_lane(build_label):
                print(line)

            # Rebrand-host lane. Runs LAST and in its own bucket: its probes
            # never join ``results``, so they cannot reach ``partition_errors``
            # / ``compute_exit_code`` and cannot poison the main lane's
            # title-deduped issue.
            rebrand_probes = await probe_rebrand_host(client, auth, notebook_id)
            rebrand_state = build_rebrand_state(
                rebrand_probe_host(),
                rebrand_probes,
                load_rebrand_state(previous_state_path),
                rebrand_run_id,
            )
            write_rebrand_state(rebrand_state_path, rebrand_state)
            for line in format_rebrand_lane(rebrand_state, rebrand_probes):
                print(line)

        finally:
            if full_mode and temp_resources.notebook_id:
                print()
                print("Testing DELETE operations during cleanup...")
                await cleanup_temp_resources(client, auth, temp_resources, results)

    return results, cohort_status, rebrand_state, build_label


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
    build_label_status: BuildLabelStatus = BuildLabelStatus.UNKNOWN,
) -> int:
    """Compute the script exit code from result counts.

    Priority order (highest wins):
        1. MISMATCH  -> 1
        2. AUTH      -> 2 (signaled by the caller via sys.exit, never reached here)
        3. non-transient ERROR -> 3
        4. cohort MIGRATED -> 4 (sqTeoe tripwire flipped; loud but not RPC drift)
        5. build label STALE -> 5 (DEFAULT_BL left unattended; hygiene, not an outage)
        6. OK        -> 0

    The cohort flip sits *below* the RPC-drift codes so a run that has both real
    drift AND a cohort flip still surfaces the higher-severity drift exit; the
    flip is reported in the summary either way. A stale build label sits lower
    still: it is a maintenance signal about a constant the server does not even
    validate today (#2073), so it must never mask a live breakage.

    The rebrand-host lane is deliberately absent from this signature. It has no
    exit code of its own and contributes to none of the above: the workflow's
    non-transient-ERROR issue is deduped by title alone, so a rebrand probe that
    failed every night would open one issue and then suppress every subsequent
    main-lane degradation issue.
    """
    if counts[CheckStatus.MISMATCH] > 0:
        return 1
    if non_transient_errors:
        return 3
    if cohort_status == CohortStatus.MIGRATED:
        return 4
    if build_label_status == BuildLabelStatus.STALE:
        return 5
    return 0


def print_summary(
    results: list[CheckResult],
    cohort_status: CohortStatus = CohortStatus.UNKNOWN,
    rebrand_state: dict[str, Any] | None = None,
    build_label: BuildLabelProbe | None = None,
) -> int:
    """Print summary and return exit code.

    ``rebrand_state`` is echoed for the operator only — it is never read by
    :func:`compute_exit_code`. ``build_label`` is echoed *and* exit-coded, but
    only for a STALE verdict; omitting it scores as UNKNOWN, which alarms nothing.
    """
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

    # Rebrand-host lane summary. Informational only — it is printed after the
    # exit-coded buckets and read by none of them. ``upload`` is always
    # NOT_PROBED: that sub-question needs a manual authenticated capture.
    if rebrand_state is not None:
        observed = rebrand_state.get("observed", {})
        rendered = " ".join(f"{k}={v}" for k, v in sorted(observed.items())) or "(not run)"
        print(f"REBRAND:  {rebrand_state.get('host', '?')} -> {rendered}")
        for transition in rebrand_state.get("transitions", []):
            print(
                f"          STATE CHANGE: {transition['capability']}: "
                f"{transition['from']}->{transition['to']}"
            )

    # Build-label lane. CURRENT/DRIFTED/UNKNOWN are passes; STALE means the pin
    # in ``_env.DEFAULT_BL`` has been left unattended past its freshness window.
    build_label_status = build_label.status if build_label else BuildLabelStatus.UNKNOWN
    if build_label is not None:
        print(
            f"BUILDLBL: {build_label_status.value} - "
            f"pinned {build_label.pinned}, served {build_label.served or '(not observed)'}"
        )

    # Return exit code.
    # Priority: MISMATCH (1) > non-transient ERROR (3) > cohort MIGRATED (4)
    # > build label STALE (5) > OK (0).
    # AUTH (2) is signaled earlier via sys.exit(2) and never reaches here.
    exit_code = compute_exit_code(counts, non_transient_errors, cohort_status, build_label_status)

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
    if exit_code == 5:
        detail = build_label.detail if build_label else ""
        print(f"RESULT: STALE BUILD LABEL - {scrub_secrets(detail)}")
        print("       Bump DEFAULT_BL in src/notebooklm/_env.py to the served label.")
        print("       Chat is not broken by this; the pin has simply gone unattended.")
        return 5
    if transient_errors:
        print("RESULT: PASS - Only transient errors observed (rate-limits / ReadTimeouts)")
        print("       Review ERROR DETAILS above for affected methods.")
        return 0
    print("RESULT: PASS - All tested RPC methods OK")
    return 0


def validate_base_url_override(raw: str) -> str:
    """Validate a ``--base-url`` value against the personal app hosts.

    Two gates, in order:

    1. the host must be one of :data:`notebooklm._env.PERSONAL_APP_HOSTS` — the
       enterprise host is a different tenant model and is not a health-check
       target, so it is rejected here even though the library accepts it;
    2. everything else (https, no port / path / query / credentials) is
       delegated to the library's own ``get_base_url`` validator, so this flag
       can never accept a URL the client itself would refuse.

    The environment is restored before returning: this function only *checks*.
    Call :func:`apply_base_url_override` to make the selection take effect.
    """
    candidate = (raw or "").strip().rstrip("/")
    allowed = ", ".join(sorted(PERSONAL_APP_HOSTS))
    if urlparse(candidate).hostname not in PERSONAL_APP_HOSTS:
        raise ValueError(f"--base-url must name one of the personal app hosts: {allowed}")
    previous = os.environ.get("NOTEBOOKLM_BASE_URL")
    os.environ["NOTEBOOKLM_BASE_URL"] = candidate
    try:
        return get_base_url()
    except ValueError as exc:
        # ``get_base_url`` phrases its message in terms of the env var; this is
        # the flag path, so re-frame it rather than telling the operator to fix
        # a variable they did not set.
        raise ValueError(f"--base-url is not a usable base URL: {exc}") from exc
    finally:
        if previous is None:
            os.environ.pop("NOTEBOOKLM_BASE_URL", None)
        else:
            os.environ["NOTEBOOKLM_BASE_URL"] = previous


def apply_base_url_override(base_url: str) -> None:
    """Make ``base_url`` the selection every lazy ``get_base_url()`` reader sees."""
    os.environ["NOTEBOOKLM_BASE_URL"] = base_url


def describe_base_url_source(explicit: bool) -> str:
    """Name where the selected base host came from, for the report header."""
    if explicit:
        return "--base-url"
    if os.environ.get("NOTEBOOKLM_BASE_URL", "").strip():
        return "NOTEBOOKLM_BASE_URL"
    return "default"


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
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "Point the whole run at a specific personal app host "
            f"({', '.join(sorted(PERSONAL_APP_HOSTS))}). Manual investigation only — "
            "the nightly run stays on the default host so the main signal exercises "
            "that host. Overrides NOTEBOOKLM_BASE_URL."
        ),
    )
    parser.add_argument(
        "--rebrand-state-file",
        default=None,
        type=Path,
        help=(
            "Path the rebrand-host lane writes this run's state to (and, unless "
            "--rebrand-previous-state-file is given, also reads the previous "
            "run's state from). Omitted: compare against the checked-in "
            "last-acknowledged baseline and persist nothing."
        ),
    )
    parser.add_argument(
        "--rebrand-previous-state-file",
        default=None,
        type=Path,
        help=(
            "Read the previous run's rebrand state from a DIFFERENT path than "
            "the one written. CI passes both so a state file restored from cache "
            "can never be mistaken for this run's output."
        ),
    )
    parser.add_argument(
        "--rebrand-run-id",
        default=None,
        help=(
            "Stamp the written rebrand state with this run's identity, so a "
            "consumer can verify the document it reads was produced by this run."
        ),
    )
    args = parser.parse_args()

    source = describe_base_url_source(args.base_url is not None)
    if args.base_url is not None:
        try:
            apply_base_url_override(validate_base_url_override(args.base_url))
        except ValueError as e:
            parser.error(str(e))

    mode_str = "FULL" if args.full else "QUICK"
    print(f"RPC Health Check ({mode_str} mode)")
    print("=" * 60)
    print()

    results, cohort_status, rebrand_state, build_label = asyncio.run(
        run_health_check(
            full_mode=args.full,
            base_url_source=source,
            rebrand_state_path=args.rebrand_state_file,
            rebrand_previous_state_path=args.rebrand_previous_state_file,
            rebrand_run_id=args.rebrand_run_id,
        )
    )
    return print_summary(results, cohort_status, rebrand_state, build_label)


if __name__ == "__main__":
    sys.exit(main())
