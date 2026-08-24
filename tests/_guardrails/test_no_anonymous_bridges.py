"""Fail closed when a phase-scoped adapter lacks its planned removal phase."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
CURRENT_PHASE = 9

pytestmark = pytest.mark.repo_lint

_BRIDGE_MARKER = re.compile(
    r"\b(?:transitional|bridge|temporary)\b|\bcompatibility(?:\s+|-)projector\b",
    re.IGNORECASE,
)
_REMOVAL_PHASE = re.compile(r"\bRemoval:\s*P(?P<phase>\d+)\b")


def collect_anonymous_bridges(root: Path) -> set[str]:
    """Return modules advertising a temporary role without ``Removal: P<n>``."""
    anonymous: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstring = ast.get_docstring(tree, clean=False) or ""
        if _BRIDGE_MARKER.search(docstring) and not _REMOVAL_PHASE.search(docstring):
            anonymous.add(path.relative_to(root).as_posix())
    return anonymous


def collect_expired_bridges(root: Path, *, current_phase: int = CURRENT_PHASE) -> set[str]:
    """Return named bridges whose promised removal phase has already passed."""
    expired: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstring = ast.get_docstring(tree, clean=False) or ""
        if not _BRIDGE_MARKER.search(docstring):
            continue
        removal = _REMOVAL_PHASE.search(docstring)
        if removal is not None and current_phase > int(removal.group("phase")):
            expired.add(path.relative_to(root).as_posix())
    return expired


def test_temporary_modules_name_a_nonexpired_removal_phase() -> None:
    assert collect_anonymous_bridges(SRC_ROOT) == set()
    assert collect_expired_bridges(SRC_ROOT) == set()


def test_detector_distinguishes_named_bridges_from_ordinary_identifiers(tmp_path: Path) -> None:
    (tmp_path / "named.py").write_text(
        '"""Transitional compatibility projector.\n\nRemoval: P9\n"""\n',
        encoding="utf-8",
    )
    (tmp_path / "anonymous.py").write_text(
        '"""Temporary bridge for the old protocol."""\n',
        encoding="utf-8",
    )
    (tmp_path / "expired.py").write_text(
        '"""Compatibility projector.\n\nRemoval: P7\n"""\n',
        encoding="utf-8",
    )
    (tmp_path / "hyphenated.py").write_text(
        '"""Compatibility-projector for a legacy contract."""\n',
        encoding="utf-8",
    )
    (tmp_path / "ordinary.py").write_text(
        '"""Uses NamedTemporaryFile and bridge_to_future helpers."""\n',
        encoding="utf-8",
    )

    assert collect_anonymous_bridges(tmp_path) == {"anonymous.py", "hyphenated.py"}
    assert collect_expired_bridges(tmp_path) == {"expired.py"}
