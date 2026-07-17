"""Unit tests for the transport-neutral source batch-add policy (``_app.source_batch``).

``batch_item_is_fatal`` decides whether a per-item add failure aborts the batch. It
is the shared classifier both the REST route and the MCP tool consult, so a
mis-bucketed category would silently change REST's already-green batch behavior with
no other failing test — hence this exhaustive all-14-category gate with an
INDEPENDENT expected table (not a re-import of ``_FATAL_CATEGORIES``).
"""

from __future__ import annotations

import pytest

from notebooklm import exceptions as exc
from notebooklm._app import SourceMutationError
from notebooklm._app.errors import ErrorCategory
from notebooklm._app.source_batch import MAX_BATCH_URLS, batch_item_is_fatal

# One exemplar exception per ErrorCategory (mirrors the classify-consistency gate).
_EXEMPLARS: list[tuple[ErrorCategory, BaseException]] = [
    (ErrorCategory.NOT_FOUND, exc.SourceNotFoundError("src_456")),
    (ErrorCategory.AUTH, exc.AuthError("auth failed")),
    (ErrorCategory.RATE_LIMITED, exc.RateLimitError("slow down", retry_after=5)),
    (ErrorCategory.VALIDATION, exc.ValidationError("bad input")),
    (ErrorCategory.CONFIG, exc.ConfigurationError("missing config")),
    (ErrorCategory.DEPENDENCY, exc.MissingDependencyError("markdownify not installed")),
    (ErrorCategory.NETWORK, exc.NetworkError("connection refused")),
    (ErrorCategory.NOTEBOOK_LIMIT, exc.NotebookLimitError(499, limit=500)),
    (ErrorCategory.ARTIFACT_TIMEOUT, exc.ArtifactTimeoutError("nb-1", "task-1", 30.0)),
    (ErrorCategory.TIMEOUT, exc.WaitTimeoutError("generic wait timed out")),
    (ErrorCategory.SERVER, exc.ServerError("upstream 503")),
    (ErrorCategory.RPC, exc.RPCError("decode failed", method_id="abc123")),
    (ErrorCategory.SOURCE_MUTATION, SourceMutationError("ambiguous", "AMBIGUOUS_ID")),
    (ErrorCategory.SOURCE_ADD, exc.SourceAddError("http://bad.example")),
    (ErrorCategory.LIBRARY, exc.NotebookLMError("some library error")),
    (ErrorCategory.UNEXPECTED, RuntimeError("boom")),
]

# INDEPENDENT oracle: fatal = the categories whose REST HTTP status is 401 / 429 /
# >=500 (a service/infra failure, not specific to one URL). Hand-written on purpose —
# do NOT derive this from `_FATAL_CATEGORIES` (that would make the test tautological).
_EXPECTED_FATAL: dict[ErrorCategory, bool] = {
    ErrorCategory.NOT_FOUND: False,  # 404
    ErrorCategory.AUTH: True,  # 401
    ErrorCategory.RATE_LIMITED: True,  # 429
    ErrorCategory.VALIDATION: False,  # 400
    ErrorCategory.CONFIG: True,  # 500
    ErrorCategory.DEPENDENCY: True,  # 500
    ErrorCategory.NETWORK: True,  # 502
    ErrorCategory.NOTEBOOK_LIMIT: False,  # 409
    ErrorCategory.ARTIFACT_TIMEOUT: True,  # 504
    ErrorCategory.TIMEOUT: True,  # 504
    ErrorCategory.SERVER: True,  # 502
    ErrorCategory.RPC: True,  # 502
    ErrorCategory.SOURCE_MUTATION: False,  # 422
    ErrorCategory.SOURCE_ADD: False,  # 422 — per-URL input failure isolates (#1905)
    ErrorCategory.LIBRARY: True,  # 500
    ErrorCategory.UNEXPECTED: True,  # 500
}


def test_expected_table_covers_every_category() -> None:
    """A new ErrorCategory without an expected verdict fails here (no silent gap)."""
    assert set(_EXPECTED_FATAL) == set(ErrorCategory)
    assert {category for category, _ in _EXEMPLARS} == set(ErrorCategory)


@pytest.mark.parametrize(
    ("category", "exception"),
    _EXEMPLARS,
    ids=[category.name for category, _ in _EXEMPLARS],
)
def test_batch_item_is_fatal_matches_expected(
    category: ErrorCategory, exception: BaseException
) -> None:
    assert batch_item_is_fatal(exception) is _EXPECTED_FATAL[category]


def test_max_batch_urls_is_positive() -> None:
    assert isinstance(MAX_BATCH_URLS, int) and MAX_BATCH_URLS > 0
