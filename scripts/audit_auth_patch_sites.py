#!/usr/bin/env python3
"""Count the test-suite patch sites that reach into ``notebooklm._auth``.

This script is the **definition** of the patch-site metric used by the ``_auth``
deepening effort (ADR-0033 plan §7/§9). Ad-hoc greps for the same thing ranged
from 73 to 219 depending on which idiom the author happened to match, so the
reduction target is meaningless unless one instrument owns the count.

What counts as a site
---------------------
A call to ``monkeypatch.setattr`` or ``patch.object`` whose FIRST argument is a
**module object** resolving to ``notebooklm._auth.<module>``, and whose second
argument is a string literal naming the attribute being replaced::

    from notebooklm._auth import refresh as _auth_refresh
    monkeypatch.setattr(_auth_refresh, "_poke_session", fake)   # <- one site

**Plain assignment counts too**::

    _auth_storage._FLOCK_UNAVAILABLE_WARNED = False             # <- one site

It reaches into a module's private state exactly like ``setattr`` does, and it is
strictly worse because pytest never restores it. Counting only the call idioms
would let a later PR "improve" this metric by rewriting monkeypatch calls as
assignments while making the coupling worse. An assignment counts only when the
attribute is a real module-level name of the resolved module: a test file's alias
map is file-global while a Python binding is function-scoped, so a local can
shadow a module alias imported inside a different test (``mock.return_value = …``
is the common shape), and that check rejects it without special-casing mocks.

Each site is classified ``private`` when the attribute name starts with an
underscore and ``public`` otherwise, because §9's acceptance criterion is about
private-attribute coupling specifically.

Baseline (2026-08-08, ``87227de1``, ADR-0034 PR 0)
--------------------------------------------------
TOTAL 171 public / 109 private / 280 sites. ``_auth/storage.py`` carries 53 of
those: 27 public and 26 private. The committed ``auth_patch_sites`` baseline
also freezes every ``(module, attribute, idiom)`` count, so a privacy rename or
helper rewrite cannot make the scorecard look healthier without a reviewed
baseline diff. Re-measure rather than quoting an older number from memory.

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

Deliberate exclusions
---------------------
* **String targets** (``monkeypatch.setattr("notebooklm._auth.refresh.x", …)``)
  are NOT counted. They are a separate, separately-banned idiom — see
  ``tests/_guardrails/test_no_forbidden_monkeypatches.py`` — and counting them
  here would double-book the same debt against two gates.
* Patches of non-module objects (classes, instances, fixtures) are not module
  seams and are out of scope.
* ``monkeypatch.delattr`` / ``patch`` (non-``.object``) are likewise out of
  scope; the metric tracks module-attribute REBINDING.
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
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import cast

AUTH_PACKAGE = ("notebooklm", "_auth")
AUTH_DOTTED = ".".join(AUTH_PACKAGE)
PATCH_FUNCS = {"setattr"}  # matched as <something>.setattr(...)
REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, order=True)
class PatchSite:
    """One resolved module-object patch of a ``notebooklm._auth`` attribute."""

    module: str  # the _auth submodule, e.g. "refresh"
    attribute: str  # the attribute being rebound, e.g. "_poke_session"
    path: str  # repo-relative test file
    lineno: int
    idiom: str  # "monkeypatch.setattr" | "patch.object" | "assignment"

    @property
    def is_private(self) -> bool:
        return self.attribute.startswith("_")


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


def _auth_import_bindings(node: ast.Import | ast.ImportFrom) -> dict[str, str]:
    """Auth-module bindings created by one import statement."""
    result: dict[str, str] = {}
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module == AUTH_DOTTED:
            for alias in node.names:
                result[alias.asname or alias.name] = f"{AUTH_DOTTED}.{alias.name}"
        elif module == AUTH_PACKAGE[0]:
            for alias in node.names:
                if alias.name == AUTH_PACKAGE[1]:
                    result[alias.asname or alias.name] = AUTH_DOTTED
    else:
        for alias in node.names:
            if not alias.name.startswith(f"{AUTH_DOTTED}."):
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

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            locals_.add(node.name)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            pass

        def visit_ListComp(self, node: ast.ListComp) -> None:
            pass

        visit_SetComp = visit_ListComp
        visit_DictComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp

    collector = Collector()
    body = scope.body if not isinstance(scope, ast.Lambda) else [scope.body]
    for statement in body:
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

    def __init__(self, tree: ast.Module) -> None:
        self.context: dict[int, dict[str, str]] = {}
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
            if aliases:
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
            bindings = _auth_import_bindings(node)
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
                    self._bind(name, None)
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
                self._bind(name, None)
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


def _alias_context(tree: ast.Module) -> dict[int, dict[str, str]]:
    """Effective auth aliases at every node under sequential lexical rules."""
    return _AliasResolver(tree).context


def load_source_aliases(auth_dir: Path) -> dict[str, dict[str, str]]:
    """Per ``_auth`` module, its module-level aliases for OTHER ``_auth`` modules.

    ``psidts_recovery.py`` does ``from . import storage as _auth_storage``, so a
    test writing ``monkeypatch.setattr(psidts_recovery._auth_storage, …)`` is
    really patching ``_auth.storage``. Without this map such a site is either
    dropped (undercount) or billed to ``psidts_recovery`` (mis-attribution).
    """
    source_aliases: dict[str, dict[str, str]] = {}
    if not auth_dir.is_dir():
        return source_aliases
    known = {path.stem for path in auth_dir.glob("*.py")}
    for path in sorted(auth_dir.glob("*.py")):
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
    if not auth_dir.is_dir():
        return names
    for path in sorted(auth_dir.glob("*.py")):
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

    # Fully-qualified: notebooklm._auth.<module>[.<attr>]
    if dotted.startswith(f"{AUTH_DOTTED}."):
        tail = dotted[len(AUTH_DOTTED) + 1 :].split(".")
        if len(tail) == 1:
            return tail[0]
        if len(tail) == 2:
            return source_aliases.get(tail[0], {}).get(tail[1])
        return None

    head, _, rest = dotted.partition(".")
    target = aliases.get(head)
    if target is None:
        return None

    if target == AUTH_DOTTED:
        # `_auth.refresh` off the package binding.
        parts = rest.split(".") if rest else []
        if len(parts) == 1:
            return parts[0]
        if len(parts) == 2:
            return source_aliases.get(parts[0], {}).get(parts[1])
        return None

    if target.startswith(f"{AUTH_DOTTED}."):
        module = target[len(AUTH_DOTTED) + 1 :].split(".")[0]
        if not rest:
            return module
        parts = rest.split(".")
        if len(parts) == 1:
            return source_aliases.get(module, {}).get(parts[0])
        return None
    return None


def _patch_idiom(call: ast.Call) -> str | None:
    """Return ``monkeypatch.setattr`` / ``patch.object`` for a matching call."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in PATCH_FUNCS:
        # <fixture>.setattr(...) — the monkeypatch fixture is conventionally
        # named `monkeypatch`, but accept any receiver so renamed fixtures and
        # `mp.setattr` helpers still register.
        return "monkeypatch.setattr"
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


def collect_sites(tests_dir: Path, auth_dir: Path | None = None) -> list[PatchSite]:
    """Walk ``tests_dir`` and return every resolved ``_auth`` patch site."""
    if auth_dir is None:
        auth_dir = REPO_ROOT / "src" / "notebooklm" / "_auth"
    source_aliases = load_source_aliases(auth_dir)
    module_names = load_module_level_names(auth_dir)
    sites: list[PatchSite] = []
    for path in sorted(tests_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        candidates = [
            node
            for node in ast.walk(tree)
            if (
                isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                and _attribute_assignment_targets(node)
            )
            or (isinstance(node, ast.Call) and _patch_idiom(node) is not None)
        ]
        if not candidates:
            continue
        alias_context = _alias_context(tree)
        try:
            rel = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        for node in candidates:
            aliases = alias_context.get(id(node), {})
            # Plain rebinding: ``_auth_storage._FLOCK_UNAVAILABLE_WARNED = False``.
            # This is a patch site by every meaning that matters — it reaches into a
            # module's private state — and it is STRICTLY WORSE than monkeypatch
            # because pytest never restores it. Counting only the call idioms would
            # let a later PR "improve" the metric by converting monkeypatch calls
            # into assignments while making the coupling worse.
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                for target_node in _attribute_assignment_targets(node):
                    module = _resolve_target(target_node.value, aliases, source_aliases)
                    if module is None:
                        continue
                    # Reject a local that merely shadows a module alias (see
                    # load_module_level_names): only a real module-level name counts.
                    if target_node.attr not in module_names.get(module, set()):
                        continue
                    sites.append(
                        PatchSite(
                            module=module,
                            attribute=target_node.attr,
                            path=rel,
                            lineno=node.lineno,
                            idiom="assignment",
                        )
                    )
                continue
            if not isinstance(node, ast.Call):
                continue
            idiom = _patch_idiom(node)
            if idiom is None:
                continue
            # Both idioms accept their first two arguments by KEYWORD:
            # ``monkeypatch.setattr(target=..., name="x")`` and
            # ``patch.object(target=..., attribute="x")``. A positional-only
            # scan silently under-counts, which reads as "the metric improved".
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            target = node.args[0] if node.args else keywords.get("target")
            attr_node = (
                node.args[1]
                if len(node.args) > 1
                else keywords.get("name") or keywords.get("attribute")
            )
            if target is None or attr_node is None:
                continue
            # String targets are a different, separately-banned idiom.
            if isinstance(target, ast.Constant):
                continue
            if not (isinstance(attr_node, ast.Constant) and isinstance(attr_node.value, str)):
                continue
            module = _resolve_target(target, aliases, source_aliases)
            if module is None:
                continue
            sites.append(
                PatchSite(
                    module=module,
                    attribute=attr_node.value,
                    path=rel,
                    lineno=node.lineno,
                    idiom=idiom,
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
    """Return the stable, path/line-free baseline projection."""
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for site in sites:
        counts[(site.module, site.attribute, site.idiom)] += 1
    return {
        "version": 1,
        "summary": summarize(sites),
        "sites": [
            {"module": module, "attribute": attribute, "idiom": idiom, "count": count}
            for (module, attribute, idiom), count in sorted(counts.items())
        ],
    }


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
    if not args.auth_dir.is_dir():
        parser.error(f"not a directory: {args.auth_dir}")

    sites = collect_sites(args.tests_dir, args.auth_dir)
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
