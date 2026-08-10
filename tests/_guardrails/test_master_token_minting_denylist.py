"""Guard against the CLI re-assembling the master-token minting primitives.

#2103's structural follow-up (PR-2) moved the master-token TRANSACTION
(bootstrap / re-mint / ownership guard) out of the CLI and into
``_auth/master_token.py``: ``cli/`` now calls the coarse, audited ops
(``master_token_bootstrap`` / ``master_token_remint`` /
``bootstrap_missing_storage_from_master_token`` / ``assert_account_writable``)
via the ``notebooklm.auth`` facade instead of assembling
``exchange_master_token`` + ``mint_cookies`` + ``persist_minted_jar`` +
``write_master_token`` itself.
Those four primitives are de-blessed (``_AUTH_DEBLESSED_KEEP_IMPORTABLE`` in
``tests/_guardrails/test_public_surface.py``) but stay importable from
``notebooklm.auth`` for the documented low-level recipe and any external
caller — the ``cli/`` boundary lint (``test_cli_boundary.py``) does not (and
should not) forbid ``notebooklm.auth`` itself, since many ``cli/`` modules
legitimately reach the facade for other names.

This lint closes the gap those two facts leave open: nothing currently stops
a *future* ``cli/`` change from reaching back into one of the four minting
primitives BY NAME through the facade that is still open to it, silently
undoing PR-2's "the CLI invokes whole audited transactions" invariant.

``generate_android_id`` is intentionally NOT included: it never touches
credentials or storage (a pure random-hex generator), so a direct CLI call
to it carries none of the minting-primitive risk the other four do.

Two decidable shapes, following the established pattern in
``test_master_token_path_single_site.py`` (#2103 PR-1) and clause (v) of
``test_storage_writer_boundary.py``:

(a) A NAME import of a forbidden primitive from the ``notebooklm.auth``
    facade, any depth/spelling: ``from notebooklm.auth import mint_cookies``,
    ``from ...auth import persist_minted_jar as pmj``. (``from notebooklm
    import auth`` followed by ``auth.write_master_token`` is NOT this shape —
    the primitive name never appears in an import statement there — that's
    shape (b) below.)

(b) A MODULE-ALIAS or PACKAGE-ROOT attribute-access call: ``from .. import
    auth [as auth_helpers]`` (the exact shape ``cli/helpers.py`` and
    ``cli/_cookie_import.py`` already establish for other names), followed by
    ``auth_helpers.mint_cookies(...)`` — OR a bare ``import notebooklm``
    (with or without ``as``, with or without an explicit ``.auth`` suffix),
    followed by ``<root>.auth.mint_cookies(...)``: importing any submodule
    of ``notebooklm`` makes ``.auth`` reachable as an attribute of the
    already-loaded ``notebooklm`` package object, regardless of which name
    triggered the import (``notebooklm/__init__.py`` itself imports
    ``.auth``). An import-only lint (shape (a) alone) is blind to both of
    these — the primitive NAME never appears in an ``import`` statement.

    Not covered by either shape: ``from notebooklm.auth import *`` followed
    by a bare-name call (`ruff`'s pyflakes F403/F405 already fails CI on the
    wildcard import itself, so this is backstopped elsewhere, not a gap in
    practice); ``getattr(auth, "mint_cookies")(...)`` and other
    string-keyed/dynamic access (no static AST lint can chase this; out of
    scope for an accidental-reintroduction guard).
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI_ROOT = PROJECT_ROOT / "src" / "notebooklm" / "cli"

FORBIDDEN_MINTING_PRIMITIVES: frozenset[str] = frozenset(
    {
        "exchange_master_token",
        "mint_cookies",
        "persist_minted_jar",
        "write_master_token",
    }
)


def _is_auth_facade_module(module: str | None, level: int) -> bool:
    """Does this ``ImportFrom``'s ``(module, level)`` resolve to the top-level
    ``notebooklm.auth`` facade?

    Absolute (``level == 0``): ``module == "notebooklm.auth"``.
    Relative (``level >= 1``): whatever remains in ``module`` after popping
    ``level`` parent packages is ``"auth"`` — every ``cli/`` submodule that
    reaches the facade today does so as exactly ``auth`` (never a deeper
    ``auth.something``), and no ``auth`` submodule exists anywhere under
    ``cli/`` itself for this to false-positive against.
    """
    mod = module or ""
    if level == 0:
        return mod == "notebooklm.auth"
    return mod == "auth"


def _forbidden_name_imports(tree: ast.AST) -> list[str]:
    """Shape (a): a NAME import of a forbidden primitive from the facade."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not _is_auth_facade_module(node.module, node.level):
            continue
        hits.extend(
            alias.name for alias in node.names if alias.name in FORBIDDEN_MINTING_PRIMITIVES
        )
    return hits


def _auth_module_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Local bindings that make the ``notebooklm.auth`` MODULE object (as
    opposed to a name imported FROM it) reachable.

    Returns ``(direct_aliases, pkg_roots)``:

    * ``direct_aliases`` — names that ARE the ``auth`` module itself
      (``from .. import auth [as X]``, ``import notebooklm.auth as X``),
      reached as ``X.<primitive>``.
    * ``pkg_roots`` — names that ARE the ``notebooklm`` PACKAGE object
      (``import notebooklm``, ``import notebooklm as nb``, or a plain
      ``import notebooklm.auth`` with no ``as`` — Python binds the name
      ``notebooklm``, not ``notebooklm.auth``, for that last form), from
      which ``<root>.auth.<primitive>`` is reachable at runtime: importing
      ANY submodule of ``notebooklm`` (which every one of these shapes
      does, directly or by loading ``notebooklm/__init__.py``, which itself
      imports ``.auth``) makes ``.auth`` an attribute of the already-loaded
      ``notebooklm`` package object, regardless of which name in THIS file
      triggered the import. (PR-3 review round 2, pr3-reviewer-code: a bare
      ``import notebooklm`` — no explicit ``.auth`` at all — was still a
      live, empirically-verified bypass of the round-1 fix below, which
      only recognized the explicit ``import notebooklm.auth`` spelling.)"""
    direct: set[str] = set()
    pkg_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level >= 1 and not node.module:
            # ``from <dots> import auth [as X]`` — the imported NAME is "auth".
            direct.update(
                alias.asname or alias.name for alias in node.names if alias.name == "auth"
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "notebooklm":
            direct.update(
                alias.asname or alias.name for alias in node.names if alias.name == "auth"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "notebooklm.auth" and alias.asname:
                    direct.add(alias.asname)
                elif alias.name in ("notebooklm", "notebooklm.auth"):
                    # Either spelling binds the PACKAGE name locally when no
                    # ``as`` is given (``import notebooklm.auth`` binds
                    # ``notebooklm``, per Python's import semantics); ``as``
                    # renames that same package binding.
                    pkg_roots.add(alias.asname or "notebooklm")
    return direct, pkg_roots


def _forbidden_attribute_accesses(tree: ast.AST) -> list[str]:
    """Shape (b): ``<auth_alias>.<forbidden_primitive>`` attribute access,
    reached via a module-alias binding (shape (a) never sees the primitive
    NAME appear in an import statement at all) — including the two-level
    ``<pkg_root>.auth.<primitive>`` chain reachable from any binding of the
    ``notebooklm`` package object itself (see :func:`_auth_module_aliases`)."""
    direct, pkg_roots = _auth_module_aliases(tree)
    if not direct and not pkg_roots:
        return []
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in FORBIDDEN_MINTING_PRIMITIVES:
            continue
        base = node.value
        # Direct alias: ``auth_helpers.mint_cookies(...)`` / ``auth.mint_cookies(...)``.
        if isinstance(base, ast.Name) and base.id in direct:
            hits.append(node.attr)
            continue
        # Package-root chain: ``<pkg_root>.auth.mint_cookies(...)``.
        if (
            isinstance(base, ast.Attribute)
            and base.attr == "auth"
            and isinstance(base.value, ast.Name)
            and base.value.id in pkg_roots
        ):
            hits.append(node.attr)
    return hits


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = _forbidden_name_imports(tree) + _forbidden_attribute_accesses(tree)
    return sorted(set(names))


def test_no_cli_module_imports_minting_primitives() -> None:
    """No module under ``cli/`` may import or attribute-access any of the
    four minting primitives from the ``notebooklm.auth`` facade — by name,
    or via a module-alias attribute call."""
    violations: dict[str, list[str]] = {}
    for module in sorted(CLI_ROOT.rglob("*.py")):
        found = _violations(module)
        if found:
            violations[str(module.relative_to(PROJECT_ROOT))] = found

    assert not violations, (
        "cli/ must call the audited coarse adapters (master_token_bootstrap / "
        "master_token_remint / bootstrap_missing_storage_from_master_token / "
        "assert_account_writable), whose coordinator owns minting composition, "
        "instead of assembling minting primitives "
        "itself (#2103 PR-2/PR-3):\n"
        + "\n".join(f"  {path}: {names}" for path, names in violations.items())
    )


def test_forbidden_minting_primitives_still_resolve_on_the_facade() -> None:
    """Non-empty vocabulary, AND every entry must still be a real, resolvable
    attribute of ``notebooklm.auth`` (PR-3 review round 2, pr3-reviewer-code:
    a bare literal-equality check against a duplicated set two lines away is
    a tautology that can never fail — it doesn't catch the failure this lint
    is actually exposed to, which is a primitive getting renamed/removed out
    from under the denylist, silently leaving it guarding four dead strings
    forever while the real name goes unguarded).

    (Not cross-checked against ``_AUTH_DEBLESSED_KEEP_IMPORTABLE`` in
    ``test_public_surface.py`` — ``tests/_guardrails/test_no_cross_test_imports.py``
    forbids one ``test_*`` module importing from another; keep this list and
    that one in sync by eye when either changes.)"""
    import notebooklm.auth as auth_module

    assert FORBIDDEN_MINTING_PRIMITIVES, "the denylist must not be empty"
    sentinel = object()
    dead = [
        name
        for name in FORBIDDEN_MINTING_PRIMITIVES
        if getattr(auth_module, name, sentinel) is sentinel
    ]
    assert not dead, f"denylisted names no longer resolve on notebooklm.auth: {sorted(dead)}"


# --- self-tests: detector shapes ---------------------------------------


def _hits(snippet: str) -> list[str]:
    tree = ast.parse(snippet)
    return _forbidden_name_imports(tree) + _forbidden_attribute_accesses(tree)


def test_detects_direct_name_import_absolute() -> None:
    assert _hits("from notebooklm.auth import mint_cookies") == ["mint_cookies"]


def test_detects_direct_name_import_relative_any_depth() -> None:
    assert _hits("from ..auth import persist_minted_jar") == ["persist_minted_jar"]
    assert _hits("from ...auth import write_master_token") == ["write_master_token"]
    assert _hits("from ....auth import exchange_master_token") == ["exchange_master_token"]


def test_detects_aliased_name_import() -> None:
    assert _hits("from ..auth import mint_cookies as mc") == ["mint_cookies"]


def test_detects_module_alias_attribute_call() -> None:
    assert _hits("from .. import auth as auth_helpers\nauth_helpers.mint_cookies(a, b, c)") == [
        "mint_cookies"
    ]
    assert _hits("from .. import auth\nauth.write_master_token(p, email=e)") == [
        "write_master_token"
    ]


def test_detects_package_qualified_attribute_call() -> None:
    """A plain, non-aliased ``import notebooklm.auth`` followed by the fully
    dotted ``notebooklm.auth.<primitive>`` attribute chain — the one shape a
    naive alias-only detector misses (verified missing in review by testing
    this exact snippet, then fixed alongside this test)."""
    assert _hits("import notebooklm.auth\nnotebooklm.auth.mint_cookies(a, b, c)") == [
        "mint_cookies"
    ]


def test_detects_bare_package_import_attribute_chain() -> None:
    """A bare ``import notebooklm`` — no explicit ``.auth`` at all — followed
    by the ``notebooklm.auth.<primitive>`` chain. This is a DIFFERENT, wider
    gap than the one above: it doesn't even mention ``auth`` in the import
    statement, yet is empirically runtime-reachable (importing any
    ``notebooklm`` submodule loads ``notebooklm/__init__.py``, which itself
    imports ``.auth``, making it an attribute of the already-loaded
    ``notebooklm`` package object). Found live in PR-3 review round 2
    (pr3-reviewer-code) after round 1 only fixed the explicit
    ``import notebooklm.auth`` spelling above; verified missing, then fixed,
    then this test added."""
    assert _hits("import notebooklm\nnotebooklm.auth.mint_cookies(a, b, c)") == ["mint_cookies"]


def test_detects_aliased_package_import_attribute_chain() -> None:
    assert _hits("import notebooklm as nb\nnb.auth.persist_minted_jar(a, b)") == [
        "persist_minted_jar"
    ]


def test_detects_indirection_through_a_local_variable() -> None:
    """``fn = auth.mint_cookies`` is itself an ``Attribute`` node — caught even
    though the eventual call site (``fn(...)``) is a bare ``Name``, not an
    ``Attribute``."""
    assert _hits("from .. import auth\nfn = auth.mint_cookies\nfn(a, b, c)") == ["mint_cookies"]


def test_ignores_unforbidden_auth_names() -> None:
    """The facade is still fine to import for every OTHER name."""
    assert _hits("from ..auth import MasterTokenError, read_master_token") == []
    assert _hits("from .. import auth\nauth.read_master_token(p)") == []
    assert _hits("from .. import auth\nauth.master_token_bootstrap(**kwargs)") == []


def test_ignores_unrelated_module_alias() -> None:
    """A module aliased to the name ``auth`` that is NOT the notebooklm.auth
    facade must not be flagged (nothing does this today, but the detector
    should not false-positive on an unrelated import)."""
    assert _hits("import some_other_package as auth\nauth.mint_cookies(1, 2, 3)") == []
