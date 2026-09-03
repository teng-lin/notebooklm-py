from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tests._baselines.registry import (
    _derive_auth_patch_sites,
    _derive_auth_shared_mutations,
    _derive_browser_patch_sites,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SURVIVOR_POLICY = REPO_ROOT / "tests/fixtures/policies/auth_patch_survivors.json"
LIFECYCLE_POLICY = REPO_ROOT / "tests/fixtures/policies/auth_lifecycle_cleanup.json"

JOINT_IDENTITY = (
    "package",
    "module",
    "attribute",
    "idiom",
    "path",
    "owner_qualname",
    "owner_kind",
)
SURVIVOR_FIELDS = {
    *JOINT_IDENTITY,
    "category",
    "ceiling",
    "count",
    "owner",
    "reason",
    "removal_trigger",
}
APPROVED_CATEGORIES = {
    "compat_adapter",
    "sealed_fault",
    "runtime_gateway",
    "composition_probe",
}
LIFECYCLE_IDENTITY = (
    "path",
    "owner_qualname",
    "owner_kind",
    "production_owner",
    "method",
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"{path.relative_to(REPO_ROOT)} must contain an object"
    return value


def _counter_delta(
    left: Counter[tuple[object, ...]], right: Counter[tuple[object, ...]]
) -> dict[tuple[object, ...], int]:
    return dict(sorted((left - right).items(), key=lambda item: tuple(map(str, item[0]))))


@pytest.mark.repo_lint
def test_auth_patch_survivor_policy_exactly_matches_every_live_joint_row() -> None:
    policy = _load_json(SURVIVOR_POLICY)
    assert set(policy) == {"version", "survivors"}
    assert policy["version"] == 1
    rows = policy["survivors"]
    assert isinstance(rows, list) and rows

    policy_identities: list[tuple[object, ...]] = []
    for row in rows:
        assert isinstance(row, dict)
        assert set(row) == SURVIVOR_FIELDS, f"wrong survivor fields: {set(row) ^ SURVIVOR_FIELDS}"
        assert row["package"] in {"notebooklm._auth", "notebooklm._browser"}
        assert row["owner_kind"] == "test", "module-patch survivors cannot live in shared helpers"
        assert row["category"] in APPROVED_CATEGORIES
        assert type(row["count"]) is int and row["count"] > 0
        assert type(row["ceiling"]) is int and row["ceiling"] == row["count"]
        for field in (*JOINT_IDENTITY[1:], "owner", "reason", "removal_trigger"):
            assert isinstance(row[field], str) and row[field].strip(), f"empty {field}: {row}"
        assert row["path"].startswith("tests/")
        assert not any(token in row["path"] for token in ("*", "?", "["))
        policy_identities.append(tuple(row[field] for field in JOINT_IDENTITY))

    assert len(policy_identities) == len(set(policy_identities)), "duplicate survivor identity"
    expected = Counter(
        {tuple(row[field] for field in JOINT_IDENTITY): row["count"] for row in rows}
    )

    projections = (_derive_auth_patch_sites(), _derive_browser_patch_sites())
    live_rows: list[dict[str, object]] = []
    for projection, package in zip(
        projections, ("notebooklm._auth", "notebooklm._browser"), strict=True
    ):
        assert projection["version"] == 2
        assert projection["package"] == package
        live_rows.extend(projection["joint_sites"])
    actual = Counter(
        {tuple(row[field] for field in JOINT_IDENTITY): int(row["count"]) for row in live_rows}
    )

    assert actual == expected, (
        "survivor policy is not the exact live joint-row multiset; "
        f"missing_or_decreased={_counter_delta(expected, actual)!r}; "
        f"extra_or_increased={_counter_delta(actual, expected)!r}"
    )


@pytest.mark.repo_lint
def test_shared_helper_and_fixture_mutations_are_exact_lifecycle_operations() -> None:
    lifecycle = _load_json(LIFECYCLE_POLICY)
    assert lifecycle.get("version") == 1
    operations = lifecycle.get("operations")
    assert isinstance(operations, list)

    allowed_identities: list[tuple[object, ...]] = []
    allowed: Counter[tuple[object, ...]] = Counter()
    for operation in operations:
        assert isinstance(operation, dict)
        assert set(LIFECYCLE_IDENTITY) <= operation.keys()
        assert type(operation.get("count")) is int and operation["count"] > 0
        if operation["production_owner"] == "notebooklm._auth.cookie_policy":
            # The synchronized cookie reset is an exact module lifecycle operation,
            # not a shared-owner mutation emitted by this projection.
            continue
        identity = tuple(operation[field] for field in LIFECYCLE_IDENTITY)
        allowed_identities.append(identity)
        allowed[identity] += operation["count"]
    assert len(allowed_identities) == len(set(allowed_identities)), (
        "lifecycle policy has duplicate shared-mutation identities"
    )

    live: Counter[tuple[object, ...]] = Counter()
    projection = _derive_auth_shared_mutations()
    for row in projection["mutations"]:
        if row["owner_kind"] == "test":
            continue
        identity = (
            row["path"],
            row["owner_qualname"],
            row["owner_kind"],
            row["owner"],
            row["attribute"],
        )
        live[identity] += int(row["count"])

    # The lifecycle policy also owns fresh local cleanup operations (for
    # example cookie-warning owner resets), which the shared-owner collector
    # deliberately does not report.  Intersect on the complete shared identity
    # while retaining the policy count, so a new path/owner or count still fails.
    permitted_shared = Counter(
        {identity: count for identity, count in allowed.items() if identity in live}
    )
    assert live == permitted_shared, (
        "non-test-owned shared mutations must be exact lifecycle-policy operations; "
        f"policy_count_excess={_counter_delta(permitted_shared, live)!r}; "
        f"unclassified_or_increased={_counter_delta(live, permitted_shared)!r}"
    )
