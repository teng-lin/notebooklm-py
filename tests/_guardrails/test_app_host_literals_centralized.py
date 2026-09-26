"""Keep current and legacy personal/enterprise app hosts centralized in ``_env``.

Executable code must import the role constants or host sets instead of copying
host literals. Independent accept sets caused the rebrand failures tracked in
#2019, #2038, and #2067; enterprise aliases need the same protection.

Docstrings, comments, and tests may name concrete hosts. Test expectations must
remain independent of the production constants they verify. Constant values are
read from ``_env.py`` via AST, without importing the application.

``KNOWN_BARE_LITERALS`` is a deletion-only allowance for historical exceptions.
It is empty today: adding new literals elsewhere is not permitted.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"
ENV_MODULE = SRC_ROOT / "_env.py"

# The constants this lint protects, by *role* rather than by value: current
# and legacy hosts for each app family belong in ``_env.py``.
# Read by name so the values can swap again (as #2067 swapped them) without
# touching this file.
HOST_CONSTANT_NAMES = (
    "PERSONAL_BASE_HOST",
    "PERSONAL_LEGACY_HOST",
    "ENTERPRISE_BASE_HOST",
    "ENTERPRISE_LEGACY_HOST",
)

# Pre-existing bare literals, allowlisted so this ratchet lands without
# cross-editing files another PR owns. **Empty, and it should stay that way** --
# the last entry (``_browser/browser_capture.py``, which had hardcoded the alias in
# ``url_matches_base_host`` since #2015) was folded onto ``PERSONAL_APP_HOSTS``
# when ``accepted_login_hosts`` learned to accept both personal hosts.
# Entries may be deleted, never added.
KNOWN_BARE_LITERALS: frozenset[str] = frozenset()


def _declared_host(constant: str, env_module: Path = ENV_MODULE) -> str:
    """Return the value of ``constant`` as declared in ``_env.py``.

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
        if any(isinstance(t, ast.Name) and t.id == constant for t in targets):
            return node.value.value
    raise AssertionError(f"{constant} not found in {env_module}")


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


@pytest.mark.parametrize("constant", HOST_CONSTANT_NAMES)
def test_env_module_declares_each_host(constant: str):
    """Non-empty vocabulary, else the lint below would silently pass."""
    assert _declared_host(constant)


def test_the_two_hosts_are_distinct():
    """Guards the failure mode #2067 nearly shipped.

    A naive value-swap leaves both constants holding the same host. Every
    accept-set built from ``PERSONAL_APP_HOSTS`` then silently halves, and the
    lint below would still pass -- it would just police one host twice.
    """
    assert _declared_host("PERSONAL_BASE_HOST") != _declared_host("PERSONAL_LEGACY_HOST")


@pytest.mark.parametrize("constant", HOST_CONSTANT_NAMES)
def test_no_bare_host_literals_outside_env(constant: str):
    """No module may hardcode either host in code; import it from ``_env``."""
    host = _declared_host(constant)
    violations: list[str] = []

    for module in sorted(SRC_ROOT.rglob("*.py")):
        if module == ENV_MODULE:
            continue
        relative = module.relative_to(SRC_ROOT).as_posix()
        if relative in KNOWN_BARE_LITERALS:
            continue
        for lineno, value in _code_string_constants(module):
            if host in value:
                violations.append(f"{relative}:{lineno} hardcodes {host!r}")

    assert not violations, (
        f"Import {constant} from notebooklm._env instead of inlining "
        f"{host!r} (two copies of a domain fact is what produced #2019):\n  "
        + "\n  ".join(violations)
    )


def test_allowlist_entries_are_still_real():
    """A stale allowlist entry means the fold already happened -- delete it.

    Without this, an entry could outlive the literal it excuses and silently
    re-permit a bare host literal in that file.
    """
    hosts = [_declared_host(name) for name in HOST_CONSTANT_NAMES]
    stale: list[str] = []
    for relative in sorted(KNOWN_BARE_LITERALS):
        module = SRC_ROOT / relative
        if not module.exists():
            stale.append(f"{relative} (file is gone)")
            continue
        values = [value for _, value in _code_string_constants(module)]
        if not any(host in value for host in hosts for value in values):
            stale.append(f"{relative} no longer hardcodes either personal host")

    assert not stale, "Remove these now-unnecessary KNOWN_BARE_LITERALS entries:\n  " + "\n  ".join(
        stale
    )
