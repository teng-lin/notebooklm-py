"""Guard that batchexecute RPC method IDs live only in ``rpc/types.py``.

CLAUDE.md is explicit: ``src/notebooklm/rpc/types.py`` is the *source of truth*
for every obfuscated batchexecute method ID, and the only escape hatch is the
env-driven runtime override in ``rpc/overrides.py``. Nothing else in
``src/notebooklm/`` should hardcode a raw method-ID string.

This AST lint enforces that invariant two ways:

* **Value containment** -- no string literal anywhere under
  ``src/notebooklm/`` (excluding ``rpc/``) may equal a known ``RPCMethod``
  value. A developer who pastes ``"R7cb6c"`` into a feature module is
  bypassing the enum (and the override system that keys off it).
* **Positional shape** -- no string literal may be passed as the method
  argument of an ``rpc_call`` / ``_rpc_call`` invocation. The method argument
  must be an ``RPCMethod`` member access, never an inline string -- this also
  catches a *freshly invented* ID that has not (yet) been added to the enum.

The method-ID vocabulary is read from ``rpc/types.py`` via AST so this lint
never drifts from the source of truth and pulls in no import side effects.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"
TYPES_MODULE = SRC_ROOT / "rpc" / "types.py"

# Call targets whose first positional argument is a method ID. Matched by the
# (possibly attribute-qualified) callee name, e.g. ``self._rpc.rpc_call(...)``.
RPC_DISPATCH_NAMES = frozenset({"rpc_call", "_rpc_call"})


def _rpc_method_values(types_module: Path = TYPES_MODULE) -> frozenset[str]:
    """Return the set of ``RPCMethod`` string values declared in ``types.py``."""
    tree = ast.parse(types_module.read_text(encoding="utf-8"), filename=str(types_module))
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RPCMethod":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    values.add(stmt.value.value)
    return frozenset(values)


def _feature_files() -> list[Path]:
    """All ``src/notebooklm`` Python files outside the RPC layer (``rpc/``)."""
    return sorted(p for p in SRC_ROOT.rglob("*.py") if p.relative_to(SRC_ROOT).parts[0] != "rpc")


def _repo_relative(path: Path) -> Path:
    return path.resolve().relative_to(PROJECT_ROOT)


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _hardcoded_method_id_offenders(
    tree: ast.AST,
    method_values: frozenset[str],
    location: str,
) -> list[str]:
    """Return ``location``-prefixed offender strings for a parsed module tree.

    Pure on its inputs so a unit test can exercise it against a planted
    fixture without touching the filesystem.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        # (1) Any string literal equal to a known RPCMethod value.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in method_values:
                offenders.append(
                    f"{location}:{node.lineno}: hardcoded RPCMethod value {node.value!r}"
                )
        # (2) A string literal passed as the method argument to rpc_call.
        if isinstance(node, ast.Call) and node.args:
            if _callee_name(node.func) in RPC_DISPATCH_NAMES:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    offenders.append(
                        f"{location}:{node.lineno}: rpc_call() method argument is the "
                        f"string literal {first.value!r}, not an RPCMethod member"
                    )
    return offenders


def test_rpc_method_values_are_discovered() -> None:
    """Sanity-check the AST extractor so a future refactor of the enum can't
    silently empty the vocabulary and turn the lint into a no-op."""
    assert len(_rpc_method_values()) >= 40


def test_no_hardcoded_rpc_method_ids_outside_rpc_layer() -> None:
    method_values = _rpc_method_values()
    offenders: list[str] = []
    for path in _feature_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(
            _hardcoded_method_id_offenders(tree, method_values, str(_repo_relative(path)))
        )

    assert not offenders, (
        "Batchexecute RPC method IDs must live only in src/notebooklm/rpc/types.py "
        "(the source of truth per CLAUDE.md). Reference them via the RPCMethod enum "
        "instead of hardcoding the obfuscated string; the only runtime escape hatch "
        "is rpc/overrides.py.\n\n" + "\n".join(offenders)
    )


def test_lint_flags_a_planted_hardcoded_method_id() -> None:
    """The lint must catch both a pasted RPCMethod value and an inline method
    argument to ``rpc_call`` -- including an ID not (yet) in the enum."""
    method_values = _rpc_method_values()
    planted_known_id = next(iter(method_values))

    fixture = "\n".join(
        [
            f'LEAKED = "{planted_known_id}"',  # pasted known RPCMethod value
            'await self._rpc.rpc_call("InVnTd1", params)',  # inline (unknown) ID
        ]
    )
    tree = ast.parse(fixture)

    offenders = _hardcoded_method_id_offenders(tree, method_values, "<fixture>")

    assert any(planted_known_id in offender for offender in offenders), offenders
    assert any("InVnTd1" in offender for offender in offenders), offenders


def test_lint_ignores_rpcmethod_member_dispatch() -> None:
    """A correct ``rpc_call(RPCMethod.X, ...)`` call must not be flagged."""
    tree = ast.parse("await self._rpc.rpc_call(RPCMethod.LIST_NOTEBOOKS, params)")
    assert _hardcoded_method_id_offenders(tree, _rpc_method_values(), "<fixture>") == []
