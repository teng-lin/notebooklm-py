"""URL validation utilities.

These helpers use proper URL parsing to avoid substring matching vulnerabilities
flagged by CodeQL (py/incomplete-url-substring-sanitization).
"""

import logging
import re
from collections.abc import Iterable
from ipaddress import IPv6Address, ip_address
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlsplit, urlunsplit

from ._env import ENTERPRISE_BASE_HOST, PERSONAL_APP_HOSTS

# Control characters (C0, DEL, C1) that ``unquote`` can reintroduce into a
# derived display title — a NUL/newline must never reach a source title.
_TITLE_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Upper bound on a URL-derived display title (a filename stem; 200 is generous).
_MAX_URL_TITLE_LEN = 200

# The NotebookLM marketing/landing host (note: no ``.com``). A request to the
# app host (either personal host) is redirected here — typically
# ``notebooklm.google/?location=unsupported`` — when Google's region /
# anti-abuse risk-control declines the request's *environment* (VPN/proxy or
# datacenter IP, IP/timezone/language mismatch, non-browser access pattern).
# This is distinct from the ``accounts.google.com`` login redirect (expired or
# invalid auth) and from a genuine page-structure change.
_NOTEBOOKLM_MARKETING_HOST = "notebooklm.google"

# The NotebookLM *app* hosts — the hosts that can legitimately serve a page
# carrying ``WIZ_global_data``. Landing anywhere else means the request never
# reached the app, which is a different failure from "the app's page changed".
# Derived from ``_env``'s host-role constants so the post-rebrand alias, the
# enterprise host, and any future addition stay in one place. (``_env`` imports
# nothing from this module, so there is no cycle.)
#
# This enumerates exactly the same hosts as ``_env._ALLOWED_BASE_HOSTS`` today,
# and is still spelled out separately rather than aliased to it, because the two
# answer different questions: ``_ALLOWED_BASE_HOSTS`` decides where we may *send
# credentials*, while this set answers the read-only "did this response come from
# the app?". Those can legitimately diverge again — an app host we are not
# willing to send cookies to, or an accepted base URL that never serves the app
# shell — so neither set should be redefined in terms of the other.
_NOTEBOOKLM_APP_HOSTS: frozenset[str] = PERSONAL_APP_HOSTS | {ENTERPRISE_BASE_HOST}

# Google's cookie-mismatch interstitial. Reaching it means Google rejected the
# cookies as not matching the host they were presented to (a cookie *scoping*
# failure), which is emphatically not the same as an expired session — see
# issue #2038. The page 302s onward to a ``support.google.com`` help article, so
# it is only ever visible in the redirect *history*, never as the final URL of a
# followed chain.
_COOKIE_MISMATCH_PATH = "cookiemismatch"


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


#: The exact hosts a YouTube *video id* may be read from. Deliberately narrower
#: than :func:`is_youtube_url`'s ``*.youtube.com`` suffix test: that answers
#: "did the caller mean YouTube?" (and drives the "looks like YouTube but no
#: video id" warning), while this decides whether a path/query is a video
#: address the ``ADD_SOURCE`` YouTube variant can carry.
_YOUTUBE_VIDEO_HOSTS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)

#: Path prefixes that place the video id in the *next* segment.
_YOUTUBE_ID_PATH_PREFIXES = ("shorts", "embed", "live", "v")


def is_valid_youtube_video_id(video_id: str) -> bool:
    """Validate YouTube video ID format."""
    return bool(video_id and re.match(r"^[a-zA-Z0-9_-]+$", video_id))


def youtube_video_id_from_parsed_url(parsed: Any, hostname: str) -> str | None:
    """Extract the raw YouTube video ID from a parsed URL."""
    if hostname == "youtu.be":
        path = parsed.path.lstrip("/")
        if path:
            return path.split("/")[0].strip()
        return None

    path_segments = parsed.path.lstrip("/").split("/")

    # Unpack instead of indexing ``path_segments[0]`` / ``[1]``: these are
    # URL path segments, not an RPC payload, but the positional-RPC ratchet
    # is type-blind, so the unpack keeps the benign string parse off the
    # flagged ``name[int]`` shape (semantics identical to the prior
    # ``len(...) >= 2`` + index reads).
    if len(path_segments) >= 2:
        prefix, segment, *_rest = path_segments
        if prefix.lower() in _YOUTUBE_ID_PATH_PREFIXES:
            return segment.strip()

    if parsed.query:
        query_params = parse_qs(parsed.query)
        v_param = query_params.get("v", [])
        # ``next(iter(...))`` instead of ``v_param[0]`` for the same
        # type-blind-ratchet reason; ``v_param`` is the parse_qs value list.
        first_v = next(iter(v_param), None)
        if first_v:
            return first_v.strip()

    return None


def extract_youtube_video_id(url: str, *, logger: logging.Logger) -> str | None:
    """Extract a YouTube video ID from supported URL formats.

    Pure and transport-neutral: the source-add family's YouTube dispatch is a
    URL-parsing question, not a wire one, so both the hoisted ``source.add_url``
    workflow and the remaining below-port batch path read it from here rather
    than each owning a copy. ``logger`` stays a parameter so the parse
    diagnostic keeps being emitted under the *caller's* logger name.
    """
    try:
        parsed = urlparse(url.strip())
        hostname = (parsed.hostname or "").lower()

        if hostname not in _YOUTUBE_VIDEO_HOSTS:
            return None

        video_id = youtube_video_id_from_parsed_url(parsed, hostname)

        if video_id and is_valid_youtube_video_id(video_id):
            return video_id

        return None

    except (AttributeError, TypeError, ValueError) as e:
        logger.debug("Failed to parse YouTube URL '%s': %s", url[:100], e)
        return None


_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")


def _normalize_percent_escapes(value: str) -> str:
    return _PERCENT_ESCAPE.sub(lambda match: match.group(0).upper(), value)


def url_identity(url: str, *, logger: logging.Logger) -> tuple[str, str]:
    """Return a conservative identity for sparse-response reconciliation.

    Two spellings of the same address share one identity — a YouTube URL is
    identified by its video id, everything else by a canonicalized
    scheme/authority/path/query — so a batch write's response row can be matched
    back to the request that asked for it. Deliberately conservative: it only
    normalizes what is unambiguously equivalent (case in the scheme and host,
    IPv6 and percent-escape spelling, the default port, an empty path), because
    a false *merge* would attribute one caller's success to another request.

    Pure and transport-neutral, like :func:`extract_youtube_video_id`, which it
    delegates the YouTube half to; ``logger`` is that call's parse diagnostic.
    """
    candidate = url.strip()
    video_id = extract_youtube_video_id(candidate, logger=logger)
    if video_id is not None:
        return ("youtube", video_id)

    try:
        parsed = urlsplit(candidate)
        # URL inputs are validated before an identity is asked for, but a
        # response URL is untrusted wire data. Keep malformed values distinct
        # and let the caller fail closed rather than raising from normalization.
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        return ("raw", candidate)
    if not parsed.scheme or not parsed.hostname:
        return ("raw", candidate)

    hostname = parsed.hostname.lower()
    is_ipv6 = False
    try:
        address = ip_address(hostname)
    except ValueError:
        pass
    else:
        hostname = address.compressed
        is_ipv6 = isinstance(address, IPv6Address)
    if is_ipv6:
        hostname = f"[{hostname}]"
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    authority = hostname if port is None or default_port else f"{hostname}:{port}"
    if parsed.username is not None:
        userinfo = _normalize_percent_escapes(parsed.username)
        if parsed.password is not None:
            userinfo += f":{_normalize_percent_escapes(parsed.password)}"
        authority = f"{userinfo}@{authority}"
    normalized = urlunsplit(
        (
            parsed.scheme.lower(),
            authority,
            _normalize_percent_escapes(parsed.path or "/"),
            _normalize_percent_escapes(parsed.query),
            "",
        )
    )
    return ("url", normalized)


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

    .. warning::
       **Never use this to conclude that a session expired.** Practically every
       Google-served page — the NotebookLM app itself, the region/anti-abuse
       gate page, and ordinary ``support.google.com`` help articles — carries an
       ``accounts.google.com`` link somewhere (sign-in, account chooser,
       manage-account menu, privacy footer). A body hit is therefore evidence of
       almost nothing.

       The authoritative signal for "we were bounced to login" is the **final
       URL** (:func:`is_google_auth_redirect`). Two separate false positives
       have already been traced to using this helper as an auth classifier:
       issue #1630 (the ``notebooklm.google`` gate page) and issue #2038 /
       #2019, where a ``support.google.com`` help article reached via a
       ``CookieMismatch`` hop was reported to users as an expired login for six
       consecutive days while the credentials were in fact valid.

       :func:`notebooklm._auth.account._probe_authuser` and
       :mod:`notebooklm._auth.extraction` both deliberately check only the final
       URL for this reason. This helper is retained as a public export for
       backward compatibility and for callers that genuinely want the literal
       question "does this text mention a Google login URL?".

    Args:
        text: HTML or JSON text that may contain URLs

    Returns:
        True if any URL in the text points to Google accounts
    """
    # Find URLs in the text (href="...", src="...", or standalone https://...)
    url_pattern = r'https?://[^\s"\'<>]+'
    urls = re.findall(url_pattern, text)
    return any(is_google_auth_redirect(url) for url in urls)


def is_notebooklm_app_host(url: str) -> bool:
    """Check whether ``url`` is served by a NotebookLM *app* host.

    True for the consumer host (``notebooklm.google.com``), its post-rebrand
    alias (``notebook.google.com``), and the enterprise host
    (``notebooklm.cloud.google.com``) — the hosts that can serve a page
    containing ``WIZ_global_data``. Deliberately an exact-host match: the
    marketing/gate host ``notebooklm.google`` is a different host (no ``.com``)
    and must not qualify, and no subdomain of the app hosts serves the app
    shell.

    Used to split a token-extraction failure into "the app's page shape changed"
    (we are on the app host — file a bug) versus "we never reached the app"
    (we are somewhere else — a redirect/environment problem). Before issue
    #2038 both collapsed into a misleading "Authentication expired".

    Args:
        url: URL to check (typically ``response.url`` after redirects).

    Returns:
        True if the URL's host is a NotebookLM app host.
    """
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except (AttributeError, TypeError, ValueError):
        return False
    return hostname in _NOTEBOOKLM_APP_HOSTS


def is_cookie_mismatch_redirect(url: str) -> bool:
    """Check whether ``url`` is Google's ``accounts.google.com/CookieMismatch``.

    Google serves this interstitial when the cookies presented do not match the
    host they were sent to — a cookie *scoping* problem (a ``storage_state.json``
    whose per-cookie domains were flattened, cookies belonging to a different
    Google session, or a stale ``__Secure-1PSIDTS``). It is not an expired
    login, and re-running ``notebooklm login`` is only one of several possible
    remediations, so it warrants its own diagnostic (#2038).

    The path is matched case-insensitively and ignores any trailing slash;
    the query string (which carries a ``continue=`` target) is not considered.

    Args:
        url: URL to check — usually a hop from ``response.history``, since the
            interstitial 302s onward and is rarely the final URL.

    Returns:
        True if the URL is the cookie-mismatch interstitial.
    """
    if not is_google_auth_redirect(url):
        return False
    # No parse guard needed here: ``is_google_auth_redirect`` only returns True
    # after ``urlparse(url)`` succeeded, so the identical re-parse below cannot
    # raise. Malformed input (e.g. an unterminated IPv6 literal) is swallowed by
    # that helper and short-circuits above.
    return urlparse(url).path.strip("/").lower() == _COOKIE_MISMATCH_PATH


def find_cookie_mismatch_hop(urls: Iterable[str]) -> str | None:
    """Return the first cookie-mismatch URL in ``urls``, or ``None``.

    ``response.url`` alone cannot see the interstitial because it 302s onward to
    a ``support.google.com`` help article, so callers pass the redirect history
    followed by the final URL. See :func:`is_cookie_mismatch_redirect`.

    Args:
        urls: Candidate URLs in chain order (redirect history, then final URL).

    Returns:
        The first matching URL, or ``None`` when the chain has no such hop.
    """
    return next((url for url in urls if is_cookie_mismatch_redirect(url)), None)


def is_notebooklm_unavailable_redirect(url: str) -> bool:
    """Check if a URL is the NotebookLM marketing/landing host (an access gate).

    A request to an app host redirected to the bare
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
