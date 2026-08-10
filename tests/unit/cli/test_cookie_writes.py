"""Unit tests for the cookie-write + account-selection helpers.

Covers the failure-outcome and nonfatal-warning branches of
``_select_account``, ``_select_refresh_account``, and
``_write_extracted_cookies`` that the CLI-level flows don't otherwise
exercise directly.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx

from notebooklm._auth.storage import LoginWriteOutcome, LoginWriteStatus
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
        io = make_recording_io()

        out = cookie_writes._select_account(io, [first, second], account_email=None)

        assert out is first
        assert len(io.emitted) == 1
        assert "did not mark a default" in io.emitted[0]


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
    # Domains matter: the write-time filter and the post-filter Tier-1
    # recheck both run on this state, so the required cookies must sit on
    # an allowlisted domain to exercise the success paths.
    return {
        "cookies": [
            {"name": "SID", "value": "x", "domain": ".google.com", "path": "/"},
            {"name": "__Secure-1PSIDTS", "value": "y", "domain": ".google.com", "path": "/"},
        ]
    }


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
            patch.object(cookie_writes, "replace_from_login", side_effect=OSError("disk full")),
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

    def test_lock_unavailable_returns_outcome(self, tmp_path):
        """A fail-closed ``lock_unavailable`` writer outcome surfaces as a
        STORAGE_WRITE_FAILED failure (nothing written)."""
        storage_path = tmp_path / "storage_state.json"
        with (
            patch.object(
                cookie_writes, "validate_with_recovery", return_value=(_ok_storage(), None)
            ),
            patch.object(
                cookie_writes,
                "replace_from_login",
                return_value=LoginWriteOutcome(LoginWriteStatus.LOCK_UNAVAILABLE),
            ),
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
        assert "lock unavailable" in out.message

    def test_verification_value_error_warns(self, tmp_path):
        storage_path = tmp_path / "storage_state.json"
        io = make_recording_io(run_async=MagicMock(side_effect=ValueError("bad token")))
        with (
            patch.object(
                cookie_writes, "validate_with_recovery", return_value=(_ok_storage(), None)
            ),
            patch.object(cookie_writes, "fetch_tokens_with_domains", MagicMock()),
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
        assert any("failed verification" in message for message in io.emitted)

    def test_verification_network_error_warns(self, tmp_path):
        storage_path = tmp_path / "storage_state.json"
        io = make_recording_io(run_async=MagicMock(side_effect=httpx.ConnectError("offline")))
        with (
            patch.object(
                cookie_writes, "validate_with_recovery", return_value=(_ok_storage(), None)
            ),
            patch.object(cookie_writes, "fetch_tokens_with_domains", MagicMock()),
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
        assert any("could not verify" in message for message in io.emitted)

    def test_happy_path_returns_none(self, tmp_path):
        storage_path = tmp_path / "storage_state.json"
        io = make_recording_io(run_async=MagicMock())
        with (
            patch.object(
                cookie_writes, "validate_with_recovery", return_value=(_ok_storage(), None)
            ),
            patch.object(cookie_writes, "fetch_tokens_with_domains", MagicMock()),
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
        assert io.emitted == []
        # The real writer landed the state (account embedded in the same write).
        stored = json.loads(storage_path.read_text())
        assert {c["name"] for c in stored["cookies"]} == {"SID", "__Secure-1PSIDTS"}
        assert stored["notebooklm"]["account"] == {"authuser": 0, "email": "a@gmail.com"}


# ---------------------------------------------------------------------------
# _write_extracted_cookies — write-time cookie-domain filtering
# ---------------------------------------------------------------------------


def _rookiepy_cookie(domain: str, name: str) -> dict:
    """A rookiepy-shaped raw cookie row (matches the extractor output)."""
    return {
        "domain": domain,
        "name": name,
        "value": "v",
        "path": "/",
        "secure": True,
        "expires": None,
        "http_only": False,
    }


def _sibling_heavy_jar() -> list[dict]:
    """Required auth cookies plus sibling-product rows.

    Mirrors what the Firefox container extractor can hand the writer: its
    suffix-based domain match (rookiepy semantics: dot-prefixed = suffix
    match) returns cookies for every ``*.google.com`` subdomain even when
    the extraction request contained only ``REQUIRED_COOKIE_DOMAINS``.
    """
    return [
        _rookiepy_cookie(".google.com", "SID"),
        _rookiepy_cookie(".google.com", "HSID"),
        _rookiepy_cookie(".google.com", "__Secure-1PSIDTS"),
        _rookiepy_cookie("notebooklm.google.com", "OSID"),
        # Google-hosted siblings are retained by the compatibility-first
        # trusted-root policy; YouTube remains an explicit opt-in.
        _rookiepy_cookie("mail.google.com", "MAIL_OSID"),
        _rookiepy_cookie(".mail.google.com", "COMPASS"),
        _rookiepy_cookie("docs.google.com", "DOCS_OSID"),
        _rookiepy_cookie("myaccount.google.com", "ACCOUNT_OSID"),
        _rookiepy_cookie(".youtube.com", "YOUTUBE_SID"),
    ]


class TestWriteExtractedCookiesDomainFilter:
    """Write-time filtering parity with the Playwright path.

    Trusted Google roots use compatibility-first suffix matching so unknown
    functional hosts survive. Unrelated domains and non-Google optional
    products remain excluded unless explicitly requested.
    """

    def _write(self, tmp_path, raw, **kwargs):
        """Run the real validate → filter → write pipeline; return the persisted state."""
        storage_path = tmp_path / "storage_state.json"
        io = make_recording_io(run_async=MagicMock())
        with patch.object(cookie_writes, "fetch_tokens_with_domains", MagicMock()):
            out = cookie_writes._write_extracted_cookies(
                io,
                raw,
                storage_path=storage_path,
                profile=None,
                authuser=0,
                email="a@gmail.com",
                **kwargs,
            )
        assert out is None
        return json.loads(storage_path.read_text())

    def test_default_keeps_google_subdomains_but_drops_youtube(self, tmp_path):
        """Google-hosted siblings survive; the distinct YouTube root does not."""
        persisted = self._write(tmp_path, _sibling_heavy_jar())
        names = {c["name"] for c in persisted["cookies"]}
        assert {
            "SID",
            "HSID",
            "__Secure-1PSIDTS",
            "OSID",
            "MAIL_OSID",
            "COMPASS",
            "DOCS_OSID",
            "ACCOUNT_OSID",
        } <= names
        assert "YOUTUBE_SID" not in names

    def test_opt_in_label_persists_youtube(self, tmp_path):
        """``--include-domains=youtube`` adds the distinct YouTube root."""
        persisted = self._write(tmp_path, _sibling_heavy_jar(), include_domains={"youtube"})
        names = {c["name"] for c in persisted["cookies"]}
        assert {"SID", "__Secure-1PSIDTS", "YOUTUBE_SID"} <= names

    def test_opt_in_all_persists_every_sibling(self, tmp_path):
        """``--include-domains=all`` keeps every optional-label cookie."""
        persisted = self._write(tmp_path, _sibling_heavy_jar(), include_domains={"all"})
        names = {c["name"] for c in persisted["cookies"]}
        assert {"MAIL_OSID", "COMPASS", "DOCS_OSID", "ACCOUNT_OSID", "YOUTUBE_SID"} <= names

    def test_regional_cctld_cookies_survive(self, tmp_path):
        """Regional ``.google.<ccTLD>`` variants pass the write-time filter."""
        raw = [
            _rookiepy_cookie(".google.com", "SID"),
            _rookiepy_cookie(".google.com", "__Secure-1PSIDTS"),
            _rookiepy_cookie("accounts.google.co.uk", "REG_UK"),
            _rookiepy_cookie("accounts.google.com.hk", "REG_HK"),
            _rookiepy_cookie(".google.de", "REG_DE"),
        ]
        persisted = self._write(tmp_path, raw)
        names = {c["name"] for c in persisted["cookies"]}
        assert {"SID", "__Secure-1PSIDTS", "REG_UK", "REG_HK", "REG_DE"} <= names

    def test_functional_drive_and_download_domains_survive(self, tmp_path):
        """Drive-ingest and media-download domains pass the write-time filter.

        ``drive.google.com`` (both dotted variants) and
        ``.googleusercontent.com`` are in ``REQUIRED_COOKIE_DOMAINS`` —
        Drive-source ingest follows redirects through drive.google.com and
        artifact media downloads hit ``*.googleusercontent.com`` — so the
        write-time policy must never drop them. Pins the functional-domain
        survival so a future policy edit cannot silently break Drive ingest
        or downloads.
        """
        raw = [
            _rookiepy_cookie(".google.com", "SID"),
            _rookiepy_cookie(".google.com", "__Secure-1PSIDTS"),
            _rookiepy_cookie("drive.google.com", "DRIVE_HOST"),
            _rookiepy_cookie(".drive.google.com", "DRIVE_DOT"),
            _rookiepy_cookie("drive.usercontent.google.com", "DRIVE_DOWNLOAD"),
            _rookiepy_cookie(".googleusercontent.com", "GUC_DOMAIN"),
            _rookiepy_cookie("lh3.googleusercontent.com", "GUC_HOST"),
        ]
        persisted = self._write(tmp_path, raw)
        names = {c["name"] for c in persisted["cookies"]}
        assert {
            "SID",
            "__Secure-1PSIDTS",
            "DRIVE_HOST",
            "DRIVE_DOT",
            "DRIVE_DOWNLOAD",
            "GUC_DOMAIN",
            "GUC_HOST",
        } <= names

    def test_host_scoped_googleusercontent_subdomain_survives(self, tmp_path):
        """Host-scoped Googleusercontent rows follow the trusted-root policy."""
        raw = [
            _rookiepy_cookie(".google.com", "SID"),
            _rookiepy_cookie(".google.com", "__Secure-1PSIDTS"),
            _rookiepy_cookie(".googleusercontent.com", "GUC_DOMAIN"),
            _rookiepy_cookie("lh3.googleusercontent.com", "GUC_HOST"),
        ]
        persisted = self._write(tmp_path, raw)
        names = {c["name"] for c in persisted["cookies"]}
        assert "GUC_DOMAIN" in names
        assert "GUC_HOST" in names
        # Exact-match the host-scoped domain (not substring membership) so the
        # trusted-subdomain row is asserted precisely; CodeQL flags
        # ``"host" in <domain-collection>`` as incomplete URL sanitization.
        assert any(
            cookie["domain"] == "lh3.googleusercontent.com" for cookie in persisted["cookies"]
        )

    def test_required_cookie_only_on_filtered_domain_fails_before_write(self, tmp_path):
        """Filtering away a required cookie fails loudly instead of writing.

        The converter accepts opted-in runtime domains, so a jar whose only
        ``SID`` sits on ``youtube.com`` passes ``validate_with_recovery``;
        the write-time filter then drops that row. The post-filter Tier-1
        recheck must return a validation failure (and write nothing) rather
        than persisting unusable auth with a success exit.
        """
        storage_path = tmp_path / "storage_state.json"
        io = make_recording_io(run_async=MagicMock())
        raw = [
            _rookiepy_cookie(".youtube.com", "SID"),
            _rookiepy_cookie(".google.com", "__Secure-1PSIDTS"),
        ]
        with patch.object(cookie_writes, "fetch_tokens_with_domains", MagicMock()):
            out = cookie_writes._write_extracted_cookies(
                io,
                raw,
                storage_path=storage_path,
                profile=None,
                authuser=0,
                email="a@gmail.com",
            )
        assert isinstance(out, CookieValidationFailure)
        assert out.code == "COOKIE_VALIDATION_FAILED"
        assert "SID" in out.message
        assert not storage_path.exists()
