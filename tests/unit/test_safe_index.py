"""Tests for ``notebooklm.rpc._safe_index.safe_index``.

Covers happy descent, strict-mode raise (the only mode since the
``NOTEBOOKLM_STRICT_DECODE=0`` soft-mode opt-out was retired in v0.7.0), and
backward-compat exception hierarchy (``except RPCError`` catches strict-mode
errors).
"""

from __future__ import annotations

import warnings

import pytest

import notebooklm.rpc as rpc_pkg
from notebooklm.exceptions import (
    DecodingError,
    RPCError,
    UnknownRPCMethodError,
)
from notebooklm.rpc._safe_index import safe_index
from notebooklm.rpc.decoder import safe_index as safe_index_via_decoder


def test_safe_index_helper_is_reexported_via_decoder():
    """Helper is importable from both _safe_index and decoder modules."""
    assert safe_index is safe_index_via_decoder
    # Also pinned through the rpc package namespace so all three import paths
    # resolve to the same object.
    assert rpc_pkg.safe_index is safe_index


def test_happy_three_level_descent_returns_leaf():
    data = [[["leaf"]]]
    result = safe_index(data, 0, 0, 0, method_id="abc", source="test")
    assert result == "leaf"


def test_descent_with_no_path_returns_root():
    data = ["root"]
    result = safe_index(data, method_id="abc", source="test")
    assert result == ["root"]


def test_drift_outer_strict_mode_raises_with_attributes(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
    data = ["only-one"]
    with pytest.raises(UnknownRPCMethodError) as exc_info:
        safe_index(data, 5, method_id="abc-method", source="test.outer")
    err = exc_info.value
    assert err.method_id == "abc-method"
    assert err.source == "test.outer"
    assert err.path == ()
    assert err.data_at_failure is not None
    assert "only-one" in err.data_at_failure


def test_drift_strict_mode_chains_original_exception(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
    data = ["only-one"]
    with pytest.raises(UnknownRPCMethodError) as exc_info:
        safe_index(data, 5, method_id="abc", source="test")
    assert isinstance(exc_info.value.__cause__, IndexError)


def test_descending_into_none_is_caught_and_rerouted(monkeypatch):
    """``data[0]`` returning None then descending again triggers TypeError."""
    monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
    data = [None]
    with pytest.raises(UnknownRPCMethodError) as exc_info:
        safe_index(data, 0, 0, method_id="abc", source="test.none")
    err = exc_info.value
    assert err.path == (0,)
    assert isinstance(err.__cause__, TypeError)


def test_descending_into_int_is_caught_and_rerouted(monkeypatch):
    """Indexing an int raises TypeError, which safe_index catches."""
    monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
    data = [42]
    with pytest.raises(UnknownRPCMethodError) as exc_info:
        safe_index(data, 0, 0, method_id="abc", source="test.int")
    assert isinstance(exc_info.value.__cause__, TypeError)


def test_descending_into_str_raises_drift():
    """A str at an intermediate hop is drift, NOT a silent char index.

    ``"abc"[0] == "a"`` — a string is indexable but is never a valid container
    at an intermediate descent hop in a decoded RPC payload. Descending it would
    smuggle a bogus 1-char "value" past drift detection. safe_index must reject
    it as drift instead (regression for #1485 codex review).
    """
    with pytest.raises(UnknownRPCMethodError) as exc_info:
        safe_index(["abc"], 0, 0, method_id="abc", source="test.str")
    err = exc_info.value
    # hop 0 descends ["abc"][0] -> "abc" (a str); hop 1 ([0]) is rejected, so
    # the truncated path stops at (0,).
    assert err.path == (0,)
    assert err.source == "test.str"
    assert err.data_at_failure is not None
    assert "abc" in err.data_at_failure


def test_descending_into_top_level_str_raises_drift():
    """A str passed directly as ``data`` (descended at hop 0) is drift."""
    with pytest.raises(UnknownRPCMethodError) as exc_info:
        safe_index("abc", 0, method_id="abc", source="test.top_str")
    assert exc_info.value.path == ()


def test_descending_into_bytes_raises_drift():
    """bytes is also indexable-but-not-a-container; reject it as drift too."""
    with pytest.raises(UnknownRPCMethodError):
        safe_index([b"abc"], 0, 0, method_id="abc", source="test.bytes")


def test_str_leaf_value_is_returned_not_rejected():
    """A str as the *final* leaf is fine — only intermediate hops are checked."""
    assert safe_index([["leaf"]], 0, 0, method_id="abc", source="test.leaf") == "leaf"
    # And a bare string with no descent is returned untouched.
    assert safe_index("leaf", method_id="abc", source="test.no_descent") == "leaf"


def test_strict_mode_exception_is_catchable_as_rpc_error(monkeypatch):
    """Backward compat: ``except RPCError`` still catches strict-mode raise."""
    monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
    data = []
    try:
        safe_index(data, 0, method_id="abc", source="test")
    except RPCError as e:
        assert isinstance(e, UnknownRPCMethodError)
        assert isinstance(e, DecodingError)
    else:
        pytest.fail("Expected RPCError to be raised")


def test_strict_mode_truthy_values(monkeypatch):
    """``true`` and ``True`` should also enable strict mode."""
    for value in ("1", "true", "True"):
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", value)
        with pytest.raises(UnknownRPCMethodError):
            safe_index([], 0, method_id="abc", source="test")


def test_legacy_falsy_env_values_are_a_no_op(monkeypatch):
    """The retired ``NOTEBOOKLM_STRICT_DECODE`` opt-out no longer softens decoding.

    Formerly ``"0"`` / ``"no"`` / ``"false"`` / ``""`` restored
    warn-and-return-``None``; that soft-mode path was removed in v0.7.0, so
    every value now still raises on drift.
    """
    for value in ("0", "no", "false", ""):
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", value)
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with pytest.raises(UnknownRPCMethodError):
                safe_index([], 0, method_id="abc", source="test")


def test_data_at_failure_is_truncated(monkeypatch):
    """Repr is truncated to ~200 chars to avoid blowing up logs/exceptions."""
    monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
    huge = [["x" * 5000]]
    with pytest.raises(UnknownRPCMethodError) as exc_info:
        safe_index(huge, 0, 99, method_id="abc", source="test")
    assert exc_info.value.data_at_failure is not None
    assert len(exc_info.value.data_at_failure) <= 210  # 200 + ellipsis margin


def test_unknown_rpc_method_error_truncates_string_raw_response(monkeypatch):
    """Regression: ``UnknownRPCMethodError`` must honor RPCError's truncation cap.

    Previously the subclass unconditionally reassigned ``self.raw_response``
    after the base class truncated, bypassing the contract. The preview is now
    capped at 80 chars + "..." (NOTEBOOKLM_DEBUG=1 opts into the full body).
    """
    monkeypatch.delenv("NOTEBOOKLM_DEBUG", raising=False)
    huge = "x" * 5000
    err = UnknownRPCMethodError("boom", raw_response=huge)
    assert err.raw_response is not None
    assert isinstance(err.raw_response, str)
    assert len(err.raw_response) == 83
    assert err.raw_response.endswith("...")


def test_unknown_rpc_method_error_preserves_non_string_raw_response():
    """Non-string raw_response (dict/list) is preserved as-is.

    The subclass widens the type to ``Any`` to support structured payloads;
    truncation only applies to strings.
    """
    payload = {"chunk": ["a", "b"], "meta": {"k": "v"}}
    err = UnknownRPCMethodError("boom", raw_response=payload)
    assert err.raw_response is payload
