"""Unit tests for test taxonomy inventory helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest


def _load_inventory_script():
    root = Path(__file__).resolve().parents[2]
    path = root / "scripts" / "test_taxonomy_inventory.py"
    spec = importlib.util.spec_from_file_location("test_taxonomy_inventory", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_logical_module_key_strips_migration_suffixes() -> None:
    inventory = _load_inventory_script()

    assert (
        inventory.logical_module_key("tests/integration/test_sources_integration.py") == "sources"
    )
    assert inventory.logical_module_key("tests/unit/test_source_characterization.py") == "source"
    assert inventory.logical_module_key("tests/unit/test_artifacts_vcr.py") == "artifacts"
    assert inventory.logical_module_key("tests/unit/test_notes_mock.py") == "notes"


def test_normalized_identity_preserves_class_function_and_parameter_id() -> None:
    inventory = _load_inventory_script()
    record = inventory.ItemRecord(
        nodeid=(
            "tests/integration/test_sources_integration.py::"
            "TestSourcesAPI::test_list_sources[case-a]"
        ),
        path="tests/integration/test_sources_integration.py",
        markers=frozenset(),
    )

    assert (
        inventory.normalized_identity(record)
        == "sources::TestSourcesAPI::test_list_sources[case-a]"
    )


def test_normalized_identity_uses_nodeid_delimiter_for_suffix() -> None:
    inventory = _load_inventory_script()
    record = inventory.ItemRecord(
        nodeid=(
            "C:\\work\\repo\\tests\\integration\\test_sources_integration.py::"
            "TestSourcesAPI::test_list_sources"
        ),
        path="tests/integration/test_sources_integration.py",
        markers=frozenset(),
    )

    assert inventory.normalized_identity(record) == "sources::TestSourcesAPI::test_list_sources"


def test_duplicate_normalized_identities_are_reported() -> None:
    inventory = _load_inventory_script()
    records = [
        inventory.ItemRecord(
            nodeid="tests/integration/test_sources_integration.py::test_same",
            path="tests/integration/test_sources_integration.py",
            markers=frozenset(),
        ),
        inventory.ItemRecord(
            nodeid="tests/unit/test_sources.py::test_same",
            path="tests/unit/test_sources.py",
            markers=frozenset(),
        ),
    ]

    duplicates = inventory.duplicate_normalized_identities(records)

    assert duplicates == {
        "sources::test_same": [
            "tests/integration/test_sources_integration.py::test_same",
            "tests/unit/test_sources.py::test_same",
        ]
    }


def test_move_map_disambiguates_duplicate_normalized_identities() -> None:
    inventory = _load_inventory_script()
    records = [
        inventory.ItemRecord(
            nodeid="tests/integration/test_sources_integration.py::test_same",
            path="tests/integration/test_sources_integration.py",
            markers=frozenset(),
        ),
        inventory.ItemRecord(
            nodeid="tests/unit/test_sources.py::test_same",
            path="tests/unit/test_sources.py",
            markers=frozenset(),
        ),
    ]

    duplicates = inventory.duplicate_normalized_identities(
        records,
        move_map={"tests/unit/test_sources.py::test_same": "sources_unit::test_same"},
    )

    assert duplicates == {}


def test_collect_items_uses_repo_absolute_tests_path(monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = _load_inventory_script()
    captured: dict[str, Any] = {}

    def fake_pytest_main(args, plugins):
        captured["args"] = args
        captured["plugins"] = plugins
        return 0

    monkeypatch.setattr(inventory.pytest, "main", fake_pytest_main)

    assert inventory.collect_items() == []
    assert captured["args"][-1] == str(inventory.TESTS_DIR)
    assert Path(captured["args"][-1]).is_absolute()


def test_allowlist_entries_from_text_ignores_comments_and_rationales() -> None:
    inventory = _load_inventory_script()

    entries = inventory.allowlist_entries_from_text(
        "\n"
        "# comment\n"
        "tests/integration/test_a.py # reviewed reason\n"
        "tests/integration/test_b.py #reviewed reason without spacing\n"
        "tests/integration/test_c.py  # reviewed reason with extra spacing\n"
        "tests/integration/test_hash#name.py # reviewed reason\n"
    )

    assert entries == [
        "tests/integration/test_a.py",
        "tests/integration/test_b.py",
        "tests/integration/test_c.py",
        "tests/integration/test_hash#name.py",
    ]


def test_taxonomy_policy_is_file_level_not_nodeid_level() -> None:
    inventory = _load_inventory_script()
    records = [
        inventory.ItemRecord(
            nodeid="tests/integration/test_mock.py::test_renamed",
            path="tests/integration/test_mock.py",
            markers=frozenset({"allow_no_vcr"}),
        ),
        inventory.ItemRecord(
            nodeid="tests/e2e/test_live.py::test_live",
            path="tests/e2e/test_live.py",
            markers=frozenset({"e2e"}),
        ),
    ]

    violations = inventory.taxonomy_policy_violations(
        records,
        allow_no_vcr_files=["tests/integration/test_mock.py"],
    )

    assert violations == []


def test_taxonomy_policy_flags_new_allow_no_vcr_files() -> None:
    inventory = _load_inventory_script()
    records = [
        inventory.ItemRecord(
            nodeid="tests/integration/test_new_mock.py::test_mock_only",
            path="tests/integration/test_new_mock.py",
            markers=frozenset({"allow_no_vcr"}),
        ),
    ]

    violations = inventory.taxonomy_policy_violations(records, allow_no_vcr_files=[])

    assert len(violations) == 1
    assert "tests/integration/test_new_mock.py" in violations[0]


def test_taxonomy_policy_flags_e2e_items_without_marker() -> None:
    inventory = _load_inventory_script()
    records = [
        inventory.ItemRecord(
            nodeid="tests/e2e/test_live.py::test_live",
            path="tests/e2e/test_live.py",
            markers=frozenset(),
        ),
    ]

    violations = inventory.taxonomy_policy_violations(records, allow_no_vcr_files=[])

    assert len(violations) == 1
    assert "tests/e2e/test_live.py::test_live" in violations[0]


def test_taxonomy_policy_flags_cassette_without_vcr_marker() -> None:
    inventory = _load_inventory_script()
    records = [
        inventory.ItemRecord(
            nodeid="tests/integration/test_live.py::test_live",
            path="tests/integration/test_live.py",
            markers=frozenset(),
            has_use_cassette=True,
        ),
    ]

    violations = inventory.taxonomy_policy_violations(records, allow_no_vcr_files=[])

    assert len(violations) == 1
    assert "tests/integration/test_live.py::test_live" in violations[0]


def test_taxonomy_policy_flags_stale_allow_no_vcr_files() -> None:
    inventory = _load_inventory_script()

    violations = inventory.taxonomy_policy_violations(
        [],
        allow_no_vcr_files=["tests/integration/test_old_mock.py"],
    )

    assert len(violations) == 1
    assert "tests/integration/test_old_mock.py" in violations[0]


def test_rendered_baseline_outputs_excludes_audit_report() -> None:
    inventory = _load_inventory_script()

    outputs = inventory.rendered_baseline_outputs([])

    assert inventory.DEFAULT_REPORT_PATH not in outputs
    assert set(outputs) == {
        inventory.ALLOW_NO_VCR_FILES_PATH,
        inventory.ALLOW_NO_VCR_NODEIDS_PATH,
        inventory.VCR_ALLOW_NO_VCR_NODEIDS_PATH,
    }


def test_main_rejects_conflicting_actions() -> None:
    inventory = _load_inventory_script()

    with pytest.raises(SystemExit):
        inventory.main(["--check", "--write-report"])


def test_main_rejects_report_path_without_write_report(tmp_path: Path) -> None:
    inventory = _load_inventory_script()

    with pytest.raises(SystemExit) as exc_info:
        inventory.main(["--report-path", str(tmp_path / "taxonomy.md")])

    assert exc_info.value.code == 2
