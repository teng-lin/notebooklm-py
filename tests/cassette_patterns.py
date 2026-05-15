"""Cassette sanitization helpers shared between VCR config and the bulk re-scrub script.

This module is the canonical home for cassette-mutating utilities. T8.A4 will
populate it with the SENSITIVE_PATTERNS registry currently inlined in
``tests/vcr_config.py``; T8.D7 (this commit) adds the chunk-prefix re-derivation
helper that T8.B6's bulk re-scrub script depends on.

Why the helper lives here, not in ``vcr_config.py``:

- ``vcr_config.py`` is loaded for every VCR-decorated test, but its public surface
  is intentionally narrow (VCR object + matchers). Scrub-time string surgery is
  a separate concern and benefits from being importable on its own (the bulk
  re-scrub script in ``scripts/`` has no need for the VCR object).
- T8.B6 reads cassettes off disk, re-scrubs every interaction, and re-derives
  byte-counts in a single pass. It imports ``recompute_chunk_prefix`` from here.
- Decoder tolerance behavior in ``src/notebooklm/rpc/decoder.py`` (warning on
  byte-count mismatch but still parsing the JSON) is intentionally UNCHANGED —
  this helper exists so cassettes don't trigger that warning during replay, not
  to harden the decoder against drift in production responses.
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
       Y bytes`` warnings during replay (the JSON is still parsed — see the
       tolerance block at decoder.py:217-237 — but the warning is noise).

    This helper walks the body, identifies every digit-only "header" line that
    is immediately followed by a non-header line, and replaces the header with
    the correct count for that payload. Byte count uses ``len(payload.encode(
    "utf-8"))`` — matching the on-wire protocol AND the
    ``len(json_str.encode("utf-8"))`` calculation the decoder uses. For
    ASCII-only payloads (the common case for batchexecute JSON), this is
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
