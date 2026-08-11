"""Guard that ``master_token.json`` is derived in exactly one place.

Before #2103's structural follow-up, four call sites derived the master-token
sibling path independently and disagreed on canonicalization for a
symlinked/relative ``storage_state.json`` path — a resolved-parent join
(``_auth/recovery.py``'s L4 rung), an unresolved ``with_name``
(``_app/auth_check.py``, and the pre-#2104 CLI login writer), and a raw
``.parent`` join (``cli/services/auth_refresh.py``). #2103/#2104/#2105 fixed
the write-time divergence for one site at a time; PR-1 of the structural
follow-up converges all four onto :func:`notebooklm.paths.master_token_path_for`,
the sole derivation site.

This lint keeps it that way, mirroring the established pattern in
``test_app_host_literals_centralized.py`` (#2019's "two notions of one fact
disagree" lesson, applied here): no module under ``src/notebooklm/`` may spell
the ``"master_token.json"`` string literal in executable code except
``paths.py`` itself. A second literal is exactly how the four-site divergence
this PR closes was created in the first place.

Scope is deliberately narrow: only the two AST shapes that actually construct
a *path* from the literal are flagged —

* ``x / "master_token.json"`` (a ``/`` join, the ``BinOp``/``Div`` shape every
  pre-convergence derivation site used in some form), and
* ``x.with_name("master_token.json")`` (the other shape two sites used).

Human-readable mentions of the filename — log messages, error strings, a
docstring — are common and legitimate (``master.py``'s ``MasterTokenError``
messages, ``auth_check.py``'s user-facing hint) and are NOT path derivation;
flagging every string containing the substring would drown the one signal
that matters in false positives from prose. Test modules are out of scope on
purpose — a test asserting a concrete sibling path is the point of the test,
not a second source of truth.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"
PATHS_MODULE = SRC_ROOT / "paths.py"

MASTER_TOKEN_FILENAME = "master_token.json"
_CANONICAL_PATH_MODULE = "notebooklm.paths"
_EXPECTED_CALLERS = {
    "_app/master_token.py:bootstrap_login",
    "_app/master_token.py:inspect_master_token_status",
    "_auth/master_token_bootstrap.py:MasterTokenBootstrapper.bootstrap_from_oauth_token",
    "_auth/master_token_bootstrap.py:MasterTokenBootstrapper.bootstrap_storage",
    "_auth/master_token_bootstrap.py:MasterTokenBootstrapper.remint_from_stored_token",
    "_auth/profile_store.py:ProfileStore.read_master_token",
    "_auth/profile_store.py:ProfileStore.write_master_token",
    "paths.py:get_master_token_path",
    "_auth/recovery.py:_try_master_token_reauth_result",
}


def _is_master_token_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == MASTER_TOKEN_FILENAME


def _derivation_sites(module: Path) -> list[int]:
    """Return line numbers where the literal is used to construct a path.

    Matches ``<expr> / "master_token.json"`` and
    ``<expr>.with_name("master_token.json")`` — the two shapes every
    pre-convergence call site used. A bare mention elsewhere (log message,
    error string, docstring) does not match either shape and is ignored.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    sites: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and _is_master_token_literal(node.right)
        ) or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "with_name"
            and len(node.args) == 1
            and _is_master_token_literal(node.args[0])
        ):
            sites.append(node.lineno)
    return sites


def test_paths_module_declares_the_derivation():
    """Non-empty vocabulary, else the lint below would silently pass."""
    assert _derivation_sites(PATHS_MODULE), (
        f"{PATHS_MODULE} no longer derives {MASTER_TOKEN_FILENAME!r} via a "
        "'/' join or .with_name(...) — master_token_path_for was rewritten; "
        "update this lint's detection shapes to match."
    )


def test_no_bare_master_token_derivation_outside_paths_module():
    """No module may derive the sibling path itself; call ``master_token_path_for``."""
    violations: list[str] = []

    for module in sorted(SRC_ROOT.rglob("*.py")):
        if module == PATHS_MODULE:
            continue
        relative = module.relative_to(SRC_ROOT).as_posix()
        for lineno in _derivation_sites(module):
            violations.append(f"{relative}:{lineno} derives {MASTER_TOKEN_FILENAME!r} directly")

    assert not violations, (
        "Call notebooklm.paths.master_token_path_for(storage_path) instead of "
        f"deriving {MASTER_TOKEN_FILENAME!r} directly (a second derivation site "
        "is exactly how the four-site canonicalization divergence #2103 PR-1 "
        "closed was created):\n  " + "\n  ".join(violations)
    )


def _resolved_import(
    path: Path, node: ast.ImportFrom, *, source_root: Path = SRC_ROOT
) -> str | None:
    if node.level == 0:
        return node.module or ""
    package = ("notebooklm", *path.relative_to(source_root).parent.parts)
    parents = node.level - 1
    if parents >= len(package):
        return None
    base = package[: len(package) - parents]
    suffix = tuple((node.module or "").split(".")) if node.module else ()
    return ".".join((*base, *suffix))


def _bound_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.arg):
            names.add(item.arg)
        elif isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
            names.add(item.id)
        elif isinstance(item, ast.alias):
            names.add(item.asname or item.name.split(".", 1)[0])
    return names


def _scope_bindings(
    path: Path, body: list[ast.stmt], *, source_root: Path = SRC_ROOT
) -> tuple[set[str], set[str], set[str]]:
    """Return exact direct/module bindings and names made ambiguous in one scope."""
    direct: set[str] = set()
    modules: set[str] = set()
    canonical_assignments: dict[str, int] = {}
    all_assignments: dict[str, int] = {}
    for statement in body:
        statement_bound = _bound_names(statement)
        for name in statement_bound:
            all_assignments[name] = all_assignments.get(name, 0) + 1
        if (
            isinstance(statement, ast.ImportFrom)
            and _resolved_import(path, statement, source_root=source_root) == _CANONICAL_PATH_MODULE
        ):
            for item in statement.names:
                if item.name == "master_token_path_for":
                    name = item.asname or item.name
                    direct.add(name)
                    canonical_assignments[name] = canonical_assignments.get(name, 0) + 1
        elif isinstance(statement, ast.Import):
            for item in statement.names:
                if item.name == _CANONICAL_PATH_MODULE:
                    name = item.asname or item.name.split(".", 1)[0]
                    modules.add(name)
                    canonical_assignments[name] = canonical_assignments.get(name, 0) + 1
    ambiguous = {
        name
        for name, count in all_assignments.items()
        if count != canonical_assignments.get(name, 0) or canonical_assignments.get(name, 0) != 1
    }
    return direct - ambiguous, modules - ambiguous, ambiguous


class _PathCallCollector(ast.NodeVisitor):
    def __init__(self, path: Path, tree: ast.Module, *, source_root: Path = SRC_ROOT) -> None:
        self.path = path
        self.source_root = source_root
        self.module_direct, self.module_aliases, self.module_ambiguous = _scope_bindings(
            path, tree.body, source_root=source_root
        )
        if path == source_root / "paths.py":
            local_definitions = [
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == "master_token_path_for"
            ]
            other_bindings = [
                node
                for node in tree.body
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and "master_token_path_for" in _bound_names(node)
            ]
            if len(local_definitions) == 1 and not other_bindings:
                self.module_direct.add("master_token_path_for")
                self.module_ambiguous.discard("master_token_path_for")
        self.stack: list[str] = []
        self.local_direct: list[set[str]] = []
        self.local_aliases: list[set[str]] = []
        self.local_ambiguous: list[set[str]] = []
        self.callers: set[str] = set()
        self.untrusted: set[str] = set()

    @property
    def owner(self) -> str:
        return ".".join(self.stack) if self.stack else "<module>"

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        direct, aliases, ambiguous = _scope_bindings(
            self.path, node.body, source_root=self.source_root
        )
        ambiguous |= {
            arg.arg for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        }
        if node.args.vararg:
            ambiguous.add(node.args.vararg.arg)
        if node.args.kwarg:
            ambiguous.add(node.args.kwarg.arg)
        self.stack.append(node.name)
        self.local_direct.append(direct - ambiguous)
        self.local_aliases.append(aliases - ambiguous)
        self.local_ambiguous.append(ambiguous)
        for statement in node.body:
            self.visit(statement)
        self.local_ambiguous.pop()
        self.local_aliases.pop()
        self.local_direct.pop()
        self.stack.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self.stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        spelling: str | None = None
        trusted = False
        if isinstance(node.func, ast.Name) and node.func.id == "master_token_path_for":
            spelling = node.func.id
            local_ambiguous = self.local_ambiguous[-1] if self.local_ambiguous else set()
            local_direct = self.local_direct[-1] if self.local_direct else set()
            trusted = node.func.id in local_direct or (
                node.func.id not in local_ambiguous
                and node.func.id in self.module_direct
                and node.func.id not in self.module_ambiguous
            )
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "master_token_path_for":
            spelling = ast.unparse(node.func)
            if isinstance(node.func.value, ast.Name):
                name = node.func.value.id
                local_ambiguous = self.local_ambiguous[-1] if self.local_ambiguous else set()
                local_aliases = self.local_aliases[-1] if self.local_aliases else set()
                trusted = name in local_aliases or (
                    name not in local_ambiguous
                    and name in self.module_aliases
                    and name not in self.module_ambiguous
                )
        if spelling is not None:
            label = f"{self.path.relative_to(self.source_root).as_posix()}:{self.owner}"
            (self.callers if trusted else self.untrusted).add(label)
        self.generic_visit(node)


def _master_token_path_callers(root: Path) -> tuple[set[str], set[str]]:
    callers: set[str] = set()
    untrusted: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _PathCallCollector(path, tree, source_root=root)
        collector.visit(tree)
        callers.update(collector.callers)
        untrusted.update(collector.untrusted)
    return callers, untrusted


def test_master_token_path_callers_are_exact_including_profile_store_methods() -> None:
    callers, untrusted = _master_token_path_callers(SRC_ROOT)
    assert callers == _EXPECTED_CALLERS
    assert untrusted == set()


def test_path_call_detector_bites_on_aliases_and_rebindings(tmp_path: Path) -> None:
    root = tmp_path / "notebooklm"
    root.mkdir()
    good = root / "good.py"
    good.write_text(
        "import notebooklm.paths as p\ndef use(path):\n    return p.master_token_path_for(path)\n",
        encoding="utf-8",
    )
    callers, untrusted = _master_token_path_callers(root)
    assert callers == {"good.py:use"}
    assert untrusted == set()

    good.write_text(
        "import notebooklm.paths as p\np = object()\ndef use(path):\n    return p.master_token_path_for(path)\n",
        encoding="utf-8",
    )
    callers, untrusted = _master_token_path_callers(root)
    assert callers == set()
    assert untrusted == {"good.py:use"}
