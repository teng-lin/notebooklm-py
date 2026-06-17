"""Tests for auth cookie-domain policy and allowlist (split in D1 PR-2).

This file owns one concern from the auth subpackage. The original
monolithic auth test module was split into six concern-aligned files
alongside the deletion of ``_AuthFacadeModule``; see ADR-0003
(superseded) and ADR-0007 (test-monkeypatch policy) for the rationale.
"""

import json
from typing import Any

import httpx
import pytest

from notebooklm._auth.cookies import load_httpx_cookies
from notebooklm._auth.storage import save_cookies_to_storage, snapshot_cookie_jar
from notebooklm.auth import (
    build_httpx_cookies_from_storage,
    convert_rookiepy_cookies_to_storage_state,
    extract_cookies_from_storage,
    extract_cookies_with_domains,
)


class TestIsAllowedCookieDomain:
    """Test cookie domain validation security."""

    def test_accepts_exact_matches_from_allowlist(self):
        """Test accepts domains in ALLOWED_COOKIE_DOMAINS."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain(".google.com") is True
        assert _is_allowed_cookie_domain("notebooklm.google.com") is True
        assert _is_allowed_cookie_domain("notebooklm.cloud.google.com") is True
        assert _is_allowed_cookie_domain(".notebooklm.cloud.google.com") is True
        assert _is_allowed_cookie_domain(".googleusercontent.com") is True
        assert _is_allowed_cookie_domain(".accounts.google.com") is True

    def test_accepts_valid_google_subdomains(self):
        """Test accepts legitimate Google subdomains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain("lh3.google.com") is True
        assert _is_allowed_cookie_domain("accounts.google.com") is True
        assert _is_allowed_cookie_domain("www.google.com") is True

    def test_accepts_googleusercontent_subdomains(self):
        """Test accepts googleusercontent.com subdomains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain("lh3.googleusercontent.com") is True
        assert _is_allowed_cookie_domain("drum.usercontent.google.com") is True

    def test_rejects_malicious_lookalike_domains(self):
        """Test rejects domains like 'evil-google.com' that end with google.com."""
        from notebooklm.auth import _is_allowed_cookie_domain

        # These domains end with ".google.com" but are NOT subdomains
        assert _is_allowed_cookie_domain("evil-google.com") is False
        assert _is_allowed_cookie_domain("malicious-google.com") is False
        assert _is_allowed_cookie_domain("fakegoogle.com") is False

    def test_rejects_fake_googleusercontent_domains(self):
        """Test rejects fake googleusercontent domains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain("evil-googleusercontent.com") is False
        assert _is_allowed_cookie_domain("fakegoogleusercontent.com") is False

    def test_rejects_unrelated_domains(self):
        """Test rejects completely unrelated domains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain("example.com") is False
        assert _is_allowed_cookie_domain("evil.com") is False
        assert _is_allowed_cookie_domain("google.evil.com") is False


# =============================================================================
# CONSTANT TESTS
# =============================================================================


class TestIsGoogleDomain:
    """Test the unified _is_google_domain function (whitelist approach)."""

    @pytest.mark.parametrize(
        "domain,expected",
        [
            # Base Google domain
            (".google.com", True),
            # .google.com.XX pattern (country-code second-level domains)
            (".google.com.sg", True),  # Singapore
            (".google.com.au", True),  # Australia
            (".google.com.br", True),  # Brazil
            (".google.com.hk", True),  # Hong Kong
            (".google.com.tw", True),  # Taiwan
            (".google.com.mx", True),  # Mexico
            (".google.com.ar", True),  # Argentina
            (".google.com.tr", True),  # Turkey
            (".google.com.ua", True),  # Ukraine
            # .google.co.XX pattern (countries using .co)
            (".google.co.uk", True),  # United Kingdom
            (".google.co.jp", True),  # Japan
            (".google.co.in", True),  # India
            (".google.co.kr", True),  # South Korea
            (".google.co.za", True),  # South Africa
            (".google.co.nz", True),  # New Zealand
            (".google.co.id", True),  # Indonesia
            (".google.co.th", True),  # Thailand
            # .google.XX pattern (single ccTLD)
            (".google.cn", True),  # China
            (".google.de", True),  # Germany
            (".google.fr", True),  # France
            (".google.it", True),  # Italy
            (".google.es", True),  # Spain
            (".google.nl", True),  # Netherlands
            (".google.pl", True),  # Poland
            (".google.ru", True),  # Russia
            (".google.ca", True),  # Canada
            (".google.cat", True),  # Catalonia (3-letter special case)
            # Invalid domains that should be rejected
            (".google.zz", False),  # Invalid ccTLD
            (".google.xyz", False),  # Not in whitelist
            (".google.com.fake", False),  # Not in whitelist
            (".notebooklm.google.com", False),  # Accepted by auth allowlist, not here
            (".mail.google.com", False),
            (".drive.google.com", False),
            (".evilnotebooklm.google.com", False),
            (".notebooklm.google.com.evil", False),
            (".notgoogle.com", False),
            (".evil-google.com", False),
            ("google.com", False),  # Missing leading dot
            ("google.com.sg", False),  # Missing leading dot
            (".youtube.com", False),
            (".google.", False),  # Incomplete
            ("", False),  # Empty
        ],
    )
    def test_is_google_domain(self, domain, expected):
        """Test _is_google_domain with various domain patterns."""
        from notebooklm.auth import _is_google_domain

        assert _is_google_domain(domain) is expected

    @pytest.mark.parametrize(
        "domain",
        [
            # Case sensitivity - cookie domains per RFC should be lowercase
            ".GOOGLE.COM",
            ".Google.Com",
            ".google.COM.SG",
            ".GOOGLE.CO.UK",
            ".GOOGLE.DE",
        ],
    )
    def test_rejects_uppercase_domains(self, domain):
        """Test that uppercase domains are rejected (case-sensitive matching).

        Per RFC 6265, cookie domains SHOULD be lowercase. Playwright and browsers
        normalize domains to lowercase, so we don't need case-insensitive matching.
        """
        from notebooklm.auth import _is_google_domain

        assert _is_google_domain(domain) is False

    @pytest.mark.parametrize(
        "domain",
        [
            " .google.com",  # Leading space
            ".google.com ",  # Trailing space
            "\t.google.com",  # Tab
            ".google.com\n",  # Newline
        ],
    )
    def test_rejects_domains_with_whitespace(self, domain):
        """Test that domains with whitespace are rejected."""
        from notebooklm.auth import _is_google_domain

        assert _is_google_domain(domain) is False

    @pytest.mark.parametrize(
        "domain",
        [
            ".google..com",  # Double dot
            "..google.com",  # Leading double dot
            ".google.com.",  # Trailing dot
        ],
    )
    def test_rejects_malformed_domains(self, domain):
        """Test that malformed domains with extra dots are rejected."""
        from notebooklm.auth import _is_google_domain

        assert _is_google_domain(domain) is False

    @pytest.mark.parametrize(
        "domain",
        [
            # Subdomains without leading dot are still rejected
            "accounts.google.com",
            "lh3.google.com",
            # Subdomains of regional domains are still rejected (not whitelisted)
            "accounts.google.de",
            "lh3.google.co.uk",
            ".accounts.google.de",  # Leading dot but regional subdomain
        ],
    )
    def test_rejects_subdomains(self, domain):
        """Test that non-canonical subdomains are rejected by _is_google_domain.

        _is_google_domain only accepts .google.com and regional root domains
        (.google.com.sg, etc). Auth extraction uses ALLOWED_COOKIE_DOMAINS for
        auth-specific subdomains.
        """
        from notebooklm.auth import _is_google_domain

        assert _is_google_domain(domain) is False

    @pytest.mark.parametrize(
        "domain",
        [
            ".google.com.sg.fake",  # Extra suffix
            ".fake.google.com.sg",  # Prefix on regional
            ".google.com.sgx",  # Extended TLD
            ".google.co.ukx",  # Extended co.XX
            ".google.dex",  # Extended ccTLD
        ],
    )
    def test_rejects_suffix_exploits(self, domain):
        """Test that attempts to exploit suffix matching are rejected."""
        from notebooklm.auth import _is_google_domain

        assert _is_google_domain(domain) is False


class TestIsAllowedAuthDomain:
    """Test auth cookie domain validation including regional Google domains."""

    def test_accepts_primary_google_domains(self):
        """Test accepts domains in ALLOWED_COOKIE_DOMAINS."""
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain(".google.com") is True
        assert _is_allowed_auth_domain("notebooklm.google.com") is True
        assert _is_allowed_auth_domain(".notebooklm.google.com") is True  # Issue #329
        assert _is_allowed_auth_domain(".googleusercontent.com") is True

    def test_accepts_all_regional_patterns(self):
        """Test accepts all three regional Google domain patterns (Issue #20)."""
        from notebooklm.auth import _is_allowed_auth_domain

        # .google.com.XX pattern
        assert _is_allowed_auth_domain(".google.com.sg") is True  # Singapore
        assert _is_allowed_auth_domain(".google.com.au") is True  # Australia

        # .google.co.XX pattern
        assert _is_allowed_auth_domain(".google.co.uk") is True  # UK
        assert _is_allowed_auth_domain(".google.co.jp") is True  # Japan

        # .google.XX pattern
        assert _is_allowed_auth_domain(".google.de") is True  # Germany
        assert _is_allowed_auth_domain(".google.fr") is True  # France

    def test_accepts_youtube_for_opt_in_post_t5g(self):
        """YouTube cookies pass the runtime gate so ``--include-domains=youtube``
        works end-to-end.

        The cookie-domain split enforces blast-radius reduction at
        *extraction* time (rookiepy is asked for
        :data:`REQUIRED_COOKIE_DOMAINS` only). The runtime gate stays
        permissive over the full union
        (:data:`ALLOWED_COOKIE_DOMAINS`) so opted-in YouTube cookies
        survive ``convert_rookiepy_cookies_to_storage_state``,
        ``extract_cookies_with_domains``, and
        ``build_httpx_cookies_from_storage`` — all of which delegate to
        this gate.
        """
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain(".youtube.com") is True
        assert _is_allowed_auth_domain("youtube.com") is True
        assert _is_allowed_auth_domain("accounts.youtube.com") is True
        assert _is_allowed_auth_domain(".accounts.youtube.com") is True

    def test_accepts_sibling_google_subdomains(self):
        """Sibling Google subdomains pass via the ``.google.com`` suffix.

        Drive / Docs / myaccount / Mail are subdomains of ``.google.com``,
        so the runtime gate accepts them via the suffix branch (tier 3 of
        :func:`_is_allowed_cookie_domain`) even though only ``drive.*`` is
        in :data:`REQUIRED_COOKIE_DOMAINS`.
        """
        from notebooklm.auth import _is_allowed_auth_domain

        # Drive (in REQUIRED) — exact-match tier
        assert _is_allowed_auth_domain("drive.google.com") is True
        assert _is_allowed_auth_domain(".drive.google.com") is True
        # Docs / myaccount / mail — accepted via .google.com suffix tier
        # (storage_state.json may still contain these from legacy logins).
        assert _is_allowed_auth_domain("docs.google.com") is True
        assert _is_allowed_auth_domain(".docs.google.com") is True
        assert _is_allowed_auth_domain("myaccount.google.com") is True
        assert _is_allowed_auth_domain(".myaccount.google.com") is True
        assert _is_allowed_auth_domain("mail.google.com") is True

    def test_rejects_unrelated_domains(self):
        """Test rejects non-Google domains."""
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain("evil.com") is False
        assert _is_allowed_auth_domain(".evil-google.com") is False
        assert _is_allowed_auth_domain(".not-youtube.com") is False
        assert _is_allowed_auth_domain("notyoutube.com") is False

    def test_rejects_malicious_google_lookalikes(self):
        """Test rejects domains that look like Google but aren't."""
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain("google.com.evil.sg") is False
        # Note: post-#360 unification, .mail.google.com is accepted (it's a
        # legitimate Google-owned subdomain). Only foreign suffixes are rejected.
        assert _is_allowed_auth_domain(".google.com.evil") is False
        assert _is_allowed_auth_domain(".evilnotebooklm.google.com.evil") is False
        assert _is_allowed_auth_domain(".not-google.com.sg") is False
        assert _is_allowed_auth_domain(".google.zz") is False  # Invalid ccTLD

    def test_requires_leading_dot_for_regional(self):
        """Test regional domains require leading dot.

        Regional ccTLDs like ``google.com.sg`` (no leading dot) are not in
        ALLOWED_COOKIE_DOMAINS and are not accepted by ``_is_google_domain``
        (which requires the leading dot for regional patterns) or by the
        suffix paths (which require the leading-dot suffix).
        """
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain("google.com.sg") is False
        assert _is_allowed_auth_domain("google.co.uk") is False
        assert _is_allowed_auth_domain("google.de") is False


class TestAuthDomainPriority:
    """Test `_auth_domain_priority` tier mapping for duplicate-cookie resolution."""

    @pytest.mark.parametrize(
        "domain,expected",
        [
            (".google.com", 4),
            (".notebooklm.google.com", 3),
            (".notebooklm.cloud.google.com", 3),
            ("notebooklm.google.com", 2),
            ("notebooklm.cloud.google.com", 2),
            (".google.de", 1),
            (".google.com.sg", 1),
            (".google.co.uk", 1),
            (".googleusercontent.com", 0),
            ("evil.com", 0),
            ("", 0),
        ],
    )
    def test_priority_tiers(self, domain, expected):
        from notebooklm.auth import _auth_domain_priority

        assert _auth_domain_priority(domain) == expected

    def test_priority_strict_ordering(self):
        """Higher tiers strictly outrank lower tiers — no ties between named tiers."""
        from notebooklm.auth import _auth_domain_priority

        priorities = [
            _auth_domain_priority(".google.com"),
            _auth_domain_priority(".notebooklm.google.com"),
            _auth_domain_priority("notebooklm.google.com"),
            _auth_domain_priority(".google.de"),
            _auth_domain_priority(".googleusercontent.com"),
        ]
        assert priorities == sorted(priorities, reverse=True)
        assert len(set(priorities)) == len(priorities)


class TestIsAllowedCookieDomainRegional:
    """Test _is_allowed_cookie_domain with regional Google domains."""

    def test_accepts_regional_google_domains_for_downloads(self):
        """Test that download cookie validation accepts regional domains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        # .google.com.XX pattern
        assert _is_allowed_cookie_domain(".google.com.sg") is True
        assert _is_allowed_cookie_domain(".google.com.au") is True

        # .google.co.XX pattern
        assert _is_allowed_cookie_domain(".google.co.uk") is True
        assert _is_allowed_cookie_domain(".google.co.jp") is True

        # .google.XX pattern
        assert _is_allowed_cookie_domain(".google.de") is True
        assert _is_allowed_cookie_domain(".google.fr") is True

    def test_still_accepts_subdomains(self):
        """Test that subdomain suffix matching still works."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain("lh3.google.com") is True
        assert _is_allowed_cookie_domain("accounts.google.com") is True
        assert _is_allowed_cookie_domain("lh3.googleusercontent.com") is True

    def test_youtube_accepted_for_opt_in_post_t5g(self):
        """YouTube remains in the runtime allowlist.

        Blast-radius reduction is enforced at extraction time, not by the
        runtime gate. See :class:`TestIsAllowedAuthDomain` for the
        rationale and :class:`TestSiblingGoogleProductExtraction` for the
        extraction-time contracts.
        """
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain(".youtube.com") is True
        assert _is_allowed_cookie_domain("youtube.com") is True
        assert _is_allowed_cookie_domain("accounts.youtube.com") is True
        assert _is_allowed_cookie_domain(".accounts.youtube.com") is True

    def test_accepts_sibling_google_subdomains(self):
        """Sibling Google subdomains still pass via the ``.google.com`` suffix tier."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain("drive.google.com") is True
        assert _is_allowed_cookie_domain("docs.google.com") is True
        assert _is_allowed_cookie_domain("myaccount.google.com") is True
        assert _is_allowed_cookie_domain("mail.google.com") is True

    def test_rejects_invalid_domains(self):
        """Test rejects invalid domains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain(".google.zz") is False
        assert _is_allowed_cookie_domain("evil-google.com") is False
        assert _is_allowed_cookie_domain(".not-youtube.com") is False
        assert _is_allowed_cookie_domain("notyoutube.com") is False


class TestExtractCookiesRegionalDomains:
    """Test cookie extraction from regional Google domains (Issue #20, #27)."""

    @pytest.mark.parametrize(
        "domain,sid_value,description",
        [
            (".google.com.sg", "sid_from_singapore", "Issue #20 - Singapore"),
            (".google.cn", "sid_from_china", "Issue #27 - China"),
            (".google.co.uk", "sid_from_uk", "UK regional domain"),
            (".google.de", "sid_from_de", "Germany regional domain"),
        ],
    )
    def test_extracts_sid_from_regional_domain(self, domain, sid_value, description):
        """Test extracts SID cookie from regional Google domains."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": sid_value, "domain": domain},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": domain},
                {"name": "OSID", "value": "osid_value", "domain": "notebooklm.google.com"},
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)

        assert cookies["SID"] == sid_value
        assert cookies["OSID"] == "osid_value"

    def test_extracts_sid_from_all_regional_patterns(self):
        """Test extracts SID from all three regional domain patterns."""
        # Test .google.com.XX
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_sg", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_sg"

        # Test .google.co.XX
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_uk", "domain": ".google.co.uk"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.co.uk"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_uk"

        # Test .google.XX
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_de", "domain": ".google.de"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.de"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_de"

    def test_extracts_multiple_regional_cookies(self):
        """Test extracts cookies from multiple regional domains."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_au", "domain": ".google.com.au"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.au"},
                {"name": "HSID", "value": "hsid_jp", "domain": ".google.co.jp"},
                {"name": "SSID", "value": "ssid_de", "domain": ".google.de"},
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)

        assert cookies["SID"] == "sid_au"
        assert cookies["HSID"] == "hsid_jp"
        assert cookies["SSID"] == "ssid_de"

    def test_prefers_primary_domain_over_regional(self):
        """Test that .google.com cookie wins over regional domains.

        Regression test for PR #34: When the same cookie name exists on both
        .google.com and a regional domain (e.g., .google.com.sg), the .google.com
        value should ALWAYS be used regardless of cookie order in the list.
        """
        # Test case 1: base domain listed first
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_global", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "SID", "value": "sid_regional", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_global", ".google.com should win (base first)"

        # Test case 2: regional domain listed first (this was the bug scenario)
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_regional", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
                {"name": "SID", "value": "sid_global", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_global", ".google.com should win (regional first)"

    def test_regional_google_sid_outranks_sibling_product_sid(self):
        """Regional Google SID outranks YouTube SID when both are present.

        Post-#360 the allowlist accepts sibling-product cookies, but the
        priority ladder still prefers a regional Google SID over a YouTube
        SID when both are seen, because YouTube falls into the unranked tier
        (priority 0) and regional Google domains sit at priority 1.
        """
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "youtube_sid", "domain": ".youtube.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".youtube.com"},
                {"name": "SID", "value": "regional_sid", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "regional_sid"

    def test_cookie_extraction_order_independent(self):
        """Test that cookie extraction is deterministic regardless of list order.

        Regression test for PR #34: The original bug caused non-deterministic
        behavior because Python dict's "last one wins" behavior meant the result
        depended on cookie iteration order.

        This test verifies that all permutations produce the same result.
        """
        from itertools import permutations

        base_cookies = [
            {"name": "SID", "value": "sid_base", "domain": ".google.com"},
            {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            {"name": "SID", "value": "sid_sg", "domain": ".google.com.sg"},
            {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
            {"name": "SID", "value": "sid_de", "domain": ".google.de"},
            {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.de"},
        ]

        results = set()
        for ordering in permutations(base_cookies):
            storage_state = {"cookies": list(ordering)}
            cookies = extract_cookies_from_storage(storage_state)
            results.add(cookies["SID"])

        # All permutations should produce the same result: .google.com wins
        assert results == {"sid_base"}, (
            f"Extraction should be deterministic, but got different results: {results}"
        )

    def test_regional_only_uses_first_encountered(self):
        """Test behavior when only regional domains exist (no .google.com).

        When .google.com is not present, we use whatever cookie we encounter.
        This documents the expected fallback behavior.
        """
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_sg", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
                {"name": "SID", "value": "sid_de", "domain": ".google.de"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.de"},
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)

        # Without .google.com, first encountered wins
        assert cookies["SID"] == "sid_sg"


class TestLoadHttpxCookiesRegional:
    """Test load_httpx_cookies with regional Google domains."""

    def test_loads_cookies_from_regional_domain(self, tmp_path):
        """Test loading httpx cookies from regional Google domain."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_from_uk", "domain": ".google.co.uk"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.co.uk"},
                {"name": "HSID", "value": "hsid_val", "domain": ".google.co.uk"},
            ]
        }

        storage_file = tmp_path / "storage.json"
        storage_file.write_text(json.dumps(storage_state))

        cookies = load_httpx_cookies(path=storage_file)
        assert cookies.get("SID", domain=".google.co.uk") == "sid_from_uk"

    def test_loads_cookies_from_all_regional_patterns(self, tmp_path):
        """Test loading httpx cookies from all regional patterns."""
        # Test with .google.de (single ccTLD)
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_de", "domain": ".google.de"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.de"},
            ]
        }
        storage_file = tmp_path / "storage.json"
        storage_file.write_text(json.dumps(storage_state))

        cookies = load_httpx_cookies(path=storage_file)
        assert cookies.get("SID", domain=".google.de") == "sid_de"


class TestSiblingGoogleProductExtraction:
    """Cookie extraction behavior for sibling Google product domains.

    The cookie-domain split enforces blast-radius reduction at
    *extraction* time (``_build_google_cookie_domains`` defaults to
    :data:`REQUIRED_COOKIE_DOMAINS`, so rookiepy never returns YouTube
    cookies unless the user opts in). The runtime gate stays permissive
    over the full union so that opted-in cookies survive every downstream
    filter. This class pins the contract that the downstream
    storage-state filters do *not* drop sibling-product cookies once they
    have been extracted — neither for Google subdomains nor for opted-in
    YouTube cookies.
    """

    GOOGLE_SUBDOMAIN_SIBLINGS = [
        "drive.google.com",
        ".drive.google.com",
        "docs.google.com",
        ".docs.google.com",
        "myaccount.google.com",
        ".myaccount.google.com",
        "mail.google.com",
        ".mail.google.com",
    ]
    YOUTUBE_DOMAINS = [
        ".youtube.com",
        "youtube.com",
        "accounts.youtube.com",
        ".accounts.youtube.com",
    ]

    @pytest.mark.parametrize("domain", GOOGLE_SUBDOMAIN_SIBLINGS)
    def test_extract_cookies_with_domains_keeps_google_subdomain_siblings(self, domain):
        """``extract_cookies_with_domains`` keeps Drive/Docs/myaccount/Mail cookies."""
        storage_state = {
            "cookies": [
                # Required SID on .google.com so extraction doesn't fail
                {"name": "SID", "value": "base_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "PRODUCT_TOKEN", "value": "sibling", "domain": domain},
            ]
        }
        cookie_map = extract_cookies_with_domains(storage_state)
        assert ("PRODUCT_TOKEN", domain, "/") in cookie_map
        assert cookie_map[("PRODUCT_TOKEN", domain, "/")] == "sibling"

    @pytest.mark.parametrize("domain", YOUTUBE_DOMAINS)
    def test_extract_cookies_with_domains_keeps_opted_in_youtube(self, domain):
        """Contract: ``extract_cookies_with_domains`` keeps YouTube cookies.

        Blast radius is reduced at extraction time (rookiepy does not
        return YouTube cookies by default). When a user has opted in via
        ``--include-domains=youtube`` and the storage_state contains a
        YouTube cookie, this filter must keep it so the opt-in is
        observable end-to-end.
        """
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "base_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "YT_TOKEN", "value": "yt", "domain": domain},
            ]
        }
        cookie_map = extract_cookies_with_domains(storage_state)
        assert ("YT_TOKEN", domain, "/") in cookie_map
        assert cookie_map[("YT_TOKEN", domain, "/")] == "yt"

    @pytest.mark.parametrize("domain", GOOGLE_SUBDOMAIN_SIBLINGS)
    def test_load_httpx_cookies_keeps_google_subdomain_siblings(self, tmp_path, domain):
        """``load_httpx_cookies`` accepts Google-subdomain sibling cookies."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "base_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "PRODUCT_TOKEN", "value": "sibling", "domain": domain},
            ]
        }
        storage_file = tmp_path / "storage.json"
        storage_file.write_text(json.dumps(storage_state))

        cookies = load_httpx_cookies(path=storage_file)
        assert cookies.get("PRODUCT_TOKEN", domain=domain) == "sibling"

    @pytest.mark.parametrize("domain", YOUTUBE_DOMAINS)
    def test_load_httpx_cookies_keeps_opted_in_youtube(self, tmp_path, domain):
        """``load_httpx_cookies`` keeps opted-in YouTube cookies.

        Blast radius is reduced at extraction time. The runtime gate
        (and therefore this loader) is permissive over the union so
        ``--include-domains=youtube`` cookies survive download/refresh
        operations.
        """
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "base_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "YT_TOKEN", "value": "yt", "domain": domain},
            ]
        }
        storage_file = tmp_path / "storage.json"
        storage_file.write_text(json.dumps(storage_state))

        cookies = load_httpx_cookies(path=storage_file)
        assert cookies.get("YT_TOKEN", domain=domain) == "yt"

    @pytest.mark.parametrize("domain", GOOGLE_SUBDOMAIN_SIBLINGS)
    def test_convert_rookiepy_keeps_google_subdomain_siblings(self, domain):
        """rookiepy → storage_state conversion keeps Google-subdomain siblings."""
        raw = [
            {
                "domain": domain,
                "name": "PRODUCT_TOKEN",
                "value": "sibling",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert len(result["cookies"]) == 1
        assert result["cookies"][0]["domain"] == domain

    @pytest.mark.parametrize("domain", YOUTUBE_DOMAINS)
    def test_convert_rookiepy_keeps_opted_in_youtube(self, domain):
        """rookiepy → storage_state keeps opted-in YouTube cookies.

        Blast radius is reduced at extraction time by
        ``_build_google_cookie_domains`` (the rookiepy domain filter).
        Once a YouTube cookie has been extracted (because the user
        opted in), this converter must keep it so the opt-in is
        observable end-to-end.
        """
        raw = [
            {
                "domain": domain,
                "name": "YT_TOKEN",
                "value": "yt",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert len(result["cookies"]) == 1
        assert result["cookies"][0]["domain"] == domain
        assert result["cookies"][0]["name"] == "YT_TOKEN"

    def test_strict_allowlisted_domains_still_work(self):
        """Regression: pre-existing strict-allowlisted domains keep working.

        Ensures the unification didn't accidentally drop any of the original
        canonical NotebookLM auth domains.
        """
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "v1", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "v2", "domain": ".google.com"},
                {"name": "OSID", "value": "v3", "domain": "notebooklm.google.com"},
                {"name": "OSID2", "value": "v4", "domain": ".notebooklm.google.com"},
                {"name": "ACC", "value": "v5", "domain": "accounts.google.com"},
                {"name": "ACC2", "value": "v6", "domain": ".accounts.google.com"},
                {"name": "MEDIA", "value": "v7", "domain": ".googleusercontent.com"},
            ]
        }
        cookie_map = extract_cookies_with_domains(storage_state)
        assert ("SID", ".google.com", "/") in cookie_map
        assert ("HSID", ".google.com", "/") in cookie_map
        assert ("OSID", "notebooklm.google.com", "/") in cookie_map
        assert ("OSID2", ".notebooklm.google.com", "/") in cookie_map
        assert ("ACC", "accounts.google.com", "/") in cookie_map
        assert ("ACC2", ".accounts.google.com", "/") in cookie_map
        assert ("MEDIA", ".googleusercontent.com", "/") in cookie_map

    def test_unified_filter_rejects_unrelated_domains(self):
        """Regression: cookies from unrelated domains are still rejected."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "v1", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "EVIL", "value": "x", "domain": ".evil.com"},
                {"name": "EVIL2", "value": "y", "domain": ".not-google.com"},
                {"name": "EVIL3", "value": "z", "domain": ".evil-google.com"},
                {"name": "EVIL4", "value": "w", "domain": ".not-youtube.com"},
            ]
        }
        cookie_map = extract_cookies_with_domains(storage_state)
        kept_names = {name for name, _, _ in cookie_map}
        assert kept_names == {"SID", "__Secure-1PSIDTS"}


class TestPathAwareCookieIdentity:
    """Issue #369: ``(name, domain, path)`` is the cookie identity per RFC 6265
    §5.3. Two cookies sharing ``(name, domain)`` at different paths must coexist
    end-to-end across the load/save APIs, and ``_find_cookie_for_storage`` must
    not return a sibling on a different path.
    """

    # Shared fixtures: the OSID path-sibling pair under test is identical
    # across cases except for the values and any extra Playwright-shaped
    # fields (httpOnly/secure/expires) the load/save round trip needs.

    _OSID_DOMAIN = "accounts.google.com"

    def _required_cookies(self) -> list[dict[str, Any]]:
        return [
            {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "tts",
                "domain": ".google.com",
                "path": "/",
            },
        ]

    def _osid_siblings(
        self,
        *,
        root_value: str = "root",
        u0_value: str = "u0",
        extras: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return an ``OSID@/`` + ``OSID@/u/0/`` pair sharing optional fields."""
        base = extras or {}
        return [
            {
                "name": "OSID",
                "value": root_value,
                "domain": self._OSID_DOMAIN,
                "path": "/",
                **base,
            },
            {
                "name": "OSID",
                "value": u0_value,
                "domain": self._OSID_DOMAIN,
                "path": "/u/0/",
                **base,
            },
        ]

    def _storage_state(self, osids: list[dict[str, Any]]) -> dict[str, Any]:
        return {"cookies": [*self._required_cookies(), *osids]}

    def test_extract_cookies_with_domains_keeps_path_siblings(self):
        """Two cookies sharing name+domain at distinct paths both survive
        extraction (pre-#369 the second one silently shadowed the first)."""
        cookie_map = extract_cookies_with_domains(self._storage_state(self._osid_siblings()))
        assert cookie_map[("OSID", self._OSID_DOMAIN, "/")] == "root"
        assert cookie_map[("OSID", self._OSID_DOMAIN, "/u/0/")] == "u0"

    def test_build_httpx_cookies_keeps_path_siblings(self, tmp_path):
        """The load-side dedup in ``build_httpx_cookies_from_storage`` keys
        on ``(name, domain, path)`` so both path-siblings land in the jar."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps(self._storage_state(self._osid_siblings())))

        jar = build_httpx_cookies_from_storage(storage)
        observed = {(c.name, c.domain, c.path): c.value for c in jar.jar if c.name == "OSID"}
        assert observed == {
            ("OSID", self._OSID_DOMAIN, "/"): "root",
            ("OSID", self._OSID_DOMAIN, "/u/0/"): "u0",
        }

    def test_round_trip_load_fetch_save_preserves_siblings(self, tmp_path):
        """A synthetic ``OSID@/`` + ``OSID@/u/0/`` storage_state survives a
        load → fetch (no-op mutation) → save round trip with **both** entries
        intact. Closes the issue's acceptance criterion."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                self._storage_state(
                    self._osid_siblings(
                        extras={"httpOnly": False, "secure": False, "expires": -1},
                    )
                )
            )
        )

        jar = build_httpx_cookies_from_storage(storage)
        snapshot = snapshot_cookie_jar(jar)
        # No mutation; save_cookies_to_storage is a no-op delta.
        save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        reloaded = json.loads(storage.read_text())
        osids = [c for c in reloaded["cookies"] if c["name"] == "OSID"]
        assert {(c["domain"], c["path"], c["value"]) for c in osids} == {
            (self._OSID_DOMAIN, "/", "root"),
            (self._OSID_DOMAIN, "/u/0/", "u0"),
        }

    def test_legacy_full_merge_preserves_path_siblings(self, tmp_path):
        """The legacy ``original_snapshot=None`` merge keys ``cookies_by_key`` /
        ``stored_keys`` on ``(name, domain, path)`` so a refreshed cookie at
        path ``/`` does not overwrite a sibling at ``/u/0/`` and vice versa."""
        import warnings

        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                self._storage_state(
                    self._osid_siblings(
                        root_value="root_old",
                        u0_value="u0_old",
                        extras={"expires": -1},
                    )
                )
            )
        )

        jar = httpx.Cookies()
        jar.set("SID", "sid", domain=".google.com", path="/")
        jar.set("__Secure-1PSIDTS", "tts", domain=".google.com", path="/")
        jar.set("OSID", "root_new", domain="accounts.google.com", path="/")
        jar.set("OSID", "u0_new", domain="accounts.google.com", path="/u/0/")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            save_cookies_to_storage(jar, storage)

        reloaded = json.loads(storage.read_text())
        osids = [c for c in reloaded["cookies"] if c["name"] == "OSID"]
        observed = {(c["domain"], c["path"], c["value"]) for c in osids}
        # Both siblings refreshed independently — pre-#369 the legacy merge
        # would have shadowed one with the other because cookies_by_key was
        # keyed on (name, domain).
        assert observed == {
            (self._OSID_DOMAIN, "/", "root_new"),
            (self._OSID_DOMAIN, "/u/0/", "u0_new"),
        }

    def test_normalize_cookie_map_widens_legacy_2tuple_input(self):
        """Pre-#369 callers built ``{(name, domain): value}`` dicts by hand;
        those keep working — :func:`normalize_cookie_map` widens the missing
        path to ``/`` so the rest of the pipeline sees the canonical shape."""
        from notebooklm._auth.cookies import normalize_cookie_map

        result = normalize_cookie_map(
            {
                ("SID", ".google.com"): "abc",
                ("OSID", "accounts.google.com"): "xyz",
            }
        )
        assert result == {
            ("SID", ".google.com", "/"): "abc",
            ("OSID", "accounts.google.com", "/"): "xyz",
        }

    def test_normalize_cookie_map_warns_on_malformed_tuple(self, caplog):
        """A malformed tuple key would otherwise vanish into a downstream
        'missing required cookies' error. Surface it via ``logger.warning``."""
        import logging

        from notebooklm._auth.cookies import normalize_cookie_map

        with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
            result = normalize_cookie_map(
                {("SID", ".google.com", "/", "extra"): "abc"}  # type: ignore[dict-item]
            )
        assert result == {}
        assert any("malformed cookie key" in rec.message for rec in caplog.records)

    def test_update_cookie_input_preserves_legacy_2tuple_shape(self):
        """A caller that originally passed a legacy 2-tuple dict must observe
        the same shape after an in-place refresh — internal widening should
        not bleed 3-tuple keys into their data structure."""
        from notebooklm.auth import _update_cookie_input

        target: dict[Any, str] = {
            ("SID", ".google.com"): "old_sid",
            ("OSID", "accounts.google.com"): "old_osid",
        }
        fresh: dict[tuple[str, str, str], str] = {
            ("SID", ".google.com", "/"): "new_sid",
            ("OSID", "accounts.google.com", "/"): "new_osid",
        }

        _update_cookie_input(target, fresh)

        assert target == {
            ("SID", ".google.com"): "new_sid",
            ("OSID", "accounts.google.com"): "new_osid",
        }

    def test_find_cookie_for_storage_picks_path_sibling(self):
        """``_find_cookie_for_storage`` must discriminate on path: a stored
        ``(OSID, accounts.google.com, /u/0/)`` row must NOT be refreshed with
        the ``OSID`` value from path ``/``. Pre-#369 the path was ignored and
        either sibling could be returned."""
        from notebooklm.auth import _find_cookie_for_storage

        # Two in-memory cookies sharing (name, domain) at distinct paths
        # coexist in a single ``http.cookiejar`` because its internal index is
        # ``(domain, path, name)`` — exactly the identity #369 makes first-class.
        jar = httpx.Cookies()
        jar.set("OSID", "root", domain=self._OSID_DOMAIN, path="/")
        jar.set("OSID", "u0", domain=self._OSID_DOMAIN, path="/u/0/")
        cookies_by_key = {(c.name, c.domain, c.path or "/"): c for c in jar.jar}

        # Looking up the /u/0/ key must return the /u/0/ cookie, not the / one.
        found = _find_cookie_for_storage(
            cookies_by_key,
            ("OSID", self._OSID_DOMAIN, "/u/0/"),
            stored_value="stale_u0",
        )
        assert found is not None
        assert found.value == "u0"
        assert found.path == "/u/0/"


class TestRookiepyDomainsCoverage:
    """Confirm ``_login_with_browser_cookies`` knows about every sibling product.

    After the fix the rookiepy ``domains`` list defaults to REQUIRED only, but
    the ``--include-domains`` opt-in still has to cover every sibling
    product. Pin that every known sibling label resolves to at least one
    domain, so future contributors can't forget to wire up a new label.
    """

    def test_every_optional_label_has_domains(self):
        from notebooklm.auth import OPTIONAL_COOKIE_DOMAINS_BY_LABEL

        for label, domains in OPTIONAL_COOKIE_DOMAINS_BY_LABEL.items():
            assert domains, f"label {label!r} maps to an empty domain set"

    def test_allowlist_union_still_covers_legacy_siblings(self):
        """ALLOWED_COOKIE_DOMAINS (union) covers every legacy sibling domain.

        External callers that read the union constant for documentation /
        validation purposes (e.g. ``cli.session`` historically) keep
        seeing the same domains. Only the runtime gate has tightened.
        """
        from notebooklm._auth.cookie_policy import ALLOWED_COOKIE_DOMAINS

        for domain in (
            ".youtube.com",
            "accounts.youtube.com",
            "drive.google.com",
            "docs.google.com",
            "myaccount.google.com",
        ):
            assert domain in ALLOWED_COOKIE_DOMAINS, (
                f"{domain!r} must be in ALLOWED_COOKIE_DOMAINS (union) so "
                "callers that read the legacy constant still see it"
            )


class TestConvertRookiepyCookies:
    """Test conversion from rookiepy cookie dicts to storage_state.json format."""

    def test_converts_basic_cookie(self):
        """Single cookie is converted to storage_state format."""
        raw = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "abc",
                "path": "/",
                "secure": True,
                "expires": 1234567890,
                "http_only": False,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert result["cookies"][0] == {
            "name": "SID",
            "value": "abc",
            "domain": ".google.com",
            "path": "/",
            "expires": 1234567890,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        }
        assert result["origins"] == []

    def test_none_expires_becomes_minus_one(self):
        """rookiepy uses None for session cookies; storage_state uses -1."""
        raw = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert result["cookies"][0]["expires"] == -1

    def test_filters_non_google_domains(self):
        """Non-Google domains are dropped."""
        raw = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            },
            {
                "domain": "evil.com",
                "name": "TRACK",
                "value": "y",
                "path": "/",
                "secure": False,
                "expires": None,
                "http_only": False,
            },
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert len(result["cookies"]) == 1
        assert result["cookies"][0]["name"] == "SID"

    def test_snake_to_camel_case(self):
        """http_only (rookiepy) → httpOnly (storage_state)."""
        raw = [
            {
                "domain": ".google.com",
                "name": "X",
                "value": "y",
                "path": "/",
                "secure": False,
                "expires": None,
                "http_only": True,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert "http_only" not in result["cookies"][0]
        assert result["cookies"][0]["httpOnly"] is True

    def test_empty_list(self):
        """Empty cookie list returns empty structure."""
        assert convert_rookiepy_cookies_to_storage_state([]) == {
            "cookies": [],
            "origins": [],
        }

    def test_regional_google_domain_included(self):
        """Regional domains like .google.co.uk are kept."""
        raw = [
            {
                "domain": ".google.co.uk",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert len(result["cookies"]) == 1

    def test_notebooklm_subdomain_included(self):
        """Playwright-style NotebookLM subdomain cookies are kept."""
        raw = [
            {
                "domain": ".notebooklm.google.com",
                "name": "OSID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert len(result["cookies"]) == 1
        assert result["cookies"][0]["domain"] == ".notebooklm.google.com"
        assert result["cookies"][0]["name"] == "OSID"

    def test_sibling_google_product_subdomains_kept(self):
        """Auth conversion keeps sibling Google subdomain cookies (after the fix).

        The cookie-domain split narrowed the runtime gate from the union to REQUIRED, but the
        ``.google.com`` suffix branch of :func:`_is_allowed_cookie_domain`
        still accepts cookies on Drive / Docs / myaccount / Mail. Only
        ``youtube.com`` (which is not a subdomain of ``.google.com``) is
        now dropped by default — see
        :class:`TestSiblingGoogleProductExtraction` for that contract.
        """
        raw = [
            {
                "domain": domain,
                "name": "SID",
                "value": "v",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            }
            for domain in (
                ".mail.google.com",
                ".drive.google.com",
                ".docs.google.com",
                ".myaccount.google.com",
            )
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        kept_domains = {c["domain"] for c in result["cookies"]}
        assert kept_domains == {
            ".mail.google.com",
            ".drive.google.com",
            ".docs.google.com",
            ".myaccount.google.com",
        }

    def test_unrelated_domains_still_filtered(self):
        """Cookies from non-Google domains are still dropped."""
        raw = [
            {
                "domain": ".evil.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            },
            {
                "domain": ".not-google.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            },
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert result == {"cookies": [], "origins": []}


class TestMinimumRequiredCookies:
    """Test minimum required cookies constant."""

    def test_minimum_required_cookies_contains_sid(self):
        """Test MINIMUM_REQUIRED_COOKIES contains SID."""
        from notebooklm._auth.cookie_policy import MINIMUM_REQUIRED_COOKIES

        assert "SID" in MINIMUM_REQUIRED_COOKIES


class TestAllowedCookieDomains:
    """Test cookie-domain constants (REQUIRED, OPTIONAL, ALLOWED union)."""

    def test_allowed_cookie_domains_union(self):
        """``ALLOWED_COOKIE_DOMAINS`` is the union of REQUIRED + OPTIONAL.

        Pins the union so external code that still imports the old constant
        keeps working. Internal callers should prefer the explicit
        REQUIRED/OPTIONAL constants — see the cookie-domain split
        migration note in ``src/notebooklm/auth.py``.
        """
        from notebooklm._auth.cookie_policy import ALLOWED_COOKIE_DOMAINS
        from notebooklm.auth import (
            OPTIONAL_COOKIE_DOMAINS,
            REQUIRED_COOKIE_DOMAINS,
        )

        assert ALLOWED_COOKIE_DOMAINS == REQUIRED_COOKIE_DOMAINS | OPTIONAL_COOKIE_DOMAINS

    def test_required_cookie_domains_preserve_normalization_variants(self):
        """REQUIRED keeps both host and dotted variants of each domain.

        Codex caution from the cookie-domain split design: http.cookiejar may normalize
        ``Domain=accounts.google.com`` to ``.accounts.google.com``. If
        REQUIRED only contained one variant, the next extraction would
        silently drop the cookie. Pin both forms here.
        """
        from notebooklm.auth import REQUIRED_COOKIE_DOMAINS

        # Required core auth + drive ingest set.
        expected = {
            ".google.com",
            "google.com",
            ".notebooklm.google.com",
            "notebooklm.google.com",
            ".notebooklm.cloud.google.com",
            "notebooklm.cloud.google.com",
            ".googleusercontent.com",
            "accounts.google.com",
            ".accounts.google.com",
            "drive.google.com",
            ".drive.google.com",
        }
        missing = expected - REQUIRED_COOKIE_DOMAINS
        assert not missing, f"REQUIRED_COOKIE_DOMAINS is missing: {missing}"

    def test_required_is_frozenset(self):
        """REQUIRED must be a frozenset so it cannot be mutated at runtime."""
        from notebooklm._auth.cookie_policy import ALLOWED_COOKIE_DOMAINS
        from notebooklm.auth import (
            OPTIONAL_COOKIE_DOMAINS,
            REQUIRED_COOKIE_DOMAINS,
        )

        assert isinstance(REQUIRED_COOKIE_DOMAINS, frozenset)
        assert isinstance(OPTIONAL_COOKIE_DOMAINS, frozenset)
        assert isinstance(ALLOWED_COOKIE_DOMAINS, frozenset)

    def test_optional_cookie_domains_label_partition(self):
        """OPTIONAL labels partition the OPTIONAL domain set exactly."""
        from notebooklm.auth import (
            OPTIONAL_COOKIE_DOMAINS,
            OPTIONAL_COOKIE_DOMAINS_BY_LABEL,
        )

        union = frozenset().union(*OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values())
        assert union == OPTIONAL_COOKIE_DOMAINS

    def test_required_and_optional_are_disjoint(self):
        """No domain appears in both REQUIRED and OPTIONAL.

        Otherwise the runtime gate would be ambiguous and the
        ``--include-domains`` opt-in would have no observable effect for
        the overlapping domain.
        """
        from notebooklm.auth import OPTIONAL_COOKIE_DOMAINS, REQUIRED_COOKIE_DOMAINS

        assert REQUIRED_COOKIE_DOMAINS.isdisjoint(OPTIONAL_COOKIE_DOMAINS)


# =============================================================================
# REGIONAL GOOGLE DOMAIN TESTS (Issue #20 fix)
# =============================================================================
