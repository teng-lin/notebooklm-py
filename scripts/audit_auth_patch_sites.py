#!/usr/bin/env python3
"""Count the test-suite patch sites that reach into ``notebooklm._auth``.

This script is the **definition** of the patch-site metric used by the ``_auth``
deepening effort (ADR-0033 plan §7/§9). Ad-hoc greps for the same thing ranged
from 73 to 219 depending on which idiom the author happened to match, so the
reduction target is meaningless unless one instrument owns the count.

What counts as a site
---------------------
A supported direct, item, namespace, monkeypatch, or ``patch.object`` /
``patch.multiple`` / ``patch.dict`` mutation whose target resolves to a
``notebooklm._auth.<module>`` module object. Literal finite-loop names and bulk
mapping keys expand into one row per attribute; unresolved names fail closed::

    from notebooklm._auth import refresh as _auth_refresh
    monkeypatch.setattr(_auth_refresh, "_poke_session", fake)   # <- one site

**Plain assignment counts too**::

    _auth_storage._FLOCK_UNAVAILABLE_WARNED = False             # <- one site

It reaches into a module's private state exactly like ``setattr`` does, and it is
strictly worse because pytest never restores it. Counting only the call idioms
would let a later PR "improve" this metric by rewriting monkeypatch calls as
assignments while making the coupling worse. New attributes count too; otherwise
``module.NEW_SEAM = value`` would be a laundering path. Sequential lexical alias
resolution distinguishes a real module binding from a shadowing local such as
``mock.return_value`` without relying on an existing-name allowlist.

Each site is classified ``private`` when the attribute name starts with an
underscore and ``public`` otherwise, because §9's acceptance criterion is about
private-attribute coupling specifically.

Baseline (2026-09-02, ``6aabddad7``, auth patch-coupling PR 1)
----------------------------------------------------------------
The schema-v2 private-package family contains 310 module-rebinding sites: 244 in
``_auth`` and 66 in ``_browser``.  The separately frozen public
``notebooklm.auth`` facade contains 526 operation-boundary substitutions.  The
schema-v2 baselines freeze the complete package/target/path/lexical-owner row;
the family scorecard makes package relocation visible.  Re-measure rather than
quoting an older number from memory.

Re-run this script to compare; do not trust a number quoted elsewhere.

Known limits — read before trusting a delta
-------------------------------------------
* **Privacy-class gaming.** §9 measures *private* sites, so renaming a private
  attribute to a public one moves a site between columns without reducing
  coupling. Check the ``total`` column and the per-attribute list, not just
  ``private``, when reading a delta.
This is a static count, so two things can move it without the coupling changing:
* **Helper indirection.** Collapsing N patches into one shared fixture reduces the
  count to 1 while the coupling is unchanged. A falling count next to a new
  conftest helper deserves a look at the helper, not applause.
* **Lexical alias scopes** are resolved independently for BOTH idioms: a genuine
  function-local auth import is usable in that function, while an assignment,
  walrus, ``for``/``with``/``except`` target, parameter, or unrelated nested
  import shadows an outer module alias and nested scopes inherit that. Before
  this, ``storage = object()`` followed by
  ``storage.SEAM = 1`` counted as a patch of the real module whenever ``SEAM``
  happened to be a genuine module-level name — the ``mock.return_value`` shape,
  and 44 of the original 262 sites.

Supported grammar — this is a repository ratchet, not a Python interpreter
-------------------------------------------------------------------------
The collector intentionally recognizes the finite forms used by this repository:
explicit family imports and lexical aliases; direct attribute/item/namespace
mutation through the idioms above; literal names, mappings, and finite literal
loops/containers without unpacking; and direct local helper calls whose explicit
arguments resolve to a finite target set. Direct finite arguments to the
syntactic ``list(...)`` form used by the suite are inspected only far enough to
prevent a resolved family target being hidden inside one. Other constructor and
unpacking forms are outside this grammar.

This script does not promise arbitrary Python control-flow or pytest fixture
semantics. Within the supported grammar, a resolved family target is counted, a
proven-fresh/non-family target is excluded, and an unresolved family-related
target raises ``AuditError``. Unsupported syntax is not evidence of a coupling
reduction: make the target statically resolvable or keep the existing projected
row. Dynamic attributes or keys against an already-resolved family module also
fail closed. Unknown ownership is conservative only when reached from a
statically resolved family value within this grammar; the audit is not a proof
over every value Python could produce at runtime.

Deliberate exclusions
---------------------
* **String targets** (``monkeypatch.setattr("notebooklm._auth.refresh.x", …)``)
  are NOT counted. They are a separate, separately-banned idiom — see
  ``tests/_guardrails/test_no_forbidden_monkeypatches.py`` — and counting them
  here would double-book the same debt against two gates.
* Patches of non-module objects (classes, instances, fixtures) are not module
  seams and are out of scope.
* String-form ``patch`` (non-``.object``) is likewise out of scope; the metric
  tracks statically resolved module mutation. Object-form deletion and item/
  namespace mutation are included so syntax changes cannot hide the debt.
* Stdlib/third-party modules reached THROUGH an ``_auth`` namespace --
  ``monkeypatch.setattr(_auth_refresh.os, "name", "nt")``,
  ``browser_capture.time``, ``_auth_refresh.httpx`` -- rebind that other
  module, not an ``_auth`` seam attribute, so they are not sites.

Resolution handles the aliased-import idiom the suite actually uses --
``from notebooklm._auth import refresh as _auth_refresh``,
``import notebooklm._auth.refresh as _auth_refresh``, plain
``import notebooklm._auth.refresh`` (dotted attribute access), and
``from notebooklm import _auth`` followed by ``_auth.refresh``.

It also resolves INDIRECT sites, where a test reaches one ``_auth`` module
through another's alias for it: ``psidts_recovery.py`` binds
``from . import storage as _auth_storage``, so
``monkeypatch.setattr(psidts_recovery._auth_storage, "save_cookies_to_storage", …)``
is a patch of ``_auth.storage`` and is billed to ``storage``. Getting this
wrong is not cosmetic -- it moved 12 sites between modules during this
script's own verification.

Pure stdlib + ``ast``: the package under test is never imported, so the count is
stable regardless of the environment the audit runs in. Output is sorted, so a
diff between two runs is a real change.

Usage::

    python scripts/audit_auth_patch_sites.py
    python scripts/audit_auth_patch_sites.py --json
    python scripts/audit_auth_patch_sites.py --tests-dir tests --module refresh
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, cast

AUTH_PACKAGE = ("notebooklm", "_auth")
AUTH_DOTTED = ".".join(AUTH_PACKAGE)
PATCH_FUNCS = {"setattr", "delattr"}  # matched as <something>.setattr(...)
REPO_ROOT = Path(__file__).resolve().parent.parent
_AMBIGUOUS_FAMILY_ALIAS = "<ambiguous-family-alias>"


@dataclass(frozen=True, order=True)
class PatchSite:
    """One resolved module-object patch of a ``notebooklm._auth`` attribute."""

    module: str  # the _auth submodule, e.g. "refresh"
    attribute: str  # the attribute being rebound, e.g. "_poke_session"
    path: str  # repo-relative test file
    lineno: int
    idiom: str  # "monkeypatch.setattr" | "patch.object" | "assignment"
    package: str = AUTH_DOTTED
    owner_qualname: str = "<module>"
    owner_kind: str = "helper"

    @property
    def is_private(self) -> bool:
        return self.attribute.startswith("_")


class AuditError(RuntimeError):
    """A family mutation was found but could not be expanded safely."""


def _dotted_name(node: ast.AST) -> str | None:
    """Render ``a.b.c`` attribute/name chains as a dotted string, else ``None``."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _auth_import_bindings(
    node: ast.Import | ast.ImportFrom,
    package_dotted: str = AUTH_DOTTED,
) -> dict[str, str]:
    """Inspected-package module bindings created by one import statement."""
    result: dict[str, str] = {}
    package_parent, package_name = package_dotted.rsplit(".", 1)
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module == package_dotted:
            for alias in node.names:
                result[alias.asname or alias.name] = f"{package_dotted}.{alias.name}"
        elif module == package_parent:
            for alias in node.names:
                if alias.name == package_name:
                    result[alias.asname or alias.name] = package_dotted
    else:
        for alias in node.names:
            if alias.name != package_dotted and not alias.name.startswith(f"{package_dotted}."):
                continue
            if alias.asname:
                result[alias.asname] = alias.name
            else:
                result[alias.name] = alias.name
    return result


def _target_names(node: ast.AST) -> set[str]:
    """Names bound by an assignment/pattern target (attributes bind no name)."""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    if isinstance(node, ast.Tuple | ast.List):
        return {name for item in node.elts for name in _target_names(item)}
    return set()


def _pattern_names(pattern: ast.pattern) -> set[str]:
    """Capture names introduced by one structural pattern."""
    if isinstance(pattern, ast.MatchAs):
        names = {pattern.name} if pattern.name else set()
        return names | (_pattern_names(pattern.pattern) if pattern.pattern else set())
    if isinstance(pattern, ast.MatchStar):
        return {pattern.name} if pattern.name else set()
    if isinstance(pattern, ast.MatchMapping):
        names = {pattern.rest} if pattern.rest else set()
        return names | {name for item in pattern.patterns for name in _pattern_names(item)}
    if isinstance(pattern, ast.MatchClass):
        return {
            name
            for item in (*pattern.patterns, *pattern.kwd_patterns)
            for name in _pattern_names(item)
        }
    if isinstance(pattern, ast.MatchSequence | ast.MatchOr):
        return {name for item in pattern.patterns for name in _pattern_names(item)}
    return set()


def _scope_declarations(
    scope: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
) -> tuple[set[str], set[str], set[str]]:
    """Return lexical locals/global/nonlocal declarations for a function scope.

    Comprehensions and nested definitions are child scopes.  In particular, a
    comprehension target must not shadow an alias in the containing function.
    """
    locals_: set[str] = set()
    globals_: set[str] = set()
    nonlocals: set[str] = set()
    args = scope.args
    locals_.update(arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs))
    locals_.update(arg.arg for arg in (args.vararg, args.kwarg) if arg is not None)

    class Collector(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Store | ast.Del):
                locals_.add(node.id)

        def visit_Import(self, node: ast.Import) -> None:
            locals_.update((alias.asname or alias.name).split(".")[0] for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            locals_.update(alias.asname or alias.name for alias in node.names)

        def visit_Global(self, node: ast.Global) -> None:
            globals_.update(node.names)

        def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
            nonlocals.update(node.names)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.name:
                locals_.add(node.name)
            for child in node.body:
                self.visit(child)

        def visit_match_case(self, node: ast.match_case) -> None:
            locals_.update(_pattern_names(node.pattern))
            if node.guard:
                self.visit(node.guard)
            for child in node.body:
                self.visit(child)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            locals_.add(node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            locals_.add(node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            locals_.add(node.name)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            pass

        def visit_ListComp(self, node: ast.ListComp) -> None:
            pass

        def visit_SetComp(self, node: ast.SetComp) -> None:
            pass

        def visit_DictComp(self, node: ast.DictComp) -> None:
            pass

        def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
            pass

    collector = Collector()
    if isinstance(scope, ast.Lambda):
        collector.visit(scope.body)
    else:
        for statement in scope.body:
            collector.visit(statement)
    locals_.difference_update(globals_ | nonlocals)
    return locals_, globals_, nonlocals


@dataclass
class _AliasScope:
    """One sequential Python namespace used by the patch-site resolver."""

    kind: str
    parent: _AliasScope | None
    lexical: set[str]
    globals: set[str]
    nonlocals: set[str]
    bindings: dict[str, str | None]


class _AliasResolver:
    """Build per-node alias states with Python's sequential scope rules."""

    def __init__(self, tree: ast.Module, package_dotted: str = AUTH_DOTTED) -> None:
        self.context: dict[int, dict[str, str]] = {}
        self.package_dotted = package_dotted
        self.scope = _AliasScope("module", None, set(), set(), set(), {})
        self._visit(tree)

    def _record(self, node: ast.AST) -> None:
        self.context[id(node)] = self._effective()

    def _free_parent(self, scope: _AliasScope) -> _AliasScope | None:
        parent = scope.parent
        if scope.kind in {"function", "lambda", "comprehension"}:
            while parent is not None and parent.kind == "class":
                parent = parent.parent
        return parent

    def _effective_from(self, scope: _AliasScope | None) -> dict[str, str]:
        if scope is None:
            return {}
        active = self._effective_from(self._free_parent(scope))
        # Auth aliases are deliberately sparse while a function's lexical-name
        # set can contain hundreds of ordinary locals. Filter the small active
        # alias map instead of rebuilding the large shadow-name union for every
        # AST node; under coverage on Python 3.10 the latter made this audit hit
        # pytest's 60-second timeout.
        for name in tuple(active):
            if name in scope.lexical or name in scope.bindings:
                active.pop(name)
        for name, target in scope.bindings.items():
            if target is not None:
                active[name] = target
        return active

    def _effective(self) -> dict[str, str]:
        return self._effective_from(self.scope)

    def _bind(self, name: str, target: str | None) -> None:
        # global/nonlocal declarations affect resolution inside this scope.  The
        # overlay is intentionally local to the definition traversal: merely
        # defining an uncalled helper must not mutate later module audit state.
        # A non-alias binding only matters when it shadows an inherited alias or
        # replaces a prior branch binding. Omitting every other ``None`` keeps
        # snapshots proportional to auth aliases rather than all Python locals.
        if target is None and name not in self.scope.bindings:
            if name in self.scope.lexical:
                return
            parent = self._free_parent(self.scope)
            if parent is None or name not in self._effective_from(parent):
                return
        self.scope.bindings[name] = target

    def _binding_target(self, node: ast.AST) -> str | None:
        """Resolve a simple lexical alias of the inspected package/module."""
        dotted = _dotted_name(node)
        if dotted is None:
            return None
        head, separator, tail = dotted.partition(".")
        scope: _AliasScope | None = self.scope
        resolved_head: str | None = None
        found = False
        while scope is not None:
            if head in scope.bindings:
                resolved_head = scope.bindings[head]
                found = True
                break
            if head in scope.lexical:
                found = True
                break
            scope = self._free_parent(scope)
        if found and resolved_head is None:
            return None
        if resolved_head is None:
            resolved_head = head
        if resolved_head == _AMBIGUOUS_FAMILY_ALIAS:
            return _AMBIGUOUS_FAMILY_ALIAS
        resolved = resolved_head + (separator + tail if separator else "")
        if resolved == self.package_dotted or resolved.startswith(f"{self.package_dotted}."):
            return resolved
        return None

    def _snapshot(self) -> dict[str, str | None]:
        return dict(self.scope.bindings)

    def _restore(self, state: dict[str, str | None]) -> None:
        self.scope.bindings = dict(state)

    @staticmethod
    def _merge(states: list[dict[str, str | None]]) -> dict[str, str | None]:
        """Conservatively retain a facade alias possible on any branch."""
        missing = object()
        merged: dict[str, str | None] = {}
        for name in set().union(*(state.keys() for state in states)):
            values = [state.get(name, missing) for state in states]
            aliases = sorted({value for value in values if isinstance(value, str)})
            if len(aliases) > 1:
                merged[name] = _AMBIGUOUS_FAMILY_ALIAS
            elif aliases:
                merged[name] = aliases[0]
            elif missing not in values:
                merged[name] = None
        return merged

    def _visit_seq(self, nodes: list[ast.stmt]) -> None:
        for node in nodes:
            self._visit(node)

    def _visit_function_header(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
    ) -> None:
        if not isinstance(node, ast.Lambda):
            for decorator in node.decorator_list:
                self._visit(decorator)
            for type_param in getattr(node, "type_params", []):
                self._visit(type_param)
        for default in (*node.args.defaults, *[item for item in node.args.kw_defaults if item]):
            self._visit(default)
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if arg.annotation:
                self._visit(arg.annotation)
        if node.args.vararg and node.args.vararg.annotation:
            self._visit(node.args.vararg.annotation)
        if node.args.kwarg and node.args.kwarg.annotation:
            self._visit(node.args.kwarg.annotation)
        if not isinstance(node, ast.Lambda) and node.returns:
            self._visit(node.returns)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        self._visit_function_header(node)
        if not isinstance(node, ast.Lambda):
            self._bind(node.name, None)
        locals_, globals_, nonlocals = _scope_declarations(node)
        parent = self.scope
        self.scope = _AliasScope(
            "lambda" if isinstance(node, ast.Lambda) else "function",
            parent,
            locals_,
            globals_,
            nonlocals,
            {},
        )
        if isinstance(node, ast.Lambda):
            self._visit(node.body)
        else:
            self._visit_seq(node.body)
        self.scope = parent

    def _visit_comprehension(self, node: ast.AST) -> None:
        generators = node.generators  # type: ignore[attr-defined]
        # The first iterable is evaluated in the enclosing scope.
        self._visit(generators[0].iter)
        parent = self.scope
        lexical = set().union(*(_target_names(generator.target) for generator in generators))
        self.scope = _AliasScope("comprehension", parent, lexical, set(), set(), {})
        for index, generator in enumerate(generators):
            if index:
                self._visit(generator.iter)
            self._visit(generator.target)
            for name in _target_names(generator.target):
                self._bind(name, None)
            for condition in generator.ifs:
                self._visit(condition)
        if isinstance(node, ast.DictComp):
            self._visit(node.key)
            self._visit(node.value)
        else:
            self._visit(node.elt)  # type: ignore[attr-defined]
        self.scope = parent

    def _visit_pattern_values(self, pattern: ast.pattern) -> None:
        if isinstance(pattern, ast.MatchValue):
            self._visit(pattern.value)
        elif isinstance(pattern, ast.MatchClass):
            self._visit(pattern.cls)
            for item in (*pattern.patterns, *pattern.kwd_patterns):
                self._visit_pattern_values(item)
        elif isinstance(pattern, ast.MatchMapping):
            for key in pattern.keys:
                self._visit(key)
            for item in pattern.patterns:
                self._visit_pattern_values(item)
        elif isinstance(pattern, ast.MatchSequence | ast.MatchOr):
            for item in pattern.patterns:
                self._visit_pattern_values(item)
        elif isinstance(pattern, ast.MatchAs) and pattern.pattern:
            self._visit_pattern_values(pattern.pattern)

    def _visit(self, node: ast.AST) -> None:  # noqa: C901, PLR0912, PLR0915
        self._record(node)
        if isinstance(node, ast.Module):
            self._visit_seq(node.body)
            return
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            self._visit_function(node)
            return
        if isinstance(node, ast.ClassDef):
            for decorator in node.decorator_list:
                self._visit(decorator)
            for base in node.bases:
                self._visit(base)
            for keyword in node.keywords:
                self._visit(keyword.value)
            for type_param in getattr(node, "type_params", []):
                self._visit(type_param)
            self._bind(node.name, None)
            parent = self.scope
            self.scope = _AliasScope("class", parent, set(), set(), set(), {})
            self._visit_seq(node.body)
            self.scope = parent
            return
        if isinstance(node, ast.Import | ast.ImportFrom):
            bindings = _auth_import_bindings(node, self.package_dotted)
            for alias in node.names:
                name = alias.asname or (
                    alias.name if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0]
                )
                self._bind(name, bindings.get(name))
            return
        if isinstance(node, ast.Assign):
            self._visit(node.value)
            for target in node.targets:
                self._visit(target)
                for name in _target_names(target):
                    binding = (
                        self._binding_target(node.value) if isinstance(target, ast.Name) else None
                    )
                    self._bind(name, binding)
            return
        if isinstance(node, ast.AnnAssign):
            self._visit(node.annotation)
            if node.value:
                self._visit(node.value)
                self._visit(node.target)
                for name in _target_names(node.target):
                    self._bind(name, None)
            return
        if isinstance(node, ast.AugAssign):
            self._visit(node.target)
            self._visit(node.value)
            for name in _target_names(node.target):
                self._bind(name, None)
            return
        if isinstance(node, ast.NamedExpr):
            self._visit(node.value)
            self._visit(node.target)
            for name in _target_names(node.target):
                binding = (
                    self._binding_target(node.value) if isinstance(node.target, ast.Name) else None
                )
                self._bind(name, binding)
            return
        if isinstance(node, ast.If):
            self._visit(node.test)
            entry = self._snapshot()
            self._visit_seq(node.body)
            body = self._snapshot()
            self._restore(entry)
            self._visit_seq(node.orelse)
            other = self._snapshot()
            self._restore(self._merge([body, other]))
            return
        if isinstance(node, ast.For | ast.AsyncFor):
            self._visit(node.iter)
            entry = self._snapshot()
            self._visit(node.target)
            for name in _target_names(node.target):
                self._bind(name, None)
            self._visit_seq(node.body)
            iteration = self._snapshot()
            self._restore(entry)
            self._visit_seq(node.orelse)
            normal = self._snapshot()
            self._restore(self._merge([entry, iteration, normal]))
            return
        if isinstance(node, ast.While):
            self._visit(node.test)
            entry = self._snapshot()
            self._visit_seq(node.body)
            iteration = self._snapshot()
            self._restore(entry)
            self._visit_seq(node.orelse)
            normal = self._snapshot()
            self._restore(self._merge([entry, iteration, normal]))
            return
        if isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                self._visit(item.context_expr)
                if item.optional_vars:
                    self._visit(item.optional_vars)
                    for name in _target_names(item.optional_vars):
                        self._bind(name, None)
            self._visit_seq(node.body)
            return
        if isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            try_node = cast(ast.Try, node)
            entry = self._snapshot()
            self._visit_seq(try_node.body)
            exits = [self._snapshot()]
            for handler in try_node.handlers:
                self._restore(entry)
                if handler.type:
                    self._visit(handler.type)
                if handler.name:
                    self._bind(handler.name, None)
                self._visit_seq(handler.body)
                if handler.name:
                    self.scope.bindings.pop(handler.name, None)
                exits.append(self._snapshot())
            self._restore(self._merge(exits))
            self._visit_seq(try_node.orelse)
            self._visit_seq(try_node.finalbody)
            return
        if isinstance(node, ast.Match):
            self._visit(node.subject)
            entry = self._snapshot()
            exits = [entry]
            for case in node.cases:
                self._restore(entry)
                self._record(case)
                self._visit_pattern_values(case.pattern)
                for name in _pattern_names(case.pattern):
                    self._bind(name, None)
                if case.guard:
                    self._visit(case.guard)
                self._visit_seq(case.body)
                exits.append(self._snapshot())
            self._restore(self._merge(exits))
            return
        if isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
            self._visit_comprehension(node)
            return
        for child in ast.iter_child_nodes(node):
            self._visit(child)


def _alias_context(
    tree: ast.Module,
    package_dotted: str = AUTH_DOTTED,
) -> dict[int, dict[str, str]]:
    """Effective auth aliases at every node under sequential lexical rules."""
    return _AliasResolver(tree, package_dotted).context


def _source_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    return sorted(source.glob("*.py")) if source.is_dir() else []


def load_source_aliases(auth_dir: Path) -> dict[str, dict[str, str]]:
    """Per ``_auth`` module, its module-level aliases for OTHER ``_auth`` modules.

    ``psidts_recovery.py`` does ``from . import storage as _auth_storage``, so a
    test writing ``monkeypatch.setattr(psidts_recovery._auth_storage, …)`` is
    really patching ``_auth.storage``. Without this map such a site is either
    dropped (undercount) or billed to ``psidts_recovery`` (mis-attribution).
    """
    source_aliases: dict[str, dict[str, str]] = {}
    if not auth_dir.exists():
        return source_aliases
    files = _source_files(auth_dir)
    known = {path.stem for path in files}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        local: dict[str, str] = {}
        for node in tree.body:  # module level only
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            if node.module is None:
                # from . import storage as _auth_storage
                for alias in node.names:
                    if alias.name in known:
                        local[alias.asname or alias.name] = alias.name
        source_aliases[path.stem] = local
    return source_aliases


def load_module_level_names(auth_dir: Path) -> dict[str, set[str]]:
    """Per ``_auth`` module, the names actually bound at module level.

    Used to keep the ``assignment`` idiom honest. A test file's alias map is
    file-global while a Python binding is function-scoped, so a local variable
    can shadow a module alias imported inside a different test — e.g.
    ``test_auth_cold_start_recovery.py`` imports ``headless_reauth as headless``
    inside three tests, and elsewhere binds ``headless`` to an ``AsyncMock``.
    Requiring the assigned attribute to be a real module-level name of the
    resolved module rejects ``mock.return_value = …`` without special-casing
    mock internals, and costs nothing for genuine rebinding.
    """
    names: dict[str, set[str]] = {}
    if not auth_dir.exists():
        return names
    for path in _source_files(auth_dir):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        bound: set[str] = set()
        for node in tree.body:  # module level only
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bound.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                bound.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound.add(alias.asname or alias.name.split(".")[0])
        names[path.stem] = bound
    return names


def _resolve_target(
    node: ast.AST,
    aliases: dict[str, str],
    source_aliases: dict[str, dict[str, str]] | None = None,
    package_dotted: str = AUTH_DOTTED,
    module_names: set[str] | None = None,
) -> str | None:
    """Resolve a patch target expression to the ``_auth`` submodule it patches.

    A bare module reference resolves to that module. A single trailing
    attribute is resolved through the *source* module's own aliases: if it
    names another ``_auth`` module the site is attributed THERE
    (``psidts_recovery._auth_storage`` -> ``storage``); if it names a stdlib or
    third-party module reached through the namespace
    (``_auth_refresh.os``, ``browser_capture.time``) it is not an ``_auth``
    seam coupling and resolves to ``None``.
    """
    source_aliases = source_aliases or {}
    dotted = _dotted_name(node)
    if dotted is None:
        return None

    module_names = module_names or set()
    single_module = package_dotted.rsplit(".", 1)[-1]
    if dotted == package_dotted and single_module in module_names:
        return single_module

    # Fully-qualified: notebooklm._auth.<module>[.<attr>]
    if dotted.startswith(f"{package_dotted}."):
        tail = dotted[len(package_dotted) + 1 :].split(".")
        if len(tail) == 1 and tail[0] in module_names:
            return tail[0]
        if len(tail) == 2:
            return source_aliases.get(tail[0], {}).get(tail[1])
        return None

    head, _, rest = dotted.partition(".")
    target = aliases.get(head)
    if target is None:
        return None
    if target == _AMBIGUOUS_FAMILY_ALIAS:
        raise AuditError("mutation target is an ambiguous family alias after control-flow merge")

    if target == package_dotted:
        if not rest and single_module in module_names:
            return single_module
        # `_auth.refresh` off the package binding.
        parts = rest.split(".") if rest else []
        if len(parts) == 1 and parts[0] in module_names:
            return parts[0]
        if len(parts) == 2:
            return source_aliases.get(parts[0], {}).get(parts[1])
        return None

    if target.startswith(f"{package_dotted}."):
        target_parts = target[len(package_dotted) + 1 :].split(".")
        if len(target_parts) != 1 or target_parts[0] not in module_names:
            return None
        module = target_parts[0]
        if not rest:
            return module
        parts = rest.split(".")
        if len(parts) == 1:
            return source_aliases.get(module, {}).get(parts[0])
        return None
    return None


def _literal_string(node: ast.AST | None, constants: dict[str, str] | None = None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return (constants or {}).get(node.id)
    return None


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    literal_bindings: dict[str, str] = {}
    binding_counts: Counter[str] = Counter()

    class BindingCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.names: set[str] = set()

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Store | ast.Del):
                self.names.add(node.id)

        def visit_Import(self, node: ast.Import) -> None:
            self.names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            self.names.update(alias.asname or alias.name for alias in node.names)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.names.add(node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.names.add(node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.names.add(node.name)

    for statement in tree.body:
        collector = BindingCollector()
        collector.visit(statement)
        binding_counts.update(collector.names)
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            literal_bindings[statement.targets[0].id] = statement.value.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            literal_bindings[statement.target.id] = statement.value.value
    # A ``global`` write in a nested function rebinds the module name at
    # runtime. Such a name is not a stable literal constant, even though its
    # original literal assignment is the only binding in ``tree.body``.
    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
    ):
        global_names = _scope_declarations(function)[1]
        scope_nodes = (
            list(ast.walk(function.body))
            if isinstance(function, ast.Lambda)
            else _function_scope_nodes(function)
        )
        rebound_globals = {
            node.id
            for node in scope_nodes
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and node.id in global_names
        }
        binding_counts.update(rebound_globals)
    return {name: value for name, value in literal_bindings.items() if binding_counts[name] == 1}


def _literal_constants_context(
    tree: ast.Module, constants: dict[str, str]
) -> dict[int, dict[str, str]]:
    """Return module constants visible at each node under Python lexical scope."""

    result: dict[int, dict[str, str]] = {}
    constant_names = set(constants)

    def visit(node: ast.AST, blocked: set[str]) -> None:
        result[id(node)] = {name: value for name, value in constants.items() if name not in blocked}
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            for decorator in getattr(node, "decorator_list", []):
                visit(decorator, blocked)
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    visit(default, blocked)
            locals_, globals_, nonlocals = _scope_declarations(node)
            function_blocked = (blocked - globals_) | (
                ((locals_ | nonlocals) & constant_names) - globals_
            )
            body: list[ast.AST] = (
                list(node.body) if not isinstance(node, ast.Lambda) else [node.body]
            )
            for statement in body:
                visit(statement, function_blocked)
            return
        if isinstance(node, ast.ClassDef):
            for decorator in node.decorator_list:
                visit(decorator, blocked)
            for base in node.bases:
                visit(base, blocked)
            for keyword in node.keywords:
                visit(keyword.value, blocked)
            class_bound = {
                name
                for statement in node.body
                if isinstance(statement, ast.Assign | ast.AnnAssign | ast.AugAssign)
                for target in (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                for name in _target_names(target)
            }
            for statement in node.body:
                visit(
                    statement,
                    blocked
                    if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)
                    else blocked | (class_bound & constant_names),
                )
            return
        for child in ast.iter_child_nodes(node):
            visit(child, blocked)

    visit(tree, set())
    return result


def _literal_values_context(
    tree: ast.Module,
    constant_context: dict[int, dict[str, str]] | None = None,
) -> dict[int, dict[str, tuple[str, ...]]]:
    """Finite literal bindings for simple loops, keyed by nodes in the loop body."""
    result: dict[int, dict[str, tuple[str, ...]]] = defaultdict(dict)
    for loop in (node for node in ast.walk(tree) if isinstance(node, ast.For)):
        if not isinstance(loop.target, ast.Name) or not isinstance(
            loop.iter, ast.Tuple | ast.List | ast.Set
        ):
            continue
        values = tuple(
            value
            for item in loop.iter.elts
            if (value := _literal_string(item, (constant_context or {}).get(id(item), {})))
            is not None
        )
        if len(values) != len(loop.iter.elts) or not values:
            continue
        for statement in loop.body:
            for child in ast.walk(statement):
                result[id(child)][loop.target.id] = values
    return result


def _literal_strings(
    node: ast.AST | None,
    constants: dict[str, str] | None = None,
    expanded: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    value = _literal_string(node, constants)
    if value is not None:
        return [value]
    if isinstance(node, ast.Name):
        return list((expanded or {}).get(node.id, ()))
    return []


def _owner_metadata(tree: ast.Module, test_path: Path | None = None) -> dict[int, tuple[str, str]]:
    """Map nodes to their stable lexical owner and owner kind."""

    result: dict[int, tuple[str, str]] = {}

    def decorated_as_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if _dotted_name(target) in {
                "pytest.fixture",
                "pytest_asyncio.fixture",
                "fixture",
                "yield_fixture",
            }:
                return True
        return False

    collectable_file = test_path is not None and test_path.name.startswith("test_")

    def walk(
        node: ast.AST,
        stack: tuple[str, ...],
        kind: str,
        collectable_class: bool = False,
    ) -> None:
        result[id(node)] = (".".join(stack) if stack else "<module>", kind)
        if isinstance(node, ast.ClassDef):
            child_stack = (*stack, node.name)
            child_collectable = collectable_file and node.name.startswith("Test")
            for child in ast.iter_child_nodes(node):
                walk(child, child_stack, kind, child_collectable)
            return
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            child_stack = (*stack, node.name)
            if decorated_as_fixture(node):
                child_kind = "fixture"
            elif (
                collectable_file
                and node.name.startswith("test_")
                and (not stack or collectable_class)
            ):
                child_kind = "test"
            else:
                child_kind = "helper"
            for child in ast.iter_child_nodes(node):
                walk(child, child_stack, child_kind, collectable_class)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, stack, kind, collectable_class)

    walk(tree, (), "helper")
    return result


def _function_scope_nodes(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    result: list[ast.AST] = []

    def walk(current: ast.AST) -> None:
        result.append(current)
        for child in ast.iter_child_nodes(current):
            if isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
            ):
                continue
            walk(child)

    for statement in node.body:
        walk(statement)
    return result


def _function_definitions(
    tree: ast.Module,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    result: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            qualname = ".".join((*self.stack, node.name))
            result[qualname] = node
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _function
        visit_AsyncFunctionDef = _function

    Visitor().visit(tree)
    return result


def _function_parameters(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    return [
        argument.arg
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    ]


def _definitely_nonfamily_typed_parameters(
    tree: ast.Module,
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    family_prefixes: tuple[str, ...],
) -> set[tuple[str, str]]:
    """Return parameters whose imported concrete type is outside the family."""

    imports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name

    result: set[tuple[str, str]] = set()
    for owner, function in functions.items():
        for argument in (
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        ):
            annotation = _dotted_name(argument.annotation) if argument.annotation else None
            if annotation is None:
                continue
            head, separator, tail = annotation.partition(".")
            resolved = imports.get(head)
            if resolved is None:
                continue
            dotted = resolved + (separator + tail if separator else "")
            if not any(
                dotted == prefix or dotted.startswith(f"{prefix}.") for prefix in family_prefixes
            ):
                result.add((owner, argument.arg))
    return result


def _call_argument(
    call: ast.Call,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parameter: str,
) -> ast.AST | None:
    positional = [argument.arg for argument in (*function.args.posonlyargs, *function.args.args)]
    if parameter in positional:
        index = positional.index(parameter)
        if index < len(call.args):
            return call.args[index]
    return next((keyword.value for keyword in call.keywords if keyword.arg == parameter), None)


def _root_name(node: ast.AST | None) -> str | None:
    current = node
    while isinstance(current, ast.Attribute | ast.Subscript):
        current = current.value
    if (
        isinstance(current, ast.Call)
        and isinstance(current.func, ast.Name)
        and current.func.id == "vars"
        and current.args
    ):
        return _root_name(current.args[0])
    return current.id if isinstance(current, ast.Name) else None


def _mutation_parameter_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    include_method_receivers: bool,
) -> set[str]:
    def assignment_targets(targets: list[ast.AST]) -> list[ast.AST]:
        result: list[ast.AST] = []
        for target in targets:
            is_namespace_subscript = isinstance(target, ast.Subscript) and (
                include_method_receivers
                or isinstance(target.value, ast.Attribute)
                and target.value.attr == "__dict__"
                or isinstance(target.value, ast.Call)
                and isinstance(target.value.func, ast.Name)
                and target.value.func.id == "vars"
            )
            if isinstance(target, ast.Attribute) or is_namespace_subscript:
                result.append(target)
        return result

    parameters = set(_function_parameters(function))
    used: set[str] = set()
    for node in _function_scope_nodes(function):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func) or ""
            tail = name.rsplit(".", 1)[-1]
            if (
                _patch_idiom(node) is not None
                or name in {"setattr", "delattr"}
                or tail in {"multiple", "dict", "setitem", "delitem"}
            ):
                target = _call_arg(node, 0, "target", "obj", "object", "mapping", "in_dict")
                if target is not None:
                    targets.append(target)
            elif isinstance(node.func, ast.Attribute) and (
                include_method_receivers
                or (
                    isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "__dict__"
                )
                or (
                    isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Name)
                    and node.func.value.func.id == "vars"
                )
            ):
                targets.append(node.func.value)
        elif isinstance(node, ast.Assign):
            targets.extend(assignment_targets(list(node.targets)))
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets.extend(assignment_targets([node.target]))
        elif isinstance(node, ast.Delete):
            targets.extend(assignment_targets(list(node.targets)))
        for target in targets:
            root = _root_name(target)
            if root is not None and root in parameters:
                used.add(root)
    return used


def _forwarded_parameter_context(
    tree: ast.Module,
    owners: dict[int, tuple[str, str]],
    resolve_argument: Callable[[ast.AST, ast.Call], str | None],
    *,
    context: str,
    include_method_receivers: bool = False,
    excluded_parameters: set[tuple[str, str]] | None = None,
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Resolve finite helper target parameters from direct call sites."""

    functions = _function_definitions(tree)
    parameters = {name: set(_function_parameters(node)) for name, node in functions.items()}
    owner_kinds = {
        name: owners.get(id(next(iter(node.body), node)), (name, "helper"))[1]
        for name, node in functions.items()
    }
    direct_used = {
        (name, parameter)
        for name, node in functions.items()
        if owner_kinds[name] in {"helper", "fixture"}
        for parameter in _mutation_parameter_names(
            node, include_method_receivers=include_method_receivers
        )
        if parameter not in {"self", "cls"}
        and (name, parameter) not in (excluded_parameters or set())
    }
    if not direct_used:
        return {}

    def finite_container_cells(node: ast.AST) -> tuple[ast.AST, ...] | None:
        if isinstance(node, ast.Tuple | ast.List | ast.Set):
            return tuple(node.elts)
        if isinstance(node, ast.Dict):
            return (*filter(None, node.keys), *node.values)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "list"
        ):
            return (*node.args, *(keyword.value for keyword in node.keywords))
        return None

    def finite_container_values(node: ast.AST, call: ast.Call) -> tuple[set[str], bool]:
        resolved = resolve_argument(node, call)
        if resolved is not None:
            return {resolved}, False
        cells = finite_container_cells(node)
        if cells is None:
            return set(), not isinstance(node, ast.Constant)
        found: set[str] = set()
        opaque = False
        for cell in cells:
            child_values, child_opaque = finite_container_values(cell, call)
            found.update(child_values)
            opaque |= child_opaque
        return found, opaque

    by_leaf: dict[str, set[str]] = defaultdict(set)
    for name in functions:
        by_leaf[name.rsplit(".", 1)[-1]].add(name)
    bindings: list[tuple[tuple[str, str], ast.AST | None, ast.Call, str]] = []
    ambiguous: set[str] = set()
    parent_by_id = {
        id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        candidates: set[str] = set()
        if isinstance(call.func, ast.Name):
            candidates = by_leaf.get(call.func.id, set())
        elif (
            isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in {"self", "cls"}
        ):
            caller = owners.get(id(call), ("<module>", "helper"))[0]
            class_name = caller.rpartition(".")[0]
            candidate = f"{class_name}.{call.func.attr}" if class_name else ""
            candidates = {candidate} if candidate in functions else set()
        if len(candidates) != 1:
            ambiguous.update(candidates)
            continue
        callee = next(iter(candidates))
        caller = owners.get(id(call), ("<module>", "helper"))[0]
        for parameter in parameters[callee]:
            bindings.append(
                (
                    (callee, parameter),
                    _call_argument(call, functions[callee], parameter),
                    call,
                    caller,
                )
            )
    for name_node in (node for node in ast.walk(tree) if isinstance(node, ast.Name)):
        candidates = by_leaf.get(name_node.id, set())
        parent = parent_by_id.get(id(name_node))
        direct = isinstance(parent, ast.Call) and parent.func is name_node
        if candidates and not direct:
            ambiguous.update(candidates)

    # A relay parameter becomes relevant only when it flows into an already
    # mutation-relevant helper parameter. This avoids treating every ordinary
    # test parameter or ``self`` attribute assignment as a family mutation.
    used = set(direct_used)
    changed = True
    while changed:
        changed = False
        for destination, argument, _call, caller in bindings:
            if destination not in used or not isinstance(argument, ast.Name):
                continue
            source = (caller, argument.id)
            if (
                argument.id in parameters.get(caller, set())
                and owner_kinds.get(caller) in {"helper", "fixture"}
                and argument.id not in {"self", "cls"}
                and source not in used
            ):
                used.add(source)
                changed = True

    values: dict[tuple[str, str], set[str]] = defaultdict(set)
    unresolved: set[tuple[str, str]] = set()
    edges: list[tuple[tuple[str, str], tuple[str, str]]] = []
    calls_seen: set[tuple[str, str]] = set()
    for destination, argument, call, caller in bindings:
        if destination not in used:
            continue
        calls_seen.add(destination)
        if argument is None:
            unresolved.add(destination)
            continue
        resolved = resolve_argument(argument, call)
        if resolved is not None:
            values[destination].add(resolved)
        elif isinstance(argument, ast.Name) and argument.id in parameters.get(caller, set()):
            source = (caller, argument.id)
            if source in used:
                edges.append((source, destination))
            else:
                unresolved.add(destination)
        else:
            cells = finite_container_cells(argument)
            if cells is not None:
                container_values, opaque = finite_container_values(argument, call)
                values[destination].update(container_values)
                if container_values or opaque:
                    # The container identity is not the contained family owner.
                    # Retain the family evidence, then reject the ambiguity.
                    unresolved.add(destination)
            elif not isinstance(argument, ast.Constant):
                unresolved.add(destination)
    for candidate in ambiguous:
        unresolved.update(key for key in used if key[0] == candidate)
    changed = True
    while changed:
        changed = False
        for source, destination in edges:
            before = len(values[destination])
            values[destination].update(values[source])
            changed |= len(values[destination]) != before
            if source in unresolved and destination not in unresolved:
                unresolved.add(destination)
                changed = True
    for key in used:
        if key not in calls_seen and key not in values:
            unresolved.add(key)
        # Unknown non-family/third-party helper arguments do not become family
        # owners merely because their helper mutates them. Once any call path
        # proves this parameter can receive a family owner, however, every
        # other path must also resolve finitely or the audit fails closed.
        if key in unresolved and values[key]:
            raise AuditError(
                f"{context}: helper mutation target {key[0]}.{key[1]} is not finitely resolved"
            )
    return {
        owner: {
            parameter: tuple(sorted(values[(owner, parameter)]))
            for parameter in parameters[owner]
            if values[(owner, parameter)]
        }
        for owner in functions
    }


def _forwarded_alias_variants(
    aliases: dict[str, str],
    owner: str,
    node: ast.AST,
    forwarded: dict[str, dict[str, tuple[str, ...]]],
) -> list[dict[str, str]]:
    names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
    substitutions = [
        (name, values) for name, values in forwarded.get(owner, {}).items() if name in names
    ]
    if not substitutions:
        return [aliases]
    result: list[dict[str, str]] = []
    for chosen in product(*(values for _, values in substitutions)):
        variant = dict(aliases)
        variant.update(
            {name: value for (name, _), value in zip(substitutions, chosen, strict=True)}
        )
        result.append(variant)
    return result


def _patch_idiom(call: ast.Call) -> str | None:
    """Return ``monkeypatch.setattr`` / ``patch.object`` for a matching call."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in PATCH_FUNCS:
        # <fixture>.setattr(...) — the monkeypatch fixture is conventionally
        # named `monkeypatch`, but accept any receiver so renamed fixtures and
        # `mp.setattr` helpers still register.
        return f"monkeypatch.{func.attr}"
    if func.attr == "object":
        # patch.object / mock.patch.object / unittest.mock.patch.object
        receiver = _dotted_name(func.value)
        if receiver and receiver.split(".")[-1] == "patch":
            return "patch.object"
    return None


def _attribute_assignment_targets(
    node: ast.Assign | ast.AugAssign | ast.AnnAssign,
) -> list[ast.Attribute]:
    """Return direct attribute targets that actually rebind a value."""
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]
    else:
        targets = [node.target] if node.value is not None else []
    return [target for target in targets if isinstance(target, ast.Attribute)]


def _call_arg(call: ast.Call, index: int, *names: str) -> ast.AST | None:
    if len(call.args) > index:
        return call.args[index]
    wanted = set(names)
    return next((kw.value for kw in call.keywords if kw.arg in wanted), None)


def _namespace_module(
    node: ast.AST,
    aliases: dict[str, str],
    source_aliases: dict[str, dict[str, str]],
    package_dotted: str,
    module_names: set[str],
) -> str | None:
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return _resolve_target(node.value, aliases, source_aliases, package_dotted, module_names)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "vars"
        and len(node.args) == 1
    ):
        return _resolve_target(node.args[0], aliases, source_aliases, package_dotted, module_names)
    return None


def _literal_mapping_keys(
    node: ast.AST | None,
    *,
    context: str,
    constants: dict[str, str] | None = None,
    expanded: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    if node is None:
        return []
    if not isinstance(node, ast.Dict):
        raise AuditError(f"{context}: mapping keys are not a literal dict")
    keys: list[str] = []
    for key in node.keys:
        values = _literal_strings(key, constants, expanded)
        if not values:
            raise AuditError(f"{context}: mapping contains a dynamic key")
        keys.extend(values)
    return keys


def _namespace_method_rows(
    call: ast.Call,
    aliases: dict[str, str],
    source_aliases: dict[str, dict[str, str]],
    package_dotted: str,
    module_names: set[str],
    *,
    context: str,
    constants: dict[str, str] | None = None,
    expanded: dict[str, tuple[str, ...]] | None = None,
) -> list[tuple[str, str, str]] | None:
    if not isinstance(call.func, ast.Attribute):
        return None
    module = _namespace_module(
        call.func.value, aliases, source_aliases, package_dotted, module_names
    )
    if module is None:
        return None
    method = call.func.attr
    if method in {"get", "items", "keys", "values", "copy", "__contains__"}:
        return []
    if method == "update":
        attributes: list[str] = []
        if call.args:
            attributes.extend(
                _literal_mapping_keys(
                    call.args[0], context=context, constants=constants, expanded=expanded
                )
            )
        for keyword in call.keywords:
            if keyword.arg is None:
                attributes.extend(
                    _literal_mapping_keys(
                        keyword.value,
                        context=context,
                        constants=constants,
                        expanded=expanded,
                    )
                )
            else:
                attributes.append(keyword.arg)
        if not attributes:
            raise AuditError(f"{context}: unexpandable namespace update against {module}")
        return [(module, attribute, "namespace.update") for attribute in attributes]
    if method in {"setdefault", "pop", "__setitem__", "__delitem__"}:
        attributes = _literal_strings(_call_arg(call, 0, "key"), constants, expanded)
        if not attributes:
            raise AuditError(f"{context}: dynamic namespace {method} against {module}")
        return [(module, attribute, f"namespace.{method}") for attribute in attributes]
    raise AuditError(f"{context}: unexpandable namespace method {method} against {module}")


def _extract_call_sites(
    call: ast.Call,
    aliases: dict[str, str],
    source_aliases: dict[str, dict[str, str]],
    package_dotted: str,
    module_names: set[str],
    *,
    context: str,
    constants: dict[str, str] | None = None,
    expanded: dict[str, tuple[str, ...]] | None = None,
) -> list[tuple[str, str, str]]:
    """Expand one supported mutation call into module/attribute/idiom rows."""

    func_name = _dotted_name(call.func) or ""
    func_tail = func_name.split(".")[-1]
    namespace_rows = _namespace_method_rows(
        call,
        aliases,
        source_aliases,
        package_dotted,
        module_names,
        context=context,
        constants=constants,
        expanded=expanded,
    )
    if namespace_rows is not None:
        return namespace_rows
    idiom = _patch_idiom(call)
    if idiom is not None or func_name in {"setattr", "delattr"}:
        target = _call_arg(call, 0, "target", "obj", "object")
        if target is None or isinstance(target, ast.Constant):
            return []
        module = _resolve_target(target, aliases, source_aliases, package_dotted, module_names)
        if module is None:
            return []
        attrs = _literal_strings(_call_arg(call, 1, "name", "attribute"), constants, expanded)
        if not attrs:
            raise AuditError(f"{context}: dynamic attribute against {package_dotted}.{module}")
        return [(module, attr, idiom or func_name) for attr in attrs]

    receiver = func_name.rsplit(".", 1)[0] if "." in func_name else ""
    is_patch = receiver.split(".")[-1] == "patch"
    if is_patch and func_tail == "multiple":
        target = _call_arg(call, 0, "target")
        if target is None:
            return []
        module = _resolve_target(target, aliases, source_aliases, package_dotted, module_names)
        if module is None:
            return []
        multiple_attrs: list[str] = []
        for keyword in call.keywords:
            if keyword.arg is None:
                multiple_attrs.extend(
                    _literal_mapping_keys(
                        keyword.value,
                        context=context,
                        constants=constants,
                        expanded=expanded,
                    )
                )
                continue
            if keyword.arg != "target":
                multiple_attrs.append(keyword.arg)
        if not multiple_attrs:
            raise AuditError(f"{context}: empty/unexpandable patch.multiple against {module}")
        return [(module, attr, "patch.multiple") for attr in multiple_attrs]

    if (is_patch and func_tail == "dict") or func_tail in {"setitem", "delitem"}:
        mapping = _call_arg(call, 0, "in_dict", "dic", "mapping")
        if mapping is None:
            return []
        module = _namespace_module(mapping, aliases, source_aliases, package_dotted, module_names)
        if module is None:
            return []
        if is_patch and func_tail == "dict":
            values = _call_arg(call, 1, "values")
            keys = _literal_mapping_keys(
                values, context=context, constants=constants, expanded=expanded
            )
            for keyword in call.keywords:
                if keyword.arg is None:
                    raise AuditError(f"{context}: dynamic patch.dict against {module}")
                if keyword.arg not in {"in_dict", "values", "clear"}:
                    keys.append(keyword.arg)
            if not keys:
                raise AuditError(f"{context}: empty/unexpandable patch.dict against {module}")
            return [(module, key, "patch.dict") for key in keys]
        keys = _literal_strings(_call_arg(call, 1, "name", "key"), constants, expanded)
        if not keys:
            raise AuditError(f"{context}: dynamic item key against {module}")
        return [(module, key, f"monkeypatch.{func_tail}") for key in keys]
    return []


def _assignment_rows(
    node: ast.Assign | ast.AugAssign | ast.AnnAssign | ast.Delete,
    aliases: dict[str, str],
    source_aliases: dict[str, dict[str, str]],
    package_dotted: str,
    module_names: dict[str, set[str]],
    *,
    context: str,
    constants: dict[str, str] | None = None,
    expanded: dict[str, tuple[str, ...]] | None = None,
) -> list[tuple[str, str, str]]:
    if isinstance(node, ast.Assign):
        targets = node.targets
        idiom = "assignment"
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]
        idiom = "augmented-assignment"
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target] if node.value is not None else []
        idiom = "annotated-assignment"
    else:
        targets = node.targets
        idiom = "deletion"
    rows: list[tuple[str, str, str]] = []
    known_modules = set(module_names)
    for target in targets:
        target_idiom = idiom
        module: str | None = None
        attribute: str | None = None
        if isinstance(target, ast.Attribute):
            module = _resolve_target(
                target.value, aliases, source_aliases, package_dotted, known_modules
            )
            attribute = target.attr
        elif isinstance(target, ast.Subscript):
            module = _namespace_module(
                target.value, aliases, source_aliases, package_dotted, known_modules
            )
            if module is not None:
                attributes = _literal_strings(target.slice, constants, expanded)
                if not attributes:
                    raise AuditError(f"{context}: dynamic namespace item against {module}")
                target_idiom = f"item-{target_idiom}"
                rows.extend((module, item, target_idiom) for item in attributes)
                continue
        if module is not None and attribute is not None:
            rows.append((module, attribute, target_idiom))
    return rows


def collect_sites(
    tests_dir: Path,
    auth_dir: Path | None = None,
    *,
    package_dotted: str = AUTH_DOTTED,
) -> list[PatchSite]:
    """Walk ``tests_dir`` and return expanded patch sites into one package/module."""
    if auth_dir is None:
        auth_dir = REPO_ROOT / "src" / "notebooklm" / "_auth"
    source_aliases = load_source_aliases(auth_dir)
    module_names = load_module_level_names(auth_dir)
    sites: list[PatchSite] = []
    known_modules = set(module_names)
    project_root = tests_dir.parent if tests_dir.name == "tests" else REPO_ROOT
    for path in sorted(tests_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        alias_context = _alias_context(tree, package_dotted)
        owners = _owner_metadata(tree, path)
        constants = _module_string_constants(tree)
        constant_context = _literal_constants_context(tree, constants)
        literal_values = _literal_values_context(tree, constant_context)

        def resolve_forwarded(
            argument: ast.AST,
            call: ast.Call,
            *,
            _alias_context: dict[int, dict[str, str]] = alias_context,
        ) -> str | None:
            module = _resolve_target(
                argument,
                _alias_context.get(id(call), {}),
                source_aliases,
                package_dotted,
                known_modules,
            )
            return f"{package_dotted}.{module}" if module is not None else None

        forwarded = _forwarded_parameter_context(
            tree,
            owners,
            resolve_forwarded,
            context=path.as_posix(),
            excluded_parameters=_definitely_nonfamily_typed_parameters(
                tree, _function_definitions(tree), (package_dotted,)
            ),
        )
        try:
            rel = path.relative_to(project_root).as_posix()
        except ValueError:
            rel = path.as_posix()
        for node in ast.walk(tree):
            aliases = alias_context.get(id(node), {})
            context = f"{rel}:{getattr(node, 'lineno', 0)}"
            owner_qualname, owner_kind = owners.get(id(node), ("<module>", "helper"))
            rows: list[tuple[str, str, str]] = []
            for variant in _forwarded_alias_variants(aliases, owner_qualname, node, forwarded):
                if isinstance(node, ast.Call):
                    rows.extend(
                        _extract_call_sites(
                            node,
                            variant,
                            source_aliases,
                            package_dotted,
                            known_modules,
                            context=context,
                            constants=constant_context.get(id(node), {}),
                            expanded=literal_values.get(id(node), {}),
                        )
                    )
                elif isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign | ast.Delete):
                    rows.extend(
                        _assignment_rows(
                            node,
                            variant,
                            source_aliases,
                            package_dotted,
                            module_names,
                            context=context,
                            constants=constant_context.get(id(node), {}),
                            expanded=literal_values.get(id(node), {}),
                        )
                    )
            for module, attribute, idiom in rows:
                sites.append(
                    PatchSite(
                        module=module,
                        attribute=attribute,
                        path=rel,
                        lineno=getattr(node, "lineno", 0),
                        idiom=idiom,
                        package=package_dotted,
                        owner_qualname=owner_qualname,
                        owner_kind=owner_kind,
                    )
                )
    return sorted(sites)


def summarize(sites: list[PatchSite]) -> dict[str, dict[str, int]]:
    """Per-module public/private/total counts, plus a ``TOTAL`` row."""
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"public": 0, "private": 0, "total": 0})
    for site in sites:
        row = counts[site.module]
        row["private" if site.is_private else "public"] += 1
        row["total"] += 1
    summary = {module: counts[module] for module in sorted(counts)}
    summary["TOTAL"] = {
        "public": sum(row["public"] for row in summary.values()),
        "private": sum(row["private"] for row in summary.values()),
        "total": sum(row["total"] for row in summary.values()),
    }
    return summary


def build_projection(sites: list[PatchSite]) -> dict[str, object]:
    """Return schema-v2 target, path, owner, and full-joint projections."""
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    files: dict[str, int] = defaultdict(int)
    owners: dict[tuple[str, str, str], int] = defaultdict(int)
    joint: dict[tuple[str, str, str, str, str, str, str], int] = defaultdict(int)
    for site in sites:
        counts[(site.module, site.attribute, site.idiom)] += 1
        files[site.path] += 1
        owners[(site.path, site.owner_qualname, site.owner_kind)] += 1
        joint[
            (
                site.package,
                site.module,
                site.attribute,
                site.idiom,
                site.path,
                site.owner_qualname,
                site.owner_kind,
            )
        ] += 1
    return {
        "version": 2,
        "package": sites[0].package if sites else None,
        "summary": summarize(sites),
        "sites": [
            {"module": module, "attribute": attribute, "idiom": idiom, "count": count}
            for (module, attribute, idiom), count in sorted(counts.items())
        ],
        "files": [{"path": path, "count": count} for path, count in sorted(files.items())],
        "owners": [
            {
                "path": path,
                "owner_qualname": owner,
                "owner_kind": kind,
                "count": count,
            }
            for (path, owner, kind), count in sorted(owners.items())
        ],
        "joint_sites": [
            {
                "package": package,
                "module": module,
                "attribute": attribute,
                "idiom": idiom,
                "path": path,
                "owner_qualname": owner,
                "owner_kind": kind,
                "count": count,
            }
            for (package, module, attribute, idiom, path, owner, kind), count in sorted(
                joint.items()
            )
        ],
    }


RowIdentity = tuple[tuple[str, object], ...]


def _rows_by_identity(projection: dict[str, object], key: str) -> dict[RowIdentity, int]:
    rows = projection.get(key, [])
    if not isinstance(rows, list):
        return {}
    result: dict[RowIdentity, int] = {}
    for row in rows:
        if not isinstance(row, dict) or "count" not in row:
            continue
        identity: RowIdentity = tuple(
            (str(name), value) for name, value in sorted(row.items()) if name != "count"
        )
        result[identity] = int(row["count"])
    return result


def projection_growth(previous: object, current: object) -> list[str]:
    """Return every no-growth violation in a schema-v2 patch projection."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return ["projection is not an object"]
    previous_map: Any = previous
    current_map: Any = current
    errors: list[str] = []
    for key in ("sites", "files", "owners", "joint_sites"):
        old, new = _rows_by_identity(previous_map, key), _rows_by_identity(current_map, key)
        for identity, count in new.items():
            before = old.get(identity, 0)
            if count > before:
                errors.append(f"{key} {dict(identity)} grew {before} -> {count}")
    old_total = previous_map.get("summary", {}).get("TOTAL", {})
    new_total = current_map.get("summary", {}).get("TOTAL", {})
    for name in ("total", "private"):
        if int(new_total.get(name, 0)) > int(old_total.get(name, 0)):
            errors.append(
                f"summary TOTAL.{name} grew {old_total.get(name, 0)} -> {new_total[name]}"
            )
    old_assignments = sum(
        count
        for identity, count in _rows_by_identity(previous_map, "joint_sites").items()
        if dict(identity).get("idiom")
        in {
            "assignment",
            "annotated-assignment",
            "augmented-assignment",
            "item-assignment",
            "item-annotated-assignment",
            "item-augmented-assignment",
        }
    )
    new_assignments = sum(
        count
        for identity, count in _rows_by_identity(current_map, "joint_sites").items()
        if dict(identity).get("idiom")
        in {
            "assignment",
            "annotated-assignment",
            "augmented-assignment",
            "item-assignment",
            "item-annotated-assignment",
            "item-augmented-assignment",
        }
    )
    if new_assignments > old_assignments:
        errors.append(f"direct assignments grew {old_assignments} -> {new_assignments}")
    return errors


def build_family_scorecard(projections: list[dict[str, object]]) -> dict[str, object]:
    """Combine family projections without erasing package relocation."""
    raw_projections: Any = projections
    joints: list[dict[str, Any]] = [
        row for projection in raw_projections for row in projection.get("joint_sites", [])
    ]
    total = sum(int(row["count"]) for row in joints)
    private = sum(int(row["count"]) for row in joints if str(row["attribute"]).startswith("_"))
    assignments = sum(
        int(row["count"])
        for row in joints
        if "assignment" in str(row["idiom"]) and "monkeypatch" not in str(row["idiom"])
    )
    packages = {
        str(projection.get("package")): int(
            projection.get("summary", {}).get("TOTAL", {}).get("total", 0)
        )
        for projection in raw_projections
    }
    return {
        "version": 1,
        "summary": {
            "total": total,
            "private": private,
            "public": total - private,
            "distinct_targets": len(
                {(row["package"], row["module"], row["attribute"]) for row in joints}
            ),
            "files": len({row["path"] for row in joints}),
            "lexical_owners": len(
                {(row["path"], row["owner_qualname"], row["owner_kind"]) for row in joints}
            ),
            "assignments": assignments,
        },
        "packages": packages,
        "joint_sites": sorted(
            joints,
            key=lambda row: tuple(str(row.get(key, "")) for key in sorted(row)),
        ),
    }


def family_scorecard_growth(previous: object, current: object) -> list[str]:
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return ["family scorecard is not an object"]
    previous_map: Any = previous
    current_map: Any = current
    errors: list[str] = []
    old_joint = {
        tuple(sorted((k, v) for k, v in row.items() if k != "count")): int(row["count"])
        for row in previous_map.get("joint_sites", [])
    }
    new_joint = {
        tuple(sorted((k, v) for k, v in row.items() if k != "count")): int(row["count"])
        for row in current_map.get("joint_sites", [])
    }
    for identity, count in new_joint.items():
        if count > old_joint.get(identity, 0):
            errors.append(
                f"family joint row {dict(identity)} grew {old_joint.get(identity, 0)} -> {count}"
            )
    for name, value in current_map.get("summary", {}).items():
        if name in {
            "total",
            "private",
            "distinct_targets",
            "files",
            "lexical_owners",
            "assignments",
        }:
            old = int(previous_map.get("summary", {}).get(name, 0))
            if int(value) > old:
                errors.append(f"family {name} grew {old} -> {value}")
    for package, value in current_map.get("packages", {}).items():
        old = int(previous_map.get("packages", {}).get(package, 0))
        if int(value) > old:
            errors.append(f"package {package} grew {old} -> {value}")
    return errors


def render_table(summary: dict[str, dict[str, int]]) -> str:
    """Render the per-module count table as fixed-width text."""
    width = max((len(name) for name in summary), default=6)
    width = max(width, len("module"))
    lines = [
        f"{'module'.ljust(width)}  {'public':>7}  {'private':>7}  {'total':>7}",
        f"{'-' * width}  {'-' * 7}  {'-' * 7}  {'-' * 7}",
    ]
    for name, row in summary.items():
        if name == "TOTAL":
            lines.append(f"{'-' * width}  {'-' * 7}  {'-' * 7}  {'-' * 7}")
        lines.append(
            f"{name.ljust(width)}  {row['public']:>7}  {row['private']:>7}  {row['total']:>7}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--tests-dir",
        type=Path,
        default=REPO_ROOT / "tests",
        help="directory to walk (default: <repo>/tests)",
    )
    parser.add_argument(
        "--auth-dir",
        type=Path,
        default=REPO_ROOT / "src" / "notebooklm" / "_auth",
        help="the _auth package to resolve aliases against (default: <repo>/src/notebooklm/_auth)",
    )
    parser.add_argument(
        "--module-file",
        type=Path,
        help="resolve one module facade instead of the --auth-dir package",
    )
    parser.add_argument(
        "--package-prefix",
        default=AUTH_DOTTED,
        help="dotted private package represented by --auth-dir",
    )
    parser.add_argument(
        "--module",
        action="append",
        default=None,
        help="restrict output to this _auth submodule (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument(
        "--list-sites",
        action="store_true",
        help="also print every site (file:line attribute) in text mode",
    )
    args = parser.parse_args(argv)

    if not args.tests_dir.is_dir():
        parser.error(f"not a directory: {args.tests_dir}")
    # Fail loudly rather than under-report: a missing/renamed _auth dir silently
    # drops every indirect site (12 of them at the 2026-08-07 baseline) and still
    # exits 0, which would read as "the count went down".
    source = args.module_file or args.auth_dir
    if args.module_file is not None and not source.is_file():
        parser.error(f"not a file: {source}")
    if args.module_file is None and not source.is_dir():
        parser.error(f"not a directory: {source}")

    sites = collect_sites(
        args.tests_dir,
        source,
        package_dotted=args.package_prefix,
    )
    if args.module:
        wanted = set(args.module)
        sites = [site for site in sites if site.module in wanted]
    summary = summarize(sites)

    if args.json:
        json.dump(build_projection(sites), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    print(render_table(summary))
    if args.list_sites:
        print()
        for site in sites:
            kind = "private" if site.is_private else "public "
            print(f"{kind}  {site.module}.{site.attribute}  ({site.path}:{site.lineno})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
