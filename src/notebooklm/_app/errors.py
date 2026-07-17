"""Transport-neutral exception classification.

The CLI ``error_handler`` except-ladder and the MCP server's ``_CODE_TABLE``
both answer the same question — *which category of failure is this exception,
and is retrying worthwhile?* — and historically each kept its own copy of that
mapping. :func:`classify` is the single neutral source of truth for the
**category** decision; each adapter keeps its OWN code vocabulary and projects
the category onto it (CLI string codes + exit codes, MCP manifest-pinned codes).
See the rev-2 plan §5 ("split, not unified").

The category set is deliberately granular enough that the CLI's
``error_handler`` can recover every code it emits today 1:1:

==========================  ====================================
:class:`ErrorCategory`      CLI ``error_handler`` code
==========================  ====================================
``NOT_FOUND``               ``NOT_FOUND``
``AUTH``                    ``AUTH_ERROR``
``RATE_LIMITED``            ``RATE_LIMITED``
``VALIDATION``              ``VALIDATION_ERROR``
``CONFIG``                  ``CONFIG_ERROR``
``DEPENDENCY``              ``CONFIG_ERROR`` (missing optional extra; folds into CONFIG_ERROR)
``NETWORK``                 ``NETWORK_ERROR``
``NOTEBOOK_LIMIT``          ``NOTEBOOK_LIMIT``
``ARTIFACT_TIMEOUT``        ``ARTIFACT_TIMEOUT``
``TIMEOUT``                 (generic wait timeout — CLI maps to its own code)
``SERVER``                  (5xx — CLI currently folds into ``NOTEBOOKLM_ERROR``)
``RPC``                     (other RPC failures -> ``NOTEBOOKLM_ERROR``)
``SOURCE_MUTATION``         (``SourceMutationError`` carries its own ``.code``)
``SOURCE_ADD``              (``SourceAddError`` -> ``NOTEBOOKLM_ERROR``; non-fatal per-item)
``UNEXPECTED``              ``UNEXPECTED_ERROR`` (non-library exceptions)
==========================  ====================================

``SOURCE_MUTATION`` is the ``_app``-raised :class:`SourceMutationError`. It is
a deterministic CLI-input failure that carries its own ``.code`` vocabulary
(``AMBIGUOUS_ID`` / ``NOT_FOUND`` / ``CONFIRM_REQUIRED`` / …), so the CLI
projects that carried code rather than a category-derived one; the category
exists only so the coverage test never sees it fall through to ``LIBRARY``.

:func:`classify` is **class-sensitive**: it tests ``isinstance`` against the
``notebooklm.exceptions`` hierarchy most-specific-first, so an
:class:`ArtifactTimeoutError` classifies as ``ARTIFACT_TIMEOUT`` (not the
generic ``TIMEOUT``) and a :class:`NotebookLimitError` as ``NOTEBOOK_LIMIT``
(not the generic ``RPC``/library catch-all). Ordering matters because the
exceptions form a diamond (e.g. ``ArtifactTimeoutError`` is both a
``WaitTimeoutError`` and an ``ArtifactError``).

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from ..exceptions import (
    ArtifactTimeoutError,
    AuthError,
    ClientError,
    ConfigurationError,
    MissingDependencyError,
    NetworkError,
    NotebookLimitError,
    NotebookLMError,
    NotFoundError,
    RateLimitError,
    RPCError,
    ServerError,
    SourceAddError,
    ValidationError,
    WaitTimeoutError,
)
from .source_mutations import SourceMutationError


class ErrorCategory(Enum):
    """Transport-neutral failure category.

    Each value names a distinct kind of failure that adapters route
    differently (exit code, retry advice, manifest code). The set is granular
    enough that every existing CLI ``error_handler`` code is recoverable 1:1
    (see the module docstring table).
    """

    #: Resource lookup failed — a ``*NotFoundError`` (notebook/source/artifact/
    #: note/mind-map/label).
    NOT_FOUND = "not_found"
    #: Authentication / authorization failure; re-auth may help.
    AUTH = "auth"
    #: Rate limit exceeded; back off and retry.
    RATE_LIMITED = "rate_limited"
    #: Invalid user input / parameters.
    VALIDATION = "validation"
    #: Missing or invalid configuration (auth storage, env).
    CONFIG = "config"
    #: A required *optional* dependency (an install extra) is not installed —
    #: e.g. ``output_format="markdown"`` needs the ``markdownify`` extra. Distinct
    #: from the generic :attr:`CONFIG` (a bad auth/storage setup) so adapters can
    #: surface an *install the extra* hint instead of the auth/storage one (#1959).
    DEPENDENCY = "dependency"
    #: Connection / DNS / pre-RPC transport failure.
    NETWORK = "network"
    #: Notebook quota appears exhausted.
    NOTEBOOK_LIMIT = "notebook_limit"
    #: Artifact generation did not reach a terminal state in time. Distinct
    #: from the generic :attr:`TIMEOUT` so adapters keep their ``ARTIFACT_*``
    #: code + structured-status payload.
    ARTIFACT_TIMEOUT = "artifact_timeout"
    #: A non-artifact wait/poll timeout (source readiness, research task).
    TIMEOUT = "timeout"
    #: Server-side error (5xx).
    SERVER = "server"
    #: Other RPC-protocol failure after the connection succeeded.
    RPC = "rpc"
    #: A CLI-input source mutation failure (``SourceMutationError``) that
    #: carries its own ``.code`` taxonomy (``AMBIGUOUS_ID`` / ``NOT_FOUND`` /
    #: ``CONFIRM_REQUIRED`` / …). Distinct from the generic :attr:`LIBRARY`
    #: catch-all so adapters can recover that carried code rather than folding
    #: it into the library default.
    SOURCE_MUTATION = "source_mutation"
    #: A per-source ADD failure (``SourceAddError``) — NotebookLM rejected this
    #: specific source input (invalid/inaccessible/paywalled/empty/unparseable
    #: URL). Distinct from the generic :attr:`LIBRARY` catch-all so adapters
    #: project it as a 4xx input error and, in a batch add, ISOLATE it as a
    #: per-item error instead of aborting the whole batch. ``_source/add.py``
    #: re-raises every infra signal (auth/rate-limit/server/network) UNWRAPPED,
    #: so a ``SourceAddError`` is guaranteed to be a per-item input failure.
    SOURCE_ADD = "source_add"
    #: A library error that fits none of the above (catch-all under
    #: ``NotebookLMError``).
    LIBRARY = "library"
    #: A non-library exception escaped — likely a bug.
    UNEXPECTED = "unexpected"


#: Short remediation hint for each :class:`ErrorCategory`, or ``None`` when no
#: useful action exists beyond reading the message. This is the single neutral
#: source of truth for the hint text shared by the MCP projector (which pairs it
#: with its own manifest ``code`` in ``mcp/_errors.CATEGORY_TABLE``) and the REST
#: error body (``server/_errors``), so the two surfaces cannot drift. Covers
#: EVERY category (pinned by the adapter coverage tests).
CATEGORY_HINTS: dict[ErrorCategory, str | None] = {
    ErrorCategory.NOT_FOUND: (
        "Check the id/name with the matching *_list tool; the resource may have been deleted."
    ),
    ErrorCategory.AUTH: "Re-authenticate and retry.",
    ErrorCategory.RATE_LIMITED: "Back off and retry after a short delay.",
    ErrorCategory.VALIDATION: "Fix the invalid argument and retry; this will not succeed unchanged.",
    ErrorCategory.CONFIG: "Check the auth profile / storage configuration.",
    ErrorCategory.DEPENDENCY: (
        "Install the optional dependency, then retry (e.g. pip install 'notebooklm-py[markdown]')."
    ),
    ErrorCategory.NETWORK: "Transient connectivity issue; retry.",
    ErrorCategory.NOTEBOOK_LIMIT: "Notebook quota is exhausted; delete an existing notebook first.",
    ErrorCategory.ARTIFACT_TIMEOUT: (
        "Generation is still running; poll the task status with the task_id."
    ),
    ErrorCategory.TIMEOUT: "The operation did not finish in time; retry or poll for completion.",
    ErrorCategory.SERVER: "Upstream NotebookLM error; retry after a short delay.",
    ErrorCategory.RPC: None,
    ErrorCategory.SOURCE_MUTATION: (
        "Resolve the source reference (it was missing, ambiguous, or needs confirmation)."
    ),
    ErrorCategory.SOURCE_ADD: (
        "NotebookLM could not add this source (invalid/inaccessible URL, paywalled, empty, "
        "or unparseable); fix the input and retry — a failed source stub may have been "
        "created, so list the notebook's sources filtered to the error status to "
        "find and remove it."
    ),
    ErrorCategory.LIBRARY: None,
    ErrorCategory.UNEXPECTED: None,
}


def did_you_mean_hint(candidates: Sequence[Mapping[str, str]]) -> str:
    """Build the NOT_FOUND "did you mean" hint from near-miss candidates.

    Shared by every surface (MCP ``tool_error_payload``, the REST error body,
    the CLI ``NOT_FOUND`` envelope) so the phrasing cannot drift. Lists each
    candidate's title **and id** inline — the MCP wire flattens the structured
    error to a string via ``to_tool_error`` (which serializes only
    code/message/retriable/hint and drops the structured ``candidates`` list), so
    the id must live in the hint text for a flat-string client to retry by id
    without another list call. Replaces the generic :data:`CATEGORY_HINTS`
    NOT_FOUND hint only when a lookup actually produced near matches.
    """
    parts = ", ".join(f"{c['title']!r} (id: {c['id']})" for c in candidates)
    return f"Did you mean: {parts}? Pass the full title or id."


@dataclass(frozen=True)
class ClassifiedError:
    """The neutral classification of an exception.

    Attributes:
        category: The :class:`ErrorCategory` the exception falls into.
        retriable: Whether retrying the same operation could plausibly
            succeed. ``True`` only for the transient categories
            (rate-limit / server / timeout / network); ``False`` for
            deterministic failures (validation / not-found / auth / config /
            quota) and for the unexpected catch-all.
    """

    category: ErrorCategory
    retriable: bool


#: Categories for which a retry could plausibly succeed.
_RETRIABLE_CATEGORIES = frozenset(
    {
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.SERVER,
        ErrorCategory.TIMEOUT,
        ErrorCategory.ARTIFACT_TIMEOUT,
        ErrorCategory.NETWORK,
    }
)


def is_retriable(category: ErrorCategory) -> bool:
    """Return whether retrying an operation that failed with ``category`` may succeed.

    The single neutral source of the retriability decision (the same
    :data:`_RETRIABLE_CATEGORIES` set that backs :func:`classify`), so a surface
    that only knows a *category* (e.g. the REST server projecting a hand-raised
    ``HTTPException`` status onto a category, where there is no exception to
    :func:`classify`) can read the same flag without re-deriving it.
    """
    return category in _RETRIABLE_CATEGORIES


def _normalized_rpc_code(exc: RPCError) -> int | None:
    """Return ``exc.rpc_code`` normalized to an ``int``, or ``None`` if absent/non-numeric.

    ``rpc_code`` is typed ``str | int | None``; a string ``"5"`` must compare
    equal to the integer status, so this coerces before comparison and tolerates
    a non-numeric value (returns ``None`` rather than raising).
    """
    code = getattr(exc, "rpc_code", None)
    if code is None:
        return None
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


#: rpc_codes that mean a *transient / server-side* failure (not specific to the one
#: input): HTTP 5xx, plus the gRPC-status infra codes (4 DEADLINE_EXCEEDED, 8
#: RESOURCE_EXHAUSTED, 13 INTERNAL, 14 UNAVAILABLE). Used to keep a SourceAddError
#: whose bare-RPCError cause carries one of these FATAL in a batch add — the per-source
#: rejection codes (e.g. 3 INVALID_ARGUMENT / 9 FAILED_PRECONDITION) fall through to
#: the non-fatal SOURCE_ADD instead.
_TRANSIENT_GRPC_CODES = frozenset({4, 8, 13, 14})


def _is_transient_rpc_code(code: int | None) -> bool:
    """Whether ``code`` denotes a transient/server-side failure worth a retry."""
    if code is None:
        return False
    return 500 <= code < 600 or code in _TRANSIENT_GRPC_CODES


def _category_for(exc: BaseException) -> ErrorCategory:
    """Return the most-specific :class:`ErrorCategory` for ``exc``.

    The checks run most-specific-first because the exception hierarchy is a
    diamond — e.g. ``ArtifactTimeoutError`` is a ``WaitTimeoutError`` *and* an
    ``ArtifactError``, and a ``*NotFoundError`` is also an ``RPCError``. The
    first matching ``isinstance`` wins, so subclass branches MUST precede their
    bases.
    """
    # --- Class-sensitive specifics (must precede their bases) -----------------
    # Artifact timeout before the generic WaitTimeoutError umbrella.
    if isinstance(exc, ArtifactTimeoutError):
        return ErrorCategory.ARTIFACT_TIMEOUT
    # Any other wait/poll timeout (source readiness, research) — but NOT an
    # artifact timeout (handled above).
    if isinstance(exc, WaitTimeoutError):
        return ErrorCategory.TIMEOUT
    # Notebook quota before the generic RPC/library catch-alls (NotebookLimit
    # is a NotebookError -> NotebookLMError, not an RPCError).
    if isinstance(exc, NotebookLimitError):
        return ErrorCategory.NOTEBOOK_LIMIT

    # --- RPC-family branches (all subclass RPCError) --------------------------
    # NotFound mixes in RPCError; it must precede the RPCError catch-all so a
    # missing resource is NOT_FOUND, not generic RPC.
    if isinstance(exc, NotFoundError):
        return ErrorCategory.NOT_FOUND
    if isinstance(exc, AuthError):
        return ErrorCategory.AUTH
    if isinstance(exc, RateLimitError):
        return ErrorCategory.RATE_LIMITED
    if isinstance(exc, ServerError):
        return ErrorCategory.SERVER

    # --- Network (pre-RPC). RPCTimeoutError is a NetworkError, so this also
    # covers the transport-timeout case as NETWORK (it is not a WaitTimeout). --
    if isinstance(exc, NetworkError):
        return ErrorCategory.NETWORK

    # --- Validation / configuration ------------------------------------------
    # ResearchTaskMismatchError subclasses ValidationError; caught here.
    if isinstance(exc, ValidationError):
        return ErrorCategory.VALIDATION
    # MissingDependency subclasses ConfigurationError; it MUST precede the
    # ConfigurationError branch so a missing optional extra gets the "install the
    # extra" hint rather than the auth/storage one (#1959).
    if isinstance(exc, MissingDependencyError):
        return ErrorCategory.DEPENDENCY
    if isinstance(exc, ConfigurationError):
        return ErrorCategory.CONFIG

    # --- gRPC status-5 (NOT_FOUND) surfaced as a bare ClientError -------------
    # ``rpc/decoder.py`` raises ``ClientError(rpc_code=5)`` for a gRPC status-5
    # result (a deliberate non-``NotFoundError`` choice to dodge the auth-retry
    # path), so a genuine missing resource would otherwise fall through to the
    # generic ``RPC`` catch-all -> 502. Map it to ``NOT_FOUND`` here, before that
    # catch-all. The match is narrow to code **5 only** — the same decoder site
    # also raises code **7** (permission-denied), which must NOT be swept in —
    # and normalizes ``rpc_code`` (typed ``str | int | None``) so a string
    # ``"5"`` is not missed. Purely additive (no exception-type change), so the
    # ``RPC`` exemplar (a bare ``RPCError`` with no ``rpc_code``) is unaffected
    # and the consistency gate stays green.
    if isinstance(exc, ClientError) and _normalized_rpc_code(exc) == 5:
        return ErrorCategory.NOT_FOUND

    # --- Remaining RPC failures (decoding, unknown-method, client 4xx, ...) ---
    if isinstance(exc, RPCError):
        return ErrorCategory.RPC

    # --- Per-source ADD failure (SourceAddError). ----------------------------
    # A SourceError -> NotebookLMError (NOT an RPCError), so it reaches here only
    # after every RPC/infra branch missed. ``_source/add.py`` re-raises the TYPED
    # infra signals (auth/rate-limit/server/network) UNWRAPPED and wraps only a
    # residual RPCError as SourceAddError — usually a genuine per-source rejection
    # (bad URL, FAILED_PRECONDITION, …), which isolates as the NON-fatal SOURCE_ADD.
    # BUT a transient/server failure can still reach the wrap as a *bare* RPCError
    # (the null-result-with-status path in ``rpc/decoder.py`` raises RPCError with an
    # infra ``rpc_code`` rather than a typed ServerError). Keep those FATAL so a batch
    # add aborts for retry/backoff instead of masking a rate-limit/5xx as a per-item
    # error. Must precede the LIBRARY catch-all to keep its distinct 4xx category.
    if isinstance(exc, SourceAddError):
        cause = getattr(exc, "cause", None)
        if isinstance(cause, RPCError) and _is_transient_rpc_code(_normalized_rpc_code(cause)):
            return ErrorCategory.SERVER
        return ErrorCategory.SOURCE_ADD

    # --- CLI-input source-mutation error (carries its own .code taxonomy). ----
    # A direct NotebookLMError subclass, so it must precede the LIBRARY
    # catch-all to keep its distinct category.
    if isinstance(exc, SourceMutationError):
        return ErrorCategory.SOURCE_MUTATION

    # --- Any other library error ---------------------------------------------
    if isinstance(exc, NotebookLMError):
        return ErrorCategory.LIBRARY

    # --- Not one of ours -----------------------------------------------------
    return ErrorCategory.UNEXPECTED


def classify(exc: BaseException) -> ClassifiedError:
    """Classify ``exc`` into a neutral category + retriability decision.

    Args:
        exc: The exception to classify. Library exceptions
            (:class:`~notebooklm.exceptions.NotebookLMError` subclasses) map to
            a specific category; anything else maps to
            :attr:`ErrorCategory.UNEXPECTED`.

    Returns:
        A frozen :class:`ClassifiedError` carrying the category and whether a
        retry is worthwhile. The classification is purely structural
        (``isinstance``), so it is stable and side-effect-free.
    """
    category = _category_for(exc)
    return ClassifiedError(category=category, retriable=is_retriable(category))
