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

from ..auth import (
    MasterTokenError,
    assert_account_writable,
    master_token_bootstrap,
    master_token_remint,
    read_master_token,
)
from ..paths import get_storage_path, master_token_path_for
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
    # Canonicalize an explicit ``--storage`` exactly like the auth-source resolver
    # does (``cli/services/auth_source.py``): a symlinked/relative alias must select
    # the same profile as its target, or the token would be written beside the alias
    # while the L4 recovery rung (which resolves via ``canonical_storage_key``) looks
    # beside the real file. Profile-derived paths are already absolute.
    storage_path = (
        Path(storage).expanduser().resolve() if storage else get_storage_path(profile=profile)
    )

    try:
        if refresh:
            asyncio.run(master_token_remint(storage_path))
            console.print(f"[green]Re-minted cookies[/green] -> {storage_path}")
            return
        if not account_email:
            console.print("[red]--master-token requires --account EMAIL[/red]")
            exit_with_code(1)
        # Guard before the (interactive) oauth_token capture so a wrong profile
        # fails fast instead of after a full sign-in. Advisory only — the
        # authoritative, race-free enforcement lives under the storage-write
        # lock inside master_token_bootstrap's persist step (#2103 PR-2 D6).
        assert_account_writable(email=account_email, storage_path=storage_path, force=force)
        # Cheap pre-capture probe (#2103 PR-2 D5): a malformed master_token.json
        # fails BEFORE the ~300s interactive sign-in, not after. android_id
        # resolution itself (explicit -> stored -> generated) now happens
        # inside master_token_bootstrap.
        read_master_token(master_token_path_for(storage_path))
        token = oauth_token or mt_service.capture_oauth_token(browser=browser, cdp_url=cdp_url)
        count = asyncio.run(
            master_token_bootstrap(
                email=account_email,
                oauth_token=token,
                storage_path=storage_path,
                android_id=android_id,
                force=force,
            )
        )
        console.print(
            f"[green]Master-token login OK[/green] — {count} notebooks. Saved to {storage_path}"
        )
    except MasterTokenError as exc:
        console.print(f"[red]{exc}[/red]")
        exit_with_code(1)
