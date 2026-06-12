"""Unit tests for the artifact-envelope unwrap helpers (issue #1491 burndown).

These cover the two functions that centralise the positional envelope/leaf
descent the ``_artifact``/``_artifacts`` decode sites previously open-coded:

* :func:`unwrap_artifact_rows` — the ``[[row, ...]]`` envelope probe shared by
  ``ArtifactListingService.list_raw`` (``LIST_ARTIFACTS``) and
  ``ArtifactsAPI.suggest_reports`` (``GET_SUGGESTED_REPORTS``); and
* :func:`unwrap_mind_map_generation_leaf` — the two-level ``result[0][0]`` leaf
  descent of a ``GENERATE_MIND_MAP`` reply.

Each table proves the helper reproduces the EXACT result of the prior inline
reads on present/happy, empty, too-short, and malformed inputs (the soft-vs-
strict semantics the migration had to preserve).
"""

from __future__ import annotations

from typing import Any

import pytest

from notebooklm._row_adapters.artifacts import (
    MIND_MAP_LEAF_ABSENT,
    unwrap_artifact_rows,
    unwrap_mind_map_generation_leaf,
)
from notebooklm.rpc.types import RPCMethod

_LIST_METHOD = RPCMethod.LIST_ARTIFACTS.value


def _unwrap(result: list[Any]) -> list[Any]:
    return unwrap_artifact_rows(result, method_id=_LIST_METHOD, source="test")


class TestUnwrapArtifactRows:
    """``unwrap_artifact_rows`` reproduces the inline ``[[row,...]]`` probe."""

    def test_wrapped_envelope_returns_inner_rows(self) -> None:
        """A single-element envelope whose inner head is a row unwraps."""
        rows = [["id1", "Title1"], ["id2", "Title2"]]
        assert _unwrap([rows]) == rows

    def test_wrapped_empty_inner_returns_empty_inner(self) -> None:
        """A single outer element wrapping an empty inner list unwraps to ``[]``."""
        # ``not inner`` short-circuits before the inner-head probe — the
        # legitimately-empty notebook shape.
        inner: list[Any] = []
        assert _unwrap([inner]) is inner

    def test_already_flat_rows_returned_unchanged(self) -> None:
        """A flat list of >1 rows is not single-element, so it is returned as-is."""
        flat = [["id1"], ["id2"]]
        assert _unwrap(flat) is flat

    def test_single_flat_row_with_scalar_head_returned_unchanged(self) -> None:
        """A single outer element whose inner head is a SCALAR is a flat row.

        ``[[scalar, ...]]`` is the already-flat ``[row]`` shape (one row whose
        first field is a scalar), not a wrapped envelope — so it must NOT be
        unwrapped. Mirrors the prior ``isinstance(inner[0], list)`` guard.
        """
        flat = [["id-as-scalar-head", 2, 3]]
        assert _unwrap(flat) is flat

    def test_single_element_non_list_returned_unchanged(self) -> None:
        """A single outer element that is not a list is left flat (caller's row)."""
        flat = ["scalar-row"]
        assert _unwrap(flat) is flat

    def test_empty_outer_returned_unchanged(self) -> None:
        """An empty outer list (len != 1) is returned unchanged (== ``[]``)."""
        outer: list[Any] = []
        assert _unwrap(outer) is outer

    def test_matches_legacy_inline_logic_across_shapes(self) -> None:
        """Differential check against the exact pre-migration inline algorithm."""

        def legacy(result: list[Any]) -> list[Any]:
            items = result
            if len(result) == 1 and isinstance(result[0], list):
                inner = result[0]
                if not inner or isinstance(inner[0], list):
                    items = inner
            return items

        shapes: list[list[Any]] = [
            [[["a"], ["b"]]],  # wrapped
            [[]],  # wrapped empty inner
            [["scalar", 1]],  # flat single row (scalar head)
            [["a"], ["b"]],  # flat multi-row
            ["scalar"],  # single non-list element
            [],  # empty
            [[["only-row"]]],  # wrapped single row
        ]
        for shape in shapes:
            assert _unwrap(shape) == legacy(shape), shape


def _leaf(result: Any) -> Any:
    return unwrap_mind_map_generation_leaf(
        result, method_id=RPCMethod.GENERATE_MIND_MAP.value, source="test"
    )


class TestUnwrapMindMapGenerationLeaf:
    """``unwrap_mind_map_generation_leaf`` reproduces the ``result[0][0]`` descent."""

    def test_happy_returns_present_leaf(self) -> None:
        assert _leaf([['{"name": "Tree"}']]) == '{"name": "Tree"}'

    def test_present_none_leaf_is_returned_not_treated_as_absent(self) -> None:
        """A PRESENT ``None`` leaf must be returned verbatim, not the sentinel.

        The historical code processes a ``None`` leaf (it serialises to a
        ``"null"`` note body), so the helper must distinguish it from the
        absence shapes via the sentinel.
        """
        result = _leaf([[None]])
        assert result is None
        assert result is not MIND_MAP_LEAF_ABSENT

    def test_present_empty_string_leaf_returned(self) -> None:
        result = _leaf([[""]])
        assert result == ""
        assert result is not MIND_MAP_LEAF_ABSENT

    @pytest.mark.parametrize(
        "result",
        [
            None,  # null payload
            [],  # empty outer
            "scalar",  # non-list payload
            0,  # falsy non-list
            [[]],  # present outer, empty inner
            [None],  # present outer, non-list inner
            ["scalar-inner"],  # present outer, non-list (str) inner
        ],
    )
    def test_absence_shapes_return_sentinel(self, result: Any) -> None:
        """Short / non-list outer or inner returns the absence sentinel (soft)."""
        assert _leaf(result) is MIND_MAP_LEAF_ABSENT

    def test_matches_legacy_inline_logic(self) -> None:
        """Differential check: sentinel iff the legacy nested guard skipped."""

        def legacy_found(result: Any) -> tuple[bool, Any]:
            if result and isinstance(result, list) and len(result) > 0:
                inner = result[0]
                if isinstance(inner, list) and len(inner) > 0:
                    return True, inner[0]
            return False, None

        shapes: list[Any] = [
            [["leaf"]],
            [[None]],
            [[""]],
            [[123]],
            [[]],
            [None],
            [],
            None,
            "x",
            [["a", "b"]],  # inner len > 1 — head only
        ]
        for shape in shapes:
            found, expected_leaf = legacy_found(shape)
            actual = _leaf(shape)
            if found:
                assert actual == expected_leaf, shape
                assert actual is not MIND_MAP_LEAF_ABSENT, shape
            else:
                assert actual is MIND_MAP_LEAF_ABSENT, shape
