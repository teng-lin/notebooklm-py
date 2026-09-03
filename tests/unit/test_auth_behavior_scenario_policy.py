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


@pytest.mark.parametrize("marker", ["skip", "skipif", "xfail", "repo_lint", "reality"])
def test_replacement_nodes_must_be_passing_ci_evidence(marker: str) -> None:
    node = "tests/unit/test_x.py::test_x"
    collection = _collection(node)
    collection["items"][0]["non_passing_markers"] = [marker]
    row = _row([node], [node], [{"base": node, "head": [node]}])

    with pytest.raises(PolicyError, match="not passing CI evidence"):
        validate_policy({"version": 1, "scenarios": [row]}, collection, collection)


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


@pytest.mark.parametrize(
    "bad_path",
    [
        "src/notebooklm/_auth/*.py",
        "src/notebooklm/_auth/../refresh.py",
        "src/notebooklm/_auth",
    ],
)
def test_affected_paths_must_be_exact_normalized_source_files(bad_path: str) -> None:
    collection = _collection("tests/unit/test_x.py::test_x")
    row = _row(
        ["tests/unit/test_x.py::test_x"],
        ["tests/unit/test_x.py::test_x"],
        [{"base": "tests/unit/test_x.py::test_x", "head": ["tests/unit/test_x.py::test_x"]}],
    )
    row["affected_paths"] = [bad_path]

    with pytest.raises(PolicyError, match="exact normalized production files"):
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


def test_plural_lifecycle_evidence_matches_every_decreased_joint_row() -> None:
    collection = _collection()
    module_mutation = {
        "package": "notebooklm._auth",
        "module": "cookie_policy",
        "attribute": "_WARNED",
        "idiom": "assignment",
        "path": "tests/conftest.py",
        "owner_qualname": "reset",
        "owner_kind": "fixture",
        "count": 2,
    }
    shared_mutation = {
        "package": "notebooklm._auth",
        "owner": "notebooklm._auth.keepalive.State.process_default().cache",
        "attribute": "clear",
        "idiom": "mutator",
        "path": "tests/conftest.py",
        "owner_qualname": "reset",
        "owner_kind": "fixture",
        "count": 2,
    }
    lifecycle_row = {
        "path": "tests/conftest.py",
        "replaced_base_mutations": [module_mutation, shared_mutation],
    }
    validate_policy(
        {"version": 1, "scenarios": []},
        collection,
        collection,
        base_policy={"version": 1, "scenarios": []},
        required_base_mutations=[module_mutation, shared_mutation],
        lifecycle_policy={"version": 1, "operations": [lifecycle_row]},
        base_lifecycle_policy={"version": 1, "operations": []},
    )


@pytest.mark.parametrize("evidence_counts", [[1, 1], [1, 2]])
def test_final_evidence_rejects_duplicate_and_split_count_rows(
    evidence_counts: list[int],
) -> None:
    collection = _collection()
    required = {
        "package": "notebooklm._auth",
        "module": "cookie_policy",
        "attribute": "_WARNED",
        "idiom": "assignment",
        "path": "tests/conftest.py",
        "owner_qualname": "reset",
        "owner_kind": "fixture",
        "count": sum(evidence_counts),
    }
    lifecycle_row = {
        "path": "tests/conftest.py",
        "replaced_base_mutations": [{**required, "count": count} for count in evidence_counts],
    }

    with pytest.raises(PolicyError, match="exactly match"):
        validate_policy(
            {"version": 1, "scenarios": []},
            collection,
            collection,
            base_policy={"version": 1, "scenarios": []},
            required_base_mutations=[required],
            lifecycle_policy={"version": 1, "operations": [lifecycle_row]},
            base_lifecycle_policy={"version": 1, "operations": []},
        )


def test_lifecycle_schema_rename_does_not_make_existing_rows_new_evidence() -> None:
    collection = _collection()
    validate_policy(
        {"version": 1, "scenarios": []},
        collection,
        collection,
        base_policy={"version": 1, "scenarios": []},
        required_base_mutations=[],
        lifecycle_policy={
            "version": 1,
            "operations": [{"path": "tests/conftest.py", "replaced_base_mutations": []}],
        },
        base_lifecycle_policy={
            "version": 1,
            "operations": [{"path": "tests/conftest.py", "replaced_base_mutation": None}],
        },
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


def _pinned_unresolved_helper_scenario():
    nodes = [
        "tests/unit/test_storage_writer.py::test_clear_in_band_account_swallows_lock_unavailable",
        "tests/unit/test_storage_writer.py::test_persist_minted_jar_fails_closed_on_lock_unavailable",
        "tests/unit/test_storage_writer.py::test_replace_from_login_failed_write_leaves_legacy_account_untouched",
        "tests/unit/test_storage_writer.py::test_replace_from_login_fails_closed_on_lock_unavailable",
        "tests/unit/test_storage_writer.py::test_replace_from_remint_takes_storage_lock",
        "tests/unit/test_storage_writer.py::test_update_account_metadata_fails_closed_on_lock_unavailable",
    ]
    collection = _collection(*nodes)
    helper = "tests/unit/test_storage_writer.py::_patch_lock_unavailable"
    collection["unresolved_helpers"] = [helper]
    collection["helper_consumers"][helper] = []
    row = _row(
        list(nodes),
        list(nodes),
        [{"base": node, "head": [node]} for node in nodes],
        owner_kind="helper",
    )
    row["base_mutations"] = [
        {
            "package": "notebooklm._auth",
            "module": "profile_store",
            "attribute": "_STORAGE_LOCKS",
            "idiom": "monkeypatch.setattr",
            "path": "tests/unit/test_storage_writer.py",
            "owner_qualname": "_patch_lock_unavailable",
            "owner_kind": "helper",
            "count": 1,
        }
    ]
    return collection, row


def test_pinned_base_helper_mutation_may_use_authored_exact_mapping() -> None:
    collection, row = _pinned_unresolved_helper_scenario()

    validate_policy({"version": 1, "scenarios": [row]}, collection, collection)


def test_pinned_base_helper_mutation_rejects_an_unrelated_extra_selector() -> None:
    collection, row = _pinned_unresolved_helper_scenario()
    extra = "tests/unit/test_storage_writer.py::test_unrelated"
    collection["items"].append(
        {"nodeid": extra, "canonical_node": extra, "fixtures": ["reset_auth"]}
    )
    row["base_selectors"].append(extra)
    row["replacement_selectors"].append(extra)
    row["node_mapping"].append({"base": extra, "head": [extra]})

    with pytest.raises(PolicyError, match="helper/fixture consumers"):
        validate_policy({"version": 1, "scenarios": [row]}, collection, collection)


def test_unresolved_helper_exception_rejects_an_exact_row_near_miss() -> None:
    collection, row = _pinned_unresolved_helper_scenario()
    row["base_mutations"][0]["count"] = 2

    with pytest.raises(PolicyError, match="unresolved dynamic callers"):
        validate_policy({"version": 1, "scenarios": [row]}, collection, collection)
