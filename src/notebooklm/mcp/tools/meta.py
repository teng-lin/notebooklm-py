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

It also accepts an opt-in ``include_account`` flag that adds the account tier +
notebook/source limits (for an agent to pace against quota). That block requires
a *live* session, so it is off by default — the default call stays a fast,
network-free auth-health probe that works even when unauthenticated.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import Context

from ... import __version__
from ..._app.auth_check import AuthCheckPlan, run_auth_check
from ...exceptions import NotebookLMError
from ...paths import get_active_profile, get_storage_path
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
    """Best-effort account tier + limits for quota pacing.

    The local auth probe only proves on-disk storage health, not a live token, so
    ``include_account`` can still hit an expired session. Rather than sink the
    whole ``server_info`` response, this degrades to ``available: False`` with a
    short (scrubbed) reason — keeping the diagnostic useful. ``get_account_tier``
    always returns an :class:`AccountTier`, but its ``tier`` field is best-effort
    and may be ``None`` even on success (that is ``available: True`` with
    ``tier: None``, NOT an error).
    """
    if not authenticated:
        return {"available": False, "reason": "not authenticated"}
    client = get_client(ctx)
    try:
        # Two independent reads → run concurrently (repo convention for
        # independent RPCs). A NotebookLMError from either is still caught below.
        limits, tier = await asyncio.gather(
            client.settings.get_account_limits(),
            client.settings.get_account_tier(),
        )
    except NotebookLMError as exc:  # degrade, don't sink the whole response
        # Route through the shared scrubber (same chokepoint as every other MCP
        # error): a NotebookLMError on the auth/config path can carry the on-disk
        # storage path, and this tool must never leak the host FS layout to a
        # (possibly remote) caller. ``redact`` also collapses + length-caps.
        return {"available": False, "reason": redact(str(exc))}
    return {
        "available": True,
        "tier": tier.tier,
        "plan_name": tier.plan_name,
        "notebook_limit": limits.notebook_limit,
        "source_limit": limits.source_limit,
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

        Set ``include_account=True`` to also fetch an ``account`` block for quota
        pacing: ``{available, tier, plan_name, notebook_limit, source_limit}``.
        This needs a *live* session (two RPCs), so it is off by default — the
        default call is a fast, network-free probe. When the session is missing or
        stale the block degrades to ``{available: False, reason: ...}`` rather than
        failing the whole call; ``tier`` may be ``None`` even when ``available`` is
        true (it is a best-effort signal).

        The absolute on-disk storage path is deliberately **not** returned: it
        leaks the server-host OS username / filesystem layout to any (possibly
        remote) caller, while telling the agent nothing it can act on. The
        ``profile`` name + booleans are sufficient to diagnose auth health.
        """
        with mcp_errors():
            profile = get_active_profile()
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
                "version": __version__,
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
