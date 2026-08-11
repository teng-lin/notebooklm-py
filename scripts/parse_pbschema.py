#!/usr/bin/env python3
"""Parse blutter's disassembled *.pb.dart BuilderInfo._i() methods into a .proto-like schema.

Reads the reconstructed Dart classes and, for each `static BuilderInfo _i()` body, walks the
disassembly extracting each protobuf field registration (tag, name, type, cardinality) from the
BuilderInfo adder calls (aOS/aOM/aE/aI/aOB/aD/pPM/pPS/pc, oneof via `oo`).
"""

import glob
import os
import re
import sys

# protobuf-dart BuilderInfo adder -> (proto kind, repeated?)
ADDERS = {
    "aOS": ("string", False),
    "aOM": ("message", False),  # type from <Type>
    "aOB": ("bool", False),
    "aE": ("enum", False),  # type from <Type>
    "aI": ("int32", False),
    "aInt64": ("int64", False),
    "a64": ("int64", False),
    "aD": ("double", False),
    "aF": ("float", False),
    "aOSAsBytes": ("bytes", False),
    "aQB": ("bytes", False),
    "pPM": ("message", True),
    "pPS": ("string", True),
    "pPE": ("enum", True),
    "p": ("message", True),
    "pc": ("message", True),
}

NAME_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"')
TYPE_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_.]*)>")
INT_RE = re.compile(r"r\d+ = (\d+)\b")
ADDER_RE = re.compile(r"BuilderInfo::([A-Za-z0-9_]+)\b")
CLASS_RE = re.compile(r"^class (\w+) extends GeneratedMessage")
II_RE = re.compile(r"static BuilderInfo _i\(\) \{")
ENDM_RE = re.compile(r"^  \}")


def parse_ii_body(lines):
    """Given the lines of a static _i() method body, return ordered field dicts + msg name."""
    fields = []
    msg_name = None
    # sliding recent context
    recent_name = None
    recent_tag = None
    recent_type = None
    seen_first_builderinfo = False
    for ln in lines:
        m = TYPE_RE.search(ln)
        if m and "TypeArguments" in ln:
            recent_type = m.group(1)
        mi = INT_RE.search(ln)
        if mi:
            v = int(mi.group(1))
            if 1 <= v <= 536870911:
                recent_tag = v
        mn = NAME_RE.search(ln)
        if mn and ("PP," in ln or '"' in ln):
            cand = mn.group(1)
            if not seen_first_builderinfo and cand[:1].isupper() and msg_name is None:
                # first quoted PascalCase string after entering _i() is the message name
                pass
            recent_name = cand
        if "BuilderInfo::BuilderInfo" in ln:
            seen_first_builderinfo = True
            # message name is the recent PascalCase name
            if msg_name is None and recent_name and recent_name[:1].isupper():
                msg_name = recent_name
            recent_name = None
            recent_tag = None
            recent_type = None
            continue
        ma = ADDER_RE.search(ln)
        if ma:
            adder = ma.group(1)
            if adder in ("BuilderInfo", "addUnused", "add", "oo"):
                # oneof/reserved: note oneof separately
                recent_name = None
                recent_type = None
                continue
            if adder in ADDERS and recent_name and recent_tag:
                kind, repeated = ADDERS[adder]
                typ = recent_type if kind in ("message", "enum") and recent_type else kind
                fields.append(
                    {
                        "tag": recent_tag,
                        "name": recent_name,
                        "type": typ,
                        "repeated": repeated,
                        "adder": adder,
                    }
                )
            recent_name = None
            recent_type = None
    return msg_name, fields


def parse_file(path):
    with open(path, errors="replace") as fh:
        lines = fh.read().splitlines()
    messages = {}  # class_name -> fields
    i = 0
    cur_class = None
    while i < len(lines):
        cm = CLASS_RE.match(lines[i])
        if cm:
            cur_class = cm.group(1)
        if II_RE.search(lines[i]) and cur_class:
            # collect until method end (line that is exactly "  }")
            body = []
            j = i + 1
            while j < len(lines) and not ENDM_RE.match(lines[j]):
                body.append(lines[j])
                j += 1
            _, fields = parse_ii_body(body)
            if fields:
                messages[cur_class] = fields
            i = j
            continue
        i += 1
    return messages


def main():
    root = sys.argv[1]
    patterns = sys.argv[2:] or ["orchestration.v1", "sharing", "discovery", "api.v1"]
    files = []
    for p in patterns:
        files += glob.glob(os.path.join(root, f"*{p}*", "*.pb.dart"))
    files = sorted(set(files))
    total_msgs = total_fields = 0
    for f in files:
        msgs = parse_file(f)
        if not msgs:
            continue
        print(f"\n// ===== {os.path.basename(os.path.dirname(f))}/{os.path.basename(f)} =====")
        for cls, fields in msgs.items():
            total_msgs += 1
            print(f"message {cls} {{")
            for fld in sorted(fields, key=lambda x: x["tag"]):
                rep = "repeated " if fld["repeated"] else ""
                print(f"  {rep}{fld['type']} {fld['name']} = {fld['tag']};")
                total_fields += 1
            print("}")
    sys.stderr.write(
        f"\nparsed {total_msgs} messages, {total_fields} fields from {len(files)} files\n"
    )


if __name__ == "__main__":
    main()
