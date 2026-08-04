"""Guard that the app's post-rebrand host alias lives only in ``_env.py``.

Two independent notions of "which host is the NotebookLM app?" that disagree is
exactly the shape that produced #2019: a valid session was reported as expired
because one code path's idea of the app host did not match reality. The fix for
#2038 added :data:`notebooklm._env.PERSONAL_APP_ALIAS_HOST` so the post-rebrand
alias ``notebook.google.com`` has a single home alongside ``PERSONAL_BASE_HOST``
and ``ENTERPRISE_BASE_HOST``.

This lint keeps it that way: no module under ``src/notebooklm/`` may hardcode the
alias in executable code. Import the constant from ``_env`` instead, so a future
rename is one line rather than a grep.

Scope is deliberately narrow -- **only the alias**, and **only real code**:

* The long-established hosts (``notebooklm.google.com`` and friends) appear in
  dozens of legitimate places: docstrings that explain behaviour, cookie-domain
  tables, URL builders. Policing those would be an eager burndown, which this
  repo explicitly does not do. The alias is the fact #2038 just centralised, so
  it is the one worth protecting from being re-copied.
* Docstrings and bare string expressions are skipped. Prose that *mentions* the
  alias (including the docstrings written for #2038) is documentation, not a
  second source of truth.

**Passive ratchet, not a burndown.** ``KNOWN_BARE_LITERALS`` allowlists sites that
predate the constant. Entries should be deleted, never added -- adding one means
a new copy of a domain fact was introduced, which is the thing this lint exists
to prevent.

The alias value is read from ``_env.py`` via AST so this lint cannot drift from
the source of truth and pulls in no import side effects.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"
ENV_MODULE = SRC_ROOT / "_env.py"

# The single constant this lint protects. Read by name so the value can change
# in ``_env.py`` without touching this file.
ALIAS_CONSTANT_NAME = "PERSONAL_APP_ALIAS_HOST"

# Pre-existing bare literals, allowlisted so this ratchet lands without
# cross-editing files another PR owns.
#
# * ``_auth/browser_capture.py`` -- ``url_matches_base_host`` has hardcoded the
#   alias since #2015. PR #2042 owned that file when #2038 landed, so folding it
#   onto ``PERSONAL_APP_ALIAS_HOST`` is left to whoever touches it next.
#   ``test_allowlist_entries_are_still_real`` fails once that happens, prompting
#   deletion of this entry.
KNOWN_BARE_LITERALS: frozenset[str] = frozenset({"_auth/browser_capture.py"})


def _alias_host(env_module: Path = ENV_MODULE) -> str:
    """Return the value of ``PERSONAL_APP_ALIAS_HOST`` declared in ``_env.py``.

    Handles both the bare ``NAME = "..."`` form and the annotated
    ``NAME: str = "..."`` one. The constant is unannotated today, but a future
    typed pass over ``_env.py`` would otherwise turn this lint into a confusing
    "constant not found" failure for a change that broke nothing.
    """
    tree = ast.parse(env_module.read_text(encoding="utf-8"), filename=str(env_module))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        if any(isinstance(t, ast.Name) and t.id == ALIAS_CONSTANT_NAME for t in targets):
            return node.value.value
    raise AssertionError(f"{ALIAS_CONSTANT_NAME} not found in {env_module}")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Return ``id()``s of Constant nodes that are docstrings / bare strings.

    Any string that is the whole of an expression statement is prose, not a
    value the code uses -- module, class and function docstrings all take this
    shape, as do free-floating explanatory strings.
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }


def _code_string_constants(module: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, value)`` for string constants used as *values*."""
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    prose = _docstring_nodes(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in prose
    ]


def test_env_module_declares_the_alias():
    """Non-empty vocabulary, else the lint below would silently pass."""
    assert _alias_host() == "notebook.google.com"


def test_no_bare_alias_literals_outside_env():
    """No module may hardcode the alias in code; import it from ``_env``."""
    alias = _alias_host()
    violations: list[str] = []

    for module in sorted(SRC_ROOT.rglob("*.py")):
        if module == ENV_MODULE:
            continue
        relative = module.relative_to(SRC_ROOT).as_posix()
        if relative in KNOWN_BARE_LITERALS:
            continue
        for lineno, value in _code_string_constants(module):
            if alias in value:
                violations.append(f"{relative}:{lineno} hardcodes {alias!r}")

    assert not violations, (
        f"Import {ALIAS_CONSTANT_NAME} from notebooklm._env instead of inlining "
        f"{alias!r} (two copies of a domain fact is what produced #2019):\n  "
        + "\n  ".join(violations)
    )


def test_allowlist_entries_are_still_real():
    """A stale allowlist entry means the fold already happened -- delete it.

    Without this, an entry could outlive the literal it excuses and silently
    re-permit a bare alias in that file.
    """
    alias = _alias_host()
    stale: list[str] = []
    for relative in sorted(KNOWN_BARE_LITERALS):
        module = SRC_ROOT / relative
        if not module.exists():
            stale.append(f"{relative} (file is gone)")
            continue
        if not any(alias in value for _, value in _code_string_constants(module)):
            stale.append(f"{relative} no longer hardcodes {alias!r}")

    assert not stale, "Remove these now-unnecessary KNOWN_BARE_LITERALS entries:\n  " + "\n  ".join(
        stale
    )
