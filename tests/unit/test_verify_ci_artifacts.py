from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_ci_artifacts.py"
SPEC = importlib.util.spec_from_file_location("verify_ci_artifacts", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify
SPEC.loader.exec_module(verify)


@dataclass
class Artifact:
    id: str
    kind: str
    status_str: str = "completed"
    interactive: bool = False
    unclassified: bool = False
    url: str | None = "https://example.invalid/artifact"

    @property
    def is_completed(self) -> bool:
        return self.status_str == "completed"

    @property
    def is_processing(self) -> bool:
        return self.status_str in {"pending", "in_progress", "pending_review", "unknown"}

    @property
    def is_failed(self) -> bool:
        return self.status_str == "failed"

    @property
    def is_interactive_mind_map(self) -> bool:
        return self.interactive

    @property
    def is_unclassified_type4(self) -> bool:
        return self.unclassified


class Artifacts:
    def __init__(self, inventories, statuses=None):
        self.inventories = [list(value) for value in inventories]
        self.statuses = list(statuses or [])
        self.list_calls = 0
        self.poll_ids = []
        self.get_ids = []

    async def list(self, notebook_id):
        index = min(self.list_calls, len(self.inventories) - 1)
        self.list_calls += 1
        return list(self.inventories[index])

    async def poll_status(self, notebook_id, resource_id):
        self.poll_ids.append(resource_id)
        status = self.statuses.pop(0) if self.statuses else "completed"
        return SimpleNamespace(status=status)

    async def get_or_none(self, notebook_id, resource_id):
        self.get_ids.append(resource_id)
        latest = self.inventories[min(self.list_calls - 1, len(self.inventories) - 1)]
        return next((artifact for artifact in latest if artifact.id == resource_id), None)


class Client:
    def __init__(self, inventories, statuses=None, notes=()):
        self.artifacts = Artifacts(inventories, statuses)
        self.notes = SimpleNamespace(list=self._notes)
        self._note_values = list(notes)

    async def _notes(self, notebook_id):
        return self._note_values


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    async def sleep(self, duration: float) -> None:
        self.value += duration


def row(
    operation_id: str,
    event: str,
    *,
    resource_id: str | None = None,
    family: str = "audio",
    notebook_id: str = "generation-role",
    id_kind: str = "studio_task",
    lifecycle: str = "settle",
    surface: str = "client",
    reason: str | None = None,
) -> dict[str, object]:
    return {
        "version": 1,
        "operation_id": operation_id,
        "event": event,
        "notebook_id": notebook_id,
        "resource_id": resource_id,
        "id_kind": id_kind,
        "family": family,
        "surface": surface,
        "node_id": "tests/e2e/test_generation.py::test_one",
        "lifecycle": lifecycle,
        "reason": reason,
    }


def write_journal(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(value) + "\n" for value in rows))
    if os.name != "nt":
        path.chmod(0o600)


@pytest.mark.asyncio
async def test_inventory_compat_preserves_zero_and_majority_thresholds(capsys) -> None:
    with pytest.raises(verify.EmptyArtifactsError):
        await verify.inventory_compat(Client([[]]), "secret-notebook")
    artifacts = [Artifact("a", "audio", "failed"), Artifact("b", "report", "completed")]
    result = await verify.inventory_compat(Client([artifacts], notes=[object()]), "secret-notebook")
    assert result["failed"] == 1  # exactly half still passes, matching the old workflow
    with pytest.raises(verify.FailedMajorityError):
        await verify.inventory_compat(
            Client([[Artifact("a", "audio", "failed"), Artifact("b", "video", "failed")]]),
            "secret-notebook",
        )
    assert "secret-notebook" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(0, 2700), (18000, 2700), (18001, 2699), (20460, 240), (20700, 0)],
)
def test_effective_budget_boundaries(elapsed: int, expected: int) -> None:
    now = 30_000_000_000_000
    marker = str(now - elapsed * 1_000_000_000)
    assert verify.compute_budget(marker, monotonic_ns=now) == expected


@pytest.mark.parametrize("marker", [None, "", "wat", "-1"])
def test_malformed_job_marker_fails(marker: str | None) -> None:
    with pytest.raises(verify.ConfigurationError):
        verify.compute_budget(marker, monotonic_ns=100)
    with pytest.raises(verify.ConfigurationError):
        verify.compute_budget("101", monotonic_ns=100)


def test_snapshot_status_preserves_media_readiness_semantics() -> None:
    assert verify._snapshot_status(Artifact("audio", "audio", url=None)) == "in_progress"
    assert verify._snapshot_status(Artifact("audio", "audio")) == "completed"
    assert verify._snapshot_status(Artifact("report", "report", url=None)) == "completed"


def test_journal_parser_rejects_schema_transition_and_binding_errors(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    invalid_cases = [
        [row(operation_id, "accepted", resource_id="a")],
        [row(operation_id, "started"), row(operation_id, "started")],
        [row(operation_id, "started"), row(operation_id, "accepted", resource_id="a", family="x")],
        [row(operation_id, "started", notebook_id="another")],
        [
            row(operation_id, "started"),
            row(operation_id, "accepted", resource_id="a"),
            row(
                operation_id,
                "rate_limited_rejected",
                resource_id="a",
                reason="typed_pre_acceptance",
            ),
        ],
    ]
    for rows in invalid_cases:
        write_journal(journal, rows)
        with pytest.raises(verify.JournalError):
            verify.parse_journal(journal, notebook_id="generation-role")


def test_journal_parser_rejects_one_resource_claimed_by_two_operations(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    write_journal(
        journal,
        [
            row(first, "started"),
            row(first, "accepted", resource_id="same"),
            row(second, "started"),
            row(second, "accepted", resource_id="same"),
        ],
    )
    with pytest.raises(verify.JournalError, match="multiple operations"):
        verify.parse_journal(journal, notebook_id="generation-role")


def test_journal_parser_accepts_backing_specific_and_recovery_edges(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    write_journal(
        journal,
        [
            row(first, "started", family="mind_map", id_kind="note_mind_map"),
            row(
                first,
                "persisted",
                resource_id="note-map",
                family="mind_map",
                id_kind="note_mind_map",
            ),
            row(
                first,
                "completed",
                resource_id="note-map",
                family="mind_map",
                id_kind="note_mind_map",
            ),
            row(second, "started", family="mind_map", lifecycle="test_owned"),
            row(
                second,
                "discovered_accepted",
                resource_id="studio-map",
                family="mind_map",
                lifecycle="test_owned",
                reason="post_create_quota",
            ),
            row(
                second,
                "delete_confirmed",
                resource_id="studio-map",
                family="mind_map",
                lifecycle="test_owned",
                reason="post_create_quota",
            ),
        ],
    )
    parsed = verify.parse_journal(journal, notebook_id="generation-role")
    assert len(parsed) == 2


def test_journal_parser_rejects_unauthorized_settling_deletion(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [
            row(operation_id, "started"),
            row(operation_id, "accepted", resource_id="artifact"),
            row(
                operation_id,
                "delete_confirmed",
                resource_id="artifact",
                reason="test_teardown",
            ),
        ],
    )
    with pytest.raises(verify.JournalError, match="unauthorized deletion"):
        verify.parse_journal(journal, notebook_id="generation-role")


@pytest.mark.asyncio
async def test_journal_verifies_completed_task_and_warns_on_missing_families(
    tmp_path, capsys
) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [row(operation_id, "started"), row(operation_id, "accepted", resource_id="artifact")],
    )
    artifact = Artifact("artifact", "audio")
    client = Client([[artifact]] * 8, statuses=["completed"])
    result = await verify.verify_journal(
        client,
        notebook_id="generation-role",
        journal_path=journal,
        timeout=240,
        minimum_discovery_window=0,
        quiet_polls=1,
        poll_interval=0,
    )
    assert result["completed"] == 1
    assert "missing artifact families" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_journal_graces_then_removes_accepted_never_appears_and_adopts_crash_row(
    tmp_path,
) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [row(operation_id, "started"), row(operation_id, "accepted", resource_id="missing")],
    )
    with pytest.raises(verify.RemovedArtifactError):
        await verify.verify_journal(
            Client([[]], statuses=["not_found"] * 5),
            notebook_id="generation-role",
            journal_path=journal,
            timeout=240,
            minimum_discovery_window=0,
            quiet_polls=1,
            poll_interval=0,
        )
    write_journal(journal, [row(operation_id, "started")])
    adopted = await verify.verify_journal(
        Client([[Artifact("orphan", "audio")]] * 6),
        notebook_id="generation-role",
        journal_path=journal,
        timeout=240,
        minimum_discovery_window=0,
        quiet_polls=1,
        poll_interval=0,
    )
    assert adopted["accepted"] == 1


@pytest.mark.asyncio
async def test_transient_not_found_recovers_before_grace(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [row(operation_id, "started"), row(operation_id, "accepted", resource_id="artifact")],
    )
    artifact = Artifact("artifact", "audio")
    client = Client([[artifact], [], [], [artifact]])
    result = await verify.verify_journal(
        client,
        notebook_id="generation-role",
        journal_path=journal,
        timeout=240,
        minimum_discovery_window=0,
        quiet_polls=1,
        poll_interval=0,
    )
    assert result["completed"] == 1
    assert client.artifacts.poll_ids == []


@pytest.mark.asyncio
async def test_note_backed_resource_uses_public_persistent_lookup(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [
            row(operation_id, "started", family="mind_map", id_kind="note_mind_map"),
            row(
                operation_id,
                "persisted",
                resource_id="note-map",
                family="mind_map",
                id_kind="note_mind_map",
            ),
            row(
                operation_id,
                "completed",
                resource_id="note-map",
                family="mind_map",
                id_kind="note_mind_map",
            ),
        ],
    )
    artifact = Artifact("note-map", "mind_map")
    client = Client([[artifact]] * 8)
    result = await verify.verify_journal(
        client,
        notebook_id="generation-role",
        journal_path=journal,
        timeout=240,
        minimum_discovery_window=0,
        quiet_polls=1,
        poll_interval=0,
    )
    assert result["completed"] == 1
    assert client.artifacts.get_ids == []
    assert client.artifacts.poll_ids == []


@pytest.mark.asyncio
async def test_note_backed_resource_requires_repeated_misses_before_removal(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [
            row(operation_id, "started", family="mind_map", id_kind="note_mind_map"),
            row(
                operation_id,
                "persisted",
                resource_id="note-map",
                family="mind_map",
                id_kind="note_mind_map",
            ),
        ],
    )
    artifact = Artifact("note-map", "mind_map")
    client = Client([[artifact], [], [artifact]])
    result = await verify.verify_journal(
        client,
        notebook_id="generation-role",
        journal_path=journal,
        timeout=240,
        minimum_discovery_window=0,
        quiet_polls=1,
        poll_interval=0,
    )
    assert result["completed"] == 1

    with pytest.raises(verify.RemovedArtifactError, match="delisted"):
        await verify.verify_journal(
            Client([[artifact], [], []]),
            notebook_id="generation-role",
            journal_path=journal,
            timeout=240,
            minimum_discovery_window=0,
            quiet_polls=1,
            poll_interval=0,
            not_found_grace=2,
        )


@pytest.mark.asyncio
async def test_unjournaled_interactive_row_is_settled_as_studio_backing(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [row(operation_id, "started", family="mind_map", id_kind="studio_task")],
    )
    artifact = Artifact("interactive", "mind_map", interactive=True)
    client = Client([[artifact]] * 8, statuses=["completed"])
    result = await verify.verify_journal(
        client,
        notebook_id="generation-role",
        journal_path=journal,
        timeout=240,
        minimum_discovery_window=0,
        quiet_polls=1,
        poll_interval=0,
    )
    assert result["completed"] == 1
    assert client.artifacts.poll_ids == []
    assert client.artifacts.get_ids == []


@pytest.mark.asyncio
async def test_started_only_test_owned_operation_cannot_adopt_discovered_artifact(
    tmp_path,
) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [
            row(
                operation_id,
                "started",
                family="mind_map",
                lifecycle="test_owned",
            )
        ],
    )
    leaked = Artifact("interactive", "mind_map", interactive=True)

    with pytest.raises(verify.JournalError, match="test-owned operation has no verified deletion"):
        await verify.verify_journal(
            Client([[leaked]]),
            notebook_id="generation-role",
            journal_path=journal,
            timeout=240,
            minimum_discovery_window=0,
            quiet_polls=1,
            poll_interval=0,
        )


@pytest.mark.asyncio
async def test_late_quota_created_artifact_still_requires_verified_deletion(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [
            row(
                operation_id,
                "started",
                family="mind_map",
                lifecycle="test_owned",
            ),
            row(
                operation_id,
                "quota_no_commit_observed",
                family="mind_map",
                lifecycle="test_owned",
                reason="post_create_reconciliation",
            ),
        ],
    )
    late_artifact = Artifact("late-interactive", "mind_map", interactive=True)

    with pytest.raises(verify.JournalError, match="test-owned operation has no verified deletion"):
        await verify.verify_journal(
            Client([[late_artifact]]),
            notebook_id="generation-role",
            journal_path=journal,
            timeout=240,
            minimum_discovery_window=0,
            quiet_polls=1,
            poll_interval=0,
        )


@pytest.mark.asyncio
async def test_quota_created_artifact_is_rechecked_during_settlement(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    quota_operation, settled_operation = str(uuid.uuid4()), str(uuid.uuid4())
    write_journal(
        journal,
        [
            row(
                quota_operation,
                "started",
                family="mind_map",
                lifecycle="test_owned",
            ),
            row(
                quota_operation,
                "quota_no_commit_observed",
                family="mind_map",
                lifecycle="test_owned",
                reason="post_create_reconciliation",
            ),
            row(settled_operation, "started"),
            row(settled_operation, "accepted", resource_id="settled-audio"),
        ],
    )
    settled = Artifact("settled-audio", "audio")
    late_artifact = Artifact("late-interactive", "mind_map", interactive=True)

    with pytest.raises(verify.JournalError, match="test-owned operation has no verified deletion"):
        await verify.verify_journal(
            Client([[settled], [settled, late_artifact]]),
            notebook_id="generation-role",
            journal_path=journal,
            timeout=240,
            minimum_discovery_window=0,
            quiet_polls=1,
            poll_interval=0,
        )


@pytest.mark.asyncio
async def test_unmatched_started_requires_matching_public_backing(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [row(operation_id, "started", family="mind_map", id_kind="studio_task")],
    )
    note_backed = Artifact("note-map", "mind_map")
    with pytest.raises(verify.JournalError, match="uniquely reconciled"):
        await verify.verify_journal(
            Client([[note_backed]] * 4),
            notebook_id="generation-role",
            journal_path=journal,
            timeout=240,
            minimum_discovery_window=0,
            quiet_polls=1,
            poll_interval=0,
        )


@pytest.mark.asyncio
async def test_inventory_artifact_requires_a_matching_journal_start(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [
            row(operation_id, "started"),
            row(operation_id, "accepted", resource_id="tracked"),
        ],
    )
    tracked = Artifact("tracked", "audio")
    unjournaled = Artifact("unjournaled", "video")

    with pytest.raises(verify.JournalError, match="no matching journal start"):
        await verify.verify_journal(
            Client([[tracked, unjournaled]]),
            notebook_id="generation-role",
            journal_path=journal,
            timeout=240,
            minimum_discovery_window=0,
            quiet_polls=1,
            poll_interval=0,
        )


@pytest.mark.asyncio
async def test_late_inventory_artifact_requires_a_matching_journal_start(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [
            row(operation_id, "started"),
            row(operation_id, "accepted", resource_id="tracked"),
        ],
    )
    tracked = Artifact("tracked", "audio")
    late_unjournaled = Artifact("late-unjournaled", "video")

    with pytest.raises(verify.JournalError, match="no matching journal start"):
        await verify.verify_journal(
            Client([[tracked], [tracked, late_unjournaled]]),
            notebook_id="generation-role",
            journal_path=journal,
            timeout=240,
            minimum_discovery_window=0,
            quiet_polls=1,
            poll_interval=0,
        )


@pytest.mark.asyncio
async def test_accepted_task_that_is_delisted_fails_even_if_poll_reports_completed(
    tmp_path,
) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [row(operation_id, "started"), row(operation_id, "accepted", resource_id="artifact")],
    )
    artifact = Artifact("artifact", "audio")
    with pytest.raises(verify.RemovedArtifactError, match="delisted"):
        await verify.verify_journal(
            Client([[artifact], []]),
            notebook_id="generation-role",
            journal_path=journal,
            timeout=240,
            minimum_discovery_window=0,
            quiet_polls=1,
            poll_interval=0,
        )


@pytest.mark.asyncio
async def test_terminal_failed_majority_uses_only_terminal_denominator(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    rows = []
    artifacts = []
    for index, family in enumerate(("audio", "video", "report")):
        operation_id = str(uuid.uuid4())
        resource_id = f"artifact-{index}"
        rows.extend(
            [
                row(operation_id, "started", family=family),
                row(operation_id, "accepted", resource_id=resource_id, family=family),
            ]
        )
        status = "failed" if index < 2 else "completed"
        artifacts.append(Artifact(resource_id, family, status))
    write_journal(journal, rows)
    with pytest.raises(verify.FailedMajorityError):
        await verify.verify_journal(
            Client([artifacts] * 10),
            notebook_id="generation-role",
            journal_path=journal,
            timeout=240,
            minimum_discovery_window=0,
            quiet_polls=1,
            poll_interval=0,
        )


@pytest.mark.asyncio
async def test_settlement_uses_one_inventory_snapshot_for_all_tasks(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    rows = []
    artifacts = []
    for index, family in enumerate(("audio", "video", "report")):
        operation_id = str(uuid.uuid4())
        resource_id = f"artifact-{index}"
        rows.extend(
            [
                row(operation_id, "started", family=family),
                row(operation_id, "accepted", resource_id=resource_id, family=family),
            ]
        )
        artifacts.append(Artifact(resource_id, family))
    write_journal(journal, rows)
    client = Client([artifacts, artifacts])
    result = await verify.verify_journal(
        client,
        notebook_id="generation-role",
        journal_path=journal,
        timeout=240,
        minimum_discovery_window=0,
        quiet_polls=1,
        poll_interval=0,
    )
    assert result["completed"] == 3
    assert client.artifacts.list_calls == 2
    assert client.artifacts.poll_ids == []


@pytest.mark.asyncio
async def test_late_visibility_resets_discovery_quiet_window(tmp_path) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(journal, [row(operation_id, "started")])
    artifact = Artifact("late", "audio")
    client = Client([[], [], [artifact], [artifact], [artifact], [artifact]])
    clock = Clock()
    result = await verify.verify_journal(
        client,
        notebook_id="generation-role",
        journal_path=journal,
        timeout=240,
        clock=clock,
        sleep=clock.sleep,
        minimum_discovery_window=0,
        quiet_polls=3,
        poll_interval=30,
    )
    assert result["accepted"] == 1
    assert clock.value >= 120


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "in_progress", "unknown", "pending_review"])
async def test_every_nonterminal_state_times_out(tmp_path, status: str) -> None:
    journal = tmp_path / "journal.jsonl"
    operation_id = str(uuid.uuid4())
    write_journal(
        journal,
        [row(operation_id, "started"), row(operation_id, "accepted", resource_id="artifact")],
    )
    artifact = Artifact("artifact", "audio", status)
    clock = Clock()
    with pytest.raises(verify.SettlementTimeoutError):
        await verify.verify_journal(
            Client([[artifact]] * 30, statuses=[status] * 30),
            notebook_id="generation-role",
            journal_path=journal,
            timeout=240,
            clock=clock,
            sleep=clock.sleep,
            minimum_discovery_window=0,
            quiet_polls=1,
            poll_interval=30,
        )


@pytest.mark.asyncio
async def test_insufficient_time_fails_without_opening_inventory(tmp_path) -> None:
    client = Client([[Artifact("never-read", "audio")]])
    with pytest.raises(verify.InsufficientTimeError):
        await verify.verify_journal(
            client,
            notebook_id="generation-role",
            journal_path=tmp_path / "missing",
            timeout=239,
        )
    assert client.artifacts.list_calls == 0


def test_manifest_requires_unique_confirmed_generation_role(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "copies": [
                    {
                        "role": "generation",
                        "status": "confirmed",
                        "notebook_id": "managed-generation",
                    }
                ],
            }
        )
    )
    if os.name != "nt":
        manifest.chmod(0o600)
    assert verify.generation_id_from_manifest(manifest, "generation") == "managed-generation"
    with pytest.raises(verify.ConfigurationError):
        verify.generation_id_from_manifest(manifest, "reference")

    document = json.loads(manifest.read_text())
    document["copies"][0]["notebook_id"] = ""
    manifest.write_text(json.dumps(document))
    if os.name != "nt":
        manifest.chmod(0o600)
    with pytest.raises(verify.ConfigurationError):
        verify.generation_id_from_manifest(manifest, "generation")


def test_package_telemetry_uses_inventory_compat_after_detached_workflow_removal() -> None:
    workflow_dir = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    assert not (workflow_dir / "verify-artifacts.yml").exists()
    workflow = (workflow_dir / "verify-package.yml").read_text()
    assert "scripts/verify_ci_artifacts.py" in workflow
    assert "--mode inventory-compat" in workflow
    assert "NOTEBOOKLM_GENERATION_NOTEBOOK_ID" in workflow
