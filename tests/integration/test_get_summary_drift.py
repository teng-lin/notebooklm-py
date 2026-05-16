"""Strict-mode coverage for ``NotebooksAPI.get_summary``.

The site at ``_notebooks.py:get_summary`` used to swallow ``IndexError`` /
``TypeError`` from an unguarded ``result[0][0][0]`` descent. It was migrated
to ``safe_index`` so:

* In the default soft rollout (``NOTEBOOKLM_STRICT_DECODE`` unset), drift
  warn-logs and the method still returns ``""`` — preserving legacy semantics
  for the single caller at ``cli/notebook.py``.
* With ``NOTEBOOKLM_STRICT_DECODE=1``, drift raises
  ``UnknownRPCMethodError`` carrying ``method_id=RPCMethod.SUMMARIZE.value``
  and ``source='_notebooks.get_summary'`` for debuggability.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._notebooks import NotebooksAPI
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc import RPCMethod

# mock-only drift tests; no HTTP, no cassette. Opt out of the
# tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


def _make_api(rpc_return):
    api = NotebooksAPI.__new__(NotebooksAPI)
    core = MagicMock()
    core.rpc_call = AsyncMock(return_value=rpc_return)
    api._core = core
    return api


@pytest.mark.asyncio
async def test_get_summary_happy_path_returns_string(monkeypatch):
    """Well-formed response shape extracts the summary string."""
    monkeypatch.delenv("NOTEBOOKLM_STRICT_DECODE", raising=False)
    # Real shape: [[[summary_string, ...], topics, ...]]
    api = _make_api([[["the summary text"]]])

    summary = await api.get_summary("nb_happy")

    assert summary == "the summary text"


@pytest.mark.asyncio
async def test_get_summary_happy_path_also_holds_under_strict(monkeypatch):
    """Strict mode must not penalize valid payloads."""
    monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
    api = _make_api([[["the summary text"]]])

    summary = await api.get_summary("nb_happy_strict")

    assert summary == "the summary text"


@pytest.mark.asyncio
async def test_get_summary_drift_soft_mode_returns_empty_with_warning(monkeypatch, caplog):
    """Default soft mode: drift returns ``""`` and emits a warn-level log.

    Preserves legacy CLI behavior — ``description.summary`` truthiness check
    at ``cli/notebook.py:213`` continues to work without spurious errors.
    """
    monkeypatch.delenv("NOTEBOOKLM_STRICT_DECODE", raising=False)
    # result[0] is an empty list → result[0][0] raises IndexError.
    api = _make_api([[]])

    with caplog.at_level(logging.WARNING, logger="notebooklm"):
        summary = await api.get_summary("nb_drift_soft")

    assert summary == ""
    drift_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "safe_index drift" in r.message
    ]
    assert drift_warnings, (
        f"expected safe_index drift warning, got: {[r.message for r in caplog.records]}"
    )
    # The warning carries the call-site label so log readers can locate the
    # offending decoder without a stack trace.
    assert any("_notebooks.get_summary" in r.message for r in drift_warnings)


@pytest.mark.asyncio
async def test_get_summary_drift_strict_mode_raises_typed_error(monkeypatch):
    """Strict mode: drift raises ``UnknownRPCMethodError`` with context."""
    monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
    api = _make_api([[]])

    with pytest.raises(UnknownRPCMethodError) as exc_info:
        await api.get_summary("nb_drift_strict")

    err = exc_info.value
    assert err.method_id == RPCMethod.SUMMARIZE.value
    assert err.source == "_notebooks.get_summary"
    # Descent succeeded for result[0]; failure landed at the next hop.
    assert err.path == (0,)
    assert err.data_at_failure is not None


@pytest.mark.asyncio
async def test_get_summary_falsy_summary_returns_empty(monkeypatch):
    """A None/empty summary at the expected path returns ``""``.

    Distinguishes "drift" (shape mismatch) from "empty value" (valid shape,
    nothing to surface) — both produce ``""`` but only the former logs.
    """
    monkeypatch.delenv("NOTEBOOKLM_STRICT_DECODE", raising=False)
    api = _make_api([[[None]]])

    summary = await api.get_summary("nb_empty_value")

    assert summary == ""
