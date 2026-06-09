"""Streamed-chat wire mechanics for NotebookLM private chat calls.

This module owns only streamed-chat wire request construction and response
parsing. Conversation flow, caching, source resolution, and ``AskResult``
construction stay in :mod:`notebooklm._chat`.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, replace
from typing import Any, NoReturn, Protocol
from urllib.parse import quote, urlencode

from .._env import get_default_bl, get_default_language
from .._row_adapters.chat import (
    AnswerRow,
    CitationDetail,
    CitationRow,
    ErrorPayloadRow,
    PassageRow,
    StreamFrameRow,
    TextLeafRow,
)
from ..auth import format_authuser_value
from ..exceptions import ChatError, ChatResponseParseError, UnknownRPCMethodError
from ..rpc._safe_index import safe_index
from ..rpc.decoder import strip_anti_xssi
from ..rpc.encoder import nest_source_ids
from ..rpc.types import get_query_url
from ..types import ChatReference

# Deliberate: use the ``notebooklm._chat`` logger namespace (not this module's)
# so existing log filters keep matching the chat parser diagnostics.
logger = logging.getLogger("notebooklm._chat")

# ``safe_index`` source labels for the streamed-chat descents. The streamed
# chat endpoint (``GenerateFreeFormStreamed``) is not a batchexecute RPC, so
# there is no obfuscated method ID to thread — descents pass ``method_id=None``
# and rely on these labels to localize schema drift in raised
# ``UnknownRPCMethodError`` diagnostics (ADR-0011).
_CHUNK_SOURCE = "_chat_wire._extract_chunk_with_parseable"

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class AuthSnapshotLike(Protocol):
    """Structural auth snapshot accepted by streamed-chat request builders."""

    @property
    def csrf_token(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def authuser(self) -> int: ...

    @property
    def account_email(self) -> str | None: ...


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


def build_streaming_chat_request(
    *,
    snapshot: AuthSnapshotLike,
    notebook_id: str,
    question: str,
    source_ids: list[str],
    conversation_history: list | None,
    conversation_id: str | None,
    reqid: int,
) -> tuple[str, str, dict[str, str]]:
    """Assemble ``(url, body, extra_headers)`` for one streamed-chat attempt.

    ``conversation_id=None`` tells the server to use the user's current
    conversation on this notebook, creating one if none exists. The
    server-recorded id is NOT returned in the streaming response — it
    must be recovered separately via ``hPTbtc``
    (``ChatAPI.get_conversation_id``) after the ask. Non-None values are
    follow-up asks and are forwarded verbatim into ``params[4]``.

    See issue #659 for the bug class that motivated this contract.
    """
    sources_array = nest_source_ids(source_ids, 2)

    params: list[Any] = [
        sources_array,
        question,
        conversation_history,
        [2, None, [1], [1]],
        conversation_id,
        None,  # [5] - always null
        None,  # [6] - always null
        notebook_id,  # [7] - required for server-side conversation persistence
        1,  # [8] - always 1
    ]

    params_json = json.dumps(params, separators=(",", ":"))
    f_req_json = json.dumps([None, params_json], separators=(",", ":"))
    encoded_req = quote(f_req_json, safe="")

    body_parts = [f"f.req={encoded_req}"]
    if snapshot.csrf_token:
        encoded_at = quote(snapshot.csrf_token, safe="")
        body_parts.append(f"at={encoded_at}")
    body = "&".join(body_parts) + "&"

    url_params: dict[str, str] = {
        "bl": get_default_bl(),
        "hl": get_default_language(),
        "_reqid": str(reqid),
        "rt": "c",
    }
    if snapshot.session_id:
        url_params["f.sid"] = snapshot.session_id
    if snapshot.account_email or snapshot.authuser:
        url_params["authuser"] = format_authuser_value(
            snapshot.authuser,
            snapshot.account_email,
        )

    url = f"{get_query_url()}?{urlencode(url_params)}"
    return url, body, {}


def parse_streaming_chat_response(response_text: str) -> StreamingChatParseResult:
    """Parse a streamed-chat response into answer, references, and conversation ID.

    Failure contract (see :class:`notebooklm.exceptions.ChatResponseParseError`):

    * **Zero parseable chunks** — no chunk in the response yielded a
      successfully decoded ``wrb.fr`` envelope. This means either the
      response body was empty/garbage, or the API's wire format drifted
      and the parser no longer recognizes the envelope shape. Raises
      :class:`ChatResponseParseError`.
    * **Chunks parsed but empty answer** — at least one ``wrb.fr`` chunk
      decoded, but no chunk yielded answer text (the model legitimately
      returned an empty response). Returns
      ``StreamingChatParseResult("", refs, conv_id)`` — empty answer is
      a valid outcome, not a parse failure.
    """
    # Shared anti-XSSI stripper (rpc.decoder.strip_anti_xssi) is the single
    # owner of the )]}' prefix removal. For the real chat wire format the
    # prefix is always followed by a newline, so the subsequent ``.strip()``
    # yields a byte-for-byte-identical result to the prior blind ``[4:]`` slice.
    response_text = strip_anti_xssi(response_text)

    lines = response_text.strip().split("\n")
    best_marked_answer = ""
    best_marked_refs: list[ChatReference] = []
    best_unmarked_answer = ""
    best_unmarked_refs: list[ChatReference] = []
    server_conv_id: str | None = None
    parseable_chunk_count = 0

    def process_chunk(json_str: str) -> None:
        """Process a JSON chunk, updating best answer candidates and their refs."""
        nonlocal best_marked_answer, best_marked_refs
        nonlocal best_unmarked_answer, best_unmarked_refs
        nonlocal server_conv_id, parseable_chunk_count
        text, is_answer, refs, conv_id, parseable = _extract_chunk_with_parseable(json_str)
        if parseable:
            parseable_chunk_count += 1
        if text:
            if is_answer and len(text) > len(best_marked_answer):
                best_marked_answer = text
                best_marked_refs = refs
            elif not is_answer and len(text) > len(best_unmarked_answer):
                best_unmarked_answer = text
                best_unmarked_refs = refs
        if conv_id:
            server_conv_id = conv_id

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
        # No ``wrb.fr`` envelopes recognized — distinguishable from a
        # legitimate empty answer (which still produces at least one
        # parseable chunk). Raise so callers can distinguish wire-drift
        # / empty-body from "the model returned nothing."
        raise ChatResponseParseError(
            f"No parseable chunks in streaming chat response ({len(lines)} lines scanned). "
            "The response was empty or the API wire format may have changed."
        )

    if best_marked_answer:
        longest_answer = best_marked_answer
        final_refs = best_marked_refs
    elif best_unmarked_answer:
        logger.warning(
            "No marked answer found; falling back to longest unmarked "
            "text (%d chars). The API response format may have changed.",
            len(best_unmarked_answer),
        )
        longest_answer = best_unmarked_answer
        final_refs = best_unmarked_refs
    else:
        longest_answer = ""
        final_refs = []

    if not longest_answer:
        logger.warning(
            "No answer extracted from response (%d lines parsed, %d parseable chunks)",
            len(lines),
            parseable_chunk_count,
        )

    # Assign citation numbers without mutating the dataclass instances in place
    # (prepares for an eventual ``frozen=True`` sweep on public domain types).
    # The list is rebuilt — externally identical to the prior mutation since
    # only ``citation_number`` ever changes here.
    final_refs = [
        replace(ref, citation_number=idx) if ref.citation_number is None else ref
        for idx, ref in enumerate(final_refs, start=1)
    ]

    return StreamingChatParseResult(longest_answer, final_refs, server_conv_id)


def extract_answer_and_refs_from_chunk(
    json_str: str,
) -> tuple[str | None, bool, list[ChatReference], str | None]:
    """Extract answer text, references, and conversation ID from one response chunk.

    Public 4-tuple wrapper around :func:`_extract_chunk_with_parseable`.
    The parseable-flag bit is internal-only — it exists for the streaming
    parser's "zero parseable chunks" detection and is not part of this
    module's outward-facing contract.
    """
    text, is_answer, refs, conv_id, _parseable = _extract_chunk_with_parseable(json_str)
    return text, is_answer, refs, conv_id


def _extract_chunk_with_parseable(
    json_str: str,
) -> tuple[str | None, bool, list[ChatReference], str | None, bool]:
    """Extract answer/refs/conv-id from one chunk and report wire-format parseability.

    The 5th element is True iff at least one ``wrb.fr`` envelope was
    found AND its inner JSON decoded successfully — regardless of whether
    any answer text was extracted. This lets the streaming parser
    distinguish two failure modes:

    * Zero parseable chunks → API drift or empty body (raise).
    * At least one parseable chunk but no text → real empty answer (return).
    """
    refs: list[ChatReference] = []

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return None, False, refs, None, False

    if not isinstance(data, list):
        return None, False, refs, None, False

    parseable = False
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

        if tag != "wrb.fr" or len(item) < 3:
            continue

        inner_json = frame.inner_json
        if not isinstance(inner_json, str):
            # item[2] is null — check item[5] for a server-side error payload.
            # Don't flip ``parseable`` here: a null inner_json without a
            # recognized error payload is not a successfully decoded
            # envelope. The error-payload path raises, so flow only
            # reaches the next iteration when item[5] was absent/unusable.
            error_payload = frame.error_payload
            if error_payload is not None:
                raise_if_rate_limited(error_payload)
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
                # ``_row_adapters/chat.py`` instead of scattered single-level
                # subscripts here (issue #1491). ``text`` is the load-bearing
                # answer leaf; an absent/empty/non-string leaf legitimately means
                # "no answer in this chunk" (heartbeat-ish), so fall through.
                answer = AnswerRow(first)
                text = answer.text
                if text is None:
                    continue

                refs = parse_citations(first)
                return text, answer.is_answer, refs, answer.server_conversation_id, parseable
        # inner_json decoded but the record didn't yield usable answer data
        # — either the outer ``isinstance(inner_data, list) and len > 0``
        # guard failed (dict, empty list, non-list) OR the inner
        # ``isinstance(first, list) and len > 0`` guard failed. In either
        # case we keep ``parseable = True`` and fall through to the next
        # item. Real-world ``wrb.fr`` heartbeats like ``"[]"`` hit this
        # branch and are deliberately still counted as parseable so a
        # heartbeats-only stream surfaces as "empty answer" rather than
        # "API drift" / ``ChatResponseParseError``.

    return None, False, refs, None, parseable


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
                raise ChatError(
                    "Chat request was rate limited or rejected by the API. "
                    "Wait a few seconds and try again."
                )
    except ChatError:
        raise
    except Exception:
        logger.debug(
            "Could not parse chat error payload; continuing with empty-answer handling",
            exc_info=True,
        )


def parse_citations(first: list) -> list[ChatReference]:
    """Parse citation details from a streamed-chat response structure."""
    try:
        # ``AnswerRow.citations`` centralises the ``first[4][3]`` descent and
        # degrades to ``[]`` for the "answer without citations" shapes the old
        # inline length guards tolerated (issue #1491).
        refs: list[ChatReference] = []
        for cite in AnswerRow(first).citations:
            ref = parse_single_citation(cite)
            if ref is not None:
                refs.append(ref)
        return refs
    except (IndexError, TypeError, AttributeError) as e:
        logger.debug(
            "Citation parsing failed (API structure may have changed): %s",
            e,
            exc_info=True,
        )
        return []


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
    answer_start_char, answer_end_char = extract_answer_range(cite_inner)
    score = extract_score(cite_inner)

    return ChatReference(
        source_id=source_id,
        cited_text=cited_text,
        start_char=start_char,
        end_char=end_char,
        chunk_id=chunk_id,
        answer_start_char=answer_start_char,
        answer_end_char=answer_end_char,
        score=score,
    )


def extract_answer_range(cite_inner: list) -> tuple[int | None, int | None]:
    """Extract the answer-text range that this citation supports.

    The server emits ``cite_inner[3] = [[None, answer_start, answer_end]]``
    pointing at the span of the answer string the citation backs. This is
    distinct from the source-side range in ``cite_inner[4]``.

    Returns ``(None, None)`` if either position is missing, not an int,
    a bool, negative, or if ``end < start`` — the two positions are
    semantically paired and one without the other is meaningless to
    downstream consumers.
    """
    # ``CitationDetail.answer_range`` centralises the ``cite_inner[3][0]``
    # descent (``[None, start, end]``) and returns ``(None, None)`` for every
    # malformed shape the old inline guards rejected (issue #1491).
    start, end = CitationDetail(cite_inner).answer_range()
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
    """Extract cited text and character positions from citation data.

    ``start_char`` and ``end_char`` are treated as a semantically paired range:
    if exactly one is present after walking all passages, both are dropped to
    ``None`` so the downstream :class:`ChatReference` paired-offset invariant
    never trips on a half-populated source range. The cited text (if any) is
    still returned.
    """
    # ``CitationDetail.passages`` centralises the ``cite_inner[4]`` descent and
    # ``PassageRow`` the per-passage ``passage_wrapper[0]`` / ``passage_data[0..2]``
    # reads (issue #1491); absent/short shapes degrade to ``[]`` / ``None``.
    texts: list[str] = []
    start_char: int | None = None
    end_char: int | None = None

    for passage_wrapper in CitationDetail(cite_inner).passages:
        passage = PassageRow(passage_wrapper)
        if not passage.is_well_formed:
            continue

        if start_char is None and isinstance(passage.start_char, int):
            start_char = passage.start_char
        if isinstance(passage.end_char, int):
            end_char = passage.end_char

        collect_texts_from_nested(passage.text_payload, texts)

    cited_text = " ".join(texts) if texts else None
    # Drop a half-populated range so the ChatReference invariant accepts it.
    # Also reject an inverted range (end before start) for the same reason.
    if (
        (start_char is None) != (end_char is None)
        or start_char is not None
        and end_char is not None
        and start_char > end_char
    ):
        start_char = None
        end_char = None
    return cited_text, start_char, end_char


def collect_texts_from_nested(nested: Any, texts: list[str]) -> None:
    """Collect text strings from deeply nested passage structure."""
    if not isinstance(nested, list):
        return

    for nested_group in nested:
        if not isinstance(nested_group, list):
            continue
        for inner in nested_group:
            # ``TextLeafRow`` centralises the ``inner[2]`` text-payload read and
            # the ``len(inner) >= 3`` well-formedness guard (issue #1491).
            leaf = TextLeafRow(inner)
            if not leaf.is_well_formed:
                continue
            text_val = leaf.text_value
            if isinstance(text_val, str) and text_val.strip():
                texts.append(text_val.strip())
            elif isinstance(text_val, list):
                for item in text_val:
                    if isinstance(item, str) and item.strip():
                        texts.append(item.strip())


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
