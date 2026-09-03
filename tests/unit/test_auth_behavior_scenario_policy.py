"""Executable schema and collection semantics for auth behavior scenarios."""

from __future__ import annotations

import pytest
from scripts.check_auth_behavior_scenario_policy import (
    PolicyError,
    _canonical,
    _static_helper_consumers,
    validate_policy,
)


def test_parameter_id_containing_double_colon_has_a_stable_canonical_node() -> None:
    assert _canonical("tests/test_x.py::test_x[a::b]") == "tests/test_x.py::test_x"


def _collection(*nodeids: str):
    return {
        "version": 1,
        "items": [
            {
                "nodeid": nodeid,
                "canonical_node": nodeid.split("[", 1)[0],
                "fixtures": ["reset_auth"],
            }
            for nodeid in nodeids
        ],
        "fixture_consumers": {"tests/unit/test_x.py::reset_auth": list(nodeids)},
        "helper_consumers": {"tests/unit/test_x.py::helper": list(nodeids)},
    }


def _row(base_selectors, replacement_selectors, mapping, *, owner_kind="test"):
    owner = {
        "fixture": "reset_auth",
        "helper": "helper",
        "test": base_selectors[0].rsplit("::", 1)[-1].split("[", 1)[0],
    }[owner_kind]
    return {
        "id": "refresh-ordering",
        "base_selectors": base_selectors,
        "replacement_selectors": replacement_selectors,
        "node_mapping": mapping,
        "affected_paths": ["src/notebooklm/_auth/refresh.py"],
        "contracts": ["ordering", "result"],
        "base_mutations": [
            {
                "package": "notebooklm._auth",
                "module": "refresh",
                "attribute": "_runner",
                "idiom": "monkeypatch.setattr",
                "path": "tests/unit/test_x.py",
                "owner_qualname": owner,
                "owner_kind": owner_kind,
                "count": 1,
            }
        ],
    }


def test_parameter_prefix_must_map_the_complete_concrete_set() -> None:
    base = _collection("tests/unit/test_x.py::test_old[a]", "tests/unit/test_x.py::test_old[b]")
    head = _collection("tests/unit/test_x.py::test_new[a]", "tests/unit/test_x.py::test_new[b]")
    row = _row(
        ["tests/unit/test_x.py::test_old"],
        ["tests/unit/test_x.py::test_new"],
        [
            {
                "base": "tests/unit/test_x.py::test_old[a]",
                "head": ["tests/unit/test_x.py::test_new[a]"],
            },
            {
                "base": "tests/unit/test_x.py::test_old[b]",
                "head": ["tests/unit/test_x.py::test_new[b]"],
            },
        ],
    )
    validate_policy({"version": 1, "scenarios": [row]}, base, head)


def test_incomplete_parameter_mapping_and_wildcards_fail() -> None:
    base = _collection("tests/unit/test_x.py::test_old[a]", "tests/unit/test_x.py::test_old[b]")
    head = _collection("tests/unit/test_x.py::test_new")
    row = _row(
        ["tests/unit/test_x.py::test_old"],
        ["tests/unit/test_x.py::test_new"],
        [{"base": "tests/unit/test_x.py::test_old[a]", "head": ["tests/unit/test_x.py::test_new"]}],
    )
    with pytest.raises(PolicyError, match="mapping is not exact"):
        validate_policy({"version": 1, "scenarios": [row]}, base, head)
    row["base_selectors"] = ["tests/unit/test_x.py::*"]
    with pytest.raises(PolicyError, match="wildcards"):
        validate_policy({"version": 1, "scenarios": [row]}, base, head)


@pytest.mark.parametrize("owner_kind", ["fixture", "helper"])
def test_fixture_and_helper_selectors_cover_every_consumer(owner_kind: str) -> None:
    base = _collection("tests/unit/test_x.py::test_old[a]", "tests/unit/test_x.py::test_old[b]")
    head = _collection("tests/unit/test_x.py::test_new")
    row = _row(
        ["tests/unit/test_x.py::test_old[a]"],
        ["tests/unit/test_x.py::test_new"],
        [{"base": "tests/unit/test_x.py::test_old[a]", "head": ["tests/unit/test_x.py::test_new"]}],
        owner_kind=owner_kind,
    )
    with pytest.raises(PolicyError, match="helper/fixture consumers"):
        validate_policy({"version": 1, "scenarios": [row]}, base, head)


def test_unknown_contract_tag_and_extra_schema_field_fail() -> None:
    collection = _collection("tests/unit/test_x.py::test_x")
    row = _row(
        ["tests/unit/test_x.py::test_x"],
        ["tests/unit/test_x.py::test_x"],
        [{"base": "tests/unit/test_x.py::test_x", "head": ["tests/unit/test_x.py::test_x"]}],
    )
    row["contracts"] = ["looks-good"]
    with pytest.raises(PolicyError, match="contract"):
        validate_policy({"version": 1, "scenarios": [row]}, collection, collection)
    row["contracts"] = ["result"]
    row["free_form"] = "escape"
    with pytest.raises(PolicyError, match="wrong fields"):
        validate_policy({"version": 1, "scenarios": [row]}, collection, collection)


def test_scenario_evidence_must_exactly_match_decreased_joint_rows() -> None:
    base = _collection("tests/unit/test_x.py::test_old")
    head = _collection("tests/unit/test_x.py::test_new")
    row = _row(
        ["tests/unit/test_x.py::test_old"],
        ["tests/unit/test_x.py::test_new"],
        [
            {
                "base": "tests/unit/test_x.py::test_old",
                "head": ["tests/unit/test_x.py::test_new"],
            }
        ],
    )
    required = [dict(row["base_mutations"][0])]
    validate_policy(
        {"version": 1, "scenarios": [row]},
        base,
        head,
        base_policy={"version": 1, "scenarios": []},
        required_base_mutations=required,
    )
    required[0]["attribute"] = "_different"
    with pytest.raises(PolicyError, match="exactly match"):
        validate_policy(
            {"version": 1, "scenarios": [row]},
            base,
            head,
            base_policy={"version": 1, "scenarios": []},
            required_base_mutations=required,
        )


def test_existing_scenario_is_immutable_and_only_head_nodes_must_remain() -> None:
    old_base = _collection("tests/unit/test_x.py::test_old")
    head = _collection("tests/unit/test_x.py::test_new")
    row = _row(
        ["tests/unit/test_x.py::test_old"],
        ["tests/unit/test_x.py::test_new"],
        [
            {
                "base": "tests/unit/test_x.py::test_old",
                "head": ["tests/unit/test_x.py::test_new"],
            }
        ],
    )
    policy = {"version": 1, "scenarios": [row]}
    validate_policy(policy, old_base, head, base_policy=policy, required_base_mutations=[])
    changed = {"version": 1, "scenarios": [{**row, "contracts": ["result"]}]}
    with pytest.raises(PolicyError, match="immutable"):
        validate_policy(changed, old_base, head, base_policy=policy)


def test_helper_alias_call_is_marked_unresolved_and_fails_closed(tmp_path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text(
        "def helper(): pass\n"
        "def test_a(): helper()\n"
        "def test_b():\n"
        "    alias = helper\n"
        "    alias()\n"
        "def test_dynamic(name):\n"
        "    globals()[name]()\n"
        "async def test_nested():\n"
        "    async def _run():\n"
        "        helper()\n"
        "    await _run()\n",
        encoding="utf-8",
    )
    items = [
        {
            "nodeid": "tests/test_x.py::test_a",
            "canonical_node": "tests/test_x.py::test_a",
        },
        {
            "nodeid": "tests/test_x.py::test_b",
            "canonical_node": "tests/test_x.py::test_b",
        },
        {
            "nodeid": "tests/test_x.py::test_nested",
            "canonical_node": "tests/test_x.py::test_nested",
        },
        {
            "nodeid": "tests/test_x.py::test_dynamic",
            "canonical_node": "tests/test_x.py::test_dynamic",
        },
    ]
    consumers, unresolved = _static_helper_consumers(tmp_path, items)
    assert consumers["tests/test_x.py::helper"] == [
        "tests/test_x.py::test_a",
        "tests/test_x.py::test_nested",
    ]
    assert consumers["tests/test_x.py::test_nested._run"] == ["tests/test_x.py::test_nested"]
    assert unresolved == [
        "tests/test_x.py::helper",
        "tests/test_x.py::test_nested._run",
    ]
