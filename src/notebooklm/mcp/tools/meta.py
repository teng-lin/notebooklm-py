"""Meta MCP tool: ``server_info``.

Reports the package version and a local auth-health probe so an agent can tell,
before any notebook call, whether the server is authenticated. The auth check
reuses the transport-neutral :func:`notebooklm._app.auth_check.run_auth_check`
core (storage-exists / JSON-valid / cookies-present / SID), driven against the
on-disk ``storage_state.json`` the runtime would actually load (no network
round-trip — ``test_fetch`` is off).

``server_info`` takes no notebook argument and is read-only. The storage path +
active profile are resolved via the neutral :mod:`notebooklm.paths` helpers, so
this module imports NO ``click`` / ``rich`` / ``cli``.

It also accepts an opt-in ``include_account`` flag that adds the account
notebook/source limits + the global output language (for an agent to pace against
quota and know which language artifacts generate in). That block requires a *live*
session, so it is off by default — the default call stays a fast, network-free
auth-health probe that works even when unauthenticated.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..._app.auth_check import AuthCheckPlan, run_auth_check
from ..._version_info import version_string
from ...exceptions import NotebookLMError
from ...paths import get_storage_path, resolve_profile
from .._confirm import READ_ONLY
from .._context import get_client
from .._errors import mcp_errors, redact
from ..server import SERVER_NAME


def _no_env_auth_json() -> str:
    """Inline-auth reader for the neutral core.

    The MCP server authenticates from on-disk storage (``from_storage``), never
    from inline ``NOTEBOOKLM_AUTH_JSON``, so the plan always sets
    ``has_env_auth=False`` and this accessor is never invoked. It is wired only
    to satisfy the core's required keyword.
    """
    return ""  # pragma: no cover - unreachable while has_env_auth is False


async def _account_block(ctx: Context, *, authenticated: bool) -> dict[str, Any]:
    """Best-effort account identity + limits for quota pacing.

    ``email`` / ``authuser`` are the signed-in Google account, sourced from the
    client (in-memory ``AuthTokens`` → persisted metadata → a single live
    ``WIZ_global_data`` probe when authenticated). ``GET_USER_SETTINGS`` carries no
    identity, hence this separate source. ``client.get_account_email`` never raises
    for network/on-disk faults (degrades to ``None``); ``email`` is ``None`` only
    for pre-account-binding profiles that also can't be probed. The live probe is
    skipped when unauthenticated (``live_fallback=authenticated``) — identity is
    then whatever the profile has on disk.

    The limits/language fields need a *live* session. The local auth probe
    only proves on-disk storage health, not a live token, so ``include_account``
    can still hit an expired session. Rather than sink the whole ``server_info``
    response, that degrades to ``available: False`` with a short (scrubbed) reason
    (identity still included) — keeping the diagnostic useful.
    """
    client = get_client(ctx)
    # Identity from a single source (the client). Never raises. ``live_fallback`` is
    # gated on ``authenticated`` — suppress the live WIZ probe when the session is
    # already known stale (it would just fail), so the unauth path stays network-free.
    identity: dict[str, Any] = {
        "email": await client.get_account_email(live_fallback=authenticated),
        "authuser": client.get_account_authuser(),
    }
    if not authenticated:
        return {**identity, "available": False, "reason": "not authenticated"}
    try:
        # Both account limits and output language ride one GET_USER_SETTINGS
        # response (#1724): a single fetch instead of two identical POSTs.
        settings = await client.settings.get_user_settings()
        limits, output_language = settings.limits, settings.output_language
    except NotebookLMError as exc:  # degrade, don't sink the whole response
        # Route through the shared scrubber (same chokepoint as every other MCP
        # error): a NotebookLMError on the auth/config path can carry the on-disk
        # storage path, and this tool must never leak the host FS layout to a
        # (possibly remote) caller. ``redact`` also collapses + length-caps.
        return {**identity, "available": False, "reason": redact(str(exc))}
    return {
        **identity,
        "available": True,
        "notebook_limit": limits.notebook_limit,
        "source_limit": limits.source_limit,
        # Global account output language (e.g. "en" / "ja" / "zh_Hans"); ``None``
        # when unset or unparseable. Read-only here — a setter is tracked in #1723.
        "output_language": output_language,
    }


def register(mcp: Any) -> None:
    """Register the meta tool on ``mcp``."""

    @mcp.tool(annotations=READ_ONLY)
    async def server_info(ctx: Context, include_account: bool = False) -> dict[str, Any]:
        """Report the server version and local authentication health.

        Returns the package ``version`` and an ``auth`` block (``authenticated`` /
        ``storage_exists`` / ``json_valid`` / ``cookies_present`` / ``sid_cookie`` /
        ``profile``). Use it to confirm the server is logged in before driving
        notebook tools; if ``authenticated`` is false, run ``notebooklm login`` on
        the server host.

        Set ``include_account=True`` to also fetch an ``account`` block: the
        signed-in identity ``{email, authuser}`` (in-memory/persisted first, then a
        single live ``WIZ_global_data`` probe when authenticated — ``email`` is
        ``None`` only when it can't be discovered at all) plus quota-pacing fields
        ``{available, notebook_limit, source_limit, output_language}``
        (``output_language`` is the global account setting, e.g. ``"en"``/``"ja"``,
        or ``None`` when unset or unparseable). The quota fields need a *live*
        session (a few reads), so the block is off by default — the default call is
        a fast, network-free probe. When the session is missing or stale the quota
        fields degrade to ``{available: False, reason: ...}`` (identity still
        included) rather than failing the whole call.

        ``profile`` names the resolved storage profile the probe ran against
        (e.g. ``"default"``); the booleans are the actual health signals.

        The absolute on-disk storage path is deliberately **not** returned: it
        leaks the server-host OS username / filesystem layout to any (possibly
        remote) caller, while telling the agent nothing it can act on.
        """
        with mcp_errors():
            # Report the *resolved* profile (never ``None``): this names the
            # profile the auth probe actually ran against (#1790, #1791).
            profile = resolve_profile()
            storage_path = get_storage_path(profile)
            plan = AuthCheckPlan(
                storage_path=storage_path,
                profile=profile,
                has_env_auth=False,
                has_home_env=False,
                auth_source_label=f"file ({storage_path})",
                test_fetch=False,
                json_output=True,
            )
            result = await run_auth_check(plan, read_env_auth_json=_no_env_auth_json)
            info: dict[str, Any] = {
                "server": SERVER_NAME,
                "version": version_string(),
                "auth": {
                    "authenticated": result.all_passed,
                    "storage_exists": bool(result.checks.get("storage_exists")),
                    "json_valid": bool(result.checks.get("json_valid")),
                    "cookies_present": bool(result.checks.get("cookies_present")),
                    "sid_cookie": bool(result.checks.get("sid_cookie")),
                    "profile": profile,
                },
            }
            if include_account:
                info["account"] = await _account_block(ctx, authenticated=result.all_passed)
            return info
