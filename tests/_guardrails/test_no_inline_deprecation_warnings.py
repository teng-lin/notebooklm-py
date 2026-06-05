"""Meta-lint: no inline ``DeprecationWarning`` outside ``_deprecation.py``.

ADR-0018 (``docs/adr/0018-deprecation-strategy.md``) requires that **every**
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
**not** silence them. (Two of those sites were later removed in v0.8.0 / #1363 —
``poll(task_id=None)`` ambiguity now raises ``AmbiguousResearchTaskError`` and
``NotebooksAPI.share()`` is gone — but the lint still guards the remaining and
all future sites.) Tellingly, **3 of 4 independent ADR-compliance audit
passes reported ADR-0018 "clean"** and missed this entire class — exactly the
kind of blind spot that human/agent review keeps missing. The durable fix is a
gate on the right dimension (call shape), not another round of vigilance.

**Scope: ``DeprecationWarning`` only.** This lint governs the *deprecation*
category exclusively. Other warning categories — ``RuntimeWarning`` /
``UserWarning`` etc. — are NOT deprecations and are allowed to live inline at
their call site (ADR-0018 only governs deprecations). For example
``_auth/storage.py``'s ``save_cookies_to_storage`` race advisory is a permanent
back-compat shim, not a scheduled removal, so it is a ``RuntimeWarning`` emitted
inline and this lint correctly leaves it alone.

Modelled after the AST-based lints in ``tests/_guardrails/`` (e.g.
``test_no_core_imports.py`` / ``test_asyncio_loop_affinity_guard.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"

# The single sanctioned home for the ``DeprecationWarning`` family. The gated
# ``warn_deprecated`` helper emits its warning from here.
ALLOWED_FILE = SRC_ROOT / "_deprecation.py"


def _resolve_warn_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Resolve a module's import aliases for the ``warnings`` API.

    Returns ``(module_aliases, warn_aliases)`` where:

    * ``module_aliases`` are the names bound to the ``warnings`` *module*
      (``import warnings`` → ``{"warnings"}``; ``import warnings as w`` →
      ``{"w"}``). A call ``<alias>.warn(...)`` against any of these is a hit.
    * ``warn_aliases`` are the names bound directly to ``warnings.warn``
      (``from warnings import warn`` → ``{"warn"}``;
      ``from warnings import warn as deprecate`` → ``{"deprecate"}``). A bare
      call ``<alias>(...)`` against any of these is a hit.

    Resolving against the file's actual imports (rather than hard-coding the
    name ``warn``) closes the aliasing bypass: ``import warnings as w;
    w.warn(..., DeprecationWarning)`` and ``from warnings import warn as
    deprecate; deprecate(..., DeprecationWarning)`` are both caught.
    """
    module_aliases: set[str] = set()
    warn_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "warnings":
                    module_aliases.add(alias.asname or "warnings")
        elif isinstance(node, ast.ImportFrom) and node.module == "warnings":
            for alias in node.names:
                if alias.name == "warn":
                    warn_aliases.add(alias.asname or "warn")
    return module_aliases, warn_aliases


def _is_warn_call(func: ast.expr, module_aliases: set[str], warn_aliases: set[str]) -> bool:
    """Return ``True`` if ``func`` is a ``warnings.warn`` callee for this file.

    Matches the attribute form ``<module_alias>.warn(...)`` and the bare
    ``<warn_alias>(...)`` form, using the alias sets resolved from the file's
    imports by :func:`_resolve_warn_bindings`.
    """
    if isinstance(func, ast.Attribute):
        return (
            func.attr == "warn"
            and isinstance(func.value, ast.Name)
            and (func.value.id in module_aliases)
        )
    if isinstance(func, ast.Name):
        return func.id in warn_aliases
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
    module_aliases, warn_aliases = _resolve_warn_bindings(tree)
    violations: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _is_warn_call(node.func, module_aliases, warn_aliases)
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
        "gate (ADR-0018). Route them through notebooklm._deprecation "
        f"(e.g. warn_deprecated): {offenders}"
    )


def _hits_in_source(src: str) -> int:
    """Count scanner hits in a source snippet (drives the real scan logic).

    Resolves the snippet's own import aliases via :func:`_resolve_warn_bindings`
    and matches calls with :func:`_is_warn_call` + :func:`_names_deprecation_warning`,
    mirroring :func:`_scan` exactly so the self-check exercises the real path.
    """
    tree = ast.parse(src)
    module_aliases, warn_aliases = _resolve_warn_bindings(tree)
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_warn_call(node.func, module_aliases, warn_aliases)
        and _names_deprecation_warning(node)
    )


def test_lint_detects_the_offending_shape() -> None:
    """Self-check: the scanner flags a real ``warnings.warn(..., DeprecationWarning)``.

    Guards against the scanner silently degrading to a no-op (which would let
    the recurrence it exists to prevent slip through). Every callee form
    (attribute vs bare name, *including aliased imports*) crossed with every
    category form (positional vs ``category=``) must be detected, and a benign
    category must not match.
    """
    # Attribute form, plain and aliased module import.
    assert (
        _hits_in_source('import warnings\nwarnings.warn("x", DeprecationWarning, stacklevel=2)')
        == 1
    )
    assert _hits_in_source('import warnings as w\nw.warn("x", DeprecationWarning)') == 1
    assert _hits_in_source('import warnings\nwarnings.warn("x", category=DeprecationWarning)') == 1
    # Bare form, plain and aliased function import.
    assert _hits_in_source('from warnings import warn\nwarn("x", DeprecationWarning)') == 1
    assert _hits_in_source('from warnings import warn\nwarn("x", category=DeprecationWarning)') == 1
    assert (
        _hits_in_source(
            'from warnings import warn as deprecate\ndeprecate("x", DeprecationWarning)'
        )
        == 1
    )

    # A bare ``warn`` NOT imported from ``warnings`` must NOT match (the alias
    # resolution closes the previous over-broad ``func.id == "warn"`` match).
    assert _hits_in_source('from logging import warn\nwarn("x", DeprecationWarning)') == 0
    # An attribute ``.warn`` on something that is not the warnings module.
    assert _hits_in_source('logger.warn("x", DeprecationWarning)') == 0

    # Non-deprecation categories must NOT match: ADR-0018 governs deprecations
    # only, so an inline RuntimeWarning/UserWarning is legitimately allowed.
    assert _hits_in_source('import warnings\nwarnings.warn("x", UserWarning)') == 0
    assert _hits_in_source('import warnings\nwarnings.warn("x", RuntimeWarning, stacklevel=2)') == 0
