"""Validation tests for the test relevance and routing ledger (ADR-0022 / Phase 1).

Guards that the relevance ledger:
1. Conforms to the required schema and decision vocabulary.
2. Contains complete metadata for every reviewed test node (owner, rationale, oracle, failure).
3. Enforces that historical entries have explicit reasons, owners, and review conditions.
4. Enforces disjoint sets between PR execution and historical qualification.
5. Keeps key architectural invariants (v1.0 gates, promoted lifecycle cases) correctly classified.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = REPO_ROOT / "tests" / "fixtures" / "test_relevance_ledger.json"

VALID_DECISIONS = {
    "pr_routine",
    "pr_contract",
    "extended_qualification",
    "historical_manual",
    "consolidate",
    "deleted_after_replacement",
}

REQUIRED_ENTRY_FIELDS = {
    "nodeid",
    "file",
    "subsystem",
    "failure_detected",
    "oracle",
    "overlapping_tests",
    "runtime_cost",
    "decision",
    "owner",
    "rationale",
    "replacement",
    "review_condition",
}


@pytest.fixture(scope="module")
def ledger_data() -> dict[str, object]:
    assert LEDGER_PATH.is_file(), f"Relevance ledger missing at {LEDGER_PATH}"
    with LEDGER_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def test_ledger_schema_and_summary(ledger_data: dict[str, object]) -> None:
    assert ledger_data.get("version") == "1.0.0"
    schema = ledger_data.get("schema")
    assert isinstance(schema, dict)
    assert set(schema.keys()) >= REQUIRED_ENTRY_FIELDS

    summary = ledger_data.get("summary")
    assert isinstance(summary, dict)
    total_entries = summary.get("total_entries")
    assert isinstance(total_entries, int) and total_entries > 900

    by_decision = summary.get("by_decision")
    assert isinstance(by_decision, dict)
    assert set(by_decision.keys()) <= VALID_DECISIONS
    assert sum(by_decision.values()) == total_entries


def test_every_entry_has_complete_metadata(ledger_data: dict[str, object]) -> None:
    entries = ledger_data.get("entries")
    assert isinstance(entries, list) and len(entries) > 0

    seen_nodeids: set[str] = set()
    for entry in entries:
        assert isinstance(entry, dict)
        assert set(entry.keys()) == REQUIRED_ENTRY_FIELDS

        nodeid = entry["nodeid"]
        assert nodeid and isinstance(nodeid, str)
        assert nodeid not in seen_nodeids, f"Duplicate nodeid in ledger: {nodeid}"
        seen_nodeids.add(nodeid)

        assert entry["file"] and isinstance(entry["file"], str)
        assert entry["subsystem"] and isinstance(entry["subsystem"], str)
        assert entry["failure_detected"] and isinstance(entry["failure_detected"], str)
        assert entry["oracle"] and isinstance(entry["oracle"], str)
        assert isinstance(entry["overlapping_tests"], list)
        assert entry["runtime_cost"] and isinstance(entry["runtime_cost"], str)
        assert entry["owner"] and isinstance(entry["owner"], str)
        assert entry["review_condition"] and isinstance(entry["review_condition"], str)
        assert entry["rationale"] and isinstance(entry["rationale"], str)

        decision = entry["decision"]
        assert decision in VALID_DECISIONS, f"Invalid decision {decision!r} on {nodeid}"


def test_historical_entries_have_rationales_and_owners(ledger_data: dict[str, object]) -> None:
    entries = ledger_data.get("entries", [])
    assert isinstance(entries, list)

    historical_entries = [e for e in entries if e["decision"] == "historical_manual"]
    assert len(historical_entries) >= 20, "Expected substantial historical entries"

    for entry in historical_entries:
        assert entry["rationale"], f"Historical entry {entry['nodeid']} missing rationale"
        assert entry["owner"], f"Historical entry {entry['nodeid']} missing owner"
        assert entry["review_condition"], (
            f"Historical entry {entry['nodeid']} missing review_condition"
        )


def test_consolidated_entries_specify_replacements(ledger_data: dict[str, object]) -> None:
    entries = ledger_data.get("entries", [])
    assert isinstance(entries, list)

    consolidated = [
        e for e in entries if e["decision"] in ("consolidate", "deleted_after_replacement")
    ]
    for entry in consolidated:
        assert entry["replacement"], f"Entry {entry['nodeid']} requires a replacement reference"


def test_pr_and_historical_sets_are_disjoint(ledger_data: dict[str, object]) -> None:
    entries = ledger_data.get("entries", [])
    assert isinstance(entries, list)

    pr_nodes = {e["nodeid"] for e in entries if e["decision"] in ("pr_routine", "pr_contract")}
    historical_nodes = {e["nodeid"] for e in entries if e["decision"] == "historical_manual"}

    overlap = pr_nodes & historical_nodes
    assert not overlap, f"PR and historical nodes must be disjoint; overlap: {overlap}"


def test_key_architectural_decisions_are_pinned(ledger_data: dict[str, object]) -> None:
    entries = {e["nodeid"]: e for e in ledger_data.get("entries", [])}

    # v1.0 gates MUST be active PR contracts (not historical)
    v1_gate = [e for nid, e in entries.items() if "test_v100_release_gate.py" in nid]
    assert v1_gate and all(e["decision"] == "pr_contract" for e in v1_gate)

    v1_deprecations = [
        e for nid, e in entries.items() if "test_v100_deprecation_coverage.py" in nid
    ]
    assert v1_deprecations and all(e["decision"] == "pr_contract" for e in v1_deprecations)

    # v0.8 completed migration MUST be historical_manual
    v08_deprecations = [
        e for nid, e in entries.items() if "test_v080_deprecation_coverage.py" in nid
    ]
    assert v08_deprecations and all(e["decision"] == "historical_manual" for e in v08_deprecations)

    # Promoted lifecycle waves MUST be pr_routine
    promoted_waves = [
        "tests/unit/test_client_lifecycle_waves.py::test_open_is_transactional_and_concurrent_callers_coalesce",
        "tests/unit/test_client_lifecycle_waves.py::test_open_failure_rolls_back_every_transport_and_preserves_original",
        "tests/unit/test_client_lifecycle_waves.py::test_cancelling_non_owner_open_does_not_abort_owner",
        "tests/unit/test_client_lifecycle_waves.py::test_cancelled_close_aborts_hung_graceful_wait_but_finishes_teardown",
        "tests/unit/test_client_lifecycle_waves.py::test_close_reopen_allocates_a_new_resource_epoch",
        "tests/unit/test_client_lifecycle_waves.py::test_registered_child_self_close_fails_fast_without_leaking_admission",
        "tests/unit/test_client_lifecycle_waves.py::test_poll_callback_self_close_fails_fast_and_poll_settles_once",
    ]
    for wave in promoted_waves:
        assert wave in entries, f"Promoted lifecycle wave missing: {wave}"
        assert entries[wave]["decision"] == "pr_routine"
