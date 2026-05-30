"""Tests for the cassette-guard CLI (``tests/scripts/check_cassettes_clean.py``).

Focused on :func:`_iter_cassettes` path resolution — in particular that the
illustrative-``examples/`` exemption is scoped to the cassette tree and does NOT
create a silent blind spot when ``--secrets-only`` scans an unrelated directory
such as ``tests/fixtures/`` (coderabbit review on #1266).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "tests" / "scripts" / "check_cassettes_clean.py"
_spec = importlib.util.spec_from_file_location("tests_check_cassettes_clean", _SCRIPT)
assert _spec and _spec.loader
ccc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ccc)


def test_default_cassette_scan_excludes_examples_subtree() -> None:
    """The default ``tests/cassettes`` scan still skips ``examples/`` fixtures."""
    found = ccc._iter_cassettes([], recursive=True)
    assert all("examples" not in p.parts for p in found)


def test_relative_cassette_path_still_excludes_examples() -> None:
    """A relative cassette-dir arg resolves to the cassette tree and keeps the
    exemption (guards the absolute-vs-relative comparison)."""
    found = ccc._iter_cassettes(["tests/cassettes"], recursive=True)
    assert all("examples" not in p.parts for p in found)


def test_non_cassette_dir_does_not_skip_examples(tmp_path: Path) -> None:
    """An ``examples/`` subtree / ``example_*`` file under an unrelated scan
    root IS scanned — the exemption is cassette-tree-only, so the secrets-only
    fixture scan has no blind spot."""
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "leak.json").write_text("{}", encoding="utf-8")
    (tmp_path / "example_thing.json").write_text("{}", encoding="utf-8")
    (tmp_path / "normal.json").write_text("{}", encoding="utf-8")

    found = ccc._iter_cassettes([str(tmp_path)], recursive=True, extensions=(".json",))
    names = {p.name for p in found}
    assert names == {"leak.json", "example_thing.json", "normal.json"}


def test_explicit_file_under_examples_is_always_scanned(tmp_path: Path) -> None:
    """An explicitly-named file is scanned even under an ``examples/`` dir."""
    ex = tmp_path / "examples"
    ex.mkdir()
    target = ex / "example_explicit.yaml"
    target.write_text("interactions: []", encoding="utf-8")
    found = ccc._iter_cassettes([str(target)])
    assert found == [target]
