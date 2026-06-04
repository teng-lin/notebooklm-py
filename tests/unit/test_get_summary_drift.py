"""Strict-mode coverage for ``NotebooksAPI.get_summary``.

The site at ``_notebooks.py:get_summary`` used to swallow ``IndexError`` /
``TypeError`` from an unguarded ``result[0][0][0]`` descent. It was migrated
to ``safe_index`` so drift raises ``UnknownRPCMethodError`` carrying
``method_id=RPCMethod.SUMMARIZE.value`` and ``source='_notebooks.get_summary'``
for debuggability. Strict decoding is the only mode — the legacy
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
    from _fixtures.fake_core import make_fake_core

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
    """Drift raises ``UnknownRPCMethodError`` with context (the only mode)."""
    # result[0] is an empty list → result[0][0] raises IndexError.
    api = _make_api([[]])

    with pytest.raises(UnknownRPCMethodError) as exc_info:
        await api.get_summary("nb_drift")

    err = exc_info.value
    assert err.method_id == RPCMethod.SUMMARIZE.value
    assert err.source == "_notebooks.get_summary"
    # Descent succeeded for result[0]; failure landed at the next hop.
    assert err.path == (0,)
    assert err.data_at_failure is not None


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
