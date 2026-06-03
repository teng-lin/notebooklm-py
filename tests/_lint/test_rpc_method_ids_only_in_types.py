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
* **Call shape** -- no string literal may be passed as the method argument of
  an ``rpc_call`` / ``_rpc_call`` invocation, nor to the ``RPCMethod``
  constructor, whether positionally or by keyword (``method=`` / ``value=``).
  The method argument must be an ``RPCMethod`` member access, never an inline
  string -- this also catches a *freshly invented* ID that has not (yet) been
  added to the enum.

The method-ID vocabulary is read from ``rpc/types.py`` via AST so this lint
never drifts from the source of truth and pulls in no import side effects.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"
TYPES_MODULE = SRC_ROOT / "rpc" / "types.py"

# Call targets whose method-ID argument must be an ``RPCMethod`` member, never
# an inline string. Matched by the (possibly attribute-qualified) callee name,
# e.g. ``self._rpc.rpc_call(...)`` or a direct ``RPCMethod(...)`` construction.
# Each maps to the keyword name its method-ID argument can also be passed under,
# so ``rpc_call(method="...")`` / ``RPCMethod(value="...")`` cannot slip past the
# positional check.
RPC_DISPATCH_KEYWORDS: dict[str, str] = {
    "rpc_call": "method",
    "_rpc_call": "method",
    "RPCMethod": "value",
}


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


def _method_id_argument(node: ast.Call, keyword: str) -> ast.expr | None:
    """Return the method-ID argument of ``node`` (positional first arg or the
    named keyword), or ``None`` if neither is present."""
    if node.args:
        return node.args[0]
    for kw in node.keywords:
        if kw.arg == keyword:
            return kw.value
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
        # (2) A string literal passed (positionally or by keyword) as the method
        #     argument to rpc_call/_rpc_call, or to the RPCMethod constructor.
        #     This also catches a *freshly invented* ID not yet in the enum.
        if isinstance(node, ast.Call):
            callee = _callee_name(node.func)
            keyword = RPC_DISPATCH_KEYWORDS.get(callee) if callee is not None else None
            if keyword is not None:
                method_arg = _method_id_argument(node, keyword)
                if isinstance(method_arg, ast.Constant) and isinstance(method_arg.value, str):
                    offenders.append(
                        f"{location}:{node.lineno}: {callee}() method argument is the "
                        f"string literal {method_arg.value!r}, not an RPCMethod member"
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
    """The lint must catch a pasted RPCMethod value plus every inline-string
    bypass: positional and keyword ``rpc_call`` arguments and a direct
    ``RPCMethod`` construction -- including IDs not (yet) in the enum."""
    method_values = _rpc_method_values()
    planted_known_id = next(iter(method_values))

    fixture = "\n".join(
        [
            f'LEAKED = "{planted_known_id}"',  # pasted known RPCMethod value
            'await self._rpc.rpc_call("InVnTd1", params)',  # inline positional (unknown) ID
            'await self._rpc.rpc_call(method="InVnTd2", params=p)',  # inline keyword ID
            'RPCMethod("InVnTd3")',  # direct constructor, positional
            'RPCMethod(value="InVnTd4")',  # direct constructor, keyword
        ]
    )
    tree = ast.parse(fixture)

    offenders = _hardcoded_method_id_offenders(tree, method_values, "<fixture>")

    assert any(planted_known_id in offender for offender in offenders), offenders
    for fresh_id in ("InVnTd1", "InVnTd2", "InVnTd3", "InVnTd4"):
        assert any(fresh_id in offender for offender in offenders), (fresh_id, offenders)


def test_lint_ignores_rpcmethod_member_dispatch() -> None:
    """Correct dispatch -- a member access positionally or by keyword, and a
    dynamic (non-literal) RPCMethod construction -- must not be flagged."""
    method_values = _rpc_method_values()
    for source in (
        "await self._rpc.rpc_call(RPCMethod.LIST_NOTEBOOKS, params)",
        "await self._rpc.rpc_call(method=RPCMethod.LIST_NOTEBOOKS, params=p)",
        "RPCMethod(resolved_id)",  # dynamic arg -- legitimate, not a literal
    ):
        tree = ast.parse(source)
        assert _hardcoded_method_id_offenders(tree, method_values, "<fixture>") == [], source
