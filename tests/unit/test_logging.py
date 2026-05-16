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
