"""Static AST checks enforcing the ADR-008 ``cli/services`` layering boundary.

This file scans cleaned ``cli/services`` modules for forbidden imports —
top-level ``click`` and relative imports from sibling presentation/runtime
modules (``..rendering``, ``..error_handler``, ``..runtime``). It also
inventories the Stage-3 transitional exceptions for workflow services still
being migrated out of rendering/exit ownership.

Scope: every file under ``cli/services/`` (recursively, excluding
``__init__.py`` and ``__pycache__``) must be classified into exactly one of
three sets:

* ``GUARDED_PATHS`` — fully cleaned modules. ``_boundary_violations`` must
  return an empty list AND the module must have no Pattern A
  ``console.print`` + ``exit_with_code`` co-occurrence.
* ``TRANSITIONAL_GUARDED_PATHS`` — modules still owning some presentation or
  exit policy that the architecture plan moves back to the command layer.
  Each entry declares its exact current violations (``forbidden_imports``
  list + ``pattern_a_violations`` list of ``(function_name, line)`` tuples).
  Removing a violation in a refactor PR must update the declaration in the
  same PR; adding one is rejected outright by the tests below.
* ``WAIVED_PATHS`` — modules with a documented, indefinite exception (e.g.
  Click parser-time callbacks where ``raise click.BadParameter`` is the
  contract Click itself defines). Empty by default; entries require an
  explicit rationale.

The ``test_inventory_completeness`` test enforces the partition: every
service module must appear in exactly one set. New modules added under
``cli/services/`` will fail the test until classified.

Pattern A definition: ``console.print`` and ``exit_with_code`` co-occur as
Pattern A iff both are called from within the SAME
``ast.FunctionDef | ast.AsyncFunctionDef`` body (at any nesting depth inside
that function, but NOT crossing into a nested ``FunctionDef`` /
``AsyncFunctionDef``). The implementation in :func:`_pattern_a_pairs`
reports one pair per ``exit_with_code`` call site so that line drift after
a refactor elsewhere is caught — silent shifts (e.g. an unrelated edit
moving the line) would otherwise mask a real regression.
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
#
# Note: level-3 imports (``...rendering``, from a deeper subpackage like
# ``cli/services/login/``) are NOT flagged here. Login submodules currently
# rely on that path; tracking their reach-in is done via the
# ``pattern_a_violations`` inventory inside ``TRANSITIONAL_GUARDED_PATHS``
# (e.g. ``cli/services/login/browser_accounts.py``). Broadening the import
# scanner to level-3 would surface those automatically; that's a deliberate
# follow-up, not a gap to plug here.
FORBIDDEN_RELATIVE_PARENTS = {"rendering", "error_handler", "runtime"}

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SERVICES_ROOT = REPO_ROOT / "src" / "notebooklm" / "cli" / "services"

# Fully cleaned service modules. Each must have zero ``_boundary_violations``
# AND zero Pattern A pairs (see :func:`_pattern_a_pairs`).
#
# Login ``browser_accounts.py`` and ``cookie_writes.py`` still have narrow
# lazy level-3 presentation/runtime seams (progress/warning output and
# ``run_async``). The current boundary scanner intentionally does not flag
# those imports; their important invariant is zero Pattern A pairs until the
# remaining seams are inverted into caller-provided callbacks.
# Login ``cookie_domains.py`` is pure service code: the command layer hosts
# Click ``BadParameter`` translation and optional-domain warning rendering.
GUARDED_PATHS = {
    "cli/services/auth_diagnostics.py": SERVICES_ROOT / "auth_diagnostics.py",
    "cli/services/artifact_generation.py": SERVICES_ROOT / "artifact_generation.py",
    "cli/services/confirming_mutation.py": SERVICES_ROOT / "confirming_mutation.py",
    "cli/services/download.py": SERVICES_ROOT / "download.py",
    "cli/services/generate.py": SERVICES_ROOT / "generate.py",
    "cli/services/listing.py": SERVICES_ROOT / "listing.py",
    "cli/services/login/browser_accounts.py": SERVICES_ROOT / "login" / "browser_accounts.py",
    "cli/services/login/cookie_domains.py": SERVICES_ROOT / "login" / "cookie_domains.py",
    "cli/services/login/cookie_jar.py": SERVICES_ROOT / "login" / "cookie_jar.py",
    "cli/services/login/cookie_writes.py": SERVICES_ROOT / "login" / "cookie_writes.py",
    "cli/services/login/exceptions.py": SERVICES_ROOT / "login" / "exceptions.py",
    "cli/services/login/outcomes.py": SERVICES_ROOT / "login" / "outcomes.py",
    "cli/services/login/profile_targets.py": SERVICES_ROOT / "login" / "profile_targets.py",
    "cli/services/login/rookiepy_errors.py": SERVICES_ROOT / "login" / "rookiepy_errors.py",
    "cli/services/polling.py": SERVICES_ROOT / "polling.py",
    "cli/services/research.py": SERVICES_ROOT / "research.py",
    "cli/services/session_context.py": SERVICES_ROOT / "session_context.py",
    "cli/services/skill_install.py": SERVICES_ROOT / "skill_install.py",
    "cli/services/source_clean.py": SERVICES_ROOT / "source_clean.py",
    "cli/services/source_add.py": SERVICES_ROOT / "source_add.py",
    "cli/services/source_content.py": SERVICES_ROOT / "source_content.py",
    "cli/services/source_listing.py": SERVICES_ROOT / "source_listing.py",
    "cli/services/source_mutations.py": SERVICES_ROOT / "source_mutations.py",
    "cli/services/source_research.py": SERVICES_ROOT / "source_research.py",
    "cli/services/source_serializers.py": SERVICES_ROOT / "source_serializers.py",
    "cli/services/source_wait.py": SERVICES_ROOT / "source_wait.py",
}

# Stage 3 migration inventory. These modules currently own presentation
# and/or exit policy, which the architecture plan moves back to the command
# layer. Each entry is a dict with the exact violation inventory:
#
#   ``path``                  — ``pathlib.Path`` to the module.
#   ``forbidden_imports``     — exact list of strings ``_boundary_violations``
#                               returns for this module. Adding a new
#                               violation should fail this test; removing one
#                               should update the expected list in the same
#                               PR.
#   ``pattern_a_violations``  — exact list of ``(function_name, lineno)`` for
#                               every ``exit_with_code`` call site that
#                               co-occurs with a ``console.print`` in the
#                               same function body. Empty list means the
#                               module has no Pattern A pairs (but may still
#                               own exit policy via helpers — see
#                               ``rationale``).
#   ``pattern_b_violations``  — optional rationale string for click-runtime
#                               usage (``click.confirm``, ``raise
#                               click.ClickException``, parser-time
#                               ``click.BadParameter``) that's NOT a Pattern
#                               A pair but still reaches into Click.
#   ``rationale``             — short note on what migration is in flight or
#                               why the module is here.
TRANSITIONAL_GUARDED_PATHS: dict[str, dict[str, object]] = {
    "cli/services/auth_source.py": {
        "path": SERVICES_ROOT / "auth_source.py",
        "forbidden_imports": [
            "auth_source.py:198: forbidden top-level import: 'click'",
        ],
        "pattern_a_violations": [],
        "pattern_b_violations": (
            "uses click.get_current_context() to read the auth-source "
            "override out of the active Click context; intrinsically "
            "Click-bound until the context-reader is moved to the command "
            "layer."
        ),
        "rationale": (
            "Adapter that reads the auth-source override from the live Click "
            "context; the function-level ``import click`` is the only Click "
            "reach-in."
        ),
    },
    "cli/services/login/chromium_accounts.py": {
        "path": SERVICES_ROOT / "login" / "chromium_accounts.py",
        "forbidden_imports": [],
        "pattern_a_violations": [
            ("_read_chromium_profile_cookies_from_selector", 62),
            ("_read_chromium_profile_cookies_from_selector", 81),
            ("_read_chromium_profile_cookies_from_selector", 86),
            ("_enumerate_chromium_profiles_fanout", 140),
            ("_enumerate_chromium_profiles_fanout", 168),
            ("_enumerate_chromium_profiles_fanout", 223),
        ],
        "rationale": (
            "Chromium-profile enumeration owns presentation + exit codes "
            "for profile-selection failures; level-3 rendering reach-in not "
            "flagged by ``_boundary_violations``. The ``rookiepy_error`` "
            "branch now wraps the typed-message helper "
            "(:func:`_handle_rookiepy_error`) in a ``console.print(...)`` "
            "so the helper itself stays in :data:`GUARDED_PATHS`; the "
            "exit_with_code line moved by 1 in each helper. "
            "``_enumerate_chromium_profiles_fanout`` now dispatches on a "
            "typed outcome returned by :func:`_enumerate_one_jar` instead "
            "of catching ``SystemExit``."
        ),
    },
    "cli/services/login/firefox_accounts.py": {
        "path": SERVICES_ROOT / "login" / "firefox_accounts.py",
        "forbidden_imports": [],
        "pattern_a_violations": [
            ("_read_firefox_container_cookies", 70),
            ("_read_firefox_container_cookies", 76),
            ("_read_firefox_container_cookies", 94),
            ("_read_firefox_container_cookies", 97),
            ("_read_firefox_container_cookies", 100),
        ],
        "rationale": (
            "Firefox container-cookie reader owns presentation + exit "
            "codes for container/decryption failures; level-3 rendering "
            "reach-in not flagged by ``_boundary_violations``."
        ),
    },
    "cli/services/login/refresh.py": {
        "path": SERVICES_ROOT / "login" / "refresh.py",
        "forbidden_imports": [],
        "pattern_a_violations": [
            ("_exit_on_outcome", 74),
            ("_confirm_profile_account_overwrite", 203),
            ("_refresh_from_browser_cookies", 301),
            ("_login_with_browser_cookies", 364),
            ("_login_with_browser_cookies", 379),
        ],
        "rationale": (
            "Browser-cookie refresh flow owns interactive confirmation, "
            "presentation, and exit codes; multiple Pattern A pairs. "
            "``_exit_on_outcome`` is the shared adapter that converts the "
            "typed outcome returned by the (now-GUARDED) helper-chain "
            "modules into the legacy ``console.print + exit_with_code`` "
            "shape this driver still owns."
        ),
    },
    "cli/services/playwright_login.py": {
        "path": SERVICES_ROOT / "playwright_login.py",
        "forbidden_imports": [
            "playwright_login.py:51: forbidden relative import: '..error_handler'",
            "playwright_login.py:52: forbidden relative import: '..rendering'",
            "playwright_login.py:53: forbidden relative import: '..runtime'",
        ],
        "pattern_a_violations": [
            ("ensure_chromium_installed", 573),
            ("validate_login_flag_conflicts", 679),
            ("validate_login_flag_conflicts", 684),
            ("validate_login_flag_conflicts", 690),
            ("validate_login_flag_conflicts", 693),
            ("prepare_login_paths", 730),
            ("run_playwright_login", 803),
            ("run_playwright_login", 904),
            ("run_playwright_login", 911),
            ("run_playwright_login", 933),
            ("run_playwright_login", 940),
            ("run_playwright_login", 965),
            ("run_playwright_login", 983),
            ("run_playwright_login", 1026),
        ],
        "rationale": (
            "Playwright login orchestrator owns the full presentation + "
            "exit-code matrix for browser-automation failures; one of the "
            "largest migration targets in this inventory. Line numbers "
            "shifted in commit e3871dc3 (subprocess-stderr redaction, "
            "PR #1111) but the inventory was not updated in that PR; "
            "refreshed here as a drive-by mechanical fix."
        ),
    },
}

# Modules with a documented, indefinite exception. Empty by default; adding
# to this dict requires a documented architecture exception.
#
# Entry schema (when populated):
#   ``path``      — ``pathlib.Path`` to the module.
#   ``rationale`` — short note citing the architecture decision that grants
#                   the waiver. WAIVED entries are NOT scanned for boundary
#                   violations or Pattern A pairs, so the rationale must be
#                   load-bearing.
WAIVED_PATHS: dict[str, dict[str, object]] = {}


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


def _is_console_print_call(node: ast.AST) -> bool:
    """``console.print(...)`` — module-level ``console`` symbol from rendering."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "print"
        and isinstance(func.value, ast.Name)
        and func.value.id == "console"
    )


def _is_exit_with_code_call(node: ast.AST) -> bool:
    """``exit_with_code(...)`` — sibling-import from ``..error_handler``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Name) and func.id == "exit_with_code"


def _function_calls(
    funcnode: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[list[int], list[int]]:
    """Walk a function body for ``console.print`` and ``exit_with_code`` call lines.

    Recurses into nested ``If`` / ``Try`` / ``For`` / ``With`` blocks so the
    check is order-insensitive within a single function. Stops at nested
    ``FunctionDef`` / ``AsyncFunctionDef`` so the enclosing pair count is
    not contaminated by inner-helper calls — those are accounted for
    separately when the walker reaches the nested def.
    """
    prints: list[int] = []
    exits: list[int] = []

    def _walk(node: ast.AST) -> None:
        # Don't descend into nested function definitions — they get their
        # own pair-count via the outer ``_pattern_a_pairs`` driver.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not funcnode:
            return
        if _is_console_print_call(node):
            prints.append(node.lineno)
        if _is_exit_with_code_call(node):
            exits.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            _walk(child)

    for child in ast.iter_child_nodes(funcnode):
        _walk(child)
    return prints, exits


def _pattern_a_pairs(path: pathlib.Path) -> list[tuple[str, int]]:
    """Return ``(function_name, exit_with_code_line)`` for every Pattern A pair.

    Pattern A: a function body (``FunctionDef`` or ``AsyncFunctionDef``)
    contains BOTH at least one ``console.print`` call AND at least one
    ``exit_with_code`` call (at any nesting depth within that function, but
    not crossing into a nested ``FunctionDef`` / ``AsyncFunctionDef``).
    Each such ``exit_with_code`` line is reported once so that drift in
    either direction (added or removed lines) trips the transitional
    inventory check.
    """
    pairs: list[tuple[str, int]] = []

    def _visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prints, exits = _function_calls(node)
            if prints and exits:
                pairs.extend((node.name, line) for line in exits)
            # Recurse into the body so nested function defs are also
            # inspected; ``_function_calls`` already stopped at the nested
            # boundary so we won't double-count.
            for child in ast.iter_child_nodes(node):
                _visit(child)
            return
        for child in ast.iter_child_nodes(node):
            _visit(child)

    _visit(ast.parse(path.read_text()))
    return pairs


def _iter_service_modules() -> Iterator[pathlib.Path]:
    """Yield every service module path, excluding ``__init__.py`` files."""
    for path in SERVICES_ROOT.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        # ``rglob`` already skips ``__pycache__`` (compiled artefacts live as
        # ``*.pyc``), but be explicit so a stray ``.py`` under
        # ``__pycache__`` is also ignored.
        if "__pycache__" in path.parts:
            continue
        yield path


def _logical_name(path: pathlib.Path) -> str:
    """Convert an absolute services path to the ``cli/services/...`` key form."""
    rel = path.relative_to(REPO_ROOT / "src" / "notebooklm")
    return rel.as_posix()


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
    "logical_name,entry",
    sorted(TRANSITIONAL_GUARDED_PATHS.items()),
)
def test_transitional_services_boundary_violations_are_documented(logical_name, entry):
    """Stage-3 service migrations must not grow new presentation/runtime reach-ins.

    Checks the ``forbidden_imports`` inventory for each transitional module
    against the live scan. Adding a new import-level violation should fail
    this test; removing one should update the expected list in the same PR.
    The Pattern A inventory has its own assertion below in
    :func:`test_no_console_print_with_exit_with_code`.
    """
    path = entry["path"]
    expected_violations = entry["forbidden_imports"]
    assert isinstance(path, pathlib.Path)
    assert isinstance(expected_violations, list)
    assert path.exists(), f"Expected guarded service module at {path}"
    violations = _boundary_violations(path)
    assert violations == expected_violations, (
        f"{logical_name} ADR-008 boundary inventory changed.\n"
        "If this removes a violation, update the expected list in the same PR.\n"
        "If this adds a violation, move rendering/exit policy back to the command layer.\n"
        "Current violations:\n  " + "\n  ".join(violations)
    )


def test_inventory_completeness():
    """Every service module must appear in exactly one of the three sets.

    Catches new modules added under ``cli/services/`` that haven't been
    classified yet, and catches double-listings that would otherwise let
    a module silently pass a check it shouldn't.
    """
    seen: dict[str, list[str]] = {}
    for name in GUARDED_PATHS:
        seen.setdefault(name, []).append("GUARDED_PATHS")
    for name in TRANSITIONAL_GUARDED_PATHS:
        seen.setdefault(name, []).append("TRANSITIONAL_GUARDED_PATHS")
    for name in WAIVED_PATHS:
        seen.setdefault(name, []).append("WAIVED_PATHS")

    duplicates = {n: locs for n, locs in seen.items() if len(locs) > 1}
    assert not duplicates, (
        f"Service modules must appear in exactly one classification set; duplicates: {duplicates}"
    )

    classified = set(seen)
    actual = {_logical_name(p) for p in _iter_service_modules()}

    missing = actual - classified
    extra = classified - actual

    assert not missing, (
        "New service modules must be classified into GUARDED_PATHS, "
        "TRANSITIONAL_GUARDED_PATHS, or WAIVED_PATHS before this test will "
        f"pass:\n  {sorted(missing)}"
    )
    assert not extra, (
        "Classification sets reference modules that no longer exist on "
        f"disk; remove them from the inventory:\n  {sorted(extra)}"
    )


def test_no_console_print_with_exit_with_code():
    """Pattern A (``console.print`` + ``exit_with_code`` in one function) is gated.

    Fails when:

    * A module NOT in ``TRANSITIONAL_GUARDED_PATHS`` has any Pattern A pair
      (a service module must not own both presentation and exit policy
      together inside a single function body).
    * A transitional module's actual Pattern A pairs do not exactly match
      its declared ``pattern_a_violations`` list. This catches both new
      regressions and silent line-shifts from refactors elsewhere — if
      the declared lines are no longer the live lines, the diff is
      visible in the failure message and the inventory must be updated in
      the same PR.
    """
    failures: list[str] = []
    for path in sorted(_iter_service_modules()):
        name = _logical_name(path)
        actual = _pattern_a_pairs(path)
        if name in TRANSITIONAL_GUARDED_PATHS:
            entry = TRANSITIONAL_GUARDED_PATHS[name]
            expected = entry["pattern_a_violations"]
            assert isinstance(expected, list)
            # Compare as sorted tuples so insertion-order changes in the
            # inventory don't trip the check.
            expected_sorted = sorted(expected)
            actual_sorted = sorted(actual)
            if expected_sorted != actual_sorted:
                failures.append(
                    f"{name}: declared pattern_a_violations do not match "
                    "live AST scan.\n"
                    f"  declared: {expected_sorted}\n"
                    f"  actual:   {actual_sorted}"
                )
            continue
        if name in WAIVED_PATHS or name in GUARDED_PATHS:
            if actual:
                failures.append(
                    f"{name}: module is in GUARDED_PATHS/WAIVED_PATHS but "
                    f"has Pattern A pairs: {sorted(actual)}"
                )
            continue
        # Should be unreachable thanks to test_inventory_completeness, but
        # surface a clear message rather than a parametrize failure if it
        # ever does happen. The primary requirement is classification, not
        # the presence/absence of pairs — surface the pair count as
        # secondary context only.
        failures.append(
            f"{name}: unclassified module — classify into GUARDED_PATHS, "
            "TRANSITIONAL_GUARDED_PATHS, or WAIVED_PATHS "
            f"(Pattern A pairs found: {sorted(actual)})."
        )

    assert not failures, "Pattern A inventory drift:\n  " + "\n  ".join(failures)


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


def test_pattern_a_helper_detects_co_occurrence(tmp_path):
    """Sanity check: synthetic same-function ``console.print`` + ``exit_with_code``."""
    src = (
        "from __future__ import annotations\n"
        "\n"
        "def fail():\n"
        "    console.print('boom')\n"
        "    exit_with_code(1)\n"
    )
    bad = tmp_path / "fake_pattern_a.py"
    bad.write_text(src)
    pairs = _pattern_a_pairs(bad)
    assert pairs == [("fail", 5)], pairs


def test_pattern_a_helper_ignores_split_helpers(tmp_path):
    """``console.print`` in helper, ``exit_with_code`` in caller → NOT Pattern A.

    The narrow Pattern A definition is intentional: it only catches direct
    co-occurrence inside a single ``FunctionDef`` body. Helpers that emit
    presentation and a separate caller that handles exit codes are a
    different (preferable) shape and must NOT trip the check.
    """
    src = (
        "from __future__ import annotations\n"
        "\n"
        "def _emit(msg):\n"
        "    console.print(msg)\n"
        "\n"
        "def driver():\n"
        "    _emit('boom')\n"
        "    exit_with_code(1)\n"
    )
    ok = tmp_path / "fake_split.py"
    ok.write_text(src)
    assert _pattern_a_pairs(ok) == []


def test_pattern_a_helper_ignores_nested_def_co_occurrence(tmp_path):
    """A nested ``def`` containing both calls is reported under the NESTED name.

    Confirms ``_function_calls`` stops at the nested boundary so the outer
    function's count is not contaminated by inner-helper calls.
    """
    src = (
        "from __future__ import annotations\n"
        "\n"
        "def outer():\n"
        "    def inner():\n"
        "        console.print('x')\n"
        "        exit_with_code(1)\n"
        "    inner()\n"
    )
    bad = tmp_path / "fake_nested.py"
    bad.write_text(src)
    assert _pattern_a_pairs(bad) == [("inner", 6)]
