#!/usr/bin/env python3
"""Regenerate ``docs/android/enums.txt`` from a blutter dump directory.

Every ``ProtobufEnum`` value in the Dart AOT snapshot is an object holding its integer
(``off_8``) and its name (``off_10``).  The object store (``objs.txt``) is exhaustive and lists
instances grouped by class, so a class name that several Dart libraries define shows up as
several separate runs of objects.  The object pool (``pp.txt``) only inlines the values the
compiled code references directly, but its ``List<Enum>`` ``values`` literals are referenced
from the defining library's disassembly (``asm/<library>/*.dart``), which attributes each run
to a library.  Every run is rendered as its own block, so two enums that merely share a Dart
class name are never merged, and an integer that carries two names inside one enum (an alias)
is rendered twice rather than silently collapsed.
"""

import glob
import os
import re
import sys

OBJECT_RE = re.compile(r"^Obj!(?P<cls>\w+)@(?P<ref>[0-9a-fA-F]+) : \{$")
POOL_OBJECT_RE = re.compile(r"^\[pp\+0x[0-9a-fA-F]+\] Obj!(?P<cls>\w+)@(?P<ref>[0-9a-fA-F]+) : \{")
POOL_LIST_RE = re.compile(
    r"^\[pp\+0x(?P<offset>[0-9a-fA-F]+)\] List<(?P<cls>\w+)>\(\d+\) \[(?P<refs>[^\]]*)\]"
)
LIST_REF_RE = re.compile(r"Obj!\w+@(?P<ref>[0-9a-fA-F]+)")
VALUE_RE = re.compile(r"^\s+off_8: int\((?P<value>-?0x[0-9a-fA-F]+|-?\d+)\),?$")
NAME_RE = re.compile(r"^\s+off_10: \"(?P<name>[^\"]+)\"")
POOL_OFFSET_RE = re.compile(r"pp\+0x(?P<offset>[0-9a-fA-F]+)\b")
ENUM_CLASS_RE = re.compile(r"^class (?P<cls>\w+) extends ProtobufEnum\b", re.MULTILINE)


def parse_enum_runs(objs_path):
    """Return ``[(class, {object ref: (integer, name)}), …]`` — one entry per class run.

    ``objs.txt`` lists the instances of one class contiguously, so a new run of the same
    class name after other objects is a different Dart class that happens to share the name.
    """
    with open(objs_path, errors="replace") as fh:
        lines = fh.read().split("\n")
    runs = []
    previous_cls = None
    for index, line in enumerate(lines):
        match = OBJECT_RE.match(line)
        if not match:
            continue
        cls = match.group("cls")
        is_enum = index + 1 < len(lines) and "Super!ProtobufEnum" in lines[index + 1]
        if not is_enum:
            previous_cls = None
            continue
        value = name = None
        for body in lines[index + 2 : index + 6]:
            if (mv := VALUE_RE.match(body)) is not None:
                value = int(mv.group("value"), 0)
            if (mn := NAME_RE.match(body)) is not None:
                name = mn.group("name")
        if value is None or name is None:
            continue
        if cls != previous_cls:
            runs.append((cls, {}))
            previous_cls = cls
        runs[-1][1][match.group("ref").lower()] = (value, name)
    return runs


def parse_pool(pp_path, enum_classes):
    """Return inlined object refs and ``values`` lists per enum class from ``pp.txt``.

    ``pool_refs`` is ``{class: {ref}}`` for objects the pool inlines directly;
    ``pool_lists`` is ``{class: [(pool offset, {ref}), …]}`` for its ``List<Enum>`` literals.
    """
    pool_refs = {}
    pool_lists = {}
    with open(pp_path, errors="replace") as fh:
        for line in fh:
            if (match := POOL_OBJECT_RE.match(line)) is not None:
                if match.group("cls") in enum_classes:
                    pool_refs.setdefault(match.group("cls"), set()).add(match.group("ref").lower())
            elif (match := POOL_LIST_RE.match(line)) is not None:
                if match.group("cls") in enum_classes:
                    refs = {
                        m.group("ref").lower() for m in LIST_REF_RE.finditer(match.group("refs"))
                    }
                    pool_lists.setdefault(match.group("cls"), []).append(
                        (match.group("offset").lower(), refs)
                    )
    return pool_refs, pool_lists


def parse_libraries(asm_root, enum_classes):
    """Return ``(offset -> {library}, class -> {library})`` from the per-library disassembly.

    A library is the directory name directly under ``asm/``.  ``offset_libraries`` records
    which libraries reference each pool offset; ``class_libraries`` records which libraries
    declare each enum class.
    """
    offset_libraries = {}
    class_libraries = {}
    for path in glob.glob(os.path.join(asm_root, "*", "*.dart")):
        library = os.path.basename(os.path.dirname(path))
        with open(path, errors="replace") as fh:
            text = fh.read()
        for match in ENUM_CLASS_RE.finditer(text):
            if match.group("cls") in enum_classes:
                class_libraries.setdefault(match.group("cls"), set()).add(library)
        for match in POOL_OFFSET_RE.finditer(text):
            offset_libraries.setdefault(match.group("offset").lower(), set()).add(library)
    return offset_libraries, class_libraries


def attribute_runs(runs, pool_lists, offset_libraries, class_libraries):
    """Return ``[(class, library or None, objects)]`` by tying each run to its library.

    A run whose objects appear in a ``values`` list takes the library that references that
    list — but only when every such list points at the same single library; conflicting
    list evidence leaves the run unattributed instead of letting ``pp.txt`` order decide.
    When a class has exactly one declaring library, or exactly one library is left unclaimed
    after the list evidence, the remaining run takes it; otherwise it stays unattributed
    rather than being guessed.
    """
    attributed = []
    by_class = {}
    for index, (cls, _objects) in enumerate(runs):
        by_class.setdefault(cls, []).append(index)
    for cls, indexes in by_class.items():
        libraries = dict.fromkeys(indexes)
        for index in indexes:
            refs = set(runs[index][1])
            candidates = set()
            for offset, list_refs in pool_lists.get(cls, ()):
                if refs & list_refs:
                    candidates |= offset_libraries.get(offset, set())
            if len(candidates) == 1:
                libraries[index] = candidates.pop()
        unclaimed = set(class_libraries.get(cls, ())) - set(filter(None, libraries.values()))
        pending = [index for index, library in libraries.items() if library is None]
        if len(pending) == 1 and len(unclaimed) == 1:
            libraries[pending[0]] = unclaimed.pop()
        for index in indexes:
            attributed.append((cls, libraries[index], runs[index][1]))
    return attributed


def render(attributed, pool_refs):
    """Render one block per (class, library) in the ``enums.txt`` format."""
    blocks = []
    order = sorted(attributed, key=lambda item: (item[0], item[1] or "~"))
    for cls, library, objects in order:
        pairs = sorted(set(objects.values()))
        pool_values = {objects[ref][0] for ref in pool_refs.get(cls, ()) if ref in objects}
        header = f"=== {cls} ({len(pairs)})"
        header += f"  [library {library}]" if library else "  [library unattributed]"
        if not pool_values:
            header += "  [objs-ONLY]"
        elif adds := sorted({value for value, _ in pairs} - pool_values):
            header += f"  [objs adds {adds}]"
        if aliases := sorted({v for v, _ in pairs if sum(1 for x, _ in pairs if x == v) > 1}):
            header += f"  [aliases {aliases}]"
        lines = [header + " ==="] + [f"  {value} = {name}" for value, name in pairs]
        blocks.append("\n".join(lines))
    return "\n" + "\n\n".join(blocks) + "\n"


def main(argv=None):
    """Write the merged, library-attributed enum inventory for a blutter dump to stdout."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(
            "usage: parse_pbenums.py <blutter out dir containing objs.txt, pp.txt and asm/>",
            file=sys.stderr,
        )
        return 2
    root = argv[0]
    runs = parse_enum_runs(os.path.join(root, "objs.txt"))
    enum_classes = {cls for cls, _ in runs}
    pool_refs, pool_lists = parse_pool(os.path.join(root, "pp.txt"), enum_classes)
    offset_libraries, class_libraries = parse_libraries(os.path.join(root, "asm"), enum_classes)
    attributed = attribute_runs(runs, pool_lists, offset_libraries, class_libraries)
    sys.stdout.write(render(attributed, pool_refs))
    total = sum(len(set(objects.values())) for _, _, objects in attributed)
    unattributed = sum(1 for _, library, _ in attributed if library is None)
    print(
        f"parsed {len(attributed)} enums ({len(enum_classes)} class names, "
        f"{unattributed} unattributed), {total} values",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
