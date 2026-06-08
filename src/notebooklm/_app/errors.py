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
``NETWORK``                 ``NETWORK_ERROR``
``NOTEBOOK_LIMIT``          ``NOTEBOOK_LIMIT``
``ARTIFACT_TIMEOUT``        ``ARTIFACT_TIMEOUT``
``TIMEOUT``                 (generic wait timeout — CLI maps to its own code)
``SERVER``                  (5xx — CLI currently folds into ``NOTEBOOKLM_ERROR``)
``RPC``                     (other RPC failures -> ``NOTEBOOKLM_ERROR``)
``SOURCE_MUTATION``         (``SourceMutationError`` carries its own ``.code``)
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

from dataclasses import dataclass
from enum import Enum

from ..exceptions import (
    ArtifactTimeoutError,
    AuthError,
    ConfigurationError,
    NetworkError,
    NotebookLimitError,
    NotebookLMError,
    NotFoundError,
    RateLimitError,
    RPCError,
    ServerError,
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
    #: A library error that fits none of the above (catch-all under
    #: ``NotebookLMError``).
    LIBRARY = "library"
    #: A non-library exception escaped — likely a bug.
    UNEXPECTED = "unexpected"


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
    if isinstance(exc, ConfigurationError):
        return ErrorCategory.CONFIG

    # --- Remaining RPC failures (decoding, unknown-method, client 4xx, ...) ---
    if isinstance(exc, RPCError):
        return ErrorCategory.RPC

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
    return ClassifiedError(category=category, retriable=category in _RETRIABLE_CATEGORIES)
