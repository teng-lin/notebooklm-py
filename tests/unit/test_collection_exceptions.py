"""Tests for the collection exception classes."""

from __future__ import annotations

import pytest

from notebooklm.exceptions import (
    CollectionError,
    CollectionNotFoundError,
    NotebookLMError,
    NotFoundError,
    RPCError,
)


def test_collection_error_is_base() -> None:
    assert issubclass(CollectionError, NotebookLMError)


def test_collection_not_found_mro() -> None:
    assert issubclass(CollectionNotFoundError, NotFoundError)
    assert issubclass(CollectionNotFoundError, RPCError)
    assert issubclass(CollectionNotFoundError, CollectionError)


def test_collection_not_found_carries_id_and_method() -> None:
    exc = CollectionNotFoundError("coll_1", method_id="I3xc3c")
    assert exc.collection_id == "coll_1"
    assert exc.method_id == "I3xc3c"
    assert "coll_1" in str(exc)


def test_collection_not_found_catchable_as_each_base() -> None:
    for base in (NotFoundError, RPCError, CollectionError, NotebookLMError):
        with pytest.raises(base):
            raise CollectionNotFoundError("coll_1")
