"""Command-layer driver for ``notebooklm login --master-token[-refresh]``.

Thin Click-adjacent glue over the ``notebooklm.auth`` master-token transaction
ops (#2103 PR-2 structural follow-up — the CLI invokes whole audited
transactions, never assembles minting primitives itself): resolves the
profile's paths, runs the async bootstrap/remint, and renders the outcome.
Kept out of ``session_cmd.py`` to hold that module under the size ratchet
(ADR-0008).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .._app.master_token import (
    MasterTokenError,
    MasterTokenLoginPlan,
    bootstrap_login,
    remint_login,
)
from ..paths import get_storage_path
from .error_handler import exit_with_code
from .rendering import console
from .services.login import master_token as mt_service


def run_master_token_login(
    ctx,
    *,
    storage,
    browser,
    account_email,
    oauth_token,
    android_id,
    cdp_url,
    refresh,
    force=False,
):
    """Bootstrap or refresh headless master-token auth (see ``login --master-token``)."""
    profile = ctx.obj.get("profile") if ctx.obj else None
    del ctx
    storage_path = None
    plan = None
    capture_oauth_token = None
    run_async = None
    login_result = None
    remint_result = None
    try:
        # Canonicalize an explicit ``--storage`` exactly like the auth-source
        # resolver. Profile-derived paths are already absolute.
        storage_path = (
            Path(storage).expanduser().resolve() if storage else get_storage_path(profile=profile)
        )
        run_async = asyncio.run
        try:
            if refresh:
                remint_result = remint_login(storage_path, run_async=run_async)
                console.print(f"[green]Re-minted cookies[/green] -> {remint_result.storage_path}")
                return
            if not account_email:
                console.print("[red]--master-token requires --account EMAIL[/red]")
                exit_with_code(1)
            plan = MasterTokenLoginPlan(
                email=account_email,
                storage_path=storage_path,
                android_id=android_id,
                force=force,
            )
            capture_oauth_token = mt_service.capture_oauth_token
            login_result = bootstrap_login(
                plan,
                oauth_token=oauth_token,
                browser=browser,
                cdp_url=cdp_url,
                capture_oauth_token=capture_oauth_token,
                run_async=run_async,
            )
            console.print(
                "[green]Master-token login OK[/green] — "
                f"{login_result.notebook_count} notebooks. Saved to {login_result.storage_path}"
            )
        except MasterTokenError as exc:
            console.print(f"[red]{exc}[/red]")
            exit_with_code(1)
    finally:
        del profile
        del storage_path
        del plan
        del capture_oauth_token
        del run_async
        del login_result
        del remint_result
        del storage
        del browser
        del account_email
        del oauth_token
        del android_id
        del cdp_url
        del refresh
        del force
