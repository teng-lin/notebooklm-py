"""Unit tests for the MCP structured-error projection.

``mcp/_errors.py`` translates a :class:`~notebooklm.exceptions.NotebookLMError`
into a FastMCP :class:`~fastmcp.exceptions.ToolError` carrying a structured
payload ``{code, message, retriable, hint?}``. The ``code``/``retriable``/
``hint`` are derived from ``_app.errors.classify`` via a
``category -> (code, hint)`` table; ``message`` is redaction-capped but the
``code`` + ``retriable`` are always preserved.

These tests pin, for an exemplar of EVERY ``ErrorCategory``, the projected
code + retriable + hint. The exemplar list mirrors
``tests/_guardrails/test_classify_error_handler_consistency.py`` so the two
ladders stay aligned.
"""

from __future__ import annotations

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm import exceptions as exc  # noqa: E402 - after importorskip guard
from notebooklm._app import SourceMutationError  # noqa: E402 - after importorskip guard
from notebooklm._app.errors import (  # noqa: E402 - after importorskip guard
    ErrorCategory,
    classify,
)
from notebooklm.mcp._errors import (  # noqa: E402 - after importorskip guard
    CATEGORY_TABLE,
    ERROR_CODES,
    mcp_errors,
    to_tool_error,
    tool_error_payload,
)

# One exemplar per category — same exemplars the CLI consistency gate uses.
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

# The MCP code each neutral category projects onto, and whether it is retriable.
# retriable mirrors ``_app.errors`` (rate-limit / server / timeout / network),
# never re-derived here.
# NOTE: this map is duplicated INTENTIONALLY from ``CATEGORY_TABLE`` (and from
# ``test_mcp_classify_consistency.py``) as an INDEPENDENT ORACLE — do NOT "DRY" it
# into a shared import. Hand-writing the expected projection is what lets the test
# catch a wrong edit to the production table; importing the table would make it
# tautological.
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


def test_table_covers_every_category() -> None:
    """A new ``ErrorCategory`` with no table entry fails here."""
    assert set(CATEGORY_TABLE) == set(ErrorCategory)


def test_error_codes_is_the_table_code_set() -> None:
    """``ERROR_CODES`` is the pinned set of codes the table can emit."""
    assert frozenset(code for code, _ in CATEGORY_TABLE.values()) == ERROR_CODES


def test_one_exemplar_per_category() -> None:
    """Exactly one exemplar per category — the parametrization is exhaustive."""
    assert {category for category, _ in _EXEMPLARS} == set(ErrorCategory)


@pytest.mark.parametrize(
    ("category", "exception"),
    _EXEMPLARS,
    ids=[category.name for category, _ in _EXEMPLARS],
)
def test_payload_projects_code_retriable_hint(
    category: ErrorCategory, exception: BaseException
) -> None:
    payload = tool_error_payload(exception)
    expected_code, expected_hint = CATEGORY_TABLE[category]
    classified = classify(exception)

    assert payload["code"] == expected_code == _CATEGORY_TO_MCP_CODE[category]
    assert payload["retriable"] is classified.retriable
    assert isinstance(payload["message"], str) and payload["message"]
    if expected_hint is None:
        assert "hint" not in payload
    else:
        assert payload["hint"] == expected_hint


def test_retriable_categories_are_marked_retriable() -> None:
    """The transient categories project retriable=True; deterministic ones False."""
    retriable = {
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.SERVER,
        ErrorCategory.TIMEOUT,
        ErrorCategory.ARTIFACT_TIMEOUT,
        ErrorCategory.NETWORK,
    }
    for category, exception in _EXEMPLARS:
        assert tool_error_payload(exception)["retriable"] is (category in retriable)


def test_message_is_redaction_capped_but_code_preserved() -> None:
    """A very long message is capped; code + retriable still present and correct."""
    long = exc.ValidationError("x" * 2000)
    payload = tool_error_payload(long)
    assert payload["code"] == "VALIDATION"
    assert payload["retriable"] is False
    assert len(payload["message"]) <= 320  # cap + ellipsis slack


def test_to_tool_error_returns_tool_error_with_payload() -> None:
    err = to_tool_error(exc.RateLimitError("slow", retry_after=3))
    assert isinstance(err, ToolError)
    # FastMCP ToolError surfaces the structured payload; the code must be readable.
    assert "RATE_LIMITED" in str(err)


def test_mcp_errors_translates_notebooklm_error() -> None:
    with pytest.raises(ToolError) as caught, mcp_errors():  # noqa: PT012
        raise exc.NotFoundError("missing")
    assert "NOT_FOUND" in str(caught.value)


def test_mcp_errors_wraps_unexpected_exception() -> None:
    """A plain ``RuntimeError`` is wrapped into a ToolError with code UNEXPECTED.

    Without this the advertised ``UNEXPECTED`` projection is never produced — a
    non-library exception would escape ``mcp_errors()`` unwrapped.
    """
    with pytest.raises(ToolError) as caught, mcp_errors():  # noqa: PT012
        raise RuntimeError("boom")
    assert "UNEXPECTED" in str(caught.value)


def test_mcp_errors_propagates_base_exceptions() -> None:
    """``CancelledError`` (a ``BaseException``) propagates uncaught — never wrapped.

    ``except Exception`` deliberately does not catch ``asyncio.CancelledError`` /
    ``KeyboardInterrupt`` / ``SystemExit`` so cancellation/shutdown is never
    swallowed into a ToolError.
    """
    import asyncio

    with pytest.raises(asyncio.CancelledError), mcp_errors():
        raise asyncio.CancelledError
