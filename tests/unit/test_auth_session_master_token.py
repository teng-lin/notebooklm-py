"""Tests for the layer-4 master-token re-mint recovery in _auth/session.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import notebooklm._auth.cookies as cookies_mod
from notebooklm._auth import master_token as mt
from notebooklm._auth import session as session_mod
from notebooklm._auth.mint_service import MintService
from notebooklm._auth.profile_store import ProfileStore


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
    auth = MagicMock(storage_path=None)
    assert await session_mod._try_master_token_reauth(auth=auth, kernel=MagicMock()) is False


@pytest.mark.asyncio
async def test_reauth_declines_without_token_file(tmp_path):
    auth = MagicMock(storage_path=tmp_path / "storage_state.json")
    assert await session_mod._try_master_token_reauth(auth=auth, kernel=MagicMock()) is False


@pytest.mark.asyncio
async def test_reauth_success_remints_and_reloads(tmp_path):
    mt.write_master_token(
        tmp_path / "master_token.json", email="e@x.com", master_token="aas_et/M", android_id="abc"
    )
    auth = MagicMock(storage_path=tmp_path / "storage_state.json")
    jar = httpx.Cookies()
    jar.set("SID", "v", domain=".google.com")
    # _replace_cookie_jar runs for real against the MagicMock kernel (a no-op on a
    # mock jar) — only the network mint + the recovery-aware reload are stubbed.
    with (
        _patch_mint(AsyncMock(return_value=jar)),
        patch.object(
            ProfileStore,
            "replace_minted_session",
            autospec=True,
            side_effect=_persist_writes_valid_storage,
        ) as persist,
        patch.object(cookies_mod, "build_httpx_cookies_from_storage", return_value=httpx.Cookies()),
    ):
        ok = await session_mod._try_master_token_reauth(auth=auth, kernel=MagicMock())
    assert ok is True
    persist.assert_called_once()


@pytest.mark.asyncio
async def test_reauth_returns_false_on_revoked_token(tmp_path):
    mt.write_master_token(
        tmp_path / "master_token.json", email="e@x.com", master_token="aas_et/M", android_id="abc"
    )
    auth = MagicMock(storage_path=tmp_path / "storage_state.json")
    with _patch_mint(AsyncMock(side_effect=mt.MasterTokenError("revoked"))):
        ok = await session_mod._try_master_token_reauth(auth=auth, kernel=MagicMock())
    assert ok is False
