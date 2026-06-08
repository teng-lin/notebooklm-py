"""Static AST checks enforcing the ADR-0008 ``cli/services`` layering boundary.

This file scans cleaned ``cli/services`` modules for forbidden imports —
top-level ``click`` and relative imports that *resolve* to the command-layer
presentation/runtime modules (``notebooklm.cli.rendering`` /
``error_handler`` / ``runtime``). The relative-import check is
resolution-based and depth-agnostic: each ``from ..X`` / ``from ...X`` /
``from ....X`` is resolved to its absolute target module and flagged iff that
target is one of the forbidden command modules, regardless of dot count
(#1400). It also inventories the Stage-3 transitional exceptions for workflow
services still being migrated out of rendering/exit ownership.

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

# Relative imports that *resolve* to these absolute command-layer modules are
# forbidden, regardless of nesting depth or dot count. The check is
# **resolution-based**: for every ``from ..X`` / ``from ...X`` / ``from ....X``
# relative import in a scanned module, the scanner computes the import's
# absolute target module from the importing module's package + the dot count
# (Python's own relative-import resolution) and flags it iff the resolved
# target is one of these presentation/runtime command modules.
#
# History: the scanner originally flagged only the level-2 (``..rendering``)
# form by dot-count. ``cli/services/login/*`` modules sit one directory deeper,
# so their ``...rendering`` reach-ins resolved to the real ``cli.rendering``
# while slipping past the gate (#1393). #1393 tightened the bound to dot-depths
# 2 AND 3 — but that bound was coupled to the current max directory depth: a
# future ``cli/services/login/sub/`` module could reach ``cli.rendering`` via a
# level-4 ``....rendering`` import and evade the gate (#1400). The check is now
# depth-agnostic and resolution-based, so the forbidden set is expressed as the
# absolute module names below and the dot-count no longer matters: ``....auth``
# (→ ``notebooklm.auth``) stays allowed from any depth, while ``....rendering``
# (→ ``notebooklm.cli.rendering``) is flagged from any depth.
FORBIDDEN_RELATIVE_PARENTS = {"rendering", "error_handler", "runtime"}

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"
SERVICES_ROOT = REPO_ROOT / "src" / "notebooklm" / "cli" / "services"
CLI_PACKAGE = "notebooklm.cli"

# Absolute command-layer modules a ``cli/services`` module must never reach via
# a relative import — derived from ``FORBIDDEN_RELATIVE_PARENTS`` so the two
# stay in lockstep. A resolved relative target is forbidden iff it equals one
# of these exactly (e.g. ``notebooklm.cli.rendering``) or is a submodule of one
# (e.g. ``notebooklm.cli.rendering.sub``).
FORBIDDEN_ABSOLUTE_TARGETS = frozenset(
    f"{CLI_PACKAGE}.{name}" for name in FORBIDDEN_RELATIVE_PARENTS
)

# Fully cleaned service modules. Each must have zero ``_boundary_violations``
# AND zero Pattern A pairs (see :func:`_pattern_a_pairs`).
#
# The login submodules (``browser_accounts.py``, ``cookie_writes.py``,
# ``chromium_accounts.py``, ``firefox_accounts.py``, ``refresh.py``) used to
# reach the command layer's ``console`` / ``exit_with_code`` / ``run_async``
# through narrow LEVEL-3 (``...rendering`` / ``...error_handler`` /
# ``...runtime``) seams that slipped past this scanner. #1393 tightened the
# import check to level-3 and inverted those reach-ins behind a caller-injected
# ``LoginIO`` sink (Protocol + resolver in ``cli/services/login/io_seam.py``;
# concrete sink registered by the command layer in
# ``cli/playwright_login_io.py``). The ``_emit`` / ``_emit_progress`` /
# ``_emit_warning`` helpers now forward to ``io.emit`` — no presentation import
# remains in any of these modules, so they are genuinely GUARDED.
# Login ``cookie_domains.py`` is pure service code: the command layer hosts
# Click ``BadParameter`` translation and optional-domain warning rendering.
GUARDED_PATHS = {
    "cli/services/auth_diagnostics.py": SERVICES_ROOT / "auth_diagnostics.py",
    "cli/services/auth_source.py": SERVICES_ROOT / "auth_source.py",
    "cli/services/confirming_mutation.py": SERVICES_ROOT / "confirming_mutation.py",
    "cli/services/download.py": SERVICES_ROOT / "download.py",
    "cli/services/generate.py": SERVICES_ROOT / "generate.py",
    "cli/services/label_listing.py": SERVICES_ROOT / "label_listing.py",
    "cli/services/listing.py": SERVICES_ROOT / "listing.py",
    "cli/services/login/browser_accounts.py": SERVICES_ROOT / "login" / "browser_accounts.py",
    "cli/services/login/chromium_accounts.py": SERVICES_ROOT / "login" / "chromium_accounts.py",
    "cli/services/login/cookie_domains.py": SERVICES_ROOT / "login" / "cookie_domains.py",
    "cli/services/login/cookie_jar.py": SERVICES_ROOT / "login" / "cookie_jar.py",
    "cli/services/login/cookie_writes.py": SERVICES_ROOT / "login" / "cookie_writes.py",
    "cli/services/login/exceptions.py": SERVICES_ROOT / "login" / "exceptions.py",
    "cli/services/login/firefox_accounts.py": SERVICES_ROOT / "login" / "firefox_accounts.py",
    "cli/services/login/io_seam.py": SERVICES_ROOT / "login" / "io_seam.py",
    "cli/services/login/outcomes.py": SERVICES_ROOT / "login" / "outcomes.py",
    "cli/services/login/profile_targets.py": SERVICES_ROOT / "login" / "profile_targets.py",
    "cli/services/login/refresh.py": SERVICES_ROOT / "login" / "refresh.py",
    "cli/services/login/rookiepy_errors.py": SERVICES_ROOT / "login" / "rookiepy_errors.py",
    "cli/services/playwright_login.py": SERVICES_ROOT / "playwright_login.py",
    "cli/services/playwright_redaction.py": SERVICES_ROOT / "playwright_redaction.py",
    "cli/services/polling.py": SERVICES_ROOT / "polling.py",
    "cli/services/research.py": SERVICES_ROOT / "research.py",
    "cli/services/session_context.py": SERVICES_ROOT / "session_context.py",
    "cli/services/source_listing.py": SERVICES_ROOT / "source_listing.py",
    "cli/services/source_mutations.py": SERVICES_ROOT / "source_mutations.py",
    "cli/services/source_research.py": SERVICES_ROOT / "source_research.py",
    "cli/services/source_serializers.py": SERVICES_ROOT / "source_serializers.py",
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
# Emptied by #1391: ``playwright_login.py`` was the sole remaining entry. The
# drain inverted its ``console.print`` / ``exit_with_code`` / ``run_async``
# reach-ins into a caller-injected ``LoginIO`` sink (the concrete sink +
# command wrappers live in ``cli/playwright_login_io.py``), so the service no
# longer imports ``..rendering`` / ``..error_handler`` / ``..runtime`` and
# carries zero Pattern A pairs — it is now a fully cleaned ``GUARDED_PATHS``
# module. Every service module under ``cli/services/`` is now either GUARDED or
# WAIVED; this dict stays declared (and asserted ``== {}`` below) so a future
# re-introduction of a transitional module is a deliberate, reviewed addition.
TRANSITIONAL_GUARDED_PATHS: dict[str, dict[str, object]] = {}

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
    ADR-0008 (they keep forward-reference type hints possible without
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


def _package_for_path(path: pathlib.Path) -> str | None:
    """Return the importing module's dotted package, or ``None`` if off-tree.

    For a real module under ``src/`` (e.g.
    ``src/notebooklm/cli/services/login/refresh.py``), the package is the
    dotted path of its *containing directory*
    (``notebooklm.cli.services.login``) — that is the anchor Python uses to
    resolve relative imports. Synthetic fixtures created in ``tmp_path`` live
    outside ``src/``; they return ``None`` and must pass an explicit
    ``package=`` to :func:`_boundary_violations` to simulate a tree location.
    """
    try:
        rel = path.resolve().relative_to(SRC_ROOT)
    except ValueError:
        return None
    parts = rel.with_suffix("").parts
    # Drop the module's own leaf name to get its containing package.
    return ".".join(parts[:-1])


def _resolve_relative(target: str, package: str) -> str:
    """Resolve a dotted relative import ``target`` against ``package``.

    Mirrors :func:`importlib._bootstrap._resolve_name`: ``level`` leading dots
    strip ``level - 1`` trailing components off ``package``, then the named
    remainder (if any) is appended. ``..rendering`` from
    ``notebooklm.cli.services`` → ``notebooklm.cli.rendering``; ``....rendering``
    from ``notebooklm.cli.services.login.sub`` → ``notebooklm.cli.rendering``.

    Raises :class:`ValueError` for an import that reaches past the top-level
    package (more dots than ``package`` has components) — exactly the case
    Python itself rejects with ``ImportError: attempted relative import beyond
    top-level package``. Such an import can never execute, so silently clamping
    it (``rsplit`` returning a too-short list) would mis-resolve it to a wrong
    absolute module and could mask a real reach-in; failing loudly is correct.
    """
    level = len(target) - len(target.lstrip("."))
    name = target.lstrip(".")
    if level > 1:
        bits = package.rsplit(".", level - 1)
        if len(bits) < level:
            raise ValueError(
                f"attempted relative import {target!r} beyond top-level package {package!r}"
            )
        base = bits[0]
    else:
        base = package
    return f"{base}.{name}" if name else base


def _is_forbidden_target(resolved: str) -> bool:
    """Flag iff ``resolved`` is a forbidden command module or a submodule of one."""
    return any(
        resolved == forbidden or resolved.startswith(f"{forbidden}.")
        for forbidden in FORBIDDEN_ABSOLUTE_TARGETS
    )


def _boundary_violations(path: pathlib.Path, *, package: str | None = None) -> list[str]:
    """Return human-readable violation strings (empty iff clean).

    Relative imports are resolved to their absolute target module (from the
    importing module's ``package`` + the dot count) and flagged iff the target
    is a forbidden command-layer module — independent of the dot count. The
    ``package`` is derived from ``path`` for real on-tree modules; off-tree
    fixtures must pass it explicitly to simulate their tree location.
    """
    if package is None:
        package = _package_for_path(path)
    violations: list[str] = []
    for target, line in _runtime_imports(path):
        # Top-level import like ``import click`` or ``from click import ...``.
        head = target.lstrip(".").split(".", 1)[0]
        if not target.startswith(".") and head in FORBIDDEN_TOP_LEVEL_MODULES:
            violations.append(f"{path.name}:{line}: forbidden top-level import: {target!r}")
            continue
        if not target.startswith("."):
            continue
        # Relative import: resolve to its absolute target module and flag iff
        # that target is a command-layer presentation/runtime module
        # (``notebooklm.cli.rendering`` / ``error_handler`` / ``runtime``),
        # regardless of the dot count. ``..rendering`` from
        # ``cli/services/*``, ``...rendering`` from ``cli/services/login/*``,
        # and a hypothetical ``....rendering`` from
        # ``cli/services/login/sub/*`` all resolve to the same forbidden
        # module; ``....auth`` (→ ``notebooklm.auth``) does not and stays
        # allowed at any depth.
        if package is None:
            raise AssertionError(
                f"Cannot resolve relative import {target!r} in {path}: the module "
                "is off-tree (outside src/) so its package is unknown. Pass an "
                "explicit package= to _boundary_violations() for synthetic fixtures."
            )
        resolved = _resolve_relative(target, package)
        if _is_forbidden_target(resolved):
            violations.append(
                f"{path.name}:{line}: forbidden relative import: {target!r} "
                f"(resolves to {resolved})"
            )
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
    assert not violations, f"{logical_name} violates ADR-0008 boundary:\n  " + "\n  ".join(
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
        f"{logical_name} ADR-0008 boundary inventory changed.\n"
        "If this removes a violation, update the expected list in the same PR.\n"
        "If this adds a violation, move rendering/exit policy back to the command layer.\n"
        "Current violations:\n  " + "\n  ".join(violations)
    )


def test_transitional_allowlist_is_empty():
    """The Stage-3 transitional allowlist is fully drained (#1391).

    ``playwright_login.py`` was the last entry; the drain inverted its
    presentation / exit / async reach-ins behind a caller-injected
    ``LoginIO`` sink (concrete sink + command wrappers in
    ``cli/playwright_login_io.py``), promoting it to ``GUARDED_PATHS``. This
    is the ADR-0008 end state: every ``cli/services/`` module is enforced at
    the level-2-import boundary, with no transitional carve-outs. Adding a new
    transitional entry is a deliberate regression this assertion blocks.
    """
    assert TRANSITIONAL_GUARDED_PATHS == {}, (
        "TRANSITIONAL_GUARDED_PATHS must stay empty after #1391 — a service "
        "module that owns presentation/exit policy must instead invert it "
        "behind a caller-injected sink (see cli/playwright_login_io.py) and "
        f"land in GUARDED_PATHS. Unexpected entries: {sorted(TRANSITIONAL_GUARDED_PATHS)}"
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

    Simulated from a ``cli/services/*`` module: ``from .. import rendering``
    resolves to ``notebooklm.cli.rendering``.
    """
    bad = tmp_path / "fake_service_alias_form.py"
    bad.write_text("from __future__ import annotations\nfrom .. import rendering\n")
    violations = _boundary_violations(bad, package="notebooklm.cli.services")
    assert any("rendering" in v for v in violations), violations


def test_guard_helper_detects_level_2_relative_import(tmp_path):
    """``from ..rendering import X`` (level-2) must trip the guard.

    The baseline case: a ``cli/services/*`` module reaching its presentation
    sibling. Simulated from package ``notebooklm.cli.services`` →
    ``notebooklm.cli.rendering``.
    """
    bad = tmp_path / "fake_level2_service.py"
    with bad.open("w", encoding="utf-8", newline="\n") as f:
        f.write("from __future__ import annotations\nfrom ..rendering import console\n")
    violations = _boundary_violations(bad, package="notebooklm.cli.services")
    assert any("rendering" in v for v in violations), violations


def test_guard_helper_detects_level_3_relative_import(tmp_path):
    """``from ...rendering import X`` (level-3) must trip the guard (#1393).

    Login submodules sit one directory deeper than ``cli/services``, so their
    presentation reach-ins use three leading dots and resolve to the real
    ``cli.rendering`` module. Simulated from package
    ``notebooklm.cli.services.login`` → ``notebooklm.cli.rendering``.
    """
    bad = tmp_path / "fake_level3_service.py"
    # Pin LF + UTF-8 so the fixture is byte-stable across the OS test matrix.
    with bad.open("w", encoding="utf-8", newline="\n") as f:
        f.write("from __future__ import annotations\nfrom ...rendering import console\n")
    violations = _boundary_violations(bad, package="notebooklm.cli.services.login")
    assert any("rendering" in v for v in violations), violations


def test_guard_helper_detects_level_4_relative_import_resolving_to_cli(tmp_path):
    """``from ....rendering import X`` (level-4) must trip the guard when it
    resolves to ``cli.rendering`` (#1400).

    This is the future blind spot the old depth-2/3 bound left open: a
    hypothetical ``cli/services/login/sub/*`` module (package
    ``notebooklm.cli.services.login.sub``) reaching the command layer via four
    leading dots resolves to ``notebooklm.cli.rendering`` — a presentation
    reach-in that the depth-coupled scanner would have missed. The
    resolution-based check flags it regardless of dot count.
    """
    bad = tmp_path / "fake_level4_cli_service.py"
    with bad.open("w", encoding="utf-8", newline="\n") as f:
        f.write("from __future__ import annotations\nfrom ....rendering import console\n")
    violations = _boundary_violations(bad, package="notebooklm.cli.services.login.sub")
    assert any("rendering" in v for v in violations), violations


def test_guard_helper_allows_level_4_relative_import_to_notebooklm(tmp_path):
    """``from ....auth import X`` (level-4) must NOT trip the guard.

    From a ``cli/services/login/*`` module (package
    ``notebooklm.cli.services.login``), four leading dots resolve to a package
    outside ``cli`` (``notebooklm.auth``), a legitimate dependency the login
    modules genuinely use. The resolution-based check must keep allowing it at
    any depth.
    """
    ok = tmp_path / "fake_level4_notebooklm_auth.py"
    # Pin LF + UTF-8 so the fixture is byte-stable across the OS test matrix.
    with ok.open("w", encoding="utf-8", newline="\n") as f:
        f.write("from __future__ import annotations\nfrom ....auth import AuthTokens\n")
    assert _boundary_violations(ok, package="notebooklm.cli.services.login") == []


def test_guard_helper_allows_level_4_notebooklm_namesake_module(tmp_path):
    """``from ....config import X`` (level-4) → ``notebooklm.config`` must pass.

    A ``notebooklm.*`` top-level module that happens to share a *leaf* name is
    not a ``cli.*`` reach-in. From ``notebooklm.cli.services.login``,
    ``....config`` resolves to ``notebooklm.config`` (not
    ``notebooklm.cli.config``), so the namesake must not be flagged. Guards
    against a regression that keys off the leaf name instead of the resolved
    absolute module.
    """
    ok = tmp_path / "fake_level4_notebooklm_config.py"
    with ok.open("w", encoding="utf-8", newline="\n") as f:
        f.write("from __future__ import annotations\nfrom ....config import DEFAULT_ENDPOINT\n")
    assert _boundary_violations(ok, package="notebooklm.cli.services.login") == []


def test_guard_helper_allows_level_1_intra_package_import(tmp_path):
    """``from .rendering import X`` (level-1) must NOT trip the guard.

    A single-dot import from a ``cli/services/*`` module targets a *sibling
    inside* ``cli.services`` (``notebooklm.cli.services.rendering``), not the
    command-layer ``notebooklm.cli.rendering``. Guards the ``level == 1``
    branch of ``_resolve_relative`` against a regression that would resolve
    intra-package imports up to the ``cli`` package.
    """
    ok = tmp_path / "fake_level1_service.py"
    with ok.open("w", encoding="utf-8", newline="\n") as f:
        f.write("from __future__ import annotations\nfrom .rendering import build_table\n")
    assert _boundary_violations(ok, package="notebooklm.cli.services") == []


def test_resolve_relative_rejects_import_beyond_top_level_package():
    """``_resolve_relative`` raises when an import reaches past the root package.

    Mirrors Python's own ``ImportError: attempted relative import beyond
    top-level package``. Such an import can never execute, so silently
    clamping it would mis-resolve to a wrong absolute module and could mask a
    real reach-in; the helper must fail loudly instead. (gemini-code-assist /
    claude review on #1401.)
    """
    # ``....rendering`` (level-4) from a 2-component package overruns the root.
    with pytest.raises(ValueError, match="beyond top-level package"):
        _resolve_relative("....rendering", "a.b")


def test_guard_helper_allows_type_checking_imports(tmp_path):
    """``TYPE_CHECKING`` guarded imports are NOT runtime deps and must pass."""
    ok = tmp_path / "service_with_type_checking.py"
    ok.write_text(
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from ..rendering import ListRender  # noqa\n"
    )
    assert _boundary_violations(ok, package="notebooklm.cli.services") == []


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
