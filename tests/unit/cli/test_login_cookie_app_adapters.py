"""Compatibility tests for the CLI adapters over ``_app.login_cookie``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest

import notebooklm.auth as auth
from notebooklm._app.login_cookie import Account
from notebooklm.cli import _cookie_import
from notebooklm.cli.services.login import (
    browser_accounts,
    chromium_accounts,
    cookie_domains,
    cookie_jar,
)
from notebooklm.cli.services.login.outcomes import CookieValidationFailure, StaleCookies
from tests._fixtures.login_io import RecordingLoginIO


def _row(name: str, value: str = "value") -> dict[str, object]:
    return {"name": name, "value": value, "domain": ".google.com", "path": "/"}


class _DirectBaseException(BaseException):
    pass


def test_import_adapter_maps_neutral_failure_without_context(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException) as caught:
        _cookie_import._import_cookie_json(
            payload={},
            storage_path=tmp_path / "state.json",
            include_domains=set(),
            include_optional=False,
        )
    assert "Cookie JSON must be" in str(caught.value)
    assert caught.value.__cause__ is None


def test_domain_adapters_preserve_mutable_and_frozen_shapes() -> None:
    labels = cookie_domains._parse_include_domains(("YouTube, docs",))
    assert labels == {"youtube", "docs"}
    assert isinstance(labels, set)
    resolved = cookie_domains._resolve_optional_cookie_domains(labels)
    assert isinstance(resolved, frozenset)
    assert cookie_domains._build_google_cookie_domains(include_domains=labels) == sorted(
        cookie_domains._build_google_cookie_domains(include_domains=labels)
    )


def test_cookie_jar_adapter_preserves_stale_outcome_text() -> None:
    io = RecordingLoginIO(run_async=MagicMock(side_effect=ValueError("rejected")))
    with (
        patch.object(cookie_jar, "validate_with_recovery", return_value=({"cookies": []}, None)),
        patch.object(auth, "extract_cookies_with_domains", return_value={}),
        patch.object(auth, "build_cookie_jar", return_value=object()),
        patch.object(auth, "enumerate_accounts", MagicMock(return_value=object())),
    ):
        result = cookie_jar._enumerate_one_jar([], "chrome", None, quiet=True, io=io)
    assert isinstance(result, StaleCookies)
    assert result.code == "STALE_COOKIES"
    assert result.message == "Saved cookies for chrome are too stale for Google to re-authenticate."


@pytest.mark.parametrize("quiet", [False, True])
def test_cookie_validation_failure_compatibility_adapter_is_importable(quiet: bool) -> None:
    result = cookie_jar._cookie_validation_failure(
        {"cookies": []},
        ValueError("missing SID"),
        browser_name="chrome",
        quiet=quiet,
    )
    assert isinstance(result, CookieValidationFailure)
    assert result.code == "COOKIE_VALIDATION_FAILED"
    if quiet:
        assert result.message == "No valid Google authentication cookies found in chrome."
    else:
        assert "missing SID" in result.message
        assert "No valid Google authentication cookies found" in result.message


def test_chromium_final_rebuild_routes_through_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = MagicMock(browser="chrome", human_name="Default", directory_name="Default")
    account = Account(0, "one@example.com", True, "Default")
    module = MagicMock()
    module.read_chromium_profile_cookies.return_value = [{"cookie": "raw"}]
    monkeypatch.setattr(chromium_accounts, "_chromium_profiles_module", lambda: module)
    monkeypatch.setattr(chromium_accounts, "_enumerate_one_jar", lambda *_, **__: [account])
    projected = Account(0, "one@example.com", True, "Default")
    projector = MagicMock(return_value=projected)
    monkeypatch.setattr(chromium_accounts, "project_browser_account", projector)

    result = chromium_accounts._enumerate_chromium_profiles_fanout(
        RecordingLoginIO(),
        "chrome",
        [profile],
        verbose=False,
        include_domains=None,
    )
    assert not isinstance(result, StaleCookies)
    assert result[1] == [projected]
    projector.assert_called_once_with(account, browser_profile="Default", is_default=True)


def test_chromium_projector_base_exception_scrubs_fanout_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = MagicMock(browser="chrome", human_name="Default", directory_name="Default")
    account = Account(0, "secret@example.com", True, "Default")
    raw = [{"name": "SID", "value": "COOKIE-SENTINEL"}]
    module = MagicMock()
    module.read_chromium_profile_cookies.return_value = raw
    failure = _DirectBaseException("projector failed")
    monkeypatch.setattr(chromium_accounts, "_chromium_profiles_module", lambda: module)
    monkeypatch.setattr(chromium_accounts, "_enumerate_one_jar", lambda *_, **__: [account])
    monkeypatch.setattr(
        chromium_accounts,
        "project_browser_account",
        MagicMock(side_effect=failure),
    )

    with pytest.raises(_DirectBaseException) as caught:
        chromium_accounts._enumerate_chromium_profiles_fanout(
            RecordingLoginIO(),
            "chrome",
            [profile],
            verbose=False,
            include_domains=None,
        )
    assert caught.value is failure
    current = failure.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_name == "_enumerate_chromium_profiles_fanout":
            for name in (
                "raw",
                "per_profile_cookies",
                "accounts",
                "account",
                "profile",
                "project_account",
                "enumerate_jar",
                "read_cookies",
            ):
                assert name not in current.tb_frame.f_locals
        current = current.tb_next


@pytest.mark.parametrize("failure", [_DirectBaseException("base"), KeyboardInterrupt()])
def test_browser_dispatch_failure_scrubs_cookie_and_capability_frame(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    raw = [{"name": "SID", "value": "COOKIE-SENTINEL"}]
    module = MagicMock()
    module.is_chromium_browser.return_value = False
    monkeypatch.setattr(browser_accounts, "_chromium_profiles_module", lambda: module)
    monkeypatch.setattr(browser_accounts, "_read_browser_cookies", lambda *_, **__: raw)
    monkeypatch.setattr(
        browser_accounts,
        "_enumerate_one_jar",
        MagicMock(side_effect=failure),
    )

    with pytest.raises(BaseException) as caught:
        browser_accounts._enumerate_browser_accounts("firefox", io=RecordingLoginIO())
    assert caught.value is failure
    current = failure.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_name == "_enumerate_browser_accounts":
            for name in (
                "cookies_result",
                "raw_cookies",
                "result",
                "enum_result",
                "enumerate_jar",
                "read_browser",
                "fanout",
            ):
                assert name not in current.tb_frame.f_locals
        current = current.tb_next


def test_import_callback_base_exception_scrubs_app_and_cli_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failure = _DirectBaseException("filter failed")
    monkeypatch.setattr(
        _cookie_import,
        "filter_storage_state_cookies_by_domain_policy",
        MagicMock(side_effect=failure),
    )
    payload = [_row("SID"), _row("__Secure-1PSIDTS"), _row("OSID")]

    with pytest.raises(_DirectBaseException) as caught:
        _cookie_import._import_cookie_json(
            payload=payload,
            storage_path=tmp_path / "state.json",
            include_domains=set(),
            include_optional=False,
        )
    assert caught.value is failure
    current = failure.__traceback__
    while current is not None:
        if current.tb_frame.f_globals.get("__name__") in {
            "notebooklm._app.login_cookie",
            "notebooklm.cli._cookie_import",
        }:
            for name in (
                "request",
                "payload",
                "normalized_state",
                "filter_storage_state",
                "replace_from_login",
                "result",
            ):
                assert name not in current.tb_frame.f_locals
        current = current.tb_next
