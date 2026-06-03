"""Canonical registry of cassette-mutating utilities.

This module is the single source of truth for what counts as sensitive in a
recorded HTTP cassette and for cassette-byte-count surgery. It exports two
complementary halves:

1. **Sanitization registry.** A canonical
   list of regex (pattern, replacement) pairs covering Google session
   cookies, ``__Secure-*`` / ``__Host-*`` cookies, WIZ_global_data token
   fields, email addresses, and Playwright ``storage_state`` cookie objects;
   a single ``scrub_string`` entry point that applies them; and an
   ``is_clean`` validator that judges cookie-value cleanliness via exact-
   match membership in ``SCRUB_PLACEHOLDERS`` (closing a previous
   "starts with S" character-class hole). Before this consolidation the
   same patterns lived as an inline ``SENSITIVE_PATTERNS`` list in
   :mod:`tests.vcr_config` and were duplicated piecemeal in
   ``tests/check_cassettes_clean.sh`` — that drift risk is what motivated
   the consolidation.

2. **Chunked-response byte-count re-derivation.** The
   :func:`recompute_chunk_prefix` helper walks an XSSI-framed batchexecute
   body and rewrites every digit-only ``<count>`` header to match the actual
   byte-length of the immediately-following payload line. After scrubbing
   replaces a 21-char user ID with the 17-char ``SCRUBBED_USER_ID``
   placeholder the advertised count no longer matches the payload, so this
   helper runs as a second pass inside :func:`tests.vcr_config.scrub_response`
   to keep cassettes self-consistent and avoid tripping the decoder's
   byte-count-mismatch DEBUG log during replay.

Why both halves live here, not split into two modules:

- ``vcr_config.py`` is loaded for every VCR-decorated test, but its public
  surface is intentionally narrow (the VCR object + matchers). Scrub-time
  string surgery is a separate concern and benefits from being importable
  on its own (the bulk re-scrub script in ``scripts/`` imports both
  ``scrub_string`` AND ``recompute_chunk_prefix`` directly).
- Decoder tolerance behavior in ``src/notebooklm/rpc/decoder.py`` (still
  parses the JSON on byte-count mismatch, now logging at DEBUG rather
  than WARNING — see #669) is what makes the recompute pass optional for
  correctness; these helpers exist so cassettes stay self-consistent for
  shape-lint and don't add log noise during replay, not to harden the
  decoder against drift in production responses.

Exports
-------
- :data:`SESSION_COOKIES`     standard Google session cookie names
- :data:`SECURE_COOKIES`      ``__Secure-*`` cookie names (caught by umbrella)
- :data:`HOST_COOKIES`        ``__Host-*`` cookie names (caught by umbrella)
- :data:`OPTIONAL_COOKIES`    non-essential cookies surfaced for completeness
- :data:`EMAIL_PROVIDERS`     provider domains we redact in emails
- :data:`SCRUB_PLACEHOLDERS`  exact-match allowlist of expected sentinels
- :data:`DISPLAY_NAME_FALSE_POSITIVES`  two-Cap-word strings to NEVER scrub
- :data:`SENSITIVE_PATTERNS`  ordered (regex, replacement) registry
- :func:`scrub_string`        single sanitization entry point
- :func:`is_clean`            validator returning ``(ok, leaks)``
- :func:`find_credential_leaks`   high-severity-shape-only subset (fixture-safe)
- :func:`find_high_entropy_leaks` field-agnostic novel-token entropy backstop
- :func:`recompute_chunk_prefix`  XSSI byte-count re-derivation

Necessary-not-sufficient boundary (issue #1382)
-----------------------------------------------
The name-anchored / known-shape detectors below are NECESSARY but not
SUFFICIENT: a novel credential prefix, or a known secret in an un-targeted
field, passes them silently (this has bitten the guard twice — ``LSID`` and
``JrWMbf``). :func:`find_high_entropy_leaks` is the deliberate field-agnostic
complement that catches a long high-entropy base64/hex token in ANY quoted
JSON scalar. See its KNOWN-SHAPE BOUNDARY block for the residual-risk decision
this draws (it is scoped to quoted scalars on purpose; non-quoted high-entropy
text remains GitHub-secret-scanning's job). Relates to ADR-006.

Upload + Drive token coverage
-----------------------------
The registry below extends the canonical cookie/CSRF/email coverage with
scrubbers for Google's resumable-upload and Drive integration paths:

* ``X-GUploader-UploadID`` response headers leak per-upload session tokens.
* ``upload_id=...`` query parameters echo the same token into request URLs.
* ``AONS...`` strings are Drive ACL/permission tokens emitted with file
  metadata. The 20-char tail threshold avoids matching incidental
  ``AONS`` mentions in code or docs.
* Drive file IDs (33-44 char ``[A-Za-z0-9_-]`` strings) appear inside
  ``"file_id": "..."`` JSON keys and ``/drive/v3/files/<id>`` URLs. The
  pattern is intentionally context-anchored so that bare 36-char UUIDs
  (artifact IDs, source IDs, conversation IDs) are NOT scrubbed — those
  are NotebookLM-internal identifiers and matching them would corrupt
  cassette replay.

Display-name + avatar coverage
------------------------------
The registry below also covers two display/identity leak shapes that the
core structured scrubbers miss because the data is double-encoded inside a
WRB-payload JSON string:

* **Escaped JSON display-name literals.** Google's sharing RPCs emit owner
  metadata as positional list elements inside a stringified WRB payload —
  the display name surfaces as ``\\"First Last\\"`` rather than a
  structured ``"displayName": "..."`` key. The core structured patterns
  key-anchor on the outer JSON key, so they never fire on the inner
  double-encoded form. The display-name pattern anchors on the
  escape-quote shape ``\\"...\\"`` and carries an explicit false-positive
  allowlist (font families, UI titles, artifact/notebook names produced
  by the test corpus) so that legitimate two-Capitalized-word fixture
  content is preserved. This false-positive list is the load-bearing
  safety net — a broad
  ``>[A-Z][a-z]+\\s[A-Z][a-z]+<`` regex without it would corrupt source-
  rename and artifact-list cassettes during replay.
* **lh3.googleusercontent.com avatar URLs.** Both the ``/a/`` and ``/ogw/``
  path forms carry per-user avatar tokens. The pattern collapses the whole
  URL (host + path + token, including any trailing ``=s512``-style sizing
  suffix) to ``SCRUBBED_AVATAR_URL``.

Google API-key coverage
-----------------------
The NotebookLM web page embeds a Google API key in ``WIZ_global_data``. It
surfaces across several field names (``B8SWKb``, ``VqImj``, and ``JrWMbf``);
each is scrubbed by a name-anchored pattern. ``JrWMbf`` was originally missing,
so its value round-tripped unscrubbed into the interactive mind-map cassettes —
the API-key analog of the ``LSID`` cookie leak. To close that class of gap for
good, the registry also carries a field-name-agnostic catch-all
(:data:`_GOOGLE_API_KEY_PATTERN`) that collapses the canonical ``AIza`` +
35-char Google API-key shape to ``SCRUBBED_API_KEY`` wherever it appears, with a
matching ``is_clean`` detector so any unredacted key is flagged regardless of
which field carried it.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# =============================================================================
# Chunked-response byte-count re-derivation
# =============================================================================

# XSSI anti-hijack prefix used by Google batchexecute responses.
# Format: ")]}'" followed by two newlines, then alternating <count>\n<payload>\n
# chunks. See ``src/notebooklm/rpc/decoder.py`` for the parser.
_XSSI_PREFIX = ")]}'\n\n"

# A "chunk header" line is a line consisting of ONLY ASCII digits — that's the
# advertised byte count for the next payload line. Restricting to ASCII digits
# avoids accidentally treating a JSON payload line that happens to start with a
# digit-like character as a header. ``fullmatch`` anchors at both ends so we
# don't need explicit ``\A`` / ``\Z`` (claude-bot review on PR #554).
_CHUNK_HEADER_RE = re.compile(r"\d+")


def recompute_chunk_prefix(body: str) -> str:
    """Re-derive ``<count>`` prefixes in a chunked response body.

    Google's batchexecute responses are framed as alternating header/payload
    lines, optionally preceded by the XSSI ``)]}'\\n\\n`` prefix. After
    scrubbing replaces strings of unequal length (e.g. a 21-char user ID with
    the 17-char ``SCRUBBED_USER_ID`` placeholder), the advertised byte-count no
    longer matches the actual payload length, which causes:

    1. ``test_cassette_shapes.py`` byte-count assertion failures.
    2. ``decoder.py`` to emit ``Chunk at line N declares X bytes but payload is
       Y bytes`` DEBUG logs during replay (the JSON is still parsed — see the
       tolerance block at decoder.py:217-237 — but well-formed cassettes
       shouldn't trip the log at all).

    This helper walks the body, identifies every digit-only "header" line that
    is immediately followed by a non-header line, and replaces the header with
    the correct count for that payload. Byte count uses ``len(payload.encode(
    "utf-8"))`` — matching the ``len(json_str.encode("utf-8"))`` calculation
    the decoder uses (which is what the cassette shape lint validates, even
    though Google's live framing appears to use a different unit; see the
    Note: block on :func:`notebooklm.rpc.decoder.parse_chunked_response`).
    For ASCII-only payloads (the common case for batchexecute JSON), this is
    identical to ``len(payload)``, so the shape-lint character-length
    assertion in ``test_cassette_shapes.py`` still passes.

    Idempotent: running the helper on a body whose counts already match yields
    an identical string (no spurious whitespace changes). Conservative: if the
    body doesn't look like a chunked response (no digit-only header lines), it
    is returned unchanged.

    Args:
        body: The response body as a Python ``str``. May or may not be prefixed
            with the XSSI marker.

    Returns:
        The body with every digit-only header line replaced by the correct
        byte-count for the immediately-following payload line. Trailing
        newlines, the XSSI prefix, and non-header lines are preserved verbatim.

    Examples:
        Single-chunk body where the payload was scrubbed shorter::

            >>> recompute_chunk_prefix("18\\n[[\\"longer_id_123\\"]]")
            '18\\n[["longer_id_123"]]'
            >>> recompute_chunk_prefix("18\\n[[\\"x\\"]]")
            '7\\n[["x"]]'

        XSSI-wrapped multi-chunk body::

            >>> body = ")]}'\\n\\n10\\n[1,2,3]\\n20\\n[[\\"a\\"]]\\n"
            >>> # After scrubbing one payload from "[1,2,3]" to "[1,2]" the
            >>> # leading "10" header becomes stale; recompute_chunk_prefix
            >>> # rewrites it to match the new payload length.

    """
    if not body:
        return body

    # Preserve the XSSI prefix exactly. Splitting on it (instead of stripping a
    # fixed number of characters) is robust to alternate-length prefixes if
    # Google ever changes the marker — though only ``)]}'\n\n`` is observed.
    if body.startswith(_XSSI_PREFIX):
        prefix = _XSSI_PREFIX
        remainder = body[len(_XSSI_PREFIX) :]
    else:
        prefix = ""
        remainder = body

    # Splitting on "\n" preserves a trailing empty string if ``remainder`` ends
    # in "\n", which lets us reconstruct the original terminator faithfully via
    # "\n".join(...).
    lines = remainder.split("\n")

    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # A header line is followed by a non-header payload line. Only rewrite
        # when BOTH conditions hold — otherwise leave the line untouched. This
        # protects:
        #  - trailing digit-only sentinels with no payload (we leave them alone
        #    rather than guess what payload they would have referred to)
        #  - JSON payloads that happen to be a single integer literal
        #    immediately preceded by another digit-only line (unlikely in
        #    practice but we'd rather be conservative)
        is_header = _CHUNK_HEADER_RE.fullmatch(line) is not None
        has_payload = i + 1 < len(lines) and not _CHUNK_HEADER_RE.fullmatch(lines[i + 1])
        if is_header and has_payload:
            payload = lines[i + 1]
            new_count = len(payload.encode("utf-8"))
            out.append(str(new_count))
            out.append(payload)
            i += 2
        else:
            out.append(line)
            i += 1

    return prefix + "\n".join(out)


# =============================================================================
# Cookie name categories
# =============================================================================

# Standard Google session cookies. These are the names whose values we scrub
# from both the ``Cookie:`` / ``Set-Cookie:`` header form AND the Playwright
# ``storage_state`` JSON form.
SESSION_COOKIES: list[str] = [
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "SIDCC",
    "OSID",
    "NID",
    # ``LSID`` is the Google **login** SID cookie. Its value embeds a raw
    # ``g.a000-...`` SID token — the same credential family as ``SID`` — yet it
    # was historically MISSING from this list, so its value round-tripped into
    # committed cassettes unscrubbed (the ``notebooks_share.yaml`` leak that
    # motivated this hardening). ``LSOLH`` is the sibling login cookie that
    # carries the same token shape; both are added so neither can leak again.
    "LSID",
    "LSOLH",
]

# ``__Secure-*`` cookies are caught by the umbrella ``__Secure-[^=]+`` pattern;
# this list is the canonical enumeration of names we expect to see in practice.
SECURE_COOKIES: list[str] = [
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PSIDCC",
    "__Secure-3PSIDCC",
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "__Secure-OSID",
]

# ``__Host-*`` cookies, caught by the umbrella ``__Host-[^=]+`` pattern.
HOST_COOKIES: list[str] = [
    "__Host-GAPS",
]

# Optional / non-essential Google cookies. We expose the list for completeness
# but do NOT scrub their values today (they don't carry session secrets).
OPTIONAL_COOKIES: list[str] = [
    "1P_JAR",
    "AEC",
    "CONSENT",
]

# =============================================================================
# Email provider domains we redact
# =============================================================================

EMAIL_PROVIDERS: list[str] = [
    "gmail",
    "googlemail",
    "google",
    "anthropic",
    "outlook",
    "hotmail",
    "yahoo",
    "icloud",
    "protonmail",
]

# =============================================================================
# Placeholder allowlist
# =============================================================================
# These are the only string values that may appear in place of redacted secrets
# inside a committed cassette. ``is_clean`` uses this set as an exact-match
# allowlist when deciding whether a residual cookie value is a real leak — this
# replaces the legacy ``[^S"]`` character-class heuristic that missed any real
# secret starting with the letter ``S``.
SCRUB_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "SCRUBBED",
        "SCRUBBED_CSRF",
        "SCRUBBED_SESSION",
        "SCRUBBED_USER_ID",
        "SCRUBBED_API_KEY",
        "SCRUBBED_CLIENT_ID",
        "SCRUBBED_PROJECT_ID",
        "SCRUBBED_EMAIL",
        "SCRUBBED_NAME",
        # ``SCRUBBED_EMAIL@example.com`` is the rendered form of the email
        # replacement; ``is_clean`` checks the full token, so we list it too.
        "SCRUBBED_EMAIL@example.com",
        # URL-encoded form for ``?authuser=`` query params. The provider-
        # agnostic URL detector would otherwise re-flag the canonical
        # placeholder as a leak (idempotency).
        "authuser=SCRUBBED_EMAIL%40example.com",
        # Double-encoded form for ``%3Fauthuser%3D…`` redirect-param URLs
        # (issue #1368). Same idempotency rationale as the single-encoded
        # placeholder above.
        "authuser%3DSCRUBBED_EMAIL%40example.com",
        # upload + Drive token placeholders.
        "SCRUBBED_UPLOAD_ID",
        "SCRUBBED_UPLOAD_URL",
        "SCRUBBED_AONS",
        "SCRUBBED_DRIVE_FILE_ID",
        # avatar URL placeholder (display-name + avatar scrub group).
        # The display-name escaped-literal scrubber reuses the existing
        # ``SCRUBBED_NAME`` sentinel so a cassette can carry just one
        # canonical replacement string for human names.
        "SCRUBBED_AVATAR_URL",
    }
)


# =============================================================================
# Display-name false-positive allowlist
# =============================================================================
# Two-Capitalized-word strings that LOOK like human display names but are
# legitimate UI / font-family / artifact / notebook titles produced during
# E2E test runs. The escaped-display-name scrubber (section 12 below) carries
# this as a negative lookahead so these strings are NEVER replaced — that
# protection is what keeps source-rename and artifact-list cassettes from
# being corrupted during replay.
#
# This list intentionally mirrors ``DISPLAY_NAME_FALSE_POSITIVES`` in
# ``tests/unit/test_cassette_shapes.py``. The two lists are NOT
# imported from each other to keep ``cassette_patterns.py`` a leaf module —
# the shape-lint module already depends on this registry, and a back-edge
# would create a cycle. New entries must be added to BOTH lists. The unit
# test ``test_display_name_false_positives_mirror_shape_lint`` asserts they
# stay in sync.
DISPLAY_NAME_FALSE_POSITIVES: frozenset[str] = frozenset(
    {
        # Google Sans family (font-family CSS in HTML responses).
        "Google Sans",
        "Google Sans Text",
        "Google Sans Arabic",
        "Google Sans Japanese",
        "Google Sans Korean",
        "Google Sans Simplified Chinese",
        "Google Sans Traditional Chinese",
        # Browser user-agent brand surfaced in Sec-CH-UA HTML responses.
        "Microsoft Edge",
        # Account UI page title (not a person's name).
        "Account Information",
        # Artifact / notebook titles produced by the test corpus.
        "Agent Development Tutorials",
        "Agent Flashcards",
        "Agent Quiz",
        "Slide Deck",
        "Tool Use Loop",
        "Claude Code",
    }
)


# =============================================================================
# Pattern construction helpers
# =============================================================================

_EMAIL_PATTERN_BASE = r"[A-Za-z0-9._%+\-]+@(?:" + "|".join(EMAIL_PROVIDERS) + r")\.com"

# Single-encoded ``authuser=<local>%40<provider>`` query-param shape and its
# double-encoded ``authuser%3D<local>%40<provider>`` sibling. The double-encoded
# form arises when the email-bearing inner URL is itself the *value* of a
# ``continue=`` redirect param, so its ``?authuser=`` gets percent-encoded one
# extra level (``?``→``%3F``, ``=``→``%3D``, ``@``→``%40``) — issue #1368. Both
# anchor on the ``authuser`` separator (literal ``=`` vs encoded ``%3D``) rather
# than on the email's domain, so Workspace / corporate addresses the
# provider-list pattern misses are still caught. The shared email tail uses the
# wire-form local part (``%`` is legal because ``%2B`` etc. arrive encoded)
# followed by the encoded ``%40`` separator and a domain. Neither shape has a
# legitimate non-secret occurrence in a cassette, so collapsing to the canonical
# placeholder is safe and the detectors below treat a match as a leak by
# definition.
#
# ``AUTHUSER_EMAIL_DOUBLE_ENCODED_PATTERN`` is public (no leading underscore)
# because ``scripts/rescrub-cassettes.py`` imports it across the module boundary
# — matching the convention every other cross-imported name in this module
# (``SENSITIVE_PATTERNS``, ``SCRUB_PLACEHOLDERS``, ``find_credential_leaks`` …)
# follows. The ``_AUTHUSER_EMAIL_TAIL`` / ``_AUTHUSER_EMAIL_PATTERN`` helpers
# stay private; they are only consumed within this module.
_AUTHUSER_EMAIL_TAIL = r"[A-Za-z0-9._%+\-]+%40[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
_AUTHUSER_EMAIL_PATTERN = r"authuser=" + _AUTHUSER_EMAIL_TAIL
AUTHUSER_EMAIL_DOUBLE_ENCODED_PATTERN = r"authuser%3D" + _AUTHUSER_EMAIL_TAIL

# Negative-lookahead alternation built from the false-positive allowlist.
# Each entry is regex-escaped because some legitimate UI titles could in
# theory contain regex metacharacters (none do today, but future additions
# might). Sort by descending length so longer prefixes match before shorter
# ones — e.g. ``Google Sans Text`` must be tried before ``Google Sans``,
# otherwise the lookahead would consume only the shared prefix and the
# scrubber would proceed to clobber the longer name.
_DISPLAY_NAME_ALLOWLIST_ALT = "|".join(
    re.escape(name) for name in sorted(DISPLAY_NAME_FALSE_POSITIVES, key=len, reverse=True)
)


# Catch-all Google auth-token shapes, applied as a defense-in-depth first pass
# over EVERY scrubbed string (bodies + headers + cookie values). These are the
# raw credential prefixes that ride inside session cookies, OAuth flows, and
# SIDTS rotation tokens — the ``LSID`` leak proved that a cookie-name allowlist
# alone is insufficient, so we also scrub the token shapes directly wherever
# they appear. ``is_clean`` carries the matching detector
# (``_DETECT_AUTH_TOKEN``) so the cassette guard flags any of these shapes that
# survive.
#
# Anchoring strategy per shape:
#
#   * ``g\.a000-`` — anchored on the literal ``g.a000-`` prefix (note the
#     REQUIRED trailing ``-``). That prefix is itself distinctive enough that
#     no length floor is needed: ``g.a000-<anything>`` in a cassette is a SID
#     token by construction and is never legitimate fixture content, so we
#     scrub even a one-char tail. The hyphen requirement keeps the bare
#     account-prefix ``g.a000`` (no token tail) from matching.
#   * ``sidts-`` / ``ya29\.`` — less distinctive prefixes, so each carries a
#     length floor (``{10,}`` / ``{20,}``) to avoid firing on an incidental
#     short literal such as a bare ``ya29`` mention in a comment.
#
# Anything matched collapses to ``SCRUBBED`` (which contains none of the
# prefixes), so repeated passes are idempotent.
_AUTH_TOKEN_PATTERNS: list[str] = [
    r"g\.a000-[A-Za-z0-9_\-]+",
    r"sidts-[A-Za-z0-9_\-]{10,}",
    r"ya29\.[A-Za-z0-9_\-]{20,}",
]

# Google API-key shape (``AIza`` + 35 ``[A-Za-z0-9_-]`` chars), applied as a
# defense-in-depth catch-all alongside the name-anchored WIZ_global_data
# ``B8SWKb`` / ``VqImj`` / ``JrWMbf`` scrubbers below. The name-anchored
# patterns scrub whatever a *known* API-key field carries; this shape pattern
# scrubs the key wherever it appears regardless of the surrounding field name.
# The ``JrWMbf`` leak (the NotebookLM web API key round-tripping into the
# ``generate_mind_map_interactive`` / ``mind_maps_interactive`` cassettes) is
# the API-key analog of the ``LSID`` cookie leak: a credential rode in a field
# that was not on the allowlist, so a name-anchored guard alone missed it. The
# ``AIza`` prefix + a 35-char tail is the canonical Google API-key shape and
# never occurs as legitimate fixture content, so collapsing it to the
# ``SCRUBBED_API_KEY`` sentinel is safe. ``is_clean`` carries the matching
# detector (``_DETECT_GOOGLE_API_KEY``) so any surviving raw key is flagged.
#
# The tail uses ``{35,}`` (35-OR-MORE), not an exact ``{35}``: a greedy
# open-ended quantifier consumes the WHOLE contiguous key-char run. With an
# exact ``{35}`` a longer-than-canonical key (e.g. ``AIza`` + 36 chars) would be
# scrubbed only up to its first 39 chars, leaving a trailing fragment that the
# ``SCRUBBED_API_KEY`` replacement no longer re-matches — a silent partial leak
# in an unknown field with no name-anchored backstop (gemini review on #1266).
_GOOGLE_API_KEY_PATTERN = r"AIza[0-9A-Za-z_\-]{35,}"


def _cookie_header_replacer(name: str) -> tuple[str, str]:
    """Build (regex, replacement) for a Cookie / Set-Cookie header pattern.

    Uses a negative lookbehind anchor so a legitimate non-protected cookie
    whose name *ends* with a protected name (e.g. ``BSID=...``) is not
    accidentally scrubbed — see ``tests/unit/test_cookie_redaction.py``.
    """
    return (
        rf"(?<![A-Za-z0-9_-]){re.escape(name)}=[^;]+",
        f"{name}=SCRUBBED",
    )


# Single cookie-name alternation reused by EVERY JSON-shape cookie scrubber
# (storage_state name-first / value-first / bare-key forms). Derived from
# :data:`SESSION_COOKIES` plus the ``__Secure-*`` / ``__Host-*`` umbrellas so a
# name added to the registry (the ``LSID`` / ``LSOLH`` additions that motivated
# this hardening) is picked up by the header form AND the JSON forms in one
# place. Previously these alternations were hand-duplicated per pattern, so
# ``LSID`` would have been scrubbed in the header but only partially scrubbed in
# storage_state JSON — and ``is_clean`` (whose detector IS registry-derived via
# ``_COOKIE_NAMES_GROUP``) would then flag the residual value as a leak. Keeping
# scrubber and detector both registry-derived closes that drift.
_COOKIE_JSON_NAMES_GROUP = (
    "|".join(re.escape(name) for name in SESSION_COOKIES) + r'|__Secure-[^"]+|__Host-[^"]+'
)


# =============================================================================
# Sensitive patterns
# =============================================================================
# The list is order-sensitive: earlier patterns run first. Each entry is a
# ``(regex, replacement)`` pair consumed by :func:`re.sub` in :func:`scrub_string`
# below. Most replacements are static strings; display-name and Drive-file-ID
# scrubbers use context-aware (callable) replacements where exact-match
# allowlists or surrounding context need to be consulted.
SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    # -------------------------------------------------------------------------
    # 0. Catch-all Google auth-token shapes (defense in depth)
    # -------------------------------------------------------------------------
    # These run FIRST, before any cookie-name-anchored pattern, so a session
    # token leaks NOTHING even when it rides inside a cookie whose name is not
    # on the allowlist (the ``LSID`` leak class), inside a response/request
    # BODY, or inside any header value. Each shape is a distinctive,
    # high-entropy Google credential prefix with no legitimate non-secret
    # occurrence in a cassette:
    #
    #   * ``g.a000-...``  — the raw SID token embedded in SID/LSID cookie
    #                       values and OAuth flows.
    #   * ``sidts-...``   — the ``__Secure-*PSIDTS`` rotation-timestamp token.
    #   * ``ya29....``    — Google OAuth2 access tokens.
    #
    # See :data:`_AUTH_TOKEN_PATTERNS` for the per-shape anchoring strategy
    # (``g.a000-`` is distinctive enough to need no length floor; ``sidts-`` /
    # ``ya29.`` carry floors to avoid incidental short-literal matches).
    # Anything matched collapses to ``SCRUBBED`` so no fragment of the original
    # token survives, and the replacement does not itself re-match (idempotent).
    *((p, "SCRUBBED") for p in _AUTH_TOKEN_PATTERNS),
    # Google API-key shape catch-all (defense in depth). Runs before the
    # name-anchored WIZ_global_data API-key scrubbers in section 4 so a key
    # leaks NOTHING even when it rides in a WIZ field that is not on the
    # allowlist (the ``JrWMbf`` leak class). Collapses to ``SCRUBBED_API_KEY``,
    # which does not itself re-match (idempotent).
    (_GOOGLE_API_KEY_PATTERN, "SCRUBBED_API_KEY"),
    # -------------------------------------------------------------------------
    # 1. Cookie-header form: "Name=Value; ..."
    # -------------------------------------------------------------------------
    *(_cookie_header_replacer(name) for name in SESSION_COOKIES),
    # ``__Secure-*`` / ``__Host-*`` umbrellas — the prefix is distinctive
    # enough that no legitimate non-protected cookie shares it, so no
    # lookbehind anchor is needed.
    (r"(__Secure-[^=]+)=[^;]+", r"\1=SCRUBBED"),
    (r"(__Host-[^=]+)=[^;]+", r"\1=SCRUBBED"),
    # -------------------------------------------------------------------------
    # 2. CSRF and session tokens in WIZ_global_data (HTML / JSON responses)
    # -------------------------------------------------------------------------
    # The value match uses the escape-aware idiom ``(?:[^"\\]|\\.)*`` (matched
    # to the cookie-shape patterns below). A naive ``[^"]+`` would stop at the
    # first JSON-escaped quote (``\"``) and leave the tail of a secret in the
    # cassette while still producing a "SCRUBBED" prefix that ``is_clean``
    # accepts as a placeholder — silently leaking the suffix.
    (r'"SNlM0e"\s*:\s*"(?:[^"\\]|\\.)*"', '"SNlM0e":"SCRUBBED_CSRF"'),
    (r'"FdrFJe"\s*:\s*"(?:[^"\\]|\\.)*"', '"FdrFJe":"SCRUBBED_SESSION"'),
    # -------------------------------------------------------------------------
    # 3. URL / form-body parameters
    # -------------------------------------------------------------------------
    (r"f\.sid=[^&]+", "f.sid=SCRUBBED"),
    # Negative lookbehind anchors the param-name boundary so legitimate
    # parameters whose names *end* in ``at`` (``flat=...``, ``rate=...``,
    # ``format=...``) are not accidentally scrubbed.
    (r"(?<![A-Za-z0-9_-])at=[A-Za-z0-9_-]+", "at=SCRUBBED_CSRF"),
    (r'"at"\s*:\s*"(?:[^"\\]|\\.)*"', '"at":"SCRUBBED_CSRF"'),
    # -------------------------------------------------------------------------
    # 4. PII / IDs in WIZ_global_data
    # -------------------------------------------------------------------------
    (r'"oPEP7c"\s*:\s*"(?:[^"\\]|\\.)*"', '"oPEP7c":"SCRUBBED_EMAIL"'),
    (r'"S06Grb"\s*:\s*"(?:[^"\\]|\\.)*"', '"S06Grb":"SCRUBBED_USER_ID"'),
    (r'"W3Yyqf"\s*:\s*"(?:[^"\\]|\\.)*"', '"W3Yyqf":"SCRUBBED_USER_ID"'),
    (r'"qDCSke"\s*:\s*"(?:[^"\\]|\\.)*"', '"qDCSke":"SCRUBBED_USER_ID"'),
    (r'"B8SWKb"\s*:\s*"(?:[^"\\]|\\.)*"', '"B8SWKb":"SCRUBBED_API_KEY"'),
    (r'"VqImj"\s*:\s*"(?:[^"\\]|\\.)*"', '"VqImj":"SCRUBBED_API_KEY"'),
    # ``JrWMbf`` is the NotebookLM web API key embedded in WIZ_global_data. It
    # was missing from this list, so its value round-tripped unscrubbed into the
    # interactive mind-map cassettes (the leak this scrubber closes). Sibling of
    # the ``B8SWKb`` / ``VqImj`` API-key fields above.
    (r'"JrWMbf"\s*:\s*"(?:[^"\\]|\\.)*"', '"JrWMbf":"SCRUBBED_API_KEY"'),
    (r'"QGcrse"\s*:\s*"(?:[^"\\]|\\.)*"', '"QGcrse":"SCRUBBED_CLIENT_ID"'),
    (r'"iQJtYd"\s*:\s*"(?:[^"\\]|\\.)*"', '"iQJtYd":"SCRUBBED_PROJECT_ID"'),
    # -------------------------------------------------------------------------
    # 5. Email addresses
    # -------------------------------------------------------------------------
    # JSON-quoted form. The replacement embeds ``@example.com`` so a second
    # scrub pass on already-scrubbed content is a no-op (idempotent).
    (f'"{_EMAIL_PATTERN_BASE}"', '"SCRUBBED_EMAIL@example.com"'),
    # ``authuser=<email>`` query-param form. The client appends this to
    # every batchexecute URL whenever ``account_email`` is set, so request
    # URIs would otherwise leak the maintainer's email. Anchoring on
    # ``authuser=`` (not the email's domain) scrubs Workspace / corporate
    # addresses the provider-list pattern misses, with no false-positive
    # risk elsewhere. The replacement keeps the ``%40`` shape so VCR
    # matchers still see a well-formed value on replay.
    (_AUTHUSER_EMAIL_PATTERN, "authuser=SCRUBBED_EMAIL%40example.com"),
    # Double-encoded ``authuser%3D<email>`` form (issue #1368). When the
    # email-bearing inner URL is the *value* of a ``continue=`` redirect param
    # (Google account-menu ``SignOutOptions`` / ``brandaccounts`` links), its
    # ``?authuser=`` is percent-encoded one extra level, so the literal-``=``
    # pattern above never fires. Mirrors the single-encoded rule with the
    # canonical ``authuser%3DSCRUBBED_EMAIL%40example.com`` placeholder, which
    # does not itself re-match (idempotent).
    (
        AUTHUSER_EMAIL_DOUBLE_ENCODED_PATTERN,
        "authuser%3DSCRUBBED_EMAIL%40example.com",
    ),
    # Unquoted-context fallback (mailto: hrefs, raw HTML/JS chunks).
    (_EMAIL_PATTERN_BASE, "SCRUBBED_EMAIL@example.com"),
    # -------------------------------------------------------------------------
    # 6. Display names — JSON-key-anchored ONLY
    # -------------------------------------------------------------------------
    # We deliberately do NOT use a broad ``>[A-Z][a-z]+\s[A-Z][a-z]+<`` pattern
    # here: that would clobber legitimate two-Capitalized-word fixture content
    # such as ``>Source Title<`` in source-rename cassettes. Anchoring on the
    # JSON key keeps the scrubber surgical.
    (r"Google Account: [^\"<]+", "Google Account: SCRUBBED_NAME"),
    (r'"displayName"\s*:\s*"[^"]+"', '"displayName":"SCRUBBED_NAME"'),
    (r'"givenName"\s*:\s*"[^"]+"', '"givenName":"SCRUBBED_NAME"'),
    (r'"familyName"\s*:\s*"[^"]+"', '"familyName":"SCRUBBED_NAME"'),
    # Legacy hard-coded fixture name patterns kept for backward compatibility
    # with cassettes recorded before the structural patterns above existed.
    (r">People Conf<", ">SCRUBBED_NAME<"),
    (r'"People Conf"', '"SCRUBBED_NAME"'),
    # -------------------------------------------------------------------------
    # 7. Playwright ``storage_state.json`` cookie objects
    # -------------------------------------------------------------------------
    # The header-form patterns above never fire on a serialized storage_state
    # body, so we need explicit structural patterns for the JSON shape. The
    # cookie-value match uses the escape-aware idiom ``[^"\\]*(?:\\.[^"\\]*)*``
    # instead of the naive ``[^"]*``: the naive class terminates at the first
    # ``"`` even when JSON-escaped (``\"``), which would silently leak the
    # tail of a value containing a literal quote.
    (
        rf'("name":\s*"(?:{_COOKIE_JSON_NAMES_GROUP})"\s*,\s*"value":\s*")'
        r'[^"\\]*(?:\\.[^"\\]*)*(")',
        r"\1SCRUBBED\2",
    ),
    (
        r'("value":\s*")[^"\\]*(?:\\.[^"\\]*)*'
        rf'("\s*,\s*"name":\s*"(?:{_COOKIE_JSON_NAMES_GROUP})")',
        r"\1SCRUBBED\2",
    ),
    # -------------------------------------------------------------------------
    # 8. Direct JSON-dict-with-cookie-name-as-key shape: ``{"SID": "value"}``
    # -------------------------------------------------------------------------
    # ``is_clean`` detects this shape via ``_DETECT_COOKIE_JSON_KEY``; without a
    # corresponding scrubber, a leak in this form would be unfixable by
    # ``scrub_string`` (the validator would flag it but the sanitizer could
    # never clean it). The value match uses the escape-aware idiom to match
    # the other JSON-shape patterns above.
    (
        rf'("(?:{_COOKIE_JSON_NAMES_GROUP})"\s*:\s*")[^"\\]*(?:\\.[^"\\]*)*(")',
        r"\1SCRUBBED\2",
    ),
    # -------------------------------------------------------------------------
    # 9. Upload tokens
    # -------------------------------------------------------------------------
    # X-GUploader-UploadID response header line. The token is a long random
    # string that uniquely identifies a resumable-upload session.
    (
        r"X-GUploader-UploadID: [A-Za-z0-9_\-]+",
        "X-GUploader-UploadID: SCRUBBED_UPLOAD_ID",
    ),
    # Full upload URL that embeds the upload_id token in its query string.
    # Match the whole URL (up to the next quote or whitespace) and collapse
    # to a stable canonical form on NotebookLM's trusted upload endpoint so
    # cassette replay still passes runtime upload-URL validation.
    (
        r"https://notebooklm\.google\.com/upload/_/\?[^\"\s]*upload_id=[A-Za-z0-9_\-]+",
        "https://notebooklm.google.com/upload/_/?upload_id=SCRUBBED_UPLOAD_ID",
    ),
    # Standalone upload_id query parameter (anywhere it appears outside the
    # full upload URL above).
    (r"upload_id=[A-Za-z0-9_\-]+", "upload_id=SCRUBBED_UPLOAD_ID"),
    # -------------------------------------------------------------------------
    # 10. Drive AONS tokens
    # -------------------------------------------------------------------------
    # AONS-prefixed strings are Drive permission/ACL tokens. The 20-char tail
    # threshold avoids matching short literal "AONS" mentions in code or
    # documentation while catching real tokens (which are typically 50+ chars).
    (r"AONS[A-Za-z0-9_\-]{20,}", "SCRUBBED_AONS"),
    # -------------------------------------------------------------------------
    # 11. Drive file IDs — context-aware ONLY
    # -------------------------------------------------------------------------
    # Match ONLY inside Drive contexts: a ``"file_id": "..."`` JSON key or a
    # ``/drive/v3/files/<id>`` URL path. Bare 33-44 char strings elsewhere
    # are NOT scrubbed — that would false-positive on artifact IDs, source
    # IDs, conversation IDs, and other internal NotebookLM identifiers.
    (
        r'("file_id"\s*:\s*")[A-Za-z0-9_\-]{33,44}(")',
        r"\1SCRUBBED_DRIVE_FILE_ID\2",
    ),
    (
        r"(/drive/v3/files/)[A-Za-z0-9_\-]{33,44}",
        r"\1SCRUBBED_DRIVE_FILE_ID",
    ),
    # -------------------------------------------------------------------------
    # 12. Escaped JSON display-name literals
    # -------------------------------------------------------------------------
    # Owner display names surface inside Google's sharing RPCs as positional
    # list elements inside a stringified WRB payload, e.g.
    # ``[\"alice@gmail.com\",1,[],[\"First Last\",\"https://lh3...\"]]``.
    # The structured ``"displayName": "..."`` scrubbers in section 6 do not
    # fire on the double-encoded form, so we add an escape-anchored pattern
    # here. A negative lookahead carries the false-positive allowlist (font
    # families, UI titles, artifact / notebook names) so that legitimate
    # two-Capitalized-word fixture content is preserved — without that
    # allowlist a broad ``\\"[A-Z][a-z]+(?: [A-Z][a-z]+)+\\"`` regex would
    # clobber strings like ``\"Source Title\"`` during replay.
    #
    # The pattern requires the LITERAL escaped quotes (``\\"`` in regex,
    # which is ``\"`` in the cassette body — a backslash followed by a real
    # quote inside a JSON string) so it never fires on bare ``"Foo Bar"``
    # JSON values; those are handled by the existing displayName /
    # givenName / familyName JSON-key-anchored scrubbers in section 6.
    (
        rf'\\"(?!(?:{_DISPLAY_NAME_ALLOWLIST_ALT})\\")'
        r'[A-Z][a-z]+(?: [A-Z][a-z]+)+\\"',
        r'\\"SCRUBBED_NAME\\"',
    ),
    # -------------------------------------------------------------------------
    # 13. lh3.googleusercontent.com avatar URLs (both /a/ and /ogw/ paths)
    # -------------------------------------------------------------------------
    # Both the ``/a/`` and ``/ogw/`` path forms embed per-user avatar
    # tokens. The character class includes ``=`` and ``-`` because the URL
    # tail carries sizing modifiers (e.g. ``=s512``, ``=s32-c-mo``). The
    # whole URL (scheme through token suffix) collapses to a single
    # placeholder so that no fragment of the original token survives.
    (
        r"https?://lh3\.googleusercontent\.com/(?:a|ogw)/[A-Za-z0-9_=\-]+",
        "SCRUBBED_AVATAR_URL",
    ),
]


# =============================================================================
# Public entry points
# =============================================================================


def scrub_string(text: str) -> str:
    """Apply every sensitive-pattern replacement to ``text``.

    This is the single sanitization entry point consumed by
    :mod:`tests.vcr_config` (and by future cassette tooling). The function is
    idempotent on already-scrubbed content: each replacement embeds a sentinel
    that does not itself match any pattern in :data:`SENSITIVE_PATTERNS`.
    """
    for pattern, replacement in SENSITIVE_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text


# Pre-compiled detection-only patterns for :func:`is_clean`.
#
# ``is_clean`` is a *validator* — it must NOT modify text. It pulls cookie
# values out of every shape we know about and asks: "is this value one of the
# expected SCRUB_PLACEHOLDERS?" If not, it's a leak. The detection regexes
# differ from the scrub regexes in that they only need to extract the value;
# we lean on the placeholder allowlist to decide leak-or-not.

_COOKIE_NAMES_GROUP = (
    "|".join(re.escape(name) for name in SESSION_COOKIES) + r"|__Secure-[^=\"]+|__Host-[^=\"]+"
)

_DETECT_COOKIE_HEADER = re.compile(
    rf"(?<![A-Za-z0-9_-])(?P<name>{_COOKIE_NAMES_GROUP})=(?P<value>[^;\s]+)"
)
_DETECT_COOKIE_JSON_NAME_FIRST = re.compile(
    rf'"name"\s*:\s*"(?P<name>{_COOKIE_NAMES_GROUP})"\s*,\s*"value"\s*:\s*"'
    r'(?P<value>(?:[^"\\]|\\.)*)"'
)
_DETECT_COOKIE_JSON_VALUE_FIRST = re.compile(
    r'"value"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"\s*,\s*"name"\s*:\s*"'
    rf'(?P<name>{_COOKIE_NAMES_GROUP})"'
)
_DETECT_COOKIE_JSON_KEY = re.compile(
    rf'"(?P<name>{_COOKIE_NAMES_GROUP})"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"'
)

# WIZ_global_data and form-body token fields, in the same order as the
# corresponding scrubbers in ``SENSITIVE_PATTERNS``. Compiled at import time so
# repeated ``is_clean`` calls (one per cassette under CI) don't pay the cost.
# The value capture uses the same escape-aware idiom as the cookie-shape
# detectors above so a token containing a JSON-escaped quote (``\"``) is
# captured in full instead of truncated at the first literal quote.
_DETECT_TOKEN_FIELDS: list[tuple[str, re.Pattern[str]]] = [
    ("SNlM0e (CSRF)", re.compile(r'"SNlM0e"\s*:\s*"((?:[^"\\]|\\.)*)"')),
    ("FdrFJe (session)", re.compile(r'"FdrFJe"\s*:\s*"((?:[^"\\]|\\.)*)"')),
    ("oPEP7c (email)", re.compile(r'"oPEP7c"\s*:\s*"((?:[^"\\]|\\.)*)"')),
    ("S06Grb (user_id)", re.compile(r'"S06Grb"\s*:\s*"((?:[^"\\]|\\.)*)"')),
    ("W3Yyqf (user_id)", re.compile(r'"W3Yyqf"\s*:\s*"((?:[^"\\]|\\.)*)"')),
    ("qDCSke (user_id)", re.compile(r'"qDCSke"\s*:\s*"((?:[^"\\]|\\.)*)"')),
    ("B8SWKb (api_key)", re.compile(r'"B8SWKb"\s*:\s*"((?:[^"\\]|\\.)*)"')),
    ("VqImj (api_key)", re.compile(r'"VqImj"\s*:\s*"((?:[^"\\]|\\.)*)"')),
    ("JrWMbf (api_key)", re.compile(r'"JrWMbf"\s*:\s*"((?:[^"\\]|\\.)*)"')),
    ("QGcrse (client_id)", re.compile(r'"QGcrse"\s*:\s*"((?:[^"\\]|\\.)*)"')),
    ("iQJtYd (project_id)", re.compile(r'"iQJtYd"\s*:\s*"((?:[^"\\]|\\.)*)"')),
]

# Compiled detection-only pattern for emails (no replacement string baked in).
# Three-shape detector — mirrors the scrubber patterns in section 5 above:
#   1. Literal ``@`` form on the provider allowlist (JSON, mailto: hrefs).
#   2. URL-encoded ``authuser=<email>`` query-param form for *any* domain.
#   3. Double-encoded ``authuser%3D<email>`` redirect-param form (issue #1368).
_DETECT_EMAIL = re.compile(
    r"[A-Za-z0-9._%+\-]+@(?:"
    + "|".join(EMAIL_PROVIDERS)
    + r")\.com"
    + r"|"
    + _AUTHUSER_EMAIL_PATTERN
    + r"|"
    + AUTHUSER_EMAIL_DOUBLE_ENCODED_PATTERN
)

# upload + Drive token detectors.
#
# Each entry is (label, regex) where the regex's group(1) captures the value
# that must match a known scrub placeholder. The regexes deliberately accept
# both raw tokens AND their canonical placeholder form so that an already-
# scrubbed cassette passes ``is_clean`` cleanly (idempotent validation).
_DETECT_UPLOAD_DRIVE_FIELDS: list[tuple[str, re.Pattern[str]]] = [
    # X-GUploader-UploadID response header: value must be SCRUBBED_UPLOAD_ID.
    ("upload header", re.compile(r"X-GUploader-UploadID:\s*([A-Za-z0-9_\-]+)")),
    # Standalone upload_id query parameter: value must be SCRUBBED_UPLOAD_ID.
    # This intentionally matches BOTH dirty tokens and the canonical
    # placeholder; the placeholder check below decides leak-or-clean.
    ("upload_id param", re.compile(r"upload_id=([A-Za-z0-9_\-]+)")),
    # Drive AONS tokens — 20-char tail threshold avoids matching short
    # literal ``AONS`` mentions in code or docs. The captured group is the
    # FULL token (including the ``AONS`` prefix) so the placeholder
    # ``SCRUBBED_AONS`` is matched directly.
    ("Drive AONS token", re.compile(r"(AONS[A-Za-z0-9_\-]{20,}|SCRUBBED_AONS)")),
    # Drive file ID in JSON key: ``"file_id": "<id>"``.
    ("Drive file_id (JSON)", re.compile(r'"file_id"\s*:\s*"([^"]+)"')),
    # Drive file ID in URL: ``/drive/v3/files/<id>``.
    ("Drive file_id (URL)", re.compile(r"/drive/v3/files/([A-Za-z0-9_\-]+)")),
]

# Full upload URL is rewritten to a trusted-host placeholder with a scrubbed
# upload_id. Any NotebookLM upload URL carrying a non-placeholder upload_id is
# a leak.
_DETECT_UPLOAD_URL = re.compile(
    r"https://notebooklm\.google\.com/upload/_/\?[^\"\s]*upload_id=(?!SCRUBBED_UPLOAD_ID\b)"
)

# escaped JSON display-name literal detector.
#
# Matches ``\"First Last\"`` inside a double-encoded JSON string. The
# false-positive allowlist is consulted at the call site in ``is_clean``
# rather than baked into the regex so the detector stays simple and the
# allowlist remains observable. The captured group is the inner name (no
# escape quotes) so we can compare it to DISPLAY_NAME_FALSE_POSITIVES
# directly.
_DETECT_DISPLAY_NAME_ESCAPED = re.compile(r'\\"([A-Z][a-z]+(?: [A-Z][a-z]+)+)\\"')

# avatar URL detector (/ogw/ group). The pattern matches
# both ``/a/`` and ``/ogw/`` path forms. The scrubber collapses the entire
# URL to ``SCRUBBED_AVATAR_URL``, so any match here is by definition a
# leak (the placeholder string doesn't itself contain ``lh3.``).
_DETECT_AVATAR_URL = re.compile(r"https?://lh3\.googleusercontent\.com/(?:a|ogw)/[A-Za-z0-9_=\-]+")

# Catch-all auth-token detector — the validator twin of
# :data:`_AUTH_TOKEN_PATTERNS`. The scrubber collapses every ``g.a000-...`` /
# ``sidts-...`` / ``ya29....`` token to ``SCRUBBED`` (which contains none of
# these prefixes), so ANY match here is by definition an unredacted leak —
# regardless of which cookie name or body field carried it. This is the guard
# rail that would have caught the ``LSID`` leak: it never depended on ``LSID``
# being on the cookie allowlist.
_DETECT_AUTH_TOKEN = re.compile("|".join(_AUTH_TOKEN_PATTERNS))

# Google API-key shape detector — the validator twin of
# :data:`_GOOGLE_API_KEY_PATTERN`. The scrubber collapses every ``AIza...`` key
# to the ``SCRUBBED_API_KEY`` sentinel (which does not contain the ``AIza``
# prefix), so ANY match here is by definition an unredacted leak regardless of
# which WIZ field carried it. This is the field-name-agnostic backstop that
# closes the ``JrWMbf`` gap.
_DETECT_GOOGLE_API_KEY = re.compile(_GOOGLE_API_KEY_PATTERN)

# Double-encoded ``authuser%3D<email>`` shape detector (issue #1368). Unlike the
# single-encoded ``authuser=<email>`` form — which only ``is_clean`` runs,
# because the literal-``@`` provider branch of ``_DETECT_EMAIL`` also fires on
# legitimate placeholder content like ``alice@gmail.com`` — the double-encoded
# ``authuser%3D…%40…`` shape has NO legitimate occurrence anywhere in the repo,
# so it is safe to run over arbitrary fixture directories via
# :data:`_CREDENTIAL_DETECTORS`. The negative lookahead excludes the canonical
# ``authuser%3DSCRUBBED_EMAIL%40example.com`` placeholder so an already-scrubbed
# cassette validates cleanly (idempotent).
_DETECT_AUTHUSER_EMAIL_DOUBLE_ENCODED = re.compile(
    r"authuser%3D(?!SCRUBBED_EMAIL%40example\.com)" + _AUTHUSER_EMAIL_TAIL
)


# Detectors with ZERO legitimate-occurrence risk anywhere in the repository:
# raw Google auth-token shapes (``g.a000-`` / ``sidts-`` / ``ya29.``), the
# canonical Google API-key shape (``AIza`` + 35 chars), and the double-encoded
# ``authuser%3D<email>`` redirect-param shape (issue #1368). Unlike the
# cookie-value / display-name / email heuristics that :func:`is_clean` also runs
# — which intentionally fire on placeholder fixture content such as ``"Scrubbed
# Note Title"`` or ``alice@gmail.com`` — these shapes never match legitimate
# test data, so they are safe to run over arbitrary files (golden JSON/HTML
# fixtures, docs, source) with no per-file allowlist. This is what the
# ``--secrets-only`` mode of ``check_cassettes_clean.py`` uses to extend leak
# detection beyond ``tests/cassettes/`` without drowning in false positives.
_CREDENTIAL_DETECTORS: list[tuple[str, re.Pattern[str]]] = [
    ("auth token", _DETECT_AUTH_TOKEN),
    ("Google API key", _DETECT_GOOGLE_API_KEY),
    ("double-encoded authuser email", _DETECT_AUTHUSER_EMAIL_DOUBLE_ENCODED),
]


# =============================================================================
# Generic high-entropy / unknown-field credential scan
# =============================================================================
#
# THE KNOWN-SHAPE BOUNDARY (residual-risk decision; ADR-006, issue #1382).
# ---------------------------------------------------------------------------
# Everything ABOVE this point is *name-anchored* (cookie names, WIZ field IDs)
# or *known-shape* (``g.a000-`` / ``sidts-`` / ``ya29.`` / ``AIza`` prefixes).
# That makes the guard NECESSARY-but-not-SUFFICIENT: a credential family the
# registry does not yet know about — a NOVEL token prefix, or a known secret
# riding in an un-targeted JSON field — passes the targeted detectors silently.
# This is not hypothetical: the guard has missed two such shapes historically
# (the ``LSID`` cookie whose name was off the allowlist, and the ``JrWMbf`` WIZ
# field whose API key the name-anchored scrubbers skipped), and #1372 fixed a
# double-encoded-email shape that leaked the maintainer's address. Each fix
# extended the known-shape set by ONE entry — reactive, after-the-leak.
#
# This scanner is the deliberate, field-agnostic complement: it flags a quoted
# JSON string scalar whose ENTIRE value is one long, high-entropy
# base64url/hex run, regardless of the field name carrying it. It is the
# "would have caught it generically" backstop the targeted detectors lack.
#
# Why it is SCOPED to a *quoted scalar value* rather than scanning every token:
# committed cassettes are dense with legitimate high-entropy text — minified
# CSS class names, ``fonts.gstatic.com`` / ``googleusercontent.com`` URLs,
# 1.5 KB hex mind-map blobs, and 600-char ``context=eJw…`` zlib-base64 query
# params. A raw per-token entropy scan would drown in thousands of those false
# positives. But NONE of that legitimate content appears as a *clean quoted
# JSON scalar* (``"field":"<one-pure-token>"``) — which is exactly the shape a
# leaked credential takes when it rides in an unknown field. Anchoring on that
# shape is what makes ZERO false positives across every committed cassette
# achievable while still catching a planted novel token. This residual-risk
# property is therefore a DECISION, not an accident: anything that is NOT a
# quoted high-entropy scalar (a credential split across structural punctuation,
# a token in a non-JSON encoding) remains the backstop's blind spot, covered by
# GitHub secret-scanning and the name-anchored detectors above.
#
# Thresholds (calibrated against every committed cassette — see the unit tests
# ``test_entropy_scan_*`` and ``test_all_committed_cassettes_pass_entropy_scan``):
#
#   * base64url / mixed-alphabet tokens: length >= 40 AND Shannon entropy
#     >= 4.0 bits/char. A 40+ char run drawn from a ~64-symbol alphabet with
#     >=4.0 entropy is a random secret by construction; legitimate quoted
#     scalars in cassettes (UUIDs, opaque IDs) never reach both bars at once.
#   * pure-hex tokens (``[0-9a-fA-F]`` only): a separate length floor of 64,
#     because a 16-symbol alphabet CAPS Shannon entropy at log2(16) = 4.0
#     bits/char — a hex secret can never reliably clear the 4.0 bar, so length
#     alone gates it. 64 hex chars = 256 bits, comfortably above any incidental
#     hex literal (a 32-hex MD5 or a 36-char UUID stays well under the floor).
#
# Allowlist (known-benign long tokens that must NOT be flagged):
#
#   * the canonical ``SCRUB_PLACEHOLDERS`` sentinels (idempotent validation);
#   * the canonical UUID shape (``8-4-4-4-12`` hex-with-dashes) — these are
#     NotebookLM-internal artifact / source / conversation IDs that the
#     surrounding scrubbers deliberately preserve. The base64 length+entropy
#     gate already excludes them (36 chars < 40, dashes depress entropy), but
#     the explicit shape skip documents the intent and guards against a future
#     threshold change re-flagging them.
_ENTROPY_MIN_LEN_BASE64 = 40
_ENTROPY_MIN_BITS = 4.0
_ENTROPY_MIN_LEN_HEX = 64

# Cap the substring fed to the entropy computation. A cassette can carry a
# multi-megabyte base64-encoded asset as one quoted scalar (an inline image /
# PDF / audio blob), and computing Shannon entropy over the whole run would be a
# needless O(n) hot loop per scanned line. A random credential is high-entropy
# throughout, so a 512-char prefix is a faithful representative — the entropy of
# the prefix and the whole token converge well before this bound (gemini review
# on #1387). Length gating still uses the FULL token length, not the cap.
_ENTROPY_SAMPLE_CHARS = 512

# A quoted JSON string scalar whose entire content is a single base64url / hex
# token. ``\A``/``\Z`` inside the captured group are unnecessary because the
# surrounding quotes already anchor the run to the FULL value — a token split
# by any non-``[A-Za-z0-9_\-+/=]`` byte simply won't match.
_DETECT_QUOTED_TOKEN = re.compile(r'"([A-Za-z0-9_\-+/=]+)"')

# Canonical UUID shape (``8-4-4-4-12``). Internal NotebookLM IDs, never secrets.
_UUID_SHAPE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# Pure-hex run (no base64-only symbols). Used to route a token onto the hex
# length floor instead of the entropy bar (hex entropy caps at 4.0).
_HEX_ONLY = re.compile(r"\A[0-9a-fA-F]+\Z")


def _shannon_entropy(token: str) -> float:
    """Shannon entropy of ``token`` in bits per character.

    A measure of per-character randomness: a token drawn uniformly from an
    ``N``-symbol alphabet approaches ``log2(N)`` bits/char, while repetitive or
    small-alphabet content scores lower. Returns ``0.0`` for the empty string.

    Only the first :data:`_ENTROPY_SAMPLE_CHARS` characters are analyzed so a
    multi-megabyte base64 asset scalar does not turn this into a per-line hot
    loop; a random credential is high-entropy throughout, so the prefix is
    representative.
    """
    if not token:
        return 0.0
    sample = token[:_ENTROPY_SAMPLE_CHARS]
    length = len(sample)
    return -sum((count / length) * math.log2(count / length) for count in Counter(sample).values())


def _is_high_entropy_token(token: str) -> bool:
    """Return ``True`` if ``token`` looks like a novel high-entropy credential.

    See the KNOWN-SHAPE BOUNDARY block above for the full rationale and the
    threshold calibration. In brief: a pure base64url/hex run that is long
    AND random enough that no legitimate quoted-scalar cassette value reaches
    both bars, with the placeholder + UUID allowlist applied first.
    """
    if token in SCRUB_PLACEHOLDERS:
        return False
    if _UUID_SHAPE.match(token):
        return False
    if _HEX_ONLY.match(token):
        # Hex entropy caps at log2(16) == 4.0 bits/char, so length alone gates
        # a hex secret; the entropy bar below would never reliably fire on it.
        return len(token) >= _ENTROPY_MIN_LEN_HEX
    if len(token) < _ENTROPY_MIN_LEN_BASE64:
        return False
    return _shannon_entropy(token) >= _ENTROPY_MIN_BITS


def find_high_entropy_leaks(text: str) -> list[str]:
    """Flag quoted high-entropy base64/hex scalars in ANY field (issue #1382).

    The field-agnostic complement to the name-anchored / known-shape detectors:
    catches a novel credential shape — or a known secret riding in an
    un-targeted JSON field — that the targeted scrubbers miss. Scoped to a
    *quoted JSON string scalar* so it never fires on the legitimate high-entropy
    text (CSS, URLs, compressed-query blobs) that pervades real cassettes. See
    the KNOWN-SHAPE BOUNDARY block above for the boundary this deliberately
    draws. Each returned string is a human-readable ``"Leak (...): '...'"``.
    """
    leaks: list[str] = []
    for match in _DETECT_QUOTED_TOKEN.finditer(text):
        token = match.group(1)
        if _is_high_entropy_token(token):
            leaks.append(f"Leak (high-entropy token): {token!r}")
    return leaks


def find_credential_leaks(text: str) -> list[str]:
    """Return high-severity credential leaks (auth tokens + Google API keys).

    A focused subset of :func:`is_clean` that runs ONLY the credential-shape
    detectors in :data:`_CREDENTIAL_DETECTORS` PLUS the field-agnostic
    high-entropy scan (:func:`find_high_entropy_leaks`). These shapes have no
    legitimate occurrence anywhere in the repo, so unlike :func:`is_clean` this
    is safe to point at fixture directories full of intentional placeholder
    content. Each returned string is a human-readable ``"Leak (...): '...'"``
    description.
    """
    leaks: list[str] = []
    for label, regex in _CREDENTIAL_DETECTORS:
        for match in regex.finditer(text):
            leaks.append(f"Leak ({label}): {match.group(0)!r}")
    leaks.extend(find_high_entropy_leaks(text))
    return leaks


def is_clean(text: str) -> tuple[bool, list[str]]:
    """Validate that ``text`` contains no unredacted sensitive data.

    Closes the cookie-value leak heuristic: cleanliness is judged by exact
    membership in :data:`SCRUB_PLACEHOLDERS`, NOT by the legacy "starts with
    S" character-class heuristic that allowed any real secret beginning with
    ``S`` (and there are plenty — SID values, SAPISID values, OAuth ``state``
    tokens) to slip past the guard.

    Parameters
    ----------
    text:
        The full text of a cassette (or any string) to inspect.

    Returns
    -------
    ``(ok, leaks)`` where ``ok`` is ``True`` iff ``leaks`` is empty. Each leak
    string is a human-readable description suitable for printing in CI output.

    Display-name + avatar coverage
    ---------------------------------------
    Escaped display-name literals (``\\"First Last\\"`` inside double-
    encoded WRB payloads) and ``lh3.googleusercontent.com/(a|ogw)/`` avatar
    URLs are BOTH scrubbed and detected. The display-name detector consults
    :data:`DISPLAY_NAME_FALSE_POSITIVES` so legitimate two-Capitalized-word
    fixture content (font families, UI titles, artifact / notebook names)
    is not flagged. The structured ``"displayName": "..."``-style fields
    from section 6 remain scrub-only (no detector) — that gap is harmless
    because A6a's escape-anchored detector also catches the equivalent
    leak shape inside any stringified WRB payload, which is where these
    fields actually surface.
    """
    leaks: list[str] = []

    # --- 1. Cookie shapes ---------------------------------------------------
    seen: set[tuple[str, str]] = set()
    for regex, shape in (
        (_DETECT_COOKIE_HEADER, "cookie header"),
        (_DETECT_COOKIE_JSON_NAME_FIRST, "storage_state (name-first)"),
        (_DETECT_COOKIE_JSON_VALUE_FIRST, "storage_state (value-first)"),
        (_DETECT_COOKIE_JSON_KEY, "JSON key"),
    ):
        for match in regex.finditer(text):
            name = match.group("name")
            value = match.group("value")
            key = (name, value)
            if key in seen:
                continue
            seen.add(key)
            if value not in SCRUB_PLACEHOLDERS:
                leaks.append(
                    f"Leak ({shape}): cookie {name!r} value {value!r} is not"
                    f" a known scrub placeholder"
                )

    # --- 2. Real email addresses (any provider we redact) -------------------
    # Skip canonical placeholders so the provider-agnostic ``authuser=``
    # branch of ``_DETECT_EMAIL`` (which matches any TLD) doesn't re-flag
    # the scrubbed replacement on a second pass.
    for match in _DETECT_EMAIL.finditer(text):
        matched = match.group(0)
        if matched in SCRUB_PLACEHOLDERS:
            continue
        leaks.append(f"Leak (email): {matched!r}")

    # --- 3. Token / ID fields that should be redacted ----------------------
    for label, regex in _DETECT_TOKEN_FIELDS:
        for match in regex.finditer(text):
            value = match.group(1)
            if value not in SCRUB_PLACEHOLDERS:
                leaks.append(f"Leak ({label}): {value!r}")

    # --- 4. Upload + Drive token fields ------------------------------------
    for label, regex in _DETECT_UPLOAD_DRIVE_FIELDS:
        for match in regex.finditer(text):
            value = match.group(1)
            if value not in SCRUB_PLACEHOLDERS:
                leaks.append(f"Leak ({label}): {value!r}")

    # --- 5. Full upload URL -----------------------------------------------
    # The scrubber preserves the trusted host/path but rewrites upload_id to
    # SCRUBBED_UPLOAD_ID, so any match here is by definition a leak.
    for match in _DETECT_UPLOAD_URL.finditer(text):
        leaks.append(f"Leak (upload URL): {match.group(0)!r}")

    # --- 6. Escaped display-name literals ----------------------------------
    # The false-positive allowlist (font families, UI titles, artifact /
    # notebook names) is consulted here rather than baked into the regex so
    # the detector stays simple and the allowlist remains observable.
    for match in _DETECT_DISPLAY_NAME_ESCAPED.finditer(text):
        inner = match.group(1)
        if inner in DISPLAY_NAME_FALSE_POSITIVES:
            continue
        leaks.append(f"Leak (escaped display name): {match.group(0)!r}")

    # --- 7. Avatar URLs ---------------------------------------------------
    # The scrubber collapses the whole URL to ``SCRUBBED_AVATAR_URL``, so any
    # match of the raw URL form here is by definition a leak.
    for match in _DETECT_AVATAR_URL.finditer(text):
        leaks.append(f"Leak (avatar URL): {match.group(0)!r}")

    # --- 8. Catch-all Google auth-token shapes -----------------------------
    # ``g.a000-...`` / ``sidts-...`` / ``ya29....`` tokens are scrubbed to
    # ``SCRUBBED`` wherever they appear (cookie values on or off the allowlist,
    # response bodies, headers). Any surviving raw token is a leak by
    # definition — this is the cookie-name-agnostic backstop that closes the
    # ``LSID`` gap.
    for match in _DETECT_AUTH_TOKEN.finditer(text):
        leaks.append(f"Leak (auth token): {match.group(0)!r}")

    # --- 9. Google API-key shape (field-name-agnostic) ---------------------
    # ``AIza...`` keys are scrubbed to ``SCRUBBED_API_KEY`` wherever they
    # appear, regardless of the WIZ field name carrying them. Any surviving raw
    # key is a leak by definition — this is the backstop that closes the
    # ``JrWMbf`` gap the name-anchored field detectors missed.
    for match in _DETECT_GOOGLE_API_KEY.finditer(text):
        leaks.append(f"Leak (Google API key): {match.group(0)!r}")

    # --- 10. Generic high-entropy / unknown-field credential scan ----------
    # Field-agnostic backstop for a NOVEL credential shape — or a known secret
    # in an un-targeted JSON field — that the name-anchored / known-shape
    # detectors above miss (issue #1382). Scoped to a quoted high-entropy
    # base64/hex scalar so it never fires on the legitimate high-entropy text
    # (CSS, URLs, compressed-query blobs) that pervades real cassettes. See the
    # KNOWN-SHAPE BOUNDARY block near :func:`find_high_entropy_leaks`.
    leaks.extend(find_high_entropy_leaks(text))

    return (not leaks, leaks)


# =============================================================================
# Synthetic error-response builders for VCR recording
# =============================================================================
#
# These helpers exist so error-shape cassettes can be generated whose
# responses match the shapes our client's exception mapping (see
# :mod:`notebooklm._runtime.helpers` for ``is_auth_error`` and the retry
# middleware for 429/5xx) keys on:
#
#   - HTTP 429  -> ``TransportRateLimited`` -> ``RateLimitError``
#   - HTTP 5xx  -> ``TransportServerError`` -> ``ServerError``
#   - HTTP 400  -> ``is_auth_error()``       -> refresh path + ``AuthError`` on
#                                               second failure
#
# The synthetic bodies are **not** captured from Google. They are deliberately
# minimal and exist purely to validate client-side exception mapping. Documented
# warning lives in ``docs/development.md`` under "Synthetic error cassettes".

ERROR_MODE_RATE_LIMIT = "429"
ERROR_MODE_SERVER = "5xx"
ERROR_MODE_EXPIRED_CSRF = "expired_csrf"

VALID_ERROR_MODES: frozenset[str] = frozenset(
    {ERROR_MODE_RATE_LIMIT, ERROR_MODE_SERVER, ERROR_MODE_EXPIRED_CSRF}
)

# Filename prefix that error-cassette generators MUST apply to cassettes
# produced through this plumbing. The prefix is mechanical: it lets a
# reader of ``tests/cassettes/`` distinguish synthetic error shapes from real
# recordings at a glance, without having to open the YAML.
SYNTHETIC_ERROR_CASSETTE_PREFIX = "error_synthetic_"


def synthetic_error_cassette_name(mode: str, slug: str) -> str:
    """Build the canonical ``error_synthetic_<mode>_<slug>.yaml`` filename.

    Args:
        mode: One of ``VALID_ERROR_MODES``.
        slug: A short identifier for the RPC being recorded (e.g. ``"list_notebooks"``).

    Raises:
        ValueError: If ``mode`` is not a recognized synthetic-error mode.
    """
    if mode not in VALID_ERROR_MODES:
        raise ValueError(
            f"Unknown synthetic error mode {mode!r}. Valid modes: {sorted(VALID_ERROR_MODES)}"
        )
    return f"{SYNTHETIC_ERROR_CASSETTE_PREFIX}{mode}_{slug}.yaml"


def build_synthetic_error_response(
    mode: str,
) -> tuple[int, bytes, dict[str, str]]:
    """Return a ``(status_code, body, headers)`` triple for a synthetic error.

    The shape is intentionally minimal; the client's exception mapping keys on
    the HTTP status code (see :func:`notebooklm._runtime.helpers.is_auth_error`
    and the 429 / 5xx branches in the retry middleware), so a
    syntactically-valid Google error-shaped body is sufficient.

    For the ``expired_csrf`` mode we return HTTP 400 — not 401 — because that
    matches the documented Google contract: NotebookLM returns 400 (not 401/403)
    when the embedded CSRF token has expired, which is why ``is_auth_error``
    treats 400 as an auth-refresh trigger. See
    :func:`notebooklm._runtime.helpers.is_auth_error`.

    Args:
        mode: One of ``VALID_ERROR_MODES``.

    Returns:
        A tuple of ``(status_code, body_bytes, headers_dict)`` suitable for
        constructing an ``httpx.Response``.

    Raises:
        ValueError: If ``mode`` is not a recognized synthetic-error mode.
    """
    if mode == ERROR_MODE_RATE_LIMIT:
        body = (
            b'{"error": {"code": 429, "message": "Rate limited", "status": "RESOURCE_EXHAUSTED"}}'
        )
        # Retry-After is honored by the 429 retry loop in ``_perform_authed_post``.
        # Setting a small value keeps the recording-time loop short.
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Retry-After": "1",
        }
        return (429, body, headers)
    if mode == ERROR_MODE_SERVER:
        body = b'{"error": {"code": 500, "message": "Internal error"}}'
        headers = {"Content-Type": "application/json; charset=UTF-8"}
        return (500, body, headers)
    if mode == ERROR_MODE_EXPIRED_CSRF:
        # NotebookLM returns 400 (not 401/403) for expired CSRF — this matches
        # the ``is_auth_error`` branch that treats 400/401/403 as auth-refresh
        # triggers. The body shape echoes Google's typical "invalid request"
        # response; the client keys on status code, not body, for this path.
        body = (
            b'{"error": {"code": 400, "message": "Invalid request token", '
            b'"status": "INVALID_ARGUMENT"}}'
        )
        headers = {"Content-Type": "application/json; charset=UTF-8"}
        return (400, body, headers)
    raise ValueError(
        f"Unknown synthetic error mode {mode!r}. Valid modes: {sorted(VALID_ERROR_MODES)}"
    )
