"""Guard that both personal app hosts live only in ``_env.py``.

Two independent notions of "which host is the app?" that disagree is exactly the
shape that produced #2019: a valid session was reported as expired because one
code path's idea of the app host did not match reality. #2038 gave the
post-rebrand host a single home; #2067 made it the *default* and gave the
pre-rebrand host one too (:data:`~notebooklm._env.PERSONAL_LEGACY_HOST`).

This lint keeps it that way: no module under ``src/notebooklm/`` may hardcode
either host in executable code. Import the constant from ``_env`` instead, so
the next move is one line rather than a grep.

**Why both hosts, now.** Until #2067 this lint policed only the rebrand host,
on the reasoning that the legacy host appeared in "dozens of legitimate places"
(cookie-domain tables, URL builders) and policing it would be an eager burndown.
That is no longer true: the flip folded the last of those onto constants, and
`_code_string_constants` measures **zero** bare literals for either host. Adding
the second host therefore costs nothing today -- no allowlist, no burndown --
while closing the asymmetry that would otherwise let the *now-non-default* host
be re-copied freely, which is the precise mistake #2019 was made of.

Scope stays narrow -- **only real code**. Docstrings and bare string
expressions are skipped: prose that *mentions* a host is documentation, not a
second source of truth. Test modules are out of scope on purpose; a test that
spells a host literally is asserting against a concrete value, which is the
point of the test.

**Passive ratchet, not a burndown.** ``KNOWN_BARE_LITERALS`` allowlists sites that
predate the constant. Entries should be deleted, never added -- adding one means
a new copy of a domain fact was introduced, which is the thing this lint exists
to prevent.

Both host values are read from ``_env.py`` via AST so this lint cannot drift
from the source of truth and pulls in no import side effects.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"
ENV_MODULE = SRC_ROOT / "_env.py"

# The constants this lint protects, by *role* rather than by value: whichever
# host is the default and whichever is the fallback both belong in ``_env.py``.
# Read by name so the values can swap again (as #2067 swapped them) without
# touching this file.
HOST_CONSTANT_NAMES = ("PERSONAL_BASE_HOST", "PERSONAL_LEGACY_HOST")

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
