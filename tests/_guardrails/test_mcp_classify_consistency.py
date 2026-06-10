"""Consistency gate: the MCP error projection agrees with ``_app.errors.classify``.

Per ADR-0021, ``_app.errors.classify`` is the single neutral source of the
failure **category**; each adapter projects that category onto its own code
vocabulary. The MCP server's :data:`notebooklm.mcp._errors.CATEGORY_TABLE`
projects every category onto a manifest-pinned ``(code, hint)`` pair, and copies
``retriable`` verbatim from the classification.

This gate pins that projection: for a properly-constructed exemplar of **every**
:class:`~notebooklm._app.errors.ErrorCategory`, the MCP ``code`` actually emitted
by :func:`tool_error_payload` must equal the code this module maps that category
to, and ``retriable`` must equal the classification's. If someone adds an
exception (or a category) and updates only one ladder, this fails.

It mirrors ``tests/_guardrails/test_classify_error_handler_consistency.py`` (the
CLI side) so the two adapter ladders cannot silently drift from the neutral
classification.
"""

from __future__ import annotations

import pytest

# The canonical contributor install omits the `mcp` extra (no fastmcp). This
# guardrail imports ``notebooklm.mcp._errors`` (which imports fastmcp), so skip
# the whole module cleanly when the extra is absent rather than fail collection.
pytest.importorskip("fastmcp")

from notebooklm import exceptions as exc  # noqa: E402 - after importorskip guard
from notebooklm._app import SourceMutationError  # noqa: E402 - after importorskip guard
from notebooklm._app.errors import (  # noqa: E402 - after importorskip guard
    ErrorCategory,
    classify,
)
from notebooklm.mcp._errors import (  # noqa: E402 - after importorskip guard
    CATEGORY_TABLE,
    ERROR_CODES,
    tool_error_payload,
)

# The MCP code each neutral category projects onto. Distinct codes recover the
# category 1:1; the only collapse is LIBRARY -> ``ERROR`` (the catch-all).
# NOTE: this map is duplicated INTENTIONALLY from ``CATEGORY_TABLE`` (and from
# ``test_errors.py``) as an INDEPENDENT ORACLE — do NOT "DRY" it into a shared
# import. Hand-writing the expected projection here is what makes the gate able to
# catch a wrong edit to the production table; importing the table would make the
# test tautological.
_CATEGORY_TO_MCP_CODE: dict[ErrorCategory, str] = {
    ErrorCategory.NOT_FOUND: "NOT_FOUND",
    ErrorCategory.AUTH: "AUTH",
    ErrorCategory.RATE_LIMITED: "RATE_LIMITED",
    ErrorCategory.VALIDATION: "VALIDATION",
    ErrorCategory.CONFIG: "CONFIG",
    ErrorCategory.NETWORK: "NETWORK",
    ErrorCategory.NOTEBOOK_LIMIT: "NOTEBOOK_LIMIT",
    ErrorCategory.ARTIFACT_TIMEOUT: "ARTIFACT_TIMEOUT",
    ErrorCategory.TIMEOUT: "TIMEOUT",
    ErrorCategory.SERVER: "SERVER",
    ErrorCategory.RPC: "RPC",
    ErrorCategory.SOURCE_MUTATION: "SOURCE_MUTATION",
    ErrorCategory.LIBRARY: "ERROR",
    ErrorCategory.UNEXPECTED: "UNEXPECTED",
}

# One exemplar per category — the same exemplars the CLI consistency gate uses.
_EXEMPLARS: list[tuple[ErrorCategory, BaseException]] = [
    (ErrorCategory.NOT_FOUND, exc.SourceNotFoundError("src_456")),
    (ErrorCategory.AUTH, exc.AuthError("auth failed")),
    (ErrorCategory.RATE_LIMITED, exc.RateLimitError("slow down", retry_after=5)),
    (ErrorCategory.VALIDATION, exc.ValidationError("bad input")),
    (ErrorCategory.CONFIG, exc.ConfigurationError("missing config")),
    (ErrorCategory.NETWORK, exc.NetworkError("connection refused")),
    (ErrorCategory.NOTEBOOK_LIMIT, exc.NotebookLimitError(499, limit=500)),
    (ErrorCategory.ARTIFACT_TIMEOUT, exc.ArtifactTimeoutError("nb-1", "task-1", 30.0)),
    (ErrorCategory.TIMEOUT, exc.WaitTimeoutError("generic wait timed out")),
    (ErrorCategory.SERVER, exc.ServerError("upstream 503")),
    (ErrorCategory.RPC, exc.RPCError("decode failed", method_id="abc123")),
    (ErrorCategory.SOURCE_MUTATION, SourceMutationError("ambiguous", "AMBIGUOUS_ID")),
    (ErrorCategory.LIBRARY, exc.NotebookLMError("some library error")),
    (ErrorCategory.UNEXPECTED, RuntimeError("boom")),
]


def test_category_map_covers_every_category() -> None:
    """Every ``ErrorCategory`` has an MCP-code projection — a new category fails here."""
    assert set(_CATEGORY_TO_MCP_CODE) == set(ErrorCategory)
    assert set(CATEGORY_TABLE) == set(ErrorCategory)


def test_table_codes_match_the_expected_projection() -> None:
    """``CATEGORY_TABLE``'s codes equal this gate's expected projection 1:1."""
    assert {cat: code for cat, (code, _) in CATEGORY_TABLE.items()} == _CATEGORY_TO_MCP_CODE


def test_projected_codes_are_within_error_codes() -> None:
    """Every projected code is in the pinned ``ERROR_CODES`` set."""
    assert set(_CATEGORY_TO_MCP_CODE.values()) <= set(ERROR_CODES)


def test_one_exemplar_per_category() -> None:
    """Exactly one exemplar per category, so the parametrization is exhaustive."""
    assert {category for category, _ in _EXEMPLARS} == set(ErrorCategory)


@pytest.mark.parametrize(
    ("expected_category", "exception"),
    _EXEMPLARS,
    ids=[category.name for category, _ in _EXEMPLARS],
)
def test_mcp_code_matches_classify_category(
    expected_category: ErrorCategory, exception: BaseException
) -> None:
    # 1. classify lands the exemplar in the expected category.
    classified = classify(exception)
    assert classified.category is expected_category
    # 2. the MCP payload's emitted code is the projection of that category.
    payload = tool_error_payload(exception)
    assert payload["code"] == _CATEGORY_TO_MCP_CODE[expected_category]
    # 3. retriable is copied verbatim from the classification (never re-derived).
    assert payload["retriable"] is classified.retriable
