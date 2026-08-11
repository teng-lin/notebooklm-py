"""Parsers for the mobile-derived wire reference data.

Two reference artifacts, both recovered from the official NotebookLM Android
app and checked into ``docs/mobile/``:

* ``docs/mobile/schema.proto`` — protobuf messages with real field names and
  tag numbers, recovered from the Dart AOT ``BuilderInfo`` disassembly.
* ``docs/mobile/enums.txt`` — every ``ProtobufEnum`` value with its exact
  integer, merged from the snapshot object pool **and** the object store.

Why this matters for a positional JSON client
---------------------------------------------
The web transport is ``batchexecute``: responses are positional JSON arrays with
no field names on the wire. The mobile transport carries the *same backend
messages* over gRPC, where fields are tag-addressed. The two line up exactly:

    JSON index i  ==  protobuf tag (i + 1)

Verified independently on several messages, e.g. ``Source.settings`` is tag 4 and
is read at index 3, whose ``SourceSettings.status`` is tag 2 and is read at index
1 — matching this client's documented ``source[3][1]`` descent.

That equivalence is what lets a guardrail check our hardcoded positional
constants against a real schema instead of against themselves.

Caveats baked into the parsers
------------------------------
* **Duplicate message names.** The proto contains several messages twice: once
  under an ``orchestration.v1``/``v1`` wire package and once under a
  ``…mobile.app.protos.persistence`` package (the app's local storage schema,
  with *different* tags). Lookups are therefore section-scoped —
  :func:`ProtoSchema.field_tag` requires a section hint and refuses ambiguous
  matches rather than silently picking one.
* **``fieldType`` is a parser artifact.** The upstream extraction emits the
  literal placeholder ``fieldType`` where it failed to recover a real name
  (11 of 767 fields). Those are exposed as-is; do not treat them as real names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
PROTO_PATH = _REPO_ROOT / "docs" / "mobile" / "schema.proto"
ENUMS_PATH = _REPO_ROOT / "docs" / "mobile" / "enums.txt"

#: Emitted by the upstream schema extractor when a field name could not be
#: recovered. Not a real field name — see the module docstring.
UNRECOVERED_FIELD_NAME = "fieldType"

_SECTION_RE = re.compile(r"^//\s*=====\s*(?P<section>.+?)\s*=====\s*$")
_MESSAGE_RE = re.compile(r"^message\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\{")
_FIELD_RE = re.compile(
    r"^\s+(?:(?P<label>repeated|optional)\s+)?"
    r"(?P<type>[A-Za-z_][A-Za-z0-9_.]*)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<tag>\d+)\s*;"
)
_ENUM_HEADER_RE = re.compile(r"^===\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+\((?P<count>\d+)\)")
_ENUM_VALUE_RE = re.compile(r"^\s+(?P<value>-?\d+)\s*=\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*$")


@dataclass(frozen=True)
class ProtoField:
    """One field of a recovered protobuf message."""

    name: str
    tag: int
    type: str
    repeated: bool

    @property
    def json_index(self) -> int:
        """The batchexecute array index this field occupies (``tag - 1``)."""
        return self.tag - 1

    @property
    def name_recovered(self) -> bool:
        """False when the extractor could not recover the real field name."""
        return self.name != UNRECOVERED_FIELD_NAME


@dataclass(frozen=True)
class ProtoMessage:
    """A recovered message, scoped to the proto section that declared it."""

    name: str
    section: str
    fields: tuple[ProtoField, ...]

    def field(self, name: str) -> ProtoField | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def field_by_tag(self, tag: int) -> ProtoField | None:
        for f in self.fields:
            if f.tag == tag:
                return f
        return None


class AmbiguousMessageError(LookupError):
    """A message name resolved to more than one section and no hint narrowed it."""


@dataclass
class ProtoSchema:
    """All messages parsed from ``docs/mobile/schema.proto``."""

    messages: list[ProtoMessage] = field(default_factory=list)

    def find(self, name: str, section_hint: str | None = None) -> ProtoMessage:
        """Resolve a message by name, narrowed by a substring of its section.

        Raises :class:`AmbiguousMessageError` when several sections declare the
        name and the hint does not narrow it to one. Guessing here would silently
        validate our wire constants against the app's *local storage* schema,
        which uses different tags for several messages.
        """
        matches = [m for m in self.messages if m.name == name]
        if section_hint:
            matches = [m for m in matches if section_hint in m.section]
        if not matches:
            hint = f" (section hint {section_hint!r})" if section_hint else ""
            raise LookupError(f"no message named {name!r}{hint} in {PROTO_PATH.name}")
        if len(matches) > 1:
            sections = ", ".join(sorted(m.section for m in matches))
            raise AmbiguousMessageError(
                f"message {name!r} is declared in multiple sections ({sections}); "
                "pass a section_hint that selects exactly one"
            )
        return matches[0]

    def field_tag(self, message: str, field_name: str, section_hint: str | None = None) -> int:
        msg = self.find(message, section_hint)
        f = msg.field(field_name)
        if f is None:
            known = ", ".join(x.name for x in msg.fields)
            raise LookupError(f"{message}.{field_name} not found; fields are: {known}")
        return f.tag


@lru_cache(maxsize=1)
def load_proto_schema() -> ProtoSchema:
    """Parse the recovered proto. Cached — the file is static."""
    schema = ProtoSchema()
    section = "(unsectioned)"
    current: str | None = None
    fields: list[ProtoField] = []

    for raw in PROTO_PATH.read_text(encoding="utf-8").splitlines():
        if (m := _SECTION_RE.match(raw)) is not None:
            section = m.group("section")
            continue
        if (m := _MESSAGE_RE.match(raw)) is not None:
            current = m.group("name")
            fields = []
            continue
        if current is not None and raw.startswith("}"):
            schema.messages.append(
                ProtoMessage(name=current, section=section, fields=tuple(fields))
            )
            current = None
            fields = []
            continue
        if current is not None and (m := _FIELD_RE.match(raw)) is not None:
            fields.append(
                ProtoField(
                    name=m.group("name"),
                    tag=int(m.group("tag")),
                    type=m.group("type"),
                    repeated=m.group("label") == "repeated",
                )
            )
    return schema


@lru_cache(maxsize=1)
def load_enums() -> dict[str, dict[int, str]]:
    """Parse the merged enum dump into ``{enum_name: {value: MEMBER_NAME}}``.

    Merged from both the object pool and the object store: the pool alone yields
    74 enums / 273 values, the merge 77 / ~1900. Auditing against the pool alone
    manufactures false "we invented this value" findings and hides real members.
    """
    enums: dict[str, dict[int, str]] = {}
    current: str | None = None
    for raw in ENUMS_PATH.read_text(encoding="utf-8").splitlines():
        if (m := _ENUM_HEADER_RE.match(raw)) is not None:
            current = m.group("name")
            enums.setdefault(current, {})
            continue
        if current is not None and (m := _ENUM_VALUE_RE.match(raw)) is not None:
            enums[current][int(m.group("value"))] = m.group("name")
    return enums
