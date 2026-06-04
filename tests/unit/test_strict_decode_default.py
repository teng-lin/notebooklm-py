"""Tests pinning strict-only decoding (ADR-0011).

The ``NOTEBOOKLM_STRICT_DECODE=0`` soft-mode opt-out was retired in v0.7.0
(see ``docs/deprecations.md``). Strict decoding is now the only mode:
:func:`safe_index` raises :class:`~notebooklm.exceptions.UnknownRPCMethodError`
on descent failure regardless of the (now-ignored) env var.

These tests guard against a regression that reintroduces a soft-mode
fallback or makes the retired env var change behavior again.
"""

from __future__ import annotations

import warnings

import pytest

from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc._safe_index import safe_index

_STRICT_DECODE_ENV = "NOTEBOOKLM_STRICT_DECODE"


def test_safe_index_raises_on_drift_when_env_unset(monkeypatch):
    """With the env var unset, ``safe_index`` raises on descent failure."""
    monkeypatch.delenv(_STRICT_DECODE_ENV, raising=False)
    with pytest.raises(UnknownRPCMethodError) as exc_info:
        safe_index([], 0, method_id="abc", source="test.default_strict")
    err = exc_info.value
    assert err.method_id == "abc"
    assert err.source == "test.default_strict"


@pytest.mark.parametrize("value", ["0", "", "false", "False", "no", "off", "1", "true"])
def test_retired_env_var_is_a_no_op(monkeypatch, value):
    """``NOTEBOOKLM_STRICT_DECODE`` is ignored — drift always raises.

    The legacy soft-mode opt-out was retired in v0.7.0, so no value of the
    env var (including the old ``"0"`` opt-out) restores warn-and-return-``None``.
    """
    monkeypatch.setenv(_STRICT_DECODE_ENV, value)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with pytest.raises(UnknownRPCMethodError):
            safe_index([], 0, method_id="abc", source="test.no_op")


def test_safe_index_success_returns_value(monkeypatch):
    """A valid descent returns the value with no warning."""
    monkeypatch.setenv(_STRICT_DECODE_ENV, "0")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert safe_index(["leaf"], 0, method_id="abc", source="test.success") == "leaf"
