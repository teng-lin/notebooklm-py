"""Unit tests for the Firefox-family cookie helper error branches.

``_read_firefox_container_cookies`` maps every extractor failure to a
friendly message + ``exit_with_code(1)`` (raised as ``SystemExit``). These
tests pin the three terminal error handlers and the early return in
``_maybe_warn_firefox_containers_in_use`` when no profile is found.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from notebooklm.cli.services.login import firefox_accounts


def _fake_containers_module(profile_path, *, extract_side_effect=None):
    """Build a stand-in for the ``_firefox_containers`` module."""
    mod = MagicMock()
    mod.find_firefox_profile_path.return_value = profile_path
    mod.resolve_container_id.return_value = "none"
    if extract_side_effect is not None:
        mod.extract_firefox_container_cookies.side_effect = extract_side_effect
    return mod


class TestReadFirefoxContainerCookiesErrors:
    def test_file_not_found_exits(self, tmp_path):
        mod = _fake_containers_module(
            tmp_path, extract_side_effect=FileNotFoundError("no cookies.sqlite")
        )
        with (
            patch.object(firefox_accounts, "_firefox_containers_module", return_value=mod),
            patch.object(firefox_accounts, "console") as console,
            pytest.raises(SystemExit) as exc,
        ):
            firefox_accounts._read_firefox_container_cookies("none", verbose=False)
        assert exc.value.code == 1
        assert any("no cookies.sqlite" in str(c) for c in console.print.call_args_list)

    def test_oserror_routes_through_rookiepy_handler(self, tmp_path):
        mod = _fake_containers_module(tmp_path, extract_side_effect=OSError("database is locked"))
        with (
            patch.object(firefox_accounts, "_firefox_containers_module", return_value=mod),
            patch.object(firefox_accounts, "console") as console,
            pytest.raises(SystemExit) as exc,
        ):
            firefox_accounts._read_firefox_container_cookies("none", verbose=False)
        assert exc.value.code == 1
        # The locked-DB message from _handle_rookiepy_error is surfaced.
        printed = " ".join(str(c) for c in console.print.call_args_list)
        assert "database is locked" in printed

    def test_runtime_error_routes_through_rookiepy_handler(self, tmp_path):
        mod = _fake_containers_module(
            tmp_path, extract_side_effect=RuntimeError("totally unexpected")
        )
        with (
            patch.object(firefox_accounts, "_firefox_containers_module", return_value=mod),
            patch.object(firefox_accounts, "console"),
            pytest.raises(SystemExit) as exc,
        ):
            firefox_accounts._read_firefox_container_cookies("none", verbose=False)
        assert exc.value.code == 1

    def test_sqlite_database_error_exits(self, tmp_path):
        mod = _fake_containers_module(
            tmp_path, extract_side_effect=sqlite3.DatabaseError("malformed db")
        )
        with (
            patch.object(firefox_accounts, "_firefox_containers_module", return_value=mod),
            patch.object(firefox_accounts, "console") as console,
            pytest.raises(SystemExit) as exc,
        ):
            firefox_accounts._read_firefox_container_cookies("none", verbose=False)
        assert exc.value.code == 1
        printed = " ".join(str(c) for c in console.print.call_args_list)
        assert "malformed db" in printed

    def test_success_returns_cookies(self, tmp_path):
        mod = _fake_containers_module(tmp_path)
        mod.extract_firefox_container_cookies.return_value = [{"name": "SID"}]
        with (
            patch.object(firefox_accounts, "_firefox_containers_module", return_value=mod),
            patch.object(firefox_accounts, "console"),
            patch.object(
                firefox_accounts, "_build_google_cookie_domains", return_value=[".google.com"]
            ),
        ):
            cookies = firefox_accounts._read_firefox_container_cookies("none", verbose=False)
        assert cookies == [{"name": "SID"}]


class TestMaybeWarnFirefoxContainersInUse:
    def test_no_profile_returns_silently(self):
        mod = MagicMock()
        mod.find_firefox_profile_path.return_value = None
        with (
            patch.object(firefox_accounts, "_firefox_containers_module", return_value=mod),
            patch.object(firefox_accounts, "console") as console,
        ):
            firefox_accounts._maybe_warn_firefox_containers_in_use()
        mod.has_container_cookies_in_use.assert_not_called()
        console.print.assert_not_called()

    def test_warns_when_container_cookies_in_use(self, tmp_path):
        mod = MagicMock()
        mod.find_firefox_profile_path.return_value = tmp_path
        mod.has_container_cookies_in_use.return_value = True
        with (
            patch.object(firefox_accounts, "_firefox_containers_module", return_value=mod),
            patch.object(firefox_accounts, "console") as console,
        ):
            firefox_accounts._maybe_warn_firefox_containers_in_use()
        console.print.assert_called_once()
        assert "Multi-Account Container" in console.print.call_args[0][0]
