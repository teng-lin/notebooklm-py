"""Privacy boundary tests for the Web Play Books VCR hooks."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

from tests._helpers.play_book_http_cassette import (
    SYNTHETIC_BOOK_ROWS,
    PlayBookHttpCassetteScrubber,
)


def _live_add_body() -> str:
    source_spec = [None] * 16
    source_spec[0] = "PRIVATE_UNMODELED_ACCOUNT_DATA"
    source_spec[10] = 1
    source_spec[15] = [
        1,
        "PRIVATE_CONTENT_ID",
        "Private library title",
        "Private library description",
        "https://private.example/cover",
        4.9,
        ["Private author"],
    ]
    args = [[source_spec], "11111111-1111-4111-8111-111111111111", [2]]
    envelope = [[["X1snv", json.dumps(args), None, "generic"]]]
    return urlencode({"f.req": json.dumps(envelope), "at": "SCRUBBED_CSRF:1"})


def test_play_book_request_scrubber_replaces_private_library_fields() -> None:
    request = SimpleNamespace(
        uri="https://notebook.google.com/batchexecute?rpcids=X1snv",
        body=_live_add_body(),
        headers={},
    )

    PlayBookHttpCassetteScrubber().scrub_request(request)

    assert "PRIVATE_CONTENT_ID" not in request.body
    assert "Private library" not in request.body
    assert "PRIVATE_UNMODELED_ACCOUNT_DATA" not in request.body
    outer = json.loads(parse_qs(request.body)["f.req"][0])
    args = json.loads(outer[0][0][1])
    assert args[0][0][15][1] == "EIBOOK00000001"
    assert args[0][0][15][2] == "Placeholder ebook 001"
    assert args[1] == "00000000-0000-4000-8000-000000000001"


def test_play_book_response_scrubber_replaces_the_entire_private_library() -> None:
    private_result = json.dumps(
        [[["PRIVATE_CONTENT_ID", 1, "Private library title", "Private description"]]]
    )
    payload = json.dumps([["wrb.fr", "mVtEUb", private_result]])
    live_body = f")]}}'\n\n1\n{payload}\n"
    response = {
        "body": {"string": live_body},
        "headers": {"content-length": [str(len(live_body.encode()))]},
        "status": {"code": 200, "message": "OK"},
    }

    PlayBookHttpCassetteScrubber().scrub_response(response)

    clean_body = response["body"]["string"]
    assert "PRIVATE_CONTENT_ID" not in clean_body
    assert "Private library" not in clean_body
    payload = next(line for line in clean_body.splitlines() if line.startswith("[["))
    frame = json.loads(payload)
    assert json.loads(frame[0][2]) == [SYNTHETIC_BOOK_ROWS]
    assert response["headers"]["content-length"] == [str(len(clean_body.encode()))]


def test_play_book_response_scrubber_replaces_the_private_add_response() -> None:
    private_stub = [["PRIVATE_SOURCE_ID"], "7", ["PRIVATE_UNMODELED_DATA", None, None, None, 20]]
    private_result = json.dumps([[private_stub], None, [[private_stub, 0]]])
    payload = json.dumps([["wrb.fr", "X1snv", private_result]])
    live_body = f")]}}'\n\n1\n{payload}\n"
    response = {
        "body": {"string": live_body},
        "headers": {"content-length": [str(len(live_body.encode()))]},
        "status": {"code": 200, "message": "OK"},
    }

    PlayBookHttpCassetteScrubber().scrub_response(response)

    clean_body = response["body"]["string"]
    assert "PRIVATE_SOURCE_ID" not in clean_body
    assert "PRIVATE_UNMODELED_DATA" not in clean_body
    payload = next(line for line in clean_body.splitlines() if line.startswith("[["))
    frame = json.loads(payload)
    result = json.loads(frame[0][2])
    assert result[0][0][0][0] == "00000000-0000-4000-8000-000000000002"
    assert result[0][0][1] == "Placeholder ebook 001"
    assert response["headers"]["content-length"] == [str(len(clean_body.encode()))]
