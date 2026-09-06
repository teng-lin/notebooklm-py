"""Bidirectional gate keeping the v1 runway live until the 1.0 release cut."""

from __future__ import annotations

import re
from pathlib import Path

from ._v100_breaks import V100_BREAKING_CHANGES

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"
_FLIP_VERSION = (1, 0, 0)
_MACHINERY = {
    "def normalize_legacy_client_options": "flat client option compatibility normalizer",
    'warn_registered_deprecation("client_rpc_call_web")': "Web rpc_call warning",
    'warn_registered_deprecation("client_rpc_call_android")': "Android rpc_call warning",
    "class LazyWebSidecar": "Android Web-compatibility sidecar",
    "Awaiting NotebookLMClient.from_storage(...) is deprecated": "awaitable factory warning",
    "pre-profiles home-root layout": "pre-profiles storage warning",
    "NotebookMetadata.modified_at is deprecated": "metadata rename warning",
    'warn_registered_deprecation("mcp_confirmed_name_references")': (
        "confirmed MCP name-reference warning"
    ),
}


def _version_tuple(path: Path) -> tuple[int, int, int]:
    match = re.search(r'(?m)^version\s*=\s*"(\d+)\.(\d+)\.(\d+)', path.read_text())
    assert match is not None
    return tuple(map(int, match.groups()))  # type: ignore[return-value]


def _source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in SRC_ROOT.rglob("*.py"))


def _orphans(version: tuple[int, int, int], source: str, has_breaks: bool) -> list[str]:
    counts = {marker: source.count(marker) for marker in _MACHINERY}
    problems: list[str] = []
    if version < _FLIP_VERSION:
        missing = [marker for marker, count in counts.items() if count != 1]
        if missing:
            problems.append(f"v1 runway markers must occur exactly once: {missing}")
        if not has_breaks:
            problems.append("V100_BREAKING_CHANGES must stay populated before v1")
    else:
        present = [marker for marker, count in counts.items() if count]
        if present:
            problems.append(f"v1 runway machinery survived the release cut: {present}")
        if has_breaks:
            problems.append("V100_BREAKING_CHANGES must be drained at v1")
    return problems


def test_no_orphaned_v100_breaks_at_release() -> None:
    problems = _orphans(
        _version_tuple(PROJECT_ROOT / "pyproject.toml"),
        _source(),
        bool(V100_BREAKING_CHANGES),
    )
    assert not problems, problems


def test_v100_release_detector_bites_both_sides() -> None:
    live = "\n".join(_MACHINERY)
    assert not _orphans((0, 9, 0), live, True)
    assert _orphans((0, 9, 0), "", True)
    assert not _orphans((1, 0, 0), "", False)
    assert _orphans((1, 0, 0), live, False)
