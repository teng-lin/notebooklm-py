"""Pure, conservative content checks shared by ingestion and MCP advisories."""

from __future__ import annotations

#: A READY ``web_page`` source whose indexed text is shorter than this many
#: characters gets a non-blocking content-sanity ``warning`` on ``source_wait``
#: (likely a dead link / soft-404 / paywall "ghost source"). Deliberately
#: conservative — advisory only, never a rejection. See :func:`_thin_content_warning`.
_THIN_SOURCE_CHAR_THRESHOLD = 100

#: Per-source budget for the advisory thin-content body fetch. The check is
#: best-effort and must not let ``source_wait`` overrun its own ``timeout`` waiting
#: on a slow ``GET_SOURCE`` — a fetch that exceeds this degrades to no warning.
_THIN_SOURCE_FETCH_TIMEOUT_SECONDS = 5.0

#: A READY ``web_page`` source can ingest a soft-404 / dead link as a full-bodied
#: page (HTTP 200) whose body is the site's "broken link" boilerplate — too long to
#: trip the char-thin rule above. Only bodies SHORTER than this are scanned for a
#: dead-link phrase below: a real error-page body is almost always small, and the
#: gate keeps a large healthy page from being lowercased + substring-scanned (and
#: narrows the false-positive window for the weaker phrases). The reported case was
#: 1,766 chars; 2000 leaves a ~13% margin.
_SOFT_404_BODY_SCAN_LIMIT = 2000

#: Multi-word / anchored dead-link & error-page markers scanned (casefolded) in a
#: sub-:data:`_SOFT_404_BODY_SCAN_LIMIT` ``web_page`` body. Deliberately anchored —
#: NO bare ``"404"`` / ``"oops"`` / ``"not found"`` — and, because they only fire on
#: a short body, even the weaker markers ("broken link") match only a soft-404-shaped
#: page. Advisory only; misses non-English / non-matching error pages. See
#: :func:`_thin_content_warning`.
_SOFT_404_PHRASES = frozenset(
    {
        "broken link",
        "page not found",
        "page isn't available",
        "page does not exist",
        "page no longer available",
        "no longer available",
        "error 404",
        "404 not found",
        "whoops!",
    }
)

#: A bot-challenge / WAF interstitial (Cloudflare "Just a moment…", Akamai "Access
#: Denied") also serves HTTP 200 and ingests as a READY ``web_page``, but its body
#: clears the char-thin gate and carries none of the dead-link vocabulary above, so
#: #1709 missed it. Scanned (casefolded) up to :data:`_BOT_CHALLENGE_BODY_SCAN_LIMIT`
#: — a wider cap than :data:`_SOFT_404_BODY_SCAN_LIMIT` because a challenge page
#: carries more script/boilerplate than a soft-404 stub, so a real interstitial can
#: land just over the tighter dead-link cap. Anchored to interstitial phrasing (no
#: bare ``"cloudflare"`` / ``"cookies"``) so a real article that merely mentions a
#: WAF vendor doesn't false-positive; advisory only, misses non-English pages. See
#: :func:`_thin_content_warning`.
_BOT_CHALLENGE_BODY_SCAN_LIMIT = 5000

_BOT_CHALLENGE_PHRASES = frozenset(
    {
        "just a moment",
        "enable javascript and cookies",
        "checking your browser",
        "attention required",
        "access denied",
        "security verification",
        "captcha",
        # Vendor-anchored: bare ``ray id`` is a substring of ordinary text like
        # "array id" / "array identifier"; require the Cloudflare prefix so a normal
        # technical page can't trip a false WAF warning (#1923 review).
        "cloudflare ray id",
    }
)


def text_content_warning(content: str, *, char_count: int | None = None) -> str | None:
    """Identify thin text, soft errors and bot challenges; absence is not proof of quality."""
    if char_count is None:
        char_count = len(content.strip())
    if char_count < _THIN_SOURCE_CHAR_THRESHOLD:
        return (
            f"little/no text extracted ({char_count} chars) — may be empty, "
            "not-yet-indexed, a soft-404/dead link, blocked, or paywalled; "
            'verify with source_read (detail="full").'
        )
    # ponytail: short multi-word phrase scans over a length-gated body — no
    # liveness probe, no classifier; misses non-English / long-bodied error pages.
    # The bot-challenge cap is the wider of the two, so it drives the outer gate;
    # the dead-link pass keeps its own tighter cap. Dead-link takes precedence when
    # a body somehow trips both.
    if char_count < _BOT_CHALLENGE_BODY_SCAN_LIMIT:
        # ``content`` is typed ``str`` but guard against a backend that reports a
        # non-thin ``char_count`` yet a ``None`` body — make the intent explicit
        # rather than lean on the outer ``except`` silently swallowing it.
        body = (content or "").casefold()
        if char_count < _SOFT_404_BODY_SCAN_LIMIT and any(
            phrase in body for phrase in _SOFT_404_PHRASES
        ):
            return (
                f"ingested as ready ({char_count} chars) but the body matches a "
                "dead-link / error-page pattern (e.g. 'broken link') — likely a "
                'soft-404; verify with source_read (detail="full").'
            )
        if any(phrase in body for phrase in _BOT_CHALLENGE_PHRASES):
            return (
                f"ingested as ready ({char_count} chars) but the body matches a "
                "bot-challenge / WAF interstitial pattern (e.g. 'Just a moment' / "
                "'access denied') — the real page was likely blocked "
                '(Cloudflare / Akamai); verify with source_read (detail="full").'
            )
    return None
