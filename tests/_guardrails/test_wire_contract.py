"""Wire-contract guardrail: positional constants and enums vs the real schema.

This client reads ``batchexecute`` responses by hardcoded array index. Nothing on
the wire carries field names, so a wrong index is silent — it yields a plausible
value of the wrong thing, and a unit test written from the same misunderstanding
will happily confirm it.

That is not hypothetical. A 2026-08 audit against the schema recovered from the
official Android app found an inverted ownership flag, transposed generation
options, a swapped status enum, and two constants naming fields that have never
existed — the last of which had *passing* unit tests built around the imaginary
shape.

These checks close that loop by validating our constants against an independent
source of truth:

* **A. Declared mappings hold.** For every entry in ``_wire_contract.MAPPINGS``,
  ``constant == proto_tag - 1``. Entries marked ``known_bad`` are ``xfail`` and
  carry the issue reference; the fixing PR must drop the marker.
* **B. No constant escapes review.** Every ``_*_POS``-style constant in
  ``src/notebooklm/_row_adapters/`` appears in ``MAPPINGS`` or ``UNMAPPED``. A new
  positional read cannot land without someone recording what it points at.
* **C. Enum values agree.** Client enum members match the recovered backend enum,
  and known gaps stay declared.

The reference data lives in ``docs/mobile/schema.proto`` and
``docs/mobile/enums.txt``. Both are recovered artifacts, not guesses — see
``docs/mobile/endpoints.md`` for the recovery method, and
``tests/_guardrails/_wire_schema.py`` for the index↔tag equivalence this relies on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._guardrails._wire_contract import (
    ENUM_BINDINGS,
    ENUM_GAPS,
    MAPPINGS,
    MODULE_LEVEL,
    PINNED,
    UNMAPPED,
    Mapping,
    Pinned,
)
from tests._guardrails._wire_schema import load_enums, load_proto_schema

pytestmark = pytest.mark.repo_lint

_SRC = Path(__file__).resolve().parents[2] / "src" / "notebooklm"

#: Everything that decodes positional wire arrays. Widening this set is the
#: intended way to bring more of the client under the contract — the coverage
#: test will then demand a registry entry for each newly-visible constant.
_SCANNED_DIRS = (_SRC / "_row_adapters",)
_SCANNED_FILES = (_SRC / "_settings.py", _SRC / "_mind_maps_api.py")

# Matches both `X: ClassVar[int] = 3` (class scope) and `X = 3` (module scope).
_CONST_RE = re.compile(
    r"^(?P<indent>\s*)(?P<name>_[A-Z][A-Z0-9_]*(?:POS|IDX|INDEX)[A-Z0-9_]*)"
    r"\s*(?::\s*ClassVar\[int\]\s*)?=\s*(?P<value>-?\d+)\s*(?:#.*)?$"
)
_CLASS_RE = re.compile(r"^class\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)")


def _scanned_paths() -> list[Path]:
    paths = [p for d in _SCANNED_DIRS for p in sorted(d.glob("*.py")) if p.name != "__init__.py"]
    paths.extend(p for p in _SCANNED_FILES if p.exists())
    return paths


def _discover_constants() -> dict[tuple[str, str, str], int]:
    """Return ``{(module, class_or_MODULE_LEVEL, const): value}``."""
    found: dict[tuple[str, str, str], int] = {}
    for path in _scanned_paths():
        current = MODULE_LEVEL
        for line in path.read_text(encoding="utf-8").splitlines():
            if (m := _CLASS_RE.match(line)) is not None:
                current = m.group("name")
                continue
            if (m := _CONST_RE.match(line)) is None:
                continue
            # Zero indent means module scope even if a class was seen earlier.
            scope = current if m.group("indent") else MODULE_LEVEL
            found[(path.stem, scope, m.group("name"))] = int(m.group("value"))
    return found


def _actual_value(mapping: Mapping, constants: dict[tuple[str, str, str], int]) -> int:
    key = (mapping.module, mapping.cls, mapping.const)
    if key not in constants:
        pytest.fail(
            f"{mapping.module}.{mapping.cls}.{mapping.const} is mapped in _wire_contract "
            "but no longer exists in the adapter. Remove the stale mapping."
        )
    return constants[key]


@pytest.mark.parametrize(
    "mapping",
    MAPPINGS,
    ids=[f"{m.module}.{m.cls}.{m.const}" for m in MAPPINGS],
)
def test_positional_constant_matches_proto_tag(mapping: Mapping) -> None:
    """A. Each declared constant equals ``proto tag - 1``."""
    if mapping.known_bad:
        pytest.xfail(
            f"known-wrong mapping tracked in {mapping.known_bad}: "
            f"{mapping.const} claims to read something other than "
            f"{mapping.message}.{mapping.field}. Remove this xfail in the fixing PR."
        )

    schema = load_proto_schema()
    if mapping.field is not None:
        tag = schema.field_tag(mapping.message, mapping.field, mapping.section)
        described = f"{mapping.message}.{mapping.field}"
    else:
        # Name-unrecovered field: assert the tag exists on the message, so a
        # schema re-extraction that renumbers it still trips this test.
        msg = schema.find(mapping.message, mapping.section)
        assert mapping.tag is not None  # guaranteed by Mapping.__post_init__
        assert msg.field_by_tag(mapping.tag) is not None, (
            f"{mapping.message} has no field at tag {mapping.tag}; the pinned tag for "
            f"{mapping.const} is stale. {mapping.note}"
        )
        tag = mapping.tag
        described = f"{mapping.message} tag {tag}"
    expected = tag - 1
    actual = _actual_value(mapping, _discover_constants())

    assert actual == expected, (
        f"{mapping.module}.{mapping.cls}.{mapping.const} = {actual}, but "
        f"{described} is protobuf tag {tag}, i.e. JSON index {expected}.\n"
        "Either the constant is wrong, or the mapping in _wire_contract.py names the "
        "wrong field. Do not 'fix' this by editing the expected value without "
        "checking a real captured response."
    )


def test_every_adapter_constant_is_declared() -> None:
    """B. No positional constant escapes the registry."""
    constants = _discover_constants()
    declared = {(m.module, m.cls, m.const) for m in MAPPINGS}
    declared |= {(u.module, u.cls, u.const) for u in UNMAPPED}
    declared |= {(p.module, p.cls, p.const) for p in PINNED}

    undeclared = sorted(set(constants) - declared)
    assert not undeclared, (
        "These positional constants are neither mapped to a wire field nor "
        "explicitly recorded as unmapped:\n"
        + "\n".join(f"  {m}.{c}.{k} = {constants[(m, c, k)]}" for m, c, k in undeclared)
        + "\n\nAdd each to MAPPINGS (with the protobuf message and field it reads) or "
        "to UNMAPPED (with an honest reason). An UNMAPPED entry is far better than a "
        "guessed mapping — see tests/_guardrails/_wire_contract.py."
    )


def test_no_stale_registry_entries() -> None:
    """B (converse). The registry does not describe constants that no longer exist."""
    constants = set(_discover_constants())
    stale = sorted(
        (m.module, m.cls, m.const) for m in MAPPINGS if (m.module, m.cls, m.const) not in constants
    ) + sorted(
        (u.module, u.cls, u.const) for u in UNMAPPED if (u.module, u.cls, u.const) not in constants
    )
    assert not stale, (
        "The wire-contract registry references constants that no longer exist:\n"
        + "\n".join(f"  {m}.{c}.{k}" for m, c, k in stale)
    )


@pytest.mark.parametrize("client_enum", sorted(ENUM_BINDINGS))
def test_enum_values_match_backend(client_enum: str) -> None:
    """C. Declared enum members match the recovered backend enum."""
    backend_name, bindings = ENUM_BINDINGS[client_enum]
    backend = load_enums().get(backend_name)
    assert backend, f"{backend_name} missing from docs/mobile/enums.txt"

    mismatches = [
        f"  {client_enum}({value}) claims {member!r}, backend has {backend.get(value)!r}"
        for value, member in bindings.items()
        if backend.get(value) != member
    ]
    assert not mismatches, f"{client_enum} disagrees with the backend enum:\n" + "\n".join(
        mismatches
    )


@pytest.mark.parametrize("client_enum", sorted(ENUM_GAPS))
def test_declared_enum_gaps_still_exist(client_enum: str) -> None:
    """C (converse). Declared gaps are real backend members, so the list stays honest.

    If a gap is closed in the client, its entry should be moved from ``ENUM_GAPS``
    into ``ENUM_BINDINGS`` rather than deleted.
    """
    # A gap-only enum (declared in ENUM_GAPS but not ENUM_BINDINGS) would raise
    # KeyError here and surface as a traceback rather than a usable message.
    binding = ENUM_BINDINGS.get(client_enum)
    assert binding is not None, (
        f"ENUM_GAPS declares {client_enum!r} but ENUM_BINDINGS does not. Add the "
        "backend enum name there first — the gap list is checked against it."
    )
    backend_name, _ = binding
    backend = load_enums().get(backend_name, {})
    wrong = [
        f"  declared gap {value} ({member!r}) is not a backend member of {backend_name}"
        for value, member, _ref in ENUM_GAPS[client_enum]
        if backend.get(value) != member
    ]
    assert not wrong, "ENUM_GAPS is out of date:\n" + "\n".join(wrong)


def test_reference_data_is_present_and_parsable() -> None:
    """Fail loudly if the recovered reference data is missing or empty."""
    schema = load_proto_schema()
    enums = load_enums()
    assert len(schema.messages) > 200, f"proto looks truncated: {len(schema.messages)} messages"
    assert len(enums) > 50, f"enum dump looks truncated: {len(enums)} enums"


@pytest.mark.parametrize("pin", PINNED, ids=[f"{p.module}.{p.cls}.{p.const}" for p in PINNED])
def test_live_pinned_constants_unchanged(pin: Pinned) -> None:
    """Freeze constants whose meaning is known from live evidence only.

    These read ``addUnused()`` slots, so the proto cannot confirm them (see
    :class:`Pinned`). The check is a change-detector, not a validation: it fails
    if someone edits the value without revisiting the evidence.
    """
    actual = _discover_constants().get((pin.module, pin.cls, pin.const))
    assert actual == pin.value, (
        f"{pin.module}.{pin.cls}.{pin.const} changed from {pin.value} to {actual}.\n"
        f"It is pinned as: {pin.means}\n"
        f"Established by: {pin.evidence}\n"
        "The proto cannot check this slot, so re-confirm against live data before "
        "changing the pin."
    )
