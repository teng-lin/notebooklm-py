"""Focused tests for ADR-0022 baseline growth acknowledgement."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.audit_auth_patch_sites import (
    PatchSite,
    build_family_scorecard,
    build_projection,
    family_scorecard_growth,
    projection_growth,
)
from scripts.audit_auth_shared_mutations import projection_growth as shared_mutation_growth

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


def _site(
    module: str,
    attribute: str,
    path: str,
    *,
    package: str = "notebooklm._auth",
    owner: str = "test_x",
    kind: str = "test",
    idiom: str = "monkeypatch.setattr",
) -> PatchSite:
    return PatchSite(module, attribute, path, 1, idiom, package, owner, kind)


def test_patch_projection_rejects_cross_swap_and_lexical_owner_movement() -> None:
    previous = build_projection(
        [_site("refresh", "one", "tests/test_a.py"), _site("refresh", "two", "tests/test_b.py")]
    )
    cross_swap = build_projection(
        [_site("refresh", "two", "tests/test_a.py"), _site("refresh", "one", "tests/test_b.py")]
    )
    owner_move = build_projection([_site("refresh", "one", "tests/test_a.py", owner="helper")])
    assert any("joint_sites" in error for error in projection_growth(previous, cross_swap))
    assert any(
        "owners" in error or "joint_sites" in error
        for error in projection_growth(previous, owner_move)
    )


def test_patch_projection_rejects_target_path_count_and_assignment_growth() -> None:
    previous = build_projection([_site("refresh", "one", "tests/test_a.py")])
    current = build_projection(
        [
            _site("refresh", "one", "tests/test_a.py"),
            _site("refresh", "new", "tests/test_b.py", idiom="assignment"),
        ]
    )
    errors = projection_growth(previous, current)
    assert any("sites" in error and "new" in error for error in errors)
    assert any("files" in error and "test_b.py" in error for error in errors)
    assert any("direct assignments grew" in error for error in errors)


@pytest.mark.parametrize("destination", ["notebooklm._browser", "notebooklm.auth"])
def test_family_scorecard_rejects_auth_package_relocation(destination: str) -> None:
    auth = build_projection([_site("refresh", "seam", "tests/test_x.py")])
    empty = build_projection([])
    previous = build_family_scorecard([auth, empty])
    moved = build_projection(
        [
            _site(
                "headless_reauth" if destination.endswith("_browser") else "auth",
                "seam",
                "tests/test_x.py",
                package=destination,
            )
        ]
    )
    current = build_family_scorecard([build_projection([]), moved])
    assert family_scorecard_growth(previous, current)


def test_shared_object_displacement_growth_is_rejected() -> None:
    previous = {"version": 1, "summary": {"total": 0}, "mutations": []}
    current = {
        "version": 1,
        "summary": {"total": 1, "private": 1, "helper_or_fixture": 1, "assignments": 0},
        "mutations": [
            {
                "package": "notebooklm._auth",
                "owner": "notebooklm._auth.recovery.ColdRecoveryState",
                "attribute": "_state",
                "idiom": "monkeypatch.setattr",
                "path": "tests/conftest.py",
                "owner_qualname": "reset",
                "owner_kind": "fixture",
                "count": 1,
            }
        ],
    }
    assert shared_mutation_growth(previous, current)
