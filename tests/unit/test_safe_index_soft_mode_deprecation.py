"""Deprecation-warning coverage for ``safe_index`` soft mode."""

from __future__ import annotations

import logging
import warnings

import pytest

from notebooklm._env import STRICT_DECODE_ENV
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc._safe_index import safe_index


def test_soft_mode_drift_emits_deprecation_warning_and_logs(monkeypatch, caplog):
    """Explicit soft mode still returns ``None`` but now warns loudly."""
    monkeypatch.setenv(STRICT_DECODE_ENV, "0")

    with (
        caplog.at_level(logging.WARNING, logger="notebooklm.rpc._safe_index"),
        pytest.warns(
            DeprecationWarning,
            match=r"NOTEBOOKLM_STRICT_DECODE=0.*test\.soft.*v0\.6\.0",
        ),
    ):
        result = safe_index([], 0, method_id="abc", source="test.soft")

    assert result is None
    assert any("safe_index drift" in record.message for record in caplog.records)


def test_soft_mode_success_does_not_emit_deprecation_warning(monkeypatch):
    """The deprecation warning is tied to fallback use, not env-var presence."""
    monkeypatch.setenv(STRICT_DECODE_ENV, "0")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = safe_index(["leaf"], 0, method_id="abc", source="test.success")

    assert result == "leaf"
    assert not [warning for warning in caught if issubclass(warning.category, DeprecationWarning)]


def test_strict_mode_drift_keeps_raising_without_deprecation_warning(monkeypatch):
    """Strict mode must not fall through to the soft-mode warning branch."""
    monkeypatch.setenv(STRICT_DECODE_ENV, "1")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(UnknownRPCMethodError):
            safe_index([], 0, method_id="abc", source="test.strict")

    assert not [warning for warning in caught if issubclass(warning.category, DeprecationWarning)]
