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
