#!/usr/bin/env python3
"""Scrub a NotebookLM web-UI ``batchexecute`` capture down to the shareable RPC
payload SHAPE — no cookies, no CSRF, no session ids, no free-text values.

Paste a DevTools **Copy as cURL** (or the raw request body, or a ``.har``) on
stdin or via ``--file``. The tool reads ONLY the ``f.req`` form field — where the
RPC payloads live — decodes it, and prints each call's parameter structure with
every *string* leaf redacted to its length. Cookies, the ``at=`` CSRF token,
``f.sid`` and request headers all live OUTSIDE ``f.req``, so they are never read
or emitted: the output is safe to paste into a bug report by construction.

Usage:
  pbpaste | python scrub_rpc_capture.py              # macOS clipboard
  python scrub_rpc_capture.py --file curl.txt
  python scrub_rpc_capture.py --rpcid CCqFvf < curl.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import unquote

# Credential shapes that must NEVER appear in the output (defense-in-depth net).
_LEAK = re.compile(
    r"SID=|SAPISID|__Secure-|ya29\.|AIza[0-9A-Za-z_-]{20}|SNlM0e|\bat=[A-Za-z0-9:_-]{8}",
)


def _find_freq(text: str) -> str | None:
    m = re.search(r"f\.req=([^&'\"\n\r]+)", text)
    return unquote(m.group(1)) if m else None


def _redact(node):
    if isinstance(node, str):
        return f"<str:{len(node)}>"
    if isinstance(node, list):
        return [_redact(x) for x in node]
    if isinstance(node, dict):
        return {k: _redact(v) for k, v in node.items()}
    return node  # int / float / bool / None — structural constants are kept


def _iter_calls(outer):
    if not isinstance(outer, list) or not outer:
        return
    level = outer[0]
    if isinstance(level, list) and level and isinstance(level[0], list):
        calls = level
    elif isinstance(level, list):
        calls = [level]
    else:
        return
    for call in calls:
        if isinstance(call, list) and len(call) >= 2 and isinstance(call[0], str):
            yield call[0], call[1]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--file", help="read from a file instead of stdin")
    ap.add_argument("--rpcid", help="only show this rpcid (e.g. CCqFvf)")
    args = ap.parse_args()

    text = (
        open(args.file, encoding="utf-8", errors="replace").read()
        if args.file
        else sys.stdin.read()
    )
    freq = _find_freq(text)
    if not freq:
        return _die(
            "No `f.req=` field found. Paste a DevTools 'Copy as cURL' of the "
            "batchexecute request (or its raw body)."
        )
    try:
        outer = json.loads(freq)
    except json.JSONDecodeError as e:
        return _die(f"Could not parse f.req as JSON: {e}")

    lines, n_calls, n_strings = [], 0, 0
    for rpcid, inner in _iter_calls(outer):
        if args.rpcid and rpcid != args.rpcid:
            continue
        params = inner
        if isinstance(inner, str) and inner:
            try:
                params = json.loads(inner)
            except json.JSONDecodeError:
                params = inner
        red = _redact(params)
        n_strings += json.dumps(red).count("<str:")
        rendered = json.dumps(red, ensure_ascii=False, separators=(",", ":"))
        lines.append(f"  {rpcid}  →  {rendered}")
        n_calls += 1

    if not n_calls:
        return _die(
            "No RPC calls found in f.req" + (f" for rpcid {args.rpcid!r}." if args.rpcid else ".")
        )

    out = "\n".join(lines)
    if _LEAK.search(out):  # should be impossible — fail closed if it ever happens
        return _die("Refusing to print: a credential-shaped token survived. Report this.")

    print("RPC payload shapes (string values → <str:N>; cookies / at= / headers never read):\n")
    print(out)
    print(
        f"\n{n_calls} call(s), {n_strings} string value(s) redacted. "
        "Safe to share — no cookies / CSRF / session ids are present in f.req."
    )
    return 0


def _die(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
