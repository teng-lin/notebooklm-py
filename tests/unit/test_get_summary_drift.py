"""Strict-mode coverage for ``NotebooksAPI.get_summary``.

The site at ``_notebooks.py:get_summary`` used to swallow ``IndexError`` /
``TypeError`` from an unguarded ``result[0][0][0]`` descent. It was migrated
to ``safe_index`` so drift raises ``UnknownRPCMethodError`` carrying
``method_id=RPCMethod.SUMMARIZE.value`` for debuggability. As of #1485 the
descent is delegated to the shared ``_extract_summary`` helper (single source
of truth with ``get_description``), so drift now surfaces with
``source='_notebooks._extract_summary'`` and the genuinely-absent shapes
(``result[0]`` None / empty / null summary slot) return ``""`` identically in
both entry points. Strict decoding is the only mode — the legacy
``NOTEBOOKLM_STRICT_DECODE=0`` soft-mode opt-out (which warn-logged and
returned ``""``) was retired in v0.7.0; see ADR-0011.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from notebooklm._notebooks import NotebooksAPI
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc import RPCMethod


def _make_api(rpc_return):
    from tests._fixtures.fake_core import make_fake_core

    api = NotebooksAPI.__new__(NotebooksAPI)
    core = make_fake_core(rpc_call=AsyncMock(return_value=rpc_return))
    api._rpc = core
    return api


@pytest.mark.asyncio
async def test_get_summary_happy_path_returns_string():
    """Well-formed response shape extracts the summary string."""
    # Real shape: [[[summary_string, ...], topics, ...]]
    api = _make_api([[["the summary text"]]])

    summary = await api.get_summary("nb_happy")

    assert summary == "the summary text"


@pytest.mark.asyncio
async def test_get_summary_drift_raises_typed_error():
    """Present-but-malformed payload raises ``UnknownRPCMethodError`` (the only mode)."""
    # result = [[[]]]: result[0] (== outer) is [[]] — present and non-None — but
    # its summary slot result[0][0] is an empty list, so the safe_index descent
    # into result[0][0][0] raises IndexError. A present-but-malformed inner
    # payload is genuine drift (distinct from a routinely-absent/None result[0]).
    api = _make_api([[[]]])

    with pytest.raises(UnknownRPCMethodError) as exc_info:
        await api.get_summary("nb_drift")

    err = exc_info.value
    assert err.method_id == RPCMethod.SUMMARIZE.value
    # The descent is delegated to _extract_summary, so the drift carries the
    # shared helper's source label (#1485).
    assert err.source == "_notebooks._extract_summary"
    assert err.data_at_failure is not None


@pytest.mark.asyncio
async def test_get_summary_scalar_result_zero_raises():
    """A scalar ``result[0]`` is present-but-malformed drift, not absence.

    Regression for #1485 codex review: the server returned ``result == [123]``,
    so ``result[0]`` is a bare int rather than the expected
    ``[summary_string, ...]`` list. This must raise drift — a
    ``not isinstance(..., list)`` short-circuit would wrongly suppress it to "".
    """
    api = _make_api([123])

    with pytest.raises(UnknownRPCMethodError) as exc_info:
        await api.get_summary("nb_scalar_drift")

    err = exc_info.value
    assert err.method_id == RPCMethod.SUMMARIZE.value
    assert err.source == "_notebooks._extract_summary"
    # The very first descent hop (result[0][0]) fails on the scalar, so the
    # truncated path is empty.
    assert err.path == ()


@pytest.mark.asyncio
async def test_get_summary_str_result_zero_raises():
    """A *string* ``result[0]`` raises drift, not a 1-char "summary".

    Regression for #1485 codex review (second round): the server returned
    ``result == ["abc"]``, so ``result[0]`` is a bare string. A string is
    indexable, so a naive descent would return ``"a"`` (``"abc"[0]``) and
    silently pass it off as the summary. safe_index now rejects intermediate
    str/bytes, so this surfaces as drift.
    """
    api = _make_api(["abc"])

    with pytest.raises(UnknownRPCMethodError) as exc_info:
        await api.get_summary("nb_str_drift")

    assert exc_info.value.source == "_notebooks._extract_summary"


@pytest.mark.asyncio
async def test_get_summary_falsy_summary_returns_empty():
    """A None/empty summary at the expected path returns ``""``.

    Distinguishes "drift" (shape mismatch, which raises) from "empty value"
    (valid shape, nothing to surface) — the latter descends successfully to
    ``None`` and returns ``""``.
    """
    api = _make_api([[[None]]])

    summary = await api.get_summary("nb_empty_value")

    assert summary == ""


@pytest.mark.asyncio
async def test_get_summary_empty_notebook_returns_empty():
    """A summary-less notebook (result[0] is None) returns "" not drift.

    Regression for #1485: a brand-new, source-less notebook has no summary
    yet, so the SUMMARIZE result[0] payload is None. That routine "no summary
    yet" state must surface as "" rather than ``UnknownRPCMethodError``.
    """
    api = _make_api([None])

    summary = await api.get_summary("nb_empty_notebook")

    assert summary == ""


@pytest.mark.asyncio
async def test_get_summary_empty_result_list_returns_empty():
    """An empty/absent outer result returns "" rather than raising.

    Regression for #1485: result being [] (no outer[0] slot at all) is the
    same routine "no summary yet" state and must not be mis-classified as
    wire-schema drift.
    """
    api = _make_api([])

    summary = await api.get_summary("nb_empty_result")

    assert summary == ""


@pytest.mark.asyncio
async def test_get_summary_null_summary_slot_returns_empty():
    """A null summary slot (result[0][0] is None) returns "" — consistency win.

    Regression for #1485 codex review: ``result == [[None]]`` means the
    ``outer`` payload is present but its summary slot is explicitly null. Before
    delegating to ``_extract_summary``, ``get_summary`` raised drift here while
    ``get_description`` returned "" for the same shape. Both now agree on "".
    """
    api = _make_api([[None]])

    summary = await api.get_summary("nb_null_slot")

    assert summary == ""


@pytest.mark.asyncio
async def test_get_summary_empty_outer_returns_empty():
    """An empty ``outer`` (result == [[]]) returns "" — consistency win.

    Regression for #1485 codex review: ``result == [[]]`` means ``result[0]``
    is an empty list (no summary slot at all) — the same routine "no summary
    yet" absence that ``_extract_summary`` already treated as "". ``get_summary``
    now delegates and agrees, rather than raising drift on an empty container.
    """
    api = _make_api([[]])

    summary = await api.get_summary("nb_empty_outer")

    assert summary == ""
