"""Canonical runtime registry of credential-shaped names and token shapes.

This is the single source of truth that the logging redaction patterns
(:mod:`notebooklm._logging`) and the exception scrubbing (
:mod:`notebooklm.exceptions`, via ``scrub_secrets``) DERIVE from. Before this
module the redaction cookie alternation was enumerated inline in
``_logging.py`` and drifted from the project's own cassette-sanitization
registry (``tests/cassette_patterns.py``): session cookies the cassette
registry classifies as must-scrub (``NID`` / ``LSOLH`` / ``__Host-GAPS``) were
absent from the runtime alternation, so a bare ``NID=g.a000-...`` token logged
at DEBUG (refresh-cmd stdout/stderr) round-tripped verbatim through
``scrub_secrets`` (issue #1517).

Runtime code cannot import from ``tests/`` (the test tree is not shipped), so
this module re-states the canonical names here. A PARITY GUARDRAIL test
(``tests/_guardrails/test_runtime_secret_registry_parity.py``) asserts that
this registry stays in lockstep with the cassette registry on every axis:
:data:`RUNTIME_SESSION_COOKIES` is a SUPERSET of the cassette registry's
must-scrub bare session cookies; the ``__Secure-*`` / ``__Host-*`` umbrellas
cover every cassette ``SECURE_COOKIES`` / ``HOST_COOKIES`` name *by
construction*; and :data:`AUTH_TOKEN_SHAPE_PATTERNS` is regex-string-equal to
the cassette registry's token-shape + Google-API-key set. So any new must-scrub
shape added to the cassette registry forces this runtime registry to keep up.

Three complementary layers:

1. **Name-anchored cookie alternation** (:data:`RUNTIME_SESSION_COOKIES`).
   ``Name=Value`` cookie pairs whose name is on this list have their value
   redacted, preserving the name as a shape hint. This mirrors the cassette
   registry's bare ``SESSION_COOKIES``.

2. **Prefix umbrellas** (:data:`SECURE_HOST_UMBRELLA_PATTERNS`). Any
   ``__Secure-…=`` / ``__Host-…=`` cookie pair is redacted by its prefix alone,
   so a *future* secure/host cookie name (one not yet enumerated anywhere) fails
   closed by construction — closing the gap a name-list-only guard would leave
   (codex review of #1517: ``__Secure-NEWSESSION=opaqueBase64`` must not leak).

3. **Carrier-agnostic shape catch-alls** (:data:`AUTH_TOKEN_SHAPE_PATTERNS`).
   Defense in depth: the raw ``g.a000-`` / ``sidts-`` / ``ya29.`` credential
   prefixes AND the Google API-key shape (``AIza…``) are redacted wherever they
   appear — even under an UNKNOWN carrier name — so disclosure fails closed
   regardless of which cookie/field carries the value. Ported from the cassette
   registry's ``_AUTH_TOKEN_PATTERNS`` + ``_GOOGLE_API_KEY_PATTERN``.
"""

from __future__ import annotations

import re

__all__ = [
    "AUTH_TOKEN_SHAPE_PATTERNS",
    "COOKIE_VALUE_REPLACEMENT",
    "COOKIE_VALUE_SUFFIX",
    "RUNTIME_SESSION_COOKIES",
    "SECURE_HOST_UMBRELLA_PATTERNS",
    "build_cookie_alternation",
]


# Shared quote-aware ``=<value>`` suffix for the name-anchored cookie patterns
# (the bare-name alternation in ``_logging.py`` AND the ``__Secure-*`` /
# ``__Host-*`` umbrellas below). RFC 6265 permits a cookie value to be wrapped in
# double quotes (``cookie-value = *cookie-octet / DQUOTE *cookie-octet DQUOTE``),
# so a refresh-cmd log line may carry ``SID="opaque"`` / ``__Secure-X="opaque"``.
# An optional opening ``"`` (group +1) and closing ``"`` (group +3) bracket the
# value (group +2); the value class still EXCLUDES ``"`` so it stops before the
# closing quote rather than swallowing it. Without the optional quotes the whole
# pattern failed to match a quoted value and it LEAKED — gemini review of #1530.
#
# Both consumers place exactly ONE capture group (the cookie name) before this
# suffix, so the suffix groups are always 2/3/4 and the shared
# :data:`COOKIE_VALUE_REPLACEMENT` (``\1=\2***\4``) preserves the name, the
# ``=``, and the surrounding quotes (if any) while collapsing the value to
# ``***``. Keep that one-preceding-group invariant when reusing the suffix.
COOKIE_VALUE_SUFFIX = r"=(\"?)([^;\s,\"'<>]+)(\"?)"
COOKIE_VALUE_REPLACEMENT = r"\1=\2***\4"


# Bare Google session cookie names whose VALUES must be redacted from any log
# line or exception surface. This is the runtime analog of the cassette
# registry's ``SESSION_COOKIES``. ``__Secure-*`` / ``__Host-*`` cookies are NOT
# enumerated here — they are caught by the prefix umbrellas below (fail-closed
# for future names), exactly as the cassette scrubber handles them. Longer /
# more-specific names appear first so the cookie alternation built from this
# list does not let a shorter name shadow a longer one (e.g. ``SAPISID`` before
# ``APISID``). The parity guardrail keeps this a superset of the cassette
# registry's must-scrub bare session cookies.
RUNTIME_SESSION_COOKIES: tuple[str, ...] = (
    "SAPISID",
    "APISID",
    "SIDCC",
    "LSOLH",
    "HSID",
    "SSID",
    "OSID",
    "LSID",
    "NID",
    "SID",
)


# ``__Secure-*`` / ``__Host-*`` cookie-pair umbrellas. The prefix is distinctive
# enough that no legitimate non-protected cookie shares it, so the value is
# redacted by prefix alone — a future secure/host cookie name (not yet
# enumerated anywhere) fails closed by construction.
#
# The cookie-NAME class is "any run up to the ``=``", mirroring the cassette
# registry's ``(__Secure-[^=]+)=…`` / ``(__Host-[^=]+)=…`` umbrellas. A narrower
# class such as ``[A-Za-z0-9_-]+`` would LEAK any RFC 6265 ``token``-charset name
# containing ``. ! # $ % & ' * + ^ ` | ~`` (e.g. ``__Secure-NEW.SESSION=…``,
# ``__Host-GAPS.v2=…``) — codex review of #1517. The only exclusions are the
# cookie-pair DELIMITERS the surrounding log text uses — ``=`` (name/value
# split) plus whitespace / ``;`` / ``,`` (pair + prose boundaries) — so the name
# captures the WHOLE RFC token (including ``'`` / `` ` `` / ``~``) yet a bare
# ``__Secure-[^=]+`` on a free-form log line cannot swallow surrounding prose the
# way the cassette form (which runs on an already-parsed single cookie scalar)
# safely can. Over-broad here errs toward MORE redaction, which is the fail-safe
# direction. Group 1 is the full cookie name (preserved as a shape hint); the
# value (quoted or not) is matched by the shared :data:`COOKIE_VALUE_SUFFIX`.
SECURE_HOST_UMBRELLA_PATTERNS: tuple[str, ...] = (
    r"(__Secure-[^=\s;,]+)" + COOKIE_VALUE_SUFFIX,
    r"(__Host-[^=\s;,]+)" + COOKIE_VALUE_SUFFIX,
)


# Carrier-agnostic Google credential shapes, applied as defense-in-depth so a
# secret leaks NOTHING even when it rides inside a cookie / field whose name is
# not on :data:`RUNTIME_SESSION_COOKIES`. Ported verbatim from the cassette
# registry's ``_AUTH_TOKEN_PATTERNS`` + ``_GOOGLE_API_KEY_PATTERN`` (the parity
# guardrail asserts regex-string equality so the two cannot drift):
#
#   * ``g.a000-`` — the raw SID token embedded in SID/LSID/LSOLH cookie values
#     and OAuth flows. The prefix is distinctive enough to need no length floor;
#     the REQUIRED trailing ``-`` keeps the bare account-prefix ``g.a000`` from
#     matching.
#   * ``sidts-`` / ``ya29.`` — less distinctive prefixes, so each carries a
#     length floor (``{10,}`` / ``{20,}``) to avoid firing on an incidental
#     short literal (a bare ``ya29`` mention in prose).
#   * ``AIza…`` — the canonical Google API-key shape (``AIza`` + 35-or-more
#     key chars). The NotebookLM web page embeds one in ``WIZ_global_data``
#     (``JrWMbf`` / ``B8SWKb`` / ``VqImj`` fields); routed through
#     ``data_at_failure`` / ``payload_preview`` it would otherwise leak. ``{35,}``
#     (not ``{35}``) is greedy so a longer-than-canonical key is consumed whole,
#     never leaving a re-matchable tail fragment (cassette-registry rationale).
AUTH_TOKEN_SHAPE_PATTERNS: tuple[str, ...] = (
    r"g\.a000-[A-Za-z0-9_\-]+",
    r"sidts-[A-Za-z0-9_\-]{10,}",
    r"ya29\.[A-Za-z0-9_\-]{20,}",
    r"AIza[0-9A-Za-z_\-]{35,}",
)


def build_cookie_alternation(names: tuple[str, ...] = RUNTIME_SESSION_COOKIES) -> str:
    """Return a regex alternation of escaped cookie names, longest-first.

    Sorting by descending length is a defensive convention: Python's ``re``
    engine backtracks, so ordering is not load-bearing for correctness, but
    longest-first minimizes backtracking and guarantees the captured name group
    reflects the full cookie name rather than a matching suffix.
    """
    return "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
