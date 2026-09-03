"""Tests for the cookie-domain blast-radius split.

Pins the two-layer contract:

* ``REQUIRED_COOKIE_DOMAINS`` is the default *requested extraction* set fed
  to rookiepy. The write filter then retains boundary-matched trusted Google
  roots for compatibility, even when an extractor returns host-scoped
  subdomain cookies. Distinct roots such as YouTube require opt-in on
  ``notebooklm login`` / ``notebooklm auth refresh`` /
  ``notebooklm auth inspect``.
* The runtime gate consults the full ``REQUIRED ∪ OPTIONAL`` union so
  opted-in cookies survive every downstream filter
  (``convert_rookiepy_cookies_to_storage_state``,
  ``extract_cookies_with_domains``,
  ``build_httpx_cookies_from_storage``).

These tests are split out from ``test_auth.py`` because they cross the
auth/cli boundary — the contracts only make sense as a set.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from notebooklm._auth.cookie_policy import ALLOWED_COOKIE_DOMAINS
from notebooklm._auth.cookie_policy import (
    build_cookie_domain_allowlist as _neutral_build_cookie_domain_allowlist,
)
from notebooklm._auth.cookie_policy import (
    resolve_optional_cookie_domains as _neutral_resolve_optional_cookie_domains,
)
from notebooklm.auth import (
    OPTIONAL_COOKIE_DOMAINS,
    OPTIONAL_COOKIE_DOMAINS_BY_LABEL,
    REQUIRED_COOKIE_DOMAINS,
    _is_allowed_cookie_domain,
    convert_rookiepy_cookies_to_storage_state,
)
from notebooklm.cli.services.login import (
    _build_google_cookie_domains,
    _resolve_optional_cookie_domains,
)
from notebooklm.cli.session_cmd import _parse_include_domains
from notebooklm.notebooklm_cli import cli
from tests._fixtures import patch_session_login_dual


class TestNeutralBuilderMatchesCliBuilder:
    """Drift canary: the neutral cookie-domain builder in
    ``notebooklm._auth.cookie_policy`` (consumed by the Playwright
    browser-capture filter) must stay equivalent to the CLI extractor builder
    ``cli.services.login._build_google_cookie_domains`` (consumed by the
    rookiepy / Firefox paths). Both derive from the same shared constants; this
    pins that they never silently diverge, so the on-disk ``storage_state.json``
    cookie set is identical regardless of which login path wrote it.
    """

    @pytest.mark.parametrize(
        "include_optional, include_domains",
        [
            (False, None),
            (True, None),
            (False, set()),
            (False, {"youtube"}),
            (False, {"docs"}),
            (False, {"myaccount"}),
            (False, {"mail"}),
            (False, {"youtube", "docs"}),
            (False, {"youtube", "docs", "myaccount", "mail"}),
            (False, {"all"}),
        ],
    )
    def test_builders_produce_identical_domain_sets(self, include_optional, include_domains):
        cli_domains = _build_google_cookie_domains(
            include_optional=include_optional, include_domains=include_domains
        )
        neutral_domains = _neutral_build_cookie_domain_allowlist(
            include_optional=include_optional, include_domains=include_domains
        )
        assert cli_domains == neutral_domains
        assert cli_domains == sorted(cli_domains)

    @pytest.mark.parametrize(
        "labels",
        [set(), {"youtube"}, {"docs", "mail"}, {"all"}],
    )
    def test_optional_resolvers_match(self, labels):
        assert _resolve_optional_cookie_domains(labels) == (
            _neutral_resolve_optional_cookie_domains(labels)
        )


class TestWriteTimeFilterParity:
    """All login/import persist paths route through ONE writer that binds ONE filter.

    Before b-PR3 each rookiepy/Firefox writer imported its own module-level
    ``filter_storage_state_cookies_by_domain_policy`` binding and the pin asserted
    those bindings were identical. The write-time filter and post-filter
    required-cookie revalidation are now owned by ``ProfileStore.replace_from_login``;
    the three CLI writers
    (``cookie_writes._write_extracted_cookies``,
    ``refresh._login_with_browser_cookies``, ``_cookie_import._import_cookie_json``)
    all call the native path-shaped operation through the ``notebooklm.auth`` ledger. On-disk parity
    with the Playwright capture arms now follows from **writer-routing identity**
    (one writer, one filter) rather than from each module binding the same filter.
    """

    def test_all_login_paths_route_through_one_writer(self):
        """Routing identity: every login/import flow uses the native operation."""
        import notebooklm.auth as auth_module
        from notebooklm.cli import _cookie_import
        from notebooklm.cli.services.login import cookie_writes, refresh

        canonical = auth_module.replace_profile_from_login
        assert cookie_writes.replace_profile_from_login is canonical
        assert _cookie_import.replace_profile_from_login is canonical
        # The refresh driver reaches it through its injectable deps seam.
        assert refresh.default_refresh_deps().replace_profile_from_login is canonical

    def test_writer_binds_the_playwright_filter(self):
        """Identity pin: the filter the writer applies IS the filter the
        Playwright capture arms use (one filter, bound in one place now).

        Since ADR-0034 PR 7C the login owner is ``_auth/profile_store.py``, beside
        the path transaction; minted-session filtering remains in ``_auth/storage.py``.
        This is write-time policy, not browser code, so this asserts against
        the canonical home.
        """
        from notebooklm._auth import cookie_filter, storage
        from notebooklm._browser import browser_capture
        from notebooklm.auth import (
            filter_storage_state_cookies_by_domain_policy as playwright_filter,
        )

        canonical = cookie_filter.filter_storage_state_cookies_by_domain_policy
        assert storage._safe_cookie_shape is cookie_filter._safe_cookie_shape
        assert browser_capture._safe_cookie_shape is storage._safe_cookie_shape
        assert storage.filter_storage_state_cookies_by_domain_policy is canonical
        assert browser_capture.filter_storage_state_cookies_by_domain_policy is canonical
        assert playwright_filter is canonical

    @pytest.mark.parametrize("include_domains", [None, {"mail"}, {"all"}])
    def test_writer_persists_same_domain_set_as_playwright_filter(self, tmp_path, include_domains):
        """Behavioral pin: the on-disk cookie set ``replace_from_login`` persists
        equals what the Playwright write-time filter keeps, cookie-for-cookie.

        Because all three CLI writers call this one function, proving parity for
        the writer proves it for every login/import path.
        """
        from notebooklm.auth import (
            filter_storage_state_cookies_by_domain_policy as playwright_filter,
        )
        from notebooklm.auth import replace_from_login

        probe_domains = [
            ".google.com",
            "google.com",
            "notebooklm.google.com",
            "accounts.google.com",
            ".googleusercontent.com",
            ".google.co.uk",
            ".google.de",
            "mail.google.com",
            ".mail.google.com",
            "docs.google.com",
            "myaccount.google.com",
            ".youtube.com",
            "evil-google.com",
            ".not-youtube.com",
        ]
        # Required cookies on an allowlisted domain so the writer's post-filter
        # required-cookie revalidation does not short-circuit before the write.
        cookies = [
            {"name": "SID", "value": "v", "domain": ".google.com", "path": "/"},
            {"name": "__Secure-1PSIDTS", "value": "v", "domain": ".google.com", "path": "/"},
            *(
                {"name": f"C{i}", "value": "v", "domain": d, "path": "/"}
                for i, d in enumerate(probe_domains)
            ),
        ]
        state = {"cookies": cookies, "origins": []}

        storage_path = tmp_path / "storage_state.json"
        outcome = replace_from_login(storage_path, dict(state), include_domains=include_domains)
        assert outcome.ok
        persisted = {
            (c["name"], c["domain"]) for c in json.loads(storage_path.read_text())["cookies"]
        }
        playwright_kept = {
            (c["name"], c["domain"])
            for c in playwright_filter(dict(state), include_domains=include_domains)["cookies"]
        }
        assert persisted == playwright_kept


class TestRequiredVsOptional:
    """REQUIRED is empirically justified; OPTIONAL is opt-in only."""

    def test_required_set_includes_runtime_auth_domains(self):
        """Codex caution: host + dotted variants must both stay in REQUIRED."""
        for domain in (
            ".google.com",
            "google.com",
            ".notebooklm.google.com",
            "notebooklm.google.com",
            ".notebook.google.com",
            "notebook.google.com",
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
    """The runtime gate consults the REQUIRED ∪ OPTIONAL union.

    Blast-radius reduction is enforced at *extraction* time (see
    :class:`TestBuildGoogleCookieDomains` and
    :class:`TestBlastRadiusExtractor` below): rookiepy only returns cookies
    from :data:`REQUIRED_COOKIE_DOMAINS` by default, so YouTube cookies
    never reach ``storage_state.json`` unless the user explicitly opts in
    via ``--include-domains=youtube``. The runtime gate must stay
    permissive over the full union so opted-in cookies survive the
    downstream auth/storage filters.
    """

    def test_runtime_gate_accepts_youtube_for_opt_in(self):
        """Contract: the runtime gate accepts every OPTIONAL domain.

        If it rejected ``.youtube.com`` here, ``--include-domains=youtube``
        cookies would be extracted by rookiepy and then immediately
        filtered back out by ``convert_rookiepy_cookies_to_storage_state``,
        ``extract_cookies_with_domains``, and
        ``build_httpx_cookies_from_storage`` — all of which delegate to
        this gate.
        """
        assert _is_allowed_cookie_domain(".youtube.com") is True
        assert _is_allowed_cookie_domain("youtube.com") is True
        assert _is_allowed_cookie_domain("accounts.youtube.com") is True
        assert _is_allowed_cookie_domain(".accounts.youtube.com") is True

    def test_runtime_gate_accepts_required_set(self):
        """Every REQUIRED domain passes the runtime gate (exact-match tier)."""
        for domain in REQUIRED_COOKIE_DOMAINS:
            assert _is_allowed_cookie_domain(domain) is True, (
                f"{domain!r} must pass the runtime gate (it's in REQUIRED)"
            )

    def test_runtime_gate_accepts_google_subdomain_optional_siblings(self):
        """Google-root siblings and regional subdomains pass the suffix tier."""
        for domain in (
            "docs.google.com",
            ".docs.google.com",
            "myaccount.google.com",
            ".myaccount.google.com",
            "mail.google.com",
            ".mail.google.com",
            "drive.usercontent.google.com",
            "accounts.google.com.hk",
            "lh3.google.co.uk",
            "lh3.googleusercontent.com",
        ):
            assert _is_allowed_cookie_domain(domain) is True

    def test_runtime_gate_still_rejects_lookalikes(self):
        """The permissive gate is still strict against lookalike domains."""
        assert _is_allowed_cookie_domain(".not-youtube.com") is False
        assert _is_allowed_cookie_domain("notyoutube.com") is False
        assert _is_allowed_cookie_domain("evil-google.com") is False
        assert _is_allowed_cookie_domain("evilgoogle.com") is False
        assert _is_allowed_cookie_domain("google.com.evil.com") is False
        assert _is_allowed_cookie_domain("google.uk.co") is False
        assert _is_allowed_cookie_domain("google.co.uk.evil.com") is False
        assert _is_allowed_cookie_domain(".google.zz") is False


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
        """``include_optional=True`` restores the previous broad set."""
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


class TestGeminiNotebookRebrandHost:
    """``notebook.google.com`` (the July 2026 Gemini Notebook rebrand host) is
    treated as a REQUIRED auth domain so ``--browser-cookies`` extraction
    requests it and the runtime loader keeps its binding cookies.

    Background (issue #2013): Google rebranded NotebookLM to Gemini Notebook
    on 2026-07-16 and now also serves the app from ``notebook.google.com``.
    The per-product binding cookies (``OSID`` / ``__Secure-OSID``) are set on
    this host in addition to the legacy ``notebooklm.google.com``. Before this
    fix the host was absent from :data:`REQUIRED_COOKIE_DOMAINS`, so
    ``notebooklm login --browser-cookies`` never asked rookiepy for it and a
    user whose browser only populated ``OSID`` on the rebrand host would fail
    the Tier-2 secondary-binding check (or silently keep a stale jar).
    """

    def test_rebrand_host_in_required_set(self):
        """Both dotted and bare rebrand host variants are REQUIRED."""
        # Set-intersection form sidesteps CodeQL's substring-sanitization
        # heuristic (same pattern as test_youtube_rejected_by_default above).
        rebrand_variants = frozenset({"notebook.google.com", ".notebook.google.com"})
        assert rebrand_variants & REQUIRED_COOKIE_DOMAINS == rebrand_variants

    def test_rebrand_host_requested_by_default_extraction(self):
        """Default ``_build_google_cookie_domains`` asks for the rebrand host."""
        domains = frozenset(_build_google_cookie_domains())
        # Set-intersection form sidesteps CodeQL's substring-sanitization
        # heuristic.
        rebrand_variants = frozenset({"notebook.google.com", ".notebook.google.com"})
        assert rebrand_variants & domains == rebrand_variants

    def test_rebrand_host_passes_runtime_gate(self):
        """The rebrand host is accepted by the runtime cookie-domain gate."""
        assert _is_allowed_cookie_domain("notebook.google.com") is True
        assert _is_allowed_cookie_domain(".notebook.google.com") is True

    def test_rebrand_host_osid_survives_extraction(self):
        """A storage_state with ``OSID`` only on ``notebook.google.com`` is
        kept by ``extract_cookies_with_domains`` (the load-time loader)."""
        from notebooklm._auth.cookies import extract_cookies_with_domains

        storage = {
            "cookies": [
                {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "psidts",
                    "domain": ".google.com",
                    "path": "/",
                },
                {
                    "name": "OSID",
                    "value": "rebrand_osid",
                    "domain": "notebook.google.com",
                    "path": "/",
                },
            ],
            "origins": [],
        }
        cookie_map = extract_cookies_with_domains(storage)
        assert cookie_map[("OSID", "notebook.google.com", "/")] == "rebrand_osid"

    def test_legacy_app_host_osid_outranks_rebrand_host_when_both_present(self):
        """When ``OSID`` is set on both the legacy and rebrand hosts, the
        legacy ``notebooklm.google.com`` (Tier 2) wins over
        ``notebook.google.com`` (Tier 2) only via first-occurrence within the
        same tier — but the dotted ``.notebooklm.google.com`` (Tier 3)
        strictly outranks both. This guards against the rebrand host silently
        shadowing the canonical app host."""
        from notebooklm._auth.cookies import extract_cookies_from_storage

        storage = {
            "cookies": [
                {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "psidts",
                    "domain": ".google.com",
                    "path": "/",
                },
                {
                    "name": "OSID",
                    "value": "rebrand",
                    "domain": "notebook.google.com",
                    "path": "/",
                },
                {
                    "name": "OSID",
                    "value": "legacy",
                    "domain": ".notebooklm.google.com",
                    "path": "/",
                },
            ],
            "origins": [],
        }
        cookies = extract_cookies_from_storage(storage)
        # Tier 3 (.notebooklm.google.com) outranks Tier 2 (notebook.google.com).
        assert cookies["OSID"] == "legacy"


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
    """Blast-radius reduction is enforced at extraction time, not runtime.

    Contract: rookiepy is asked only for ``REQUIRED_COOKIE_DOMAINS`` by
    default. Sibling-product cookies (YouTube) therefore never enter
    ``storage_state.json`` unless the user explicitly opts in. The
    downstream runtime gate is permissive over the full union so
    opted-in cookies still flow through.

    These tests pin the contract at the *only* choke point that gates
    blast radius: the domain list fed to the browser-cookie extractor.
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

    def test_default_extraction_list_excludes_youtube(self):
        """Default login asks rookiepy for REQUIRED only — no YouTube domains.

        The actual blast-radius control: rookiepy never returns YouTube
        cookies under the default flag set, so they never reach
        ``storage_state.json``. The downstream runtime gate stays
        permissive so opted-in cookies survive the filters.
        """
        youtube_variants = frozenset({".youtube.com", "youtube.com"})
        domains_default = frozenset(_build_google_cookie_domains())
        assert domains_default.isdisjoint(youtube_variants)

    def test_opt_in_then_default_resets_extraction_set(self):
        """After ``--include-domains=youtube`` then a default re-login, no YouTube.

        Simulates the codex-flagged "user opted in once, then forgot
        and re-ran default" case. The extractor must actively exclude on
        re-run, not just default to a smaller request set.
        """
        youtube_variants = frozenset({".youtube.com", "youtube.com"})

        # First run with --include-domains=youtube: YouTube cookies extracted.
        domains_optin = frozenset(_build_google_cookie_domains(include_domains={"youtube"}))
        # Set-intersection form sidesteps CodeQL's substring-sanitization
        # heuristic.
        assert youtube_variants & domains_optin

        # Second run with default: YouTube is NOT in the extraction list.
        domains_default = frozenset(_build_google_cookie_domains())
        assert domains_default.isdisjoint(youtube_variants)

    def test_youtube_cookies_survive_when_opted_in(self, tmp_path: Path):
        """End-to-end: ``--include-domains=youtube`` persists YouTube cookies.

        The Gemini-flagged regression: under the original tightened
        runtime gate, ``--include-domains=youtube`` was a silent no-op
        because every downstream filter
        (:func:`convert_rookiepy_cookies_to_storage_state`,
        :func:`extract_cookies_with_domains`,
        :func:`build_httpx_cookies_from_storage`) dropped the cookies
        again on the way through. This test exercises the full pipeline
        and asserts opted-in cookies survive end-to-end.
        """
        from notebooklm.auth import (
            _is_allowed_auth_domain,
            build_httpx_cookies_from_storage,
            extract_cookies_with_domains,
        )

        # Simulate the rookiepy output for a user who passed
        # ``--include-domains=youtube``: REQUIRED auth cookies plus a YouTube
        # opt-in cookie.
        raw = [
            self._raw_cookie(".google.com", "SID"),
            self._raw_cookie(".google.com", "__Secure-1PSIDTS"),
            self._raw_cookie(".youtube.com", "LOGIN_INFO"),
        ]
        # Step 1: rookiepy → storage_state conversion must keep the YouTube
        # cookie (the runtime gate is permissive over the union).
        storage_state = convert_rookiepy_cookies_to_storage_state(raw)
        kept_domains = frozenset(c["domain"] for c in storage_state["cookies"])
        assert {".youtube.com"} <= kept_domains, (
            "convert_rookiepy_cookies_to_storage_state must keep YouTube "
            "cookies once they have been extracted; the runtime gate is "
            "permissive over the union."
        )
        assert any(c["name"] == "LOGIN_INFO" for c in storage_state["cookies"])

        # Step 2: storage_state → DomainCookieMap (used by AuthTokens).
        cookie_map = extract_cookies_with_domains(storage_state)
        assert ("LOGIN_INFO", ".youtube.com", "/") in cookie_map, (
            "extract_cookies_with_domains must keep YouTube cookies for the opt-in path."
        )

        # Step 3: storage_state → httpx jar (used by downloads + refresh).
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(json.dumps(storage_state))
        jar = build_httpx_cookies_from_storage(storage_file)
        assert jar.get("LOGIN_INFO", domain=".youtube.com") == "v", (
            "build_httpx_cookies_from_storage must keep YouTube cookies for the opt-in path."
        )

        # Step 4: the runtime gate itself accepts every YouTube variant.
        for domain in (".youtube.com", "youtube.com", "accounts.youtube.com"):
            assert _is_allowed_auth_domain(domain) is True, (
                f"_is_allowed_auth_domain({domain!r}) must accept the domain so "
                "opted-in cookies survive every downstream filter."
            )


class TestTokenVerificationStillWorksAfterMinimumSet:
    """Token-verification regression test (load-bearing for the cookie-domain split).

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
        from notebooklm._auth.cookies import load_httpx_cookies

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

        with patch_session_login_dual("_login_browser_cookies_single") as login_single:
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

        with patch_session_login_dual("_login_browser_cookies_single"):
            result = runner.invoke(
                cli,
                ["login", "--browser-cookies", "chrome"],
            )

        assert result.exit_code == 0, result.output
        assert "sibling-product domains are not explicitly requested" in result.output
        assert "trusted Google roots may still be retained" in result.output

    def test_login_with_include_domains_suppresses_migration_note(self, monkeypatch):
        """The migration note is suppressed once the user opts in."""
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        runner = CliRunner()

        with patch_session_login_dual("_login_browser_cookies_single"):
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
        assert "sibling-product domains are not explicitly requested" not in result.output

    def test_login_include_domains_on_playwright_path_no_longer_warns(self, monkeypatch, tmp_path):
        """``--include-domains`` now applies on the Playwright path (P1-17).

        Prior to P1-17 the Playwright login flow ignored ``--include-domains``
        and emitted a "no effect" warning. Now the flag drives the
        write-time cookie-domain allowlist filter so sibling-product cookies
        only get persisted when the user explicitly opts in. Confirm the
        legacy warning is gone — the test guarding the old behavior is
        repurposed as a regression guard so we don't reintroduce it.
        """
        monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        runner = CliRunner()

        # Stub Playwright import to break out before any real browser launch;
        # we only care about the pre-launch warning emission path.
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            result = runner.invoke(cli, ["login", "--include-domains", "youtube"])

        # CodeRabbit feedback: pin the failure path so the test cannot pass
        # spuriously if the warning never fired (e.g. if Playwright became
        # available via some other import shim). The Playwright-not-installed
        # branch in ``_run_playwright_login`` exits 1 with a clear message.
        assert result.exit_code == 1
        assert "Playwright not installed" in result.output
        assert "--include-domains has no effect without --browser-cookies" not in result.output


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

        with patch_session_login_dual("_refresh_from_browser_cookies") as helper:
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

        with patch_session_login_dual("_enumerate_browser_accounts") as enum:
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
