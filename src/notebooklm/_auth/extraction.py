"""HTML/WIZ field token extraction (CSRF, session ID, generic WIZ data).

NotebookLM (and other Google products) embed a JavaScript object literal named
``WIZ_global_data`` in the page chrome. Tokens like ``SNlM0e`` (CSRF) and
``FdrFJe`` (session ID) live inside that object. The helpers in this module
are the single place that knows how to parse the embedding, so all callers
benefit from the same drift tolerance and diagnostics.

Public surface (re-exported from ``notebooklm.auth``):

* :func:`extract_wiz_field` — generic ``WIZ_global_data[key]`` extractor.
* :func:`extract_csrf_from_html` — convenience wrapper for ``SNlM0e``.
* :func:`extract_session_id_from_html` — convenience wrapper for ``FdrFJe``.

Private helpers (also re-exported as white-box affordances for tests):

* :func:`_build_wiz_field_patterns` — the ordered regex patterns.
* :func:`_safe_url` — credential-stripping URL formatter for error messages.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .._url_utils import (
    contains_google_auth_redirect,
    is_google_auth_redirect,
    is_notebooklm_unavailable_redirect,
    notebooklm_unavailable_location,
)
from ..exceptions import AuthExtractionError


def _build_wiz_field_patterns(key: str) -> list[re.Pattern[str]]:
    """Build the ordered list of regex patterns used to locate a Wiz field.

    Patterns are tried in priority order: canonical double-quoted form first,
    then single-quoted, then HTML-escaped. Each pattern captures the value
    (which may be empty — empty tokens are legitimate, not a drift signal).

    All three variants tolerate backslash-escaped delimiters inside the value
    so JSON-style escapes like ``"key":"a\\"b"`` parse correctly. The inner
    character class ``[^"\\\\]*(?:\\\\.[^"\\\\]*)*`` is the standard
    "string with escapes" idiom: consume runs of non-quote/non-backslash
    chars, optionally followed by an escape pair (``\\.``) and another run.

    The HTML-escaped variant uses a tempered-dot lookahead so the capture
    stops only at a literal ``&quot;`` terminator (not at any ``&`` — values
    legitimately contain ``&amp;`` and similar entities).

    Whitespace tolerance (``\\s*``) around the colon mirrors the original
    ``extract_csrf_from_html`` regex so we don't regress.
    """
    escaped = re.escape(key)
    return [
        # 1. Canonical double-quoted: "key":"value"  (or  "key" : "value")
        #    Captures escaped quotes: "key":"a\"b" -> a\"b
        re.compile(rf'"{escaped}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"'),
        # 2. Single-quoted variant: 'key':'value' with escaped-quote support.
        re.compile(rf"'{escaped}'\s*:\s*'([^'\\]*(?:\\.[^'\\]*)*)'"),
        # 3. HTML-escaped: &quot;key&quot;:&quot;value&quot;
        #    Tempered dot so the value can contain other entities like &amp;.
        re.compile(rf"&quot;{escaped}&quot;\s*:\s*&quot;((?:(?!&quot;).)*)&quot;"),
    ]


def extract_wiz_field(html: str, key: str, *, strict: bool = True) -> str | None:
    """Extract a ``WIZ_global_data[key]`` value from a NotebookLM HTML response.

    NotebookLM (and other Google products) embed a JavaScript object literal
    named ``WIZ_global_data`` in the page chrome. Tokens like ``SNlM0e``
    (CSRF) and ``FdrFJe`` (session ID) live inside that object. This helper
    is the single place that knows how to parse the embedding so all callers
    benefit from the same drift tolerance and diagnostics.

    Tolerated input variants, tried in priority order:

    1. Canonical double-quoted ``"key":"value"`` (typical raw HTML).
    2. Single-quoted ``'key':'value'`` (rare, observed in some debug renders).
    3. HTML-escaped ``&quot;key&quot;:&quot;value&quot;`` (when the script
       block is rendered inside an attribute or escaped fragment).

    Empty values are passed through verbatim: ``"SNlM0e":""`` returns the
    empty string. Some Google endpoints legitimately emit empty tokens (e.g.
    for unauthenticated probes) and the caller — not this helper — should
    decide whether an empty value is acceptable.

    Args:
        html: The page HTML to search.
        key: Field name to extract from ``WIZ_global_data``.
        strict: When True (default) and no pattern matches, raise
            :class:`AuthExtractionError` with a sanitized preview. When False,
            return ``None`` on drift so callers can fall back gracefully.

    Returns:
        The extracted value (possibly empty), or ``None`` when ``strict=False``
        and no pattern matched.

    Raises:
        AuthExtractionError: ``strict=True`` and the key was not found.
    """
    for pattern in _build_wiz_field_patterns(key):
        match = pattern.search(html)
        if match is not None:
            return match.group(1)
    if strict:
        raise AuthExtractionError(key, html)
    return None


# Hosts whose path component may carry credentials and must be redacted in
# error-message interpolations. These are Google's OAuth surfaces:
#
# * ``accounts.google.com`` — the legacy login/consent flow (including
#   observed redirects of the shape ``/o/oauth2/auth/<token>``).
# * ``oauth2.googleapis.com`` — the token-exchange endpoint family.
# * ``oauth2.googleusercontent.com`` — observed grant-code return path in
#   some OAuth flows. (The wider ``.googleusercontent.com`` family is also
#   tagged trusted in ``_artifact/downloads.py`` for a related reason; we
#   scope this list to the ``oauth2`` subdomain because the broader family
#   carries non-auth payload URLs.)
#
# The query/fragment/userinfo stripping below applies to ALL hosts; the
# path stripping is restricted to this allowlist so non-auth failures
# still carry enough operator signal (host + path) to be diagnosable.
# Notable omission: ``www.googleapis.com`` is deliberately NOT in this
# list — it hosts many non-auth APIs (Drive, Calendar, Storage) where the
# path is operator signal, not credential. Add the specific subdomain if
# Google OAuth hosts where the path component can carry an opaque grant code
# or token and must be redacted. Using a fully anchored regex pattern avoids
# CodeQL substring/subdomain matching warnings (py/incomplete-url-substring-sanitization).
_AUTH_PATH_REDACT_PATTERN: re.Pattern[str] = re.compile(
    r"^(?:[a-z0-9-]+\.)*(?:accounts\.google\.com|oauth2\.googleapis\.com|oauth2\.googleusercontent\.com)$",
    re.IGNORECASE,
)


def _safe_url(url: str) -> str:
    """Return ``url`` stripped of credential-shaped parts for error display.

    Auth-handshake URLs can carry credentials in four positions, all of
    which we strip (the path strip is restricted to known auth hosts):

    * **Query string** — ``f.sid=...``, ``continue=...``, ``access_token=...``.
    * **Fragment** — OAuth implicit-flow tokens (``#access_token=...``).
    * **Userinfo** — ``https://TOKEN@host/...`` shapes; ``parsed.netloc``
      preserves the userinfo, so we rebuild from ``hostname`` + optional
      port instead of trusting ``netloc`` directly.
    * **Path** — restricted to ``_AUTH_PATH_REDACT_PATTERN`` (Google's OAuth
      hosts) where the path can carry an opaque grant code or token
      (``/o/oauth2/auth/<token>``). Non-auth hosts keep their path so
      operators can still identify which endpoint failed.

    The surviving ``scheme://host[:port]/path`` is enough context for an
    operator to recognize which endpoint failed without leaking session
    state. Empty input passes through verbatim so error messages with the
    default ``final_url=""`` still render cleanly instead of degenerating to
    ``"://"``.
    """
    if not url:
        return ""
    parsed = urlparse(url)
    # hostname strips userinfo; port survives separately. Both can be None
    # on malformed input, in which case we degrade gracefully to "scheme:///path".
    host = parsed.hostname or ""
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    path = parsed.path or ""
    host_lc = host.lower()
    # Match the exact host OR any subdomain using the anchored pattern
    if (
        path
        and path != "/"
        and _AUTH_PATH_REDACT_PATTERN.match(host_lc)
    ):
        # Replace with a fixed sentinel so the URL still parses cleanly and
        # the operator sees the auth-host signal without the path content.
        path = "/<redacted>"
    return f"{parsed.scheme}://{netloc}{path}"


def _unavailable_redirect_message(final_url: str) -> str:
    """Build the access-gate message for a redirect to ``notebooklm.google``.

    The redirect is Google's region / anti-abuse gate (commonly
    ``?location=unsupported``), not expired auth or a page-structure change. We
    surface the ``location`` parameter explicitly because :func:`_safe_url` drops
    the query — and that parameter is the actual diagnostic.
    """
    location = notebooklm_unavailable_location(final_url)
    where = _safe_url(final_url)
    target = f"{where} (location={location})" if location else where
    return (
        f"NotebookLM redirected this request to its region / anti-abuse access gate: "
        f"{target}. This is not a library bug or an expired login. Likely a VPN/proxy "
        "or datacenter IP, or an IP/timezone/language mismatch. Verify by opening "
        "https://notebooklm.google.com in a normal browser on the same network; if it "
        "redirects there too, use a residential connection in a supported region."
    )


def extract_csrf_from_html(html: str, final_url: str = "") -> str:
    """
    Extract CSRF token (SNlM0e) from NotebookLM page HTML.

    The CSRF token is embedded in the page's WIZ_global_data JavaScript object.
    It's required for all RPC calls to prevent cross-site request forgery.

    Args:
        html: Page HTML content from notebooklm.google.com
        final_url: The final URL after redirects (for error messages)

    Returns:
        CSRF token value (typically starts with "AF1_QpN-")

    Raises:
        ValueError: Preserved for backward compatibility — raised both when
            redirected to a Google login page and when the token is missing
            from a non-redirect response. Existing callers and tests rely on
            the ``"CSRF token not found"`` / ``"Authentication expired"``
            message substrings, so we intentionally keep the legacy type.
            Internally we delegate to :func:`extract_wiz_field` so the regex
            matrix (double-quoted / single-quoted / HTML-escaped) is shared.
    """
    # Tolerant extraction via the unified helper — accepts canonical,
    # single-quoted, and HTML-escaped variants of the WIZ_global_data field.
    token = extract_wiz_field(html, "SNlM0e", strict=False)
    if token is not None:
        return token
    # Drift path: differentiate "access gate" / "auth expired" / "shape changed"
    # because the remediation differs (fix environment / re-login / file a bug).
    # The *final URL* is the authoritative landing signal, so it is checked
    # before the weaker ``contains_google_auth_redirect(html)`` body scan — the
    # ``notebooklm.google`` gate page itself carries an ``accounts.google.com``
    # sign-in link, which would otherwise mis-route it to "auth expired" (#1630).
    if is_notebooklm_unavailable_redirect(final_url):
        raise ValueError(_unavailable_redirect_message(final_url))
    if is_google_auth_redirect(final_url) or contains_google_auth_redirect(html):
        raise ValueError(
            "Authentication expired or invalid. Run 'notebooklm login' to re-authenticate."
        )
    raise ValueError(
        f"CSRF token not found in HTML. Final URL: {_safe_url(final_url)}\n"
        "This may indicate the page structure has changed."
    )


def extract_session_id_from_html(html: str, final_url: str = "") -> str:
    """
    Extract session ID (FdrFJe) from NotebookLM page HTML.

    The session ID is embedded in the page's WIZ_global_data JavaScript object.
    It's passed in URL query parameters for RPC calls.

    Args:
        html: Page HTML content from notebooklm.google.com
        final_url: The final URL after redirects (for error messages)

    Returns:
        Session ID value

    Raises:
        ValueError: Preserved for backward compatibility — raised both when
            redirected to a Google login page and when the session ID is
            missing from a non-redirect response. See
            :func:`extract_csrf_from_html` for the rationale.
    """
    sid = extract_wiz_field(html, "FdrFJe", strict=False)
    if sid is not None:
        return sid
    # Final-URL landing host is authoritative; check it before the body scan
    # (the gate page carries an accounts.google.com link). See extract_csrf.
    if is_notebooklm_unavailable_redirect(final_url):
        raise ValueError(_unavailable_redirect_message(final_url))
    if is_google_auth_redirect(final_url) or contains_google_auth_redirect(html):
        raise ValueError(
            "Authentication expired or invalid. Run 'notebooklm login' to re-authenticate."
        )
    raise ValueError(
        f"Session ID not found in HTML. Final URL: {_safe_url(final_url)}\n"
        "This may indicate the page structure has changed."
    )
