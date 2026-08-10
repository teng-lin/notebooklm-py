"""Every ``cookie_jar`` rebind goes through ``AuthTokens.replace_cookie_jar``.

ADR-0031 Stage 4. ``AuthTokens.cookies`` and ``AuthTokens.cookie_jar`` carry the
same information in two shapes. Assigning the jar directly leaves the map
describing the previous session, and ``AuthTokens`` is public API — so the stale
value is readable by callers with nothing to signal it. The mid-session refresh
did exactly that until this stage.

``replace_cookie_jar`` rebinds both. This gate keeps a future rebind from
reintroducing the divergence by assigning the field directly. It is scoped to
assignments onto an ``AuthTokens``-shaped object (``<something>.cookie_jar = ...``)
anywhere under ``src/``, excluding ``_auth/tokens.py`` itself, which legitimately
sets the field inside ``__post_init__`` and ``replace_cookie_jar``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

_SRC = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
#: The class's own module: it defines the field and the sanctioned rebind.
_OWNER = "_auth/tokens.py"


def _direct_assignments() -> list[str]:
    """Return ``file:line`` for every ``<obj>.cookie_jar = ...`` outside the owner."""
    hits: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel == _OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "cookie_jar":
                    # ``self.cookie_jar = ...`` inside an unrelated class (e.g. a
                    # transport wrapper owning its own jar) is not an AuthTokens
                    # rebind; those live outside _auth and are matched by name
                    # only, so keep the exclusion explicit rather than implicit.
                    hits.append(f"{rel}:{node.lineno}")
    return hits


#: Assignments that are NOT ``AuthTokens`` rebinds. SHRINK-ONLY.
_NON_AUTHTOKENS: frozenset[str] = frozenset()


def test_no_direct_cookie_jar_rebind() -> None:
    """A rebind must go through ``replace_cookie_jar`` so both views move."""
    found = set(_direct_assignments()) - _NON_AUTHTOKENS
    assert not found, (
        "Direct ``.cookie_jar = ...`` assignment(s) outside _auth/tokens.py:\n"
        + "\n".join(f"  {h}" for h in sorted(found))
        + "\n\nUse ``auth.replace_cookie_jar(jar)`` — assigning the field alone "
        "leaves the public ``auth.cookies`` describing the previous session.\n"
        "If the target is not an AuthTokens (a transport owning its own jar, "
        "say), add it to _NON_AUTHTOKENS with that reason."
    )


def test_the_sanctioned_rebind_exists_and_sets_both() -> None:
    """The method the gate points callers at must actually set both views.

    Without this the gate could keep redirecting people to a method that had
    silently lost half its job — the failure mode where a guardrail's name
    outlives what it verifies.
    """
    source = (_SRC / "_auth" / "tokens.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "replace_cookie_jar"
        ),
        None,
    )
    assert fn is not None, "AuthTokens.replace_cookie_jar disappeared"

    assigned = {
        t.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Attribute)
    }
    assert {"cookie_jar", "cookies"} <= assigned, (
        f"replace_cookie_jar must set BOTH views; it sets {sorted(assigned)}"
    )
