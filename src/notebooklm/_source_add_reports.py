"""Neutral vocabulary for reporting a source-add failure above the port.

``source.add_url``, ``source.add_text``, ``source.add_drive`` and
``source.add_url_batch`` are hoisted workflows (P10 R3.2–R3.5): they sequence
typed leaves in :class:`~notebooklm._source_service.SourceService` (the batch
in ``_source_batch_service``, which the module-size budget split off) and can
therefore neither raise a public exception nor read one.  What they report instead is a
:class:`~notebooklm._source_records.SourceAddFailureRecord` — the bounded,
serializable capture of the public graph the retired custom rows let escape —
which ``_backend_compat`` replays as an *equal* public exception at the facade.

This module owns that vocabulary: which neutral reasons each below-port
``except`` clause corresponded to, the messages the retired handlers raised
verbatim, the constructors that wrap a record in a ``SOURCE_ADD`` report, and
:class:`GuardedRegistration` — everything that differs between the two probed
registrations, so the algorithm they share can live exactly once.

It exists as its own module because ``_source_service.py`` would otherwise
exceed the 1500-line ``MODULE_SIZE_BUDGET``, which outside ``_auth/`` has no
exception. That split must not move the vocabulary out from under the guard
that keeps it neutral, so P10 invariant I1's *import* half governs this module
too (``tests/_guardrails/test_service_boundary.py``): nothing here may import
``_projectors``, ``notebooklm.types``, ``_types.*``, ``_backend_compat``,
``rpc.*``, ``_row_adapters.*``, ``_web.*`` or ``httpx``. I1's *return* half
does not apply — these are not service methods, and their constructors return
the port's own ``BackendError``, which a service raises rather than returns.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from ._backend import (
    BackendContractError,
    BackendError,
    BackendErrorReason,
)
from ._operations import Operation
from ._records import (
    SourceAddFailureKind,
    SourceAddFailureRecord,
    SourceRecord,
    SourceRegisterInput,
)

# The pre-P10 ``source.add_text`` row reached this message through
# ``SourceAddService``; the workflow that replaces the row owns it verbatim so
# the public ``NonIdempotentRetryError`` text is unchanged.
TEXT_NON_IDEMPOTENT_MESSAGE = (
    "add_text cannot be marked idempotent: text sources have no "
    "reliable server-side dedupe key (titles non-unique, content "
    "not exposed). For idempotent text imports, embed a UUID in "
    "the title and dedupe client-side. See "
    "docs/python-api.md#idempotency."
)

#: Failure kinds the pre-P10 registration handlers' ``except RPCError`` wrapped
#: into a ``SourceAddError``. It is deliberately *not* "every RPC-shaped
#: reason": ``AuthError``/``RateLimitError``/``ServerError``/``NetworkError``
#: (and ``RPCTimeoutError``, a ``NetworkError`` subclass) were caught first and
#: re-raised unwrapped under ADR-0019, so callers can still act on the specific
#: type. Anything outside this set keeps the leaf's own public identity.
#:
#: ``add_text`` and ``add_url`` shared this catch ordering verbatim below the
#: port, so the hoisted workflows share one definition of it rather than each
#: re-deriving which leaves survive as themselves.
WRAPPED_REGISTRATION_FAILURE_KINDS = frozenset(
    {
        SourceAddFailureKind.RPC,
        SourceAddFailureKind.CLIENT,
        SourceAddFailureKind.DECODING,
        SourceAddFailureKind.RESPONSE_TOO_LARGE,
        SourceAddFailureKind.UNKNOWN_RPC_METHOD,
    }
)

#: ``SourceAddError``'s own default message, owned verbatim so the hoisted URL
#: and Drive workflows report the text the retired rows' ``SourceAddError(url)``
#: / ``SourceAddError(title)`` did without naming a public exception type above
#: the port. Both registrations raised it with no explicit ``message``, so both
#: read the same template — the Drive one substitutes the requested title,
#: because that is the identifier its ``SourceAddError`` carried.
DEFAULT_ADD_FAILURE_MESSAGE = (
    "Failed to add source: {url}\n"
    "Possible causes:\n"
    "  - URL is invalid or inaccessible\n"
    "  - Content is behind a paywall or requires authentication\n"
    "  - Page content is empty or could not be parsed\n"
    "  - Rate limiting or quota exceeded"
)

#: The pre-P10 ``add_drive`` handler rejected a blank Drive id before the write;
#: the hoisted workflow owns the text so the public ``ValidationError`` is
#: unchanged.
DRIVE_BLANK_FILE_ID_MESSAGE = "Drive file_id cannot be empty or whitespace-only"

#: A Drive registration may legally echo nothing, and for this variant that
#: means the file type was refused rather than that the write was lost. Owned
#: verbatim from the retired handler, hint and all.
DRIVE_NULL_RESULT_MESSAGE = (
    "API returned no data for Drive source: {title} "
    "(mime_type={mime_type!r}). This Drive file type may not be "
    "importable via Drive — NotebookLM's Drive import supports "
    "Google-native Docs/Slides/Sheets + PDF only. If it is an "
    "upload-only type (e.g. epub/docx/txt/md/rtf/odt/csv), "
    "download it and add it as a `file` source instead."
)

#: Neutral reasons the pre-P10 probe re-raised *unwrapped* after marking the
#: outcome unknown: exactly the ``(AuthError, RateLimitError, ServerError,
#: NetworkError)`` tuple its ``except`` named, plus ``TIMEOUT`` because
#: ``RPCTimeoutError`` is a ``NetworkError`` subclass. Anything else means the
#: probe could not answer for a non-transport reason and becomes the UNRESOLVED
#: ``SourceAddError``.
DIRECT_PROBE_REASONS = frozenset(
    {
        BackendErrorReason.AUTH,
        BackendErrorReason.NETWORK,
        BackendErrorReason.RATE_LIMIT,
        BackendErrorReason.SERVER,
        BackendErrorReason.TIMEOUT,
    }
)

#: Neutral reasons whose replayed public exception is an ``RPCError`` or a
#: ``NetworkError`` — exactly the two families ``honor_requested_title``'s
#: ``except (RPCError, NetworkError)`` swallowed below the port. The post-create
#: rename is non-fatal by contract: the add already succeeded, so a rename
#: failure keeps the added source and logs a warning (#1960). Every other
#: reason still aborts, so a genuinely new failure mode cannot be silently
#: absorbed by the title phase.
RENAME_SWALLOWED_REASONS = frozenset(
    {
        BackendErrorReason.AUTH,
        BackendErrorReason.CLIENT,
        BackendErrorReason.DECODING,
        BackendErrorReason.NETWORK,
        BackendErrorReason.NOTEBOOK_NOT_FOUND,
        BackendErrorReason.RATE_LIMIT,
        BackendErrorReason.RESPONSE_TOO_LARGE,
        BackendErrorReason.RPC,
        BackendErrorReason.SERVER,
        BackendErrorReason.SOURCE_NOT_FOUND,
        BackendErrorReason.TIMEOUT,
        BackendErrorReason.UNKNOWN_RPC_METHOD,
    }
)

#: Diagnostics key ``_backend_compat`` reads to restore the implicit context a
#: probe inherits from the create it was run to reconcile. ``_capture_public_
#: failure`` deliberately refuses to descend into a private ``BackendError``
#: context, so a sequencing workflow carries that earlier public failure itself.
CREATE_CONTEXT_FAILURE = "create_context_failure"


def source_add_failure(
    operation: Operation,
    record: SourceAddFailureRecord,
    *,
    outcome_unknown: bool = False,
    dispatched: bool = False,
) -> BackendError:
    """Report one source-add failure as bounded neutral evidence.

    ``_backend_compat`` replays an *equal* public exception at the facade from
    ``record`` alone, so a transport-neutral workflow never has to name — or
    construct — a public exception type.
    """
    return BackendError(
        message=record.message,
        operation=operation,
        outcome_unknown=outcome_unknown,
        diagnostics=MappingProxyType({"source_add_failure": record}),
        reason=BackendErrorReason.SOURCE_ADD,
        dispatched=dispatched,
    )


def leaf_failure_record(error: BackendError) -> SourceAddFailureRecord | None:
    """Return the leaf's captured public graph, if the backend captured one.

    Capturing it is a *web* convention, not a port requirement: another adapter
    may report a closed reason and nothing else, and the compatibility projector
    reconstructs a public exception from the reason alone in that case. ``None``
    therefore means "project by reason", not "malformed". A value of the wrong
    type is malformed, and fails closed.
    """
    record = (error.diagnostics or {}).get("public_error_failure")
    if record is None:
        return None
    if not isinstance(record, SourceAddFailureRecord):
        raise BackendContractError(
            "source registration failure has invalid public-error evidence",
            operation=error.operation,
        ) from error
    return record


def degraded_failure_record(error: BaseException) -> SourceAddFailureRecord | None:
    """The captured graph of a failure the workflow deliberately continued past.

    Unlike :func:`leaf_failure_record` this never fails closed. The pre-create
    baseline read runs before anything is written, so proceeding without it is
    safe and it degrades rather than aborting; escalating malformed evidence
    there would convert the one read that is allowed to fail into a hard one.
    """
    if not isinstance(error, BackendError):
        return None
    record = (error.diagnostics or {}).get("public_error_failure")
    return record if isinstance(record, SourceAddFailureRecord) else None


def failure_type_name(error: BaseException) -> str:
    """The public exception class name a neutral failure was translated from.

    The ambiguity and UNRESOLVED messages name the failure the caller would
    otherwise have seen; a web adapter raises its ``BackendError`` *from* that
    public leaf, so the cause carries the name the pre-P10 messages printed.
    """
    cause = error.__cause__
    return type(cause).__name__ if cause is not None else type(error).__name__


def drive_subject(file_id: str) -> str:
    """Name a Drive import in a failure message by its ``documentId``.

    The Drive reports identify the *file id*, not the requested title their
    ``SourceAddError`` carries as ``url``: the title is re-derived server-side
    and is not what the reader would search the notebook for.
    """
    return f"Drive source {file_id!r}"


def describe_sources(sources: Sequence[SourceRecord]) -> str:
    """Render matched sources as ``id (title)`` for an ambiguity message.

    The ambiguity raises tell the caller to go check the notebook's source
    list; naming the exact rows saves them diffing a list by eye against a URL
    that, by definition, appears in it more than once.
    """
    return ", ".join(f"{source.id} ({source.title!r})" for source in sources)


@dataclass(frozen=True, slots=True)
class GuardedRegistration:
    """The per-variant half of the shared baseline/register/reconcile algorithm.

    ``source.add_url`` and ``source.add_drive`` run one algorithm: snapshot the
    notebook's source ids, issue one ``source.register`` write, and — only if
    that write may have committed — re-read and diff against the snapshot. What
    differs is the payload, the predicate that recognises the new row, and the
    wording of four failure reports. Those are the fields below; the algorithm
    itself lives once, in :meth:`SourceService._guarded_registration`.
    """

    #: The operation the reports are attributed to.
    workflow: Operation
    #: The workflow's own name in log diagnostics (``add_url`` / ``add_drive``).
    label: str
    #: ``SourceAddError``'s first argument below the port: the URL, or — for
    #: Drive, whose ``documentId`` is not what the retired handler passed — the
    #: requested title.
    identity: str
    #: How the UNRESOLVED report names what it could not confirm. Not always
    #: ``identity``: Drive names the file id, which is what a reader would go
    #: looking for.
    subject: str
    payload: SourceRegisterInput
    #: Recognises a row as this request's, before the baseline diff narrows it
    #: to rows this call created.
    matches: Callable[[SourceRecord], bool]
    #: Reported when the write echoes no row at all.
    null_result_message: str
    #: Reported when a match cannot be attributed because the baseline is gone.
    baseline_ambiguity: Callable[[Sequence[SourceRecord], str | None], str]
    #: Reported when the diff leaves more than one new match.
    match_ambiguity: Callable[[Sequence[SourceRecord]], str]
    #: The idempotency layer's own diagnostic label for this create.
    idempotency_label: str


def url_baseline_ambiguity(url: str, matches: Sequence[SourceRecord], name: str | None) -> str:
    """Both halves of the URL ambiguity: the match may predate the add, or *be* it.

    Action first: MCP and REST truncate at 300 chars, while the URL and the
    matched-row description are unbounded.
    """
    return (
        "UNRESOLVED — check the notebook source list before retrying. "
        f"Cannot disambiguate URL source {url!r}: the pre-create baseline "
        f"snapshot failed ({name}), so {describe_sources(matches)} may either "
        "predate this add or be the source it just created."
    )


def url_match_ambiguity(url: str, matches: Sequence[SourceRecord]) -> str:
    """Several new rows share the URL; ``describe_sources`` names them all.

    That description grows with every match, so the manual-reconciliation
    instruction stays inside the first 300 characters.
    """
    return (
        "UNRESOLVED — check the notebook source list before retrying. "
        f"Cannot disambiguate URL source {url!r}: probe found {len(matches)} new "
        f"sources with this URL after a transport failure ({describe_sources(matches)})."
    )


def drive_baseline_ambiguity(
    file_id: str, matches: Sequence[SourceRecord], name: str | None
) -> str:
    """The Drive twin, which names no rows — its wording is the retired handler's."""
    del matches
    return (
        f"Cannot disambiguate {drive_subject(file_id)}: the pre-create baseline "
        f"snapshot failed ({name}), so a matching source may either predate this "
        "add or be the one it just created. Check the notebook source list "
        "before retrying."
    )


def drive_match_ambiguity(file_id: str, matches: Sequence[SourceRecord]) -> str:
    """Several new rows share the ``documentId`` — a real shape, not a hypothetical."""
    return (
        f"Cannot disambiguate {drive_subject(file_id)}: probe found {len(matches)} "
        "new sources with this documentId after a transport failure. Check the "
        "notebook source list before retrying."
    )


__all__ = [
    "CREATE_CONTEXT_FAILURE",
    "DEFAULT_ADD_FAILURE_MESSAGE",
    "DIRECT_PROBE_REASONS",
    "DRIVE_BLANK_FILE_ID_MESSAGE",
    "DRIVE_NULL_RESULT_MESSAGE",
    "GuardedRegistration",
    "RENAME_SWALLOWED_REASONS",
    "TEXT_NON_IDEMPOTENT_MESSAGE",
    "WRAPPED_REGISTRATION_FAILURE_KINDS",
    "degraded_failure_record",
    "describe_sources",
    "drive_baseline_ambiguity",
    "drive_match_ambiguity",
    "drive_subject",
    "failure_type_name",
    "leaf_failure_record",
    "source_add_failure",
    "url_baseline_ambiguity",
    "url_match_ambiguity",
]
