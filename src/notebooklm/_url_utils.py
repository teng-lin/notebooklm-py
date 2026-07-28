"""URL validation utilities.

These helpers use proper URL parsing to avoid substring matching vulnerabilities
flagged by CodeQL (py/incomplete-url-substring-sanitization).
"""

import re
from urllib.parse import parse_qs, unquote, urlparse

# Control characters (C0, DEL, C1) that ``unquote`` can reintroduce into a
# derived display title — a NUL/newline must never reach a source title.
_TITLE_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Upper bound on a URL-derived display title (a filename stem; 200 is generous).
_MAX_URL_TITLE_LEN = 200

# The NotebookLM marketing/landing host (note: no ``.com``). A request to the
# app host ``notebooklm.google.com`` is redirected here — typically
# ``notebooklm.google/?location=unsupported`` — when Google's region /
# anti-abuse risk-control declines the request's *environment* (VPN/proxy or
# datacenter IP, IP/timezone/language mismatch, non-browser access pattern).
# This is distinct from the ``accounts.google.com`` login redirect (expired or
# invalid auth) and from a genuine page-structure change.
_NOTEBOOKLM_MARKETING_HOST = "notebooklm.google"


def pdf_url_display_title(url: str) -> str | None:
    """Derive a display title from a direct-PDF ``url``, or ``None`` to keep it.

    Direct-PDF-URL sources arrive from the server with the raw request URL in
    their title slot (Google extracts ``<title>`` for HTML pages but leaves the
    URL verbatim for a link that points straight at a ``.pdf``) — issue #1850.
    When such a URL is used as a title, this returns a cleaner display title:
    the decoded, ``.pdf``-stripped final path segment, e.g.
    ``https://host/papers/Some%20Paper.pdf?v=2#p3`` → ``Some Paper``.

    Returns ``None`` (so the caller keeps the original title) for anything that
    would not yield a clean filename:

    * a non-``http(s)`` scheme (``data:`` / ``ftp:`` / opaque),
    * a path whose basename has no ``.pdf`` extension — e.g.
      ``https://host/download?file=x.pdf`` (path basename is ``download``),
    * a degenerate segment (root URL ``https://host/``, ``.`` / ``..``, or a
      segment that is only control characters).

    Query and fragment are ignored; a trailing slash is stripped. The result is
    control-char scrubbed and length-bounded so a title never carries a
    NUL/newline or unbounded base64 noise.
    """
    try:
        parsed = urlparse(url)
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    # Split the still-encoded path first, then decode the leaf — so a
    # percent-encoded ``%2F`` inside a segment is not treated as a separator.
    segment = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    if segment[-4:].lower() != ".pdf":
        return None
    stem = _TITLE_CONTROL_CHARS.sub("", segment[:-4]).strip()
    # A separator only reaches the leaf via a percent-encoded ``%2F`` / ``%5C``
    # (real PDF filenames have none) — treat such adversarial encodings, and
    # bare ``.`` / ``..``, as "no clean title" and keep the raw URL.
    if not stem or stem in (".", "..") or "/" in stem or "\\" in stem:
        return None
    return stem[:_MAX_URL_TITLE_LEN]


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
        return (
            hostname == "youtube.com" or hostname.endswith(".youtube.com") or hostname == "youtu.be"
        )
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
        return hostname == "accounts.google.com" or hostname.endswith(".accounts.google.com")
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
        return hostname == _NOTEBOOKLM_MARKETING_HOST or hostname.endswith(
            "." + _NOTEBOOKLM_MARKETING_HOST
        )
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
