"""Tests for the T5.G cookie-domain blast-radius split.

Pins the contract that ``REQUIRED_COOKIE_DOMAINS`` is the default
extraction + runtime-gate set and ``OPTIONAL_COOKIE_DOMAINS_BY_LABEL`` is
the opt-in surface for ``--include-domains`` on ``notebooklm login`` /
``notebooklm auth refresh`` / ``notebooklm auth inspect``.

These tests are split out from ``test_auth.py`` because they cross the
auth/cli boundary — the contracts only make sense as a set.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from notebooklm.auth import (
    ALLOWED_COOKIE_DOMAINS,
    OPTIONAL_COOKIE_DOMAINS,
    OPTIONAL_COOKIE_DOMAINS_BY_LABEL,
    REQUIRED_COOKIE_DOMAINS,
    _is_allowed_cookie_domain,
    convert_rookiepy_cookies_to_storage_state,
)
from notebooklm.cli.session import (
    _build_google_cookie_domains,
    _parse_include_domains,
    _resolve_optional_cookie_domains,
)
from notebooklm.notebooklm_cli import cli


class TestRequiredVsOptional:
    """REQUIRED is empirically justified; OPTIONAL is opt-in only."""

    def test_required_set_includes_core_auth_domains(self):
        """Codex caution: host + dotted variants must both stay in REQUIRED."""
        for domain in (
            ".google.com",
            "google.com",
            ".notebooklm.google.com",
            "notebooklm.google.com",
            ".googleusercontent.com",
            "accounts.google.com",
            ".accounts.google.com",
            "drive.google.com",
            ".drive.google.com",
        ):
            assert domain in REQUIRED_COOKIE_DOMAINS, (
                f"{domain!r} must remain in REQUIRED_COOKIE_DOMAINS; the "
                "runtime gate uses this exact-match set and dropping a "
                "variant breaks http.cookiejar normalization round-trips."
            )

    def test_required_excludes_youtube(self):
        """YouTube is OPTIONAL, not REQUIRED."""
        for domain in (".youtube.com", "youtube.com", "accounts.youtube.com"):
            assert domain not in REQUIRED_COOKIE_DOMAINS

    def test_required_excludes_other_optional_siblings(self):
        """Docs / myaccount / mail are OPTIONAL too."""
        for domain in (
            "docs.google.com",
            "myaccount.google.com",
            "mail.google.com",
        ):
            assert domain not in REQUIRED_COOKIE_DOMAINS

    def test_optional_label_keys_are_lowercase(self):
        """``--include-domains`` lowercases input; the labels must too."""
        for label in OPTIONAL_COOKIE_DOMAINS_BY_LABEL:
            assert label == label.lower(), f"label {label!r} is not lowercase"

    def test_allowed_is_union(self):
        """``ALLOWED_COOKIE_DOMAINS`` is the back-compat union."""
        assert ALLOWED_COOKIE_DOMAINS == REQUIRED_COOKIE_DOMAINS | OPTIONAL_COOKIE_DOMAINS


class TestRuntimeGate:
    """The runtime gate consults REQUIRED only, not the full union."""

    def test_runtime_gate_rejects_youtube_by_default(self):
        """T5.G regression: stored cookies on ``.youtube.com`` are not trusted.

        This is the load-bearing security boundary. If a storage_state.json
        with a YouTube cookie is loaded into the auth jar, the runtime gate
        rejects it; the cookie is never sent to notebooklm.google.com.
        """
        assert _is_allowed_cookie_domain(".youtube.com") is False
        assert _is_allowed_cookie_domain("youtube.com") is False
        assert _is_allowed_cookie_domain("accounts.youtube.com") is False
        assert _is_allowed_cookie_domain(".accounts.youtube.com") is False

    def test_runtime_gate_accepts_required_set(self):
        """Every REQUIRED domain passes the runtime gate (exact-match tier)."""
        for domain in REQUIRED_COOKIE_DOMAINS:
            assert _is_allowed_cookie_domain(domain) is True, (
                f"{domain!r} must pass the runtime gate (it's in REQUIRED)"
            )

    def test_runtime_gate_accepts_google_subdomain_optional_siblings(self):
        """Docs / myaccount / Mail still pass via the .google.com suffix tier.

        Document that the blast-radius reduction is materially narrower
        than the OPTIONAL set: only ``youtube.com`` is actively rejected.
        """
        for domain in (
            "docs.google.com",
            ".docs.google.com",
            "myaccount.google.com",
            ".myaccount.google.com",
            "mail.google.com",
            ".mail.google.com",
        ):
            assert _is_allowed_cookie_domain(domain) is True


class TestBuildGoogleCookieDomains:
    """``_build_google_cookie_domains`` is the rookiepy/Firefox feeder."""

    def test_default_returns_required_only(self):
        """Default call returns REQUIRED + regional ccTLDs (no OPTIONAL).

        This is what changes the rookiepy extraction surface — narrowing
        what we ask the browser for at login time.
        """
        domains = set(_build_google_cookie_domains())
        # Every REQUIRED domain present.
        assert REQUIRED_COOKIE_DOMAINS.issubset(domains)
        # No OPTIONAL domain present.
        assert not (OPTIONAL_COOKIE_DOMAINS & domains)

    def test_include_optional_returns_union(self):
        """``include_optional=True`` restores the pre-T5.G broad set."""
        domains = set(_build_google_cookie_domains(include_optional=True))
        assert REQUIRED_COOKIE_DOMAINS.issubset(domains)
        assert OPTIONAL_COOKIE_DOMAINS.issubset(domains)

    def test_include_domains_label_subset(self):
        """``include_domains={'youtube'}`` adds YouTube only — not docs/mail."""
        domains = set(_build_google_cookie_domains(include_domains={"youtube"}))
        assert REQUIRED_COOKIE_DOMAINS.issubset(domains)
        assert OPTIONAL_COOKIE_DOMAINS_BY_LABEL["youtube"].issubset(domains)
        # Docs / mail / myaccount are NOT pulled in.
        assert OPTIONAL_COOKIE_DOMAINS_BY_LABEL["docs"].isdisjoint(domains)
        assert OPTIONAL_COOKIE_DOMAINS_BY_LABEL["mail"].isdisjoint(domains)
        assert OPTIONAL_COOKIE_DOMAINS_BY_LABEL["myaccount"].isdisjoint(domains)

    def test_include_domains_multi_label(self):
        """``include_domains={'youtube', 'docs'}`` is the union of both."""
        domains = set(_build_google_cookie_domains(include_domains={"youtube", "docs"}))
        assert OPTIONAL_COOKIE_DOMAINS_BY_LABEL["youtube"].issubset(domains)
        assert OPTIONAL_COOKIE_DOMAINS_BY_LABEL["docs"].issubset(domains)
        # mail / myaccount NOT pulled in.
        assert OPTIONAL_COOKIE_DOMAINS_BY_LABEL["mail"].isdisjoint(domains)

    def test_include_domains_all_shortcut(self):
        """``include_domains={'all'}`` is equivalent to every label."""
        domains = set(_build_google_cookie_domains(include_domains={"all"}))
        for optional_set in OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values():
            assert optional_set.issubset(domains)


class TestParseIncludeDomains:
    """``_parse_include_domains`` accepts both flag forms and rejects unknowns."""

    def test_empty_returns_empty(self):
        assert _parse_include_domains(()) == set()

    def test_single_label(self):
        assert _parse_include_domains(("youtube",)) == {"youtube"}

    def test_multiple_flag_invocations(self):
        """``--include-domains=youtube --include-domains=docs``."""
        assert _parse_include_domains(("youtube", "docs")) == {"youtube", "docs"}

    def test_comma_separated_single_invocation(self):
        """``--include-domains=youtube,docs`` (the codex-flagged form)."""
        assert _parse_include_domains(("youtube,docs",)) == {"youtube", "docs"}

    def test_mixed_comma_and_repeated(self):
        """Mix of both forms in the same call."""
        labels = _parse_include_domains(("youtube,docs", "mail"))
        assert labels == {"youtube", "docs", "mail"}

    def test_whitespace_around_commas_tolerated(self):
        assert _parse_include_domains(("youtube , docs",)) == {"youtube", "docs"}

    def test_lowercased(self):
        assert _parse_include_domains(("YouTube,DOCS",)) == {"youtube", "docs"}

    def test_empty_fragments_dropped(self):
        assert _parse_include_domains(("youtube,,docs,",)) == {"youtube", "docs"}

    def test_unknown_label_raises(self):
        """Click surfaces this as a non-zero exit + stderr message."""
        import click

        with pytest.raises(click.BadParameter) as exc_info:
            _parse_include_domains(("zoom",))
        assert "zoom" in str(exc_info.value)
        # Help text lists the supported labels.
        assert "youtube" in str(exc_info.value)

    def test_all_label_accepted(self):
        assert _parse_include_domains(("all",)) == {"all"}


class TestResolveOptionalCookieDomains:
    """``_resolve_optional_cookie_domains`` flattens labels to a domain set."""

    def test_empty_labels(self):
        assert _resolve_optional_cookie_domains(set()) == frozenset()

    def test_single_label_resolves(self):
        result = _resolve_optional_cookie_domains({"youtube"})
        assert result == OPTIONAL_COOKIE_DOMAINS_BY_LABEL["youtube"]

    def test_all_label_returns_full_optional_set(self):
        assert _resolve_optional_cookie_domains({"all"}) == OPTIONAL_COOKIE_DOMAINS

    def test_all_takes_precedence_when_combined(self):
        """``--include-domains=all,youtube`` resolves to the full OPTIONAL set."""
        assert _resolve_optional_cookie_domains({"all", "youtube"}) == OPTIONAL_COOKIE_DOMAINS


class TestBlastRadiusExtractor:
    """Extractor actively excludes YouTube unless opted in (T5.G regression).

    The plan calls for a "blast-radius regression test" that asserts the
    storage_state.json artefact contains no YouTube cookies after a
    default-flag login. We exercise the conversion path directly because
    that is where the runtime allowlist is applied — it does not depend
    on rookiepy nor Playwright being installed.
    """

    def _raw_cookie(self, domain: str, name: str) -> dict:
        return {
            "domain": domain,
            "name": name,
            "value": "v",
            "path": "/",
            "secure": True,
            "expires": None,
            "http_only": False,
        }

    def test_default_extraction_drops_youtube_cookies(self):
        """Default login does not persist YouTube cookies, even if rookiepy yields them.

        Reads a fake rookiepy output with both a Google auth cookie and a
        YouTube cookie; converts to storage_state.json via the same code
        path login uses; asserts only the Google auth cookie survives.
        """
        google_domain = ".google.com"
        youtube_domain = ".youtube.com"
        raw = [
            # Google auth cookie (REQUIRED domain).
            self._raw_cookie(google_domain, "SID"),
            # YouTube cookie that rookiepy would have returned if the user
            # passed --include-domains=youtube at the previous run.
            self._raw_cookie(youtube_domain, "LOGIN_INFO"),
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        # Use frozenset-difference form to keep CodeQL's
        # py/incomplete-url-substring-sanitization heuristic happy — it
        # flags ``"<literal>" in container`` even when the container is
        # set-membership not URL parsing (same defensive pattern as
        # ``test_auth.TestAllowedCookieDomains``).
        kept_domains = frozenset(c["domain"] for c in result["cookies"])
        assert frozenset({google_domain}) <= kept_domains
        assert kept_domains.isdisjoint({youtube_domain})
        # Specifically the named YouTube cookie is gone.
        assert not any(c["name"] == "LOGIN_INFO" for c in result["cookies"])

    def test_opt_in_then_default_does_not_leak_youtube(self):
        """After ``--include-domains=youtube`` then a default re-login, no YouTube.

        Simulates the codex-flagged "user opted in once, then forgot
        and re-ran default" case. The extractor must actively exclude on
        re-run, not just default to a smaller request set.
        """
        youtube_variants = frozenset({".youtube.com", "youtube.com"})

        # First run with --include-domains=youtube: YouTube cookies extracted.
        domains_optin = frozenset(_build_google_cookie_domains(include_domains={"youtube"}))
        # Set-intersection form sidesteps CodeQL's substring-sanitization
        # heuristic (same reason as the test above).
        assert youtube_variants & domains_optin

        # Second run with default: YouTube is NOT in the extraction list.
        domains_default = frozenset(_build_google_cookie_domains())
        assert domains_default.isdisjoint(youtube_variants)

        # And conversion would drop a YouTube cookie even if it somehow
        # arrived from a previous run.
        raw = [self._raw_cookie(".youtube.com", "LOGIN_INFO")]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert result["cookies"] == []


class TestTokenVerificationStillWorksAfterMinimumSet:
    """Token-verification regression test (load-bearing per T5.G plan).

    Asserts that a storage_state.json built from the minimum REQUIRED set
    still satisfies the auth-jar construction path — i.e. the same flow
    ``fetch_tokens_with_domains`` uses. We stop before any network call
    because the unit-test suite must run offline; instead we assert that
    the storage state passes the upstream validators that ``fetch_tokens``
    relies on.
    """

    def test_minimum_required_storage_state_validates(self, tmp_path: Path):
        """A storage_state built from REQUIRED-only cookies passes ``extract_cookies_from_storage``.

        ``extract_cookies_from_storage`` is the validator
        ``fetch_tokens_with_domains`` calls before making any network
        request. If REQUIRED is too narrow, this validation raises
        ``ValueError`` and token-fetch never even starts.
        """
        from notebooklm.auth import extract_cookies_from_storage

        # Minimum cookies that a real login must produce. SID is the
        # canonical guard (MINIMUM_REQUIRED_COOKIES). The companion auth
        # cookies (HSID/SSID/APISID/SAPISID) live on .google.com, fully
        # within REQUIRED.
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "v_sid", "domain": ".google.com"},
                {"name": "HSID", "value": "v_hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "v_ssid", "domain": ".google.com"},
                {"name": "APISID", "value": "v_apisid", "domain": ".google.com"},
                {"name": "SAPISID", "value": "v_sapisid", "domain": ".google.com"},
                # Notebooklm session cookie on the API host.
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "v_1psidts",
                    "domain": ".google.com",
                },
            ],
            "origins": [],
        }

        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "v_sid"
        assert cookies["HSID"] == "v_hsid"

    def test_minimum_required_set_round_trips_through_load_httpx_cookies(self, tmp_path: Path):
        """The httpx jar (used by downloads + refresh) is non-empty for REQUIRED-only state."""
        from notebooklm.auth import load_httpx_cookies

        storage_state = {
            "cookies": [
                {"name": "SID", "value": "v_sid", "domain": ".google.com"},
                {"name": "HSID", "value": "v_hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "v_ssid", "domain": ".google.com"},
                {"name": "APISID", "value": "v_apisid", "domain": ".google.com"},
                {"name": "SAPISID", "value": "v_sapisid", "domain": ".google.com"},
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "v_1psidts",
                    "domain": ".google.com",
                },
            ],
            "origins": [],
        }
        storage_file = tmp_path / "storage.json"
        storage_file.write_text(json.dumps(storage_state))

        jar = load_httpx_cookies(path=storage_file)
        assert jar.get("SID", domain=".google.com") == "v_sid"


class TestLoginCliFlag:
    """``notebooklm login --include-domains`` plumbs through to the extractor."""

    def test_login_help_advertises_include_domains(self):
        """``login --help`` mentions the new flag and the supported labels."""
        runner = CliRunner()
        result = runner.invoke(cli, ["login", "--help"])
        assert result.exit_code == 0
        assert "--include-domains" in result.output
        assert "youtube" in result.output

    def test_login_rejects_unknown_include_domain_label(self, monkeypatch):
        """Unknown labels are surfaced as a click error (non-zero exit + stderr)."""
        # Block the env-var conflict guard from firing.
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "login",
                "--browser-cookies",
                "chrome",
                "--include-domains",
                "zoom",
            ],
        )
        assert result.exit_code != 0
        # Click renders BadParameter to stderr-style output captured in result.output.
        assert "zoom" in result.output or "zoom" in (result.stderr or "")

    def test_login_include_domains_forwards_to_extractor(self, monkeypatch):
        """``--include-domains=youtube`` is parsed and threaded to login helper."""
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        runner = CliRunner()

        with patch("notebooklm.cli.session._login_browser_cookies_single") as login_single:
            result = runner.invoke(
                cli,
                [
                    "login",
                    "--browser-cookies",
                    "chrome",
                    "--include-domains",
                    "youtube,docs",
                ],
            )

        assert result.exit_code == 0, result.output
        login_single.assert_called_once()
        kwargs = login_single.call_args.kwargs
        assert kwargs["include_domains"] == {"youtube", "docs"}

    def test_login_default_no_include_domains_emits_migration_note(self, monkeypatch):
        """Default browser-cookies login prints the migration note."""
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        runner = CliRunner()

        with patch("notebooklm.cli.session._login_browser_cookies_single"):
            result = runner.invoke(
                cli,
                ["login", "--browser-cookies", "chrome"],
            )

        assert result.exit_code == 0, result.output
        assert "sibling-product cookies not included" in result.output

    def test_login_with_include_domains_suppresses_migration_note(self, monkeypatch):
        """The migration note is suppressed once the user opts in."""
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        runner = CliRunner()

        with patch("notebooklm.cli.session._login_browser_cookies_single"):
            result = runner.invoke(
                cli,
                [
                    "login",
                    "--browser-cookies",
                    "chrome",
                    "--include-domains",
                    "all",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "sibling-product cookies not included" not in result.output

    def test_login_include_domains_without_browser_cookies_warns(self, monkeypatch, tmp_path):
        """``--include-domains`` on the Playwright path is a no-op — warn.

        Claude review feedback: a user who runs ``notebooklm login
        --include-domains=youtube`` (no ``--browser-cookies``) gets no
        signal that the flag did nothing. Mirror the symmetric warning
        ``--fresh`` already prints when combined with ``--browser-cookies``.
        """
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        runner = CliRunner()

        # Stub Playwright import to break out before any real browser launch.
        # We only care that the warning is printed before the Playwright path
        # is attempted, so we let the import fail (no playwright in scope)
        # and the function will SystemExit afterwards.
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            result = runner.invoke(cli, ["login", "--include-domains", "youtube"])

        assert "--include-domains has no effect without --browser-cookies" in result.output


class TestAuthRefreshCliFlag:
    """``notebooklm auth refresh --include-domains`` follows the same plumbing."""

    def test_refresh_include_domains_requires_browser_cookies(self, monkeypatch, tmp_path):
        """``--include-domains`` without ``--browser-cookies`` is rejected."""
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        # A storage file has to exist so refresh doesn't bail on FileNotFound
        # before reaching the validation.
        (tmp_path / "storage_state.json").write_text("{}")
        runner = CliRunner()

        result = runner.invoke(cli, ["auth", "refresh", "--include-domains", "youtube"])
        assert result.exit_code != 0
        assert "--browser-cookies" in result.output

    def test_refresh_include_domains_forwards_when_browser_cookies_set(self, monkeypatch, tmp_path):
        """``--include-domains`` is forwarded to the browser-refresh helper."""
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        runner = CliRunner()

        with patch("notebooklm.cli.session._refresh_from_browser_cookies") as helper:
            result = runner.invoke(
                cli,
                [
                    "auth",
                    "refresh",
                    "--browser-cookies",
                    "chrome",
                    "--include-domains",
                    "youtube",
                ],
            )

        assert result.exit_code == 0, result.output
        helper.assert_called_once()
        assert helper.call_args.kwargs["include_domains"] == {"youtube"}


class TestAuthInspectCliFlag:
    """``notebooklm auth inspect --include-domains`` thread-through."""

    def test_inspect_help_advertises_include_domains(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["auth", "inspect", "--help"])
        assert result.exit_code == 0
        assert "--include-domains" in result.output

    def test_inspect_include_domains_forwarded_to_enumerate(self):
        """``--include-domains=youtube`` reaches ``_enumerate_browser_accounts``."""
        runner = CliRunner()

        with patch("notebooklm.cli.session._enumerate_browser_accounts") as enum:
            enum.return_value = ([], [])
            result = runner.invoke(
                cli,
                [
                    "auth",
                    "inspect",
                    "--browser",
                    "chrome",
                    "--include-domains",
                    "youtube",
                    "--json",
                ],
            )

        assert result.exit_code == 0, result.output
        enum.assert_called_once()
        assert enum.call_args.kwargs["include_domains"] == {"youtube"}
