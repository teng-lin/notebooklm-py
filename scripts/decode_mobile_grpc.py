#!/usr/bin/env python3
"""Recover NotebookLM Android gRPC message *shapes* from captured protobuf bodies.

Companion to ``scripts/capture_mobile_grpc.js`` and
``docs/mobile/capture.md``. Reads the ``.pb`` files a capture session wrote
(gRPC envelope already stripped) and prints a merged schema per method+direction:
field number, wire type, whether it repeats, and — for length-delimited fields —
whether the payload is a nested message, a string, or opaque bytes.

Privacy: string/bytes *values* are never printed, only their length. Varint and
fixed32/64 values ARE printed because they are flags, enums, page sizes, and
``google.protobuf.Timestamp`` seconds/nanos — not private text. Even so, do not
commit this tool's output verbatim; summarise shapes in docs instead (see
``docs/mobile/endpoints.md``).

Usage:
    python scripts/decode_mobile_grpc.py <capture-dir> [method-substring]

The wire format is walked with a hand-rolled varint reader so the script has no
third-party dependency (the project does not ship protobuf). Length-delimited
payloads are speculatively parsed as nested messages; this over-eagerly parses
UTF-8 text that happens to be valid wire format, so treat very deep trees under
free-text fields (chat turns, source/artifact content) as approximate.
"""

from __future__ import annotations

import collections
import glob
import os
import struct
import sys

WIRE = {0: "varint", 1: "i64", 2: "len", 5: "i32"}


def read_varint(b: bytes, i: int) -> tuple[int, int]:
    shift = 0
    val = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        x = b[i]
        i += 1
        val |= (x & 0x7F) << shift
        if not x & 0x80:
            return val, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def parse_message(b: bytes) -> list[tuple[int, int, object]]:
    """Parse ``b`` as a protobuf message, consuming it fully or raising."""
    fields: list[tuple[int, int, object]] = []
    i, n = 0, len(b)
    while i < n:
        key, i = read_varint(b, i)
        field, wt = key >> 3, key & 7
        if field == 0:
            raise ValueError("field 0")
        if wt == 0:
            val, i = read_varint(b, i)
            fields.append((field, wt, val))
        elif wt == 1:
            if i + 8 > n:
                raise ValueError("truncated i64")
            fields.append((field, wt, b[i : i + 8]))
            i += 8
        elif wt == 5:
            if i + 4 > n:
                raise ValueError("truncated i32")
            fields.append((field, wt, b[i : i + 4]))
            i += 4
        elif wt == 2:
            ln, i = read_varint(b, i)
            if i + ln > n:
                raise ValueError("truncated len")
            fields.append((field, wt, b[i : i + ln]))
            i += ln
        else:
            raise ValueError(f"bad wire type {wt}")
    return fields


def classify_len(payload: bytes, depth: int) -> tuple[str, object]:
    if not payload:
        return "empty", None
    if depth < 12:
        try:
            sub = parse_message(payload)
            if sub and all(1 <= f[0] <= 536_870_911 for f in sub):
                return "message", sub
        except ValueError:
            pass
    try:
        payload.decode("utf-8")
        return "string", len(payload)
    except UnicodeDecodeError:
        return "bytes", len(payload)


class Node:
    __slots__ = ("wtypes", "max_repeat", "child", "scalars", "str_lens", "kinds")

    def __init__(self) -> None:
        self.wtypes: set[str] = set()
        self.max_repeat = 0
        self.child: dict[int, Node] | None = None
        self.scalars: set[int] = set()
        self.str_lens: set[int] = set()
        self.kinds: set[str] = set()


def merge(fields: list[tuple[int, int, object]], schema: dict[int, Node], depth: int) -> None:
    per_field = collections.Counter(f[0] for f in fields)
    seen: set[int] = set()
    for field, wt, payload in fields:
        node = schema.setdefault(field, Node())
        node.wtypes.add(WIRE[wt])
        if field not in seen:
            node.max_repeat = max(node.max_repeat, per_field[field])
            seen.add(field)
        if wt == 0 and isinstance(payload, int):
            if payload < 1_000_000_000_000 and len(node.scalars) < 6:
                node.scalars.add(payload)
        elif wt == 5 and isinstance(payload, (bytes, bytearray)):
            node.scalars.add(struct.unpack("<I", payload)[0])
        elif wt == 1 and isinstance(payload, (bytes, bytearray)):
            # fixed64. WIRE declares this type, so omitting it here silently
            # dropped every i64 sample from the reported scalars.
            node.scalars.add(struct.unpack("<Q", payload)[0])
        elif wt == 2 and isinstance(payload, (bytes, bytearray)):
            kind, info = classify_len(bytes(payload), depth)
            node.kinds.add(kind)
            if kind == "message":
                node.child = node.child or {}
                merge(info, node.child, depth + 1)  # type: ignore[arg-type]
            elif kind in ("string", "bytes") and isinstance(info, int):
                node.str_lens.add(info)


def render(schema: dict[int, Node], indent: int = 1) -> list[str]:
    out: list[str] = []
    pad = "  " * indent
    for field in sorted(schema):
        node = schema[field]
        wt = "/".join(sorted(node.wtypes))
        rep = " (repeated)" if node.max_repeat > 1 else ""
        line = f"{pad}#{field} [{wt}]{rep}"
        if node.kinds:
            line += " " + "/".join(sorted(node.kinds))
            lens = sorted(node.str_lens)
            if lens and node.kinds & {"string", "bytes"}:
                line += f" len={lens[0] if len(lens) == 1 else f'{lens[0]}..{lens[-1]}'}"
        if node.scalars and "message" not in node.kinds:
            line += f" = {sorted(node.scalars)[:6]}"
        out.append(line)
        if node.child:
            out.extend(render(node.child, indent + 1))
    return out


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(f"usage: {sys.argv[0]} <capture-dir> [method-substring]")
    directory = sys.argv[1]
    filt = sys.argv[2].lower() if len(sys.argv) > 2 else ""

    groups: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for path in sorted(glob.glob(os.path.join(directory, "*.pb"))):
        method, direction, _idx = (
            os.path.basename(path).split("_", 1)[1].rsplit(".pb", 1)[0].split(".")
        )
        if filt and filt not in method.lower():
            continue
        groups[(method, direction)].append(path)

    for method, direction in sorted(groups):
        files = groups[(method, direction)]
        schema: dict[int, Node] = {}
        sizes, parsed = [], 0
        for path in files:
            with open(path, "rb") as fh:
                body = fh.read()
            sizes.append(len(body))
            try:
                merge(parse_message(body), schema, 0)
                parsed += 1
            except ValueError as exc:
                print(f"  !! {os.path.basename(path)}: {exc}")
        print(
            f"\n### {method}.{direction}  "
            f"(n={len(files)}, parsed={parsed}, bytes={min(sizes)}..{max(sizes)})"
        )
        print("\n".join(render(schema)))


if __name__ == "__main__":
    main()
