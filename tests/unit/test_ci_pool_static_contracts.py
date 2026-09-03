"""Future-facing guardrails for the inert PR-1 CI account-pool tooling."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

ROOT = Path(__file__).resolve().parents[2]


def test_selector_secret_name_construction_is_closed_over_three_slots() -> None:
    source = (ROOT / "scripts" / "select_ci_account.py").read_text()
    assert 'ALLOWED_SLOTS = ("A", "B", "C")' in source
    assert 'f"NOTEBOOKLM_MASTER_TOKEN_JSON_{selected}"' in source
    assert "NOTEBOOKLM_ACCOUNTS_JSON" not in source


def test_selector_output_is_version_pinned_but_contains_only_safe_alias_fields() -> None:
    from scripts.select_ci_account import SCHEMA_VERSION, select_account

    assert SCHEMA_VERSION == 1
    record = select_account(
        enabled_slots="A,B,C",
        lane="nightly-web-windows",
        rotation_day="1970-01-01",
    )
    assert set(record) == {
        "account_slot",
        "master_token_secret_name",
        "lane",
        "rotation_day",
    }


def test_auth_materializer_never_accepts_token_on_argv_or_inherits_it_to_child() -> None:
    source = (ROOT / "scripts" / "materialize_ci_auth.py").read_text()
    assert 'source_env.get("NOTEBOOKLM_MASTER_TOKEN_JSON", "")' in source
    assert 'child_env.pop("NOTEBOOKLM_MASTER_TOKEN_JSON", None)' in source
    assert 'parser.add_argument("--master-token"' not in source
    assert "capture_output=True" in source
    assert "scrub_secrets" in source


def test_journal_and_verifier_are_runner_local_and_do_not_log_ids() -> None:
    helper = (ROOT / "tests" / "e2e" / "_generation_journal.py").read_text()
    verifier = (ROOT / "scripts" / "verify_ci_artifacts.py").read_text()
    assert "required journal must be under RUNNER_TEMP" in helper
    assert "print(notebook_id" not in verifier
    assert "artifact.title" not in verifier
    assert "--notebook-id-env" in verifier


def test_pr1_keeps_detached_workflow_on_explicit_compatibility_mode() -> None:
    path = ROOT / ".github" / "workflows" / "verify-artifacts.yml"
    source = path.read_text()
    assert path.is_file()
    assert "--mode inventory-compat" in source
    assert "scripts/verify_ci_artifacts.py" in source


def test_future_account_concurrency_contract_is_pinned_in_approved_spec() -> None:
    """Keep the workflow cutover vocabulary centralized and reviewable."""
    selector = (ROOT / "scripts" / "select_ci_account.py").read_text()
    aggregator = (ROOT / "scripts" / "aggregate_ci_e2e_outcomes.py").read_text()
    for lane in (
        "nightly-web-windows",
        "nightly-android-windows",
        "rpc-health-web",
        "rpc-health-android",
        "verify-package",
    ):
        assert lane in selector
        assert lane in aggregator
    docs = (ROOT / "docs" / "development.md").read_text()
    assert "100 pending" in docs
    assert "queue" in docs
    assert "never account emails or token fields" in docs
