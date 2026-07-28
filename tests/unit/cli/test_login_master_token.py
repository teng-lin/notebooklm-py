"""CLI tests for `notebooklm login --master-token[-refresh]` (service mocked)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

import notebooklm.cli.services.login.master_token as mt_service
from notebooklm._auth import browser_capture
from notebooklm.notebooklm_cli import cli
from notebooklm.paths import get_storage_path


def _seed_profile_account(monkeypatch, tmp_path, email):
    """Write a storage_state.json whose persisted account is ``email``."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    sp = get_storage_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(
        json.dumps(
            {
                "cookies": [],
                "notebooklm": {"version": 1, "account": {"authuser": 0, "email": email}},
            }
        )
    )


def test_master_token_refresh_calls_service(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    with patch.object(mt_service, "refresh", new=AsyncMock()) as ref:
        result = CliRunner().invoke(cli, ["login", "--master-token-refresh"])
    assert result.exit_code == 0, result.output
    assert ref.called
    assert "Re-minted" in result.output


def test_master_token_requires_account(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    result = CliRunner().invoke(cli, ["login", "--master-token"])
    assert result.exit_code == 1
    assert "--account" in result.output


def test_master_token_bootstrap_calls_service(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    with patch.object(mt_service, "bootstrap", new=AsyncMock(return_value=7)) as boot:
        result = CliRunner().invoke(
            cli,
            ["login", "--master-token", "--account", "e@x.com", "--oauth-token", "TOK"],
        )
    assert result.exit_code == 0, result.output
    assert boot.called
    assert boot.call_args.kwargs["oauth_token"] == "TOK"
    assert "7 notebooks" in result.output


def test_master_token_bootstrap_browser_capture_when_no_oauth(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    with (
        patch.object(mt_service, "capture_oauth_token", return_value="CAPTOK") as cap,
        patch.object(mt_service, "bootstrap", new=AsyncMock(return_value=3)) as boot,
    ):
        result = CliRunner().invoke(cli, ["login", "--master-token", "--account", "e@x.com"])
    assert result.exit_code == 0, result.output
    assert cap.called
    assert boot.call_args.kwargs["oauth_token"] == "CAPTOK"


class _ReachedPlaywright(RuntimeError):
    pass


class _PolicyProbe:
    def __init__(self, get_policy, default_policy):
        self._get_policy = get_policy
        self._default_policy = default_policy

    def __enter__(self):
        assert self._get_policy() is self._default_policy
        raise _ReachedPlaywright

    def __exit__(self, *args):
        return False


@pytest.mark.requires_playwright
@pytest.mark.parametrize("cdp_url", [None, "http://localhost:9222"])
def test_master_token_capture_uses_default_windows_policy(monkeypatch, cdp_url):
    """The policy swap must happen before Playwright enters, then be restored."""
    original_policy = object()
    default_policy = object()
    current_policy = original_policy
    transitions = []

    def get_policy():
        return current_policy

    def set_policy(policy):
        nonlocal current_policy
        current_policy = policy
        transitions.append(policy)

    monkeypatch.setattr(asyncio, "get_event_loop_policy", get_policy)
    monkeypatch.setattr(asyncio, "set_event_loop_policy", set_policy)
    monkeypatch.setattr(asyncio, "DefaultEventLoopPolicy", lambda: default_policy)
    monkeypatch.setattr(browser_capture.sys, "platform", "win32")

    with (
        patch(
            "playwright.sync_api.sync_playwright",
            return_value=_PolicyProbe(get_policy, default_policy),
        ),
        pytest.raises(_ReachedPlaywright),
    ):
        mt_service.capture_oauth_token(cdp_url=cdp_url)

    assert transitions == [default_policy, original_policy]
    assert current_policy is original_policy


def test_master_token_refuses_account_clobber(tmp_path, monkeypatch):
    _seed_profile_account(monkeypatch, tmp_path, "other@x.com")
    # Mismatch must fail fast — before any oauth_token capture.
    with patch.object(mt_service, "capture_oauth_token") as cap:
        result = CliRunner().invoke(cli, ["login", "--master-token", "--account", "e@x.com"])
    assert result.exit_code == 1
    assert "already belongs to other@x.com" in result.output
    assert not cap.called  # guard fires before sign-in


def test_master_token_refuses_clobber_via_token_owner_only(tmp_path, monkeypatch):
    # No storage_state.json, but a master_token.json owned by a different account.
    from notebooklm.auth import write_master_token
    from notebooklm.paths import get_master_token_path

    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    mtp = get_master_token_path()
    mtp.parent.mkdir(parents=True, exist_ok=True)
    write_master_token(mtp, email="other@x.com", master_token="aas_et/M", android_id="abc")
    with patch.object(mt_service, "capture_oauth_token") as cap:
        result = CliRunner().invoke(cli, ["login", "--master-token", "--account", "e@x.com"])
    assert result.exit_code == 1
    assert "already belongs to other@x.com" in result.output
    assert not cap.called


def test_master_token_force_overwrites_other_account(tmp_path, monkeypatch):
    _seed_profile_account(monkeypatch, tmp_path, "other@x.com")
    with patch.object(mt_service, "bootstrap", new=AsyncMock(return_value=4)) as boot:
        result = CliRunner().invoke(
            cli,
            ["login", "--master-token", "--account", "e@x.com", "--oauth-token", "T", "--force"],
        )
    assert result.exit_code == 0, result.output
    assert boot.call_args.kwargs["force"] is True
