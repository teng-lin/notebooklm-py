"""Tests for the source-label exception classes."""

from __future__ import annotations

import pytest

from notebooklm.exceptions import (
    LabelError,
    LabelNotFoundError,
    NotebookLMError,
    NotFoundError,
    RPCError,
)


def test_label_error_is_base() -> None:
    assert issubclass(LabelError, NotebookLMError)


def test_label_not_found_mro() -> None:
    assert issubclass(LabelNotFoundError, NotFoundError)
    assert issubclass(LabelNotFoundError, RPCError)
    assert issubclass(LabelNotFoundError, LabelError)


def test_label_not_found_carries_id_and_method() -> None:
    exc = LabelNotFoundError("label_1", method_id="I3xc3c")
    assert exc.label_id == "label_1"
    assert exc.method_id == "I3xc3c"
    assert "label_1" in str(exc)


def test_label_not_found_catchable_as_each_base() -> None:
    for base in (NotFoundError, RPCError, LabelError, NotebookLMError):
        with pytest.raises(base):
            raise LabelNotFoundError("label_1")
