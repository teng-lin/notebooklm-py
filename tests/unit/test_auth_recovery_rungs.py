"""Neutral L3 recovery-rung registry and browser-adapter tests."""

from __future__ import annotations

import importlib
from collections.abc import Generator
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

import notebooklm.auth as auth
from notebooklm._auth import headless_reauth, recovery
from notebooklm._auth.cookie_types import CookieJar
from notebooklm._auth.cookies import _LoadedCookiePair
from notebooklm._auth.headless_reauth import HeadlessReauthResult, HeadlessReauthStatus
from notebooklm._auth.recovery_rungs import (
    HeadlessRungOutcome,
    HeadlessRungStatus,
    install_headless_rung,
    installed_headless_rung,
)


@pytest.fixture(autouse=True)
def _restore_installed_rung() -> Generator[None, None, None]:
    previous = installed_headless_rung()
    try:
        yield
    finally:
        install_headless_rung(previous)


@pytest.mark.parametrize(
    ("status", "succeeded"),
    [
        (HeadlessRungStatus.SUCCEEDED, True),
        (HeadlessRungStatus.UNAVAILABLE, False),
        (HeadlessRungStatus.FAILED, False),
    ],
)
def test_headless_rung_outcome_derives_success(
    status: HeadlessRungStatus,
    succeeded: bool,
) -> None:
    outcome = HeadlessRungOutcome(status, "safe reason")

    assert outcome.succeeded is succeeded


def test_registry_install_returns_previous_rung() -> None:
    first = Mock(return_value=HeadlessRungOutcome(HeadlessRungStatus.UNAVAILABLE, "first"))
    second = Mock(return_value=HeadlessRungOutcome(HeadlessRungStatus.FAILED, "second"))

    original = install_headless_rung(first)
    assert install_headless_rung(second) is first
    assert installed_headless_rung() is second
    assert install_headless_rung(original) is second


@pytest.mark.asyncio
async def test_recovery_without_an_installed_rung_does_not_reload_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_headless_rung(None)
    loader = Mock(side_effect=AssertionError("storage reload must not run"))
    monkeypatch.setattr("notebooklm._auth.cookies._build_cookie_pair_from_storage", loader)

    result = await recovery._try_headless_reauth_result(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
    )

    assert result is None
    loader.assert_not_called()


@pytest.mark.parametrize(
    "status",
    [HeadlessRungStatus.UNAVAILABLE, HeadlessRungStatus.FAILED],
)
@pytest.mark.asyncio
async def test_recovery_reloads_storage_only_after_success(
    status: HeadlessRungStatus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "storage_state.json"
    rung = Mock(return_value=HeadlessRungOutcome(status, "did not succeed"))
    loader = Mock(side_effect=AssertionError("storage reload must not run"))
    install_headless_rung(rung)
    monkeypatch.setattr("notebooklm._auth.cookies._build_cookie_pair_from_storage", loader)

    result = await recovery._try_headless_reauth_result(
        storage_path=storage,
        allow_headless=True,
    )

    assert result is None
    rung.assert_called_once_with(storage_path=storage, allow_headless=True)
    loader.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_reloads_exact_pair_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "storage_state.json"
    expected = _LoadedCookiePair(httpx.Cookies(), CookieJar())
    rung = Mock(return_value=HeadlessRungOutcome(HeadlessRungStatus.SUCCEEDED, "captured"))
    loader = Mock(return_value=expected)
    install_headless_rung(rung)
    monkeypatch.setattr("notebooklm._auth.cookies._build_cookie_pair_from_storage", loader)

    result = await recovery._try_headless_reauth_result(
        storage_path=storage,
        allow_headless=True,
    )

    assert result is expected
    rung.assert_called_once_with(storage_path=storage, allow_headless=True)
    loader.assert_called_once_with(storage)


@pytest.mark.parametrize(
    ("browser_status", "rung_status"),
    [
        (HeadlessReauthStatus.SUCCESS, HeadlessRungStatus.SUCCEEDED),
        (HeadlessReauthStatus.UNAVAILABLE, HeadlessRungStatus.UNAVAILABLE),
        (HeadlessReauthStatus.FAILED, HeadlessRungStatus.FAILED),
    ],
)
def test_browser_adapter_maps_status_and_uses_explicit_storage_profile(
    browser_status: HeadlessReauthStatus,
    rung_status: HeadlessRungStatus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "explicit" / "storage_state.json"
    browser_profile = tmp_path / "explicit" / "browser_profile"
    resolver = Mock(return_value=browser_profile)
    attempt = Mock(return_value=HeadlessReauthResult(browser_status, "mapped reason"))
    monkeypatch.setattr(headless_reauth, "get_browser_profile_dir", resolver)
    monkeypatch.setattr(headless_reauth, "attempt_headless_reauth", attempt)

    outcome = headless_reauth.headless_rung(storage_path=storage, allow_headless=True)

    assert outcome == HeadlessRungOutcome(rung_status, "mapped reason")
    resolver.assert_called_once_with(storage_path=storage)
    attempt.assert_called_once_with(
        storage_path=storage,
        allow_headless=True,
        browser_profile=browser_profile,
    )


def test_reloading_auth_facade_reinstalls_the_lazy_default() -> None:
    install_headless_rung(None)

    reloaded = importlib.reload(auth)

    assert installed_headless_rung() is reloaded._default_headless_rung
