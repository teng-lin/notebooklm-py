"""Frozen-snapshot gate for the wire integers of every RPC ``int``-Enum.

The obfuscated wire integers in ``src/notebooklm/rpc/types.py`` (``VideoStyle``,
``VideoFormat``, ``AudioFormat``, ``InfographicStyle``, …) are an *undocumented
contract* with Google's backend. In #1597 Google changed ``VideoStyle``'s wire
integers; our stale values produced **silent** wrong output — generation
succeeded but emitted the wrong style, with no error to surface the drift.

This module freezes every such wire value into ``_RPC_ENUM_SNAPSHOT`` so any
future change is IMPOSSIBLE to land silently: it must be an explicit,
diff-visible acknowledgement in the PR that touches it. It is a passive
ratchet — near-zero cost, cohort-independent (no network), pure unit — and it
is the ONLY protection for the "Tier-Z" enums whose drift we cannot otherwise
detect.

Modeled on the frozen-snapshot pattern in
``tests/_guardrails/test_public_surface_manifest.py``
(``_UNGATED_PUBLIC_ALL_SNAPSHOT`` + a freeze test + a completeness test).
"""

from __future__ import annotations

import enum
import inspect

import pytest

import notebooklm.rpc.types as rpc_types

pytestmark = pytest.mark.repo_lint

# ---------------------------------------------------------------------------
# Frozen wire-value snapshot.
#
# Every ``int``-valued ``Enum`` defined in ``notebooklm.rpc.types`` is pinned to
# its exact ``{member_name: value}`` map, INCLUDING value-aliases like
# ``QuizQuantity.MORE`` and ``ArtifactTypeCode.QUIZ_FLASHCARD`` (the map is built
# from ``__members__``, so aliases are frozen too — an alias whose wire value
# drifted independently would otherwise slip past the gate).
#
# To change a value here you MUST edit this snapshot in the SAME PR — that diff
# line is the deliberate, reviewed acknowledgement that a wire contract moved.
# ---------------------------------------------------------------------------

_RPC_ENUM_SNAPSHOT: dict[str, dict[str, int]] = {
    "ArtifactStatus": {"PROCESSING": 1, "PENDING": 2, "COMPLETED": 3, "FAILED": 4},
    "ArtifactTypeCode": {
        "AUDIO": 1,
        "REPORT": 2,
        "VIDEO": 3,
        "QUIZ": 4,
        "QUIZ_FLASHCARD": 4,  # value-alias of QUIZ
        "MIND_MAP": 5,
        "INFOGRAPHIC": 7,
        "SLIDE_DECK": 8,
        "DATA_TABLE": 9,
    },
    "AudioFormat": {"DEEP_DIVE": 1, "BRIEF": 2, "CRITIQUE": 3, "DEBATE": 4},
    "AudioLength": {"SHORT": 1, "DEFAULT": 2, "LONG": 3},
    "ChatGoal": {"DEFAULT": 1, "CUSTOM": 2, "LEARNING_GUIDE": 3},
    "ChatResponseLength": {"DEFAULT": 1, "LONGER": 4, "SHORTER": 5},
    "ExportType": {"DOCS": 1, "SHEETS": 2},
    "InfographicDetail": {"CONCISE": 1, "STANDARD": 2, "DETAILED": 3},
    "InfographicOrientation": {"LANDSCAPE": 1, "PORTRAIT": 2, "SQUARE": 3},
    "InfographicStyle": {
        "AUTO_SELECT": 1,
        "SKETCH_NOTE": 2,
        "PROFESSIONAL": 3,
        "BENTO_GRID": 4,
        "EDITORIAL": 5,
        "INSTRUCTIONAL": 6,
        "BRICKS": 7,
        "CLAY": 8,
        "ANIME": 9,
        "KAWAII": 10,
        "SCIENTIFIC": 11,
    },
    "QuizDifficulty": {"EASY": 1, "MEDIUM": 2, "HARD": 3},
    "QuizQuantity": {"FEWER": 1, "STANDARD": 2, "MORE": 2},  # MORE = value-alias of STANDARD
    "ShareAccess": {"RESTRICTED": 0, "ANYONE_WITH_LINK": 1},
    "SharePermission": {"OWNER": 1, "EDITOR": 2, "VIEWER": 3, "_REMOVE": 4},
    "ShareViewLevel": {"FULL_NOTEBOOK": 0, "CHAT_ONLY": 1},
    "SlideDeckFormat": {"DETAILED_DECK": 1, "PRESENTER_SLIDES": 2},
    "SlideDeckLength": {"DEFAULT": 1, "SHORT": 2},
    "SourceStatus": {"PROCESSING": 1, "READY": 2, "ERROR": 3, "PREPARING": 5},
    "VideoFormat": {"EXPLAINER": 1, "BRIEF": 2, "CINEMATIC": 3},
    "VideoStyle": {
        "AUTO_SELECT": 1,
        "CUSTOM": 0,
        "CLASSIC": 2,
        "WHITEBOARD": 3,
        "KAWAII": 9,
        "ANIME": 7,
        "WATERCOLOR": 6,
        "RETRO_PRINT": 8,
        "HERITAGE": 4,
        "PAPER_CRAFT": 5,
    },
}


def _discover_int_enums() -> dict[str, type[enum.Enum]]:
    """Every ``int``-valued ``Enum`` class actually DEFINED in ``rpc/types.py``.

    A wire enum is a class that subclasses both ``int`` and ``enum.Enum`` and is
    defined in this module (``__module__`` guard excludes anything imported in).
    ``str``-valued enums (``RPCMethod``, ``ReportFormat``, ``DriveMimeType``) are
    excluded because they are not ``int`` subclasses.
    """
    return {
        name: obj
        for name, obj in vars(rpc_types).items()
        if inspect.isclass(obj)
        and obj.__module__ == rpc_types.__name__
        and issubclass(obj, int)
        and issubclass(obj, enum.Enum)
    }


def _live_value_map(enum_cls: type[enum.Enum]) -> dict[str, int]:
    """Full ``{member_name: value}`` map for an int enum, INCLUDING value-aliases.

    Iterating the enum directly (``for m in enum_cls``) silently drops aliases
    (members sharing a value with a canonical member), so an alias whose wire
    value drifted independently would never be caught. ``__members__`` exposes
    every defined name — canonical and alias — so each is frozen.
    """
    return {name: int(member.value) for name, member in enum_cls.__members__.items()}


@pytest.mark.parametrize("enum_name", sorted(_RPC_ENUM_SNAPSHOT))
def test_rpc_enum_values_frozen(enum_name: str) -> None:
    """The live wire integers of each int-Enum must equal the frozen snapshot.

    These integers are an undocumented contract with Google's backend; a silent
    change produces wrong-output-without-error (see #1597, VideoStyle).
    """
    enum_cls = getattr(rpc_types, enum_name, None)
    assert enum_cls is not None, (
        f"_RPC_ENUM_SNAPSHOT pins {enum_name}, but it no longer exists in "
        "rpc/types.py. Remove it from _RPC_ENUM_SNAPSHOT in this PR (a removed "
        "wire enum is a deliberate, reviewed change)."
    )
    live = _live_value_map(enum_cls)
    assert live == _RPC_ENUM_SNAPSHOT[enum_name], (
        f"Wire values for {enum_name} changed.\n"
        f"  snapshot: {_RPC_ENUM_SNAPSHOT[enum_name]}\n"
        f"  live:     {live}\n"
        "If intended, update the snapshot in this PR (the deliberate ack). "
        "A purely ADDED member -> just bump the snapshot. For a CHANGED or "
        "REMOVED value, ALSO add a `changed-enum-value` row to "
        "scripts/api-compat-allowlist.json (see #1597), since changing the wire "
        "value of an existing member is a public-API break."
    )


def test_snapshot_covers_every_int_enum() -> None:
    """Every int-Enum defined in rpc/types.py must be pinned in the snapshot.

    Fails if someone adds a brand-new wire enum without freezing it. ``str``
    enums (``RPCMethod``, ``ReportFormat``) are excluded by construction.
    """
    discovered = set(_discover_int_enums())
    pinned = set(_RPC_ENUM_SNAPSHOT)

    missing = discovered - pinned
    extra = pinned - discovered
    assert not missing, (
        f"New int-valued wire enum(s) not pinned in _RPC_ENUM_SNAPSHOT: {sorted(missing)}. "
        "Add each with its exact {member_name: value} map so its wire contract is frozen."
    )
    assert not extra, (
        f"_RPC_ENUM_SNAPSHOT pins enum(s) no longer defined in rpc/types.py: {sorted(extra)}."
    )


def test_snapshot_is_non_trivial() -> None:
    """Guard the discovery itself: it must find the known anchor wire enums.

    A discovery bug returning an empty/degenerate set would make the per-enum
    freeze sweep vacuously pass.
    """
    discovered = _discover_int_enums()
    for anchor in ("VideoStyle", "AudioFormat", "InfographicStyle", "ArtifactTypeCode"):
        assert anchor in discovered, f"discovery dropped the anchor wire enum {anchor!r}"
    # str-valued enums must never be treated as wire int enums.
    assert "RPCMethod" not in discovered
    assert "ReportFormat" not in discovered
