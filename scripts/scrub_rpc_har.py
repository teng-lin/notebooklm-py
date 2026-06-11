#!/usr/bin/env python3
"""Scrub a DevTools **HAR** export down to NotebookLM's RPC payload SHAPES —
request *and* response — with no cookies, tokens, or free-text values.

Capture once in the browser (DevTools → Network → ⤓ **Export HAR** /
"Save all as HAR with content"), then::

    python scrub_rpc_har.py capture.har                 # all batchexecute calls
    python scrub_rpc_har.py capture.har --rpcid CCqFvf  # just one

For every ``/batchexecute`` call the tool reads ONLY the request body's ``f.req``
field and the response body — never the ``headers``/``cookies`` arrays (where
cookies, the ``at=`` CSRF token and ``Set-Cookie`` live) — and redacts every
string leaf to its length, keeping the structural constants that carry the
wire-format signal. Output is safe to paste into a bug report by construction.

For each RPC it prints:
  request  : the params the web UI sent
  response : HTTP status + the gRPC status code (e.g. [3]) and/or result shape

so you can see at a glance whether the server rejected the request (a payload
change) or returned a new result shape (a decode change).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import unquote

_LEAK = re.compile(r"SID=|SAPISID|__Secure-|ya29\.|AIza[0-9A-Za-z_-]{20}|SNlM0e")

# A tiny rpcid → friendly-name map (write path + common reads); unknowns show raw.
_NAMES = {
    "CCqFvf": "CREATE_NOTEBOOK",
    "izAoDd": "ADD_SOURCE",
    "o4cbdc": "ADD_SOURCE_FILE",
    "wXbhsf": "LIST_NOTEBOOKS",
    "rLM1Ne": "GET_NOTEBOOK",
    "CYK0Xb": "DELETE_NOTEBOOK",
    "cZsgsb": "CREATE_NOTE",
    "izh1Gb": "GENERATE",
    "Ljjv0c": "START_FAST_RESEARCH",
}


def _redact(node):
    if isinstance(node, str):
        return f"<str:{len(node)}>"
    if isinstance(node, list):
        return [_redact(x) for x in node]
    if isinstance(node, dict):
        return {k: _redact(v) for k, v in node.items()}
    return node  # int / float / bool / None — structural constants kept verbatim


def _decode_maybe_json(s):
    if isinstance(s, str) and s and s[0] in "[{":
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return s
    return s


def _iter_json_values(text):
    """Yield each top-level JSON value in a chunked batchexecute body."""
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] not in "[{":
            i += 1
        if i >= n:
            return
        try:
            val, end = dec.raw_decode(text, i)
            yield val
            i = end
        except json.JSONDecodeError:
            i += 1


def _req_freq(entry):
    pd = entry.get("request", {}).get("postData", {})
    for p in pd.get("params", []) or []:
        if p.get("name") == "f.req":
            return unquote(p.get("value", ""))
    m = re.search(r"f\.req=([^&]+)", pd.get("text", "") or "")
    return unquote(m.group(1)) if m else None


def _iter_request_calls(freq):
    try:
        outer = json.loads(freq)
    except (json.JSONDecodeError, TypeError):
        return
    level = outer[0] if isinstance(outer, list) and outer else None
    calls = level if isinstance(level, list) and level and isinstance(level[0], list) else [level]
    for call in calls or []:
        if isinstance(call, list) and len(call) >= 2 and isinstance(call[0], str):
            yield call[0], _decode_maybe_json(call[1])


def _response_frames(entry):
    """Yield (rpcid, result, error_code) from the chunked response body."""
    body = entry.get("response", {}).get("content", {}).get("text")
    if not isinstance(body, str):
        return
    for chunk in _iter_json_values(body):
        if not isinstance(chunk, list):
            continue
        for frame in chunk:
            if not (isinstance(frame, list) and frame and isinstance(frame[0], str)):
                continue
            if frame[0] == "wrb.fr" and len(frame) > 1 and isinstance(frame[1], str):
                result = _decode_maybe_json(frame[2]) if len(frame) > 2 else None
                err = frame[5] if len(frame) > 5 and frame[5] else None
                yield frame[1], result, err
            elif frame[0] == "er" and len(frame) > 1 and isinstance(frame[1], str):
                yield frame[1], None, frame[2:] or "error"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("har", help="path to a DevTools HAR export")
    ap.add_argument("--rpcid", help="only show this rpcid (e.g. CCqFvf)")
    args = ap.parse_args()

    try:
        with open(args.har, encoding="utf-8", errors="replace") as fh:
            har = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return _die(f"Could not read HAR: {e}")

    entries = [
        e
        for e in har.get("log", {}).get("entries", [])
        if "/batchexecute" in e.get("request", {}).get("url", "")
    ]
    if not entries:
        return _die("No /batchexecute requests found in the HAR.")

    blocks: list[str] = []
    for entry in entries:
        status = entry.get("response", {}).get("status", "?")
        # response frames indexed by rpcid
        resp = {}
        for rpcid, result, err in _response_frames(entry):
            resp.setdefault(rpcid, (result, err))
        for rpcid, params in _iter_request_calls(_req_freq(entry) or ""):
            if args.rpcid and rpcid != args.rpcid:
                continue
            name = _NAMES.get(rpcid, "")
            head = f"{rpcid}" + (f"  ({name})" if name else "")
            lines = [head, f"  request : {_dump(_redact(params))}"]
            if rpcid in resp:
                result, err = resp[rpcid]
                bits = [f"HTTP {status}"]
                if err is not None:
                    bits.append(f"status_code={_dump(err)}")
                bits.append(f"result={_dump(_redact(result))}")
                lines.append("  response: " + " | ".join(bits))
            else:
                lines.append(f"  response: HTTP {status} (body not in HAR — export 'with content')")
            blocks.append("\n".join(lines))

    if not blocks:
        return _die(
            "No matching RPC calls found" + (f" for rpcid {args.rpcid!r}." if args.rpcid else ".")
        )

    out = "\n\n".join(blocks)
    if _LEAK.search(out):  # impossible by construction — fail closed if it ever happens
        return _die("Refusing to print: a credential-shaped token survived. Please report this.")

    print(
        "NotebookLM RPC capture — string values → <str:N>; cookies / headers / "
        "at= / Set-Cookie never read:\n"
    )
    print(out)
    print(
        f"\n{len(blocks)} call(s). Safe to share — no cookies / CSRF / session "
        "tokens are present (they live in headers, which this tool never reads)."
    )
    return 0


def _dump(node) -> str:
    return json.dumps(node, ensure_ascii=False, separators=(",", ":"))


def _die(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
