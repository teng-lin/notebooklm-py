from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e._generation_journal import (
    DisabledJournal,
    JournalConfigurationError,
    journal_from_environment,
)


def _required_env(tmp_path: Path, journal: Path) -> dict[str, str]:
    return {
        "NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE": "required",
        "NOTEBOOKLM_E2E_GENERATION_JOURNAL": str(journal),
        "NOTEBOOKLM_E2E_MANAGED_COPIES": "1",
        "NOTEBOOKLM_GENERATION_NOTEBOOK_ID": "generation-role",
        "RUNNER_TEMP": str(tmp_path),
    }


def _journal_file(tmp_path: Path) -> Path:
    path = tmp_path / "generation.jsonl"
    path.touch(mode=0o600)
    if os.name != "nt":
        path.chmod(0o600)
    return path


def test_required_journal_appends_versioned_transitions_without_printing_ids(
    tmp_path, capsys
) -> None:
    path = _journal_file(tmp_path)
    journal = journal_from_environment(env=_required_env(tmp_path, path), node_id="test_node")
    operation = journal.operation(
        notebook_id="generation-role",
        family="audio",
        surface="client",
        id_kind="studio_task",
        lifecycle="settle",
    )
    assert operation.last_event == "started"
    operation.accepted("artifact-secret-id")
    assert operation.last_event == "accepted"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["started", "accepted"]
    assert all(row["version"] == 1 for row in rows)
    assert len({row["operation_id"] for row in rows}) == 1
    assert "artifact-secret-id" not in capsys.readouterr().out
    lock = path.with_name(f".{path.name}.lock")
    assert lock.is_file()
    if os.name != "nt":
        assert lock.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_note_mind_map_typed_quota_closes_manual_operation() -> None:
    from tests.e2e._generation_helpers import generate_note_mind_map

    events: list[str] = []

    class Artifacts:
        async def generate_mind_map(self, notebook_id: str) -> None:
            del notebook_id
            skipped = pytest.skip.Exception("typed quota")
            skipped._notebooklm_typed_rate_limit = True
            raise skipped

    operation = SimpleNamespace(rate_limited_rejected=lambda: events.append("rejected"))
    with pytest.raises(pytest.skip.Exception, match="typed quota"):
        await generate_note_mind_map(
            SimpleNamespace(artifacts=Artifacts()), "generation-role", operation
        )
    assert events == ["rejected"]


def test_primary_and_retry_processes_append_without_truncation(tmp_path) -> None:
    path = _journal_file(tmp_path)
    env = _required_env(tmp_path, path)
    first = journal_from_environment(env=env, node_id="first")
    first.operation(
        notebook_id="generation-role",
        family="report",
        surface="cli",
        id_kind="studio_task",
        lifecycle="settle",
    ).rate_limited_rejected()
    second = journal_from_environment(env=env, node_id="retry")
    operation = second.operation(
        notebook_id="generation-role",
        family="report",
        surface="mcp",
        id_kind="studio_task",
        lifecycle="settle",
    )
    operation.accepted("accepted-id")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 4
    assert {row["node_id"] for row in rows} == {"first", "retry"}
    assert {row["surface"] for row in rows} == {"cli", "mcp"}


def test_retry_cleanup_resumes_prior_operation_uuid(tmp_path) -> None:
    path = _journal_file(tmp_path)
    env = _required_env(tmp_path, path)
    primary = journal_from_environment(env=env, node_id="primary")
    operation = primary.operation(
        notebook_id="generation-role",
        family="mind_map",
        surface="client",
        id_kind="note_mind_map",
        lifecycle="settle",
    )
    operation.persisted("note-id")
    operation.completed("note-id")
    retry = journal_from_environment(env=env, node_id="retry")
    recovered = retry.recovery_operation(
        resource_id="note-id",
        notebook_id="generation-role",
        family="mind_map",
        surface="client",
        id_kind="note_mind_map",
        reason="retry_preclean",
    )
    recovered.delete_confirmed("note-id", reason="retry_preclean")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len({row["operation_id"] for row in rows}) == 1
    assert rows[-1]["event"] == "delete_confirmed"
    assert rows[-1]["node_id"] == "primary"

    already_closed = retry.recovery_operation(
        resource_id="note-id",
        notebook_id="generation-role",
        family="mind_map",
        surface="client",
        id_kind="note_mind_map",
        reason="retry_preclean",
    )
    before = path.read_text()
    with pytest.raises(ValueError, match="transition"):
        already_closed.delete_confirmed("note-id", reason="retry_preclean")
    assert path.read_text() == before


def test_note_and_interactive_backings_have_explicit_lifecycles(tmp_path) -> None:
    path = _journal_file(tmp_path)
    journal = journal_from_environment(env=_required_env(tmp_path, path), node_id="maps")
    note = journal.operation(
        notebook_id="generation-role",
        family="mind_map",
        surface="client",
        id_kind="note_mind_map",
        lifecycle="settle",
    )
    note.persisted("note-id")
    note.completed("note-id")
    interactive = journal.operation(
        notebook_id="generation-role",
        family="mind_map",
        surface="rest",
        id_kind="studio_task",
        lifecycle="test_owned",
    )
    interactive.accepted("studio-id")
    interactive.completed("studio-id")
    interactive.delete_confirmed("studio-id", reason="test_teardown")
    events = [json.loads(line)["event"] for line in path.read_text().splitlines()]
    assert events == [
        "started",
        "persisted",
        "completed",
        "started",
        "accepted",
        "completed",
        "delete_confirmed",
    ]


def test_target_mismatch_is_rejected_before_append(tmp_path) -> None:
    path = _journal_file(tmp_path)
    journal = journal_from_environment(env=_required_env(tmp_path, path), node_id="node")
    with pytest.raises(JournalConfigurationError, match="not the managed"):
        journal.operation(
            notebook_id="multi-source-role",
            family="audio",
            surface="client",
            id_kind="studio_task",
            lifecycle="settle",
        )
    assert path.read_text() == ""


def test_writer_rejects_invalid_backing_and_transition_before_append(tmp_path) -> None:
    path = _journal_file(tmp_path)
    journal = journal_from_environment(env=_required_env(tmp_path, path), node_id="node")
    with pytest.raises(ValueError, match="mind_map family"):
        journal.operation(
            notebook_id="generation-role",
            family="audio",
            surface="client",
            id_kind="note_mind_map",
            lifecycle="settle",
        )
    assert path.read_text() == ""

    operation = journal.operation(
        notebook_id="generation-role",
        family="audio",
        surface="client",
        id_kind="studio_task",
        lifecycle="settle",
    )
    operation.accepted("artifact-id")
    before = path.read_text()
    with pytest.raises(ValueError, match="transition"):
        operation.accepted("artifact-id")
    with pytest.raises(ValueError, match="retry pre-clean"):
        operation.delete_confirmed("artifact-id", reason="test_teardown")
    assert path.read_text() == before


@pytest.mark.parametrize(
    "env",
    [
        {"NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE": "required"},
        {
            "NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE": "off",
            "NOTEBOOKLM_E2E_GENERATION_JOURNAL": "/tmp/should-not-exist",
        },
        {"NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE": "sometimes"},
    ],
)
def test_invalid_required_or_off_configuration_fails(env: dict[str, str]) -> None:
    with pytest.raises(JournalConfigurationError):
        journal_from_environment(env=env, node_id="node")


def test_off_and_unset_are_noops() -> None:
    for env in ({}, {"NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE": "off"}):
        journal = journal_from_environment(env=env, node_id="node")
        assert isinstance(journal, DisabledJournal)
        operation = journal.operation(
            notebook_id="anything",
            family="anything",
            surface="anything",
            id_kind="anything",
            lifecycle="anything",
        )
        assert operation.last_event == "disabled"
        operation.accepted("anything")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_required_journal_rejects_open_permissions(tmp_path) -> None:
    path = _journal_file(tmp_path)
    path.chmod(0o644)
    with pytest.raises(JournalConfigurationError, match="0600"):
        journal_from_environment(env=_required_env(tmp_path, path), node_id="node")
