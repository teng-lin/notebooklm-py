from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._web.artifacts import WebArtifactsAPI
from notebooklm._web.notebooks import WebNotebooksAPI
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.exceptions import NetworkError, RateLimitError, RPCError, ValidationError
from notebooklm.options import AndroidBackendConfig, ClientConfig, WebBackendConfig
from notebooklm.rpc import RPCMethod
from notebooklm.types import GenerationStatus
from tests._fixtures.fake_core import declared_noop_operation_scope, make_fake_core
from tests._helpers.operation import ClientStub
from tests.e2e._generation_journal import (
    DisabledJournal,
    JournalConfigurationError,
    journal_from_environment,
)


def _load_e2e_conftest():
    path = Path(__file__).resolve().parents[1] / "e2e" / "conftest.py"
    spec = importlib.util.spec_from_file_location("e2e_conftest", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


@pytest.fixture
def journaled_web_client(tmp_path):
    rpc = AsyncMock(return_value=[["created-id", None, None, None, 1]])
    core = make_fake_core(rpc_call=rpc)
    notebooks = WebNotebooksAPI(core, supervisor=core)
    artifacts = WebArtifactsAPI(
        rpc=core,
        supervisor=core,
        notebooks=notebooks,
        mind_maps=MagicMock(),
        note_service=MagicMock(),
    )
    client = ClientStub(artifacts=artifacts)
    path = _journal_file(tmp_path)
    journal = journal_from_environment(env=_required_env(tmp_path, path), node_id="test_node")
    _load_e2e_conftest()._install_generation_journal(client, journal)
    return client, rpc, path


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_config", [WebBackendConfig(), AndroidBackendConfig()])
@pytest.mark.parametrize(
    ("method_name", "family"),
    [
        ("generate_audio", "audio"),
        ("generate_video", "video"),
        ("generate_cinematic_video", "video"),
        ("generate_report", "report"),
        ("generate_quiz", "quiz"),
        ("generate_flashcards", "flashcards"),
        ("generate_infographic", "infographic"),
        ("generate_slide_deck", "slide_deck"),
        ("generate_data_table", "data_table"),
        ("generate_study_guide", "study_guide"),
    ],
)
async def test_assembled_client_generation_methods_reach_journal_boundary(
    tmp_path, monkeypatch, backend_config, method_name, family
):
    client = NotebookLMClient(
        AuthTokens(cookies={"SID": "synthetic"}, csrf_token="csrf", session_id="session"),
        config=ClientConfig(backend=backend_config),
    )
    # Exercise production assembly and public methods with offline admission;
    # no backend auth/open or transport is needed to observe the normalized send.
    monkeypatch.setattr(client.artifacts, "_operation_scope", declared_noop_operation_scope)
    send = AsyncMock(return_value=GenerationStatus(task_id="assembled-id", status="pending"))
    monkeypatch.setattr(client.artifacts, "_send_create_artifact", send)
    path = _journal_file(tmp_path)
    journal = journal_from_environment(env=_required_env(tmp_path, path), node_id="test_node")
    _load_e2e_conftest()._install_generation_journal(client, journal)

    result = await getattr(client.artifacts, method_name)(
        "generation-role", source_ids=["source-id"]
    )

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert result.task_id == "assembled-id"
    assert [row["event"] for row in rows] == ["started", "accepted"]
    assert all(row["family"] == family for row in rows)
    assert rows[-1]["resource_id"] == "assembled-id"
    send.assert_awaited_once()
    assert send.await_args.args[0].notebook_id == "generation-role"


@pytest.mark.asyncio
async def test_report_source_auth_failures_do_not_leave_interrupted_generation(
    journaled_web_client,
):
    client, rpc, path = journaled_web_client
    error = RPCError("unauthenticated", method_id=RPCMethod.GET_NOTEBOOK.value, rpc_code=16)
    rpc.side_effect = error

    # The primary and retry attempts both failed in get_raw(), before CREATE_ARTIFACT.
    for _ in range(2):
        with pytest.raises(RPCError) as caught:
            await client.artifacts.generate_report("generation-role")
        assert caught.value is error
    assert [call.args[0] for call in rpc.await_args_list] == [RPCMethod.GET_NOTEBOOK] * 2
    assert path.read_text() == ""

    rpc.side_effect = None
    result = await client.artifacts.generate_report("generation-role", source_ids=["source-id"])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert result.task_id == "created-id"
    assert [row["event"] for row in rows] == ["started", "accepted"]
    assert all(row["family"] == "report" for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight", [True, False])
async def test_quota_is_unconfirmed_only_after_creation_boundary(journaled_web_client, preflight):
    client, rpc, path = journaled_web_client
    method = RPCMethod.GET_NOTEBOOK if preflight else RPCMethod.CREATE_ARTIFACT
    rpc.side_effect = RateLimitError("quota", method_id=method.value, rpc_code=8)
    with pytest.raises(RateLimitError):
        await client.artifacts.generate_report(
            "generation-role", source_ids=None if preflight else ["source-id"]
        )

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["event"] for row in rows] == (
        [] if preflight else ["started", "quota_response_unconfirmed"]
    )
    assert rpc.await_args.args[0] == method


@pytest.mark.asyncio
async def test_invalid_generation_request_does_not_start_journal(journaled_web_client):
    client, rpc, path = journaled_web_client
    with pytest.raises(ValidationError):
        await client.artifacts.generate_report(
            "generation-role", report_format="invalid", source_ids=["source-id"]
        )
    rpc.assert_not_awaited()
    assert path.read_text() == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RPCError("unauthenticated", method_id=RPCMethod.CREATE_ARTIFACT.value, rpc_code=16),
        NetworkError("response lost"),
        asyncio.CancelledError(),
    ],
)
async def test_create_failure_retains_strict_interrupted_generation(journaled_web_client, error):
    client, rpc, path = journaled_web_client
    rpc.side_effect = error
    with pytest.raises(BaseException) as caught:
        await client.artifacts.generate_report("generation-role", source_ids=["source-id"])
    assert caught.value is error
    if isinstance(error, NetworkError):
        assert caught.value.unconfirmed
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["started"]
    assert rpc.await_args.args[0] == RPCMethod.CREATE_ARTIFACT


@pytest.mark.asyncio
async def test_child_generation_gets_its_own_family_before_parent_preflight_failure(
    journaled_web_client, monkeypatch
):
    client, rpc, path = journaled_web_client
    error = RPCError("source lookup failed", method_id=RPCMethod.GET_NOTEBOOK.value)

    async def resolve_sources(notebook_id):
        await asyncio.create_task(
            client.artifacts.generate_audio(notebook_id, source_ids=["source"])
        )
        raise error

    monkeypatch.setattr(client.artifacts._notebooks, "get_source_ids", resolve_sources)
    with pytest.raises(RPCError) as caught:
        await client.artifacts.generate_report("generation-role")
    assert caught.value is error
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["started", "accepted"]
    assert all(row["family"] == "audio" for row in rows)
    assert rpc.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_source_discovery_does_not_start_generation(journaled_web_client):
    client, rpc, path = journaled_web_client
    rpc.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await client.artifacts.generate_report("generation-role")
    assert rpc.await_args.args[0] == RPCMethod.GET_NOTEBOOK
    assert path.read_text() == ""


@pytest.mark.asyncio
async def test_android_report_uses_creation_boundary(tmp_path):
    from tests._fixtures.creation_conformance import (
        CREATE_ARTIFACT_METHOD,
        PROTO,
        android_artifacts_graph,
        artifact_proto,
    )

    terminal, _, _, _, artifacts = android_artifacts_graph()
    terminal.responses[CREATE_ARTIFACT_METHOD] = PROTO.CreateArtifactResponse(
        artifact=artifact_proto(
            "android-created", type_code=2, status=PROTO.ARTIFACT_STATUS_INITIALIZED
        )
    )
    path = _journal_file(tmp_path)
    journal = journal_from_environment(env=_required_env(tmp_path, path), node_id="test_node")
    _load_e2e_conftest()._install_generation_journal(ClientStub(artifacts=artifacts), journal)

    result = await artifacts.generate_report("generation-role")

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert result.task_id == "android-created"
    assert [row["event"] for row in rows] == ["started", "accepted"]
    assert all(row["family"] == "report" for row in rows)
    assert len(terminal.calls) == 1


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
async def test_note_mind_map_typed_quota_records_unconfirmed_outcome() -> None:
    from tests.e2e._generation_helpers import generate_note_mind_map

    events: list[str] = []

    class Artifacts:
        async def generate_mind_map(self, notebook_id: str) -> None:
            del notebook_id
            skipped = pytest.skip.Exception("typed quota")
            skipped._notebooklm_typed_rate_limit = True
            raise skipped

    operation = SimpleNamespace(
        quota_response_unconfirmed=lambda: events.append("quota_unconfirmed")
    )
    with pytest.raises(pytest.skip.Exception, match="typed quota"):
        await generate_note_mind_map(
            SimpleNamespace(artifacts=Artifacts()), "generation-role", operation
        )
    assert events == ["quota_unconfirmed"]


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


def test_quota_response_records_commit_uncertainty(tmp_path) -> None:
    path = _journal_file(tmp_path)
    journal = journal_from_environment(env=_required_env(tmp_path, path), node_id="quota")
    operation = journal.operation(
        notebook_id="generation-role",
        family="audio",
        surface="client",
        id_kind="studio_task",
        lifecycle="settle",
    )
    operation.quota_response_unconfirmed()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["event"] for row in rows] == ["started", "quota_response_unconfirmed"]
    assert rows[-1]["reason"] == "server_commit_unknown"


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
