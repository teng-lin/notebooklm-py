"""Transport-neutral ``auth check`` diagnostics business logic.

This is the Click-free core of ``cli/services/auth_diagnostics.py``: it runs the
"validate the cookies on disk" probe — storage-exists, JSON-valid,
cookies-present, SID-cookie, plus the optional ``--test`` token-fetch
round-trip — and returns a structured :class:`AuthCheckResult`. Every transport
adapter (the Click CLI today, a future HTTP / FastMCP surface tomorrow) drives
:func:`run_auth_check` and renders the report into its own surface + exit-code
policy; the Rich table stays in the CLI (``cli/_session_render.py``).

Two boundary-imposed seams are worth calling out:

* **The inline-auth-JSON reader is injected, never imported.** When env-supplied
  auth is active the probe reads the inline JSON instead of the file; that read
  routes through the CLI's consolidated ``read_env_auth_json`` accessor (the
  single ``NOTEBOOKLM_AUTH_JSON`` SoT in ``cli.services.auth_source``), so the
  neutral core takes a ``read_env_auth_json`` callable rather than touching
  ``os.environ`` directly.
* **The plan carries pre-resolved values.** ``storage_path`` / ``profile`` /
  ``has_env_auth`` / ``has_home_env`` are resolved by the CLI's
  ``AuthSource``-backed ``plan_from_click_context`` (which reads the Click
  context); the neutral core never reads a Click context or an env var for a
  precedence decision.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthCheckPlan:
    """One ``auth check`` invocation (pre-resolved, Click-free).

    Attributes:
        storage_path: Resolved storage_state.json path (the file the check reads
            when no env-var auth is active).
        profile: Active profile name (forwarded to the token-fetch path so
            SID/SAPISID extraction targets the right account).
        has_env_auth: ``True`` when env-supplied auth is active; short-circuits
            the file-read in favor of parsing the inline JSON.
        has_home_env: ``True`` when ``NOTEBOOKLM_HOME`` is set; used in the
            ``auth_source`` display string.
        auth_source_label: Human-readable description of where auth is read from
            (resolved by the adapter so the neutral core never branches on the
            env-var name). Surfaced verbatim in ``details.auth_source``.
        test_fetch: When ``True``, also exercise the token-fetch path (network
            round-trip). Off by default.
        json_output: When ``True``, signals the caller to render a JSON envelope
            and propagate non-zero exit on failure. Carried on the plan so the
            renderer (in the adapter) picks the right shape without re-resolving
            the flag.
        passive: When ``True``, the optional ``test_fetch`` token round-trip uses
            the strictly read-only :func:`~notebooklm.auth.fetch_tokens_passive`
            path — it never runs ``NOTEBOOKLM_REFRESH_CMD``, never fires the
            keepalive rotation poke, and never writes cookies back to disk. This
            is what an unattended readiness probe wants (issue #1569). No effect
            without ``test_fetch`` (the local cookie checks are already
            side-effect-free).
    """

    storage_path: Path
    profile: str | None
    has_env_auth: bool
    has_home_env: bool
    auth_source_label: str
    test_fetch: bool
    json_output: bool
    passive: bool = False


@dataclass
class AuthCheckResult:
    """Outcome of a single ``auth check`` run.

    The ``checks`` dict mirrors the legacy contract: each value is ``True``
    (passed), ``False`` (failed), or ``None`` (not tested — only valid for
    ``token_fetch``). ``details`` carries human-readable context the renderer
    joins into the table / JSON envelope.
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


def _read_storage_state(
    plan: AuthCheckPlan,
    *,
    read_env_auth_json: Callable[[], str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Read the storage_state dict from disk or the inline env JSON.

    Returns ``(state, error_message)``. On success ``error_message`` is ``None``;
    on failure ``state`` is ``None`` and ``error_message`` carries the
    user-facing description.
    """
    if plan.has_env_auth:
        # Env-var auth: read the inline JSON via the injected accessor so this
        # neutral core stays out of the auth-source consolidation gate's grep.
        try:
            return json.loads(read_env_auth_json()), None
        except json.JSONDecodeError as exc:
            return None, f"Invalid JSON: {exc}"
    try:
        return json.loads(plan.storage_path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON: {exc}"
    except (OSError, UnicodeDecodeError) as exc:
        # ``OSError`` on read (e.g. PermissionError) or ``UnicodeDecodeError`` on
        # a corrupt file must route through the structured renderer so --json
        # callers see a parseable ``status: "error"`` envelope.
        return None, f"Storage unreadable: {exc}"


async def run_auth_check(
    plan: AuthCheckPlan,
    *,
    read_env_auth_json: Callable[[], str],
) -> AuthCheckResult:
    """Execute an ``auth check`` plan and return the structured outcome.

    No side effects beyond the optional network round-trip — the caller renders
    the result and chooses an exit code based on
    :attr:`AuthCheckResult.all_passed`. ``async`` so the optional ``--test``
    token-fetch path can ``await`` the network round-trip directly.

    ``read_env_auth_json`` is injected (the CLI's consolidated accessor) so the
    neutral core reads the inline-auth payload without touching ``os.environ``.
    """
    from ..auth import extract_cookies_from_storage

    checks = _make_initial_checks()
    details: dict[str, Any] = {
        "storage_path": str(plan.storage_path),
        "auth_source": plan.auth_source_label,
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
    storage_state, read_error = _read_storage_state(plan, read_env_auth_json=read_env_auth_json)
    if storage_state is None:
        details["error"] = read_error
        return AuthCheckResult(plan=plan, checks=checks, details=details)
    checks["json_valid"] = True

    # Check 3: cookies present + SID lookup.
    try:
        cookies = extract_cookies_from_storage(storage_state)
        checks["cookies_present"] = bool(cookies)
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

    # Check 4: optional token-fetch round-trip. ``passive`` selects the
    # strictly read-only fetch (no refresh cmd, no rotation poke, no save) so a
    # readiness probe never mutates state or spawns a subprocess (issue #1569).
    if plan.test_fetch:
        try:
            from ..auth import fetch_tokens_passive, fetch_tokens_with_domains

            fetch = fetch_tokens_passive if plan.passive else fetch_tokens_with_domains
            token_path = None if plan.has_env_auth else plan.storage_path
            csrf, session_id = await fetch(token_path, plan.profile)
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
    "run_auth_check",
]
