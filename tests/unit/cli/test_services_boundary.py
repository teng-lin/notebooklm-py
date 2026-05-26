"""Static AST checks enforcing the ADR-008 ``cli/services`` layering boundary.

This file scans ``cli/services/listing.py`` for forbidden imports — top-level
``click`` and relative imports from sibling presentation/runtime modules
(``..rendering``, ``..error_handler``, ``..runtime``). It also inventories the
Stage-3 transitional exceptions for the workflow services still being migrated
out of rendering/exit ownership.

Scope: only ``cli/services/listing.py`` is enforced here. The audit (I0) also
flagged ``cli/services/login.py``, but between the audit and this PR landing,
PR #954 split that file into the ``cli/services/login/`` package and the
``console.print`` / Click cleanup was explicitly deferred at that point (per
PR #954's source-plan overrides). Adding the ``login/`` package to this guard
is the job of a follow-up PR; doing it here would gate a passing PR-C on
unrelated ~1500-2000 lines of refactor work. See
``.sisyphus/drafts/pr-c-migration-map.md`` for the scope decision.

The guard is a single source of truth — to extend it to the ``login/``
package, add the module paths to ``GUARDED_PATHS`` once clean, or to
``TRANSITIONAL_GUARDED_PATHS`` with exact violations while a migration is in
flight. The two helper functions below stay agnostic of which file is being
scanned.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

import pytest

# ``click`` is the only top-level module disallowed. ``rich`` is allowed
# (services may still build Rich-compatible data, just not call print on a
# console); ``typing.TYPE_CHECKING`` blocks are not enforced — service modules
# may use them to forward-reference rendering types without taking a runtime
# dependency.
FORBIDDEN_TOP_LEVEL_MODULES = {"click"}

# Relative imports from these sibling packages of ``cli/services`` are
# forbidden. The check fires for any ``from ..<name>`` or ``from ..<name>.X``
# import — i.e. ``..rendering`` and ``..rendering.something`` both count.
FORBIDDEN_RELATIVE_PARENTS = {"rendering", "error_handler", "runtime"}

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
GUARDED_PATHS = {
    "cli/services/listing.py": REPO_ROOT / "src" / "notebooklm" / "cli" / "services" / "listing.py",
}

SERVICES_ROOT = REPO_ROOT / "src" / "notebooklm" / "cli" / "services"

# Stage 3 migration inventory. These modules currently own presentation and/or
# exit policy, which the architecture plan moves back to the command layer.
# Keep this list exact: adding a new violation should fail this test; removing
# one should update the expected list in the same PR.
TRANSITIONAL_GUARDED_PATHS = {
    "cli/services/artifact_generation.py": (
        SERVICES_ROOT / "artifact_generation.py",
        [
            "artifact_generation.py:9: forbidden relative import: '..error_handler'",
            "artifact_generation.py:10: forbidden relative import: '..rendering'",
        ],
    ),
    "cli/services/source_content.py": (
        SERVICES_ROOT / "source_content.py",
        [
            "source_content.py:18: forbidden relative import: '..error_handler'",
            "source_content.py:19: forbidden relative import: '..rendering'",
        ],
    ),
    "cli/services/source_mutations.py": (
        SERVICES_ROOT / "source_mutations.py",
        [
            "source_mutations.py:18: forbidden top-level import: 'click'",
            "source_mutations.py:21: forbidden relative import: '..error_handler'",
            "source_mutations.py:22: forbidden relative import: '..rendering'",
        ],
    ),
}


def _runtime_imports(path: pathlib.Path) -> Iterator[tuple[str, int]]:
    """Yield ``(import_target, line_number)`` for every runtime import in ``path``.

    Imports inside ``if TYPE_CHECKING:`` blocks are skipped — those have no
    runtime dependency on the cited module and are explicitly allowed by
    ADR-008 (they keep forward-reference type hints possible without
    importing the presentation layer at runtime).
    """
    tree = ast.parse(path.read_text())

    def _is_type_checking_guard(test: ast.expr) -> bool:
        # Recognize ``if TYPE_CHECKING:`` and ``if typing.TYPE_CHECKING:``.
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            return True
        return (
            isinstance(test, ast.Attribute)
            and isinstance(test.value, ast.Name)
            and test.value.id == "typing"
            and test.attr == "TYPE_CHECKING"
        )

    def _walk(node: ast.AST, *, inside_type_checking: bool) -> Iterator[tuple[str, int]]:
        if isinstance(node, ast.If) and _is_type_checking_guard(node.test):
            for child in node.body:
                yield from _walk(child, inside_type_checking=True)
            for child in node.orelse:
                yield from _walk(child, inside_type_checking=inside_type_checking)
            return
        if inside_type_checking:
            # Skip imports nested under a TYPE_CHECKING guard at any depth.
            for child in ast.iter_child_nodes(node):
                yield from _walk(child, inside_type_checking=True)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield (alias.name, node.lineno)
            return
        if isinstance(node, ast.ImportFrom):
            # ``from ..rendering import X`` → level=2, module="rendering".
            # ``from ..rendering.sub import X`` → level=2, module="rendering.sub".
            # ``from .. import rendering`` → level=2, module=None — the
            # forbidden sibling is named in ``node.names`` instead, so we
            # synthesize one target per alias to keep the boundary check
            # symmetric with the ``from ..rendering import X`` form.
            level = node.level or 0
            if node.module is None and level > 0:
                for alias in node.names:
                    yield (f"{'.' * level}{alias.name}", node.lineno)
            else:
                target = f"{'.' * level}{node.module or ''}"
                yield (target, node.lineno)
            return
        for child in ast.iter_child_nodes(node):
            yield from _walk(child, inside_type_checking=inside_type_checking)

    yield from _walk(tree, inside_type_checking=False)


def _boundary_violations(path: pathlib.Path) -> list[str]:
    """Return human-readable violation strings (empty iff clean)."""
    violations: list[str] = []
    for target, line in _runtime_imports(path):
        # Top-level import like ``import click`` or ``from click import ...``.
        head = target.lstrip(".").split(".", 1)[0]
        if not target.startswith(".") and head in FORBIDDEN_TOP_LEVEL_MODULES:
            violations.append(f"{path.name}:{line}: forbidden top-level import: {target!r}")
            continue
        # Relative import ``from ..rendering ...`` etc. (exactly two leading dots
        # because that targets a sibling of ``cli/services``).
        if target.startswith("..") and not target.startswith("..."):
            remainder = target.lstrip(".")
            parent = remainder.split(".", 1)[0]
            if parent in FORBIDDEN_RELATIVE_PARENTS:
                violations.append(f"{path.name}:{line}: forbidden relative import: {target!r}")
    return violations


@pytest.mark.parametrize(
    "logical_name,path",
    sorted(GUARDED_PATHS.items()),
)
def test_services_boundary_no_forbidden_imports(logical_name, path):
    """Each guarded service module must be free of presentation/runtime imports."""
    assert path.exists(), f"Expected guarded service module at {path}"
    violations = _boundary_violations(path)
    assert not violations, f"{logical_name} violates ADR-008 boundary:\n  " + "\n  ".join(
        violations
    )


@pytest.mark.parametrize(
    "logical_name,path,expected_violations",
    sorted((name, *entry) for name, entry in TRANSITIONAL_GUARDED_PATHS.items()),
)
def test_transitional_services_boundary_violations_are_documented(
    logical_name, path, expected_violations
):
    """Stage-3 service migrations must not grow new presentation/runtime reach-ins."""
    assert path.exists(), f"Expected guarded service module at {path}"
    violations = _boundary_violations(path)
    assert violations == expected_violations, (
        f"{logical_name} ADR-008 boundary inventory changed.\n"
        "If this removes a violation, update the expected list in the same PR.\n"
        "If this adds a violation, move rendering/exit policy back to the command layer.\n"
        "Current violations:\n  " + "\n  ".join(violations)
    )


def test_guard_helper_detects_a_known_violation(tmp_path):
    """Sanity check: the helper actually flags a synthetic forbidden import.

    Without this, a logic bug in ``_boundary_violations`` would silently turn
    every guarded module into a passing test forever.
    """
    bad = tmp_path / "fake_service.py"
    bad.write_text("from __future__ import annotations\nimport click\n")
    violations = _boundary_violations(bad)
    assert any("click" in v for v in violations), violations


def test_guard_helper_detects_from_parent_import_sibling(tmp_path):
    """``from .. import rendering`` must trip the guard.

    Without the ``node.module is None`` branch in ``_runtime_imports``, the
    alias-only form silently passes — even though it carries the same runtime
    dependency on ``cli.rendering`` as ``from ..rendering import X``. CodeRabbit
    flagged this in PR #961 review.
    """
    bad = tmp_path / "fake_service_alias_form.py"
    bad.write_text("from __future__ import annotations\nfrom .. import rendering\n")
    violations = _boundary_violations(bad)
    assert any("rendering" in v for v in violations), violations


def test_guard_helper_allows_type_checking_imports(tmp_path):
    """``TYPE_CHECKING`` guarded imports are NOT runtime deps and must pass."""
    ok = tmp_path / "service_with_type_checking.py"
    ok.write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from ..rendering import ListRender  # noqa\n"
    )
    assert _boundary_violations(ok) == []
