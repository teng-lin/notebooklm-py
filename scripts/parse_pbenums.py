#!/usr/bin/env python3
"""Regenerate ``docs/android/enums.txt`` from a blutter dump directory.

Every ``ProtobufEnum`` value in the Dart AOT snapshot is an object holding its integer
(``off_8``) and its name (``off_10``).  The object store (``objs.txt``) is exhaustive; the
object pool (``pp.txt``) only inlines the values the compiled code references directly.  The
two are merged, and each enum header records which integers only the object store proved
(``[objs adds …]``) or that the pool holds no object at all (``[objs-ONLY]``), so an audit
never mistakes a pool-absent member for an invented one.
"""

import re
import sys

OBJECT_RE = re.compile(r"^Obj!(?P<cls>\w+)@(?P<ref>[0-9a-fA-F]+) : \{$")
POOL_OBJECT_RE = re.compile(r"^\[pp\+0x[0-9a-fA-F]+\] Obj!(?P<cls>\w+)@(?P<ref>[0-9a-fA-F]+) : \{")
VALUE_RE = re.compile(r"^\s+off_8: int\((?P<value>-?0x[0-9a-fA-F]+|-?\d+)\),?$")
NAME_RE = re.compile(r'^\s+off_10: "(?P<name>[^"]+)"')


def parse_enum_objects(objs_path):
    """Return ``{enum class: {object ref: (integer, name)}}`` from ``objs.txt``."""
    with open(objs_path, errors="replace") as fh:
        lines = fh.read().split("\n")
    enums = {}
    for index, line in enumerate(lines):
        match = OBJECT_RE.match(line)
        if not match or index + 1 >= len(lines) or "Super!ProtobufEnum" not in lines[index + 1]:
            continue
        value = name = None
        for body in lines[index + 2 : index + 6]:
            if (mv := VALUE_RE.match(body)) is not None:
                value = int(mv.group("value"), 0)
            if (mn := NAME_RE.match(body)) is not None:
                name = mn.group("name")
        if value is not None and name is not None:
            enums.setdefault(match.group("cls"), {})[match.group("ref").lower()] = (value, name)
    return enums


def parse_pool_refs(pp_path, enum_classes):
    """Return ``{enum class: {object refs inlined in the object pool}}`` from ``pp.txt``."""
    refs = {}
    with open(pp_path, errors="replace") as fh:
        for line in fh:
            match = POOL_OBJECT_RE.match(line)
            if match and match.group("cls") in enum_classes:
                refs.setdefault(match.group("cls"), set()).add(match.group("ref").lower())
    return refs


def render(enums, pool_refs):
    """Render the merged inventory in the ``enums.txt`` format, one block per enum."""
    blocks = []
    for cls in sorted(enums):
        values = dict(enums[cls].values())
        pool_values = {enums[cls][ref][0] for ref in pool_refs.get(cls, ()) if ref in enums[cls]}
        header = f"=== {cls} ({len(values)})"
        if not pool_values:
            header += "  [objs-ONLY]"
        elif adds := sorted(set(values) - pool_values):
            header += f"  [objs adds {adds}]"
        lines = [header + " ==="] + [f"  {value} = {values[value]}" for value in sorted(values)]
        blocks.append("\n".join(lines))
    return "\n" + "\n\n".join(blocks) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(
            "usage: parse_pbenums.py <blutter out dir containing objs.txt and pp.txt>",
            file=sys.stderr,
        )
        return 2
    root = argv[0]
    enums = parse_enum_objects(f"{root}/objs.txt")
    pool_refs = parse_pool_refs(f"{root}/pp.txt", set(enums))
    sys.stdout.write(render(enums, pool_refs))
    total = sum(len({value for value, _ in refs.values()}) for refs in enums.values())
    print(f"parsed {len(enums)} enums, {total} values", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
