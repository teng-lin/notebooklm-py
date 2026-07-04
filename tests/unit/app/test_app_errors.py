"""Tests for ``notebooklm._app.errors.classify``."""

from __future__ import annotations

import dataclasses

import pytest

from notebooklm import exceptions as exc
from notebooklm._app.download import DownloadPlanValidationError
from notebooklm._app.errors import (
    CATEGORY_HINTS,
    ClassifiedError,
    ErrorCategory,
    classify,
    did_you_mean_hint,
    is_retriable,
)
from notebooklm._app.source_add import SourceAddValidationError
from notebooklm._app.source_mutations import SourceMutationError


def _all_concrete_subclasses(root: type) -> list[type]:
    """Return every concrete (instantiable) subclass of ``root``, incl. ``root``."""
    seen: set[type] = set()
    out: list[type] = []
    stack = [root]
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        out.append(cls)
        stack.extend(cls.__subclasses__())
    return out


def _instance_without_init(cls: type) -> BaseException:
    """Build an instance bypassing ``__init__`` (constructors vary widely).

    ``classify`` is purely structural (``isinstance``), so an
    ``__init__``-less instance classifies identically to a fully-built one;
    this lets the coverage test enumerate every subclass without knowing each
    constructor's signature.
    """
    return cls.__new__(cls)  # type: ignore[no-any-return]


def test_classify_returns_frozen_classified_error() -> None:
    result = classify(exc.NotFoundError())

    assert isinstance(result, ClassifiedError)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.category = ErrorCategory.AUTH  # type: ignore[misc]


@pytest.mark.parametrize("cls", _all_concrete_subclasses(exc.NotebookLMError))
def test_every_library_exception_classifies_as_a_library_category(cls: type) -> None:
    """No ``NotebookLMError`` subclass falls through to ``UNEXPECTED``."""
    result = classify(_instance_without_init(cls))

    assert isinstance(result.category, ErrorCategory)
    assert result.category is not ErrorCategory.UNEXPECTED, (
        f"{cls.__name__} classified as UNEXPECTED — every library exception "
        "must map to a specific category."
    )


@pytest.mark.parametrize(
    ("exception", "expected_category", "expected_retriable"),
    [
        (
            exc.ArtifactTimeoutError("nb", "task", 1.0),
            ErrorCategory.ARTIFACT_TIMEOUT,
            True,
        ),
        (exc.SourceTimeoutError("src", 1.0), ErrorCategory.TIMEOUT, True),
        (exc.ResearchTimeoutError("nb", "task", 1.0), ErrorCategory.TIMEOUT, True),
        (exc.NotebookLimitError(5), ErrorCategory.NOTEBOOK_LIMIT, False),
        (exc.RateLimitError("slow down"), ErrorCategory.RATE_LIMITED, True),
        (exc.ServerError("boom"), ErrorCategory.SERVER, True),
        (exc.NetworkError("offline"), ErrorCategory.NETWORK, True),
        (exc.RPCTimeoutError("timed out"), ErrorCategory.NETWORK, True),
        (exc.AuthError("expired"), ErrorCategory.AUTH, False),
        (exc.ValidationError("bad"), ErrorCategory.VALIDATION, False),
        (exc.ConfigurationError("no auth"), ErrorCategory.CONFIG, False),
        (exc.SourceNotFoundError("src"), ErrorCategory.NOT_FOUND, False),
        (exc.NotebookNotFoundError("nb"), ErrorCategory.NOT_FOUND, False),
        (exc.DecodingError("schema drift"), ErrorCategory.RPC, False),
        (exc.NotebookLMError("generic"), ErrorCategory.LIBRARY, False),
        # ``_app``-raised errors re-based onto the public hierarchy (§11). The
        # two validation errors fold into VALIDATION via their ValidationError
        # base; SourceMutationError keeps its own category so adapters recover
        # its carried ``.code`` taxonomy.
        (
            DownloadPlanValidationError("Cannot specify both --force and --no-clobber"),
            ErrorCategory.VALIDATION,
            False,
        ),
        (SourceAddValidationError("bad url"), ErrorCategory.VALIDATION, False),
        (
            SourceMutationError("ambiguous", "AMBIGUOUS_ID"),
            ErrorCategory.SOURCE_MUTATION,
            False,
        ),
        (ValueError("not ours"), ErrorCategory.UNEXPECTED, False),
        (RuntimeError("not ours"), ErrorCategory.UNEXPECTED, False),
    ],
)
def test_class_sensitive_classification(
    exception: BaseException,
    expected_category: ErrorCategory,
    expected_retriable: bool,
) -> None:
    result = classify(exception)

    assert result.category is expected_category
    assert result.retriable is expected_retriable


def test_artifact_timeout_distinct_from_generic_wait_timeout() -> None:
    artifact = classify(exc.ArtifactTimeoutError("nb", "task", 1.0)).category
    generic = classify(exc.SourceTimeoutError("src", 1.0)).category

    assert artifact is ErrorCategory.ARTIFACT_TIMEOUT
    assert generic is ErrorCategory.TIMEOUT
    assert artifact is not generic


def test_artifact_timeout_subclasses_also_classify_as_artifact_timeout() -> None:
    pending = exc.ArtifactPendingTimeoutError("nb", "task", 1.0)
    in_progress = exc.ArtifactInProgressTimeoutError("nb", "task", 1.0)

    assert classify(pending).category is ErrorCategory.ARTIFACT_TIMEOUT
    assert classify(in_progress).category is ErrorCategory.ARTIFACT_TIMEOUT


@pytest.mark.parametrize("code", [5, "5"])
def test_client_error_status_5_classifies_as_not_found(code: int | str) -> None:
    """gRPC status-5 (raised as ``ClientError(rpc_code=5)``) is NOT_FOUND, not RPC.

    The decoder raises a bare ``ClientError`` (not a ``NotFoundError``) for a
    status-5 result; ``classify`` must recover NOT_FOUND. Both the int and the
    string form of ``rpc_code`` are normalized.
    """
    result = classify(exc.ClientError("missing", rpc_code=code))

    assert result.category is ErrorCategory.NOT_FOUND
    assert result.retriable is False


def test_client_error_status_7_is_not_swept_into_not_found() -> None:
    """Code 7 (permission-denied) from the same decoder site stays generic RPC."""
    result = classify(exc.ClientError("denied", rpc_code=7))

    assert result.category is ErrorCategory.RPC


def test_client_error_without_rpc_code_stays_rpc() -> None:
    """A ClientError carrying no rpc_code falls through to the RPC catch-all."""
    assert classify(exc.ClientError("client 4xx")).category is ErrorCategory.RPC


def test_bare_rpc_error_unaffected_by_status_5_branch() -> None:
    """The RPC exemplar (a bare RPCError, no rpc_code) keeps classifying as RPC.

    This is the exemplar the cross-adapter consistency gate uses; the additive
    status-5 branch must not perturb it.
    """
    assert classify(exc.RPCError("decode failed")).category is ErrorCategory.RPC


def test_not_found_wins_over_rpc_base() -> None:
    # *NotFoundError mixes in RPCError; classification must prefer NOT_FOUND.
    assert isinstance(exc.SourceNotFoundError("x"), exc.RPCError)
    assert classify(exc.SourceNotFoundError("x")).category is ErrorCategory.NOT_FOUND


def test_research_task_mismatch_is_validation() -> None:
    # ResearchTaskMismatchError subclasses ValidationError.
    err = exc.ResearchTaskMismatchError(task_id="A", source_research_task_id="B")
    assert classify(err).category is ErrorCategory.VALIDATION


@pytest.mark.parametrize(
    ("app_error", "expected_base", "expected_category"),
    [
        (
            DownloadPlanValidationError("boom"),
            exc.ValidationError,
            ErrorCategory.VALIDATION,
        ),
        (SourceAddValidationError("boom"), exc.ValidationError, ErrorCategory.VALIDATION),
        (
            SourceMutationError("boom", "NOT_FOUND"),
            exc.NotebookLMError,
            ErrorCategory.SOURCE_MUTATION,
        ),
    ],
)
def test_app_raised_errors_are_in_public_hierarchy_and_classify(
    app_error: BaseException,
    expected_base: type,
    expected_category: ErrorCategory,
) -> None:
    """Every ``_app``-raised exception is a ``NotebookLMError`` and classifies (§11)."""
    assert isinstance(app_error, exc.NotebookLMError)
    assert isinstance(app_error, expected_base)
    result = classify(app_error)
    assert result.category is expected_category
    assert result.category is not ErrorCategory.UNEXPECTED


def test_source_mutation_error_keeps_cli_attributes() -> None:
    """Re-basing onto NotebookLMError must not drop the CLI-read attributes."""
    err = SourceMutationError(
        "ambiguous id",
        "AMBIGUOUS_ID",
        {"source_id": "abc"},
        status_message="[dim]Matched: abc[/dim]",
    )
    assert err.message == "ambiguous id"
    assert err.code == "AMBIGUOUS_ID"
    assert err.extra == {"source_id": "abc"}
    assert err.status_message == "[dim]Matched: abc[/dim]"


def test_download_plan_validation_error_keeps_code_and_message() -> None:
    """``download_cmd`` reads ``.message`` / ``.code`` for its --json envelope."""
    err = DownloadPlanValidationError("Cannot specify both --force and --no-clobber")
    assert err.message == "Cannot specify both --force and --no-clobber"
    assert err.code == "VALIDATION_ERROR"
    assert str(err) == "Cannot specify both --force and --no-clobber"


def test_retriable_only_for_transient_categories() -> None:
    transient = {
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.SERVER,
        ErrorCategory.TIMEOUT,
        ErrorCategory.ARTIFACT_TIMEOUT,
        ErrorCategory.NETWORK,
    }
    samples = {
        ErrorCategory.NOT_FOUND: exc.SourceNotFoundError("x"),
        ErrorCategory.AUTH: exc.AuthError("x"),
        ErrorCategory.VALIDATION: exc.ValidationError("x"),
        ErrorCategory.CONFIG: exc.ConfigurationError("x"),
        ErrorCategory.NOTEBOOK_LIMIT: exc.NotebookLimitError(1),
        ErrorCategory.RPC: exc.DecodingError("x"),
        ErrorCategory.LIBRARY: exc.NotebookLMError("x"),
        ErrorCategory.UNEXPECTED: ValueError("x"),
        ErrorCategory.RATE_LIMITED: exc.RateLimitError("x"),
        ErrorCategory.SERVER: exc.ServerError("x"),
        ErrorCategory.NETWORK: exc.NetworkError("x"),
        ErrorCategory.TIMEOUT: exc.SourceTimeoutError("x", 1.0),
        ErrorCategory.ARTIFACT_TIMEOUT: exc.ArtifactTimeoutError("nb", "task", 1.0),
    }
    for category, sample in samples.items():
        result = classify(sample)
        assert result.category is category
        assert result.retriable is (category in transient)


def test_classify_retriable_delegates_to_is_retriable() -> None:
    """``classify`` reads retriability from ``is_retriable`` (single source of truth).

    Rather than re-inlining ``category in _RETRIABLE_CATEGORIES``, ``classify``
    must agree with :func:`is_retriable` for both transient and deterministic
    exceptions — so the two never drift.
    """
    samples = [
        exc.RateLimitError("x"),
        exc.ServerError("x"),
        exc.NetworkError("x"),
        exc.AuthError("x"),
        exc.ValidationError("x"),
        ValueError("x"),
    ]
    for sample in samples:
        result = classify(sample)
        assert result.retriable is is_retriable(result.category)


def test_category_hints_are_surface_neutral() -> None:
    """The shared REST+MCP hints must not name a specific tool or CLI command.

    ``CATEGORY_HINTS`` is consumed by BOTH the MCP projector and the REST error
    body, so a hint that names ``studio_status`` or ``notebooklm login`` would be
    wrong on the other surface. Guards the F2 neutralization.
    """
    banned = ("studio_status", "notebooklm ", "`")
    for category, hint in CATEGORY_HINTS.items():
        if hint is None:
            continue
        for token in banned:
            assert token not in hint, (
                f"{category} hint leaks surface-specific token {token!r}: {hint}"
            )
    assert CATEGORY_HINTS[ErrorCategory.AUTH] == "Re-authenticate and retry."
    assert "task status" in CATEGORY_HINTS[ErrorCategory.ARTIFACT_TIMEOUT]


# --- did_you_mean_hint (issue #1787) ---------------------------------------


def test_did_you_mean_hint_includes_id_and_title() -> None:
    """The hint carries id AND title so a flat-string MCP client can retry by id."""
    hint = did_you_mean_hint([{"id": "abc123", "title": "Scientific PDF Parsing"}])
    assert hint.startswith("Did you mean:")
    assert "abc123" in hint
    assert "Scientific PDF Parsing" in hint
    assert hint.endswith("Pass the full title or id.")


def test_did_you_mean_hint_lists_multiple_candidates() -> None:
    hint = did_you_mean_hint([{"id": "id1", "title": "Alpha"}, {"id": "id2", "title": "Beta"}])
    assert "id1" in hint and "id2" in hint
    assert "Alpha" in hint and "Beta" in hint
