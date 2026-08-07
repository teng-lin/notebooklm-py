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
