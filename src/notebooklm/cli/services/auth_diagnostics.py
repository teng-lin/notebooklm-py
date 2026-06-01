"""``auth check`` diagnostic service.

Owns the "validate the cookies on disk" probe and returns a structured
:class:`AuthCheckResult` to the caller. Rendering and exit-code policy
live in the command layer (see ``cli/session_cmd.py``).

Public surface
==============

* :class:`AuthCheckPlan` — frozen description of one ``auth check`` run.
* :class:`AuthCheckResult` — the structured outcome.
* :func:`run_auth_check` — async executor. Reads the auth source, runs
  every check up to the user's opt-in level (``--test`` for token fetch),
  and returns the typed result. The caller renders the result and
  decides the exit code.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .auth_source import AUTH_JSON_ENV_NAME, AuthSource, read_env_auth_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthCheckPlan:
    """One ``auth check`` invocation.

    Attributes:
        storage_path: Resolved storage_state.json path (the file the
            check will read when no env-var auth is active).
        profile: Active profile name (forwarded to the token-fetch path
            so SID/SAPISID extraction targets the right account).
        has_env_auth: ``True`` when env-supplied auth is active;
            short-circuits the file-read in favor of parsing the env var.
        has_home_env: ``True`` when ``NOTEBOOKLM_HOME`` is set; used in
            the ``auth_source`` display string.
        test_fetch: When ``True``, also exercise the token-fetch path
            (network round-trip). Off by default.
        json_output: When ``True``, signals the caller to render a JSON
            envelope and propagate non-zero exit on failure. Carried on
            the plan so the renderer (which lives in the command layer)
            can pick the right shape without re-resolving the flag.
    """

    storage_path: Path
    profile: str | None
    has_env_auth: bool
    has_home_env: bool
    test_fetch: bool
    json_output: bool


@dataclass
class AuthCheckResult:
    """Outcome of a single ``auth check`` run.

    The ``checks`` dict mirrors the legacy contract from
    ``cli/session_cmd.py``: each value is ``True`` (passed), ``False``
    (failed), or ``None`` (not tested — only valid for ``token_fetch``).

    ``details`` carries human-readable context that the renderer joins
    into the table / JSON envelope.
    """

    plan: AuthCheckPlan
    checks: dict[str, bool | None]
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        return all(v is True for v in self.checks.values() if v is not None)


def _make_initial_checks() -> dict[str, bool | None]:
    return {
        "storage_exists": False,
        "json_valid": False,
        "cookies_present": False,
        "sid_cookie": False,
        "token_fetch": None,
    }


def plan_from_click_context(ctx, *, test_fetch: bool, json_output: bool) -> AuthCheckPlan:
    """Build an :class:`AuthCheckPlan` from a Click context + flags.

    The profile + storage path come from the same :class:`AuthSource`
    resolver every other auth-aware command uses, so the diagnostic
    reports the same file the runtime would actually try to load.
    """
    auth = AuthSource.from_click_context(ctx)
    storage_path = auth.storage_path_for_diagnostics()
    has_env_auth = auth.has_env_auth
    has_home_env = bool(os.environ.get("NOTEBOOKLM_HOME"))
    return AuthCheckPlan(
        storage_path=storage_path,
        profile=auth.profile,
        has_env_auth=has_env_auth,
        has_home_env=has_home_env,
        test_fetch=test_fetch,
        json_output=json_output,
    )


def format_auth_source(plan: AuthCheckPlan) -> str:
    """Human-readable description of where ``plan`` reads auth from.

    Public helper so the command-layer renderer can re-use the same
    string in the Rich table and the JSON ``details.auth_source`` field.
    """
    if plan.has_env_auth:
        return AUTH_JSON_ENV_NAME
    if plan.has_home_env:
        return f"$NOTEBOOKLM_HOME ({plan.storage_path})"
    return f"file ({plan.storage_path})"


def _read_storage_state(plan: AuthCheckPlan) -> tuple[dict[str, Any] | None, str | None]:
    """Read the storage_state dict from disk or env var.

    Returns ``(state, error_message)``. On success ``error_message`` is
    ``None``; on failure ``state`` is ``None`` and ``error_message``
    carries the user-facing description.
    """
    if plan.has_env_auth:
        # Env-var auth: read the inline JSON via the consolidated
        # :func:`read_env_auth_json` accessor so this module stays out
        # of the auth-source consolidation gate's grep.
        try:
            return json.loads(read_env_auth_json()), None
        except json.JSONDecodeError as exc:
            return None, f"Invalid JSON: {exc}"
    try:
        return json.loads(plan.storage_path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON: {exc}"
    except (OSError, UnicodeDecodeError) as exc:
        # ``OSError`` on read (e.g. PermissionError) or
        # ``UnicodeDecodeError`` on a corrupt file must route through the
        # structured renderer so --json callers see a parseable
        # ``status: "error"`` envelope.
        return None, f"Storage unreadable: {exc}"


async def run_auth_check(plan: AuthCheckPlan) -> AuthCheckResult:
    """Execute an ``auth check`` plan and return the structured outcome.

    No side effects — the caller is responsible for rendering the result
    and choosing an exit code based on :attr:`AuthCheckResult.all_passed`.
    The function is ``async`` so the optional ``--test`` token-fetch path
    can ``await`` the network round-trip directly; the caller wraps the
    coroutine with ``cli.runtime.run_async`` to bridge into Click's sync
    handler.
    """
    from ...auth import extract_cookies_from_storage

    checks = _make_initial_checks()
    details: dict[str, Any] = {
        "storage_path": str(plan.storage_path),
        "auth_source": format_auth_source(plan),
        "cookies_found": [],
        "cookie_domains": [],
        "error": None,
    }

    # Check 1: storage exists.
    if plan.has_env_auth:
        checks["storage_exists"] = True
    else:
        checks["storage_exists"] = plan.storage_path.exists()

    if not checks["storage_exists"]:
        details["error"] = f"Storage file not found: {plan.storage_path}"
        return AuthCheckResult(plan=plan, checks=checks, details=details)

    # Check 2: JSON valid.
    storage_state, read_error = _read_storage_state(plan)
    if storage_state is None:
        details["error"] = read_error
        return AuthCheckResult(plan=plan, checks=checks, details=details)
    checks["json_valid"] = True

    # Check 3: cookies present + SID lookup.
    try:
        cookies = extract_cookies_from_storage(storage_state)
        checks["cookies_present"] = True
        checks["sid_cookie"] = "SID" in cookies
        details["cookies_found"] = list(cookies.keys())

        cookies_by_domain: dict[str, list[str]] = {}
        for cookie in storage_state.get("cookies", []):
            domain = cookie.get("domain", "")
            name = cookie.get("name", "")
            if domain and name and "google" in domain.lower():
                cookies_by_domain.setdefault(domain, []).append(name)
        details["cookies_by_domain"] = cookies_by_domain
        details["cookie_domains"] = sorted(cookies_by_domain.keys())
    except ValueError as exc:
        details["error"] = str(exc)
        return AuthCheckResult(plan=plan, checks=checks, details=details)

    # Check 4: optional token-fetch round-trip.
    if plan.test_fetch:
        try:
            from ...auth import fetch_tokens_with_domains

            token_path = None if plan.has_env_auth else plan.storage_path
            csrf, session_id = await fetch_tokens_with_domains(token_path, plan.profile)
            checks["token_fetch"] = True
            details["csrf_length"] = len(csrf)
            details["session_id_length"] = len(session_id)
        except Exception as exc:
            checks["token_fetch"] = False
            details["error"] = f"Token fetch failed: {exc}"

    return AuthCheckResult(plan=plan, checks=checks, details=details)


__all__ = [
    "AuthCheckPlan",
    "AuthCheckResult",
    "format_auth_source",
    "plan_from_click_context",
    "run_auth_check",
]
