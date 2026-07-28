"""The upload widget warns (not silently breaks) when stateless HTTP is disabled (#1915).

``FASTMCP_STATELESS_HTTP`` set explicitly falsey while the widget is on suppresses the
auto-enable, so the widget registers but cannot render. ``_resolve_stateless_http`` must
warn loudly in that case, auto-enable when the var is unset, and stay quiet otherwise.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("fastmcp")

from notebooklm.mcp.__main__ import _resolve_stateless_http  # noqa: E402

_WIDGET = "NOTEBOOKLM_MCP_UPLOAD_WIDGET"
_STATELESS = "FASTMCP_STATELESS_HTTP"


@pytest.mark.parametrize("falsey", ["false", "False", "0", "f", "no", "n", "off", "OFF"])
def test_widget_with_explicit_falsey_stateless_warns(monkeypatch, caplog, falsey: str) -> None:
    monkeypatch.setenv(_WIDGET, "1")
    monkeypatch.setenv(_STATELESS, falsey)
    with caplog.at_level(logging.WARNING):
        result = _resolve_stateless_http()
    # Honor the operator's explicit choice (do not force-enable) — just warn.
    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, f"expected a warning for {_STATELESS}={falsey!r}"
    assert "cannot render" in warnings[0].getMessage().lower()


def test_widget_with_stateless_unset_auto_enables(monkeypatch, caplog) -> None:
    monkeypatch.setenv(_WIDGET, "1")
    monkeypatch.delenv(_STATELESS, raising=False)
    with caplog.at_level(logging.INFO, logger="notebooklm.mcp.__main__"):
        result = _resolve_stateless_http()
    assert result is True  # auto-enabled for the widget
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert infos and "stateless" in infos[0].getMessage().lower()


def test_widget_with_unparseable_stateless_stays_quiet(monkeypatch, caplog) -> None:
    # A non-falsey/non-true value is neither auto-enabled nor warned — FastMCP itself
    # rejects it at import. The helper just falls through to None.
    monkeypatch.setenv(_WIDGET, "1")
    monkeypatch.setenv(_STATELESS, "maybe")
    with caplog.at_level(logging.WARNING):
        result = _resolve_stateless_http()
    assert result is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_widget_with_stateless_true_is_quiet(monkeypatch, caplog) -> None:
    monkeypatch.setenv(_WIDGET, "1")
    monkeypatch.setenv(_STATELESS, "true")
    with caplog.at_level(logging.WARNING):
        result = _resolve_stateless_http()
    assert result is None  # FastMCP reads the (true) value itself
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_no_widget_never_warns(monkeypatch, caplog) -> None:
    monkeypatch.delenv(_WIDGET, raising=False)
    monkeypatch.setenv(_STATELESS, "false")
    with caplog.at_level(logging.WARNING):
        result = _resolve_stateless_http()
    assert result is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
