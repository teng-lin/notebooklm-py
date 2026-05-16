"""Integration-via-httpx-mock tests for saved-from-chat notes (issue #660).

These exercise the full ``NotebookLMClient.notes.create_from_chat()``
path: the encoder builds the 7-element params, ``ClientCore.rpc_call``
wraps them in the batchexecute envelope and POSTs to the server, and we
assert (a) the wire body matches our captured request byte-for-byte and
(b) the returned ``Note`` is parsed correctly from the captured response.

A VCR cassette would be the canonical fixture for this (per
``tests/cassettes/notes_create*.yaml``), but recording a fresh cassette
requires a live auth session against the real service. The captured
request/response pair under ``tests/unit/fixtures/`` is the next-best
thing — it carries the exact wire payload Google's web UI sends when
its "Save to note" button is clicked.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from pytest_httpx import HTTPXMock

from notebooklm.client import NotebookLMClient
from notebooklm.rpc import RPCMethod
from notebooklm.types import AskResult, ChatReference

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_request_params() -> list:
    return json.loads((FIXTURES_DIR / "save_chat_as_note_create_note_request.json").read_text())[
        "params"
    ]


def _load_response_note() -> list:
    return json.loads((FIXTURES_DIR / "save_chat_as_note_create_note_response.json").read_text())[
        "note"
    ]


def _build_ask_result_from_request_params(params: list) -> AskResult:
    """Reconstruct the ``AskResult`` the web UI would have started from
    so the encoder can re-produce exactly the captured request."""
    notebook_id = params[0]  # noqa: F841 — kept for parallel-fixture readability
    answer_text = params[1]
    passage = params[3][0]
    cited_text = passage[4][0][0][2][0][0][2][0]
    start_char = passage[3][0][1]
    end_char = passage[3][0][2]
    passage_id = passage[5][0][0][0]
    source_id = passage[5][0][1]
    chunk_id = passage[6][0]

    return AskResult(
        answer=answer_text,
        conversation_id="captured-conv-id",
        turn_number=1,
        is_follow_up=False,
        references=[
            ChatReference(
                source_id=source_id,
                citation_number=1,
                cited_text=cited_text,
                start_char=start_char,
                end_char=end_char,
                chunk_id=chunk_id,
                passage_id=passage_id,
            )
        ],
        raw_response="",
    )


@pytest.mark.asyncio
async def test_create_from_chat_wire_round_trip(
    auth_tokens,
    httpx_mock: HTTPXMock,
    build_rpc_response,
):
    """End-to-end: client.notes.create_from_chat() POSTs the captured
    wire format and parses the captured response into a Note."""
    request_params = _load_request_params()
    response_note = _load_response_note()
    notebook_id = request_params[0]
    requested_title = request_params[4]

    # Mock the CREATE_NOTE RPC response: server returns the note as a
    # single-element list (wrapped in the wrb.fr envelope by the helper).
    response_body = build_rpc_response(RPCMethod.CREATE_NOTE, [response_note])
    httpx_mock.add_response(content=response_body.encode())

    ask_result = _build_ask_result_from_request_params(request_params)

    async with NotebookLMClient(auth_tokens) as client:
        note = await client.notes.create_from_chat(notebook_id, ask_result, title=requested_title)

    # Assert the request body carried the exact captured params.
    request = httpx_mock.get_request()
    body = request.content.decode()
    form = parse_qs(body)
    f_req = form["f.req"][0]
    outer = json.loads(f_req)
    rpc_id, inner_params_json, _null, _generic = outer[0][0]
    assert rpc_id == RPCMethod.CREATE_NOTE.value
    actual_params = json.loads(inner_params_json)
    # Deep-equal first for a clear diff on failure, then byte-exact
    # string compare to catch boolean/int divergence that == would miss
    # (Python's `False == 0` quirk; the wire payload uses 0 not false).
    assert actual_params == request_params
    assert json.dumps(actual_params, separators=(",", ":")) == json.dumps(
        request_params, separators=(",", ":")
    )

    # Assert the Note dataclass was populated from the response.
    # Server returns a different (smart-generated) title in slot [4];
    # the returned Note reflects what the server stored, not what we
    # requested — see issue #660 PR description.
    assert note.id == response_note[0]
    assert note.title == response_note[4]
    assert note.notebook_id == notebook_id
    # Content is the answer text WITH [N] markers (rich anchors live server-side).
    assert note.content == ask_result.answer
