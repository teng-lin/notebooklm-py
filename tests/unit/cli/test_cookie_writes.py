"""Unit tests for the cookie-write + account-selection helpers.

Covers the failure-outcome and nonfatal-warning branches of
``_select_account``, ``_select_refresh_account``, and
``_write_extracted_cookies`` that the CLI-level flows don't otherwise
exercise directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from notebooklm.cli.services.login import cookie_writes
from notebooklm.cli.services.login.outcomes import CookieValidationFailure
from tests._fixtures.login_io import RecordingLoginIO, make_recording_io


def test_emit_warning_emits_through_injected_sink():
    """``_emit_warning`` routes the message through the injected ``io`` sink."""
    io = RecordingLoginIO()
    cookie_writes._emit_warning(io, "[yellow]heads up[/yellow]")
    assert io.emitted == ["[yellow]heads up[/yellow]"]


class _Acct:
    def __init__(self, authuser, email, is_default):
        self.authuser = authuser
        self.email = email
        self.is_default = is_default


# ---------------------------------------------------------------------------
# _select_account
# ---------------------------------------------------------------------------


class TestSelectAccount:
    def test_no_accounts_returns_failure(self):
        out = cookie_writes._select_account(make_recording_io(), [], account_email=None)
        assert isinstance(out, CookieValidationFailure)
        assert out.code == "NO_ACCOUNTS_FOUND"

    def test_email_match_returns_account(self):
        acct = _Acct(0, "Match@Gmail.com", True)
        out = cookie_writes._select_account(
            make_recording_io(), [acct], account_email="match@gmail.com"
        )
        assert out is acct

    def test_email_not_found_returns_failure_listing_available(self):
        acct = _Acct(0, "other@gmail.com", True)
        out = cookie_writes._select_account(
            make_recording_io(), [acct], account_email="missing@gmail.com"
        )
        assert isinstance(out, CookieValidationFailure)
        assert out.code == "ACCOUNT_NOT_FOUND"
        assert "other@gmail.com" in out.message

    def test_default_account_returned_without_email(self):
        first = _Acct(0, "a@gmail.com", False)
        default = _Acct(1, "b@gmail.com", True)
        out = cookie_writes._select_account(
            make_recording_io(), [first, default], account_email=None
        )
        assert out is default

    def test_no_default_marker_warns_and_returns_first(self):
        first = _Acct(0, "a@gmail.com", False)
        second = _Acct(1, "b@gmail.com", False)
        with patch.object(cookie_writes, "_emit_warning") as emit:
            out = cookie_writes._select_account(
                make_recording_io(), [first, second], account_email=None
            )
        assert out is first
        emit.assert_called_once()
        # ``_emit_warning`` now takes ``(io, message)`` — message is arg index 1.
        assert "did not mark a default" in emit.call_args[0][1]


# ---------------------------------------------------------------------------
# _select_refresh_account
# ---------------------------------------------------------------------------


class TestSelectRefreshAccount:
    def test_no_accounts_returns_failure(self):
        out = cookie_writes._select_refresh_account([], {}, "chrome")
        assert isinstance(out, CookieValidationFailure)
        assert out.code == "NO_ACCOUNTS_FOUND"
        assert "chrome" in out.message

    def test_email_match_wins(self):
        acct = _Acct(0, "user@gmail.com", True)
        out = cookie_writes._select_refresh_account([acct], {"email": "user@gmail.com"}, "chrome")
        assert out is acct

    def test_email_present_but_missing_returns_failure(self):
        acct = _Acct(0, "other@gmail.com", True)
        out = cookie_writes._select_refresh_account([acct], {"email": "user@gmail.com"}, "chrome")
        assert isinstance(out, CookieValidationFailure)
        assert out.code == "PROFILE_ACCOUNT_MISSING"
        assert "other@gmail.com" in out.message

    def test_authuser_match_when_no_email(self):
        acct = _Acct(3, "x@gmail.com", False)
        out = cookie_writes._select_refresh_account([acct], {"authuser": 3}, "chrome")
        assert out is acct

    def test_authuser_stale_returns_failure(self):
        acct = _Acct(0, "x@gmail.com", True)
        out = cookie_writes._select_refresh_account([acct], {"authuser": 9}, "chrome")
        assert isinstance(out, CookieValidationFailure)
        assert out.code == "PROFILE_ACCOUNT_MISSING"
        assert "old account route" in out.message

    def test_no_metadata_falls_back_to_default(self):
        first = _Acct(0, "a@gmail.com", False)
        default = _Acct(1, "b@gmail.com", True)
        out = cookie_writes._select_refresh_account([first, default], {}, "chrome")
        assert out is default

    def test_no_metadata_no_default_falls_back_to_first(self):
        first = _Acct(0, "a@gmail.com", False)
        second = _Acct(1, "b@gmail.com", False)
        out = cookie_writes._select_refresh_account([first, second], {}, "chrome")
        assert out is first


# ---------------------------------------------------------------------------
# _write_extracted_cookies
# ---------------------------------------------------------------------------


def _ok_storage():
    return {"cookies": [{"name": "SID", "value": "x"}]}


class TestWriteExtractedCookies:
    # The cookie-verification probe now runs through the injected ``io``
    # sink's ``run_async`` (#1393), so each test supplies a ``RecordingLoginIO``
    # whose ``run_async`` is the stub that previously patched
    # ``notebooklm.cli.runtime.run_async``. The ``_emit_warning`` helper now
    # takes ``(io, message)``, so warning assertions read ``c.args[1]``.
    def test_validation_failure_returns_outcome(self, tmp_path):
        storage_path = tmp_path / "storage_state.json"
        with patch.object(
            cookie_writes,
            "validate_with_recovery",
            return_value=({"cookies": []}, ValueError("missing SID")),
        ):
            out = cookie_writes._write_extracted_cookies(
                make_recording_io(),
                [],
                storage_path=storage_path,
                profile=None,
                authuser=0,
                email="a@gmail.com",
            )
        assert isinstance(out, CookieValidationFailure)
        assert out.code == "COOKIE_VALIDATION_FAILED"
        assert "missing SID" in out.message

    def test_disk_write_failure_returns_outcome(self, tmp_path):
        storage_path = tmp_path / "storage_state.json"
        with (
            patch.object(
                cookie_writes, "validate_with_recovery", return_value=(_ok_storage(), None)
            ),
            patch.object(cookie_writes, "atomic_write_json", side_effect=OSError("disk full")),
        ):
            out = cookie_writes._write_extracted_cookies(
                make_recording_io(),
                [{"name": "SID"}],
                storage_path=storage_path,
                profile=None,
                authuser=0,
                email="a@gmail.com",
            )
        assert isinstance(out, CookieValidationFailure)
        assert out.code == "STORAGE_WRITE_FAILED"
        assert "disk full" in out.message

    def test_metadata_write_failure_is_nonfatal(self, tmp_path):
        storage_path = tmp_path / "storage_state.json"
        io = make_recording_io(run_async=MagicMock())
        with (
            patch.object(
                cookie_writes, "validate_with_recovery", return_value=(_ok_storage(), None)
            ),
            patch.object(cookie_writes, "atomic_write_json"),
            patch.object(cookie_writes, "fetch_tokens_with_domains", MagicMock()),
            patch("notebooklm.auth.write_account_metadata", side_effect=OSError("ro fs")),
            patch.object(cookie_writes, "_emit_warning") as emit,
        ):
            out = cookie_writes._write_extracted_cookies(
                io,
                [{"name": "SID"}],
                storage_path=storage_path,
                profile=None,
                authuser=0,
                email="a@gmail.com",
            )
        # Cookies were written; metadata failure only warns.
        assert out is None
        assert any("metadata write failed" in c.args[1] for c in emit.call_args_list)

    def test_verification_value_error_warns(self, tmp_path):
        storage_path = tmp_path / "storage_state.json"
        io = make_recording_io(run_async=MagicMock(side_effect=ValueError("bad token")))
        with (
            patch.object(
                cookie_writes, "validate_with_recovery", return_value=(_ok_storage(), None)
            ),
            patch.object(cookie_writes, "atomic_write_json"),
            patch.object(cookie_writes, "fetch_tokens_with_domains", MagicMock()),
            patch("notebooklm.auth.write_account_metadata"),
            patch.object(cookie_writes, "_emit_warning") as emit,
        ):
            out = cookie_writes._write_extracted_cookies(
                io,
                [{"name": "SID"}],
                storage_path=storage_path,
                profile=None,
                authuser=0,
                email="a@gmail.com",
            )
        assert out is None
        assert any("failed verification" in c.args[1] for c in emit.call_args_list)

    def test_verification_network_error_warns(self, tmp_path):
        storage_path = tmp_path / "storage_state.json"
        io = make_recording_io(run_async=MagicMock(side_effect=httpx.ConnectError("offline")))
        with (
            patch.object(
                cookie_writes, "validate_with_recovery", return_value=(_ok_storage(), None)
            ),
            patch.object(cookie_writes, "atomic_write_json"),
            patch.object(cookie_writes, "fetch_tokens_with_domains", MagicMock()),
            patch("notebooklm.auth.write_account_metadata"),
            patch.object(cookie_writes, "_emit_warning") as emit,
        ):
            out = cookie_writes._write_extracted_cookies(
                io,
                [{"name": "SID"}],
                storage_path=storage_path,
                profile=None,
                authuser=0,
                email="a@gmail.com",
            )
        assert out is None
        assert any("could not verify" in c.args[1] for c in emit.call_args_list)

    def test_happy_path_returns_none(self, tmp_path):
        storage_path = tmp_path / "storage_state.json"
        io = make_recording_io(run_async=MagicMock())
        with (
            patch.object(
                cookie_writes, "validate_with_recovery", return_value=(_ok_storage(), None)
            ),
            patch.object(cookie_writes, "atomic_write_json"),
            patch.object(cookie_writes, "fetch_tokens_with_domains", MagicMock()),
            patch("notebooklm.auth.write_account_metadata"),
            patch.object(cookie_writes, "_emit_warning") as emit,
        ):
            out = cookie_writes._write_extracted_cookies(
                io,
                [{"name": "SID"}],
                storage_path=storage_path,
                profile=None,
                authuser=0,
                email="a@gmail.com",
            )
        assert out is None
        emit.assert_not_called()
