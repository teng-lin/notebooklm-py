#!/usr/bin/env python3
"""Generate the test-suite taxonomy inventory and baseline allowlists."""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
INVENTORY_PATH = REPO_ROOT / "docs" / "test-suite-taxonomy-inventory.md"
ALLOW_NO_VCR_FILES_PATH = REPO_ROOT / "tests/_fixtures/integration_allow_no_vcr_files.txt"
ALLOW_NO_VCR_NODEIDS_PATH = REPO_ROOT / "tests/_fixtures/integration_allow_no_vcr_nodeids.txt"
VCR_ALLOW_NO_VCR_NODEIDS_PATH = (
    REPO_ROOT / "tests/_fixtures/integration_vcr_allow_no_vcr_nodeids.txt"
)

ALLOW_NO_VCR_RATIONALE = (
    "existing mock-only integration exception; migrate per test-suite taxonomy cleanup"
)
VCR_ALLOW_NO_VCR_RATIONALE = (
    "existing vcr/allow_no_vcr overlap; normalize per test-suite taxonomy cleanup"
)


@dataclass(frozen=True)
class ItemRecord:
    nodeid: str
    path: str
    markers: frozenset[str]
    has_use_cassette: bool = False


class CollectionRecorder:
    def __init__(self) -> None:
        self.items: list[object] = []

    def pytest_collection_modifyitems(self, config, items) -> None:
        self.items = list(items)


def _repo_relative(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def has_use_cassette_decorator(item: object) -> bool:
    """Detect ``@notebooklm_vcr.use_cassette(...)`` on a collected item."""
    func = getattr(item, "function", None)
    seen: set[int] = set()
    while func is not None and id(func) not in seen:
        seen.add(id(func))
        wrapper = getattr(func, "_self_wrapper", None)
        if wrapper is not None:
            owner = getattr(wrapper, "__self__", None)
            if owner is not None and type(owner).__name__ == "CassetteContextDecorator":
                return True
        func = getattr(func, "__wrapped__", None)
    return False


def collect_items() -> list[ItemRecord]:
    recorder = CollectionRecorder()
    args = ["--collect-only", "-q", "-o", "addopts=", str(TESTS_DIR)]
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = pytest.main(args, plugins=[recorder])
    if exit_code not in (0, pytest.ExitCode.NO_TESTS_COLLECTED):
        raise RuntimeError(
            "pytest collection failed while generating taxonomy inventory.\n"
            f"exit_code={exit_code}\n"
            f"stdout_tail={stdout.getvalue()[-1000:]}\n"
            f"stderr_tail={stderr.getvalue()[-1000:]}"
        )

    records: list[ItemRecord] = []
    for item in recorder.items:
        path = Path(item.path)
        markers = frozenset(marker.name for marker in item.iter_markers())
        records.append(
            ItemRecord(
                nodeid=item.nodeid,
                path=_repo_relative(path),
                markers=markers,
                has_use_cassette=has_use_cassette_decorator(item),
            )
        )
    return sorted(records, key=lambda record: record.nodeid)


def logical_module_key(path: str) -> str:
    stem = Path(path).stem
    if stem.startswith("test_"):
        stem = stem.removeprefix("test_")
    for suffix in ("_integration", "_mock", "_vcr", "_characterization"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def normalized_identity(record: ItemRecord, move_map: dict[str, str] | None = None) -> str:
    if move_map and record.nodeid in move_map:
        return move_map[record.nodeid]
    _, separator, suffix = record.nodeid.partition("::")
    if not separator:
        suffix = "<module>"
    return f"{logical_module_key(record.path)}::{suffix}"


def duplicate_normalized_identities(
    records: list[ItemRecord],
    move_map: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = defaultdict(list)
    for record in records:
        seen[normalized_identity(record, move_map)].append(record.nodeid)
    return {identity: nodeids for identity, nodeids in seen.items() if len(nodeids) > 1}


def _count(records: list[ItemRecord], marker: str) -> int:
    return sum(1 for record in records if marker in record.markers)


def _records_under(records: list[ItemRecord], prefix: str) -> list[ItemRecord]:
    return [record for record in records if record.path.startswith(prefix)]


def _sorted_paths(paths: set[str]) -> list[str]:
    return sorted(paths)


def _markdown_list(values: list[str], *, empty: str = "None.") -> str:
    if not values:
        return empty
    return "\n".join(f"- `{value}`" for value in values)


def _allowlist(entries: list[str], rationale: str) -> str:
    if not entries:
        return "# No entries.\n"
    return "\n".join(f"{entry} # {rationale}" for entry in sorted(entries)) + "\n"


def render_inventory(records: list[ItemRecord]) -> str:
    e2e = _records_under(records, "tests/e2e/")
    integration = _records_under(records, "tests/integration/")
    characterization_marker_files = _sorted_paths(
        {record.path for record in records if "characterization" in record.markers}
    )
    characterization_named_files = _sorted_paths(
        {_repo_relative(path) for path in TESTS_DIR.rglob("*characterization*.py")}
    )

    integration_vcr = [record for record in integration if "vcr" in record.markers]
    integration_allow_no_vcr = [
        record for record in integration if "allow_no_vcr" in record.markers
    ]
    integration_overlap = [
        record for record in integration if {"vcr", "allow_no_vcr"} <= record.markers
    ]
    cassette_only = [
        record for record in integration if record.has_use_cassette and "vcr" not in record.markers
    ]

    marker_counts = Counter(marker for record in records for marker in record.markers)

    lines = [
        "# Test Suite Taxonomy Inventory",
        "",
        "Generated by `scripts/test_taxonomy_inventory.py`.",
        "",
        "## Collection Totals",
        "",
        f"- All collected tests with `-o addopts=''`: {len(records)}",
        f"- E2E total: {len(e2e)}",
        f"- E2E marked `e2e`: {_count(e2e, 'e2e')}",
        f"- E2E not marked `e2e`: {len(e2e) - _count(e2e, 'e2e')}",
        f"- E2E marked `readonly`: {_count(e2e, 'readonly')}",
        (
            "- E2E marked both `e2e` and `readonly`: "
            f"{sum(1 for record in e2e if {'e2e', 'readonly'} <= record.markers)}"
        ),
        f"- Integration total: {len(integration)}",
        f"- Integration `vcr and not allow_no_vcr`: {len(integration_vcr) - len(integration_overlap)}",
        (
            "- Integration `allow_no_vcr and not vcr`: "
            f"{len(integration_allow_no_vcr) - len(integration_overlap)}"
        ),
        f"- Integration `vcr and allow_no_vcr`: {len(integration_overlap)}",
        f"- Integration bare `use_cassette` without `vcr`: {len(cassette_only)}",
        "",
        "## Marker Counts",
        "",
    ]
    lines.extend(f"- `{name}`: {count}" for name, count in sorted(marker_counts.items()))
    lines.extend(
        [
            "",
            "## Characterization Files",
            "",
            "### Files With `characterization` Marker",
            "",
            _markdown_list(characterization_marker_files),
            "",
            "### Files Named `*characterization*`",
            "",
            _markdown_list(characterization_named_files),
            "",
            "## Integration Allowlist Baselines",
            "",
            "### `allow_no_vcr` Files",
            "",
            _markdown_list(_sorted_paths({record.path for record in integration_allow_no_vcr})),
            "",
            "### `allow_no_vcr` Node IDs",
            "",
            (
                f"{len(integration_allow_no_vcr)} node IDs. See "
                "`tests/_fixtures/integration_allow_no_vcr_nodeids.txt` for the exact baseline."
            ),
            "",
            "### `vcr and allow_no_vcr` Node IDs",
            "",
            (
                f"{len(integration_overlap)} node IDs. See "
                "`tests/_fixtures/integration_vcr_allow_no_vcr_nodeids.txt` for the exact baseline."
            ),
            "",
            "### Bare `use_cassette` Without `vcr`",
            "",
            f"{len(cassette_only)} node IDs. Any nonzero count is a missing-marker bug.",
            "",
            "## Allowlist File Schema",
            "",
            "- UTF-8 text.",
            "- Blank lines and `#` comments are ignored.",
            "- Non-comment entries are sorted.",
            "- Each entry is followed by ` # ` and a one-line rationale.",
            "- File allowlists contain repo-relative paths.",
            "- Node ID allowlists contain full collected node IDs.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def rendered_outputs(records: list[ItemRecord]) -> dict[Path, str]:
    integration = _records_under(records, "tests/integration/")
    allow_no_vcr = [record for record in integration if "allow_no_vcr" in record.markers]
    overlap = [record for record in allow_no_vcr if "vcr" in record.markers]
    return {
        INVENTORY_PATH: render_inventory(records),
        ALLOW_NO_VCR_FILES_PATH: _allowlist(
            _sorted_paths({record.path for record in allow_no_vcr}),
            ALLOW_NO_VCR_RATIONALE,
        ),
        ALLOW_NO_VCR_NODEIDS_PATH: _allowlist(
            [record.nodeid for record in allow_no_vcr],
            ALLOW_NO_VCR_RATIONALE,
        ),
        VCR_ALLOW_NO_VCR_NODEIDS_PATH: _allowlist(
            [record.nodeid for record in overlap],
            VCR_ALLOW_NO_VCR_RATIONALE,
        ),
    }


def write_outputs(outputs: dict[Path, str]) -> None:
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)


def check_outputs(outputs: dict[Path, str]) -> int:
    stale: list[str] = []
    for path, content in outputs.items():
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing != content:
            stale.append(_repo_relative(path))
    if stale:
        print("taxonomy inventory outputs are stale:", file=sys.stderr)
        for path in stale:
            print(f"  {path}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if generated outputs differ")
    args = parser.parse_args(argv)

    records = collect_items()
    outputs = rendered_outputs(records)
    if args.check:
        return check_outputs(outputs)
    write_outputs(outputs)
    print(f"wrote {len(outputs)} taxonomy inventory output(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
