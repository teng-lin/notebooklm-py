from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.check_auth_lifecycle_cleanup_policy import (
    LifecyclePolicyError,
    live_operations,
    validate_policy,
    validate_verification_nodes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.repo_lint
def test_auth_lifecycle_cleanup_policy_exactly_matches_live_operations() -> None:
    policy = json.loads(
        (REPO_ROOT / "tests/fixtures/policies/auth_lifecycle_cleanup.json").read_text(
            encoding="utf-8"
        )
    )
    validate_policy(policy, live_operations(REPO_ROOT / "tests"))


def test_lifecycle_policy_rejects_unlisted_live_operation() -> None:
    with pytest.raises(LifecyclePolicyError, match="exact multiset"):
        validate_policy(
            {"version": 1, "operations": []},
            [
                {
                    "path": "tests/conftest.py",
                    "owner_qualname": "reset",
                    "owner_kind": "fixture",
                    "production_owner": "notebooklm._auth.state.Owner.process_default()",
                    "method": "reset",
                    "count": 1,
                    "phase": "setup_and_teardown",
                }
            ],
        )


def test_lifecycle_policy_requires_plural_exact_replacement_rows() -> None:
    live = [
        {
            "path": "tests/conftest.py",
            "owner_qualname": "reset",
            "owner_kind": "fixture",
            "production_owner": "notebooklm._auth.state.Owner.process_default()",
            "method": "reset",
            "count": 1,
            "phase": "setup_and_teardown",
        }
    ]
    row = {
        **live[0],
        "affected_paths": ["src/notebooklm/_auth/state.py"],
        "replaced_base_mutations": [],
        "verification_node_prefixes": ["tests/unit/test_x.py::test_owner"],
    }
    validate_policy({"version": 1, "operations": [row]}, live)
    singular = dict(row)
    singular["replaced_base_mutation"] = singular.pop("replaced_base_mutations")
    with pytest.raises(LifecyclePolicyError, match="wrong fields"):
        validate_policy({"version": 1, "operations": [singular]}, live)
    malformed = {**row, "replaced_base_mutations": {}}
    with pytest.raises(LifecyclePolicyError, match="must be a list"):
        validate_policy({"version": 1, "operations": [malformed]}, live)


def test_lifecycle_verification_nodes_must_collect_complete_parameter_families() -> None:
    policy = {
        "version": 1,
        "operations": [
            {
                "path": "tests/conftest.py",
                "owner_qualname": "reset",
                "owner_kind": "fixture",
                "production_owner": "notebooklm._auth.state.Owner.process_default()",
                "method": "reset",
                "count": 2,
                "phase": "setup_and_teardown",
                "affected_paths": ["src/notebooklm/_auth/state.py"],
                "replaced_base_mutations": [],
                "verification_node_prefixes": ["tests/unit/test_x.py::test_owner"],
            }
        ],
    }
    collection = {
        "items": [
            {
                "nodeid": "tests/unit/test_x.py::test_owner[a]",
                "canonical_node": "tests/unit/test_x.py::test_owner",
            },
            {
                "nodeid": "tests/unit/test_x.py::test_owner[b]",
                "canonical_node": "tests/unit/test_x.py::test_owner",
            },
        ]
    }
    validate_verification_nodes(policy, collection)
    policy["operations"][0]["verification_node_prefixes"] = ["tests/unit/test_x.py::test_owner[a]"]
    with pytest.raises(LifecyclePolicyError, match="does not collect"):
        validate_verification_nodes(policy, collection)
    policy["operations"][0]["verification_node_prefixes"] = ["tests/unit/test_x.py::test_missing"]
    with pytest.raises(LifecyclePolicyError, match="does not collect"):
        validate_verification_nodes(policy, collection)


def test_cookie_warning_reset_wrapper_is_an_exact_fixture_lifecycle_operation(tmp_path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def _reset_poke_state():\n"
        "    from notebooklm._auth import cookie_policy as policy\n"
        "    policy._reset_secondary_binding_warning_for_tests()\n"
        "    yield\n"
        "    policy._reset_secondary_binding_warning_for_tests()\n",
        encoding="utf-8",
    )
    assert live_operations(tests) == [
        {
            "path": "tests/conftest.py",
            "owner_qualname": "_reset_poke_state",
            "owner_kind": "fixture",
            "production_owner": "notebooklm._auth.cookie_policy",
            "method": "_reset_secondary_binding_warning_for_tests",
            "count": 2,
            "phase": "setup_and_teardown",
        }
    ]


def test_cookie_warning_reset_recognition_rejects_unresolved_or_rebound_targets(tmp_path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text(
        "import pytest\n"
        "class LocalPolicy:\n"
        "    def _reset_secondary_binding_warning_for_tests(self): pass\n"
        "@pytest.fixture\n"
        "def ignored_fixture():\n"
        "    policy = LocalPolicy()\n"
        "    policy._reset_secondary_binding_warning_for_tests()\n"
        "    yield\n"
        "@pytest.fixture\n"
        "def _reset_poke_state():\n"
        "    from notebooklm._auth import cookie_policy as policy\n"
        "    policy = object()\n"
        "    policy._reset_secondary_binding_warning_for_tests()\n"
        "    yield\n",
        encoding="utf-8",
    )
    assert live_operations(tests) == []
