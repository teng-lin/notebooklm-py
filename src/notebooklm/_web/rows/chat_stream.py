"""Web streamed-chat parsing mechanics for NotebookLM private chat calls.

This module owns streamed-chat response parsing. Conversation flow, request
construction, caching, source resolution, and ``AskResult``
construction stay in :mod:`notebooklm._chat`.
"""

from __future__ import annotations

import json
import logging
import math
import re
import reprlib
from dataclasses import dataclass, field, replace
from typing import Any, NoReturn

from ..._types.documents import (
    DocumentAnnotation,
    StructuredDocument,
    _utf16_slice,
    utf16_len,
)
from ...exceptions import (
    ChatError,
    ChatResponseParseError,
    RateLimitError,
    UnknownRPCMethodError,
)
from ...rpc.types import RPCMethod
from ...types import ChatReference, ConversationTurnKey, NextStepSuggestion
from ..wire.decoder import strip_anti_xssi
from ..wire.safe_index import safe_index
from .chat import (
    AnswerRow,
    CitationDetail,
    CitationRow,
    ErrorPayloadRow,
    StreamEnvelopeRow,
    StreamFrameRow,
)
from .documents import build_blocks

# Deliberate: use the ``notebooklm._chat`` logger namespace (not this module's)
# so existing log filters keep matching the chat parser diagnostics.
logger = logging.getLogger("notebooklm._chat")

# ``safe_index`` source labels for the streamed-chat descents. The streamed
# chat endpoint (``GenerateFreeFormStreamed``) is not a batchexecute RPC, so
# there is no obfuscated method ID to thread — descents pass ``method_id=None``
# and rely on these labels to localize schema drift in raised
# ``UnknownRPCMethodError`` diagnostics (ADR-0011).
_CHUNK_SOURCE = "_chat_wire._extract_chunk_with_parseable"
_CITATION_SOURCE = "_chat_wire.parse_citations"

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StreamingChatParseResult:
    """Parsed streamed-chat answer payload.

    The third field is named ``conversation_id`` for backward compatibility
    with the prior parser contract, but live API tests (issue #659) proved
    it is actually a per-stream/per-query identifier, **not** a real
    conversation_id: ``khqZz`` returns 0 turns when queried with it, and
    passing it back as a follow-up ``conversation_id`` produces a ghost
    turn the server does not record. The real conversation_id must be
    fetched separately via ``hPTbtc`` (``ChatAPI.get_conversation_id``)
    after the ask. Callers should generally ignore this field.
    """

    answer: str
    references: list[ChatReference]
    conversation_id: str | None
    #: The winning answer row's own document — its paragraphs plus the
    #: annotation map that anchored each reference's ``answer_anchor_*`` range
    #: (#2120). Empty when no chunk carried a decodable document.
    answer_document: StructuredDocument = field(default_factory=StructuredDocument)
    #: The backend's key for the answered turn (#2122), decoded from
    #: ``AnswerResponse.conversationTurnKey``. Collected **last-wins across the
    #: chunks that carried one**, NOT taken from the chunk that won the answer:
    #: the key was identical on every chunk of a turn in every observation, so
    #: there is nothing to choose between them, and collecting it independently
    #: keeps it available when no chunk wins (an empty answer still has a turn).
    #: ``None`` when no chunk carried a usable key. Unlike
    #: :attr:`conversation_id` above this is NOT a legacy field — it is the key
    #: ``SubmitFeedback`` is addressed by.
    turn_key: ConversationTurnKey | None = None
    #: Suggested follow-up questions/actions, collected last-wins across chunks
    #: that carried a populated ``NextStepSuggestions`` block.
    next_steps: list[NextStepSuggestion] = field(default_factory=list)


@dataclass(frozen=True)
class _ChunkExtraction:
    """One streamed chunk's decoded contents, internal to this module.

    Replaces the 6-tuple the chunk extractor used to return: the answer
    document added by #2120, and the two slots #2122 recovered, would have made
    it a 9-tuple whose positions no reader could keep straight. Every field
    defaults to its "nothing here" value so the several early-return paths
    (undecodable JSON, non-list payload, no usable answer row) each name only
    what they actually know.
    """

    text: str | None = None
    is_answer: bool = False
    references: list[ChatReference] = field(default_factory=list)
    conversation_id: str | None = None
    parseable: bool = False
    suggests_drift: bool = False
    document: StructuredDocument = field(default_factory=StructuredDocument)
    #: ``GenerateFreeFormStreamedResponse.isFinalResponse`` (#2122). On a
    #: chunk that yielded an answer this is the flag from the envelope that
    #: carried that answer; on one that yielded none it is the OR across the
    #: frame's envelopes, so the parser can still tell "the final chunk carried
    #: no answer" from "no final chunk arrived".
    is_final_response: bool = False
    #: The ``ConversationTurnKey`` seen on this chunk (#2122), or ``None``.
    #: Read before the answer-text gate, so a text-less chunk still reports it.
    turn_key: ConversationTurnKey | None = None
    next_steps: list[NextStepSuggestion] = field(default_factory=list)
    #: Sequence metadata from a terminal ``["e", n, ...]`` bookkeeping
    #: frame. It is deliberately not called a status: successful streams also
    #: carry this frame and ``n`` is not an RPC/gRPC error code.
    terminal_sequence: int | None = None


def parse_streaming_chat_response(response_text: str) -> StreamingChatParseResult:
    """Parse a streamed-chat response into answer, references, and conversation ID.

    Failure contract (see :class:`notebooklm.exceptions.ChatResponseParseError`):

    * **Terminal bookkeeping without an RPC payload** — the stream contains
      only transport frames and a terminal ``e`` frame. The Web backend emits
      this shape when chat quota is exhausted (the Android backend reports the
      same account state explicitly as ``RESOURCE_EXHAUSTED``). Raises
      :class:`~notebooklm.exceptions.RateLimitError`.
    * **Zero parseable chunks without a terminal frame** — no chunk in the
      response yielded a successfully decoded ``wrb.fr`` envelope. This means
      either the response body was empty/garbage, or the API's wire format
      drifted and the parser no longer recognizes the envelope shape. Raises
      :class:`ChatResponseParseError`.
    * **Chunks parsed but empty answer** — at least one ``wrb.fr`` chunk
      decoded, but no chunk yielded answer text (the model legitimately
      returned an empty response). Returns
      ``StreamingChatParseResult("", refs, conv_id)`` — empty answer is
      a valid outcome, not a parse failure.

    **Answer selection (#2122).** Chunks arrive cumulatively, so which one
    holds "the answer" has to be chosen. The backend marks the last chunk with
    ``isFinalResponse`` (``inner_data[4]``), so a *marked answer* chunk that
    also carries that flag wins outright. Only if no such chunk exists does the
    historical longest-wins heuristic decide — it is an inference standing in
    for a boolean the server already sends, and it fails silently whenever the
    final chunk is not the longest (a truncated or corrected final chunk, a
    stream ending on a short closing statement). The fallback logs a WARNING
    when it fires.

    The answer marker still decides what counts as an answer at all:
    ``isFinalResponse`` says "last chunk", not "this is an answer", so it only
    picks *between* marked answer chunks. The unmarked-text fallback and its
    drift diagnostics are unchanged.
    """
    # Shared anti-XSSI stripper (``_web.wire.decoder.strip_anti_xssi``) is the single
    # owner of the )]}' prefix removal. For the real chat wire format the
    # prefix is always followed by a newline, so the subsequent ``.strip()``
    # yields a byte-for-byte-identical result to the prior blind ``[4:]`` slice.
    response_text = strip_anti_xssi(response_text)

    lines = response_text.strip().split("\n")
    final_marked_answer = ""
    final_marked_refs: list[ChatReference] = []
    best_marked_answer = ""
    best_marked_refs: list[ChatReference] = []
    best_unmarked_answer = ""
    best_unmarked_refs: list[ChatReference] = []
    final_marked_document = StructuredDocument()
    best_marked_document = StructuredDocument()
    best_unmarked_document = StructuredDocument()
    saw_drift_signal = False
    server_conv_id: str | None = None
    turn_key: ConversationTurnKey | None = None
    next_steps: list[NextStepSuggestion] = []
    saw_final_chunk = False
    parseable_chunk_count = 0
    terminal_sequence: int | None = None

    def process_chunk(json_str: str) -> None:
        """Process a JSON chunk, updating best answer candidates and their refs."""
        nonlocal final_marked_answer, final_marked_refs, final_marked_document
        nonlocal best_marked_answer, best_marked_refs, best_marked_document
        nonlocal best_unmarked_answer, best_unmarked_refs, best_unmarked_document
        nonlocal saw_drift_signal, server_conv_id, turn_key, next_steps, parseable_chunk_count
        nonlocal saw_final_chunk, terminal_sequence
        chunk = _extract_chunk_with_parseable(json_str)
        if chunk.parseable:
            parseable_chunk_count += 1
        # Recorded whether or not the chunk bore text: it is what separates
        # "the final chunk carried no answer" from "no final chunk arrived",
        # and the fallback diagnostic below names which one happened.
        saw_final_chunk |= chunk.is_final_response
        if chunk.text:
            if chunk.is_answer:
                # Last write wins if the backend ever marks two chunks final:
                # "final" is a position claim, so the later one is the later
                # position. Not observed; the tie has to break somewhere.
                if chunk.is_final_response:
                    final_marked_answer = chunk.text
                    final_marked_refs = chunk.references
                    final_marked_document = chunk.document
                if len(chunk.text) > len(best_marked_answer):
                    best_marked_answer = chunk.text
                    best_marked_refs = chunk.references
                    best_marked_document = chunk.document
            else:
                saw_drift_signal |= chunk.suggests_drift
                if len(chunk.text) > len(best_unmarked_answer):
                    best_unmarked_answer = chunk.text
                    best_unmarked_refs = chunk.references
                    best_unmarked_document = chunk.document
        if chunk.conversation_id:
            server_conv_id = chunk.conversation_id
        if chunk.turn_key is not None:
            turn_key = chunk.turn_key
        if chunk.next_steps:
            next_steps = chunk.next_steps
        if chunk.terminal_sequence is not None:
            terminal_sequence = chunk.terminal_sequence

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        try:
            int(line)
            i += 1
            if i < len(lines):
                process_chunk(lines[i])
            i += 1
        except ValueError:
            process_chunk(line)
            i += 1

    if parseable_chunk_count == 0:
        if terminal_sequence is not None:
            # Live cross-backend characterization: when an account is out of
            # chat quota, Android reports RESOURCE_EXHAUSTED while Web emits
            # only the transport bookkeeping plus this terminal frame. The
            # sequence value itself is still NOT an error code (successful
            # streams have one too); the quota signal is the complete absence
            # of any ``wrb.fr`` payload before termination.
            raise RateLimitError(
                "Chat rate limit reached: the request ended before the server "
                "returned an RPC payload "
                f"(terminal stream sequence {terminal_sequence}). Retry later.",
                rpc_code="STREAM_TERMINATED_WITHOUT_RPC_PAYLOAD",
            )
        # No ``wrb.fr`` envelopes recognized — distinguishable from a
        # legitimate empty answer (which still produces at least one
        # parseable chunk). Raise so callers can distinguish wire-drift
        # / empty-body from "the model returned nothing."
        raise ChatResponseParseError(
            f"No parseable chunks in streaming chat response ({len(lines)} lines scanned). "
            "The response was empty or the API wire format may have changed."
        )

    if final_marked_answer:
        longest_answer = final_marked_answer
        final_refs = final_marked_refs
        final_document = final_marked_document
        if final_marked_answer != best_marked_answer:
            # The heuristic would have returned a different chunk. Worth a
            # record: this is the case #2122 says fails silently today.
            logger.debug(
                "isFinalResponse chunk (%d chars) differs from the longest "
                "marked chunk (%d chars); using the server's final marker.",
                len(final_marked_answer),
                len(best_marked_answer),
            )
    elif best_marked_answer:
        # No marked chunk carried isFinalResponse *and* text, so the answer
        # below is an inference. The two ways to get here are diagnosed
        # differently, which is the whole reason ``is_final_response`` is
        # reported for text-less chunks:
        #   * a final chunk DID arrive but carried no answer text — the stream
        #     completed and the model said nothing in its last chunk;
        #   * no chunk claimed finality at all — a truncated stream, or the
        #     flag moved and this client is now guessing on every ask.
        if saw_final_chunk:
            logger.warning(
                "The isFinalResponse chunk carried no answer text; falling back "
                "to the longest marked chunk (%d chars).",
                len(best_marked_answer),
            )
        else:
            logger.warning(
                "No chunk carried isFinalResponse; falling back to the longest "
                "marked chunk (%d chars). The stream may have been truncated, or "
                "the API response format may have changed.",
                len(best_marked_answer),
            )
        longest_answer = best_marked_answer
        final_refs = best_marked_refs
        final_document = best_marked_document
    elif best_unmarked_answer:
        if saw_drift_signal:
            logger.warning(
                "No marked answer found; falling back to longest unmarked "
                "text (%d chars). The API response format may have changed.",
                len(best_unmarked_answer),
            )
        longest_answer = best_unmarked_answer
        final_refs = best_unmarked_refs
        final_document = best_unmarked_document
    else:
        longest_answer = ""
        final_refs = []
        final_document = StructuredDocument()

    if not longest_answer:
        logger.warning(
            "No answer extracted from response (%d lines parsed, %d parseable chunks)",
            len(lines),
            parseable_chunk_count,
        )

    # Assign citation numbers without mutating the dataclass instances in place
    # (prepares for an eventual ``frozen=True`` sweep on public domain types).
    # The list is rebuilt — externally identical to the prior mutation since
    # only ``citation_number`` ever changes here. ``parse_citations`` already
    # stamps raw wire ordinals; the ``is None`` guard deliberately preserves
    # them (a skipped malformed row leaves a hole so [N] markers never shift
    # onto the wrong citation) — the dense fill applies only to refs that
    # arrived unnumbered.
    final_refs = [
        replace(ref, citation_number=idx) if ref.citation_number is None else ref
        for idx, ref in enumerate(final_refs, start=1)
    ]

    return StreamingChatParseResult(
        answer=longest_answer,
        references=final_refs,
        conversation_id=server_conv_id,
        answer_document=final_document,
        turn_key=turn_key,
        next_steps=next_steps,
    )


def extract_answer_and_refs_from_chunk(
    json_str: str,
) -> tuple[str | None, bool, list[ChatReference], str | None]:
    """Extract answer text, references, and conversation ID from one response chunk.

    Public 4-tuple wrapper around :func:`_extract_chunk_with_parseable`.
    The remaining :class:`_ChunkExtraction` fields are internal-only — they exist
    for the streaming parser's "zero parseable chunks" detection and answer
    selection, and are not part of this module's outward-facing contract.
    """
    chunk = _extract_chunk_with_parseable(json_str)
    return chunk.text, chunk.is_answer, chunk.references, chunk.conversation_id


def _extract_chunk_with_parseable(json_str: str) -> _ChunkExtraction:
    """Extract answer/refs/conv-id from one chunk and report wire-format parseability.

    :attr:`~_ChunkExtraction.parseable` is True iff at least one ``wrb.fr``
    envelope was found AND its inner JSON decoded successfully — regardless of
    whether any answer text was extracted.
    :attr:`~_ChunkExtraction.suggests_drift` is the selected row's
    :attr:`~notebooklm._web.rows.chat.AnswerRow.suggests_wire_drift` verdict:
    whether an unmarked row looks like drift rather than a deliberate empty
    answer. Together these let the streaming parser distinguish two failure
    modes:

    * Zero parseable chunks → API drift or empty body (raise).
    * At least one parseable chunk but no text → real empty answer (return).

    :attr:`~_ChunkExtraction.is_final_response` and
    :attr:`~_ChunkExtraction.turn_key` are both read from ABOVE the answer-text
    gate, so a chunk that carries no text still reports them — see their field
    comments for the exact per-item vs across-frame semantics.
    """
    refs: list[ChatReference] = []

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return _ChunkExtraction(references=refs)

    if not isinstance(data, list):
        return _ChunkExtraction(references=refs)

    parseable = False
    saw_final_envelope = False
    turn_key: ConversationTurnKey | None = None
    next_steps: list[NextStepSuggestion] = []
    terminal_sequence: int | None = None
    for item in data:
        if not isinstance(item, list) or len(item) < 2:
            continue

        # Surface server-side error frames instead of silently skipping them.
        # The batchexecute stream emits ``["er", rpc_id, code, ...]`` frames
        # when the RPC itself failed; the old parser only inspected
        # ``"wrb.fr"`` frames, so a server error collapsed into the generic
        # "no parseable chunks" / "empty response" failure. ``StreamFrameRow``
        # centralises the ``item[0]`` / ``item[2]`` / ``item[5]`` frame reads
        # (issue #1491). ``frame.tag`` is the one guaranteed slot
        # (``len(item) >= 2``) so its ``safe_index`` descent is byte-for-byte
        # identical on the happy path and only raises if the tag slot drifted.
        frame = StreamFrameRow(item)
        tag = frame.tag
        if tag == "er":
            _raise_chat_error_frame(item)

        if tag == "e":
            terminal_sequence = frame.terminal_sequence
            continue

        if tag != "wrb.fr" or len(item) < 3:
            continue

        inner_json = frame.inner_json
        if not isinstance(inner_json, str):
            # item[2] is null — this ``wrb.fr`` carries no answer JSON. In real
            # traffic that only happens when the server rejected the request and
            # put a status/error payload at item[5] instead (a successful
            # answer/heartbeat frame always has a *string* at item[2]; no live
            # capture has ever shown a null-item[2] ``wrb.fr`` on success).
            # Surface it rather than silently skipping, which collapsed every
            # rejection into the generic "no parseable chunks" failure
            # (issue #1472). The error code is NOT the ``["e", ...]`` /
            # ``["di", ...]`` / ``["af.httprm", ...]`` frames elsewhere in the
            # response — those are batchexecute stream bookkeeping and their
            # trailing number is a running byte count, not a code.
            #
            # Don't flip ``parseable`` here: a null inner_json is not a
            # successfully decoded envelope. Both raise paths below are
            # ``NoReturn``, and any present payload (``is not None`` — even an
            # empty ``[]``, which ``_raise_chat_rejection`` reports without a
            # status) is treated as a rejection, so flow only reaches the next
            # iteration when item[5] was absent or a non-list.
            error_payload = frame.error_payload
            if error_payload is not None:
                raise_if_rate_limited(error_payload)
                _raise_chat_rejection(error_payload)
            continue

        try:
            inner_data = json.loads(inner_json)
        except json.JSONDecodeError:
            # Hot-path stream parser: skip non-JSON chunks. Guard the
            # debug log with isEnabledFor so the redaction regex doesn't
            # run on every chunk when DEBUG is off.
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Stream parser: non-JSON chunk skipped")
            continue

        # The wire envelope decoded. Mark parseable BEFORE the answer-text
        # extraction so a real empty-answer chunk (text == "") still counts
        # — that's exactly the case the new failure contract preserves
        # against ``ChatResponseParseError``.
        parseable = True
        # ``isFinalResponse`` sits on the envelope, a level ABOVE the answer
        # row, so it is read here rather than off ``AnswerRow`` (#2122). It is
        # read PER ITEM: the answer returned below reports the flag from the
        # envelope that carried it, not an OR across the frame, so a chunk is
        # only "final" if the envelope holding the answer said so. The OR is
        # kept solely for the no-answer fall-through, where the question is the
        # weaker "did any envelope in this frame claim finality".
        envelope = StreamEnvelopeRow(inner_data)
        envelope_is_final = envelope.is_final_response
        saw_final_envelope |= envelope_is_final
        decoded_next_steps = [
            NextStepSuggestion(question=row.question, type_code=row.type_code)
            for row in envelope.next_step_rows
            if row.is_well_formed and row.question is not None and row.type_code is not None
        ]
        if decoded_next_steps:
            next_steps = decoded_next_steps

        if isinstance(inner_data, list) and len(inner_data) > 0:
            # ``inner_data`` is a *populated* answer record (heartbeats decode
            # to ``[]`` and are excluded by ``len > 0`` above, so they never
            # reach this strict descent). Read the answer row through
            # ``safe_index`` (no-op on the happy path since ``len > 0``); the
            # descent label localizes any top-level reshape in diagnostics.
            first = safe_index(inner_data, 0, method_id=None, source=_CHUNK_SOURCE)
            if not isinstance(first, list):
                # The populated record's answer row is not a list — a leaf
                # became a scalar/dict or an inner list became a wrapper. This
                # is genuine Google-side drift that previously collapsed into a
                # silent empty answer. Raise the same drift signal
                # ``safe_index`` uses (``UnknownRPCMethodError``) so the chat
                # path fails loudly instead of dropping the answer (ADR-0011).
                # Strict decoding is the only mode (the
                # ``NOTEBOOKLM_STRICT_DECODE=0`` soft-mode opt-out was retired
                # in v0.7.0). ``safe_index`` cannot enforce the list *type* (a
                # ``str`` answer row is still indexable), so the contract is
                # checked explicitly here.
                raise UnknownRPCMethodError(
                    f"Streamed chat answer row is not a list (got {type(first).__name__})",
                    method_id=None,
                    path=(0,),
                    source=_CHUNK_SOURCE,
                    data_at_failure=repr(first)[:200],
                )
            if len(first) > 0:
                # The populated record is wrapped in an ``AnswerRow`` so every
                # leaf read (text / answer-marker / server-conv-id / citations)
                # goes through one named position contract in
                # ``_web/rows/chat.py`` instead of scattered single-level
                # subscripts here (issue #1491). ``text`` is the load-bearing
                # answer leaf; an absent/empty/non-string leaf legitimately means
                # "no answer in this chunk" (heartbeat-ish), so fall through.
                answer = AnswerRow(first)
                # Read the key BEFORE the text gate, for the same reason
                # ``isFinalResponse`` is read above it: the key is a property of
                # the TURN, not of the answer text, and the backend sends it on
                # chunks that carry no text (chunk 1 of every observed stream).
                # Gating it on text would drop the key for an empty answer —
                # the turn a caller is most likely to want to give feedback on.
                if answer.turn_key is not None:
                    turn_key = answer.turn_key
                text = answer.text
                if text is None:
                    continue

                document = answer.document
                refs = parse_citations(first, document)
                return _ChunkExtraction(
                    text=text,
                    is_answer=answer.is_answer,
                    references=refs,
                    conversation_id=answer.server_conversation_id,
                    parseable=parseable,
                    suggests_drift=answer.suggests_wire_drift,
                    document=document,
                    is_final_response=envelope_is_final,
                    turn_key=turn_key,
                    next_steps=next_steps,
                )
        # inner_json decoded but the record didn't yield usable answer data
        # — either the outer ``isinstance(inner_data, list) and len > 0``
        # guard failed (dict, empty list, non-list) OR the inner
        # ``isinstance(first, list) and len > 0`` guard failed. In either
        # case we keep ``parseable = True`` and fall through to the next
        # item. Real-world ``wrb.fr`` heartbeats like ``"[]"`` hit this
        # branch and are deliberately still counted as parseable so a
        # heartbeats-only stream surfaces as "empty answer" rather than
        # "API drift" / ``ChatResponseParseError``.

    return _ChunkExtraction(
        references=refs,
        parseable=parseable,
        is_final_response=saw_final_envelope,
        turn_key=turn_key,
        next_steps=next_steps,
        terminal_sequence=terminal_sequence,
    )


def _raise_chat_rejection(error_payload: list) -> NoReturn:
    """Surface a ``wrb.fr`` request-rejection (status at item[5]) as a ``ChatError``.

    A streamed-chat ``wrb.fr`` frame with a null inner JSON (no answer) but a
    non-empty payload at item[5] is a server rejection — e.g. an over-long
    question yields ``["wrb.fr", None, None, None, None, [3]]`` (issue #1472),
    where ``[3]`` is a grpc-style status (``3`` == ``INVALID_ARGUMENT``). The
    caller has already run :func:`raise_if_rate_limited` for the richer
    ``UserDisplayableError`` shape, so this is the catch-all for the bare-status
    rejection that previously collapsed into the generic "no parseable chunks"
    error. The status is echoed so callers see the real failure. ``ErrorPayloadRow``
    centralises the ``error_payload[0]`` position (issue #1491).
    """
    row = ErrorPayloadRow(error_payload)
    status = row.status_code
    detail = f" (status {status!r})" if status is not None else ""
    # ``google.rpc.Status.message`` is the only server-authored text in this
    # envelope; the sentence below is this client's guess. Append the server's
    # own words when it sent any (#2188) rather than replacing the guidance:
    # the slot has never been observed populated, so nobody knows whether a
    # server message would be as actionable as the advice it displaced — a
    # terse "Invalid argument." would be a downgrade. The decoder's bare-status
    # path appends for the same reason.
    server_reason = row.message
    suffix = f" The server said: {server_reason}" if server_reason is not None else ""
    raise ChatError(
        f"Chat request was rejected by the server{detail}. "
        "This usually means the request was malformed or too large — most often "
        "an over-long question past the server-side size limit; shorten it and "
        f"try again.{suffix}"
    )


def _raise_chat_error_frame(item: list) -> NoReturn:
    """Surface a server-side ``"er"`` error frame as a ``ChatError``.

    The streamed batchexecute backend emits ``["er", rpc_id, code, ...]``
    frames when the RPC itself failed. The previous parser only inspected
    ``"wrb.fr"`` frames and silently skipped these, so a real server-side
    chat error collapsed into the generic ``ChatResponseParseError`` (or an
    empty answer). The optional ``code`` slot is read with an explicit length
    guard rather than ``safe_index`` (see the inline comment below): an absent
    code is normal for a short ``"er"`` frame and must not be treated as schema
    drift, since the frame is itself the error signal. The embedded code is
    echoed verbatim so callers see the actual failure instead of a generic
    parse error.
    """
    # The error code is optional enrichment — its absence must not be treated
    # as schema drift (an ``"er"`` frame is itself the error signal), so read
    # the slot via ``StreamFrameRow.error_code`` (length-guarded, not
    # ``safe_index``) which centralises the ``item[2]`` position (issue #1491).
    code = StreamFrameRow(item).error_code
    detail = f" (code {code!r})" if code is not None else ""
    raise ChatError(
        f"Chat request failed: the server returned an error frame{detail}. "
        "This usually means the request was rejected or the conversation "
        "could not be served; try again."
    )


def raise_if_rate_limited(error_payload: list) -> None:
    """Raise ``ChatError`` if the payload contains a UserDisplayableError."""
    try:
        # Structure: [8, None, [["type.googleapis.com/.../UserDisplayableError", ...]]]
        # ``ErrorPayloadRow`` centralises the ``error_payload[2]`` entries read
        # and the per-entry ``entry[0]`` type-string read (issue #1491).
        row = ErrorPayloadRow(error_payload)
        for entry in row.entries:
            entry_type = ErrorPayloadRow.entry_type(entry)
            if entry_type is not None and "UserDisplayableError" in entry_type:
                # Append the server's ``google.rpc.Status.message`` when it
                # sent one, keeping the client-authored remedy (#2188). No
                # recorded sample carries one, so the sentence below is what
                # users see today.
                server_reason = row.message
                suffix = f" The server said: {server_reason}" if server_reason else ""
                raise ChatError(
                    "Chat request was rate limited or rejected by the API. "
                    f"Wait a few seconds and try again.{suffix}"
                )
    except ChatError:
        raise
    except Exception:
        logger.debug(
            "Could not parse chat error payload; continuing with empty-answer handling",
            exc_info=True,
        )


def parse_citations(first: list, document: StructuredDocument | None = None) -> list[ChatReference]:
    """Parse citation details from a streamed-chat response structure.

    Absence-vs-malformed policy (#1505 continuity). Citations are *secondary*
    payload riding on a usable answer, so loudness is tiered:

    * **Absence is silent** — an answer with no citations is the common case
      (real traffic routinely sends ``None`` in the ``first[4][3]`` slot):
      no/short type block and falsy citation slots return ``[]`` with zero
      logging, via ``AnswerRow.citations`` (issue #1491).
    * **Container drift RAISES** — a non-list ``first`` (the answer row) or a
      truthy non-list citation container is structural wire drift; it raises
      :class:`UnknownRPCMethodError`, matching this parser's existing raise
      for the ``inner_data[0]`` non-list case and the
      ``unwrap_conversation_turns`` container raise (#1505): a reshaped
      container means the payload can no longer be trusted, so it must not
      silently degrade to "answer without citations".
    * **Per-row malformed WARNS and skips** — a citation entry that is present
      but unusable (wrong shape/type at a slot, no extractable source id, or
      an unexpected error while decoding it) logs at least one bounded
      ``WARNING`` (``reprlib`` previews; a deep malformed source-id tree may
      additionally emit the UUID max-recursion warning), then drops only that
      row; surviving citations are still returned so one bad row never
      destroys a good answer's remaining citations.

    Survivors keep their **raw wire ordinal** as ``citation_number`` (1-based
    position in the citation container), NOT a dense re-count. The answer
    text's literal ``[N]`` markers refer to raw positions, so re-densifying
    after a skip would silently re-anchor ``[N]`` onto a *different* citation
    (e.g. save-as-note anchoring the wrong chunk). A skipped row instead
    leaves a hole: its marker resolves to no reference and downstream
    consumers drop that anchor rather than mis-anchoring. With nothing
    skipped, raw ordinals equal the dense numbering this parser always
    produced. The final assignment in :func:`parse_streaming_chat_response`
    preserves non-``None`` numbers, so the ordinals survive unchanged.

    ``document`` is the answer row's own parsed document, supplying the
    annotation map that stamps each survivor's answer-side range (#2120). The
    stream parser has already built it and passes it in rather than paying for
    a second parse of the same tree; omit it and this function parses the
    document itself.

    The pre-hardening behavior swallowed *every* citation drift at DEBUG and
    returned ``[]`` — a Google reshape degraded to "answers with no
    citations" invisibly.
    """
    if not isinstance(first, list):
        # Same structural-drift signal ``_extract_chunk_with_parseable``
        # raises for a non-list answer row; reachable only via direct calls
        # since the stream parser already enforces it before delegating here.
        raise UnknownRPCMethodError(
            f"Streamed chat answer row is not a list (got {type(first).__name__})",
            method_id=None,
            path=(0,),
            source=_CITATION_SOURCE,
            data_at_failure=reprlib.repr(first),
        )
    refs: list[ChatReference] = []
    for raw_idx, cite in enumerate(AnswerRow(first).citations, start=1):
        try:
            ref = parse_single_citation(cite)
        except (IndexError, TypeError, AttributeError) as exc:
            # These three cover the current call graph: parse_single_citation
            # and its CitationRow/CitationDetail adapters use length-guarded
            # positional access throughout (no dict access, no int()/explicit
            # raises), so ValueError/KeyError are unreachable. Revisit this
            # tuple if those adapters ever gain either.
            logger.warning(
                "Skipping malformed citation entry (%s: %s; cite=%s) [%s]",
                type(exc).__name__,
                exc,
                reprlib.repr(cite),
                _CITATION_SOURCE,
            )
            continue
        if ref is None:
            logger.warning(
                "Skipping unusable citation entry (no parsable detail or source id; cite=%s) [%s]",
                reprlib.repr(cite),
                _CITATION_SOURCE,
            )
            continue
        # Raw wire ordinal, not a dense re-count — see the docstring: the
        # answer's literal [N] markers point at raw positions, so a skipped
        # row must leave a hole rather than shift survivors onto wrong markers.
        refs.append(replace(ref, citation_number=raw_idx))
    # Join the answer document's annotation map onto the surviving refs, by
    # object id rather than by position, so a skipped row cannot shift an
    # answer range onto its neighbour (#2120). The stream parser has already
    # built the document for its own use and passes it in; a direct caller gets
    # it parsed here.
    if document is None:
        document = AnswerRow(first).document
    return attach_answer_anchors(refs, document)


def parse_single_citation(cite: Any) -> ChatReference | None:
    """Parse a single citation entry into a ``ChatReference``."""
    # ``CitationRow`` centralises the ``cite[0][0]`` chunk-id and ``cite[1]``
    # detail-block position knowledge (issue #1491); a malformed entry yields
    # ``detail is None`` here, matching the old "skip unusable citation" guard.
    row = CitationRow(cite)
    detail = row.detail
    if detail is None:
        return None
    cite_inner = detail.raw_list

    source_id = extract_uuid_from_nested(detail.source_id_data)
    if source_id is None:
        return None

    chunk_id = row.chunk_id

    cited_text, start_char, end_char = extract_text_passages(cite_inner)
    fragment_start_char, fragment_end_char = extract_fragment_range(cite_inner)
    score = extract_score(cite_inner)

    return ChatReference(
        source_id=source_id,
        cited_text=cited_text,
        start_char=start_char,
        end_char=end_char,
        chunk_id=chunk_id,
        fragment_start_char=fragment_start_char,
        fragment_end_char=fragment_end_char,
        score=score,
    )


def extract_fragment_range(cite_inner: list) -> tuple[int | None, int | None]:
    """Extract the cited fragment's **source-side** character range.

    The server emits ``cite_inner[3] = [[None, start, end]]``: the union of
    every element range in the fragment at ``cite_inner[4][0]``, in the same
    coordinate space as ``start_char`` / ``end_char``.

    It is *not* an answer-text range. This client exposed it as
    ``answer_start_char`` / ``answer_end_char`` and documented it as one until
    #2120; a live capture on a 536-character answer returned ``[1130, 1695]``
    for its third citation, and that value equalled the union of that
    fragment's ten element ranges. The answer-side range comes from the answer
    document's annotation map instead — see :func:`attach_answer_anchors`.

    Returns ``(None, None)`` if either position is missing, not an int,
    a bool, negative, or if ``end < start`` — the two positions are
    semantically paired and one without the other is meaningless to
    downstream consumers.
    """
    # ``CitationDetail.fragment_range`` centralises the ``cite_inner[3][0]``
    # descent (``[None, start, end]``) and returns ``(None, None)`` for every
    # malformed shape the old inline guards rejected (issue #1491).
    start, end = CitationDetail(cite_inner).fragment_range()
    # bool is an int subclass in Python; reject it explicitly. Treat positions
    # as paired — one without the other (or invalid ordering) is unusable.
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
    ):
        return None, None
    if start < 0 or end < start:
        return None, None
    return start, end


def extract_score(cite_inner: list) -> float | None:
    """Extract the server-side relevance score (0.0-1.0) at ``cite_inner[2]``.

    Returns ``None`` for non-numeric values, booleans (``bool`` is an ``int``
    subclass in Python), non-finite floats (NaN, Inf), or values outside
    [0.0, 1.0]. The bound check keeps the contract documented on the field
    enforceable for downstream consumers.
    """
    # ``CitationDetail.raw_score`` centralises the ``cite_inner[2]`` read
    # (issue #1491); a short detail block yields ``None`` (no score).
    raw = CitationDetail(cite_inner).raw_score
    if raw is None:
        return None
    if isinstance(raw, bool):  # bool is a subclass of int in Python; reject
        return None
    if isinstance(raw, (int, float)):
        score = float(raw)
        if not math.isfinite(score) or not (0.0 <= score <= 1.0):
            return None
        return score
    return None


def extract_text_passages(cite_inner: list) -> tuple[str | None, int | None, int | None]:
    """Extract the cited fragment's text and its source-side character range.

    The fragment lives two levels down, at ``cite_inner[4][0]``:
    ``cite_inner[4]`` is the ``TailwindDocFragment`` *message*, and its
    ``elements`` list is one level below that. Before #2120 the descent stopped
    at ``cite_inner[4]``, so the loop iterated the single wrapper instead of the
    fragment's blocks and ``cited_text`` was truncated to whatever the first
    block held — 37 of 556 characters in the live capture that motivated the
    fix.

    Both levels of that fix had to land together. Descending to
    ``cite_inner[4][0]`` while still unwrapping each element's ``[0]`` (the
    pre-#2120 ``PassageRow`` behaviour) makes things strictly *worse* rather
    than better: an element's ``[0]`` is its ``startIndex`` — an ``int``, not a
    nested record — so every element fails the well-formedness check, all are
    skipped, and ``cited_text`` becomes ``None`` for every citation. The
    elements are ``StructuralElement`` rows and are decoded as such, by the
    document adapters shared with the source-content path (#2128).

    ``cited_text`` is what the fragment actually says: its blocks' text
    concatenated, with no separators, no stripping and no filler. It is
    deliberately *not* read back through
    :meth:`~notebooklm.types.StructuredDocument.slice`, which pads undecoded
    positions — the right trade for "what is at these offsets", the wrong one
    for "what did the model quote", where a run of placeholder characters is
    worse than a shorter string.

    **Its length is not a contract, and no string here is a length oracle.**
    It equals ``end_char - start_char`` only for an all-prose, all-BMP
    fragment, and diverges three ways otherwise: it counts Python characters
    where the range counts UTF-16 code units (an astral character costs one);
    it runs short when the fragment spans positions this client does not render
    as text (see :class:`~notebooklm.types.BlockKind`); and it can run long
    because nothing forces a span's text length to match its declared range —
    this repo's own VCR cassettes induce exactly that by scrubbing names to a
    fixed-width placeholder without adjusting the recorded offsets. When the
    width matters, take it from ``end_char - start_char``; ``document.slice()``
    returns exactly that range, but it too counts Python characters, so it is
    ``utf16_len()`` of the slice that equals the range, never ``len()``.
    ``None`` when the fragment decoded no blocks at all (a structural-anchor
    citation).

    ``start_char`` / ``end_char`` span the whole fragment — the union of every
    block's range, independently derived from the same blocks the server's own
    ``cite_inner[3]`` union covers — and are treated as a semantically paired
    range, so a fragment with no usable blocks reports ``(None, None)`` rather
    than a half-populated range the :class:`ChatReference` invariant would
    reject.
    """
    # ``CitationDetail.fragment_elements`` centralises the two-level
    # ``cite_inner[4][0]`` descent; ``build_blocks`` owns the per-element
    # ``[startIndex, endIndex, paragraph]`` reads.
    blocks = build_blocks(CitationDetail(cite_inner).fragment_elements)
    if not blocks:
        return None, None, None

    # The union of the blocks' ranges — the same quantity the server declares
    # at ``cite_inner[3]``, computed independently so the two can be compared.
    start_char = min(block.start_index for block in blocks)
    end_char = max(block.end_index for block in blocks)
    # Merge by offset, trimming what an earlier block already covered — the
    # same rule the structured-document layout applies, rather than a fourth
    # mechanism. Overlapping blocks would otherwise be concatenated whole,
    # yielding a cited_text wider than the range it reports (and, downstream, a
    # save-as-note local passage wider than its own source span). Genuine gaps
    # stay omitted: this is the readable value, not the offset-faithful one.
    cited_text = ""
    cursor = start_char
    for block in blocks:
        if block.end_index <= cursor:
            continue
        text = block.text
        if block.start_index < cursor:
            text = _utf16_slice(text, cursor - block.start_index, utf16_len(text))
        cited_text += text
        cursor = block.end_index
    return (cited_text or None), start_char, end_char


def attach_answer_anchors(
    refs: list[ChatReference], document: StructuredDocument
) -> list[ChatReference]:
    """Stamp each reference with the answer range its citation supports (#2120).

    The answer's own document carries an annotation map — ``Body``'s
    ``inlineObjectLocations`` — whose entries pair a document-object id with a
    range of the answer. That object id is the citation's own
    ``DocumentObject.objectId``, which this client already surfaces as
    ``ChatReference.chunk_id``, so the join is by id rather than by position:
    a skipped malformed citation cannot silently shift a range onto its
    neighbour.

    Ranges index ``document.text``, **not** the answer string — the answer
    carries markdown emphasis and inline ``[N]`` markers the document does not.
    A reference with no matching annotation keeps ``None`` on both fields.
    When one object id carries several annotations (the backend may anchor a
    citation in more than one place), the first in document order wins; the
    full set stays available via
    :meth:`~notebooklm.types.StructuredDocument.annotations_for`.

    Never mutates its inputs. Returns a fresh list when there is anything to
    stamp; an empty annotation map short-circuits and hands back the caller's
    own list, so the common "answer without a document" path allocates nothing.
    """
    if not document.annotations:
        return refs
    # Only anchors the decoded document can actually resolve. An annotation
    # whose range runs past the document's own extent is ordered and
    # non-negative — so it survives ``AnnotationEntryRow`` — but resolving it
    # yields an empty or truncated string, which reaches the caller as a
    # citation anchored to nothing rather than as the absent anchor it is.
    # ``None`` on both fields is the honest report (#2120).
    extent = utf16_len(document.text)
    by_object_id: dict[str, DocumentAnnotation] = {}
    for entry in document.annotations:
        if entry.end_index > extent:
            logger.warning(
                "Answer annotation for %s claims [%d, %d) beyond the answer "
                "document's %d-unit extent; leaving the reference unanchored [%s]",
                entry.object_id,
                entry.start_index,
                entry.end_index,
                extent,
                _CITATION_SOURCE,
            )
            continue
        by_object_id.setdefault(entry.object_id, entry)

    stamped: list[ChatReference] = []
    for ref in refs:
        anchor = by_object_id.get(ref.chunk_id) if ref.chunk_id else None
        if anchor is None:
            stamped.append(ref)
            continue
        stamped.append(
            replace(
                ref,
                answer_anchor_start=anchor.start_index,
                answer_anchor_end=anchor.end_index,
            )
        )
    return stamped


def extract_uuid_from_nested(data: Any, max_depth: int = 10) -> str | None:
    """Recursively extract a UUID from nested list structures."""
    if max_depth <= 0:
        logger.warning("Max recursion depth reached in UUID extraction")
        return None

    if data is None:
        return None

    if isinstance(data, str):
        return data if _UUID_PATTERN.match(data) else None

    if isinstance(data, list):
        for item in data:
            result = extract_uuid_from_nested(item, max_depth - 1)
            if result is not None:
                return result

    return None


def _extract_next_turn_content(next_turn: Any) -> str | None:
    """Extract the response content from a streaming-chat next_turn frame.

    The ``khqZz`` (``GET_CONVERSATION_TURNS``) response packs each AI answer
    as ``turn[4][0][0]`` — three nested wrappers around the answer text. The
    descent goes through :func:`safe_index` under strict decoding (the only
    mode since the ``NOTEBOOKLM_STRICT_DECODE=0`` opt-out was retired in
    v0.7.0; rationale in ADR-0011): a genuine descent failure raises
    :class:`~notebooklm.exceptions.UnknownRPCMethodError` so callers fail
    fast on Google-side shape drift.

    ``next_turn`` is a validated answer row (a list with ``len > 4`` and the
    answer role code — see ``ConversationTurnRow.is_answer``). Returns the
    answer-text string, or ``None`` when the leaf descends successfully to a
    non-string value (the caller's empty-answer fallback).
    """
    content = safe_index(
        next_turn,
        4,
        0,
        0,
        method_id=RPCMethod.GET_CONVERSATION_TURNS.value,
        source="_chat._extract_next_turn_content",
    )
    if not isinstance(content, str):
        # A non-string leaf at a structurally-valid path is normalised to
        # ``None`` so the caller's empty-answer fallback fires uniformly. This
        # is distinct from shape drift, which safe_index raises on.
        logger.debug(
            "next_turn content is not a string (type=%s); treating as drift",
            type(content).__name__,
        )
        return None
    return content
