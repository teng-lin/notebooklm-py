"""Guardrail: no test package dir may shadow/conflict with another module.

In pytest's default `prepend` import mode a packaged test dir becomes importable
under its own basename. If that basename resolves to ANY filesystem location
other than the test dir itself, it shadows that module and breaks
``import <name>`` everywhere. This catches two distinct collisions:

* an out-of-repo installed distribution (e.g. the ``mcp`` SDK in site-packages);
* the project's OWN editable-installed package — a test dir named ``notebooklm``
  would resolve to ``src/notebooklm/__init__.py`` (inside the repo), which the
  old ``"site-packages" in spec.origin`` predicate silently missed.

It also covers namespace packages (``spec.origin is None`` but
``submodule_search_locations`` is populated) and built-in/frozen modules.

See docs/plans/2026-06-08-mcp-server-redesign-design.md section 6.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]

# ``ModuleSpec.origin`` sentinels that do NOT name a real filesystem path.
_NON_PATH_ORIGINS = frozenset({"namespace", "built-in", "frozen"})


def _shadow_offender(pkg_dir: Path) -> str | None:
    """Return a description if ``pkg_dir`` shadows another module, else ``None``.

    A candidate top-level test package dir named ``pkg_dir.name`` shadows or
    conflicts iff ``find_spec(name)`` resolves to any filesystem location OTHER
    than ``pkg_dir`` itself. A name that does not resolve at all is free.
    """

    name = pkg_dir.name
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError, AttributeError):
        spec = None
    if spec is None:
        return None  # name is free

    resolved: list[Path] = []
    if spec.origin and spec.origin not in _NON_PATH_ORIGINS:
        resolved.append(Path(spec.origin).resolve().parent)
    for loc in spec.submodule_search_locations or []:
        resolved.append(Path(loc).resolve())

    # ``name`` must resolve ONLY to this test dir; anything else is a collision.
    this_dir = pkg_dir.resolve()
    if not resolved or any(p != this_dir for p in resolved):
        target = spec.origin or spec.submodule_search_locations
        return f"{pkg_dir} shadows another module '{name}' -> {target}"
    return None


def _top_level_test_pkg_dirs() -> list[Path]:
    """Test package dirs whose basename becomes importable as a top-level name."""

    dirs: list[Path] = []
    for init in TESTS_ROOT.rglob("__init__.py"):
        pkg_dir = init.parent
        # Only dirs whose parent is NOT itself a package can become top-level.
        if (pkg_dir.parent / "__init__.py").exists():
            continue
        dirs.append(pkg_dir)
    return dirs


def test_no_test_package_dir_shadows_installed_package() -> None:
    offenders = [
        offender
        for pkg_dir in _top_level_test_pkg_dirs()
        if (offender := _shadow_offender(pkg_dir)) is not None
    ]
    assert not offenders, "Test dirs shadow other modules:\n" + "\n".join(offenders)


# --- Self-tests for the guardrail's own predicate -----------------------------


def test_predicate_does_not_flag_the_real_tests_dir() -> None:
    """The actual ``tests/`` package resolves to itself -> not an offender."""

    assert _shadow_offender(TESTS_ROOT) is None


def test_predicate_flags_name_resolving_to_an_installed_package(tmp_path: Path) -> None:
    """A fabricated dir named after a stdlib package (``json``) is flagged."""

    fake = tmp_path / "json"
    fake.mkdir()
    (fake / "__init__.py").write_text("", encoding="utf-8")

    offender = _shadow_offender(fake)
    assert offender is not None
    assert "shadows another module 'json'" in offender


def test_predicate_flags_name_resolving_to_in_repo_package(tmp_path: Path) -> None:
    """A fabricated dir named ``notebooklm`` collides with the editable install.

    This is the in-repo collision the old ``site-packages`` predicate missed:
    ``notebooklm`` resolves to ``src/notebooklm`` (inside the repo, NOT in
    site-packages), yet it still shadows the real package.
    """

    fake = tmp_path / "notebooklm"
    fake.mkdir()
    (fake / "__init__.py").write_text("", encoding="utf-8")

    offender = _shadow_offender(fake)
    assert offender is not None
    assert "shadows another module 'notebooklm'" in offender


def test_predicate_does_not_flag_a_free_name(tmp_path: Path) -> None:
    """A dir whose name resolves to nothing importable is free."""

    fake = tmp_path / "definitely_not_an_installed_package_xyzzy_42"
    fake.mkdir()
    (fake / "__init__.py").write_text("", encoding="utf-8")

    assert _shadow_offender(fake) is None
