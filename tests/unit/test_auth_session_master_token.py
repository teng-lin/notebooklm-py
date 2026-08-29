"""Tests for the layer-4 master-token re-mint recovery in _auth/session.py."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import notebooklm._auth.cookies as cookies_mod
from notebooklm._auth import master_token as mt
from notebooklm._auth import session as session_mod
from notebooklm._auth.mint_service import MintService
from notebooklm._auth.profile_store import ProfileStore
from notebooklm.auth import AuthTokens
from tests._helpers.client_factory import build_client_shell_for_tests


def _auth(storage_path: Path | None) -> AuthTokens:
    jar = httpx.Cookies()
    jar.set("SID", "stale", domain=".google.com")
    jar.set("__Secure-1PSIDTS", "stale-ts", domain=".google.com")
    return AuthTokens(
        cookies={},
        csrf_token="stale-csrf",
        session_id="stale-session",
        storage_path=storage_path,
        cookie_jar=jar,
    )


@asynccontextmanager
async def _opened_recovery(auth: AuthTokens):
    """Yield recovery collaborators fenced to a lifecycle-created epoch."""
    client = build_client_shell_for_tests(auth)
    await client.__aenter__()
    try:
        collaborators = client._collaborators
        lifecycle = collaborators.lifecycle
        generation = collaborators.call_supervisor._current
        expected_epoch = lifecycle._epoch
        assert lifecycle.is_open()
        assert expected_epoch > 0
        assert generation is not None and generation.epoch == expected_epoch
        assert collaborators.kernel._active_epoch == expected_epoch
        yield collaborators.kernel, expected_epoch
    finally:
        await client.close()


def _persist_writes_valid_storage(store, request):
    """Stand-in for ``ProfileStore.replace_minted_session`` that writes a
    readable file. #2103 PR-2: the kernel's own internal reload
    (``remint_from_stored_token``'s strict-loader step) now needs a real file
    at ``path`` to succeed — mocking ``persist_minted_jar`` as a bare no-op
    (as before this PR) leaves nothing for that reload to find."""
    store.path.write_text(
        json.dumps(
            {
                # The kernel's own internal reload validates the Tier-1 minimum
                # cookie set (MINIMUM_REQUIRED_COOKIES) via the strict loader.
                "cookies": [
                    {"name": "SID", "value": "v", "domain": ".google.com"},
                    {"name": "__Secure-1PSIDTS", "value": "v", "domain": ".google.com"},
                ],
                "notebooklm": {
                    "version": 1,
                    "account": {"authuser": 0, "email": request.email},
                },
            }
        ),
        encoding="utf-8",
    )


def _patch_mint(effect):
    async def mint(_service, token):
        return await effect(token.email, token.secret, token.android_id)

    return patch.object(MintService, "mint", autospec=True, side_effect=mint)


@pytest.mark.asyncio
async def test_reauth_declines_without_storage_path():
    auth = _auth(None)
    async with _opened_recovery(auth) as (kernel, expected_epoch):
        assert (
            await session_mod._try_master_token_reauth(
                auth=auth,
                kernel=kernel,
                expected_epoch=expected_epoch,
            )
            is False
        )


@pytest.mark.asyncio
async def test_reauth_declines_without_token_file(tmp_path):
    auth = _auth(tmp_path / "storage_state.json")
    async with _opened_recovery(auth) as (kernel, expected_epoch):
        assert (
            await session_mod._try_master_token_reauth(
                auth=auth,
                kernel=kernel,
                expected_epoch=expected_epoch,
            )
            is False
        )


@pytest.mark.asyncio
async def test_reauth_success_remints_and_reloads(tmp_path):
    mt.write_master_token(
        tmp_path / "master_token.json", email="e@x.com", master_token="aas_et/M", android_id="abc"
    )
    auth = _auth(tmp_path / "storage_state.json")
    jar = httpx.Cookies()
    jar.set("SID", "v", domain=".google.com")
    # _replace_cookie_jar runs for real against the MagicMock kernel (a no-op on a
    # mock jar) — only the network mint + the recovery-aware reload are stubbed.
    async with _opened_recovery(auth) as (kernel, expected_epoch):
        with (
            _patch_mint(AsyncMock(return_value=jar)),
            patch.object(
                ProfileStore,
                "replace_minted_session",
                autospec=True,
                side_effect=_persist_writes_valid_storage,
            ) as persist,
            patch.object(
                cookies_mod,
                "build_httpx_cookies_from_storage",
                return_value=httpx.Cookies(),
            ),
        ):
            ok = await session_mod._try_master_token_reauth(
                auth=auth,
                kernel=kernel,
                expected_epoch=expected_epoch,
            )
    assert ok is True
    persist.assert_called_once()


@pytest.mark.asyncio
async def test_reauth_returns_false_on_revoked_token(tmp_path):
    mt.write_master_token(
        tmp_path / "master_token.json", email="e@x.com", master_token="aas_et/M", android_id="abc"
    )
    auth = _auth(tmp_path / "storage_state.json")
    async with _opened_recovery(auth) as (kernel, expected_epoch):
        with _patch_mint(AsyncMock(side_effect=mt.MasterTokenError("revoked"))):
            ok = await session_mod._try_master_token_reauth(
                auth=auth,
                kernel=kernel,
                expected_epoch=expected_epoch,
            )
    assert ok is False
