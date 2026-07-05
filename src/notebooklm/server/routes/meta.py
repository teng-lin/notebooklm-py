"""Server-info route — ``GET /v1/server/info``.

Mirrors the MCP ``server_info`` tool (``mcp/tools/meta.py``): reports the package
version and a local auth-health probe (storage-exists / JSON-valid /
cookies-present / SID) so an agent can tell, before any notebook call, whether the
server is authenticated. The probe reuses the transport-neutral
:func:`notebooklm._app.auth_check.run_auth_check` core driven against the on-disk
``storage_state.json`` the runtime would actually load (no network — ``test_fetch``
is off).

``?include_account=true`` additionally fetches the signed-in identity + quota
limits + output language, which need a *live* session (so the block is off by
default and degrades to ``{available: False, reason}`` on a stale session rather
than failing the whole call).

The absolute on-disk storage path is deliberately **not** returned — it leaks the
server-host OS username / filesystem layout to the caller while telling it nothing
actionable (the MCP surface scrubs it identically). This is a single-tenant
server, so the info reflects the one lifespan-bound client.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from ..._app.auth_check import AuthCheckPlan, run_auth_check
from ..._redact import redact
from ..._version_info import version_string
from ...client import NotebookLMClient
from ...exceptions import NotebookLMError
from ...paths import get_storage_path, resolve_profile
from .._context import get_client

__all__ = ["router"]

#: Named here rather than imported from ``server.app`` to avoid a circular import
#: (``app`` imports this router). Kept equal to ``server.app.SERVER_NAME`` by
#: ``tests/server/test_main.py``.
SERVER_NAME = "notebooklm-server"

router = APIRouter(prefix="/server", tags=["server"])

ClientDep = Annotated[NotebookLMClient, Depends(get_client)]


def _no_env_auth_json() -> str:
    """Inline-auth reader for the neutral core.

    The server authenticates from on-disk storage (``from_storage``), never from
    inline ``NOTEBOOKLM_AUTH_JSON``, so the plan sets ``has_env_auth=False`` and
    this accessor is never invoked. It satisfies the core's required keyword only.
    """
    return ""  # pragma: no cover - unreachable while has_env_auth is False


async def _account_block(client: NotebookLMClient, *, authenticated: bool) -> dict[str, Any]:
    """Best-effort account identity + quota limits for pacing (mirrors MCP).

    ``email`` / ``authuser`` come from the client; the limits/language fields need
    a live session and degrade to ``{available: False, reason}`` (scrubbed) rather
    than sinking the whole response when the session is stale.
    """
    identity: dict[str, Any] = {
        "email": await client.get_account_email(live_fallback=authenticated),
        "authuser": client.get_account_authuser(),
    }
    if not authenticated:
        return {**identity, "available": False, "reason": "not authenticated"}
    try:
        limits, output_language = await asyncio.gather(
            client.settings.get_account_limits(),
            client.settings.get_output_language(),
        )
    except NotebookLMError as exc:  # degrade, don't sink the whole response
        return {**identity, "available": False, "reason": redact(str(exc))}
    return {
        **identity,
        "available": True,
        "notebook_limit": limits.notebook_limit,
        "source_limit": limits.source_limit,
        "output_language": output_language,
    }


@router.get("/info")
async def server_info(
    client: ClientDep,
    include_account: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Report the server version and local authentication health.

    Returns ``version`` and an ``auth`` block (``authenticated`` /
    ``storage_exists`` / ``json_valid`` / ``cookies_present`` / ``sid_cookie`` /
    ``profile``). Set ``?include_account=true`` to also fetch an ``account`` block
    (signed-in identity + quota limits + output language); it needs a live session,
    so it degrades to ``{available: False, reason}`` rather than failing the call.

    The absolute on-disk storage path is deliberately not returned (it leaks the
    host filesystem layout while telling the agent nothing actionable).
    """
    # Report the *resolved* profile (never ``None``): this names the profile the
    # auth probe actually ran against (#1790, #1791).
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
        info["account"] = await _account_block(client, authenticated=result.all_passed)
    return info
