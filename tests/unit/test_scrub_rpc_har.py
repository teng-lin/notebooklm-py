"""Unit tests for ``scripts/scrub_rpc_har.py``.

Pins the safety contract: the tool reads ONLY the request ``f.req`` field and the
response body (never the ``headers``/``cookies`` arrays) and redacts every string
leaf while preserving the structural constants that carry the wire-format signal.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scrub_rpc_har.py"
_spec = importlib.util.spec_from_file_location("scrub_rpc_har", _SCRIPT)
assert _spec is not None and _spec.loader is not None
har = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(har)


def test_redact_strings_keep_structure() -> None:
    assert har._redact(["Title", None, [2], [1]]) == ["<str:5>", None, [2], [1]]
    assert har._redact(42) == 42 and har._redact(None) is None and har._redact(True) is True


def test_req_freq_from_text_ignores_at() -> None:
    entry = {"request": {"postData": {"text": "f.req=%5B%5D&at=AOsecretCSRF%3A1700"}}}
    assert har._req_freq(entry) == "[]"
    assert "secret" not in (har._req_freq(entry) or "")


def test_req_freq_from_params_never_reads_at_or_cookies() -> None:
    entry = {
        "request": {
            # cookies live in headers — the tool must never look here
            "headers": [{"name": "cookie", "value": "SID=g.a000SECRET"}],
            "postData": {
                "params": [
                    {"name": "f.req", "value": '[[["CCqFvf"]]]'},
                    {"name": "at", "value": "AOsecretCSRF"},
                ]
            },
        }
    }
    out = har._req_freq(entry)
    assert out == '[[["CCqFvf"]]]'
    assert "SECRET" not in out and "secret" not in out


def test_iter_request_calls() -> None:
    freq = json.dumps([[["CCqFvf", '["t",null,null,[2],[1]]', None, "generic"]]])
    assert list(har._iter_request_calls(freq)) == [("CCqFvf", ["t", None, None, [2], [1]])]


def test_response_frames_extracts_error_and_result() -> None:
    def chunk(frame: list) -> str:
        s = json.dumps([frame])
        return f"{len(s)}\n{s}\n"

    body = (
        ")]}'\n"
        + chunk(["wrb.fr", "CCqFvf", None, None, None, [3], "generic"])
        + chunk(["wrb.fr", "izAoDd", json.dumps([["id", "title"]]), None, None, None, "generic"])
    )
    frames = {
        rpcid: (result, err)
        for rpcid, result, err in har._response_frames({"response": {"content": {"text": body}}})
    }
    assert frames["CCqFvf"] == (None, [3])  # rejected with status 3, null result
    assert frames["izAoDd"][0] == [["id", "title"]] and frames["izAoDd"][1] is None


def test_html_in_response_result_is_redacted() -> None:
    """A response whose result carries an HTML blob (the WIZ_global_data class)
    with an API key + CSRF + email must be fully redacted to <str:N>."""
    secret_html = (
        "<div>AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456 "
        "SNlM0e:secretcsrf user@gmail.com g.a000ABCDEF</div>"
    )
    red = har._redact([secret_html, ["nested", 7, None]])
    rendered = json.dumps(red)
    for token in ("AIza", "SNlM0e", "user@gmail.com", "g.a000", "nested"):
        assert token not in rendered
    assert red == ["<str:%d>" % len(secret_html), ["<str:6>", 7, None]]


def test_leak_net_matches_page_html_credential_shapes() -> None:
    for token in (
        "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ01",
        "SNlM0e",
        "g.a000ABCDEF",
        "person@example.com",
        "LSID=abc",
        "1//0abcdefghijklmnopqrstuv",
    ):
        assert har._LEAK.search(token), token
    # A clean redacted line must NOT trip the net.
    assert not har._LEAK.search('CCqFvf  request : ["<str:7>",null,[2],[1]]')
