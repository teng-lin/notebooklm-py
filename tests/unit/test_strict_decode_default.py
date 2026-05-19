"""Tests for the strict-decode default flip (PR 13.9a, ADR-011).

``_env.is_strict_decode_enabled`` defaults to ``True`` once the
``NOTEBOOKLM_STRICT_DECODE`` env-var rollout completes. These tests pin the
new default along with the env-var override path so a future regression that
re-flips the default to ``"0"`` is caught immediately.

Coverage:

* Default-on: with ``NOTEBOOKLM_STRICT_DECODE`` unset, the helper returns
  ``True`` and :func:`safe_index` raises
  :class:`~notebooklm.exceptions.UnknownRPCMethodError` on descent failure.
* Opt-out: explicit ``NOTEBOOKLM_STRICT_DECODE=0`` (and other falsy values
  documented in the env-var docstring) returns ``False`` and ``safe_index``
  warns-and-returns-``None``.
* Truthy aliases: ``"true"`` / ``"True"`` continue to enable strict mode
  alongside ``"1"``.

Existing soft-mode coverage (``test_safe_index.py``, ``test_artifacts_drift.py``)
pins the SOFT-mode contract with explicit ``setenv("0")``; this file pins
that *unset* itself is now the strict-mode signal.
"""

from __future__ import annotations

import logging

import pytest

from notebooklm._env import STRICT_DECODE_ENV, is_strict_decode_enabled
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc._safe_index import safe_index


def test_default_is_strict_when_env_unset(monkeypatch):
    """With ``NOTEBOOKLM_STRICT_DECODE`` unset, the helper reports strict mode.

    Post-PR 13.9a the soft-rollout window closed: an absent env-var means
    "raise on drift," matching the production-fail-fast default that ADR-011
    pins.
    """
    monkeypatch.delenv(STRICT_DECODE_ENV, raising=False)
    assert is_strict_decode_enabled() is True


def test_safe_index_raises_by_default_when_env_unset(monkeypatch):
    """End-to-end: ``safe_index`` raises ``UnknownRPCMethodError`` by default.

    Pre-flip the same input returned ``None`` and warn-logged. This test is
    the behavioural contract for downstream callers that wrap ``safe_index``
    and now must handle the typed exception path on shape drift.
    """
    monkeypatch.delenv(STRICT_DECODE_ENV, raising=False)
    with pytest.raises(UnknownRPCMethodError) as exc_info:
        safe_index([], 0, method_id="abc", source="test.default_strict")
    err = exc_info.value
    assert err.method_id == "abc"
    assert err.source == "test.default_strict"


def test_opt_out_with_zero_returns_soft_mode(monkeypatch, caplog):
    """``NOTEBOOKLM_STRICT_DECODE=0`` is the documented opt-out path.

    Restores the legacy warn-and-return-``None`` behavior for one release
    window so downstream code that relies on the soft-mode sentinel can
    migrate at its own pace before the env var is retired.
    """
    monkeypatch.setenv(STRICT_DECODE_ENV, "0")
    assert is_strict_decode_enabled() is False

    with (
        caplog.at_level(logging.WARNING, logger="notebooklm.rpc._safe_index"),
        pytest.warns(DeprecationWarning, match="NOTEBOOKLM_STRICT_DECODE=0"),
    ):
        result = safe_index([], 0, method_id="abc", source="test.opt_out")

    assert result is None
    assert any("safe_index drift" in record.message for record in caplog.records)


@pytest.mark.parametrize("value", ["", "false", "no", "off", "0", "False"])
def test_falsy_values_disable_strict_mode(monkeypatch, value):
    """Anything not in the truthy set leaves strict mode disabled.

    Mirrors the existing soft-value coverage in ``test_safe_index.py`` but
    framed against the new default — these env values restore the soft path.
    """
    monkeypatch.setenv(STRICT_DECODE_ENV, value)
    assert is_strict_decode_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "True"])
def test_truthy_aliases_enable_strict_mode(monkeypatch, value):
    """Documented truthy aliases continue to enable strict mode explicitly."""
    monkeypatch.setenv(STRICT_DECODE_ENV, value)
    assert is_strict_decode_enabled() is True
