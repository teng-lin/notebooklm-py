"""Consistency gate: the CLI ``error_handler`` agrees with ``_app.errors.classify``.

Per ADR-0021, ``_app.errors.classify`` is the single neutral source of the failure
**category**; the CLI ``error_handler`` projects that category onto its own string-code
vocabulary (and the MCP server onto its manifest-pinned codes). Historically each layer
kept its own exception→code ladder, which could silently drift.

This gate pins the projection: for a properly-constructed exemplar of **every**
:class:`~notebooklm._app.errors.ErrorCategory`, the ``--json`` code the
``error_handler`` actually emits must equal the code this module maps that category to.
If someone adds an exception (or a category) and updates only one ladder, this fails.

It complements ``tests/unit/app/test_app_errors.py`` (every ``NotebookLMError`` subclass
classifies into *some* category) and ``tests/unit/cli/test_error_handler.py`` (each type
emits the expected envelope) by asserting the two are *the same decision*.
"""

from __future__ import annotations

import json

import pytest

from notebooklm import exceptions as exc
from notebooklm._app import SourceMutationError
from notebooklm._app.errors import ErrorCategory, classify
from notebooklm.cli.error_handler import handle_errors

# The CLI code each neutral category projects onto. The distinct codes
# (NOT_FOUND/AUTH_ERROR/…) recover 1:1; the transient/RPC-family categories
# (TIMEOUT/SERVER/RPC) and the library/source-mutation catch-alls currently
# fold into the generic ``NOTEBOOKLM_ERROR`` (a SourceMutationError that reaches
# the central handler — it is normally caught by the source command's own
# handler first — gets the library code). UNEXPECTED is the non-library bug code.
_CATEGORY_TO_CLI_CODE: dict[ErrorCategory, str] = {
    ErrorCategory.NOT_FOUND: "NOT_FOUND",
    ErrorCategory.AUTH: "AUTH_ERROR",
    ErrorCategory.RATE_LIMITED: "RATE_LIMITED",
    ErrorCategory.VALIDATION: "VALIDATION_ERROR",
    ErrorCategory.CONFIG: "CONFIG_ERROR",
    ErrorCategory.NETWORK: "NETWORK_ERROR",
    ErrorCategory.NOTEBOOK_LIMIT: "NOTEBOOK_LIMIT",
    ErrorCategory.ARTIFACT_TIMEOUT: "ARTIFACT_TIMEOUT",
    ErrorCategory.TIMEOUT: "NOTEBOOKLM_ERROR",
    ErrorCategory.SERVER: "NOTEBOOKLM_ERROR",
    ErrorCategory.RPC: "NOTEBOOKLM_ERROR",
    ErrorCategory.SOURCE_MUTATION: "NOTEBOOKLM_ERROR",
    ErrorCategory.LIBRARY: "NOTEBOOKLM_ERROR",
    ErrorCategory.UNEXPECTED: "UNEXPECTED_ERROR",
}

# One exemplar per category, constructed so the handler's attribute-dependent
# rendering (retry_after / NotebookLimit.to_error_response_extra / the artifact
# status block / NotFound id attrs) succeeds. ``ids`` name the category so a
# failure points at the drifting pair directly.
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
    """Every ``ErrorCategory`` has a CLI-code projection — a new category fails here."""
    assert set(_CATEGORY_TO_CLI_CODE) == set(ErrorCategory)


def test_one_exemplar_per_category() -> None:
    """Exactly one exemplar per category, so the parametrization is exhaustive."""
    assert {category for category, _ in _EXEMPLARS} == set(ErrorCategory)


def _emitted_code(exception: BaseException, capsys: pytest.CaptureFixture[str]) -> str:
    """The ``code`` field of the ``--json`` envelope the handler emits for ``exception``."""
    with pytest.raises(SystemExit), handle_errors(json_output=True):
        raise exception
    return json.loads(capsys.readouterr().out)["code"]


@pytest.mark.parametrize(
    ("expected_category", "exception"),
    _EXEMPLARS,
    ids=[category.name for category, _ in _EXEMPLARS],
)
def test_error_handler_code_matches_classify_category(
    expected_category: ErrorCategory,
    exception: BaseException,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 1. classify lands the exemplar in the expected category.
    assert classify(exception).category is expected_category
    # 2. the CLI handler's emitted code is the projection of that category.
    assert _emitted_code(exception, capsys) == _CATEGORY_TO_CLI_CODE[expected_category]
