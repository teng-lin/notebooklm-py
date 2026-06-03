"""Coverage-focused unit tests for ``cli/services/login/refresh.py``.

These tests target failure / edge branches not exercised by the broader
``test_login.py`` / ``test_login_multi_account.py`` suites:

* ``_login_browser_cookies_single`` — targeted-extraction write-outcome exit
  (line 154).
* ``_login_all_accounts_from_browser`` — enumeration-outcome exit, the
  no-accounts early return, and the per-account write-outcome exit (lines
  231, 234-235, 275).
* ``_refresh_from_browser_cookies`` — enumeration-outcome exit, the
  no-accounts exit, and the write-outcome exit (lines 297, 300-301, 317).
* ``_login_with_browser_cookies`` — the OSError save path, the
  account-metadata clear/write branches, and each cookie-verification
  failure branch (lines 376-379, 385-391, 400-405, 413-433).

Collaborators are patched at their ``refresh`` module import sites so each
driver runs in isolation without a real browser / network.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from notebooklm.cli.services.login import refresh
from notebooklm.cli.services.login.outcomes import BrowserCookieOutcome

REFRESH = "notebooklm.cli.services.login.refresh"
# The async bridge is no longer ``refresh.run_async`` (#1393 inverted it behind
# the injected ``LoginIO`` sink). With no ``io`` injected these drivers resolve
# the command-layer default sink (``PlaywrightLoginIO``), whose ``run_async``
# binds ``cli.playwright_login_io.run_async``. Patching it here intercepts the
# async probe while leaving ``emit`` → ``console.print`` intact so ``capsys``
# still captures the rendered warning lines.
IO_RUN_ASYNC = "notebooklm.cli.playwright_login_io.run_async"


def _account(email: str, *, authuser: int = 0, browser_profile: str = "Default") -> Any:
    return SimpleNamespace(email=email, authuser=authuser, browser_profile=browser_profile)


def _outcome(message: str = "[red]boom[/red]") -> BrowserCookieOutcome:
    """Build a concrete failure outcome instance."""
    obj = BrowserCookieOutcome.__new__(BrowserCookieOutcome)
    object.__setattr__(obj, "code", "TEST_FAILURE")
    object.__setattr__(obj, "message", message)
    return obj


# ---------------------------------------------------------------------------
# _login_browser_cookies_single — targeted write-outcome exit (line 154)
# ---------------------------------------------------------------------------


def test_login_single_enum_outcome_exits(tmp_path) -> None:
    """An enumeration outcome in the targeted path exits 1 (line 123)."""
    with (
        patch(f"{REFRESH}._enumerate_browser_accounts", return_value=_outcome()),
        patch(f"{REFRESH}.get_storage_path", return_value=tmp_path / "s.json"),
        pytest.raises(SystemExit) as exc_info,
    ):
        refresh._login_browser_cookies_single(
            "chrome",
            storage=None,
            account_email="bob@example.com",
            profile_name=None,
            active_profile="work",
        )
    assert exc_info.value.code == 1


def test_login_single_targeted_write_outcome_exits(tmp_path) -> None:
    """A write-outcome from the targeted extraction path exits 1 (line 154)."""
    account = _account("bob@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}

    with (
        patch(
            f"{REFRESH}._enumerate_browser_accounts",
            return_value=(per_profile, [account]),
        ),
        patch(f"{REFRESH}._select_account", return_value=account),
        patch(f"{REFRESH}._confirm_profile_account_overwrite"),
        patch(f"{REFRESH}._write_extracted_cookies", return_value=_outcome()),
        patch(f"{REFRESH}.get_storage_path", return_value=tmp_path / "s.json"),
        pytest.raises(SystemExit) as exc_info,
    ):
        refresh._login_browser_cookies_single(
            "chrome",
            storage=None,
            account_email="bob@example.com",
            profile_name=None,
            active_profile="work",
        )
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _login_all_accounts_from_browser — 231, 234-235, 275
# ---------------------------------------------------------------------------


def test_login_all_accounts_enum_outcome_exits() -> None:
    """An enumeration outcome exits 1 (line 231)."""
    with (
        patch(f"{REFRESH}._enumerate_browser_accounts", return_value=_outcome()),
        pytest.raises(SystemExit) as exc_info,
    ):
        refresh._login_all_accounts_from_browser("chrome")
    assert exc_info.value.code == 1


def test_login_all_accounts_no_accounts_returns(capsys) -> None:
    """No discovered accounts returns early with a notice (lines 234-235)."""
    with (
        patch(f"{REFRESH}._enumerate_browser_accounts", return_value=({}, [])),
        # list_profiles is imported lazily inside the function; patch source.
        patch("notebooklm.paths.list_profiles", return_value=[]),
    ):
        refresh._login_all_accounts_from_browser("chrome")
    out = capsys.readouterr().out
    assert "No accounts discovered" in out


def test_login_all_accounts_write_outcome_exits(tmp_path) -> None:
    """A per-account write outcome exits 1 (line 275)."""
    account = _account("alice@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}

    with (
        patch(
            f"{REFRESH}._enumerate_browser_accounts",
            return_value=(per_profile, [account]),
        ),
        patch("notebooklm.paths.list_profiles", return_value=[]),
        patch(f"{REFRESH}._profiles_by_account_email", return_value={}),
        patch(f"{REFRESH}._resolve_all_accounts_target", return_value="alice"),
        patch(f"{REFRESH}.email_to_profile_name", return_value="alice"),
        patch(f"{REFRESH}.get_storage_path", return_value=tmp_path / "alice.json"),
        patch(f"{REFRESH}._write_extracted_cookies", return_value=_outcome()),
        pytest.raises(SystemExit) as exc_info,
    ):
        refresh._login_all_accounts_from_browser("chrome")
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _refresh_from_browser_cookies — 297, 300-301, 317
# ---------------------------------------------------------------------------


def test_refresh_enum_outcome_exits(tmp_path) -> None:
    """An enumeration outcome exits 1 (line 297)."""
    with (
        patch(f"{REFRESH}._enumerate_browser_accounts", return_value=_outcome()),
        pytest.raises(SystemExit) as exc_info,
    ):
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=True
        )
    assert exc_info.value.code == 1


def test_refresh_no_accounts_exits(tmp_path, capsys) -> None:
    """No signed-in accounts exits 1 (lines 300-301)."""
    with (
        patch(f"{REFRESH}._enumerate_browser_accounts", return_value=({}, [])),
        pytest.raises(SystemExit) as exc_info,
    ):
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=True
        )
    assert exc_info.value.code == 1
    assert "No signed-in Google accounts" in capsys.readouterr().out


def test_refresh_select_outcome_exits(tmp_path) -> None:
    """A select-refresh-account outcome exits 1 (line 306)."""
    account = _account("carol@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}

    with (
        patch(
            f"{REFRESH}._enumerate_browser_accounts",
            return_value=(per_profile, [account]),
        ),
        patch(f"{REFRESH}.read_account_metadata", return_value={}),
        patch(f"{REFRESH}._select_refresh_account", return_value=_outcome()),
        pytest.raises(SystemExit) as exc_info,
    ):
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=True
        )
    assert exc_info.value.code == 1


def test_refresh_success_prints_summary(tmp_path, capsys) -> None:
    """A successful non-quiet refresh prints the ok/account summary (318-321)."""
    account = _account("carol@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}

    with (
        patch(
            f"{REFRESH}._enumerate_browser_accounts",
            return_value=(per_profile, [account]),
        ),
        patch(f"{REFRESH}.read_account_metadata", return_value={}),
        patch(f"{REFRESH}._select_refresh_account", return_value=account),
        patch(f"{REFRESH}._write_extracted_cookies", return_value=None),
        patch(f"{REFRESH}._sync_server_language_to_config"),
    ):
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=False
        )
    out = capsys.readouterr().out
    assert "refreshed from chrome" in out
    assert "carol@example.com" in out


def test_refresh_write_outcome_exits(tmp_path) -> None:
    """A write outcome exits 1 (line 317)."""
    account = _account("carol@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}

    with (
        patch(
            f"{REFRESH}._enumerate_browser_accounts",
            return_value=(per_profile, [account]),
        ),
        patch(f"{REFRESH}.read_account_metadata", return_value={}),
        patch(f"{REFRESH}._select_refresh_account", return_value=account),
        patch(f"{REFRESH}._write_extracted_cookies", return_value=_outcome()),
        pytest.raises(SystemExit) as exc_info,
    ):
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=True
        )
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _login_with_browser_cookies — save / metadata / verification branches
# ---------------------------------------------------------------------------


def _enter_login_base(stack: ExitStack) -> None:
    """Enter common patches: valid cookies + happy validation, neutral deps."""
    storage_state = {"cookies": [{"name": "SID"}], "origins": []}
    stack.enter_context(patch(f"{REFRESH}._read_browser_cookies", return_value=["raw"]))
    stack.enter_context(
        patch(f"{REFRESH}.validate_with_recovery", return_value=(storage_state, None))
    )
    stack.enter_context(patch(f"{REFRESH}.cookie_names_from_storage", return_value=["SID"]))
    stack.enter_context(patch(f"{REFRESH}.missing_cookies_hint", return_value="hint"))
    stack.enter_context(patch(f"{REFRESH}._sync_server_language_to_config"))
    # ``run_async`` is patched by callers, so the awaitable built from
    # ``fetch_tokens_with_domains`` would never be awaited. Replace it with a
    # plain (non-async) MagicMock so no orphan coroutine is created.
    stack.enter_context(
        patch(f"{REFRESH}.fetch_tokens_with_domains", new=MagicMock(return_value=None))
    )


def test_login_with_cookies_read_outcome_exits(tmp_path) -> None:
    """An outcome from ``_read_browser_cookies`` exits 1 (line 349)."""
    with (
        patch(f"{REFRESH}._read_browser_cookies", return_value=_outcome()),
        pytest.raises(SystemExit) as exc_info,
    ):
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome")
    assert exc_info.value.code == 1


def test_login_with_cookies_validation_error_exits(tmp_path, capsys) -> None:
    """A validation error from ``validate_with_recovery`` exits 1 (line 349)."""
    with (
        patch(f"{REFRESH}._read_browser_cookies", return_value=["raw"]),
        patch(
            f"{REFRESH}.validate_with_recovery",
            return_value=({"cookies": []}, "missing required cookies"),
        ),
        patch(f"{REFRESH}.cookie_names_from_storage", return_value=[]),
        patch(f"{REFRESH}.missing_cookies_hint", return_value="install hint"),
        pytest.raises(SystemExit) as exc_info,
    ):
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome")
    assert exc_info.value.code == 1
    assert "No valid Google authentication cookies" in capsys.readouterr().out


def test_login_with_cookies_save_oserror_exits(tmp_path) -> None:
    """An OSError while writing storage exits 1 (lines 376-379)."""
    with ExitStack() as stack:
        _enter_login_base(stack)
        stack.enter_context(patch(f"{REFRESH}.atomic_write_json", side_effect=OSError("disk full")))
        with pytest.raises(SystemExit) as exc_info:
            refresh._login_with_browser_cookies(tmp_path / "out" / "storage.json", "chrome")
    assert exc_info.value.code == 1


def test_login_with_cookies_write_metadata_oserror_warns(tmp_path, capsys) -> None:
    """A write_account_metadata OSError warns but does not exit (385-391)."""
    with ExitStack() as stack:
        _enter_login_base(stack)
        stack.enter_context(patch(f"{REFRESH}.atomic_write_json"))
        stack.enter_context(
            patch(
                "notebooklm.auth.write_account_metadata",
                side_effect=OSError("metadata write fail"),
            )
        )
        stack.enter_context(patch(IO_RUN_ASYNC))
        refresh._login_with_browser_cookies(
            tmp_path / "storage.json", "chrome", authuser=1, email="x@example.com"
        )
    out = capsys.readouterr().out
    assert "account metadata write failed" in out


def test_login_with_cookies_clear_metadata_oserror_logged(tmp_path, caplog) -> None:
    """A clear_account_metadata OSError on a default login is logged (400-401)."""
    import logging

    with ExitStack() as stack:
        _enter_login_base(stack)
        stack.enter_context(patch(f"{REFRESH}.atomic_write_json"))
        stack.enter_context(
            patch("notebooklm.auth.clear_account_metadata", side_effect=OSError("clear fail"))
        )
        stack.enter_context(patch(IO_RUN_ASYNC))
        stack.enter_context(caplog.at_level(logging.WARNING, logger=REFRESH))
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome")
    assert any(
        "Failed to clear stale account metadata" in rec.getMessage() for rec in caplog.records
    )


def test_login_with_cookies_account_line_printed(tmp_path, capsys) -> None:
    """When an email is provided the Account: line is printed (line 405)."""
    with ExitStack() as stack:
        _enter_login_base(stack)
        stack.enter_context(patch(f"{REFRESH}.atomic_write_json"))
        stack.enter_context(patch("notebooklm.auth.write_account_metadata"))
        stack.enter_context(patch(IO_RUN_ASYNC))
        refresh._login_with_browser_cookies(
            tmp_path / "storage.json", "chrome", authuser=2, email="dave@example.com"
        )
    out = capsys.readouterr().out
    assert "dave@example.com" in out


def test_login_with_cookies_verify_valueerror_warns(tmp_path, capsys) -> None:
    """A ValueError from verification warns but does not exit (413-421)."""
    with ExitStack() as stack:
        _enter_login_base(stack)
        stack.enter_context(patch(f"{REFRESH}.atomic_write_json"))
        stack.enter_context(patch("notebooklm.auth.clear_account_metadata"))
        stack.enter_context(patch(IO_RUN_ASYNC, side_effect=ValueError("invalid cookies")))
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome")
    out = capsys.readouterr().out
    assert "failed validation" in out


def test_login_with_cookies_verify_network_error_warns(tmp_path, capsys) -> None:
    """A network RequestError warns but does not exit (422-429)."""
    with ExitStack() as stack:
        _enter_login_base(stack)
        stack.enter_context(patch(f"{REFRESH}.atomic_write_json"))
        stack.enter_context(patch("notebooklm.auth.clear_account_metadata"))
        stack.enter_context(patch(IO_RUN_ASYNC, side_effect=httpx.RequestError("connect failed")))
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome")
    out = capsys.readouterr().out
    assert "network issue" in out


def test_login_with_cookies_verify_unexpected_error_warns(tmp_path, capsys) -> None:
    """An unexpected error warns but does not exit (430-436)."""
    with ExitStack() as stack:
        _enter_login_base(stack)
        stack.enter_context(patch(f"{REFRESH}.atomic_write_json"))
        stack.enter_context(patch("notebooklm.auth.clear_account_metadata"))
        stack.enter_context(patch(IO_RUN_ASYNC, side_effect=RuntimeError("boom")))
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome")
    out = capsys.readouterr().out
    assert "Unexpected error during verification" in out
