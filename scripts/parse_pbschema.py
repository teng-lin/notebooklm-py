#!/usr/bin/env python3
"""Parse blutter's disassembled *.pb.dart BuilderInfo._i() methods into schema evidence.

Reads the reconstructed Dart classes and, for each `static BuilderInfo _i()` body, walks the
disassembly extracting each protobuf field registration (tag, name, type, cardinality) from the
BuilderInfo adder calls (aOS/aOM/aE/aI/aOB/aD/pPM/pPS/pc, oneof via `oo`).

The output is deliberately not assigned to one synthetic protobuf package.  When blutter exposes
the ``PackageName`` object used by ``BuilderInfo``, its value is resolved through the dump's
``objs.txt`` and emitted with the original Dart library URI as per-message provenance.
"""

import dataclasses
import glob
import json
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
# The name handed to BuilderInfo() is dotted for nested messages
# ("TailwindStruct.TailwindStructEntry"); NAME_RE would skip it and the Dart
# class name ("TailwindStruct_TailwindStructEntry") would be passed off as exact.
# Only a bare register load of a string literal (``r2 = "Outer.Entry"``) counts: the
# ``r4 = const [..., createEmptyInstance, ...]`` line that follows also quotes strings.
BUILDER_NAME_RE = re.compile(r'r\d+ = "([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"\s*$')
TYPE_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_.]*)>")
INT_RE = re.compile(r"r\d+ = (\d+)\b")
ADDER_RE = re.compile(r"BuilderInfo::([A-Za-z0-9_]+)\b")
CLASS_RE = re.compile(r"^class (\w+) extends GeneratedMessage")
II_RE = re.compile(r"static BuilderInfo _i\(\) \{")
ENDM_RE = re.compile(r"^  \}")
LIBRARY_RE = re.compile(r"^// lib:.*?\burl:\s*(\S+)\s*$")
PACKAGE_REF_RE = re.compile(r"Obj!PackageName@([A-Fa-f0-9]+)")
PACKAGE_LITERAL_RE = re.compile(r"PackageName\(\s*\"([^\"]+)\"\s*\)")
PACKAGE_OBJECT_RE = re.compile(
    r"^Obj!PackageName@(?P<ref>[A-Fa-f0-9]+)\s*:\s*\{\s*"
    r"^\s*off_8:\s*(?P<name>\"(?:\\.|[^\"\\])*\")",
    re.MULTILINE,
)

# Preserve the complete historical evidence scope.  These are package-directory
# selectors, not claimed protobuf package names; exact package identity still
# comes exclusively from each BuilderInfo PackageName object.
DEFAULT_PATTERNS = (
    "google.internal.labs.tailwind.api.v1",
    "google.internal.labs.tailwind.discovery.v1",
    "google.internal.labs.tailwind.orchestration.v1",
    "google.internal.labs.tailwind.v1",
    "googledata.experiments.mobile.tailwind",
    "labs.language.tailwind.common.protos",
    "labs.language.tailwind.mobile.app.models",
    "labs.language.tailwind.mobile.app.protos.persistence",
    "labs.language.tailwind.mobile.app.services",
    "labs.language.tailwind.sharing",
    "logs.proto.labs_tailwind.metadata",
    "logs.proto.labs_tailwind",
)


@dataclasses.dataclass(frozen=True)
class MessageSchema:
    """A recovered message plus the provenance needed to form (or withhold) its FQN."""

    class_name: str
    builder_name: str | None
    fields: list[dict]
    protobuf_package: str | None
    package_object: str | None
    library_uri: str | None

    @property
    def proto_name(self):
        """Return the message name passed to BuilderInfo, falling back to its Dart class name."""
        return self.builder_name or self.class_name

    @property
    def fqn(self):
        """Return the exact protobuf FQN only when BuilderInfo's package was resolved."""
        if not self.protobuf_package:
            return None
        return f"{self.protobuf_package}.{self.proto_name}"


def parse_ii_body(lines):
    """Given the lines of a static _i() method body, return ordered field dicts + msg name."""
    fields = []
    msg_name = None
    # sliding recent context
    recent_name = None
    recent_tag = None
    recent_type = None
    recent_builder_name = None
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
            recent_name = mn.group(1)
        if not seen_first_builderinfo and (mb := BUILDER_NAME_RE.search(ln)) is not None:
            # the last quoted string before BuilderInfo() is the (possibly dotted) name
            recent_builder_name = mb.group(1)
        if "BuilderInfo::BuilderInfo" in ln:
            seen_first_builderinfo = True
            # message name is the recent PascalCase name (last segment when nested)
            if msg_name is None and recent_builder_name:
                if recent_builder_name.rsplit(".", 1)[-1][:1].isupper():
                    msg_name = recent_builder_name
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


def parse_package_names(path):
    """Return blutter PackageName object-id -> protobuf package mappings from ``objs.txt``."""
    with open(path, errors="replace") as fh:
        contents = fh.read()
    return {
        match.group("ref").lower(): json.loads(match.group("name"))
        for match in PACKAGE_OBJECT_RE.finditer(contents)
    }


def find_package_names(root):
    """Load the nearest conventional ``objs.txt`` for a blutter assembly root, if present."""
    candidates = (
        os.path.join(root, "objs.txt"),
        os.path.join(os.path.dirname(os.path.abspath(root)), "objs.txt"),
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return parse_package_names(candidate)
    return {}


def _package_evidence(lines, package_names):
    """Resolve the protobuf package supplied to BuilderInfo without guessing from the path."""
    for line in lines:
        literal = PACKAGE_LITERAL_RE.search(line)
        if literal:
            return literal.group(1), None
        package_ref = PACKAGE_REF_RE.search(line)
        if package_ref:
            ref = package_ref.group(1).lower()
            return package_names.get(ref), ref
    return None, None


def parse_file_messages(path, package_names=None):
    """Return recovered messages, including empty ones, with package and library provenance."""
    with open(path, errors="replace") as fh:
        lines = fh.read().splitlines()
    package_names = package_names or {}
    library_uri = next(
        (match.group(1) for line in lines if (match := LIBRARY_RE.match(line))),
        None,
    )
    messages = []
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
            builder_name, fields = parse_ii_body(body)
            protobuf_package, package_object = _package_evidence(body, package_names)
            messages.append(
                MessageSchema(
                    class_name=cur_class,
                    builder_name=builder_name,
                    fields=fields,
                    protobuf_package=protobuf_package,
                    package_object=package_object,
                    library_uri=library_uri,
                )
            )
            i = j
            continue
        i += 1
    return messages


def parse_file(path):
    """Return the historical class -> fields mapping, now retaining zero-field messages."""
    return {message.class_name: message.fields for message in parse_file_messages(path)}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = argv[0]
    patterns = argv[1:] or DEFAULT_PATTERNS
    files = []
    for p in patterns:
        files += glob.glob(os.path.join(root, f"*{p}*", "*.pb.dart"))
    files = sorted(set(files))
    package_names = find_package_names(root)
    total_msgs = total_fields = resolved_msgs = 0
    print("// Schema evidence fragments; do not place every message in one synthetic package.")
    print("// Protobuf FQNs below come only from BuilderInfo PackageName evidence.")
    for f in files:
        msgs = parse_file_messages(f, package_names)
        if not msgs:
            continue
        print(f"\n// ===== {os.path.basename(os.path.dirname(f))}/{os.path.basename(f)} =====")
        if msgs[0].library_uri:
            print(f"// Dart library: {msgs[0].library_uri}")
        for message in msgs:
            total_msgs += 1
            if message.fqn:
                resolved_msgs += 1
                print(f"// Protobuf FQN: {message.fqn}")
            elif message.package_object:
                print(
                    "// Protobuf FQN unresolved: "
                    f"{message.proto_name} (PackageName object {message.package_object})"
                )
            else:
                print(f"// Protobuf FQN unresolved: {message.proto_name} (no package evidence)")
            print(f"message {message.class_name} {{")
            for fld in sorted(message.fields, key=lambda x: x["tag"]):
                rep = "repeated " if fld["repeated"] else ""
                print(f"  {rep}{fld['type']} {fld['name']} = {fld['tag']};")
                total_fields += 1
            print("}")
    sys.stderr.write(
        f"\nparsed {total_msgs} messages, {total_fields} fields from {len(files)} files; "
        f"resolved {resolved_msgs}/{total_msgs} protobuf FQNs\n"
    )


if __name__ == "__main__":
    main()
