"""Tests for per-request correlation IDs via contextvars.ContextVar.

Phase 1 / T2. See .sisyphus/plans/phase-1-implementation.md.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from contextvars import Token

import pytest

from notebooklm._logging import (
    RedactingFilter,
    RedactingFormatter,
    get_request_id,
    reset_request_id,
    set_request_id,
)


@pytest.fixture(autouse=True)
def clear_request_id():
    """Snapshot/restore the request id ContextVar around each test."""
    from notebooklm._logging import _current_request_id

    token = _current_request_id.set(_current_request_id.get())
    try:
        yield
    finally:
        _current_request_id.reset(token)


def _make_record(msg: str = "test") -> logging.LogRecord:
    return logging.LogRecord(
        name="notebooklm.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=None,
        exc_info=None,
    )


# ---------------------------------------------------------------------------
# set/reset/get primitives
# ---------------------------------------------------------------------------


def test_set_request_id_returns_token_and_generated_id():
    """set_request_id() returns a Token; get_request_id() returns 8-hex."""
    token = set_request_id()
    try:
        assert isinstance(token, Token)
        rid = get_request_id()
        assert rid is not None
        assert re.fullmatch(r"[0-9a-f]{8}", rid)
    finally:
        reset_request_id(token)


def test_set_request_id_accepts_explicit_value():
    """Pass req_id=... to set a specific id."""
    token = set_request_id("CUSTOM01")
    try:
        assert get_request_id() == "CUSTOM01"
    finally:
        reset_request_id(token)


def test_reset_restores_previous_value():
    """reset_request_id restores to PREVIOUS, not None."""
    outer = set_request_id("OUTER001")
    try:
        inner = set_request_id("INNER001")
        try:
            assert get_request_id() == "INNER001"
        finally:
            reset_request_id(inner)
        assert get_request_id() == "OUTER001"
    finally:
        reset_request_id(outer)
    assert get_request_id() is None


# ---------------------------------------------------------------------------
# Filter integration
# ---------------------------------------------------------------------------


def test_filter_prefixes_after_scrub():
    """Filter prefixes [req=<id>] AFTER scrubbing. Token never appears in output."""
    filt = RedactingFilter()
    token = set_request_id("ABCD0123")
    try:
        rec = _make_record("scrubbable at=SECRET_TOK detail")
        filt.filter(rec)
        # Prefix outside the scrubbed body
        assert rec.msg.startswith("[req=ABCD0123] ")
        # Scrub still happened
        assert "SECRET_TOK" not in rec.msg
        assert "at=***" in rec.msg
    finally:
        reset_request_id(token)


def test_filter_no_prefix_when_id_unset():
    """With no id set, record.msg has no prefix."""
    filt = RedactingFilter()
    rec = _make_record("plain message")
    filt.filter(rec)
    assert not rec.msg.startswith("[req=")
    assert rec.msg == "plain message"


def test_filter_no_double_prefix_through_two_filters():
    """Marker attribute prevents second filter pass from re-prefixing."""
    filt1 = RedactingFilter()
    filt2 = RedactingFilter()
    token = set_request_id("XY00ZZ11")
    try:
        rec = _make_record("once")
        filt1.filter(rec)
        filt2.filter(rec)  # second pass, simulating two handlers
        # Only one [req=...] prefix
        assert rec.msg.count("[req=XY00ZZ11]") == 1
    finally:
        reset_request_id(token)


def test_filter_includes_prefix_for_exc_info_records():
    """exc_info path also gets the prefix on the message line."""
    filt = RedactingFilter()
    token = set_request_id("EXC00001")
    try:
        try:
            raise ValueError("oops at=LEAK")
        except ValueError:
            import sys

            rec = logging.LogRecord(
                name="notebooklm.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=0,
                msg="failure",
                args=None,
                exc_info=sys.exc_info(),
            )
        filt.filter(rec)
        assert rec.msg.startswith("[req=EXC00001] ")
        assert "LEAK" not in (rec.exc_text or "")
    finally:
        reset_request_id(token)


# ---------------------------------------------------------------------------
# asyncio Task isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_tasks_see_distinct_ids():
    """gather(task_a, task_b) — each sets its own id; emissions are tagged distinctly."""
    captured: dict[str, str] = {}

    async def worker(name: str, explicit_id: str) -> None:
        token = set_request_id(explicit_id)
        try:
            await asyncio.sleep(0)  # yield to interleave with sibling
            captured[name] = get_request_id() or ""
        finally:
            reset_request_id(token)

    await asyncio.gather(worker("a", "AAAA0000"), worker("b", "BBBB1111"))
    assert captured["a"] == "AAAA0000"
    assert captured["b"] == "BBBB1111"


@pytest.mark.asyncio
async def test_id_does_not_leak_after_reset():
    """After reset, subsequent emissions have no prefix."""
    filt = RedactingFilter()
    token = set_request_id("KEEP0001")
    rec1 = _make_record("inside")
    filt.filter(rec1)
    assert "[req=KEEP0001]" in rec1.msg

    reset_request_id(token)
    rec2 = _make_record("outside")
    filt.filter(rec2)
    assert "[req=" not in rec2.msg


@pytest.mark.asyncio
async def test_correlation_threads_through_child_loggers():
    """Emissions from notebooklm._core and notebooklm._chat get same prefix
    within a single task."""
    filt = RedactingFilter()
    token = set_request_id("CHILD001")
    try:
        rec_core = logging.LogRecord(
            name="notebooklm._core",
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg="core msg",
            args=None,
            exc_info=None,
        )
        rec_chat = logging.LogRecord(
            name="notebooklm._chat",
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg="chat msg",
            args=None,
            exc_info=None,
        )
        filt.filter(rec_core)
        filt.filter(rec_chat)
        assert rec_core.msg.startswith("[req=CHILD001] ")
        assert rec_chat.msg.startswith("[req=CHILD001] ")
    finally:
        reset_request_id(token)


# ---------------------------------------------------------------------------
# Integration with full Formatter + Filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_inherits_parent_request_id():
    """Recursive rpc_call(_is_retry=True) must NOT mint a fresh id — the
    failure→refresh→retry sequence should appear under one prefix."""
    from notebooklm._core import ClientCore

    captured_ids: list[str | None] = []

    async def fake_impl(method, params, source_path, allow_null, is_retry, rate_limit_retries=0):
        captured_ids.append(get_request_id())
        # First call: raise to trigger retry path; second call: succeed.
        if not is_retry:
            # Mimic recursive retry: outer rpc_call would call _try_refresh
            # which calls rpc_call(_is_retry=True). Use our own ClientCore
            # instance's rpc_call directly.
            return await core.rpc_call(method, params, source_path, allow_null, _is_retry=True)
        return "ok"

    core = ClientCore.__new__(ClientCore)
    core._http_client = object()  # not-None; rpc_call doesn't dereference here
    core._rpc_call_impl = fake_impl  # type: ignore[method-assign]

    result = await core.rpc_call(method=object(), params=[])  # type: ignore[arg-type]
    assert result == "ok"
    assert len(captured_ids) == 2
    assert captured_ids[0] == captured_ids[1]
    assert captured_ids[0] is not None


def test_end_to_end_prefix_visible_in_rendered_output():
    """When wired to a real handler, the rendered output has [req=...] prefix."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.NOTSET)
    handler.setFormatter(RedactingFormatter(logging.Formatter("%(message)s")))
    handler.addFilter(RedactingFilter())

    logger = logging.getLogger("test_e2e_corr_unique")
    logger.handlers.clear()
    logger.filters.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False

    token = set_request_id("E2EE0001")
    try:
        logger.warning("hello with at=SECRET")
    finally:
        reset_request_id(token)

    out = buf.getvalue()
    assert "[req=E2EE0001]" in out
    assert "SECRET" not in out
    assert "at=***" in out

    logger.handlers.clear()
    logger.filters.clear()
