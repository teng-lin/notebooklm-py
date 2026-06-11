"""Unit tests for ``scripts/scrub_rpc_capture.py``.

The scrubber must (a) read ONLY the ``f.req`` field — never the ``at=`` CSRF or
cookies — and (b) redact every string value while preserving the structural
constants that carry the wire-format signal. These tests pin both invariants.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scrub_rpc_capture.py"
_spec = importlib.util.spec_from_file_location("scrub_rpc_capture", _SCRIPT)
assert _spec is not None and _spec.loader is not None
scrub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scrub)


def test_find_freq_reads_only_freq_not_csrf() -> None:
    body = "f.req=%5B%5B%5B%22CCqFvf%22%5D%5D%5D&at=AOsecretCSRFtoken%3A1700000000000"
    assert scrub._find_freq(body) == '[[["CCqFvf"]]]'
    # The at= CSRF token is past the `&` boundary — never captured.
    assert "secret" not in (scrub._find_freq(body) or "")


def test_find_freq_ignores_cookies_in_curl() -> None:
    curl = (
        "curl 'https://notebooklm.google.com/.../batchexecute?rpcids=CCqFvf' "
        "-H 'cookie: SID=g.a000SECRET; SAPISID=SuperSecret' "
        "--data-raw 'f.req=%5B%5D&at=SECRET'"
    )
    out = scrub._find_freq(curl)
    assert out == "[]"
    assert "SECRET" not in out and "SAPISID" not in out


def test_redact_keeps_structure_redacts_strings() -> None:
    assert scrub._redact(["My Private Title", None, None, [2], [1]]) == [
        "<str:16>",
        None,
        None,
        [2],
        [1],
    ]
    # Numbers / null / bool are structural constants — kept verbatim.
    assert scrub._redact(42) == 42
    assert scrub._redact(None) is None
    assert scrub._redact(True) is True


def test_iter_calls_extracts_rpcid_and_inner() -> None:
    outer = [[["CCqFvf", '["t",null,null,[2],[1]]', None, "generic"]]]
    assert list(scrub._iter_calls(outer)) == [("CCqFvf", '["t",null,null,[2],[1]]')]
