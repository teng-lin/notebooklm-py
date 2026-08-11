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

This module also owns the *failure taxonomy* for a missing token — see
:func:`_extraction_failure`. Classification is driven exclusively by the final
URL (plus, optionally, the redirect history); the page **body is never scanned**
for auth signals. Scanning it for ``accounts.google.com`` links is the defect
fixed in #2038: nearly every Google-served page carries such a link, so the scan
reported a valid session as expired (#2019).

Private helpers (also re-exported as white-box affordances for tests):

* :func:`_build_wiz_field_patterns` — the ordered regex patterns.
* :func:`_safe_url` — credential-stripping URL formatter for error messages.
* :func:`_url_only_extraction_failure` — the single source of truth for the
  gate → cookie-mismatch → auth-redirect precedence. ``_auth.refresh`` calls it
  before parsing a response body, because Google's login page carries its own
  ``SNlM0e`` and would otherwise be mistaken for a valid app page.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlparse

from .._env import get_base_host
from .._url_utils import (
    find_cookie_mismatch_hop,
    is_google_auth_redirect,
    is_notebooklm_app_host,
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
# a future leak path emerges rather than the broad family.
_AUTH_PATH_REDACT_HOSTS: frozenset[str] = frozenset(
    {
        "accounts.google.com",
        "oauth2.googleapis.com",
        "oauth2.googleusercontent.com",
    }
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
    * **Path** — restricted to ``_AUTH_PATH_REDACT_HOSTS`` (Google's OAuth
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
    # Match the exact host OR any subdomain — both ``accounts.google.com``
    # and ``x.accounts.google.com`` count. Driving subdomain matching off the
    # frozenset (instead of hardcoded ``.endswith`` calls per host) keeps
    # adding a new host a one-line change.
    if (
        path
        and path != "/"
        and any(host_lc == h or host_lc.endswith("." + h) for h in _AUTH_PATH_REDACT_HOSTS)
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

    The "verify by opening" step names the **configured** host: the gate is
    evaluated per request, so the only reproduction that means anything is one
    against the host this client actually called. A fixed URL sends the user to
    confirm a different host than the one that failed.
    """
    location = notebooklm_unavailable_location(final_url)
    where = _safe_url(final_url)
    target = f"{where} (location={location})" if location else where
    return (
        f"NotebookLM redirected this request to its region / anti-abuse access gate: "
        f"{target}. This is not a library bug or an expired login. Likely a VPN/proxy "
        f"or datacenter IP, or an IP/timezone/language mismatch. Verify by opening "
        f"https://{get_base_host()} in a normal browser on the same network; if it "
        f"redirects there too, use a residential connection in a supported region."
    )


def _cookie_mismatch_message(hop: str, final_url: str) -> str:
    """Build the diagnostic for a redirect chain that hit ``/CookieMismatch``.

    Google serves that interstitial when the cookies presented do not match the
    host they were sent to, then 302s onward to a ``support.google.com`` help
    article. The landing page is an ordinary HTTP 200 whose body is full of
    ``accounts.google.com`` links, which is exactly what used to make this
    report as an expired login (#2038; six days of false alarms on #2019).

    The remediation is cookie *scoping*, not merely re-authentication, so the
    message names the mismatch explicitly. ``_safe_url`` would redact the path
    of any ``accounts.google.com`` URL, which would erase the very marker being
    reported, so the hop is rendered from its already-validated parts
    (scheme + host + the literal ``/CookieMismatch``) — userinfo, query and
    fragment are dropped exactly as ``_safe_url`` would drop them.
    """
    parsed = urlparse(hop)
    hop_display = f"{parsed.scheme}://{parsed.hostname or ''}/CookieMismatch"
    landed = _safe_url(final_url)
    chain = f"{hop_display}, and the chain ended at {landed}" if landed else hop_display
    return (
        f"Google's CookieMismatch page was reached during this request: {chain}. Google "
        "rejected the cookies as not matching the host they were sent to. This is a "
        "cookie-scoping problem and NOT necessarily an expired session — the credentials "
        "may be perfectly valid. Common causes: a storage_state.json whose per-cookie "
        "domains were flattened to a single host, cookies belonging to a different Google "
        "account or session, or a stale __Secure-1PSIDTS. Run 'notebooklm login' to re-extract "
        "cookies; if it recurs, verify that storage_state.json preserves each cookie's "
        "original domain."
    )


def _token_not_found_message(what: str, final_url: str) -> str:
    """Build the "token missing" diagnostic, split by where the response landed.

    Two genuinely different failures used to share one message (and, worse, were
    routinely mislabelled "Authentication expired" by a body scan):

    * **Off the app host** — we never reached NotebookLM at all, so the page
      simply has no ``WIZ_global_data`` to find. That is a redirect/environment
      problem, and the final URL is the whole diagnosis.
    * **On the app host** — the app answered but the token is not where we look,
      i.e. a real page-structure change worth filing a bug about.

    ``what`` is the human name of the missing field (``"CSRF token"`` /
    ``"Session ID"``); the ``"<what> not found in HTML"`` prefix is a documented
    message contract and is preserved in both branches.
    """
    # No URL at all is treated as the on-app-host case: the caller gave us
    # nothing to contradict "the app answered but the token moved", and that is
    # the message this path has always emitted.
    if not final_url or is_notebooklm_app_host(final_url):
        detail = "This may indicate the page structure has changed."
    else:
        detail = (
            "The response did not come from a NotebookLM app host, so the request never "
            "reached the app — this is a redirect/environment problem, not a page-structure "
            "change. Note that most Google-served pages carry accounts.google.com links, so "
            "their presence in the body is not evidence that the session expired."
        )
    return f"{what} not found in HTML. Final URL: {_safe_url(final_url)}\n{detail}"


class _LoginRedirectError(ValueError):
    """Private typed signal for a confirmed Google login redirect."""


def _url_only_extraction_failure(final_url: str, redirect_urls: Sequence[str]) -> ValueError | None:
    """Classify the failures that are decidable from the URLs alone, else ``None``.

    This is the **single source of truth for branch order**, shared with
    ``_auth.refresh._fetch_tokens_with_jar``. That caller must run these checks
    *before* handing the body to :func:`extract_csrf_from_html`, and not merely
    as an optimisation.

    Google's sign-in page carries a ``WIZ_global_data`` block with **its own**
    ``SNlM0e`` and ``FdrFJe``, so the extractors — which key purely on the
    presence of those fields and never check what host answered — return the
    *sign-in page's* tokens rather than raising. Live-captured 2026-08-03,
    anonymous (no cookies), Chrome UA::

        GET https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fnotebooklm.google.com%2F
        -> 200 https://accounts.google.com/v3/signin/identifier?...&flowName=GlifWebSignIn
           ...,"SNlM0e":"ALX_<redacted>:1785760591977","FdrFJe":"<19 digits>",...

    Note the reproducibility caveat: a *bare* ``/ServiceLogin`` with no
    ``continue=`` serves a different, smaller page with neither field. The
    ``continue=`` form is the one our own redirect chain produces, which is
    exactly the case that matters here. Google serves this surface differently
    by UA, cookies and flow, so treat the shape — not the byte count — as the
    finding.

    Because both paths call this one function they cannot drift out of order —
    an earlier revision hand-rolled the same checks in ``refresh.py`` and got
    the gate/mismatch precedence wrong there while the extractor had it right
    (caught in review of #2038).
    """
    if is_notebooklm_unavailable_redirect(final_url):
        return ValueError(_unavailable_redirect_message(final_url))
    hop = find_cookie_mismatch_hop((*redirect_urls, final_url))
    if hop is not None:
        return ValueError(_cookie_mismatch_message(hop, final_url))
    if is_google_auth_redirect(final_url):
        return _LoginRedirectError(
            f"Authentication expired or invalid. Final URL: {_safe_url(final_url)}\n"
            "Run 'notebooklm login' to re-authenticate."
        )
    return None


def _extraction_failure(what: str, final_url: str, redirect_urls: Sequence[str]) -> ValueError:
    """Classify a failed ``WIZ_global_data`` extraction into an actionable error.

    Four outcomes, each with a different remediation, ordered so the strongest
    evidence wins:

    1. **Region / anti-abuse gate** (``notebooklm.google``) — fix the network
       environment. Checked first because that gate page carries an
       ``accounts.google.com`` sign-in link (#1630).
    2. **Cookie mismatch** — fix cookie scoping. Checked before the auth branch
       because the interstitial lives on ``accounts.google.com`` and would
       otherwise be reported as an expiry (#2038).
    3. **Auth redirect** — re-authenticate. Driven by the *final URL* only.
    4. **Token missing** — see :func:`_token_not_found_message`.

    Note that branch 2 wins on a mismatch hop *anywhere* in the chain, including
    the (unobserved) shape where the chain passes through ``/CookieMismatch`` and
    then recovers back onto an app host. That precedence is deliberate: a
    mismatch hop is strong evidence of a cookie fault, and an app host that
    answers without a token after one is far more likely to be serving a
    degraded/signed-out shell than to have changed its page structure. The
    message still names where the chain actually ended, so the alternative
    reading stays available to the operator.

    Deliberately takes no ``html`` argument: the page body is not consulted at
    all. The old ``contains_google_auth_redirect(html)`` fallback matched any
    ``accounts.google.com`` URL anywhere in the body, which nearly every
    Google-served page contains, so it converted "wrong page" into
    "Authentication expired" and discarded the final URL — the one piece of
    evidence that would have identified the real fault. Every branch below
    carries the URL.

    Branches 1-3 are delegated to :func:`_url_only_extraction_failure` so this
    ordering exists in exactly one place; only branch 4 needs the page itself.

    Returns the error rather than raising it so each caller's ``raise``
    statement keeps the function's ``NoReturn``-free control flow obvious to
    both readers and type checkers.
    """
    url_failure = _url_only_extraction_failure(final_url, redirect_urls)
    if url_failure is not None:
        return url_failure
    return ValueError(_token_not_found_message(what, final_url))


def extract_csrf_from_html(
    html: str,
    final_url: str = "",
    *,
    redirect_urls: Sequence[str] = (),
) -> str:
    """
    Extract CSRF token (SNlM0e) from NotebookLM page HTML.

    The CSRF token is embedded in the page's WIZ_global_data JavaScript object.
    It's required for all RPC calls to prevent cross-site request forgery.

    Args:
        html: Page HTML content from the configured app host
        final_url: The final URL after redirects (for error messages)
        redirect_urls: The redirect chain that preceded ``final_url``, in order
            (``[str(r.url) for r in response.history]``). Optional, and only
            used to build a better diagnostic: Google's ``/CookieMismatch``
            interstitial 302s onward, so it is invisible to ``final_url`` alone.
            Omitting it costs only that one classification.

    Returns:
        CSRF token value (typically starts with "AF1_QpN-")

    Raises:
        ValueError: Preserved for backward compatibility — raised both when
            redirected to a Google login page and when the token is missing
            from a non-redirect response. Existing callers and tests rely on
            the ``"CSRF token not found"`` / ``"Authentication expired"``
            message substrings, so we intentionally keep the legacy type.
            Internally we delegate to :func:`extract_wiz_field` so the regex
            matrix (double-quoted / single-quoted / HTML-escaped) is shared,
            and to :func:`_extraction_failure` so the failure taxonomy is
            shared with :func:`extract_session_id_from_html`.
    """
    # Tolerant extraction via the unified helper — accepts canonical,
    # single-quoted, and HTML-escaped variants of the WIZ_global_data field.
    token = extract_wiz_field(html, "SNlM0e", strict=False)
    if token is not None:
        return token
    raise _extraction_failure("CSRF token", final_url, redirect_urls)


def extract_session_id_from_html(
    html: str,
    final_url: str = "",
    *,
    redirect_urls: Sequence[str] = (),
) -> str:
    """
    Extract session ID (FdrFJe) from NotebookLM page HTML.

    The session ID is embedded in the page's WIZ_global_data JavaScript object.
    It's passed in URL query parameters for RPC calls.

    Args:
        html: Page HTML content from the configured app host
        final_url: The final URL after redirects (for error messages)
        redirect_urls: The redirect chain that preceded ``final_url``. See
            :func:`extract_csrf_from_html`.

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
    raise _extraction_failure("Session ID", final_url, redirect_urls)
