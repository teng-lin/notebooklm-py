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
    _service_of,
    classify_service,
    diff,
    extract_registry,
    main,
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
