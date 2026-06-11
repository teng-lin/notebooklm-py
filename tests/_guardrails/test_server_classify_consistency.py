"""Consistency gate: the REST error projection agrees with ``_app.errors.classify``.

Per ADR-0021, ``_app.errors.classify`` is the single neutral source of the
failure **category**; each adapter projects that category onto its own vocabulary
(the CLI's exit codes, MCP's manifest codes, the REST server's HTTP status). The
server's :data:`notebooklm.server._errors.CATEGORY_STATUS` maps every category
onto exactly one status.

This gate pins that projection: every :class:`ErrorCategory` member has a status
(no gaps, no extras), and a properly-constructed exemplar of each category
classifies into it. It mirrors ``test_mcp_classify_consistency.py``; the
``CATEGORY_STATUS`` import pulls in fastapi, so the whole module is skipped
cleanly when the ``server`` extra is absent (the canonical CI install).
"""

from __future__ import annotations

import pytest

# The canonical contributor install omits the `server` extra (no fastapi). The
# server ``_errors`` module imports fastapi, so skip the whole module cleanly
# when the extra is absent rather than fail collection.
pytest.importorskip("fastapi")

from notebooklm import exceptions as exc  # noqa: E402 - after importorskip guard
from notebooklm._app import SourceMutationError  # noqa: E402 - after importorskip guard
from notebooklm._app.errors import (  # noqa: E402 - after importorskip guard
    ErrorCategory,
    classify,
)
from notebooklm.server._errors import CATEGORY_STATUS  # noqa: E402 - after importorskip guard

# The HTTP status each neutral category projects onto. Hand-written as an
# INDEPENDENT ORACLE — do NOT "DRY" it into a shared import from
# ``CATEGORY_STATUS``; importing the production table would make the gate
# tautological. Hand-mirroring is what lets it catch a wrong edit.
_CATEGORY_TO_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.AUTH: 401,
    ErrorCategory.RATE_LIMITED: 429,
    ErrorCategory.VALIDATION: 400,
    ErrorCategory.CONFIG: 500,
    ErrorCategory.NETWORK: 502,
    ErrorCategory.NOTEBOOK_LIMIT: 409,
    ErrorCategory.ARTIFACT_TIMEOUT: 504,
    ErrorCategory.TIMEOUT: 504,
    ErrorCategory.SERVER: 502,
    ErrorCategory.RPC: 502,
    ErrorCategory.SOURCE_MUTATION: 422,
    ErrorCategory.LIBRARY: 500,
    ErrorCategory.UNEXPECTED: 500,
}

# One exemplar per category — the same exemplars the CLI/MCP consistency gates use.
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


def test_status_map_covers_every_category() -> None:
    """Every ``ErrorCategory`` has a status — a new category fails here."""
    assert set(_CATEGORY_TO_STATUS) == set(ErrorCategory)
    assert set(CATEGORY_STATUS) == set(ErrorCategory)


def test_table_matches_the_expected_projection() -> None:
    """``CATEGORY_STATUS`` equals this gate's independent expected projection."""
    assert CATEGORY_STATUS == _CATEGORY_TO_STATUS


def test_one_exemplar_per_category() -> None:
    """Exactly one exemplar per category, so the parametrization is exhaustive."""
    assert {category for category, _ in _EXEMPLARS} == set(ErrorCategory)


@pytest.mark.parametrize(
    ("expected_category", "exception"),
    _EXEMPLARS,
    ids=[category.name for category, _ in _EXEMPLARS],
)
def test_status_matches_classify_category(
    expected_category: ErrorCategory, exception: BaseException
) -> None:
    classified = classify(exception)
    assert classified.category is expected_category
    assert CATEGORY_STATUS[classified.category] == _CATEGORY_TO_STATUS[expected_category]
