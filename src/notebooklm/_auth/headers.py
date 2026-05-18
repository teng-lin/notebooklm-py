"""Header/route helpers for AuthTokens.

This module is intentionally small: most authuser/header helpers already live
in :mod:`notebooklm._auth.account` (``authuser_query``,
``format_authuser_value``, ``get_authuser_for_storage``,
``get_account_email_for_storage``). What lives here is the higher-level
*routing* glue that combines them — currently only
:func:`_resolve_token_route_kwargs`, used by the token-fetch entry points to
preserve explicit caller intent vs. resolved-from-storage defaults.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .account import get_account_email_for_storage, get_authuser_for_storage


def _resolve_token_route_kwargs(
    storage_path: Path | None,
    *,
    authuser: int | None,
    account_email: str | None,
) -> dict[str, Any]:
    """Resolve token-fetch routing while preserving explicit caller intent."""
    explicit_authuser = authuser is not None
    resolved_authuser = get_authuser_for_storage(storage_path) if authuser is None else authuser
    if account_email is not None:
        resolved_account_email = account_email
    elif explicit_authuser:
        resolved_account_email = None
    else:
        resolved_account_email = get_account_email_for_storage(storage_path)

    route_kwargs: dict[str, Any] = {"authuser": resolved_authuser}
    if resolved_account_email is not None:
        route_kwargs["account_email"] = resolved_account_email
    if explicit_authuser:
        route_kwargs["force_authuser_query"] = True
    return route_kwargs
