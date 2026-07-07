"""URL validation utilities.

These helpers use proper URL parsing to avoid substring matching vulnerabilities
flagged by CodeQL (py/incomplete-url-substring-sanitization).
"""

import re
from urllib.parse import parse_qs, urlparse

# The NotebookLM marketing/landing host (note: no ``.com``). A request to the
# app host ``notebooklm.google.com`` is redirected here — typically
# ``notebooklm.google/?location=unsupported`` — when Google's region /
# anti-abuse risk-control declines the request's *environment* (VPN/proxy or
# datacenter IP, IP/timezone/language mismatch, non-browser access pattern).
# This is distinct from the ``accounts.google.com`` login redirect (expired or
# invalid auth) and from a genuine page-structure change.
_NOTEBOOKLM_MARKETING_HOST = "notebooklm.google"


def is_youtube_url(url: str) -> bool:
    """Check if a URL is a YouTube video URL.

    Uses proper hostname parsing to avoid substring matching issues
    (e.g., 'evil.com/youtube.com' would incorrectly match with substring check).

    Args:
        url: URL to check

    Returns:
        True if the URL is from YouTube (youtube.com or youtu.be)
    """
    try:
        hostname = (urlparse(url).hostname or "").lower()
        if not hostname:
            return False
        # Match youtube.com or any subdomain of youtube.com
        if re.match(r"^(?:[a-z0-9-]+\.)*youtube\.com$", hostname):
            return True
        # Match youtu.be or any subdomain of youtu.be
        if re.match(r"^(?:[a-z0-9-]+\.)*youtu\.be$", hostname):
            return True
        return False
    except (AttributeError, TypeError, ValueError):
        return False


def is_google_auth_redirect(url: str) -> bool:
    """Check if a URL is a Google authentication/login page redirect.

    Used to detect when our request to NotebookLM was redirected to
    accounts.google.com due to expired/invalid authentication.

    Args:
        url: URL to check (typically response.url after a request)

    Returns:
        True if the URL is a Google accounts page
    """
    try:
        hostname = (urlparse(url).hostname or "").lower()
        if not hostname:
            return False
        # Match accounts.google.com or any subdomain of accounts.google.com
        return bool(re.match(r"^(?:[a-z0-9-]+\.)*accounts\.google\.com$", hostname))
    except (AttributeError, TypeError, ValueError):
        return False


def contains_google_auth_redirect(text: str) -> bool:
    """Check if text (HTML/JSON) contains a Google auth redirect URL.

    Extracts URLs from text and checks if any point to accounts.google.com.
    Used to detect login page redirects in HTML response bodies.

    Args:
        text: HTML or JSON text that may contain URLs

    Returns:
        True if any URL in the text points to Google accounts
    """
    # Find URLs in the text (href="...", src="...", or standalone https://...)
    url_pattern = r'https?://[^\s"\'<>]+'
    urls = re.findall(url_pattern, text)
    return any(is_google_auth_redirect(url) for url in urls)


def is_notebooklm_unavailable_redirect(url: str) -> bool:
    """Check if a URL is the NotebookLM marketing/landing host (an access gate).

    A request to the app (``notebooklm.google.com``) redirected to the bare
    ``notebooklm.google`` host means Google's region / anti-abuse risk-control
    declined the request's environment — *not* expired auth (that goes to
    ``accounts.google.com``) and *not* a page-structure change. The bare host is
    distinguished from the app host purely by the absent ``.com`` suffix, so an
    exact / subdomain match on ``notebooklm.google`` never matches
    ``notebooklm.google.com``.

    Args:
        url: URL to check (typically ``response.url`` after redirects).

    Returns:
        True if the URL is the ``notebooklm.google`` landing host.
    """
    try:
        hostname = (urlparse(url).hostname or "").lower()
        if not hostname:
            return False
        # Match notebooklm.google or any subdomain of notebooklm.google
        return bool(re.match(r"^(?:[a-z0-9-]+\.)*notebooklm\.google$", hostname))
    except (AttributeError, TypeError, ValueError):
        return False


def notebooklm_unavailable_location(url: str) -> str | None:
    """Return the ``location`` query value from a NotebookLM access-gate URL.

    Surfaces the diagnostic Google attaches to the marketing redirect (e.g.
    ``"unsupported"`` from ``notebooklm.google/?location=unsupported``) so the
    cause is visible even though the URL scrubber drops the rest of the query.
    Returns ``None`` when absent or unparseable.

    Args:
        url: URL to inspect (typically ``response.url`` after redirects).
    """
    try:
        values = parse_qs(urlparse(url).query).get("location")
    except (AttributeError, TypeError, ValueError):
        return None
    if not values:
        return None
    # Sequence unpacking (not ``values[0]``) — the parse-qs list isn't an RPC row,
    # but the positional-indexing guardrail can't tell; the guard above keeps the
    # unpack safe.
    first, *_ = values
    # The value lands in a user-facing error string, so keep only a bounded,
    # sane diagnostic token (e.g. ``unsupported``) — never echo arbitrary,
    # newline-bearing, or URL-shaped query content.
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "", first)[:64]
    return sanitized or None
