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

# Fail-closed perimeter. ``_redact`` turns every string leaf into ``<str:N>``,
# so safety here is STRUCTURAL, not shape-based: ``_unredacted`` asserts that
# every string that survives into the output is a ``<str:N>`` placeholder and
# nothing else. This is shape-AGNOSTIC by design — it knows nothing about
# credential formats (``AIza…`` keys, ``SNlM0e`` CSRF, OAuth/session tokens,
# account email, or the ``WIZ_global_data`` page-HTML they hide in), so no new
# Google token shape can ever outrun it, and there is no credential registry to
# keep in sync with the runtime scrubber (src/notebooklm/_secrets.py).
_REDACTED_STR = re.compile(r"<str:\d+>")
# rpcids are short public method IDs (e.g. ``CCqFvf``); printed verbatim, so
# pin their shape to keep a malformed HAR from injecting text through that slot.
_SAFE_RPCID = re.compile(r"[A-Za-z0-9_]{3,24}")

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
        # Keys are wire strings too — redact them, else a decoded object like
        # {"user@gmail.com": ...} would leak the key verbatim.
        return {_redact(k): _redact(v) for k, v in node.items()}
    return node  # int / float / bool / None — structural constants kept verbatim


def _unredacted(node):
    """Return the first string leaf that is NOT a ``<str:N>`` placeholder, or None.

    The completeness check behind the fail-closed perimeter: every string in a
    redacted structure must be a ``<str:N>`` token. Dict keys are walked as well
    as values (both come off the wire); int/float/bool/None are structural
    constants (gRPC status codes, list nesting) and carry no string content, so
    they never trip it.
    """
    if isinstance(node, str):
        return None if _REDACTED_STR.fullmatch(node) else node
    if isinstance(node, list):
        children = node
    elif isinstance(node, dict):
        children = [*node.keys(), *node.values()]
    else:
        return None
    for child in children:
        bad = _unredacted(child)
        if bad is not None:
            return bad
    return None


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
        # response frames indexed by rpcid. A streamed response can emit a null
        # placeholder frame before the populated one for the same rpcid, so let
        # a later informative frame win — but never let a null clobber a hit.
        resp = {}
        for rpcid, result, err in _response_frames(entry):
            if rpcid not in resp or result is not None or err is not None:
                resp[rpcid] = (result, err)
        for rpcid, params in _iter_request_calls(_req_freq(entry) or ""):
            if args.rpcid and rpcid != args.rpcid:
                continue
            if not _SAFE_RPCID.fullmatch(rpcid):
                continue  # not a real rpcid — skip rather than print unknown text
            name = _NAMES.get(rpcid, "")
            head = f"{rpcid}" + (f"  ({name})" if name else "")
            req_red = _redact(params)
            redacted = [req_red]
            lines = [head, f"  request : {_dump(req_red)}"]
            if rpcid in resp:
                result, err = resp[rpcid]
                result_red, err_red = _redact(result), _redact(err)
                redacted += [result_red, err_red]
                bits = [f"HTTP {status}"]
                if err is not None:
                    bits.append(f"status_code={_dump(err_red)}")
                bits.append(f"result={_dump(result_red)}")
                lines.append("  response: " + " | ".join(bits))
            else:
                lines.append(f"  response: HTTP {status} (body not in HAR — export 'with content')")
            if any(_unredacted(node) is not None for node in redacted):
                # impossible by construction — fail closed if it ever happens
                return _die("Refusing to print: a value survived redaction. Please report this.")
            blocks.append("\n".join(lines))

    if not blocks:
        return _die(
            "No matching RPC calls found" + (f" for rpcid {args.rpcid!r}." if args.rpcid else ".")
        )

    out = "\n\n".join(blocks)

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
