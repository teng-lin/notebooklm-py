"""Coverage-focused unit tests for ``cli/services/login/refresh.py``.
These tests target failure / edge branches not exercised by the broader
``test_login.py`` / ``test_login_multi_account.py`` suites:
* ``_login_browser_cookies_single`` — targeted-extraction write-outcome exit.
* ``_login_all_accounts_from_browser`` — enumeration-outcome exit, the
  no-accounts early return, and the per-account write-outcome exit (lines
  231, 234-235, 275).
* ``_refresh_from_browser_cookies`` — enumeration-outcome exit, the
  no-accounts exit, and the write-outcome exit.
* ``_login_with_browser_cookies`` — the OSError save path, the
  account-metadata clear/write branches, and each cookie-verification
  failure branch.
Collaborators are patched at their ``refresh`` module import sites so each
driver runs in isolation without a real browser / network.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

import notebooklm.auth as auth_module
import notebooklm.cli.playwright_login_io as playwright_login_io_module
from notebooklm.cli.services.login import refresh
from notebooklm.cli.services.login.outcomes import BrowserCookieOutcome

REFRESH = "notebooklm.cli.services.login.refresh"
# The async bridge is no longer ``refresh.run_async`` (#1393 inverted it behind
# the injected ``LoginIO`` sink). With no ``io`` injected these drivers resolve
# the command-layer default sink (``PlaywrightLoginIO``), whose ``run_async``
# binds ``cli.playwright_login_io.run_async``. Patching it here intercepts the
# async probe while leaving ``emit`` → ``console.print`` intact so ``capsys``
# still captures the rendered warning lines.


def _account(email: str, *, authuser: int = 0, browser_profile: str = "Default") -> Any:
    return SimpleNamespace(email=email, authuser=authuser, browser_profile=browser_profile)


def _outcome(message: str = "[red]boom[/red]") -> BrowserCookieOutcome:
    """Build a concrete failure outcome instance."""
    obj = BrowserCookieOutcome.__new__(BrowserCookieOutcome)
    object.__setattr__(obj, "code", "TEST_FAILURE")
    object.__setattr__(obj, "message", message)
    return obj


def _deps(**overrides: Any) -> refresh.RefreshDeps:
    return replace(refresh.default_refresh_deps(), **overrides)


def _login_base_deps(**overrides: Any) -> refresh.RefreshDeps:
    storage_state = {"cookies": [{"name": "SID"}], "origins": []}
    return _deps(
        read_browser_cookies=MagicMock(return_value=["raw"]),
        validate_with_recovery=MagicMock(return_value=(storage_state, None)),
        cookie_names_from_storage=MagicMock(return_value=["SID"]),
        missing_cookies_hint=MagicMock(return_value="hint"),
        sync_server_language_to_config=MagicMock(),
        fetch_tokens_with_domains=MagicMock(return_value=None),
        **overrides,
    )


# ---------------------------------------------------------------------------
# _login_browser_cookies_single — targeted write-outcome exit
# ---------------------------------------------------------------------------
def test_login_single_enum_outcome_exits(tmp_path) -> None:
    """An enumeration outcome in the targeted path exits 1."""
    deps = _deps(enumerate_browser_accounts=MagicMock(return_value=_outcome()))
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_browser_cookies_single(
            "chrome",
            storage=None,
            account_email="bob@example.com",
            profile_name=None,
            active_profile="work",
            deps=deps,
        )
    assert exc_info.value.code == 1


def test_login_single_targeted_write_outcome_exits(tmp_path) -> None:
    """A write-outcome from the targeted extraction path exits 1."""
    account = _account("bob@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=(per_profile, [account])),
        select_account=MagicMock(return_value=account),
        confirm_profile_account_overwrite=MagicMock(),
        write_extracted_cookies=MagicMock(return_value=_outcome()),
        get_storage_path=MagicMock(return_value=tmp_path / "s.json"),
    )
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_browser_cookies_single(
            "chrome",
            storage=None,
            account_email="bob@example.com",
            profile_name=None,
            active_profile="work",
            deps=deps,
        )
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _login_all_accounts_from_browser — enum, no-accounts, and write-outcome paths
# ---------------------------------------------------------------------------
def test_login_all_accounts_enum_outcome_exits() -> None:
    """An enumeration outcome exits 1."""
    deps = _deps(enumerate_browser_accounts=MagicMock(return_value=_outcome()))
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_all_accounts_from_browser("chrome", deps=deps)
    assert exc_info.value.code == 1


def test_login_all_accounts_no_accounts_returns(capsys) -> None:
    """No discovered accounts returns early with a notice."""
    deps = _deps(enumerate_browser_accounts=MagicMock(return_value=({}, [])))
    refresh._login_all_accounts_from_browser("chrome", deps=deps)
    out = capsys.readouterr().out
    assert "No accounts discovered" in out


def test_login_all_accounts_write_outcome_exits(tmp_path) -> None:
    """A per-account write outcome exits 1."""
    account = _account("alice@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=(per_profile, [account])),
        list_profiles=MagicMock(return_value=[]),
        profiles_by_account_email=MagicMock(return_value={}),
        resolve_all_accounts_target=MagicMock(return_value="alice"),
        email_to_profile_name=MagicMock(return_value="alice"),
        get_storage_path=MagicMock(return_value=tmp_path / "alice.json"),
        write_extracted_cookies=MagicMock(return_value=_outcome()),
    )
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_all_accounts_from_browser("chrome", deps=deps)
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _refresh_from_browser_cookies — enum, no-accounts, and write-outcome paths
# ---------------------------------------------------------------------------
def test_refresh_enum_outcome_exits(tmp_path) -> None:
    """An enumeration outcome exits 1."""
    deps = _deps(enumerate_browser_accounts=MagicMock(return_value=_outcome()))
    with pytest.raises(SystemExit) as exc_info:
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=True, deps=deps
        )
    assert exc_info.value.code == 1


def test_refresh_no_accounts_exits(tmp_path, capsys) -> None:
    """No signed-in accounts exits 1."""
    deps = _deps(enumerate_browser_accounts=MagicMock(return_value=({}, [])))
    with pytest.raises(SystemExit) as exc_info:
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=True, deps=deps
        )
    assert exc_info.value.code == 1
    assert "No signed-in Google accounts" in capsys.readouterr().out


def test_refresh_select_outcome_exits(tmp_path) -> None:
    """A select-refresh-account outcome exits 1."""
    account = _account("carol@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=(per_profile, [account])),
        read_account_metadata=MagicMock(return_value={}),
        select_refresh_account=MagicMock(return_value=_outcome()),
    )
    with pytest.raises(SystemExit) as exc_info:
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=True, deps=deps
        )
    assert exc_info.value.code == 1


def test_refresh_success_prints_summary(tmp_path, capsys) -> None:
    """A successful non-quiet refresh prints the ok/account summary."""
    account = _account("carol@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=(per_profile, [account])),
        read_account_metadata=MagicMock(return_value={}),
        select_refresh_account=MagicMock(return_value=account),
        write_extracted_cookies=MagicMock(return_value=None),
        sync_server_language_to_config=MagicMock(),
    )
    refresh._refresh_from_browser_cookies(
        "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=False, deps=deps
    )
    out = capsys.readouterr().out
    assert "refreshed from chrome" in out
    assert "carol@example.com" in out


def test_refresh_write_outcome_exits(tmp_path) -> None:
    """A write outcome exits 1."""
    account = _account("carol@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=(per_profile, [account])),
        read_account_metadata=MagicMock(return_value={}),
        select_refresh_account=MagicMock(return_value=account),
        write_extracted_cookies=MagicMock(return_value=_outcome()),
    )
    with pytest.raises(SystemExit) as exc_info:
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=True, deps=deps
        )
    assert exc_info.value.code == 1


def test_login_with_cookies_read_outcome_exits(tmp_path) -> None:
    """An outcome from ``_read_browser_cookies`` exits 1."""
    deps = _deps(read_browser_cookies=MagicMock(return_value=_outcome()))
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome", deps=deps)
    assert exc_info.value.code == 1


def test_login_with_cookies_validation_error_exits(tmp_path, capsys) -> None:
    """A validation error from ``validate_with_recovery`` exits 1."""
    deps = _deps(
        read_browser_cookies=MagicMock(return_value=["raw"]),
        validate_with_recovery=MagicMock(
            return_value=({"cookies": []}, "missing required cookies")
        ),
        cookie_names_from_storage=MagicMock(return_value=[]),
        missing_cookies_hint=MagicMock(return_value="install hint"),
    )
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome", deps=deps)
    assert exc_info.value.code == 1
    assert "No valid Google authentication cookies" in capsys.readouterr().out


def test_login_with_cookies_save_oserror_exits(tmp_path) -> None:
    """An OSError while writing storage exits 1."""
    deps = _login_base_deps(atomic_write_json=MagicMock(side_effect=OSError("disk full")))
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_with_browser_cookies(tmp_path / "out" / "storage.json", "chrome", deps=deps)
    assert exc_info.value.code == 1


def test_login_with_cookies_write_metadata_oserror_warns(tmp_path, capsys) -> None:
    """A write_account_metadata OSError warns but does not exit."""
    deps = _login_base_deps(atomic_write_json=MagicMock())
    with (
        patch.object(
            auth_module,
            "write_account_metadata",
            side_effect=OSError("metadata write fail"),
        ),
        patch.object(playwright_login_io_module, "run_async"),
    ):
        refresh._login_with_browser_cookies(
            tmp_path / "storage.json",
            "chrome",
            authuser=1,
            email="x@example.com",
            deps=deps,
        )
    out = capsys.readouterr().out
    assert "account metadata write failed" in out


def test_login_with_cookies_clear_metadata_oserror_logged(tmp_path, caplog) -> None:
    """A clear_account_metadata OSError on a default login is logged."""
    import logging

    deps = _login_base_deps(atomic_write_json=MagicMock())
    with (
        patch.object(auth_module, "clear_account_metadata", side_effect=OSError("clear fail")),
        patch.object(playwright_login_io_module, "run_async"),
        caplog.at_level(logging.WARNING, logger=REFRESH),
    ):
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome", deps=deps)
    assert any(
        "Failed to clear stale account metadata" in rec.getMessage() for rec in caplog.records
    )


def test_login_with_cookies_account_line_printed(tmp_path, capsys) -> None:
    """When an email is provided the Account: line is printed."""
    deps = _login_base_deps(atomic_write_json=MagicMock())
    with (
        patch.object(auth_module, "write_account_metadata"),
        patch.object(playwright_login_io_module, "run_async"),
    ):
        refresh._login_with_browser_cookies(
            tmp_path / "storage.json",
            "chrome",
            authuser=2,
            email="dave@example.com",
            deps=deps,
        )
    out = capsys.readouterr().out
    assert "dave@example.com" in out


def test_login_with_cookies_verify_valueerror_warns(tmp_path, capsys) -> None:
    """A ValueError from verification warns but does not exit."""
    deps = _login_base_deps(atomic_write_json=MagicMock())
    with (
        patch.object(auth_module, "clear_account_metadata"),
        patch.object(
            playwright_login_io_module,
            "run_async",
            side_effect=ValueError("invalid cookies"),
        ),
    ):
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome", deps=deps)
    out = capsys.readouterr().out
    assert "failed validation" in out


def test_login_with_cookies_verify_network_error_warns(tmp_path, capsys) -> None:
    """A network RequestError warns but does not exit."""
    deps = _login_base_deps(atomic_write_json=MagicMock())
    with (
        patch.object(auth_module, "clear_account_metadata"),
        patch.object(
            playwright_login_io_module,
            "run_async",
            side_effect=httpx.RequestError("connect failed"),
        ),
    ):
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome", deps=deps)
    out = capsys.readouterr().out
    assert "network issue" in out


def test_login_with_cookies_verify_unexpected_error_warns(tmp_path, capsys) -> None:
    """An unexpected error warns but does not exit."""
    deps = _login_base_deps(atomic_write_json=MagicMock())
    with (
        patch.object(auth_module, "clear_account_metadata"),
        patch.object(
            playwright_login_io_module,
            "run_async",
            side_effect=RuntimeError("boom"),
        ),
    ):
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome", deps=deps)
    out = capsys.readouterr().out
    assert "Unexpected error during verification" in out
