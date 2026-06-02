"""Meta-lint: no inline ``DeprecationWarning`` outside ``_deprecation.py``.

ADR-018 (``docs/adr/0018-deprecation-strategy.md``) requires that **every**
deprecation warning be gated behind ``NOTEBOOKLM_QUIET_DEPRECATIONS`` and that
the mechanics live in a single module, ``src/notebooklm/_deprecation.py``. It
explicitly rejects "per-feature ``warnings.warn(...)`` calls" as "exactly the
fragmentation this ADR prevents."

This lint enforces that rule structurally: it walks **every** module under
``src/notebooklm/`` via the AST (so a docstring that merely mentions the word
``DeprecationWarning`` does NOT count — only real ``warnings.warn(...,
DeprecationWarning)`` *calls* do) and fails if any such call appears outside
``_deprecation.py``.

Why a lint and not vigilance: issue #1369 found four inline
``warnings.warn(..., DeprecationWarning)`` sites
(``client.py`` ``__await__``, ``_auth/storage.py`` ``save_cookies_to_storage``,
``_research.py`` ``poll(task_id=None)``, ``_notebooks.py`` ``NotebooksAPI.share()``)
that bypassed the suppression gate, so ``NOTEBOOKLM_QUIET_DEPRECATIONS=1`` did
**not** silence them. Tellingly, **3 of 4 independent ADR-compliance audit
passes reported ADR-018 "clean"** and missed this entire class — exactly the
kind of blind spot that human/agent review keeps missing. The durable fix is a
gate on the right dimension (call shape), not another round of vigilance.

Modelled after the AST-based lints in ``tests/_lint/`` (e.g.
``test_no_core_imports.py`` / ``test_asyncio_loop_affinity_guard.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"

# The single sanctioned home for the ``DeprecationWarning`` family. Every gated
# helper (``warn_deprecated`` / ``warn_get_returns_none`` / ``deprecated_kwarg``
# / ``MappingCompatMixin``) emits its warning from here.
ALLOWED_FILE = SRC_ROOT / "_deprecation.py"


def _is_warnings_warn(func: ast.expr) -> bool:
    """Return ``True`` if ``func`` is a ``warnings.warn`` / ``warn`` callee.

    Matches both the attribute form ``warnings.warn(...)`` and the bare
    ``warn(...)`` produced by ``from warnings import warn``.
    """
    if isinstance(func, ast.Attribute):
        # ``warnings.warn`` (or any ``<x>.warn``; the category check below keeps
        # this from over-matching unrelated ``.warn`` methods).
        return func.attr == "warn"
    if isinstance(func, ast.Name):
        return func.id == "warn"
    return False


def _names_deprecation_warning(node: ast.Call) -> bool:
    """Return ``True`` if the call passes ``DeprecationWarning`` as its category.

    Covers the positional second argument (``warn(msg, DeprecationWarning)``),
    the ``category=`` keyword (``warn(msg, category=DeprecationWarning)``), and
    attribute spellings (``warnings.DeprecationWarning``). Any subclass spelled
    with ``DeprecationWarning`` in the name is matched too.
    """

    def _is_deprecation_ref(expr: ast.expr) -> bool:
        if isinstance(expr, ast.Name):
            return "DeprecationWarning" in expr.id
        if isinstance(expr, ast.Attribute):
            return "DeprecationWarning" in expr.attr
        return False

    # Positional category argument: warn(message, category, ...).
    if len(node.args) >= 2 and _is_deprecation_ref(node.args[1]):
        return True
    # Keyword: warn(message, category=DeprecationWarning).
    return any(kw.arg == "category" and _is_deprecation_ref(kw.value) for kw in node.keywords)


def _scan(path: Path) -> list[int]:
    """Return ``[lineno, …]`` of inline ``DeprecationWarning`` calls in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _is_warnings_warn(node.func)
            and _names_deprecation_warning(node)
        ):
            violations.append(node.lineno)
    return violations


def test_no_inline_deprecation_warnings_outside_deprecation_module() -> None:
    """No ``warnings.warn(..., DeprecationWarning)`` outside ``_deprecation.py``.

    Route new deprecations through ``notebooklm._deprecation`` (e.g.
    ``warn_deprecated(...)``) so they honor ``NOTEBOOKLM_QUIET_DEPRECATIONS``.
    """
    offenders: dict[str, list[int]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path == ALLOWED_FILE:
            continue
        linenos = _scan(path)
        if linenos:
            offenders[str(path.relative_to(REPO_ROOT))] = linenos

    assert offenders == {}, (
        "Inline DeprecationWarning(s) bypass the NOTEBOOKLM_QUIET_DEPRECATIONS "
        "gate (ADR-018). Route them through notebooklm._deprecation "
        f"(e.g. warn_deprecated): {offenders}"
    )


def test_lint_detects_the_offending_shape() -> None:
    """Self-check: the scanner flags a real ``warnings.warn(..., DeprecationWarning)``.

    Guards against the scanner silently degrading to a no-op (which would let
    the recurrence it exists to prevent slip through). Both the positional and
    the ``category=`` spellings must be detected.
    """
    positional = ast.parse('warnings.warn("x", DeprecationWarning, stacklevel=2)')
    keyword = ast.parse('warn("x", category=DeprecationWarning)')
    benign = ast.parse('warnings.warn("x", UserWarning)')

    def _hits(tree: ast.AST) -> int:
        return sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _is_warnings_warn(node.func)
            and _names_deprecation_warning(node)
        )

    assert _hits(positional) == 1
    assert _hits(keyword) == 1
    assert _hits(benign) == 0
