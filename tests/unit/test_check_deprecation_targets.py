"""Tests for ``scripts/check_deprecation_targets.py`` (issue #1214 part c).

The release gate fails if any ``warnings.warn`` / ``DeprecationWarning``
message under ``src/notebooklm/`` names the version currently in
``pyproject.toml`` as its *removal target*. A deprecation must never point at
the version shipping it.

Tests cover:

* The gate is GREEN against the live repository tree (the lapsed v0.6.0 shims
  were deleted by #1224, so ``LAPSED_ALLOWLIST`` is now empty and nothing
  trips the gate).
* Any allowlist entry is well-formed (cites a tracking issue and a version).
* A synthetic offender naming the current version is caught (rc 1).
* The ``removed in`` / ``will be removed in`` / ``scheduled for removal in``
  phrasings are all detected, with and without the ``v`` prefix.
* A deprecation naming a *different* version does not trip the gate.
* An allowlisted offender does not block; removing the offender makes the
  allowlist entry stale (rc 1).
* Missing / malformed ``pyproject.toml`` returns rc 2.

Script is imported via spec-loading to match the convention used by
``test_check_action_pinning.py`` (``scripts/`` is not a Python package).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
from textwrap import dedent

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_deprecation_targets.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_deprecation_targets", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script():
    return _load_module()


def _write_pyproject(tmp_path: Path, version: str) -> Path:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        dedent(
            f"""
            [project]
            name = "example"
            version = "{version}"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return pyproject


def _run(script, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = script.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Live-repo guard
# ---------------------------------------------------------------------------


def test_live_repository_passes_the_gate(script) -> None:
    """The real tree is green: lapsed shims are allowlisted; nothing else trips."""
    rc, out, err = _run(script, [])
    assert rc == 0, err
    assert "OK" in out


def test_lapsed_allowlist_entries_are_well_formed(script) -> None:
    """Any allowlist entry must cite a tracking issue and name a version.

    The allowlist is legitimately empty once the lapsed shims are deleted (the
    #1213 v0.6.0 entries were dropped when their tracking PR removed the
    shims). When future lapsed shims are added back, each entry must still be
    well-formed (positive int issue, non-empty version + reason, src path) —
    kept generic rather than pinning specific issue/version values.
    """
    for entry in script.LAPSED_ALLOWLIST:
        assert isinstance(entry.issue, int) and entry.issue > 0, entry.path
        assert isinstance(entry.version, str) and entry.version, entry.path
        assert isinstance(entry.reason, str) and entry.reason, entry.path
        assert entry.path.startswith("src/notebooklm/"), entry.path


# ---------------------------------------------------------------------------
# Synthetic-tree behaviour (monkeypatch SRC_ROOT/REPO_ROOT onto the module)
# ---------------------------------------------------------------------------


@pytest.fixture()
def synthetic(script, tmp_path, monkeypatch):
    """Point the script's scan at an isolated synthetic source tree."""
    src = tmp_path / "src" / "notebooklm"
    src.mkdir(parents=True)
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(script, "SRC_ROOT", src)
    # Neutralise the real allowlist so synthetic offenders are not masked.
    monkeypatch.setattr(script, "LAPSED_ALLOWLIST", ())
    monkeypatch.setattr(script, "_ALLOWLIST_BY_KEY", {})
    return src


@pytest.mark.parametrize(
    "phrase",
    [
        "will be removed in v0.7.0",
        "will be removed in 0.7.0",
        "removed in v0.7.0",
        "scheduled for removal in v0.7.0",
        "removal in 0.7.0",
    ],
)
def test_offender_naming_current_version_is_caught(script, synthetic, tmp_path, phrase) -> None:
    (synthetic / "_feature.py").write_text(
        dedent(
            f"""
            import warnings

            def f():
                warnings.warn(
                    "old_param is deprecated and {phrase}; use new_param.",
                    DeprecationWarning,
                )
            """
        ),
        encoding="utf-8",
    )
    pyproject = _write_pyproject(tmp_path, "0.7.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1, err
    assert "_feature.py" in err
    assert "removal target" in err


def test_deprecation_naming_other_version_does_not_trip(script, synthetic, tmp_path) -> None:
    (synthetic / "_feature.py").write_text(
        dedent(
            """
            import warnings

            def f():
                warnings.warn(
                    "old_param is deprecated and will be removed in v1.0.0.",
                    DeprecationWarning,
                )
            """
        ),
        encoding="utf-8",
    )
    pyproject = _write_pyproject(tmp_path, "0.7.0")
    rc, out, _err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 0, out
    assert "OK" in out


def test_keyword_message_argument_is_scanned(script, synthetic, tmp_path) -> None:
    """A ``warnings.warn(message=...)`` keyword form must not bypass the gate."""
    (synthetic / "_feature.py").write_text(
        dedent(
            """
            import warnings

            def f():
                warnings.warn(
                    message="old_param will be removed in v0.7.0.",
                    category=DeprecationWarning,
                )
            """
        ),
        encoding="utf-8",
    )
    pyproject = _write_pyproject(tmp_path, "0.7.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1, err
    assert "_feature.py" in err


def test_direct_deprecationwarning_construction_is_scanned(script, synthetic, tmp_path) -> None:
    (synthetic / "_feature.py").write_text(
        dedent(
            """
            def f():
                raise DeprecationWarning(
                    "feature X scheduled for removal in v0.7.0."
                )
            """
        ),
        encoding="utf-8",
    )
    pyproject = _write_pyproject(tmp_path, "0.7.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1, err
    assert "_feature.py" in err


def test_allowlisted_offender_does_not_block(script, synthetic, tmp_path, monkeypatch) -> None:
    (synthetic / "_legacy.py").write_text(
        dedent(
            """
            import warnings

            def f():
                warnings.warn(
                    "legacy will be removed in v0.7.0.",
                    DeprecationWarning,
                )
            """
        ),
        encoding="utf-8",
    )
    entry = script._LapsedEntry("src/notebooklm/_legacy.py", "0.7.0", 9999, "tracked elsewhere")
    monkeypatch.setattr(script, "LAPSED_ALLOWLIST", (entry,))
    monkeypatch.setattr(script, "_ALLOWLIST_BY_KEY", {entry.key: entry})
    pyproject = _write_pyproject(tmp_path, "0.7.0")
    rc, out, _err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 0, out
    assert "allowlisted" in out


def test_stale_allowlist_entry_is_reported(script, synthetic, tmp_path, monkeypatch) -> None:
    # No source file names v0.7.0, but an allowlist entry claims one does.
    (synthetic / "_clean.py").write_text("x = 1\n", encoding="utf-8")
    entry = script._LapsedEntry("src/notebooklm/_legacy.py", "0.7.0", 9999, "gone")
    monkeypatch.setattr(script, "LAPSED_ALLOWLIST", (entry,))
    monkeypatch.setattr(script, "_ALLOWLIST_BY_KEY", {entry.key: entry})
    pyproject = _write_pyproject(tmp_path, "0.7.0")
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 1, err
    assert "Stale" in err


# ---------------------------------------------------------------------------
# Argument / parse errors
# ---------------------------------------------------------------------------


def test_missing_pyproject_returns_rc_2(script, tmp_path) -> None:
    rc, _out, err = _run(script, ["--pyproject", str(tmp_path / "nope.toml")])
    assert rc == 2
    assert "not found" in err


def test_malformed_pyproject_returns_rc_2(script, tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n', encoding="utf-8")  # no version
    rc, _out, err = _run(script, ["--pyproject", str(pyproject)])
    assert rc == 2
    assert "project.version" in err
