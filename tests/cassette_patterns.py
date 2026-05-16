"""Canonical registry of cassette-mutating utilities.

This module is the single source of truth for what counts as sensitive in a
recorded HTTP cassette and for cassette-byte-count surgery. It exports two
complementary halves:

1. **Sanitization registry (T8.A4 — audit findings I6, I7).** A canonical
   list of regex (pattern, replacement) pairs covering Google session
   cookies, ``__Secure-*`` / ``__Host-*`` cookies, WIZ_global_data token
   fields, email addresses, and Playwright ``storage_state`` cookie objects;
   a single ``scrub_string`` entry point that applies them; and an
   ``is_clean`` validator that judges cookie-value cleanliness via exact-
   match membership in ``SCRUB_PLACEHOLDERS`` (closing I7's "starts with S"
   character-class hole). Before this consolidation the same patterns lived
   as an inline ``SENSITIVE_PATTERNS`` list in :mod:`tests.vcr_config` and
   were duplicated piecemeal in ``tests/check_cassettes_clean.sh`` — that
   drift risk is what audit finding I6 named.

2. **Chunked-response byte-count re-derivation (T8.D7).** The
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
  on its own (the T8.B6 bulk re-scrub script in ``scripts/`` imports both
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
- :func:`recompute_chunk_prefix`  XSSI byte-count re-derivation (T8.D7)

Upload + Drive token coverage (T8.A6b — audit finding I17)
----------------------------------------------------------
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

Display-name + avatar coverage (T8.A6a — audit finding C4 + /ogw/ group)
------------------------------------------------------------------------
The registry below also covers two display/identity leak shapes that the
A4 structured scrubbers miss because the data is double-encoded inside a
WRB-payload JSON string:

* **Escaped JSON display-name literals.** Google's sharing RPCs emit owner
  metadata as positional list elements inside a stringified WRB payload —
  the display name surfaces as ``\\"First Last\\"`` rather than a
  structured ``"displayName": "..."`` key. The A4 patterns key-anchor on
  the outer JSON key, so they never fire on the inner double-encoded form.
  The A6a pattern anchors on the escape-quote shape ``\\"...\\"`` and
  carries an explicit false-positive allowlist (font families, UI titles,
  artifact/notebook names produced by the test corpus) so that legitimate
  two-Capitalized-word fixture content is preserved. This false-positive
  list is the load-bearing safety net — a broad
  ``>[A-Z][a-z]+\\s[A-Z][a-z]+<`` regex without it would corrupt source-
  rename and artifact-list cassettes during replay.
* **lh3.googleusercontent.com avatar URLs.** Both the ``/a/`` and ``/ogw/``
  path forms carry per-user avatar tokens. The pattern collapses the whole
  URL (host + path + token, including any trailing ``=s512``-style sizing
  suffix) to ``SCRUBBED_AVATAR_URL``.
"""

from __future__ import annotations

import re

# =============================================================================
# Chunked-response byte-count re-derivation (T8.D7)
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
# secret starting with the letter ``S`` (audit finding I7).
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
        # T8.A6b — upload + Drive token placeholders (audit I17).
        "SCRUBBED_UPLOAD_ID",
        "SCRUBBED_UPLOAD_URL",
        "SCRUBBED_AONS",
        "SCRUBBED_DRIVE_FILE_ID",
        # T8.A6a — avatar URL placeholder (audit C4 + /ogw/ group). The
        # display-name escaped-literal scrubber reuses the existing
        # ``SCRUBBED_NAME`` sentinel from A4 (section 6) so a cassette can
        # carry just one canonical replacement string for human names.
        "SCRUBBED_AVATAR_URL",
    }
)


# =============================================================================
# Display-name false-positive allowlist (T8.A6a)
# =============================================================================
# Two-Capitalized-word strings that LOOK like human display names but are
# legitimate UI / font-family / artifact / notebook titles produced during
# E2E test runs. The escaped-display-name scrubber (section 12 below) carries
# this as a negative lookahead so these strings are NEVER replaced — that
# protection is what keeps source-rename and artifact-list cassettes from
# being corrupted during replay.
#
# This list intentionally mirrors ``DISPLAY_NAME_FALSE_POSITIVES`` in
# ``tests/unit/test_cassette_shapes.py`` (T8.A3). The two lists are NOT
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


# =============================================================================
# Sensitive patterns
# =============================================================================
# The list is order-sensitive: earlier patterns run first. Each entry is a
# ``(regex, replacement)`` pair consumed by :func:`re.sub` in :func:`scrub_string`
# below. All replacements are static strings today; tasks T8.A6a/T8.A6b will
# introduce context-aware (callable) replacements when display-name and Drive-
# file-ID scrubbers land.
SENSITIVE_PATTERNS: list[tuple[str, str]] = [
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
    (r'"QGcrse"\s*:\s*"(?:[^"\\]|\\.)*"', '"QGcrse":"SCRUBBED_CLIENT_ID"'),
    (r'"iQJtYd"\s*:\s*"(?:[^"\\]|\\.)*"', '"iQJtYd":"SCRUBBED_PROJECT_ID"'),
    # -------------------------------------------------------------------------
    # 5. Email addresses
    # -------------------------------------------------------------------------
    # JSON-quoted form. The replacement embeds ``@example.com`` so a second
    # scrub pass on already-scrubbed content is a no-op (idempotent).
    (f'"{_EMAIL_PATTERN_BASE}"', '"SCRUBBED_EMAIL@example.com"'),
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
        r'("name":\s*"(?:SID|HSID|SSID|APISID|SAPISID|SIDCC|OSID|NID|'
        r'__Secure-[^"]+|__Host-[^"]+)"\s*,\s*"value":\s*")[^"\\]*(?:\\.[^"\\]*)*(")',
        r"\1SCRUBBED\2",
    ),
    (
        r'("value":\s*")[^"\\]*(?:\\.[^"\\]*)*'
        r'("\s*,\s*"name":\s*"(?:SID|HSID|SSID|APISID|SAPISID|'
        r'SIDCC|OSID|NID|__Secure-[^"]+|__Host-[^"]+)")',
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
        r'("(?:SID|HSID|SSID|APISID|SAPISID|SIDCC|OSID|NID|'
        r'__Secure-[^"]+|__Host-[^"]+)"\s*:\s*")[^"\\]*(?:\\.[^"\\]*)*(")',
        r"\1SCRUBBED\2",
    ),
    # -------------------------------------------------------------------------
    # 9. Upload tokens (T8.A6b — audit I17)
    # -------------------------------------------------------------------------
    # X-GUploader-UploadID response header line. The token is a long random
    # string that uniquely identifies a resumable-upload session.
    (
        r"X-GUploader-UploadID: [A-Za-z0-9_\-]+",
        "X-GUploader-UploadID: SCRUBBED_UPLOAD_ID",
    ),
    # Full upload URL that embeds the upload_id token in its query string.
    # Match the whole URL (up to the next quote or whitespace) and collapse
    # to a stable canonical form so the token never round-trips through a
    # cassette body. The whole URL — including the ``upload_id=`` substring
    # — is replaced so the subsequent standalone ``upload_id=`` pattern
    # cannot re-match this placeholder and produce a non-idempotent rewrite.
    (
        r"https://notebooklm\.google\.com/upload/_/\?[^\"\s]*upload_id=[A-Za-z0-9_\-]+",
        "SCRUBBED_UPLOAD_URL",
    ),
    # Standalone upload_id query parameter (anywhere it appears outside the
    # full upload URL above).
    (r"upload_id=[A-Za-z0-9_\-]+", "upload_id=SCRUBBED_UPLOAD_ID"),
    # -------------------------------------------------------------------------
    # 10. Drive AONS tokens (T8.A6b — audit I17)
    # -------------------------------------------------------------------------
    # AONS-prefixed strings are Drive permission/ACL tokens. The 20-char tail
    # threshold avoids matching short literal "AONS" mentions in code or
    # documentation while catching real tokens (which are typically 50+ chars).
    (r"AONS[A-Za-z0-9_\-]{20,}", "SCRUBBED_AONS"),
    # -------------------------------------------------------------------------
    # 11. Drive file IDs (T8.A6b — audit I17) — context-aware ONLY
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
    # 12. Escaped JSON display-name literals (T8.A6a — audit C4)
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
    # 13. lh3.googleusercontent.com avatar URLs (T8.A6a — audit /ogw/ group)
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
    ("QGcrse (client_id)", re.compile(r'"QGcrse"\s*:\s*"((?:[^"\\]|\\.)*)"')),
    ("iQJtYd (project_id)", re.compile(r'"iQJtYd"\s*:\s*"((?:[^"\\]|\\.)*)"')),
]

# Compiled detection-only pattern for emails (no replacement string baked in).
_DETECT_EMAIL = re.compile(_EMAIL_PATTERN_BASE)

# T8.A6b — upload + Drive token detectors (audit I17).
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

# Full upload URL is replaced wholesale with ``SCRUBBED_UPLOAD_URL`` rather
# than per-field, so the detector matches the whole URL form and the leak
# check is "does it appear at all" (the only legitimate appearance of an
# upload URL in a cassette is the placeholder itself, which this regex does
# NOT match — so any match here is a leak).
_DETECT_UPLOAD_URL = re.compile(r"https://notebooklm\.google\.com/upload/_/\?[^\"\s]*upload_id=")

# T8.A6a — escaped JSON display-name literal detector (audit C4).
#
# Matches ``\"First Last\"`` inside a double-encoded JSON string. The
# false-positive allowlist is consulted at the call site in ``is_clean``
# rather than baked into the regex so the detector stays simple and the
# allowlist remains observable. The captured group is the inner name (no
# escape quotes) so we can compare it to DISPLAY_NAME_FALSE_POSITIVES
# directly.
_DETECT_DISPLAY_NAME_ESCAPED = re.compile(r'\\"([A-Z][a-z]+(?: [A-Z][a-z]+)+)\\"')

# T8.A6a — avatar URL detector (audit /ogw/ group). The pattern matches
# both ``/a/`` and ``/ogw/`` path forms. The scrubber collapses the entire
# URL to ``SCRUBBED_AVATAR_URL``, so any match here is by definition a
# leak (the placeholder string doesn't itself contain ``lh3.``).
_DETECT_AVATAR_URL = re.compile(r"https?://lh3\.googleusercontent\.com/(?:a|ogw)/[A-Za-z0-9_=\-]+")


def is_clean(text: str) -> tuple[bool, list[str]]:
    """Validate that ``text`` contains no unredacted sensitive data.

    Closes audit finding I7: cookie-value cleanliness is judged by exact
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

    Display-name + avatar coverage (T8.A6a)
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
    for match in _DETECT_EMAIL.finditer(text):
        leaks.append(f"Leak (email): {match.group(0)!r}")

    # --- 3. Token / ID fields that should be redacted ----------------------
    for label, regex in _DETECT_TOKEN_FIELDS:
        for match in regex.finditer(text):
            value = match.group(1)
            if value not in SCRUB_PLACEHOLDERS:
                leaks.append(f"Leak ({label}): {value!r}")

    # --- 4. T8.A6b — upload + Drive token fields ---------------------------
    for label, regex in _DETECT_UPLOAD_DRIVE_FIELDS:
        for match in regex.finditer(text):
            value = match.group(1)
            if value not in SCRUB_PLACEHOLDERS:
                leaks.append(f"Leak ({label}): {value!r}")

    # --- 5. T8.A6b — full upload URL --------------------------------------
    # The scrubber collapses the entire URL to ``SCRUBBED_UPLOAD_URL``, so any
    # match of the raw URL form here is by definition a leak.
    for match in _DETECT_UPLOAD_URL.finditer(text):
        leaks.append(f"Leak (upload URL): {match.group(0)!r}")

    # --- 6. T8.A6a — escaped display-name literals -------------------------
    # The false-positive allowlist (font families, UI titles, artifact /
    # notebook names) is consulted here rather than baked into the regex so
    # the detector stays simple and the allowlist remains observable.
    for match in _DETECT_DISPLAY_NAME_ESCAPED.finditer(text):
        inner = match.group(1)
        if inner in DISPLAY_NAME_FALSE_POSITIVES:
            continue
        leaks.append(f"Leak (escaped display name): {match.group(0)!r}")

    # --- 7. T8.A6a — avatar URLs ------------------------------------------
    # The scrubber collapses the whole URL to ``SCRUBBED_AVATAR_URL``, so any
    # match of the raw URL form here is by definition a leak.
    for match in _DETECT_AVATAR_URL.finditer(text):
        leaks.append(f"Leak (avatar URL): {match.group(0)!r}")

    return (not leaks, leaks)


# =============================================================================
# T8.E10 — Synthetic error-response builders for VCR recording
# =============================================================================
#
# These helpers exist so that T8.E4 (and any future error-shape cassette PRs)
# can generate cassettes whose responses match the shapes our client's
# exception mapping in ``src/notebooklm/_core.py`` keys on:
#
#   - HTTP 429  -> ``_TransportRateLimited`` -> ``RateLimitError``
#   - HTTP 5xx  -> ``_TransportServerError`` -> ``ServerError``
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

# Filename prefix that T8.E4 (and any future error-cassette PR) MUST apply to
# cassettes generated through this plumbing. The prefix is mechanical: it lets a
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
    the HTTP status code (see ``_core.py:is_auth_error`` and the 429 / 5xx
    branches in ``_perform_authed_post``), so a syntactically-valid Google
    error-shaped body is sufficient.

    For the ``expired_csrf`` mode we return HTTP 400 — not 401 — because that
    matches the documented Google contract: NotebookLM returns 400 (not 401/403)
    when the embedded CSRF token has expired, which is why ``is_auth_error``
    treats 400 as an auth-refresh trigger. See ``is_auth_error`` in
    ``src/notebooklm/_core.py``.

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
