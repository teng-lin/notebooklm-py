"""Cookie-domain policy and required-cookie validation for authentication."""

from __future__ import annotations

import logging

logger = logging.getLogger("notebooklm.auth")

# Tier 1: cookies whose absence Google rejects deterministically.
#
# - ``SID``: only individually-required cookie (singleton ablation).
# - ``__Secure-1PSIDTS``: directly accepted by Google's homepage check, OR
#   recoverable via the RotateCookies POST when other auth cookies are intact.
#   When neither path is viable the homepage GET 302s to login.
#
# See ``docs/auth-keepalive.md`` §3.5 for the ablation methodology and the full
# 16-pair failure table backing this set.
MINIMUM_REQUIRED_COOKIES = {"SID", "__Secure-1PSIDTS"}


_EXTRACTION_HINT = (
    "This typically means --browser-cookies extraction was incomplete "
    "(Chrome 127+ App-Bound Encryption can cause silent partial reads). "
    "Run 'notebooklm login' to re-authenticate."
)

# Tier 2 fires per cookie-load; a single CLI run can hit it 2-3 times across
# the four loader entry points. One warning per process is enough signal.
#
# Dedupe contract: best-effort under threads, exactly-once on a single
# event loop. The check-then-set at the call site (``_validate_required_cookies``
# below) reads ``_SECONDARY_BINDING_WARNED`` and sets it to ``True`` in a single
# synchronous block with no intervening ``await``. The asyncio scheduler can
# only switch coroutines at ``await`` points, so concurrent coroutines on one
# loop cannot interleave between the check and the set — the warning fires
# exactly once per process. Under genuine OS threads (which this library does
# NOT support per the documented concurrency contract — each client is bound
# to one event loop), the pattern is racy: two threads can both observe
# ``False`` before either has written ``True``, causing a duplicate warning.
# We accept that as best-effort rather than introduce an ``asyncio.Lock``
# (would not help threads) or a ``threading.Lock`` (re-architects for a use
# case we don't support).
#
# Note: ``functools.lru_cache`` and ``logging.LoggerAdapter`` are sometimes
# suggested as drop-in dedupe primitives here. They are NOT: ``lru_cache``
# memoizes return values, not the side-effect of ``logger.warning``;
# ``LoggerAdapter`` only rewrites records, it does not filter duplicates.
_SECONDARY_BINDING_WARNED = False


def _has_valid_secondary_binding(cookie_names: set[str]) -> bool:
    """Tier 2 acceptance check (see ``MINIMUM_REQUIRED_COOKIES``).

    Pair-wise ablation against a live Google session reveals that the
    NotebookLM homepage GET requires *at least one* of two redundant
    secondary-binding paths in addition to Tier 1:

    - ``OSID`` (recent-sign-in binding), OR
    - both ``APISID`` AND ``SAPISID`` (legacy XSSI binding pair).

    Without either, Google 302s to ``accounts.google.com/v3/signin`` even when
    ``SID`` and ``__Secure-1PSIDTS`` are present and otherwise valid.
    """
    if "OSID" in cookie_names:
        return True
    return {"APISID", "SAPISID"} <= cookie_names


def _validate_required_cookies(
    cookie_names: set[str],
    *,
    context: str = "",
    extra_diagnostics: list[str] | None = None,
) -> None:
    """Enforce the Tier 1 cookie-set rule (raise) and warn on Tier 2 violation.

    Hybrid rollout: Tier 1 (``MINIMUM_REQUIRED_COOKIES``) is a hard requirement
    because its ablation evidence is unambiguous — Google rejects deterministically
    and there is no recovery path inside the library. Tier 2 (secondary binding,
    see ``_has_valid_secondary_binding``) is logged as a warning so partial
    extractions surface in user logs without breaking edge-case auth flows we
    haven't ablated yet (e.g. Workspace SSO). After one release of telemetry
    this can be promoted to a hard raise.

    Args:
        cookie_names: Names of cookies present in the loaded set (any domain).
        context: Optional suffix for the Tier 1 error message
            (e.g. ``" for downloads"``).
        extra_diagnostics: Optional extra lines inserted into the Tier 1 error
            (e.g. observed cookies, source domains) for friendlier diagnosis.
    """
    missing = MINIMUM_REQUIRED_COOKIES - cookie_names
    if missing:
        missing_names = ", ".join(sorted(missing))
        parts = [f"Missing required cookies{context}: {missing_names}"]
        if extra_diagnostics:
            parts.extend(extra_diagnostics)
        parts.append(_EXTRACTION_HINT)
        raise ValueError("\n".join(parts))

    if not _has_valid_secondary_binding(cookie_names):
        global _SECONDARY_BINDING_WARNED
        if not _SECONDARY_BINDING_WARNED:
            _SECONDARY_BINDING_WARNED = True
            logger.warning(
                "Cookie set lacks a secondary binding (need OSID, or both APISID "
                "and SAPISID). Google may reject auth on the next call. %s",
                _EXTRACTION_HINT,
            )


# Cookie domains we extract / accept by default.
#
# Empirical justification: traced cassettes
# (``tests/cassettes/*.yaml``) and the live auth-refresh path. Only the
# following domains are actually exercised during login + token refresh +
# source-add + chat-ask flows:
#   - ``notebooklm.google.com`` (the API host — all CLI RPCs land here)
#   - ``.google.com`` (carries ``SID``/``HSID``/``SSID``/etc.)
#   - ``accounts.google.com`` (token refresh + ``RotateCookies`` endpoint at
#     :data:`KEEPALIVE_ROTATE_URL`)
#   - ``.googleusercontent.com`` (authenticated media downloads — audio /
#     infographic / slide assets)
#   - ``drive.google.com`` (Drive-source ingest follows redirects through
#     here; kept in REQUIRED for source-add safety)
#
# YouTube / Docs / Mail / myaccount cookies do NOT appear in any traced
# flow. They are now :data:`OPTIONAL_COOKIE_DOMAINS` — opted in via
# ``notebooklm login --include-domains=...``. This narrows the blast
# radius if ``storage_state.json`` is ever leaked.
#
# REQUIRED is also fed verbatim to ``rookiepy.load(domains=...)`` by
# ``_login_with_browser_cookies`` (via
# :func:`notebooklm.cli.session._build_google_cookie_domains`); adding a
# domain here automatically extends what we ask the browser for at login.
#
# This frozenset is the single chokepoint for the cookie-domain
# narrowing security control: extraction is restricted to
# ``REQUIRED_COOKIE_DOMAINS`` so that a stolen ``storage_state.json``
# cannot pivot the attacker into sibling Google products that we do not
# need (YouTube, Mail, etc.). Enforcement happens at extraction time
# (what ``rookiepy`` returns); the runtime gate stays permissive over
# the ``REQUIRED ∪ OPTIONAL`` union so that ``--include-domains=...``
# opt-ins survive downstream filters (see
# :func:`_is_allowed_cookie_domain`).
REQUIRED_COOKIE_DOMAINS: frozenset[str] = frozenset(
    {
        ".google.com",
        "google.com",  # Host-only Domain=google.com cookies (rare but possible)
        # Playwright storage_state may preserve the leading dot for NotebookLM cookies.
        ".notebooklm.google.com",
        "notebooklm.google.com",
        ".notebooklm.cloud.google.com",
        "notebooklm.cloud.google.com",
        ".googleusercontent.com",
        "accounts.google.com",  # Required for token refresh + RotateCookies
        ".accounts.google.com",  # http.cookiejar may normalize Domain=accounts.google.com
        # Drive-source ingest follows redirects through drive.google.com.
        # Both dotted and non-dotted variants are listed so that
        # http.cookiejar normalization (which can add a leading dot) doesn't
        # drop a cookie at the next extraction; same defensive pattern as
        # accounts.google.com above.
        "drive.google.com",
        ".drive.google.com",
    }
)

# Sibling Google product domains — NOT exercised by any current code path
# but historically extracted "for symmetry with a logged-in browser session"
# (issue #360). Now opt-in via ``--include-domains=...`` to reduce
# storage_state.json blast radius. The keys here (``youtube``, ``docs``,
# ``myaccount``, ``mail``) are also the labels accepted by ``--include-domains``.
#
# Both dotted and non-dotted variants are listed so that http.cookiejar
# normalization (which can add a leading dot) doesn't drop a cookie at the
# next extraction.
OPTIONAL_COOKIE_DOMAINS_BY_LABEL: dict[str, frozenset[str]] = {
    "youtube": frozenset(
        {
            ".youtube.com",
            "youtube.com",
            "accounts.youtube.com",
            ".accounts.youtube.com",
        }
    ),
    "docs": frozenset({"docs.google.com", ".docs.google.com"}),
    "myaccount": frozenset({"myaccount.google.com", ".myaccount.google.com"}),
    "mail": frozenset({"mail.google.com", ".mail.google.com"}),
}

OPTIONAL_COOKIE_DOMAINS: frozenset[str] = frozenset().union(
    *OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values()
)

# Backward-compatible union — preserves the old constant name so external
# imports keep working. Internal code should prefer ``REQUIRED_*`` /
# ``OPTIONAL_*`` so the security tier is explicit at the call site.
ALLOWED_COOKIE_DOMAINS: frozenset[str] = REQUIRED_COOKIE_DOMAINS | OPTIONAL_COOKIE_DOMAINS

# Regional Google ccTLDs where Google may set auth cookies
# Users in these regions may have SID cookies on regional domains instead of .google.com
# Format: suffix after ".google." (e.g., "com.sg" for ".google.com.sg")
#
# Categories:
# - com.XX: Country-code second-level domains (Singapore, Australia, Brazil, etc.)
# - co.XX: Country domains using .co (UK, Japan, India, Korea, etc.)
# - XX: Single ccTLD countries (Germany, France, Italy, etc.)
GOOGLE_REGIONAL_CCTLDS = frozenset(
    {
        # .google.com.XX pattern (country-code second-level domains)
        "com.sg",  # Singapore
        "com.au",  # Australia
        "com.br",  # Brazil
        "com.mx",  # Mexico
        "com.ar",  # Argentina
        "com.hk",  # Hong Kong
        "com.tw",  # Taiwan
        "com.my",  # Malaysia
        "com.ph",  # Philippines
        "com.vn",  # Vietnam
        "com.pk",  # Pakistan
        "com.bd",  # Bangladesh
        "com.ng",  # Nigeria
        "com.eg",  # Egypt
        "com.tr",  # Turkey
        "com.ua",  # Ukraine
        "com.co",  # Colombia
        "com.pe",  # Peru
        "com.sa",  # Saudi Arabia
        "com.ae",  # UAE
        # .google.co.XX pattern (countries using .co second-level)
        "co.uk",  # United Kingdom
        "co.jp",  # Japan
        "co.in",  # India
        "co.kr",  # South Korea
        "co.za",  # South Africa
        "co.nz",  # New Zealand
        "co.id",  # Indonesia
        "co.th",  # Thailand
        "co.il",  # Israel
        "co.ve",  # Venezuela
        "co.cr",  # Costa Rica
        "co.ke",  # Kenya
        "co.ug",  # Uganda
        "co.tz",  # Tanzania
        "co.ma",  # Morocco
        "co.ao",  # Angola
        "co.mz",  # Mozambique
        "co.zw",  # Zimbabwe
        "co.bw",  # Botswana
        # .google.XX pattern (single ccTLD countries)
        "cn",  # China
        "de",  # Germany
        "fr",  # France
        "it",  # Italy
        "es",  # Spain
        "nl",  # Netherlands
        "pl",  # Poland
        "ru",  # Russia
        "ca",  # Canada
        "be",  # Belgium
        "at",  # Austria
        "ch",  # Switzerland
        "se",  # Sweden
        "no",  # Norway
        "dk",  # Denmark
        "fi",  # Finland
        "pt",  # Portugal
        "gr",  # Greece
        "cz",  # Czech Republic
        "ro",  # Romania
        "hu",  # Hungary
        "ie",  # Ireland
        "sk",  # Slovakia
        "bg",  # Bulgaria
        "hr",  # Croatia
        "si",  # Slovenia
        "lt",  # Lithuania
        "lv",  # Latvia
        "ee",  # Estonia
        "lu",  # Luxembourg
        "cl",  # Chile
        "cat",  # Catalonia (special case - 3 letter)
    }
)


def _is_google_domain(domain: str) -> bool:
    """Check if a cookie domain is a valid Google domain.

    Uses a whitelist approach to validate Google domains including:
    - Base domain: .google.com
    - Regional .google.com.XX: .google.com.sg, .google.com.au, etc.
    - Regional .google.co.XX: .google.co.uk, .google.co.jp, etc.
    - Regional .google.XX: .google.de, .google.fr, etc.

    This function is used by both auth cookie extraction and download cookie
    validation to ensure consistent domain handling across the codebase.

    Args:
        domain: Cookie domain to check (e.g., '.google.com', '.google.com.sg')

    Returns:
        True if domain is a valid Google domain.

    Note:
        Uses an explicit whitelist (GOOGLE_REGIONAL_CCTLDS) rather than regex
        to prevent false positives from invalid or malicious domains.
    """
    # Base Google domain
    if domain == ".google.com":
        return True

    # Check regional Google domains using whitelist
    if domain.startswith(".google."):
        suffix = domain[8:]  # Remove ".google." prefix
        return suffix in GOOGLE_REGIONAL_CCTLDS

    return False


def _is_allowed_auth_domain(domain: str) -> bool:
    """Check if a cookie domain is allowed for auth cookie extraction.

    Thin alias of :func:`_is_allowed_cookie_domain`. Both auth-jar building
    and download-cookie loading (and the persistence path that filters which
    cookies get saved back) share a single allowlist policy:

    1. Exact match against :data:`REQUIRED_COOKIE_DOMAINS` (covers the API
       host, ``.google.com`` / ``accounts.google.com`` /
       ``.googleusercontent.com`` / ``drive.google.com``, and the
       leading-dot variants ``http.cookiejar`` may normalize to).
    2. Regional Google ccTLDs (``.google.com.sg``, ``.google.co.uk``,
       ``.google.de``, …) where SID cookies may be set for users in those
       regions.
    3. Suffix matches for Google subdomains (``lh3.google.com``,
       ``accounts.google.com``) and ``.googleusercontent.com`` /
       ``.usercontent.google.com`` for authenticated media downloads.

    The previous strict / broad split (#334 / fea8315) created an asymmetry
    where ``save_cookies_to_storage`` would persist cookies that the next
    extraction would silently drop. Issue #360 collapsed both filters into
    this single policy. The cookie-domain narrowing control restricts the
    *extraction* surface: ``rookiepy`` only requests
    :data:`REQUIRED_COOKIE_DOMAINS` by default, so YouTube cookies are
    never written to ``storage_state.json`` unless the user opts in via
    ``--include-domains=youtube``. The runtime gate stays permissive over
    the full :data:`ALLOWED_COOKIE_DOMAINS` union so that opted-in cookies
    survive the downstream filters.

    Args:
        domain: Cookie domain to check (e.g., '.google.com', '.google.com.sg')

    Returns:
        True if domain is allowed for auth/download cookies.
    """
    return _is_allowed_cookie_domain(domain)


def _auth_domain_priority(domain: str) -> int:
    """Return duplicate-cookie priority for allowed auth domains.

    Higher value wins. Tiers are distinct so the resolved cookie is fully
    deterministic regardless of storage_state ordering.
    """
    if domain == ".google.com":
        return 4
    if domain == ".notebooklm.google.com":
        return 3
    if domain == "notebooklm.google.com":
        return 2
    if domain == ".notebooklm.cloud.google.com":
        return 3
    if domain == "notebooklm.cloud.google.com":
        return 2
    if _is_google_domain(domain):
        return 1
    # Allowlisted but unranked domains (e.g. .googleusercontent.com) fall through.
    return 0


def _is_allowed_cookie_domain(domain: str) -> bool:
    """Canonical cookie-domain allowlist for both auth and downloads.

    Single source of truth for "is this cookie domain one we accept at
    runtime?". Both the auth-extraction path and the download path go
    through here — :func:`_is_allowed_auth_domain` is a thin alias
    preserved for call-site readability. See issue #360 for why the split
    was collapsed.

    A domain is allowed if any of the following holds:

    1. Exact match against :data:`REQUIRED_COOKIE_DOMAINS` (the API host,
       ``.google.com``, ``accounts.google.com``, ``.googleusercontent.com``,
       ``drive.google.com``, and the leading-dot variants ``http.cookiejar``
       may normalize to).
    2. Valid Google domain via :func:`_is_google_domain` (regional ccTLDs:
       ``.google.com.sg``, ``.google.co.uk``, ``.google.de``, …).
    3. Subdomain of ``.google.com``, ``.googleusercontent.com``, or
       ``.usercontent.google.com`` (e.g. ``lh3.google.com``,
       ``lh3.googleusercontent.com``).

    The leading-dot suffix check ensures lookalikes like ``evil-google.com``
    are rejected.

    Note: the runtime gate consults the
    :data:`ALLOWED_COOKIE_DOMAINS` union (REQUIRED ∪ OPTIONAL). The
    blast-radius reduction is enforced at **extraction time** —
    ``_build_google_cookie_domains`` defaults to
    :data:`REQUIRED_COOKIE_DOMAINS` only, so rookiepy never returns
    sibling-product cookies (e.g. ``.youtube.com``) unless the user
    opts in via ``--include-domains=...``. The runtime gate must stay
    permissive over the full union so that opted-in cookies survive
    the downstream filters in :func:`convert_rookiepy_cookies_to_storage_state`,
    :func:`extract_cookies_with_domains`, and
    :func:`build_httpx_cookies_from_storage`.

    Args:
        domain: Cookie domain to check (e.g., '.google.com', 'lh3.google.com')

    Returns:
        True if domain is allowed for auth/download cookies.
    """
    # Exact match against the union of REQUIRED + OPTIONAL. Anything that
    # could have been validly opted in via ``--include-domains`` at
    # extraction time must pass this gate at runtime.
    if domain in ALLOWED_COOKIE_DOMAINS:
        return True

    # Check if it's a valid Google domain (base or regional)
    # This handles .google.com, .google.com.sg, .google.co.uk, .google.de, etc.
    if _is_google_domain(domain):
        return True

    # Suffixes for allowed download domains (leading dot provides boundary check)
    # - Subdomains of .google.com (e.g., lh3.google.com, accounts.google.com)
    # - googleusercontent.com domains for media downloads
    allowed_suffixes = (
        ".google.com",
        ".googleusercontent.com",
        ".usercontent.google.com",
    )

    # Check if domain is a subdomain of allowed suffixes
    # The leading dot ensures 'evil-google.com' does NOT match
    return any(domain.endswith(suffix) for suffix in allowed_suffixes)
