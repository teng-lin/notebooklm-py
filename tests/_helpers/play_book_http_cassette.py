"""Privacy-preserving VCR hooks for the Web Play Books add workflow."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlencode

from tests.vcr_config import ResourceIdCassetteScrubber

_LIST_RPC_ID = "mVtEUb"
_ADD_RPC_ID = "X1snv"
_SYNTHETIC_SOURCE_ID = "00000000-0000-4000-8000-000000000002"

SYNTHETIC_BOOK_ROWS: list[list[Any]] = [
    [
        "EIBOOK00000001",
        1,
        "Placeholder ebook 001",
        "<p>public-domain placeholder 001</p>",
        "https://play.google.com/books/publisher/content/images/frontcover/EIBOOK00000001",
        False,
        None,
        ["author 001"],
        4.5,
        [1],
    ],
    [
        "EIBOOK00000002",
        1,
        "Placeholder ebook 002",
        "<p>public-domain placeholder 002</p>",
        "https://play.google.com/books/publisher/content/images/frontcover/EIBOOK00000002",
        False,
        None,
        ["author 002"],
        4.2,
        [1],
    ],
    [
        "EIBOOK00000003",
        1,
        "Placeholder ebook 003",
        "<p>blocked placeholder 003</p>",
        "https://play.google.com/books/publisher/content/images/frontcover/EIBOOK00000003",
        True,
        1,
        ["author 003"],
        None,
        [1],
    ],
]

_SYNTHETIC_ADD_SPEC = [
    1,
    SYNTHETIC_BOOK_ROWS[0][0],
    SYNTHETIC_BOOK_ROWS[0][2],
    SYNTHETIC_BOOK_ROWS[0][3],
    SYNTHETIC_BOOK_ROWS[0][4],
    SYNTHETIC_BOOK_ROWS[0][8],
    SYNTHETIC_BOOK_ROWS[0][7],
]


def _set_content_length(headers: Any, length: int) -> None:
    for key in tuple(headers):
        if str(key).casefold() == "content-length":
            headers[key] = [str(length)] if isinstance(headers[key], list) else str(length)


def _synthetic_rpc_body(rpc_id: str, result: Any) -> str:
    result_text = json.dumps(result, separators=(",", ":"), ensure_ascii=True)
    payload = json.dumps(
        [["wrb.fr", rpc_id, result_text, None, None, None, "generic"]],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f")]}}'\n\n{len(payload.encode('utf-8'))}\n{payload}\n"


def _synthetic_list_body() -> str:
    return _synthetic_rpc_body(_LIST_RPC_ID, [SYNTHETIC_BOOK_ROWS])


def _synthetic_add_body() -> str:
    source_stub = [
        [_SYNTHETIC_SOURCE_ID],
        SYNTHETIC_BOOK_ROWS[0][2],
        [None, None, None, None, 20],
    ]
    return _synthetic_rpc_body(_ADD_RPC_ID, [[source_stub], None, [[source_stub, 0]]])


def _rewrite_add_body(body: str | bytes) -> str | bytes:
    as_bytes = isinstance(body, bytes)
    text = body.decode("utf-8") if as_bytes else body
    rewritten: list[tuple[str, str]] = []
    found = False
    for key, value in parse_qsl(text, keep_blank_values=True):
        if key != "f.req":
            rewritten.append((key, value))
            continue
        outer = json.loads(value)
        for batch in outer:
            for entry in batch:
                if not isinstance(entry, list) or not entry or entry[0] != _ADD_RPC_ID:
                    continue
                args = json.loads(entry[1])
                try:
                    source_spec = args[0][0]
                    expert_spec = source_spec[15]
                except (IndexError, TypeError) as exc:
                    raise ValueError("Unexpected Play Books add request shape") from exc
                if len(source_spec) != 16 or not isinstance(expert_spec, list):
                    raise ValueError("Unexpected Play Books add request shape")
                clean_source_spec: list[Any] = [None] * 16
                clean_source_spec[10] = 1
                clean_source_spec[15] = list(_SYNTHETIC_ADD_SPEC)
                args[0][0] = clean_source_spec
                entry[1] = json.dumps(args, separators=(",", ":"), ensure_ascii=True)
                found = True
        rewritten.append((key, json.dumps(outer, separators=(",", ":"), ensure_ascii=True)))
    if not found:
        raise ValueError("Play Books add cassette request did not contain X1snv")
    encoded = urlencode(rewritten)
    return encoded.encode("utf-8") if as_bytes else encoded


class PlayBookHttpCassetteScrubber:
    """Remove both resource IDs and account library details from a cassette."""

    def __init__(self) -> None:
        self._resource_ids = ResourceIdCassetteScrubber()

    def scrub_request(self, request: Any) -> Any:
        request = self._resource_ids.scrub_request(request)
        if request.uri and f"rpcids={_ADD_RPC_ID}" in request.uri:
            if not request.body:
                raise ValueError("Play Books add cassette request has no body")
            request.body = _rewrite_add_body(request.body)
        return request

    def scrub_response(self, response: dict[str, Any]) -> dict[str, Any]:
        response = self._resource_ids.scrub_response(response)
        body = response.get("body", {})
        content = body.get("string")
        if content is None:
            return response
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        if f'"{_LIST_RPC_ID}"' in text:
            synthetic = _synthetic_list_body()
        elif f'"{_ADD_RPC_ID}"' in text:
            synthetic = _synthetic_add_body()
        else:
            return response
        body["string"] = synthetic.encode("utf-8") if isinstance(content, bytes) else synthetic
        _set_content_length(response.get("headers", {}), len(synthetic.encode("utf-8")))
        return response


__all__ = ["PlayBookHttpCassetteScrubber", "SYNTHETIC_BOOK_ROWS"]
