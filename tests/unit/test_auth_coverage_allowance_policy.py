from __future__ import annotations

from datetime import date

import pytest
from scripts.check_auth_coverage_delta import CoverageDeltaError, validate_allowances


def _policy(**updates):
    row = {
        "id": "shifted-branch",
        "path": "src/notebooklm/_auth/refresh.py",
        "kind": "branch",
        "base_coordinate": [10, 12],
        "head_coordinate": [11, 13],
        "disposition": "mapped",
        "scenario_id": "refresh-ordering",
        "rationale": "branch moved with the owner extraction",
        "owner": "auth maintainers",
        "valid_for_base_sha": "abc123",
        "authored_on": "2026-09-01",
        "expires_on": "2026-09-14",
    }
    row.update(updates)
    return {"version": 1, "allowances": [row]}


def test_allowance_is_exact_short_lived_and_scenario_linked() -> None:
    rows = validate_allowances(
        _policy(),
        base_sha="abc123",
        scenario_ids={"refresh-ordering": {"src/notebooklm/_auth/refresh.py"}},
        today=date(2026, 9, 3),
    )
    assert rows[0]["id"] == "shifted-branch"


@pytest.mark.parametrize(
    ("updates", "match"),
    [
        ({"valid_for_base_sha": "wrong"}, "merge base"),
        ({"authored_on": "2026-09-04"}, "future"),
        ({"expires_on": "2026-09-20"}, "14 days"),
        ({"expires_on": "2026-09-02"}, "expired"),
        ({"scenario_id": "missing"}, "scenario link"),
    ],
)
def test_invalid_allowance_fails(updates, match) -> None:
    with pytest.raises(CoverageDeltaError, match=match):
        validate_allowances(
            _policy(**updates),
            base_sha="abc123",
            scenario_ids={"refresh-ordering": {"src/notebooklm/_auth/refresh.py"}},
            today=date(2026, 9, 3),
        )


def test_allowance_scenario_must_cover_the_same_source_path() -> None:
    with pytest.raises(CoverageDeltaError, match="does not cover"):
        validate_allowances(
            _policy(),
            base_sha="abc123",
            scenario_ids={"refresh-ordering": {"src/notebooklm/_auth/recovery.py"}},
            today=date(2026, 9, 3),
        )
