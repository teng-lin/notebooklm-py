"""Unit tests for chat-domain exception contracts.

Pins the PR-D (audit I4) failure-mode contract for
:func:`notebooklm._chat_protocol.parse_streaming_chat_response`:

* Zero parseable chunks (empty body, garbage, or API wire drift) →
  raise :class:`notebooklm.exceptions.ChatResponseParseError`.
* At least one parseable chunk but no extracted answer (the model
  legitimately returned an empty answer) → return
  :class:`StreamingChatParseResult` with ``answer == ""``.
"""

from __future__ import annotations

import json

import pytest

from notebooklm._chat_protocol import (
    StreamingChatParseResult,
    parse_streaming_chat_response,
)
from notebooklm.exceptions import ChatError, ChatResponseParseError, NotebookLMError


def _wire_chunk(text: str, *, marked: bool = True) -> str:
    """Build a length-prefixed streaming response with a single ``wrb.fr`` chunk.

    Mirrors the shape that ``_chunk`` / ``_length_prefixed`` in
    ``test_streaming_chat_protocol.py`` produce — kept inline here so this
    file stays self-contained.
    """
    marker = 1 if marked else 0
    type_info = [[], None, None, [], marker]
    inner_json = json.dumps([[text, None, None, None, type_info]])
    envelope = json.dumps([["wrb.fr", None, inner_json]])
    return f")]}}'\n{len(envelope)}\n{envelope}\n"


# ---------------------------------------------------------------------------
# Zero-parseable-chunks → raise
# ---------------------------------------------------------------------------


def test_streaming_empty_raises() -> None:
    """Empty response body → no ``wrb.fr`` envelope parsed → raise."""
    with pytest.raises(ChatResponseParseError) as raised:
        parse_streaming_chat_response("")

    assert "No parseable chunks" in str(raised.value)


def test_streaming_xssi_only_raises() -> None:
    """Body containing only the XSSI prefix has zero parseable chunks."""
    with pytest.raises(ChatResponseParseError):
        parse_streaming_chat_response(")]}'\n")


def test_streaming_garbage_text_raises() -> None:
    """Non-JSON garbage in the body yields zero parseable chunks → raise."""
    with pytest.raises(ChatResponseParseError):
        parse_streaming_chat_response("not json at all\nmore garbage\n")


def test_streaming_non_wrb_fr_envelope_raises() -> None:
    """A well-formed JSON envelope that is NOT ``wrb.fr`` is unparseable."""
    other = json.dumps([["other.envelope", None, "{}"]])
    body = f")]}}'\n{len(other)}\n{other}\n"
    with pytest.raises(ChatResponseParseError):
        parse_streaming_chat_response(body)


def test_chat_response_parse_error_is_chat_error_subclass() -> None:
    """Existing ``except ChatError`` clauses must still catch it.

    This pins the inheritance contract — :class:`ChatResponseParseError`
    MUST inherit (transitively) from :class:`ChatError`, not directly
    from :class:`NotebookLMError`. Existing chat-domain catches keep
    working without code changes.
    """
    assert issubclass(ChatResponseParseError, ChatError)
    assert issubclass(ChatResponseParseError, NotebookLMError)
    try:
        parse_streaming_chat_response("")
    except ChatError as exc:
        assert isinstance(exc, ChatResponseParseError)
    else:  # pragma: no cover
        pytest.fail("ChatResponseParseError did not propagate as ChatError")


# ---------------------------------------------------------------------------
# Chunks-parsed-but-empty-answer → return empty (the other half of the contract)
# ---------------------------------------------------------------------------


def test_streaming_parseable_chunk_with_empty_answer_returns_empty() -> None:
    """A parseable ``wrb.fr`` envelope with empty answer text is NOT a parse
    failure — it's a legitimate empty answer from the model. The parser
    must return an empty result, not raise.
    """
    body = _wire_chunk("")  # parseable envelope, empty answer text
    result = parse_streaming_chat_response(body)
    assert result == StreamingChatParseResult("", [], None)


def test_streaming_parseable_chunk_with_non_string_answer_returns_empty() -> None:
    """Envelope decodes but ``first[0]`` is not a string — still parseable,
    still an empty-answer return path (not a raise).
    """
    inner = json.dumps([[None, None, None, None, [[], None, None, [], 1]]])
    env = json.dumps([["wrb.fr", None, inner]])
    body = f")]}}'\n{len(env)}\n{env}\n"
    result = parse_streaming_chat_response(body)
    assert result.answer == ""
