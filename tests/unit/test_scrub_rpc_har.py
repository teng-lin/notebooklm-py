"""Unit tests for ``scripts/scrub_rpc_har.py``.

Pins the safety contract: the tool reads ONLY the request ``f.req`` field and the
response body (never the ``headers``/``cookies`` arrays) and redacts every string
leaf while preserving the structural constants that carry the wire-format signal.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scrub_rpc_har.py"
_spec = importlib.util.spec_from_file_location("scrub_rpc_har", _SCRIPT)
assert _spec is not None and _spec.loader is not None
har = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(har)


def test_redact_strings_keep_structure() -> None:
    assert har._redact(["Title", None, [2], [1]]) == ["<str:5>", None, [2], [1]]
    assert har._redact(42) == 42 and har._redact(None) is None and har._redact(True) is True


def test_redact_redacts_dict_keys_not_just_values() -> None:
    # A decoded object's KEYS come off the wire too — they must be redacted, or
    # {"user@gmail.com": ...} would leak the email through the key.
    assert har._redact({"user@gmail.com": "x"}) == {"<str:14>": "<str:1>"}


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
    assert red == [f"<str:{len(secret_html)}>", ["<str:6>", 7, None]]


def test_unredacted_completeness_check() -> None:
    # A fully-redacted structure passes (no surviving raw string → None);
    # dict keys are <str:N> too, since _redact redacts keys.
    assert har._unredacted(["<str:7>", None, [2], {"<str:1>": "<str:3>"}]) is None
    # int / bool / None are structural constants — never tripped.
    assert har._unredacted([3, True, None, [5]]) is None
    # Any raw string leaf is caught regardless of its shape — the check is
    # shape-AGNOSTIC, so it needs no credential-format knowledge to catch these.
    for raw in (
        "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ01",
        "SNlM0e",
        "person@example.com",
        "g.a000ABCDEF",
        "an ordinary unredacted string",
        "",
    ):
        assert har._unredacted([1, ["<str:2>", raw]]) == raw
    # A raw dict KEY must be caught too — the guard walks keys, not just values.
    assert har._unredacted({"raw@key.com": "<str:1>"}) == "raw@key.com"


def _chunk(frame: list) -> str:
    s = json.dumps([frame])
    return f"{len(s)}\n{s}\n"


def _write_har(tmp_path: Path, freq: str, body: str) -> str:
    har_doc = {
        "log": {
            "entries": [
                {
                    "request": {
                        "url": "https://notebooklm.google.com/_/batchexecute?rpcids=CCqFvf",
                        "headers": [{"name": "cookie", "value": "SID=g.a000SECRET"}],
                        "postData": {"text": f"f.req={freq}&at=AOsecretCSRF"},
                    },
                    "response": {"status": 200, "content": {"text": body}},
                }
            ]
        }
    }
    path = tmp_path / "capture.har"
    path.write_text(json.dumps(har_doc), encoding="utf-8")
    return str(path)


def test_main_prefers_populated_frame_over_null_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A streamed null placeholder before the populated frame for the same rpcid
    must not win — the populated result is what gets reported."""
    freq = json.dumps([[["CCqFvf", '["t",null,null,[2],[1]]', None, "generic"]]])
    body = (
        ")]}'\n"
        + _chunk(["wrb.fr", "CCqFvf", None, None, None, None, "generic"])  # null placeholder first
        + _chunk(["wrb.fr", "CCqFvf", json.dumps([["nb-id"]]), None, None, None, "generic"])
    )
    monkeypatch.setattr("sys.argv", ["scrub_rpc_har.py", _write_har(tmp_path, freq, body)])
    assert har.main() == 0
    out = capsys.readouterr().out
    assert 'result=[["<str:5>"]]' in out and "result=null" not in out
    # End-to-end: no secret from cookies / at= survives.
    assert "SECRET" not in out and "g.a000" not in out
