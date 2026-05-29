"""Tests for notebooklm._logging — credential redaction and configuration."""

from __future__ import annotations

import io
import logging
import sys

import pytest

from notebooklm._logging import (
    RedactingFilter,
    RedactingFormatter,
    apply_redaction,
    configure_logging,
    install_redaction,
)


def raising_exc_info(message: str) -> tuple:
    """Raise ValueError(message) and return the resulting sys.exc_info() tuple."""
    try:
        raise ValueError(message)
    except ValueError:
        return sys.exc_info()


@pytest.fixture
def saved_logger_state():
    """Snapshot/restore the notebooklm logger so each test is independent."""
    logger = logging.getLogger("notebooklm")
    saved = (
        logger.handlers[:],
        logger.filters[:],
        logger.level,
        logger.propagate,
    )
    logger.handlers.clear()
    logger.filters.clear()
    logger.setLevel(logging.WARNING)
    logger.propagate = True
    try:
        yield logger
    finally:
        logger.handlers[:] = saved[0]
        logger.filters[:] = saved[1]
        logger.setLevel(saved[2])
        logger.propagate = saved[3]


@pytest.fixture
def saved_external_logger():
    """Snapshot/restore arbitrary external loggers by name."""
    saved: dict[str, tuple] = {}

    def _save(name: str) -> logging.Logger:
        lg = logging.getLogger(name)
        saved[name] = (lg.handlers[:], lg.filters[:], lg.level, lg.propagate)
        lg.handlers.clear()
        lg.filters.clear()
        lg.setLevel(logging.WARNING)
        lg.propagate = True
        return lg

    yield _save
    for name, (h, f, lvl, p) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers[:] = h
        lg.filters[:] = f
        lg.setLevel(lvl)
        lg.propagate = p


@pytest.fixture
def saved_root_logger():
    """Snapshot/restore the root logger's handlers."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.filters[:], root.level)
    yield root
    root.handlers[:] = saved[0]
    root.filters[:] = saved[1]
    root.setLevel(saved[2])


def _record(
    msg: str,
    *args: object,
    exc_info: object = None,
    name: str = "notebooklm.test",
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=logging.WARNING,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=args or None,
        exc_info=exc_info,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Formatter tests
# ---------------------------------------------------------------------------


def test_formatter_scrubs_csrf_token_in_url():
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    rec = _record("posting to url=https://x.example?hl=en&at=SECRET_TOK&rt=c")
    out = fmt.format(rec)
    assert "SECRET_TOK" not in out
    assert "at=***" in out


_GOOGLE_COOKIE_PAIRS = [
    ("SAPISID", "abc123def"),
    ("APISID", "apisid_val"),
    ("__Secure-1PSID", "psid_xyz"),
    ("__Secure-3PSID", "psid_qrs"),
    ("__Secure-1PSIDCC", "psidcc_xyz"),
    ("__Secure-3PSIDCC", "psidcc_qrs"),
    # PSIDTS — rotating token-state cookies. Pre-fix these only redacted when
    # they appeared inside a ``Cookie:`` / ``Set-Cookie:`` header value; a
    # standalone ``__Secure-1PSIDTS=<value>`` token (as appears in refresh-cmd
    # stdout/stderr captured at DEBUG) passed through unredacted.
    ("__Secure-1PSIDTS", "psidts_xyz"),
    ("__Secure-3PSIDTS", "psidts_qrs"),
    # PAPISID — auth-API session cookies. Without an explicit alternative the
    # captured ``\1`` group would resolve to the ``APISID`` suffix only.
    ("__Secure-1PAPISID", "papisid_xyz"),
    ("__Secure-3PAPISID", "papisid_qrs"),
    ("SIDCC", "sidcc_val"),
    ("HSID", "hsid_val"),
    ("SSID", "ssid_val"),
    ("LSID", "lsid_val"),
    ("SID", "sid_val"),
]


def test_formatter_scrubs_google_session_cookies():
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    jar = "; ".join(f"{name}={value}" for name, value in _GOOGLE_COOKIE_PAIRS)
    rec = _record(f"cookie jar: {jar}")
    out = fmt.format(rec)
    for cookie_name, secret in _GOOGLE_COOKIE_PAIRS:
        assert f"{cookie_name}=***" in out, f"missing {cookie_name}=***"
        assert secret not in out


def test_formatter_scrubs_fsid_query_param():
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    rec = _record("requesting f.sid=tok-xyz-123")
    out = fmt.format(rec)
    assert "tok-xyz-123" not in out
    assert "f.sid=***" in out


def test_formatter_scrubs_upload_id_query_param():
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    rec = _record("uploading to https://notebooklm.google.com/upload/_/?upload_id=SECRET_UPLOAD_ID")
    out = fmt.format(rec)
    assert "SECRET_UPLOAD_ID" not in out
    assert "upload_id=***" in out


def test_formatter_preserves_non_secret_text():
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    rec = _record("RPC LIST_NOTEBOOKS failed for nb_id=abc123 method=LIST in 0.42s")
    out = fmt.format(rec)
    for keep in ("nb_id=abc123", "method=LIST", "0.42s", "LIST_NOTEBOOKS"):
        assert keep in out, f"lost benign field: {keep}"


def test_formatter_scrubs_exception_traceback():
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    rec = _record(
        "rpc error", exc_info=raising_exc_info("request rejected for at=SECRET_TOK in body")
    )
    out = fmt.format(rec)
    assert "SECRET_TOK" not in out
    assert "at=***" in out


def test_formatter_scrubs_oauth_credentials():
    """refresh_token / access_token / id_token / code= OAuth params."""
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    rec = _record(
        "oauth body: refresh_token=RT_SECRET&access_token=AT_SECRET"
        "&id_token=IDT_SECRET&code=AUTH_CODE_X"
    )
    out = fmt.format(rec)
    for secret in ("RT_SECRET", "AT_SECRET", "IDT_SECRET", "AUTH_CODE_X"):
        assert secret not in out, f"{secret} leaked"
    for key in ("refresh_token=***", "access_token=***", "id_token=***", "code=***"):
        assert key in out


def test_formatter_scrubs_set_cookie_response_header():
    """Set-Cookie: response header (separate from Cookie: request header)."""
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    rec = _record("Set-Cookie: SAPISID=server_minted_value; Path=/; HttpOnly; Secure")
    out = fmt.format(rec)
    assert "server_minted_value" not in out
    assert "Set-Cookie: ***" in out


def test_formatter_scrubs_bare_csrf_and_session_markers():
    """Bare ``SNlM0e`` / ``FdrFJe`` markers must be redacted in every shape
    they appear in — JSON (``"SNlM0e":"AF1_QpN-..."``), query/form
    (``FdrFJe=...``), HTML-escaped (``&quot;...&quot;``), and diagnostic prose
    (``SNlM0e value is AF1_QpN-...``).

    Pre-fix the redactor only matched the canonical wire shapes (``at=``,
    ``f.sid=``, cookies); a third-party logger or exception text emitting a
    bare ``SNlM0e``/``FdrFJe`` value — or the ``csrf=`` alias — leaked a
    credential-equivalent token (the CSRF token authorizes all RPC mutations).
    See issue #1165.
    """
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    cases = [
        # marker, full input, secret value that must NOT survive
        ("SNlM0e", "SNlM0e value is AF1_QpN-PROSE_SECRET reported", "AF1_QpN-PROSE_SECRET"),
        ("FdrFJe", "session FdrFJe=1234567890123456 in url", "1234567890123456"),
        ("csrf", "form body csrf=AF1_QpN-CSRF_ALIAS&hl=en", "AF1_QpN-CSRF_ALIAS"),
        ("SNlM0e", 'wiz data {"SNlM0e":"AF1_QpN-JSON_SECRET"}', "AF1_QpN-JSON_SECRET"),
        ("SNlM0e", "single {'SNlM0e':'AF1_QpN-SQ_SECRET'}", "AF1_QpN-SQ_SECRET"),
        (
            "FdrFJe",
            "escaped {&quot;FdrFJe&quot;:&quot;9988776655&quot;}",
            "9988776655",
        ),
    ]
    for marker, text, secret in cases:
        rec = _record(text)
        out = fmt.format(rec)
        assert secret not in out, f"{secret!r} leaked from {text!r}: got {out!r}"
        # The marker name itself is preserved (only the value is redacted).
        assert marker in out, f"marker {marker!r} stripped from {text!r}: got {out!r}"
        assert "***" in out, f"no redaction placeholder in {out!r}"

    # Surrounding punctuation / quotes / brackets must be preserved exactly —
    # only the value is replaced. (gemini-code-assist: guard against the
    # unquoted value class swallowing trailing sentence punctuation or the
    # enclosing parens / JSON quotes.)
    exact = [
        ("SNlM0e value is AF1_QpN-PROSE_SECRET.", "SNlM0e value is ***."),
        ("(FdrFJe=1234567890123456)", "(FdrFJe=***)"),
        ('{"SNlM0e":"AF1_QpN-JSON_SECRET"}', '{"SNlM0e":"***"}'),
        ("{'SNlM0e':'AF1_QpN-SQ_SECRET'}", "{'SNlM0e':'***'}"),
        (
            "{&quot;FdrFJe&quot;:&quot;9988776655&quot;}",
            "{&quot;FdrFJe&quot;:&quot;***&quot;}",
        ),
    ]
    for text, expected in exact:
        rec = _record(text)
        assert fmt.format(rec) == expected, f"unexpected redaction of {text!r}"


def test_csrf_marker_regex_capture_groups():
    """Directly pin the compiled marker patterns' capture groups so a future
    edit can't silently shift what gets captured (and therefore reproduced
    verbatim) vs. redacted. A string-shape assertion alone can't distinguish
    "captured the right span" from "coincidental prefix preservation."
    (gemini-code-assist suggestion.)
    """
    from notebooklm._logging import (
        _CSRF_MARKER_HTML_ESCAPED,
        _CSRF_MARKER_QUOTED,
        _CSRF_MARKER_UNQUOTED,
    )

    m = _CSRF_MARKER_QUOTED.search('{"SNlM0e":"AF1_QpN-JSON_SECRET"}')
    assert m is not None
    assert m.group(1) == "SNlM0e"
    assert m.group(2) == '":'  # key-closing quote through the colon
    assert m.group(3) == '"'  # value quote, back-referenced as the closer
    assert "AF1_QpN-JSON_SECRET" in m.group(0)

    m = _CSRF_MARKER_UNQUOTED.search("FdrFJe value is AF1_QpN-PROSE_SECRET.")
    assert m is not None
    assert m.group(1) == "FdrFJe"
    assert m.group(2) == " value is "
    # The trailing period is NOT part of the match (value class excludes it).
    assert m.group(0).endswith("AF1_QpN-PROSE_SECRET")

    m = _CSRF_MARKER_HTML_ESCAPED.search("{&quot;SNlM0e&quot;:&quot;sekret&quot;}")
    assert m is not None
    assert m.group(1) == "SNlM0e"
    assert m.group(2) == "&quot;:"
    assert m.group(3) == "&quot;"


def test_formatter_scrubs_bare_af1_qpn_csrf_token():
    """A standalone Google CSRF token (``AF1_QpN-...`` family) is redacted even
    with no surrounding marker — the prefix is the credential's stable shape.

    The ``AF1_QpN-`` prefix is preserved as a diagnostic shape hint; the secret
    suffix is dropped. Regression for issue #1165 (defense-in-depth)."""
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    rec = _record("rejected token AF1_QpN-LOOSE_SECRET_SUFFIX seen on the wire")
    out = fmt.format(rec)
    assert "LOOSE_SECRET_SUFFIX" not in out
    assert "AF1_QpN-***" in out


def test_formatter_marker_redaction_preserves_benign_at_suffix_fields():
    """The CSRF/session markers must not over-redact benign fields.

    ``csrf_protected``/``nb_sid`` contain fast-path token substrings (``csrf``,
    ``sid``) so the gate opens and the regex sweep genuinely runs — proving the
    anchored markers don't redact fields that merely *contain* a token
    substring (and that the cookie / ``csrf=`` patterns don't fire on a
    ``csrf_protected=`` prefix). Guards against an overbroad #1165 fix.
    (coderabbitai: the original input had no fast-path token and so was
    short-circuited before any pattern ran.)"""
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    rec = _record(
        "metrics rate=10 coordinate=5 valid=true latency_ms=420 csrf_protected=yes nb_sid=keepme"
    )
    out = fmt.format(rec)
    for keep in (
        "rate=10",
        "coordinate=5",
        "valid=true",
        "latency_ms=420",
        "csrf_protected=yes",
        "nb_sid=keepme",
    ):
        assert keep in out, f"benign field {keep!r} over-redacted: {out!r}"


def test_formatter_scrubs_psidts_in_non_header_shapes():
    """Standalone ``__Secure-[13]PSIDTS=<value>`` tokens must be redacted in
    every HTTP-shaped form they appear in (refresh-cmd stdout/stderr,
    comma-separated cookie listings) — not just inside a ``Cookie:`` header.

    Pre-fix, the cookie-name alternation listed ``__Secure-[13]PSID`` and
    ``__Secure-[13]PSIDCC`` but NOT the ``PSIDTS`` (token-state) family. A
    ``RuntimeError`` traceback containing ``__Secure-1PSIDTS=abc123`` from a
    refresh subprocess therefore leaked the value verbatim through any error
    surface that wasn't fronted by the ``Cookie:`` / ``Set-Cookie:`` regex.

    Scope note: JSON-shaped ``"name":"__Secure-1PSIDTS","value":"..."`` is
    NOT covered by this runtime redactor — that storage-state shape lives in
    the VCR cassette sanitizer (see ``tests/vcr_config.py`` and
    ``tests/unit/test_cookie_redaction.py``).
    """
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))

    cases = [
        # Refresh-cmd stdout/stderr shape: plain ``name=value`` standalone token.
        "rotation: __Secure-1PSIDTS=SECRET_PSIDTS_VALUE_A refreshed",
        # Comma-separated listing.
        "cookies: __Secure-1PSIDTS=SECRET_PSIDTS_VALUE_B, __Secure-3PSIDTS=SECRET_PSIDTS_VALUE_C",
    ]
    for text in cases:
        rec = _record(text)
        out = fmt.format(rec)
        for secret in (
            "SECRET_PSIDTS_VALUE_A",
            "SECRET_PSIDTS_VALUE_B",
            "SECRET_PSIDTS_VALUE_C",
        ):
            assert secret not in out, f"PSIDTS value {secret!r} leaked from {text!r}: got {out!r}"


def test_formatter_psid_family_redacts_independently():
    """The PSIDTS / PSIDCC / PSID variants must redact INDEPENDENTLY.

    Python's ``re`` engine backtracks, so the bare ``__Secure-1PSID``
    alternative does NOT shadow ``__Secure-1PSIDTS`` even if listed first
    (the engine commits, finds no ``=`` immediately after, then retries
    the next alternative). The longer-first ordering in the source regex
    is a defensive convention + microscale perf, not load-bearing for
    correctness.

    This test pins what IS load-bearing: each of the three suffix variants
    (``PSID`` / ``PSIDCC`` / ``PSIDTS``) must scrub when present together,
    independent of any ordering subtlety.
    """
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    text = (
        "jar: __Secure-1PSID=BARE_PSID_VAL; "
        "__Secure-1PSIDCC=PSIDCC_VAL; "
        "__Secure-1PSIDTS=PSIDTS_VAL"
    )
    rec = _record(text)
    out = fmt.format(rec)
    for secret in ("BARE_PSID_VAL", "PSIDCC_VAL", "PSIDTS_VAL"):
        assert secret not in out, f"{secret} leaked: {out!r}"
    for redacted in (
        "__Secure-1PSID=***",
        "__Secure-1PSIDCC=***",
        "__Secure-1PSIDTS=***",
    ):
        assert redacted in out, f"missing {redacted}: {out!r}"


def test_formatter_scrubs_papisid_with_correct_captured_name():
    """``__Secure-[13]PAPISID`` must be matched as its own alternative.

    Without an explicit alternative, the engine would match the ``APISID``
    suffix at position 11 instead, capturing only ``APISID`` as ``\\1``
    while leaving the ``__Secure-1P`` prefix in place. The substituted
    output looks identical at the surface (``__Secure-1PAPISID=***``)
    because the unmatched prefix is preserved verbatim, so a string-shape
    assertion alone cannot distinguish "full name captured" from
    "coincidental prefix preservation."

    We pin the invariant directly: walk ``_REDACT_PATTERNS`` and assert
    that the first cookie-pattern match on a PAPISID input has
    ``group(1) == "__Secure-1PAPISID"``. Removing the PAPISID alternative
    from the regex would flip ``group(1)`` to ``"APISID"``, which this
    assertion catches even though the substituted output would still look
    correct.
    """
    from notebooklm._logging import _REDACT_PATTERNS

    # Find the cookie pattern (the one whose replacement is "\1=***" and
    # whose source mentions __Secure-). Don't hardcode an index.
    cookie_pattern = next(
        p for p, repl in _REDACT_PATTERNS if repl == r"\1=***" and "__Secure" in p.pattern
    )

    # group(1) must be the FULL cookie name, not the APISID suffix.
    m1 = cookie_pattern.search("__Secure-1PAPISID=SECRET_1PAPISID")
    assert m1 is not None
    assert m1.group(1) == "__Secure-1PAPISID", (
        f"capture group resolved to suffix instead of full name: {m1.group(1)!r}"
    )
    m3 = cookie_pattern.search("__Secure-3PAPISID=SECRET_3PAPISID")
    assert m3 is not None
    assert m3.group(1) == "__Secure-3PAPISID", (
        f"capture group resolved to suffix instead of full name: {m3.group(1)!r}"
    )

    # End-to-end shape check via the formatter (regression for the visible
    # substitution + the no-PAPAPISID corruption guard).
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    text = "jar: __Secure-1PAPISID=SECRET_1PAPISID; __Secure-3PAPISID=SECRET_3PAPISID"
    rec = _record(text)
    out = fmt.format(rec)
    assert "SECRET_1PAPISID" not in out
    assert "SECRET_3PAPISID" not in out
    assert "__Secure-1PAPISID=***" in out
    assert "__Secure-3PAPISID=***" in out
    assert "PAPAPISID" not in out


def test_formatter_handles_nonstring_record_msg():
    """record.msg may be an Exception or other non-str; _scrub must coerce."""
    fmt = RedactingFormatter(logging.Formatter("%(message)s"))
    # logging.LogRecord accepts non-str msg; getMessage() returns str(msg) % args.
    err = RuntimeError("token at=SECRET_TOK in repr")
    rec = _record(err)  # msg is the Exception object
    out = fmt.format(rec)
    assert "SECRET_TOK" not in out
    assert "at=***" in out


def test_filter_scrubs_stack_info():
    """stack_info from logger.<level>(..., stack_info=True) is scrubbed."""
    filt = RedactingFilter()
    rec = _record("hello")
    rec.stack_info = (
        "Stack (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        "    token at=SECRET_TOK leaked here\n"
    )
    filt.filter(rec)
    assert "SECRET_TOK" not in rec.stack_info
    assert "at=***" in rec.stack_info


def test_formatter_clears_polluted_exc_text_on_record():
    """Direct formatter usage without Filter: inner.format may set unscrubbed
    record.exc_text as a side effect. RedactingFormatter re-scrubs it so a
    subsequent handler cannot leak via record.exc_text."""
    inner = logging.Formatter("%(message)s")
    fmt = RedactingFormatter(inner)
    rec = _record(
        "direct call",
        exc_info=raising_exc_info("traceback contains at=POLLUTED_TOK"),
    )
    # rec.exc_text is None initially.
    assert rec.exc_text is None
    fmt.format(rec)
    # After format(), inner sets exc_text as a side effect. We must have
    # re-scrubbed it.
    assert rec.exc_text is not None
    assert "POLLUTED_TOK" not in rec.exc_text
    assert "at=***" in rec.exc_text


# ---------------------------------------------------------------------------
# Filter tests
# ---------------------------------------------------------------------------


def test_filter_mutates_record_msg_in_place_and_preserves_exc_info():
    """v4 critical regression: exc_info is preserved (not nulled). exc_text scrubbed."""
    filt = RedactingFilter()
    rec = _record(
        "scrub me: at=ALSO_SECRET",
        exc_info=raising_exc_info("at=SECRET in traceback"),
    )

    assert filt.filter(rec) is True

    # Message scrubbed in place
    assert "ALSO_SECRET" not in rec.msg
    assert "at=***" in rec.msg
    assert rec.args == ()

    # exc_info PRESERVED (regression for v3 that nulled it)
    assert rec.exc_info is not None
    # exc_text rendered and scrubbed
    assert rec.exc_text is not None
    assert "SECRET" not in rec.exc_text
    assert "at=***" in rec.exc_text


def test_filter_isolated_from_formatter():
    """Install ONLY the Filter (no RedactingFormatter); assert it still scrubs.

    Proves the Filter alone is sufficient — removing it would fail this test.
    """
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.NOTSET)
    handler.setFormatter(logging.Formatter("%(message)s"))  # plain, not redacting
    handler.addFilter(RedactingFilter())

    logger = logging.getLogger("test_filter_isolation_unique")
    logger.handlers.clear()
    logger.filters.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False

    try:
        logger.warning("token at=SECRET_TOK in request")
        out = buf.getvalue()
        assert "SECRET_TOK" not in out
        assert "at=***" in out
    finally:
        logger.handlers.clear()
        logger.filters.clear()


# ---------------------------------------------------------------------------
# configure_logging + handler installation
# ---------------------------------------------------------------------------


def test_handler_has_redacting_filter_installed(saved_logger_state):
    """Structural assertion: configure_logging produces a handler with filter+formatter."""
    configure_logging()
    handlers = saved_logger_state.handlers
    assert len(handlers) == 1
    h = handlers[0]
    assert any(isinstance(f, RedactingFilter) for f in h.filters)
    assert isinstance(h.formatter, RedactingFormatter)
    assert getattr(h, "_notebooklm_redacting", False) is True
    assert saved_logger_state.propagate is True  # v4: propagate=True for caplog


def test_child_emission_scrubbed_via_parent_handler_mutation(saved_logger_state):
    """Records emitted on notebooklm.<child> reach parent handler with filter."""
    configure_logging()
    buf = io.StringIO()
    handler = saved_logger_state.handlers[0]
    handler.stream = buf

    logging.getLogger("notebooklm._core").warning(
        "child record: at=ALSO_SECRET", exc_info=raising_exc_info("at=SECRET_TOK detail")
    )

    out = buf.getvalue()
    assert "SECRET_TOK" not in out
    assert "ALSO_SECRET" not in out
    assert "at=***" in out


def test_records_propagate_to_root_with_scrubbed_msg(saved_logger_state, saved_root_logger):
    """caplog regression: propagate=True must still produce scrubbed records at root."""
    root_buf = io.StringIO()
    root_handler = logging.StreamHandler(root_buf)
    root_handler.setLevel(logging.NOTSET)
    root_handler.setFormatter(logging.Formatter("%(message)s"))
    saved_root_logger.addHandler(root_handler)
    saved_root_logger.setLevel(logging.WARNING)

    configure_logging()

    logging.getLogger("notebooklm._core").warning("propagated: at=SECRET_TOK should be scrubbed")

    root_out = root_buf.getvalue()
    # Record reached root via propagation (proves propagate=True still works).
    assert "propagated:" in root_out
    # AND it arrived with record.msg already mutated by our filter.
    assert "SECRET_TOK" not in root_out
    assert "at=***" in root_out


# ---------------------------------------------------------------------------
# apply_redaction / pre-existing handlers
# ---------------------------------------------------------------------------


def test_preexisting_handler_gets_redaction_with_braces_style_preserved(
    saved_logger_state,
):
    """v3 regression: apply_redaction must not crash on {message}-style formatters."""
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.NOTSET)
    h.setFormatter(logging.Formatter("{message}", style="{"))
    saved_logger_state.addHandler(h)

    # Must not raise (v3 crashed here with ValueError).
    configure_logging()

    # The pre-existing handler now has our filter and a decorating formatter.
    assert any(isinstance(f, RedactingFilter) for f in h.filters)
    assert isinstance(h.formatter, RedactingFormatter)
    assert getattr(h, "_notebooklm_redacting", False) is True

    # Scrubbing still works via the pre-existing handler.
    logging.getLogger("notebooklm._core").warning("preexisting at=SECRET_TOK record")
    out = buf.getvalue()
    assert "SECRET_TOK" not in out
    assert "at=***" in out


def test_preexisting_custom_formatter_subclass_preserved(saved_logger_state):
    """Decorator must preserve a custom formatter subclass's overrides."""

    class TaggingFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            return "[TAG] " + super().format(record)

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.NOTSET)
    h.setFormatter(TaggingFormatter("%(message)s"))
    saved_logger_state.addHandler(h)

    configure_logging()

    # Apply_redaction wraps but inner TaggingFormatter is still called.
    logging.getLogger("notebooklm._core").warning("hello at=SECRET")
    out = buf.getvalue()
    assert out.startswith("[TAG] ")
    assert "SECRET" not in out
    assert "at=***" in out


# ---------------------------------------------------------------------------
# install_redaction
# ---------------------------------------------------------------------------


def test_install_redaction_redacts_child_logger_via_propagation(
    saved_external_logger,
):
    """install_redaction('httpx') scrubs records from httpx._client via propagation."""
    saved_external_logger("httpx")
    saved_external_logger("httpx._client")

    install_redaction("httpx")

    # Find our marked handler on httpx and replace its stream.
    httpx_logger = logging.getLogger("httpx")
    our_handlers = [h for h in httpx_logger.handlers if getattr(h, "_notebooklm_redacting", False)]
    assert len(our_handlers) == 1
    buf = io.StringIO()
    our_handlers[0].stream = buf

    logging.getLogger("httpx._client").warning("request: f.sid=tok-xyz and at=SECRET_TOK")

    out = buf.getvalue()
    assert "SECRET_TOK" not in out
    assert "tok-xyz" not in out
    assert "at=***" in out
    assert "f.sid=***" in out


def test_install_redaction_decorates_preexisting_handler(saved_external_logger):
    """install_redaction also wraps an existing handler's formatter."""
    target = saved_external_logger("notebooklm_install_test")
    buf = io.StringIO()
    existing = logging.StreamHandler(buf)
    existing.setLevel(logging.NOTSET)
    existing.setFormatter(logging.Formatter("%(message)s"))
    target.addHandler(existing)
    assert getattr(existing, "_notebooklm_redacting", False) is False

    install_redaction("notebooklm_install_test")

    assert any(isinstance(f, RedactingFilter) for f in existing.filters)
    assert isinstance(existing.formatter, RedactingFormatter)
    assert getattr(existing, "_notebooklm_redacting", False) is True


def test_configure_logging_honors_debug_rpc_env(saved_logger_state, monkeypatch):
    """NOTEBOOKLM_DEBUG_RPC=1 forces DEBUG level."""
    monkeypatch.setenv("NOTEBOOKLM_DEBUG_RPC", "1")
    monkeypatch.delenv("NOTEBOOKLM_LOG_LEVEL", raising=False)
    configure_logging()
    assert saved_logger_state.level == logging.DEBUG


def test_apply_redaction_is_idempotent(saved_logger_state):
    """Calling apply_redaction twice does not double-wrap filter or formatter."""
    h = logging.StreamHandler()
    h.setLevel(logging.NOTSET)
    apply_redaction(h)
    filter_count_after_first = len(h.filters)
    fmt_after_first = h.formatter

    apply_redaction(h)

    assert len(h.filters) == filter_count_after_first  # no double filter
    assert h.formatter is fmt_after_first  # same RedactingFormatter, not re-wrapped


def test_filter_rescrubs_existing_exc_text():
    """If a record already has exc_text set (handler pre-rendered), filter re-scrubs."""
    filt = RedactingFilter()
    rec = _record("msg")
    rec.exc_text = "previously rendered with at=LEAK and SAPISID=oops"
    rec.exc_info = None  # exc_text path, not exc_info path

    filt.filter(rec)

    assert "LEAK" not in rec.exc_text
    assert "oops" not in rec.exc_text
    assert "at=***" in rec.exc_text
    assert "SAPISID=***" in rec.exc_text


def test_decorator_delegates_format_methods():
    """formatTime/formatException/formatStack delegate to inner and scrub."""
    inner = logging.Formatter("%(message)s")
    fmt = RedactingFormatter(inner)
    rec = _record("ignored")

    # formatTime delegates (and is not scrubbed — datetime strings have no secrets)
    assert fmt.formatTime(rec) == inner.formatTime(rec)

    # formatException delegates and scrubs
    ei = raising_exc_info("traceback with at=SECRET")
    rendered_exc = fmt.formatException(ei)
    assert "SECRET" not in rendered_exc
    assert "at=***" in rendered_exc

    # formatStack delegates and scrubs
    stack_text = "stack: at=ANOTHER_SECRET"
    rendered_stack = fmt.formatStack(stack_text)
    assert "ANOTHER_SECRET" not in rendered_stack
    assert "at=***" in rendered_stack


def test_install_redaction_no_root_mutation(saved_external_logger, saved_root_logger):
    """install_redaction must not touch root.handlers."""
    saved_external_logger("httpx_alt_test")
    root_handlers_before = saved_root_logger.handlers[:]

    install_redaction("httpx_alt_test")

    # Root unchanged.
    assert saved_root_logger.handlers == root_handlers_before
    # Target logger now has our marked handler.
    target = logging.getLogger("httpx_alt_test")
    assert any(getattr(h, "_notebooklm_redacting", False) for h in target.handlers)


# ---------------------------------------------------------------------------
# Fast-path gate (SECRET_FAST_PATH_TOKENS) — correctness + perf
# ---------------------------------------------------------------------------


def test_fast_path_tokens_are_lowercase():
    """Tokens must be lowercase because the gate lowercases input.

    Mixed-case tokens would never match (``"SID" in "...sid..."`` is False).
    """
    from notebooklm import _logging
    from notebooklm._logging import SECRET_FAST_PATH_TOKENS

    assert "SECRET_FAST_PATH_TOKENS" in _logging.__all__

    for token in SECRET_FAST_PATH_TOKENS:
        assert token == token.lower(), f"token {token!r} must be lowercase"


def test_fast_path_tokens_cover_every_redaction_pattern():
    """Every pattern in _REDACT_PATTERNS has at least one literal substring
    present in SECRET_FAST_PATH_TOKENS (compared case-insensitively).

    This is the load-bearing invariant of the fast-path: if a pattern's
    anchor isn't covered, the gate would skip strings the regex would have
    redacted, silently shrinking the redaction surface.
    """
    from notebooklm import _logging
    from notebooklm._logging import SECRET_FAST_PATH_TOKENS

    # Sample inputs known to trigger each pattern, paired with the lowercase
    # token that covers them. Each entry MUST contain at least one fast-path
    # token (case-insensitively) AND get rewritten by scrub_secrets.
    samples = [
        ("at=", "posted body at=SECRET_X&hl=en"),
        ("snlm0e", 'wiz data "SNlM0e":"AF1_QpN-SECRET_X"'),
        ("fdrfje", "session FdrFJe=SECRET_SID_X"),
        ("csrf", "form csrf=AF1_QpN-SECRET_X"),
        ("af1_qpn-", "bare token AF1_QpN-SECRET_X in prose"),
        ("f.sid", "url ?f.sid=ABC_DEF"),
        ("_token=", "oauth body refresh_token=RT&access_token=AT&id_token=IT"),
        ("code=", "oauth callback code=AUTH_X"),
        ("sid", "cookie SID=v1; SAPISID=v2; HSID=v3"),
        ("authorization", "Authorization: Bearer SECRET"),
        ("cookie", "Cookie: jar=foo"),
        ("set-cookie", "Set-Cookie: SID=fresh"),
    ]
    for required_token, text in samples:
        assert required_token in SECRET_FAST_PATH_TOKENS, (
            f"sample {text!r} requires token {required_token!r} in SECRET_FAST_PATH_TOKENS"
        )
        # Sanity: at least one token from the set appears in the lowercased input.
        lowered = text.lower()
        assert any(t in lowered for t in SECRET_FAST_PATH_TOKENS), (
            f"sample {text!r} would be skipped by fast-path"
        )
        # And scrub_secrets actually redacts it.
        scrubbed = _logging.scrub_secrets(text)
        assert scrubbed != text, f"scrub_secrets did not change {text!r}"


def test_fast_path_handles_case_insensitive_patterns():
    """OAuth and Authorization patterns are IGNORECASE; the fast-path must
    still trigger redaction when those anchors appear in non-canonical casing.

    Regression for the Gemini-flagged case-sensitivity bug: a log line with
    ``AUTHORIZATION: Bearer ...`` or ``Refresh_Token=...`` must NOT bypass.
    """
    from notebooklm._logging import scrub_secrets

    cases = [
        ("AUTHORIZATION: Bearer SECRET_A", "SECRET_A", "Bearer ***"),
        ("authorization: bearer SECRET_B", "SECRET_B", "bearer ***"),
        ("oauth Refresh_Token=RT_X&Code=CODE_X", "RT_X", "Refresh_Token=***"),
        ("oauth Refresh_Token=RT_X&Code=CODE_X", "CODE_X", "Code=***"),
        ("COOKIE: SID=alpha", "alpha", "COOKIE: ***"),
        ("set-COOKIE: SID=beta", "beta", "set-COOKIE: ***"),
    ]
    for text, secret, must_contain in cases:
        out = scrub_secrets(text)
        assert secret not in out, f"{secret!r} leaked from {text!r}: got {out!r}"
        assert must_contain in out, f"expected {must_contain!r} in scrubbed {text!r}: got {out!r}"


def test_fast_path_skips_innocuous_messages_unchanged():
    """A string with no fast-path token must round-trip through scrub_secrets."""
    from notebooklm._logging import scrub_secrets

    benign = "RPC LIST_NOTEBOOKS finished in 0.42s for nb_id=abc123 with 12 sources"
    assert scrub_secrets(benign) is benign or scrub_secrets(benign) == benign


def test_fast_path_bypass_skips_regex_patterns(monkeypatch):
    """Fast-path must skip the expensive regex sweep for innocuous redactions.

    This used to benchmark the speedup ratio, but timing assertions flap on
    loaded CI. The deterministic invariant is the important part: no fast-path
    token means no regex pattern is consulted; a token hit runs the full sweep.
    """
    from notebooklm import _logging

    innocuous = (
        "RPC finished in 0.42s for nb_id=abc123 with 12 sources; method=fetch req=ok latency_ms=420"
    )
    # Confirm the sample really has no fast-path token (otherwise the no-call
    # assertion proves nothing). The gate compares lowercase to lowercase.
    lowered = innocuous.lower()
    assert not any(t in lowered for t in _logging.SECRET_FAST_PATH_TOKENS), (
        "benchmark input must not contain any fast-path token"
    )

    class CountingPattern:
        def __init__(self) -> None:
            self.calls = 0

        def __repr__(self) -> str:
            return f"CountingPattern(calls={self.calls})"

        def sub(self, _replacement: str, text: str) -> str:
            self.calls += 1
            return text

    counting_patterns = tuple(
        (CountingPattern(), replacement) for _pattern, replacement in _logging._REDACT_PATTERNS
    )
    assert counting_patterns, "benchmark must install at least one counting pattern"
    monkeypatch.setattr(_logging, "_REDACT_PATTERNS", counting_patterns)

    assert _logging.scrub_secrets(innocuous) == innocuous
    assert all(pattern.calls == 0 for pattern, _replacement in counting_patterns)

    # Replace the gate predicate with one that reports a hit, forcing the full
    # _REDACT_PATTERNS sweep. This proves the call-count check would catch a
    # regression that accidentally removes the fast-path bypass.
    # Use a token guaranteed to appear in the input.
    monkeypatch.setattr(_logging, "SECRET_FAST_PATH_TOKENS", ("nb_id",))
    assert any(t in innocuous for t in _logging.SECRET_FAST_PATH_TOKENS)
    assert _logging.scrub_secrets(innocuous) == innocuous
    assert all(pattern.calls == 1 for pattern, _replacement in counting_patterns)


def test_fast_path_still_redacts_when_token_present():
    """Belt-and-suspenders: a string containing a fast-path token must still
    flow through the full regex sweep and get scrubbed."""
    from notebooklm._logging import scrub_secrets

    out = scrub_secrets("posting body at=SUPER_SECRET&hl=en")
    assert "SUPER_SECRET" not in out
    assert "at=***" in out


def test_oauth_bundle_redacts_via_extended_token_set():
    """The plan's literal token list omits OAuth anchors; we extend it.

    This regression test pins the extension: an OAuth-only string (no other
    secret markers) must STILL be redacted after the fast-path gate.
    """
    from notebooklm._logging import scrub_secrets

    body = "refresh_token=RT_X&access_token=AT_X&id_token=IT_X&code=AUTH_X"
    out = scrub_secrets(body)
    for leaked in ("RT_X", "AT_X", "IT_X", "AUTH_X"):
        assert leaked not in out, f"{leaked} leaked through fast-path"
    for redacted in (
        "refresh_token=***",
        "access_token=***",
        "id_token=***",
        "code=***",
    ):
        assert redacted in out
