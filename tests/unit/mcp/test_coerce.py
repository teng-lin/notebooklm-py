"""Unit tests for the MCP ``coerce_list`` list-param normalizer.

Covers every input shape an MCP client / LLM tool-caller might send a list param
as: a real list/tuple, a JSON-array string, a comma-separated string, a bare
scalar, empty, and ``None`` (the contract-critical "stays None" case).
"""

from __future__ import annotations

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent: importing
# ``notebooklm.mcp._coerce`` loads ``notebooklm.mcp.__init__`` -> fastmcp.
pytest.importorskip("fastmcp")

from notebooklm.mcp._coerce import coerce_list  # noqa: E402 - after importorskip guard


def test_none_stays_none() -> None:
    """The contract-critical case: None must NOT become [] (None => "all sources")."""
    assert coerce_list(None) is None


def test_real_list_is_stringified() -> None:
    assert coerce_list(["a", "b"]) == ["a", "b"]


def test_real_tuple_is_stringified() -> None:
    assert coerce_list(("a", "b")) == ["a", "b"]


def test_list_elements_are_stringified() -> None:
    assert coerce_list([1, 2]) == ["1", "2"]


def test_json_array_string() -> None:
    assert coerce_list('["a","b"]') == ["a", "b"]


def test_json_array_string_single() -> None:
    assert coerce_list('["a"]') == ["a"]


def test_json_empty_array_string() -> None:
    assert coerce_list("[]") == []


def test_json_array_non_string_elements_stringified() -> None:
    assert coerce_list("[1, 2]") == ["1", "2"]


def test_comma_string() -> None:
    assert coerce_list("a,b") == ["a", "b"]


def test_comma_string_strips_and_drops_empties() -> None:
    assert coerce_list("a, b ,") == ["a", "b"]


def test_scalar_string() -> None:
    assert coerce_list("a") == ["a"]


def test_empty_string_is_empty_list() -> None:
    assert coerce_list("") == []


def test_whitespace_string_is_empty_list() -> None:
    assert coerce_list("   ") == []


def test_non_string_scalar_is_stringified() -> None:
    """Defensive direct-call behavior (a non-string scalar is rejected by Pydantic
    before reaching a tool typed ``list[str] | str | None``)."""
    assert coerce_list(5) == ["5"]


def test_bracketed_unquoted_string_strips_brackets() -> None:
    """A bracketed-but-unquoted "[a, b]" (not valid JSON) has its matched outer
    brackets stripped before comma-split, yielding clean items."""
    assert coerce_list("[a, b]") == ["a", "b"]


def test_bracketed_unquoted_single() -> None:
    assert coerce_list("[a]") == ["a"]


def test_unbalanced_open_bracket_strips_leading_bracket() -> None:
    """An unbalanced "[a,b" (no closing bracket) still has its stray leading "["
    stripped so it can't leak into a resolved id."""
    assert coerce_list("[a,b") == ["a", "b"]
