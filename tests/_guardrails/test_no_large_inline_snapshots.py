"""Prevent new large hand-maintained snapshots in guardrail modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._baselines.guardrail_literals import (
    LARGE_LITERAL_THRESHOLD,
    guardrail_literal_growth,
    inventory_large_inline_literals,
)
from tests._baselines.module_size import module_size_growth
from tests._baselines.registry import baseline_by_name
from tests._baselines.storage_transaction_policy import storage_transaction_policy_growth

pytestmark = pytest.mark.repo_lint


def test_large_inline_literal_inventory_matches_registered_baseline() -> None:
    committed = baseline_by_name("guardrail_inline_literals").load()
    assert inventory_large_inline_literals() == committed, (
        "Large inline guardrail literals changed. Prefer a derived registry baseline; "
        "otherwise regenerate with `python scripts/regen_baselines.py` (new or larger "
        "literals require `--allow-growth`)."
    )


def test_large_inline_literal_detector_bites_and_ignores_small_literals(tmp_path: Path) -> None:
    """Red-fires-red proof for the meta-guard's real detector."""
    guardrails = tmp_path / "tests" / "_guardrails"
    guardrails.mkdir(parents=True)
    leaves = ", ".join(repr(index) for index in range(LARGE_LITERAL_THRESHOLD))
    (guardrails / "test_synthetic.py").write_text(
        f"_FROZEN = [{leaves}]\n_SMALL = [1, 2]\n",
        encoding="utf-8",
    )
    (guardrails / "_synthetic.py").write_text(
        f"_WRAPPED = frozenset({{{leaves}}})\n",
        encoding="utf-8",
    )

    assert inventory_large_inline_literals(guardrails, project_root=tmp_path) == {
        "tests/_guardrails/_synthetic.py": {"_WRAPPED": LARGE_LITERAL_THRESHOLD},
        "tests/_guardrails/test_synthetic.py": {"_FROZEN": LARGE_LITERAL_THRESHOLD},
    }


def test_growth_policies_detect_only_weakening_changes() -> None:
    """Red-fires-red proof for the three shrink-only registry policies."""
    assert module_size_growth(
        {
            "budget": 100,
            "allowlisted_ceilings": {"fat.py": 110},
            "shrink_locked_ceilings": {"locked.py": 90},
        },
        {
            "budget": 101,
            "allowlisted_ceilings": {"fat.py": 111, "new.py": 120},
            "shrink_locked_ceilings": {"locked.py": 90},
        },
    ) == [
        "budget: 100 -> 101",
        "allowlisted_ceilings.fat.py: 110 -> 111",
        "allowlisted_ceilings.new.py: new ceiling 120",
    ]
    assert (
        module_size_growth(
            {
                "budget": 100,
                "allowlisted_ceilings": {"fat.py": 110},
                "shrink_locked_ceilings": {"locked.py": 90},
            },
            {
                "budget": 100,
                "allowlisted_ceilings": {"fat.py": 105},
                "shrink_locked_ceilings": {"locked.py": 80},
            },
        )
        == []
    )
    assert module_size_growth(
        {
            "budget": 100,
            "allowlisted_ceilings": {},
            "shrink_locked_ceilings": {"locked.py": 90},
        },
        {
            "budget": 100,
            "allowlisted_ceilings": {"locked.py": 101},
            "shrink_locked_ceilings": {},
        },
    ) == [
        "locked.py: shrink_locked_ceilings 90 -> allowlisted_ceilings 101",
    ]
    assert (
        module_size_growth(
            {
                "budget": 100,
                "allowlisted_ceilings": {"locked.py": 101},
                "shrink_locked_ceilings": {},
            },
            {
                "budget": 100,
                "allowlisted_ceilings": {},
                "shrink_locked_ceilings": {"locked.py": 90},
            },
        )
        == []
    )
    assert module_size_growth(
        {
            "budget": 100,
            "allowlisted_ceilings": {},
            "shrink_locked_ceilings": {"locked.py": 90},
        },
        {
            "budget": 100,
            "allowlisted_ceilings": {},
            "shrink_locked_ceilings": {},
        },
    ) == ["shrink_locked_ceilings.locked.py: protection removed"]

    assert storage_transaction_policy_growth(
        {"raise_on_lock_unavailable": ["owner.one"]},
        {"raise_on_lock_unavailable": ["owner.one", "owner.two"]},
    ) == ["raise_on_lock_unavailable: new caller owner.two"]
    assert (
        storage_transaction_policy_growth(
            {"raise_on_lock_unavailable": ["owner.one", "owner.two"]},
            {"raise_on_lock_unavailable": ["owner.one"]},
        )
        == []
    )

    assert guardrail_literal_growth(
        {"tests/_guardrails/test_a.py": {"_FROZEN": 20}},
        {
            "tests/_guardrails/test_a.py": {"_FROZEN": 21},
            "tests/_guardrails/test_b.py": {"_NEW": 30},
        },
    ) == [
        "tests/_guardrails/test_a.py:_FROZEN: 20 -> 21 literal leaves",
        "tests/_guardrails/test_b.py:_NEW: new 30-leaf literal",
    ]
