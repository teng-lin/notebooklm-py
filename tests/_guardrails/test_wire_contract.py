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
  ``src/notebooklm/_web/rows/`` appears in ``MAPPINGS`` or ``UNMAPPED``. A new
  positional read cannot land without someone recording what it points at.
* **C. Enum values agree.** Client enum members match the recovered backend enum,
  and known gaps stay declared.

The reference data lives in ``docs/android/schema.proto`` and
``docs/android/enums.txt``. Both are recovered artifacts, not guesses — see
``docs/android/endpoints.md`` for the recovery method, and
``tests/_guardrails/_wire_schema.py`` for the index↔tag equivalence this relies on.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

import notebooklm.rpc.types as rpc_types
from notebooklm.rpc.types import ARTIFACT_STATUS_SUGGESTED_WIRE_NAME, ArtifactStatus
from tests._guardrails._wire_contract import (
    DOC,
    ENUM_BINDINGS,
    ENUM_GAPS,
    MAPPINGS,
    MODULE_LEVEL,
    PINNED,
    UNMAPPED,
    UNREAD_SHARE_STATUS_SLOTS,
    WIRE,
    Mapping,
    Pinned,
)
from tests._guardrails._wire_schema import PROTO_PATH, load_enums, load_proto_schema

pytestmark = pytest.mark.repo_lint

#: Bindings whose client side is an ``int``-Enum in ``notebooklm.rpc.types``,
#: so the table can be checked against the live members. Decode-map bindings
#: like ``SourceType`` (a ``str``-Enum keyed by wire code in a separate dict)
#: are excluded -- their values live in ``_SOURCE_TYPE_CODE_MAP``, not on the
#: enum, so ``__members__`` has nothing to compare against.
_LIVE_BOUND_ENUMS: tuple[str, ...] = tuple(
    name
    for name in sorted(ENUM_BINDINGS)
    if isinstance(getattr(rpc_types, name, None), type)
    and issubclass(getattr(rpc_types, name), int)
)

#: Client-only sentinel members that deliberately have no backend counterpart,
#: so the "every live value is bound" half of the check must skip them. Keep
#: this list short and justified — an undeclared unbound value is a real gap.
_CLIENT_SYNTHETIC_VALUES: dict[str, dict[int, str]] = {
    # Not a wire value: returned when source[3][1] is absent, malformed, or a
    # code we do not map yet. See SourceStatus.UNKNOWN in rpc/types.py.
    "SourceStatus": {-1: "UNKNOWN"},
    # Same shape for the Drive-side sibling: returned when source[3][3] is
    # populated with a code (or type) we do not model. See DriveSourceStatus.
    "DriveSourceStatus": {-1: "UNKNOWN"},
    # And again for the research search mode: returned when task_info[2] is
    # populated with a code (or type) we do not model. See DiscoveryMode.
    "DiscoveryMode": {-1: "UNKNOWN"},
}

_SRC = Path(__file__).resolve().parents[2] / "src" / "notebooklm"

#: Everything that decodes positional wire arrays. Widening this set is the
#: intended way to bring more of the client under the contract — the coverage
#: test will then demand a registry entry for each newly-visible constant.
_SCANNED_DIRS = (_SRC / "_web" / "rows",)
_SCANNED_FILES = (
    _SRC / "_web" / "settings.py",
    _SRC / "_web" / "mind_maps.py",
)

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


def test_function_call_messages_register_under_the_orchestration_package() -> None:
    """``FunctionCall``/``FunctionResponse`` carry the ``orchestration.v1`` package.

    Their Dart libraries live under an ``orchestration.v1.agency`` directory, but the
    ``BuilderInfo`` of both messages is constructed with the
    ``google.internal.labs.tailwind.orchestration.v1`` ``PackageName`` object in both the
    ``1.46.7`` and ``1.55.10`` dumps, and neither object store holds an ``agency`` package
    object. The schema must keep saying what the binary says rather than the directory name.
    """
    schema = load_proto_schema()
    message = schema.find("StructuralElement", DOC)

    function_call = message.field("functionCall")
    function_response = message.field("functionResponse")
    assert function_call is not None
    assert function_call.type == "FunctionCall"
    assert function_response is not None
    assert function_response.type == "FunctionResponse"

    fqn_lines = {
        line.removeprefix("// Protobuf FQN: ")
        for line in PROTO_PATH.read_text(encoding="utf-8").splitlines()
        if line.startswith("// Protobuf FQN: ")
    }
    for name in ("FunctionCall", "FunctionResponse"):
        assert f"google.internal.labs.tailwind.orchestration.v1.{name}" in fqn_lines
    assert not {fqn for fqn in fqn_lines if ".orchestration.v1.agency." in fqn}


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


def test_unread_share_status_slots_stay_undecoded() -> None:
    """B (extension). Nothing decodes a GET_SHARE_STATUS slot we cannot name (#2130).

    Slots 6 and 7 are populated on every live response, but the recovered
    ``GetProjectDetailsResponse`` declares no tags 7/8 — so any constant reading
    them would necessarily be named from a guess, which is the defect class this
    module exists to catch.

    Without this assertion :data:`UNREAD_SHARE_STATUS_SLOTS` would be an inert
    dict that documents an intention nothing enforces, and a future change could
    decode slot 6 into an invented field name with the whole suite green.
    """
    offenders = [
        f"  sharing.{cls}.{const} = {value} — collides with {UNREAD_SHARE_STATUS_SLOTS[value]}"
        for (module, cls, const), value in sorted(_discover_constants().items())
        if module == "sharing" and value in UNREAD_SHARE_STATUS_SLOTS
    ]
    assert not offenders, (
        "A GET_SHARE_STATUS positional constant now reads a slot recorded as "
        "deliberately-undecoded:\n"
        + "\n".join(offenders)
        + "\n\nThese proto tags have no name in docs/android/schema.proto. If a schema "
        "re-extraction has since recovered one, add a MAPPINGS entry and remove the slot "
        "from UNREAD_SHARE_STATUS_SLOTS in the same change — do not name it from a guess."
    )


@pytest.mark.parametrize("client_enum", sorted(ENUM_BINDINGS))
def test_enum_values_match_backend(client_enum: str) -> None:
    """C. Declared enum members match the recovered backend enum."""
    backend_name, bindings = ENUM_BINDINGS[client_enum]
    backend = load_enums().get(backend_name)
    assert backend, f"{backend_name} missing from docs/android/enums.txt"

    mismatches = [
        f"  {client_enum}({value}) claims {member!r}, backend has {backend.get(value)!r}"
        for value, member in bindings.items()
        if backend.get(value) != member
    ]
    assert not mismatches, f"{client_enum} disagrees with the backend enum:\n" + "\n".join(
        mismatches
    )


@pytest.mark.parametrize("client_enum", sorted(_LIVE_BOUND_ENUMS))
def test_binding_covers_the_live_client_enum(client_enum: str) -> None:
    """C (third leg). The binding table describes the *real* Python enum.

    Without this, ``ENUM_BINDINGS`` only proves the hand-written table agrees
    with the backend dump — it never imports the client, so setting
    ``QuizQuantity.MORE = 4`` and updating the frozen snapshot to match would
    leave both other gates green. That is exactly the shape of #2117, where a
    wrong value survived for months behind a snapshot that faithfully preserved
    it. Tying the table to ``__members__`` closes the loop:
    code -> table -> backend.

    Scoped to ``_LIVE_BOUND_ENUMS`` because several bindings describe decode
    maps (e.g. ``SourceType``) whose client members are strings keyed by wire
    code elsewhere, not ``int``-Enum values.
    """
    _, bindings = ENUM_BINDINGS[client_enum]
    live = getattr(rpc_types, client_enum)
    live_by_value = {member.value: member.name for member in live}

    mismatches = [
        f"  {client_enum} has no member with value {value} (table claims {member!r}); "
        f"live members are {live_by_value}"
        for value, member in bindings.items()
        if value not in live_by_value
    ]
    assert not mismatches, (
        f"{client_enum} in rpc/types.py disagrees with its _wire_contract binding:\n"
        + "\n".join(mismatches)
        + "\n\nUpdate whichever side is wrong -- but check a real captured "
        "response first, not just the enum dump."
    )

    synthetic = _CLIENT_SYNTHETIC_VALUES.get(client_enum, {})
    missing = sorted(set(live_by_value) - set(bindings) - set(synthetic))
    assert not missing, (
        f"{client_enum} declares value(s) {missing} that the _wire_contract binding "
        "does not cover, so they are never checked against the backend enum. Add "
        "them to ENUM_BINDINGS, to ENUM_GAPS with an honest reason, or — only if "
        "the member is a client-side sentinel with no backend counterpart — to "
        "_CLIENT_SYNTHETIC_VALUES."
    )

    stale_synthetic = sorted(
        value for value, name in synthetic.items() if live_by_value.get(value) != name
    )
    assert not stale_synthetic, (
        f"_CLIENT_SYNTHETIC_VALUES[{client_enum!r}] declares value(s) {stale_synthetic} "
        "that no longer match a live member of that name. Remove the stale exemption."
    )


#: ``QuizQuantity`` / ``QuizDifficulty`` are single client enums serving two
#: distinct backend messages. ``ENUM_BINDINGS`` is keyed by client enum, so only
#: the quiz copy can be bound; these pairs assert the flashcards copy still
#: agrees, turning "they declare identical values" from an assumption into a
#: checked fact. If Google ever renumbers only the flashcards side, every
#: flashcard request would go out wrong while the quiz binding stayed green --
#: the same silent class as #2116/#2117.
_PARALLEL_BACKEND_ENUMS: tuple[tuple[str, str], ...] = (
    ("QuizGenerationOptions_QuestionQuantity", "FlashcardsGenerationOptions_CardQuantity"),
    ("QuizGenerationOptions_QuizDifficulty", "FlashcardsGenerationOptions_FlashcardsDifficulty"),
)


def _semantic_members(members: dict[int, str]) -> dict[int, str]:
    """Strip an enum's shared name prefix, leaving the semantic member names.

    ``QUESTION_QUANTITY_FEWER`` and ``CARD_QUANTITY_FEWER`` both reduce to
    ``FEWER``, so two differently-named backend enums become comparable
    value-for-value. The prefix is derived rather than hardcoded, and trimmed
    back to a token boundary so a partial word is never cut.
    """
    prefix = os.path.commonprefix(list(members.values()))
    prefix = prefix[: prefix.rfind("_") + 1]
    return {value: name[len(prefix) :] for value, name in members.items()}


@pytest.mark.parametrize(("quiz_enum", "flashcards_enum"), _PARALLEL_BACKEND_ENUMS)
def test_quiz_and_flashcards_backend_enums_agree(quiz_enum: str, flashcards_enum: str) -> None:
    """The quiz and flashcards option enums agree member-for-member.

    Comparing only the numeric key sets would be too weak: if the flashcards
    enum swapped FEWER and MORE while keeping ``{0, 1, 2, 3}``, the shared
    ``QuizQuantity`` would encode flashcards backwards and a key-set assertion
    would still pass — the exact divergence this guard exists to catch. So the
    comparison is on ``{value: semantic_name}``.
    """
    enums = load_enums()
    quiz, flashcards = enums.get(quiz_enum), enums.get(flashcards_enum)
    assert quiz, f"{quiz_enum} missing from docs/android/enums.txt"
    assert flashcards, f"{flashcards_enum} missing from docs/android/enums.txt"

    quiz_semantic = _semantic_members(quiz)
    flashcards_semantic = _semantic_members(flashcards)

    assert quiz_semantic == flashcards_semantic, (
        f"{quiz_enum} and {flashcards_enum} no longer agree member-for-member "
        f"({quiz_semantic} vs {flashcards_semantic}). The shared client enum can "
        "no longer serve both -- split it, and bind each side separately."
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


def test_suggested_filter_constant_tracks_the_backend_enum_name() -> None:
    """The LIST_ARTIFACTS filter string stays tied to the code it excludes.

    ``_web/artifact/listing.py`` sends ``NOT artifact.status = "<name>"`` using the
    symbolic backend enum-value name, which cannot be derived from the integer.
    Without this, a backend rename would fail ``test_enum_values_match_backend``
    on the binding, someone would update ``ENUM_BINDINGS``, and the filter
    constant would silently stay stale — the filter would then match nothing
    and suggestion rows would start appearing in every listing.
    """
    _backend_name, bindings = ENUM_BINDINGS["ArtifactStatus"]
    assert bindings[ArtifactStatus.SUGGESTED.value] == ARTIFACT_STATUS_SUGGESTED_WIRE_NAME


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


def test_google_docs_document_id_shares_the_drive_tag() -> None:
    """``SourceRow._DRIVE_DOCUMENT_ID_POS`` indexes BOTH Drive metadata blocks.

    ``SourceRow.drive_document_id`` (#2113) reads one constant against two
    different nested messages — ``GoogleDocsSourceMetadata`` at ``metadata[0]``
    and ``GoogleDriveSourceMetadata`` at ``metadata[9]``. MAPPINGS can only
    assert it against one of them, so this pins the other half of that claim:
    if Google ever moves ``GoogleDocsSourceMetadata.documentId`` off tag 1, the
    shared constant silently starts reading the wrong slot for Docs/Slides rows
    and the add_drive idempotency probe stops matching them.
    """
    schema = load_proto_schema()
    docs_tag = schema.field_tag("GoogleDocsSourceMetadata", "documentId", WIRE)
    drive_tag = schema.field_tag("GoogleDriveSourceMetadata", "documentId", WIRE)
    assert docs_tag == drive_tag, (
        "GoogleDocsSourceMetadata.documentId and GoogleDriveSourceMetadata."
        f"documentId no longer share a tag ({docs_tag} vs {drive_tag}). "
        "SourceRow._DRIVE_DOCUMENT_ID_POS can no longer index both blocks — "
        "split it back into two constants and register each in MAPPINGS."
    )
    assert _discover_constants()[("sources", "SourceRow", "_DRIVE_DOCUMENT_ID_POS")] == (
        docs_tag - 1
    )


def test_flashcards_option_pair_shares_the_quiz_tags() -> None:
    """``ArtifactRow._OPTION_*_POS`` index BOTH option messages (#2195).

    ``QuizGenerationOptions`` and ``FlashcardsGenerationOptions`` are distinct
    messages that happen to declare the same two fields at the same tags, so
    :attr:`ArtifactRow.quiz_options` and :attr:`ArtifactRow.flashcards_options`
    share one pair of constants. MAPPINGS can only assert them against the quiz
    copy; this pins the other half.

    The sibling check ``test_quiz_and_flashcards_backend_enums_agree`` covers
    the enum *values*; this covers the field *positions*. Both are needed: Google
    renumbering only the flashcards message would silently transpose every
    flashcards read-back while the quiz side stayed green — the same
    single-sided drift as #2116, just on the decode end.
    """
    schema = load_proto_schema()
    for quiz_field, flashcards_field, const in (
        ("questionQuantity", "cardQuantity", "_OPTION_QUANTITY_POS"),
        ("quizDifficulty", "flashcardsDifficulty", "_OPTION_DIFFICULTY_POS"),
    ):
        quiz_tag = schema.field_tag("QuizGenerationOptions", quiz_field, WIRE)
        flashcards_tag = schema.field_tag("FlashcardsGenerationOptions", flashcards_field, WIRE)
        assert quiz_tag == flashcards_tag, (
            f"QuizGenerationOptions.{quiz_field} and FlashcardsGenerationOptions."
            f"{flashcards_field} no longer share a tag ({quiz_tag} vs "
            f"{flashcards_tag}). ArtifactRow.{const} can no longer index both "
            "messages — split it into two constants and register each in MAPPINGS."
        )
        assert _discover_constants()[("artifacts", "ArtifactRow", const)] == quiz_tag - 1
