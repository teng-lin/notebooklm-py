"""Focused tests for ADR-0022 baseline growth acknowledgement."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._baselines import module_size
from tests._baselines.registry import Baseline


def _new_items(previous: object, current: object) -> list[str]:
    assert isinstance(previous, list)
    assert isinstance(current, list)
    return [f"new item {item}" for item in current if item not in previous]


def test_shrink_only_baseline_write_requires_explicit_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the developer-only growth seam even when the surrounding test
    # runner (for example GitHub Actions) exports CI=true. The next test keeps
    # the fail-closed CI behavior covered independently.
    monkeypatch.delenv("CI", raising=False)
    path = tmp_path / "baseline.json"
    state = [["existing"]]
    baseline = Baseline(
        name="synthetic",
        path=path,
        derive=lambda: state[0],
        growth_check=_new_items,
    )
    baseline.write()

    state[0] = ["existing", "added"]
    with pytest.raises(RuntimeError, match="--allow-growth"):
        baseline.write()
    assert baseline.load() == ["existing"]

    baseline.write(allow_growth=True)
    assert baseline.load() == ["existing", "added"]


def test_baseline_write_still_refuses_all_regeneration_in_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = Baseline(name="synthetic", path=tmp_path / "baseline.json", derive=list)
    monkeypatch.setenv("CI", "1")

    with pytest.raises(RuntimeError, match="refusing to regenerate baselines in CI"):
        baseline.write(allow_growth=True)


def test_module_size_exemptions_require_authored_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(module_size.OVER_BUDGET_EXEMPTIONS, "exceptions.py", " ")

    with pytest.raises(RuntimeError, match="durable authored reason"):
        module_size.derive_module_size()
