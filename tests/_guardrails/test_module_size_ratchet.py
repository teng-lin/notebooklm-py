"""Module-size invariants backed by the ADR-0022 baseline registry.

The authored policy is a global line budget plus four ADR-0033 shrink locks.
Measured ceilings are derived from the live source tree and stored in
``tests/fixtures/baselines/module_size.json``. Tightening them is the ordinary
regen path; adding or raising a ceiling requires ``--allow-growth``.
"""

from __future__ import annotations

from typing import cast

import pytest

from tests._baselines.module_size import (
    MODULE_SIZE_BUDGET as POLICY_MODULE_SIZE_BUDGET,
)
from tests._baselines.module_size import (
    measure_modules,
)
from tests._baselines.registry import baseline_by_name

pytestmark = pytest.mark.repo_lint


def _committed_policy() -> dict[str, object]:
    value = baseline_by_name("module_size").load()
    assert isinstance(value, dict)
    return value


_POLICY = _committed_policy()
MODULE_SIZE_BUDGET = cast(int, _POLICY["budget"])
ALLOWLISTED_CEILINGS = cast(dict[str, int], _POLICY["allowlisted_ceilings"])
SHRINK_LOCKED_CEILINGS = cast(dict[str, int], _POLICY["shrink_locked_ceilings"])


def _over_budget_offenders(
    measured: dict[str, int], allowlist: dict[str, int], budget: int
) -> dict[str, int]:
    """Return unallowlisted modules strictly over the global budget."""
    return {
        path: lines for path, lines in measured.items() if lines > budget and path not in allowlist
    }


def _grown_offenders(
    measured: dict[str, int], ceilings: dict[str, int]
) -> dict[str, tuple[int, int]]:
    """Return pinned modules whose current line count exceeds the pin."""
    return {
        path: (measured[path], ceiling)
        for path, ceiling in ceilings.items()
        if path in measured and measured[path] > ceiling
    }


def _slack_offenders(
    measured: dict[str, int], ceilings: dict[str, int]
) -> dict[str, dict[str, int]]:
    """Return pinned modules that shrank and should tighten their pin."""
    return {
        path: {"current": measured[path], "recorded_ceiling": ceiling}
        for path, ceiling in ceilings.items()
        if path in measured and measured[path] < ceiling
    }


def _stale_entries(measured: dict[str, int], ceilings: dict[str, int]) -> list[str]:
    return sorted(path for path in ceilings if path not in measured)


def test_baseline_budget_matches_authored_policy() -> None:
    assert MODULE_SIZE_BUDGET == POLICY_MODULE_SIZE_BUDGET


def test_no_new_modules_over_budget() -> None:
    offenders = _over_budget_offenders(measure_modules(), ALLOWLISTED_CEILINGS, MODULE_SIZE_BUDGET)
    assert offenders == {}, (
        f"Module(s) exceed the {MODULE_SIZE_BUDGET}-line budget: {offenders}. "
        "Split them, or—only after explicit review—regenerate with "
        "`python scripts/regen_baselines.py --allow-growth`."
    )


def test_allowlisted_modules_do_not_exceed_their_ceiling() -> None:
    grown = _grown_offenders(measure_modules(), ALLOWLISTED_CEILINGS)
    assert grown == {}, (
        f"Allowlisted module(s) grew past their committed ceilings: {grown}. "
        "Split the growth, or explicitly acknowledge a reviewed exception with "
        "`python scripts/regen_baselines.py --allow-growth`."
    )


def test_allowlisted_ceilings_ratchet_down() -> None:
    slack = _slack_offenders(measure_modules(), ALLOWLISTED_CEILINGS)
    assert slack == {}, (
        f"Allowlisted module(s) shrank: {slack}. Lock in the saved ground with "
        "`python scripts/regen_baselines.py`."
    )


def test_shrink_locked_modules_do_not_exceed_their_pin() -> None:
    grown = _grown_offenders(measure_modules(), SHRINK_LOCKED_CEILINGS)
    assert grown == {}, (
        f"ADR-0033 shrink-locked module(s) grew past their pins: {grown}. "
        "Split the growth, or explicitly acknowledge a sanctioned exception with "
        "`python scripts/regen_baselines.py --allow-growth`."
    )


def test_shrink_locked_ceilings_ratchet_down() -> None:
    slack = _slack_offenders(measure_modules(), SHRINK_LOCKED_CEILINGS)
    assert slack == {}, (
        f"ADR-0033 shrink-locked module(s) shrank: {slack}. Tighten the baseline with "
        "`python scripts/regen_baselines.py`; do not delete under-budget shrink locks."
    )


def test_ceiling_sets_are_disjoint_and_not_stale() -> None:
    measured = measure_modules()
    overlap = sorted(set(SHRINK_LOCKED_CEILINGS) & set(ALLOWLISTED_CEILINGS))
    assert overlap == [], f"A module cannot carry both ceiling kinds: {overlap}"
    missing = _stale_entries(measured, SHRINK_LOCKED_CEILINGS)
    missing.extend(_stale_entries(measured, ALLOWLISTED_CEILINGS))
    assert missing == [], f"Committed module-size baseline contains stale paths: {sorted(missing)}"


def test_budget_is_below_every_allowlisted_ceiling() -> None:
    redundant = {
        path: ceiling
        for path, ceiling in ALLOWLISTED_CEILINGS.items()
        if ceiling <= MODULE_SIZE_BUDGET
    }
    assert redundant == {}, (
        "Over-budget allowlist entries at or below the global budget are redundant; "
        f"regenerate the baseline: {redundant}"
    )


def test_ratchet_checks_detect_their_offending_shapes() -> None:
    """Red-fires-red proof for every pure module-size detector."""
    budget = 900
    ceilings = {"fat.py": 1000}

    measured = {"new_fat.py": 950, "fat.py": 1000, "small.py": 10}
    assert _over_budget_offenders(measured, ceilings, budget) == {"new_fat.py": 950}
    assert _over_budget_offenders({"edge.py": budget}, ceilings, budget) == {}

    assert _grown_offenders({"fat.py": 1001}, ceilings) == {"fat.py": (1001, 1000)}
    assert _grown_offenders({"fat.py": 1000}, ceilings) == {}
    assert _slack_offenders({"fat.py": 950}, ceilings) == {
        "fat.py": {"current": 950, "recorded_ceiling": 1000}
    }
    assert _slack_offenders({"fat.py": 1000}, ceilings) == {}
    assert _stale_entries({}, ceilings) == ["fat.py"]


def test_credential_store_and_migration_modules_use_the_ordinary_budget() -> None:
    """Retain the boundary invariant without transient exact-LOC snapshots."""
    modules = {
        "_auth/credential_io.py",
        "_auth/master_token_file.py",
        "_auth/profile_migration.py",
        "_auth/profile_store.py",
    }
    measured = measure_modules()
    assert modules.isdisjoint(ALLOWLISTED_CEILINGS)
    assert {path: measured[path] for path in modules if measured[path] > MODULE_SIZE_BUDGET} == {}

    synthetic = dict.fromkeys(modules, MODULE_SIZE_BUDGET + 1)
    assert _over_budget_offenders(synthetic, {}, MODULE_SIZE_BUDGET) == synthetic
