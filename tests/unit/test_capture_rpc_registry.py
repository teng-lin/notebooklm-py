"""Unit tests for ``scripts/capture_rpc_registry.py`` (offline; no network/auth).

Covers the pure parse/extract/diff logic, including the edge cases that bit the
original prototype: non-id enum constants (``blog_post``) must be filtered, and an
id that is present in the bundle but not parsed must NOT be reported as a rotation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.capture_rpc_registry import (
    _normalize_label,
    _service_of,
    classify_service,
    diff,
    diff_enums,
    extract_proto_assertions,
    extract_quota_codes,
    extract_registry,
    extract_switch_enums,
    main,
    parse_enum_members_from_text,
    parse_ids_from_text,
)

# Mixed quote styles on purpose — exercises the quote-agnostic parsing of both
# the enum (CREATE is single-quoted) and the bundle (the CCqFvf registration).
_TYPES = """
class RPCMethod(str, Enum):
    LIST = "wXbhsf"
    CREATE = 'CCqFvf'
    GONE = "ZZxxYY"
    UNPARSED = "PuPpY1"
    NOT_AN_ID = "blog_post"

class SomethingElse(str, Enum):
    OTHER = "abcdef"
"""

# Two well-formed registrations, one unmapped registration, and the UNPARSED id
# present only as a bare string (not in registration form).
_BUNDLE = (
    'x=new _.uD("wXbhsf",kF,csb,[_.Ue,!1,_.Se,"/Svc.List"]);'
    "y=new _.uD('CCqFvf',a.b,c,[_.Ue,!0,_.Se,'/Svc.Create']);"
    'z=new _.uD("NewOne",p,q,[_.Ue,!1,_.Se,"/Svc.Brand"]);'
    "log('PuPpY1');"
)


def test_parse_ids_filters_non_ids_and_other_enums() -> None:
    ids = parse_ids_from_text(_TYPES)
    # blog_post (underscore) filtered out; SomethingElse.OTHER excluded (different class)
    assert ids == {
        "wXbhsf": "LIST",
        "CCqFvf": "CREATE",
        "ZZxxYY": "GONE",
        "PuPpY1": "UNPARSED",
    }
    # "abcdef" passes the _RPC_ID_RE filter on its own; it is excluded *only* by
    # the class-scope regex (it lives in SomethingElse). Assert that explicitly.
    assert "abcdef" not in ids


def test_extract_registry() -> None:
    assert extract_registry(_BUNDLE) == {
        "wXbhsf": "/Svc.List",
        "CCqFvf": "/Svc.Create",
        "NewOne": "/Svc.Brand",
    }


def test_diff_buckets() -> None:
    ours = parse_ids_from_text(_TYPES)
    live = extract_registry(_BUNDLE)
    buckets = diff(ours, live, _BUNDLE)

    assert set(buckets["confirmed"]) == {"wXbhsf", "CCqFvf"}
    assert buckets["confirmed"]["wXbhsf"] == "/Svc.List"
    # GONE is nowhere in the bundle -> a real rotation/stale alarm
    assert set(buckets["absent"]) == {"ZZxxYY"}
    # UNPARSED appears as a string but not as a parsed registration -> not an alarm
    assert set(buckets["present_unparsed"]) == {"PuPpY1"}
    # NewOne is declared by the bundle but absent from our enum
    assert set(buckets["unmapped"]) == {"NewOne"}


def test_main_bundle_file_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """End-to-end offline run of main() via --bundle-file / --types (no network/auth).

    Also exercises the __file__-independent path handling and the --check exit code.
    """
    types = tmp_path / "types.py"
    types.write_text(_TYPES, encoding="utf-8")
    bundle = tmp_path / "bundle.js"
    bundle.write_text(_BUNDLE, encoding="utf-8")

    rc = main(["--bundle-file", str(bundle), "--types", str(types)])
    out = capsys.readouterr().out
    assert rc == 0  # no --check -> 0 even though an id is ABSENT
    assert "CONFIRMED: 2" in out
    assert "ABSENT: 1" in out
    assert "NewOne" in out  # an unmapped live RPC is listed

    # --check turns the ABSENT id (ZZxxYY/GONE) into a non-zero exit
    assert main(["--bundle-file", str(bundle), "--types", str(types), "--check"]) == 1


def test_service_of() -> None:
    assert _service_of("/LabsTailwindOrchestrationService.AddSources") == (
        "LabsTailwindOrchestrationService"
    )
    # Leading slash optional; only the segment before the first dot is the service.
    assert _service_of("NotebookService.CreateNotebook") == "NotebookService"


def test_classify_service() -> None:
    # `current_services` is what the run discovered empirically (services our
    # CONFIRMED ids resolve to) — it always wins, even for a name we'd otherwise
    # bucket elsewhere.
    current = {"LabsTailwindOrchestrationService", "DasherGrowthPromotionService"}

    # Empirical hit -> current.
    assert classify_service("LabsTailwindOrchestrationService", current) == "current"
    # A current-hit on a non-LabsTailwind name still wins via the empirical set.
    assert classify_service("DasherGrowthPromotionService", current) == "current"
    # Old family by prefix (not in the empirical set this run) -> current.
    assert classify_service("LabsTailwindSharingService", current) == "current"
    # Known Discovery-Engine domain services -> enterprise (Agentspace/Vertex surface).
    assert classify_service("NotebookService", current) == "enterprise"
    assert classify_service("SourceService", current) == "enterprise"
    # Anything else is an unclassified drift signal -> other.
    assert classify_service("InteractionEventService", current) == "other"
    # Empirical precedence: when a known DE service IS in the empirical current set,
    # "current" wins over the DE-name rule — locks the documented empirical-first
    # ordering (the ``in current_services`` check must precede the DE-set check).
    assert classify_service("NotebookService", {"NotebookService"}) == "current"


def test_main_json_includes_unmapped_family(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json carries the per-id unmapped ``{method, family}`` schema (contract guard)."""
    types = tmp_path / "types.py"
    types.write_text(_TYPES, encoding="utf-8")
    bundle = tmp_path / "bundle.js"
    bundle.write_text(_BUNDLE, encoding="utf-8")

    rc = main(["--bundle-file", str(bundle), "--types", str(types), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    # NewOne (/Svc.Brand) is bundle-declared but absent from our enum -> unmapped.
    # ``Svc`` is the service our CONFIRMED ids resolve to (wXbhsf/CCqFvf -> /Svc.List,
    # /Svc.Create), so empirical-first classification tags this family "current" — this
    # also exercises the empirical path through the --json output, not just the schema.
    assert payload["unmapped"]["NewOne"] == {"method": "/Svc.Brand", "family": "current"}


# ---------------------------------------------------------------------------
# Studio enum / quota / proto drift extractors + diff_enums
# ---------------------------------------------------------------------------

# Minimal int-enum fixtures mirroring rpc/types.py's VideoFormat / AudioFormat.
_ENUM_TYPES = """
class VideoFormat(int, Enum):
    EXPLAINER = 1
    BRIEF = 2
    CINEMATIC = 3

class AudioFormat(int, Enum):
    DEEP_DIVE = 1
    BRIEF = 2
    CRITIQUE = 3
    DEBATE = 4
"""

# A bundle with the two anchored switch blocks (each carries a NEW trailing label
# our enums lack — Short / Whiteboard Animation / Lecture), the Yp quota map, and
# a proto required-field assertion. Whitespace/quote style mirrors the live shapes.
_ENUM_BUNDLE = (
    'f=function(a){switch(a){case 1:return"Explainer";case 2:return"Brief";'
    'case 3:return"Cinematic";case 4:return"Short";case 5:return"Whiteboard Animation"}};'
    'g=function(b){switch(b){case 1:return"Deep Dive";case 2:return"Brief";'
    'case 3:return"Critique";case 4:return"Debate";case 5:return"Lecture"}};'
    'Yp=[[1,{status:"RESOURCE_EXHAUSTED",result:{message:"chat limits reached"}}],'
    '[6,{status:"RESOURCE_EXHAUSTED",result:{message:"video limits reached"}}]];'
    "e=\"ExplainerVideoArtifact is missing field 'generation_options'\";"
)


def test_normalize_label() -> None:
    assert _normalize_label("Deep Dive") == "DEEP_DIVE"
    assert _normalize_label("Whiteboard Animation") == "WHITEBOARD_ANIMATION"
    assert _normalize_label("Explainer") == "EXPLAINER"


def test_extract_switch_enums_label_anchoring() -> None:
    enums = extract_switch_enums(_ENUM_BUNDLE)
    # Both blocks are attributed to our enums purely by their anchor label subset.
    assert enums["VideoFormat"] == {
        1: "Explainer",
        2: "Brief",
        3: "Cinematic",
        4: "Short",
        5: "Whiteboard Animation",
    }
    assert enums["AudioFormat"] == {
        1: "Deep Dive",
        2: "Brief",
        3: "Critique",
        4: "Debate",
        5: "Lecture",
    }


def test_extract_switch_enums_drops_unanchored_blocks() -> None:
    # A switch block with no recognizable anchor label set is not attributed.
    bundle = 'switch(x){case 1:return"Mango";case 2:return"Papaya"}'
    assert extract_switch_enums(bundle) == {}


def test_parse_enum_members_from_text() -> None:
    assert parse_enum_members_from_text(_ENUM_TYPES, "VideoFormat") == {
        "EXPLAINER": 1,
        "BRIEF": 2,
        "CINEMATIC": 3,
    }
    # Unknown class -> empty (scoped to the named class body).
    assert parse_enum_members_from_text(_ENUM_TYPES, "Nonexistent") == {}


def test_extract_quota_codes() -> None:
    assert extract_quota_codes(_ENUM_BUNDLE) == {
        1: "chat limits reached",
        6: "video limits reached",
    }


def test_extract_proto_assertions() -> None:
    assert extract_proto_assertions(_ENUM_BUNDLE) == {
        ("ExplainerVideoArtifact", "generation_options"),
    }


def test_diff_enums_new_is_report_only() -> None:
    """Our enums match the bundle's leading codes; the trailing codes are NEW only."""
    live = extract_switch_enums(_ENUM_BUNDLE)
    buckets = diff_enums(_ENUM_TYPES, live)

    assert buckets["changed"] == []
    assert buckets["stale"] == []
    assert buckets["unparsed"] == []
    # Short (4) / Whiteboard Animation (5) / Lecture (5) are bundle codes we lack.
    new_pairs = {(r["enum"], r["code"], r["label"]) for r in buckets["new"]}
    assert new_pairs == {
        ("VideoFormat", 4, "Short"),
        ("VideoFormat", 5, "Whiteboard Animation"),
        ("AudioFormat", 5, "Lecture"),
    }


def test_diff_enums_changed_is_the_alarm() -> None:
    """A label whose integer differs from ours is CHANGED (the #1597 alarm)."""
    # Our EXPLAINER claims code 2, but the bundle returns "Explainer" for case 1.
    types = """
class VideoFormat(int, Enum):
    EXPLAINER = 2
    BRIEF = 1
    CINEMATIC = 3

class AudioFormat(int, Enum):
    DEEP_DIVE = 1
    BRIEF = 2
    CRITIQUE = 3
    DEBATE = 4
"""
    live = extract_switch_enums(_ENUM_BUNDLE)
    buckets = diff_enums(types, live)

    changed = {
        (r["enum"], r["member"], r["our_value"], r["live_value"]) for r in buckets["changed"]
    }
    assert changed == {
        ("VideoFormat", "EXPLAINER", 2, 1),
        ("VideoFormat", "BRIEF", 1, 2),
    }
    assert buckets["stale"] == []


def test_diff_enums_stale_when_value_not_a_live_code() -> None:
    """Our member's value is no longer any live code for that enum -> STALE."""
    # A trimmed VideoFormat bundle exposing only codes {1, 3}; our BRIEF = 2 is gone.
    bundle = 'switch(a){case 1:return"Explainer";case 3:return"Cinematic"}'
    types = """
class VideoFormat(int, Enum):
    EXPLAINER = 1
    BRIEF = 2
    CINEMATIC = 3
"""
    live = extract_switch_enums(bundle)
    buckets = diff_enums(types, live)

    stale = {(r["enum"], r["member"], r["our_value"]) for r in buckets["stale"]}
    assert stale == {("VideoFormat", "BRIEF", 2)}
    assert buckets["changed"] == []


def test_diff_enums_stale_when_value_repurposed_to_another_member() -> None:
    """Our member's value is still a live code but now labels a DIFFERENT member -> STALE.

    The integer code wasn't retired — it was *repurposed*. Our ``BRIEF = 2`` still
    sees code 2 in the bundle, but code 2 now returns "Cinematic", which normalizes
    to ``CINEMATIC`` (a different member already in our enum). Leaving BRIEF
    unflagged would silently keep our ``BRIEF`` name pointing at a code that now
    means Cinematic, so it must be STALE.
    """
    # Anchors (Explainer, Cinematic) present so the block attributes to VideoFormat.
    # No "Brief" label in the bundle; code 2 has been reused for "Cinematic".
    bundle = 'switch(a){case 1:return"Explainer";case 2:return"Cinematic"}'
    types = """
class VideoFormat(int, Enum):
    EXPLAINER = 1
    BRIEF = 2
    CINEMATIC = 2
"""
    live = extract_switch_enums(bundle)
    buckets = diff_enums(types, live)

    stale = {(r["enum"], r["member"], r["our_value"]) for r in buckets["stale"]}
    assert ("VideoFormat", "BRIEF", 2) in stale
    # CINEMATIC's name still matches the live label for code 2 -> not CHANGED.
    assert buckets["changed"] == []


def test_diff_enums_unparsed_when_no_block() -> None:
    """A known enum (we hold an anchor) with no switch block parsed -> UNPARSED."""
    # Only VideoFormat is present; AudioFormat has no block to attribute.
    bundle = 'switch(a){case 1:return"Explainer";case 2:return"Brief";case 3:return"Cinematic"}'
    live = extract_switch_enums(bundle)
    buckets = diff_enums(_ENUM_TYPES, live)

    assert {r["enum"] for r in buckets["unparsed"]} == {"AudioFormat"}
    assert buckets["changed"] == []
    assert buckets["stale"] == []


def test_check_enums_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--check-enums exits 1 on CHANGED/STALE only; NEW/UNPARSED stay 0. --check still works."""
    types = tmp_path / "types.py"
    bundle = tmp_path / "bundle.js"

    # Clean baseline (our enums == bundle leading codes; only NEW trailing labels).
    types.write_text(_ENUM_TYPES, encoding="utf-8")
    bundle.write_text(_ENUM_BUNDLE, encoding="utf-8")
    rc = main(["--bundle-file", str(bundle), "--types", str(types), "--check-enums"])
    out = capsys.readouterr().out
    assert rc == 0  # NEW labels never fail the enum gate
    assert "STUDIO ENUM DRIFT" in out
    assert "NEW: 3" in out

    # CHANGED -> exit 1.
    types.write_text(
        "class VideoFormat(int, Enum):\n    EXPLAINER = 2\n    BRIEF = 1\n    CINEMATIC = 3\n"
        "class AudioFormat(int, Enum):\n    DEEP_DIVE = 1\n    CRITIQUE = 3\n    DEBATE = 4\n",
        encoding="utf-8",
    )
    assert main(["--bundle-file", str(bundle), "--types", str(types), "--check-enums"]) == 1


def test_check_and_check_enums_combine(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--check (id rotation) and --check-enums combine; either firing exits 1."""
    # _TYPES has an ABSENT id (ZZxxYY/GONE); _BUNDLE has no switch enums.
    types = tmp_path / "types.py"
    types.write_text(_TYPES, encoding="utf-8")
    bundle = tmp_path / "bundle.js"
    bundle.write_text(_BUNDLE, encoding="utf-8")

    # --check alone fires on the ABSENT id even with no enum data present.
    assert main(["--bundle-file", str(bundle), "--types", str(types), "--check"]) == 1
    # Combining the flags still exits 1 (the id gate fires; enum gate sees nothing).
    assert (
        main(["--bundle-file", str(bundle), "--types", str(types), "--check", "--check-enums"]) == 1
    )
