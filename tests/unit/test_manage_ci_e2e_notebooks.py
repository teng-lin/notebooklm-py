"""Unit tests for the disposable CI E2E notebook lifecycle manager."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import manage_ci_e2e_notebooks as lifecycle  # noqa: E402
from _ci_e2e_notebooks import (  # noqa: E402
    AtomicJSONStore,
    ManifestError,
    build_title,
    github_env_lines,
    new_copy_row,
    new_manifest,
    parse_title,
    validate_manifest,
)
from manage_ci_e2e_notebooks import (  # noqa: E402
    TEMPLATE_ID_ENV,
    CleanupError,
    ContractError,
    CopyUnresolvedError,
    NotebookLifecycleManager,
    PersistenceError,
    RetryPolicy,
    _safe_exception_name,
    load_prepared_contract,
    load_template_contract,
)

from notebooklm import (  # noqa: E402
    AuthError,
    ChatError,
    MindMapKind,
    NotebookNotFoundError,
    RateLimitError,
    ServerError,
    SharePermission,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (  # noqa: E402
    chat_pb2,
)

TEMPLATE_ID = "template-id"
TEMPLATE_TITLE = "notebooklm-py E2E template v1"
FINGERPRINT = "a" * 64


@dataclass
class FakeClock:
    value: float = 0.0

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += seconds


def _source(index: int, *, ready: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"source-{index}",
        title=f"Distinct topic {index}",
        kind="pasted_text",
        is_ready=ready,
    )


def _artifacts() -> list[SimpleNamespace]:
    result = [
        SimpleNamespace(
            id=f"artifact-{family}",
            title=f"Example {family}",
            kind=family,
            is_completed=True,
            is_interactive_mind_map=False,
        )
        for family in (
            "audio",
            "video",
            "report",
            "quiz",
            "flashcards",
            "infographic",
            "slide_deck",
            "data_table",
        )
    ]
    result.append(
        SimpleNamespace(
            id="artifact-interactive-map",
            title="Example mind map",
            kind="mind_map",
            is_completed=True,
            is_interactive_mind_map=True,
        )
    )
    return result


class FakeNotebooks:
    def __init__(self) -> None:
        self.items: dict[str, SimpleNamespace] = {
            TEMPLATE_ID: SimpleNamespace(
                id=TEMPLATE_ID,
                title=TEMPLATE_TITLE,
                role=SharePermission.VIEWER,
                is_owner=False,
                created_at=datetime.now(timezone.utc) - timedelta(days=90),
            )
        }
        self.copy_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []
        self.get_calls: list[str] = []
        self.list_error: BaseException | None = None
        self.list_script: deque[object] = deque()
        self.get_errors: dict[str, BaseException] = {}
        self.delete_failures: dict[str, int] = {}
        self.delete_noop_ids: set[str] = set()
        self.copy_error: BaseException | None = None
        self.copy_error_commits = False
        self.returned_id: str | None = None
        self.returned_title: str | None = None
        self.created_role: SharePermission | None = SharePermission.OWNER
        self.client: FakeClient | None = None

    async def list(self) -> list[SimpleNamespace]:
        if self.list_script:
            value = self.list_script.popleft()
            if isinstance(value, BaseException):
                raise value
            return list(value)  # type: ignore[arg-type]
        if self.list_error is not None:
            raise self.list_error
        return list(self.items.values())

    async def get(self, notebook_id: str) -> SimpleNamespace:
        self.get_calls.append(notebook_id)
        error = self.get_errors.get(notebook_id)
        if error is not None:
            raise error
        try:
            return self.items[notebook_id]
        except KeyError as exc:
            raise NotebookNotFoundError(notebook_id) from exc

    def _commit(self, title: str) -> SimpleNamespace:
        notebook_id = f"copy-{len(self.copy_calls)}"
        notebook = SimpleNamespace(
            id=notebook_id,
            title=title,
            role=self.created_role,
            is_owner=True,
            created_at=datetime.now(timezone.utc),
        )
        self.items[notebook_id] = notebook
        assert self.client is not None
        self.client.sources.by_notebook[notebook_id] = deepcopy(
            self.client.sources.by_notebook[TEMPLATE_ID]
        )
        self.client.artifacts.by_notebook[notebook_id] = deepcopy(
            self.client.artifacts.by_notebook[TEMPLATE_ID]
        )
        self.client.notes.by_notebook[notebook_id] = []
        return notebook

    async def copy(self, template_id: str, title: str) -> SimpleNamespace:
        self.copy_calls.append((template_id, title))
        committed = (
            self._commit(title) if self.copy_error_commits or self.copy_error is None else None
        )
        if self.copy_error is not None:
            raise self.copy_error
        assert committed is not None
        return SimpleNamespace(
            id=self.returned_id if self.returned_id is not None else committed.id,
            title=self.returned_title if self.returned_title is not None else committed.title,
            role=committed.role,
        )

    async def delete(self, notebook_id: str) -> None:
        self.delete_calls.append(notebook_id)
        remaining = self.delete_failures.get(notebook_id, 0)
        if remaining:
            self.delete_failures[notebook_id] = remaining - 1
            raise RuntimeError("transient delete failure containing notebook-id")
        if notebook_id not in self.delete_noop_ids:
            self.items.pop(notebook_id, None)


class FakeSources:
    def __init__(self) -> None:
        self.by_notebook: dict[str, list[SimpleNamespace]] = {
            TEMPLATE_ID: [_source(1), _source(2), _source(3)]
        }
        self.error: BaseException | None = None

    async def list(self, notebook_id: str, *, strict: bool = False) -> list[SimpleNamespace]:
        assert strict
        if self.error is not None:
            raise self.error
        return list(self.by_notebook.get(notebook_id, []))


class FakeArtifacts:
    def __init__(self) -> None:
        self.by_notebook: dict[str, list[SimpleNamespace]] = {TEMPLATE_ID: _artifacts()}
        self.list_script: deque[object] = deque()
        self.delete_error_ids: set[str] = set()
        self.delete_calls: list[tuple[str, str]] = []

    async def list(self, notebook_id: str) -> list[SimpleNamespace]:
        if self.list_script:
            value = self.list_script.popleft()
            if isinstance(value, BaseException):
                raise value
            return list(value)  # type: ignore[arg-type]
        return list(self.by_notebook.get(notebook_id, []))

    async def delete(self, notebook_id: str, artifact_id: str) -> None:
        self.delete_calls.append((notebook_id, artifact_id))
        if artifact_id in self.delete_error_ids:
            raise RuntimeError("opaque deletion failure with leaked-id")
        self.by_notebook[notebook_id] = [
            item for item in self.by_notebook.get(notebook_id, []) if item.id != artifact_id
        ]


class FakeNotes:
    def __init__(self) -> None:
        self.by_notebook: dict[str, list[SimpleNamespace]] = {TEMPLATE_ID: []}
        self.get_overrides: dict[str, SimpleNamespace] = {}

    async def list(self, notebook_id: str) -> list[SimpleNamespace]:
        return list(self.by_notebook.get(notebook_id, []))

    async def create(self, notebook_id: str, title: str, content: str) -> SimpleNamespace:
        note = SimpleNamespace(id=f"note-{notebook_id}", title=title, content=content)
        self.by_notebook.setdefault(notebook_id, []).append(note)
        return note

    async def get(self, notebook_id: str, note_id: str) -> SimpleNamespace:
        if note_id in self.get_overrides:
            return self.get_overrides[note_id]
        for note in self.by_notebook.get(notebook_id, []):
            if note.id == note_id:
                return note
        raise RuntimeError("note missing")

    async def delete(self, notebook_id: str, note_id: str) -> None:
        self.by_notebook[notebook_id] = [
            item for item in self.by_notebook.get(notebook_id, []) if item.id != note_id
        ]


class FakeMindMaps:
    def __init__(self, artifacts: FakeArtifacts) -> None:
        self.artifacts = artifacts
        self.extra: dict[str, list[SimpleNamespace]] = {}
        self.delete_calls: list[tuple[str, str, MindMapKind]] = []

    async def list(self, notebook_id: str) -> list[SimpleNamespace]:
        interactive = [
            SimpleNamespace(id=item.id, kind=MindMapKind.INTERACTIVE)
            for item in self.artifacts.by_notebook.get(notebook_id, [])
            if item.is_interactive_mind_map
        ]
        return interactive + list(self.extra.get(notebook_id, []))

    async def delete(self, notebook_id: str, mind_map_id: str, *, kind: MindMapKind) -> None:
        assert kind in {MindMapKind.INTERACTIVE, MindMapKind.NOTE_BACKED}
        self.delete_calls.append((notebook_id, mind_map_id, kind))
        self.extra[notebook_id] = [
            item for item in self.extra.get(notebook_id, []) if item.id != mind_map_id
        ]
        self.artifacts.by_notebook[notebook_id] = [
            item
            for item in self.artifacts.by_notebook.get(notebook_id, [])
            if item.id != mind_map_id
        ]


class FakeChat:
    def __init__(self) -> None:
        self.seeded: set[str] = set()
        self.questions: dict[str, str] = {}
        self.ask_error: BaseException | None = None
        self.turns_error: BaseException | None = None
        self.turns_response: object | None = None

    async def ask(self, notebook_id: str, question: str) -> SimpleNamespace:
        assert question
        if self.ask_error is not None:
            raise self.ask_error
        self.seeded.add(notebook_id)
        self.questions[notebook_id] = question
        return SimpleNamespace(answer="seeded")

    async def get_conversation_id(self, notebook_id: str) -> str | None:
        return f"conversation-{notebook_id}" if notebook_id in self.seeded else None

    async def get_history(self, notebook_id: str) -> list[tuple[str, str]]:
        return [(self.questions[notebook_id], "seed answer")] if notebook_id in self.seeded else []

    async def get_conversation_turns(
        self,
        notebook_id: str,
        conversation_id: str,
        limit: int,
    ) -> object:
        assert limit == 2
        if self.turns_error is not None:
            raise self.turns_error
        if notebook_id in self.seeded and conversation_id:
            if self.turns_response is not None:
                return self.turns_response
            return [{"question": "seed question"}, {"answer": "seed answer"}]
        return []


class FakeClient:
    def __init__(self) -> None:
        self.notebooks = FakeNotebooks()
        self.sources = FakeSources()
        self.artifacts = FakeArtifacts()
        self.notes = FakeNotes()
        self.mind_maps = FakeMindMaps(self.artifacts)
        self.chat = FakeChat()
        self.notebooks.client = self


class RecordingStore(AtomicJSONStore):
    def __init__(self, path: Path, *, fail_when: str | None = None) -> None:
        super().__init__(path, runner_temp=path.parent)
        self.snapshots: list[dict[str, Any]] = []
        self.fail_when = fail_when

    def write(self, manifest: dict[str, Any], *, template_id: str | None = None) -> None:
        self.snapshots.append(deepcopy(manifest))
        rows = manifest.get("copies", [])
        row = rows[-1] if rows else {}
        if self.fail_when == "candidate" and row.get("candidate_notebook_id"):
            raise OSError("candidate persistence failed with notebook id")
        if self.fail_when == "confirmation" and row.get("status") == "confirmed":
            raise OSError("confirmation persistence failed with notebook id")
        if self.fail_when == "reconciliation" and row.get("status") == "reconciled":
            raise OSError("reconciliation persistence failed with notebook id")
        super().write(manifest, template_id=template_id)


@pytest.fixture()
def contracts() -> tuple[dict[str, Any], dict[str, Any]]:
    template, _fingerprint = load_template_contract(
        REPO_ROOT / "tests" / "fixtures" / "e2e_template_contract.json"
    )
    prepared = load_prepared_contract(
        REPO_ROOT / "tests" / "fixtures" / "e2e_prepared_role_contract.json"
    )
    return template, prepared


def _manager(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
    *,
    client: FakeClient | None = None,
    clock: FakeClock | None = None,
    store: AtomicJSONStore | None = None,
    nonces: list[str] | None = None,
    reconcile_timeout: float = 0,
) -> tuple[NotebookLifecycleManager, FakeClient, AtomicJSONStore, FakeClock]:
    client = client or FakeClient()
    clock = clock or FakeClock()
    store = store or AtomicJSONStore(tmp_path / "manifest.json", runner_temp=tmp_path)
    nonce_values = iter(nonces or [f"{index:032x}" for index in range(1, 20)])
    manager = NotebookLifecycleManager(
        client,
        template_id=TEMPLATE_ID,
        store=store,
        template_contract=contracts[0],
        prepared_contract=contracts[1],
        retry_policy=RetryPolicy(attempts=1, base_delay=0, max_delay=0),
        reconcile_timeout=reconcile_timeout,
        clock=clock.monotonic,
        sleep=clock.sleep,
        nonce=lambda: next(nonce_values),
    )
    return manager, client, store, clock


async def _provision(
    manager: NotebookLifecycleManager,
    tmp_path: Path,
    *,
    mode: str = "full",
    mask: Any = None,
) -> dict[str, Any]:
    lane = {
        "full": "nightly-web-ubuntu",
        "readonly": "nightly-readonly-windows",
        "rpc": "rpc-health-web",
    }[mode]
    return await manager.provision(
        run_id="100",
        run_attempt="2",
        lane=lane,
        mode=mode,
        account_slot="A",
        backend="web",
        template_fingerprint=FINGERPRINT,
        github_env=tmp_path / "github-env",
        mask=mask,
    )


def test_title_grammar_is_exact_and_normative() -> None:
    title = build_title("10", "2", "nightly-web-ubuntu", "reference", "a" * 32)
    parsed = parse_title(title)
    assert parsed is not None
    assert parsed.run_id == "10"
    assert parsed.role == "reference"
    for bad in (
        title.upper(),
        title.replace("/10/", "/x/"),
        title.replace("reference", "arbitrary"),
        title + "/extra",
    ):
        assert parse_title(bad) is None


@pytest.mark.parametrize("lane", ["nightly-web-windows", "nightly-android-windows"])
def test_legacy_lane_titles_remain_sweepable_but_cannot_authorize_new_manifests(lane: str) -> None:
    title = build_title("10", "2", lane, "reference", "a" * 32)
    assert parse_title(title) is not None
    with pytest.raises(ManifestError, match="lane is not allowlisted"):
        new_manifest(
            run_id="10",
            run_attempt="2",
            lane=lane,
            mode="full",
            account_slot="A",
            backend="web",
            template_fingerprint=FINGERPRINT,
        )


def test_manifest_validation_fails_closed_on_version_role_id_and_template() -> None:
    manifest = new_manifest(
        run_id="1",
        run_attempt="1",
        lane="nightly-web-ubuntu",
        mode="full",
        account_slot="A",
        backend="web",
        template_fingerprint=FINGERPRINT,
    )
    manifest["copies"].append(
        new_copy_row(
            role="reference",
            title=build_title("1", "1", "nightly-web-ubuntu", "reference", "b" * 32),
        )
    )
    validate_manifest(manifest, template_id=TEMPLATE_ID)

    for mutate in (
        lambda value: value.update(version=99),
        lambda value: value["copies"][0].update(role="generation"),
        lambda value: value["copies"][0].update(candidate_notebook_id="!"),
        lambda value: value["copies"][0].update(candidate_notebook_id=TEMPLATE_ID),
    ):
        bad = deepcopy(manifest)
        mutate(bad)
        with pytest.raises(ManifestError):
            validate_manifest(bad, template_id=TEMPLATE_ID)


@pytest.mark.parametrize(
    ("lane", "mode"),
    [
        ("nightly-readonly-windows", "full"),
        ("nightly-web-ubuntu", "readonly"),
        ("nightly-android-macos", "readonly"),
    ],
)
def test_manifest_rejects_non_normative_nightly_lane_modes(lane: str, mode: str) -> None:
    with pytest.raises(ManifestError, match="normative combination"):
        new_manifest(
            run_id="1",
            run_attempt="1",
            lane=lane,
            mode=mode,
            account_slot="A",
            backend="web",
            template_fingerprint=FINGERPRINT,
        )


@pytest.mark.parametrize(
    ("lane", "backend"),
    [
        ("nightly-web-ubuntu", "android"),
        ("nightly-android-macos", "web"),
        ("nightly-readonly-windows", "android"),
    ],
)
def test_manifest_rejects_non_normative_nightly_lane_backends(lane: str, backend: str) -> None:
    mode = "readonly" if lane == "nightly-readonly-windows" else "full"
    with pytest.raises(ManifestError, match="lane and backend"):
        new_manifest(
            run_id="1",
            run_attempt="1",
            lane=lane,
            mode=mode,
            account_slot="A",
            backend=backend,
            template_fingerprint=FINGERPRINT,
        )


def test_manifest_rejects_duplicate_role_ids() -> None:
    manifest = new_manifest(
        run_id="1",
        run_attempt="1",
        lane="nightly-web-ubuntu",
        mode="full",
        account_slot="A",
        backend="web",
        template_fingerprint=FINGERPRINT,
    )
    for role, nonce in (("reference", "1" * 32), ("generation", "2" * 32)):
        row = new_copy_row(
            role=role,
            title=build_title("1", "1", "nightly-web-ubuntu", role, nonce),
        )
        row.update(status="confirmed", candidate_notebook_id="same-id", notebook_id="same-id")
        manifest["copies"].append(row)
    with pytest.raises(ManifestError, match="duplicated"):
        validate_manifest(manifest, template_id=TEMPLATE_ID)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_atomic_store_enforces_posix_modes_and_fsyncs_parent(tmp_path: Path) -> None:
    fsynced: list[int] = []
    store = AtomicJSONStore(tmp_path / "private" / "manifest.json", fsync=fsynced.append)
    manifest = new_manifest(
        run_id="1",
        run_attempt="1",
        lane="nightly-web-ubuntu",
        mode="full",
        account_slot="A",
        backend="web",
        template_fingerprint=FINGERPRINT,
    )
    store.write(manifest, template_id=TEMPLATE_ID)
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert len(fsynced) == 2
    assert store.read(template_id=TEMPLATE_ID) == manifest


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_atomic_store_never_chmods_an_existing_caller_owned_parent(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o750)
    store = AtomicJSONStore(parent / "manifest.json", windows=False)
    manifest = new_manifest(
        run_id="1",
        run_attempt="1",
        lane="nightly-web-ubuntu",
        mode="full",
        account_slot="A",
        backend="web",
        template_fingerprint=FINGERPRINT,
    )

    with pytest.raises(ManifestError, match="parent mode must be 0700"):
        store.write(manifest, template_id=TEMPLATE_ID)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o750


def test_windows_store_rejects_escape_and_accepts_isolated_regular_file(tmp_path: Path) -> None:
    manifest = new_manifest(
        run_id="1",
        run_attempt="1",
        lane="nightly-web-ubuntu",
        mode="full",
        account_slot="A",
        backend="web",
        template_fingerprint=FINGERPRINT,
    )
    store = AtomicJSONStore(
        tmp_path / "runner" / "manifest.json", runner_temp=tmp_path, windows=True
    )
    store.write(manifest)
    assert store.read() == manifest
    outside = AtomicJSONStore(tmp_path.parent / "outside.json", runner_temp=tmp_path, windows=True)
    with pytest.raises(ManifestError, match="RUNNER_TEMP"):
        outside.write(manifest)


def test_posix_store_rejects_runner_escape_before_creating_parent(tmp_path: Path) -> None:
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir(mode=0o700)
    outside_parent = tmp_path / "outside"
    store = AtomicJSONStore(
        outside_parent / "manifest.json",
        runner_temp=runner_temp,
        windows=False,
    )
    manifest = new_manifest(
        run_id="1",
        run_attempt="1",
        lane="nightly-web-ubuntu",
        mode="full",
        account_slot="A",
        backend="web",
        template_fingerprint=FINGERPRINT,
    )
    with pytest.raises(ManifestError, match="RUNNER_TEMP"):
        store.write(manifest)
    assert not outside_parent.exists()


@pytest.mark.asyncio
async def test_full_provision_creates_three_roles_and_publishes_activation_last(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, clock = _manager(tmp_path, contracts)
    masked: list[str] = []
    manifest = await _provision(manager, tmp_path, mask=masked.append)

    assert [row["role"] for row in manifest["copies"]] == [
        "reference",
        "generation",
        "multi-source",
    ]
    assert len(client.notebooks.copy_calls) == 3
    assert len({row["notebook_id"] for row in manifest["copies"]}) == 3
    assert all(row["prepared"] for row in manifest["copies"])
    assert clock.value == 180
    assert set(masked) == {row["notebook_id"] for row in manifest["copies"]}
    lines = (tmp_path / "github-env").read_text().splitlines()
    assert lines[-1] == "NOTEBOOKLM_E2E_MANAGED_COPIES=1"
    assert lines[-2] == "NOTEBOOKLM_E2E_REFERENCE_PREPARED=1"
    assert "NOTEBOOKLM_E2E_MANAGED_MODE=full" in lines


@pytest.mark.asyncio
async def test_rpc_provision_creates_one_role_and_dual_fallback_binding(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    manifest = await _provision(manager, tmp_path, mode="rpc")
    assert [row["role"] for row in manifest["copies"]] == ["rpc"]
    assert len(client.notebooks.copy_calls) == 1
    lines = (tmp_path / "github-env").read_text().splitlines()
    read_only = next(line.split("=", 1)[1] for line in lines if "READ_ONLY" in line)
    generation = next(line.split("=", 1)[1] for line in lines if "GENERATION" in line)
    assert read_only == generation
    assert "NOTEBOOKLM_E2E_MANAGED_COPIES=1" not in lines
    assert lines[-1] == "NOTEBOOKLM_E2E_MANAGED_MODE=rpc"


@pytest.mark.asyncio
async def test_readonly_provision_creates_only_a_managed_reference_copy(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    manifest = await _provision(manager, tmp_path, mode="readonly")
    assert [row["role"] for row in manifest["copies"]] == ["reference"]
    assert len(client.notebooks.copy_calls) == 1
    lines = (tmp_path / "github-env").read_text().splitlines()
    assert lines == [
        "NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID=copy-1",
        "NOTEBOOKLM_E2E_MANAGED_MODE=readonly",
        "NOTEBOOKLM_E2E_REFERENCE_PREPARED=1",
        "NOTEBOOKLM_E2E_MANAGED_COPIES=1",
    ]


@pytest.mark.parametrize(
    ("backend", "lane"),
    [("web", "nightly-web-ubuntu"), ("android", "nightly-android-macos")],
)
@pytest.mark.asyncio
async def test_full_mode_uses_three_distinct_roles_on_designated_os_lanes(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
    backend: str,
    lane: str,
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    manifest = await manager.provision(
        run_id="100",
        run_attempt="2",
        lane=lane,
        mode="full",
        account_slot="B",
        backend=backend,
        template_fingerprint=FINGERPRINT,
        github_env=tmp_path / "github-env",
    )
    assert manifest["backend"] == backend
    assert len(client.notebooks.copy_calls) == 3
    assert len({row["notebook_id"] for row in manifest["copies"]}) == 3


@pytest.mark.asyncio
async def test_android_protobuf_turns_prepare_and_validate_reference_role(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    notebook_id = "reference-copy"
    response = chat_pb2.ListChatTurnsResponse()
    response.chat_turns.add(observed_event_type=1, user_query_text="seed question")
    answer = response.chat_turns.add(observed_event_type=2)
    answer.act_on_sources_response.response.response = "seed answer"
    client.chat.turns_response = response

    prepared = await manager.prepare_reference(notebook_id)
    validated = await manager.validate_prepared_role(notebook_id, "reference")

    assert prepared == {"seeded_notes": 1, "history_pairs": 1}
    assert validated == prepared


@pytest.mark.asyncio
async def test_partial_preparation_never_publishes_managed_activation(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, store, _clock = _manager(tmp_path, contracts)
    masked: list[str] = []
    client.chat.ask_error = RuntimeError("seed failed with reference-id")
    with pytest.raises(RuntimeError):
        await _provision(manager, tmp_path, mask=masked.append)
    assert not (tmp_path / "github-env").exists()
    manifest = store.read(template_id=TEMPLATE_ID)
    assert len(manifest["copies"]) == 1
    assert manifest["copies"][0]["prepared"] is False
    assert masked == [manifest["copies"][0]["notebook_id"]]


@pytest.mark.asyncio
async def test_reference_validation_requires_exact_note_readback(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    notebook_id = "reference-copy"
    reference = contracts[1]["reference"]
    marker = SimpleNamespace(
        id="reference-note",
        title=reference["note_title"],
        content=reference["note_body"],
    )
    client.notes.by_notebook[notebook_id] = [marker]
    client.notes.get_overrides[marker.id] = SimpleNamespace(
        id=marker.id,
        title=marker.title,
        content="wrong readback",
    )
    client.chat.seeded.add(notebook_id)
    client.chat.questions[notebook_id] = reference["question"]

    with pytest.raises(ContractError, match="readback"):
        await manager.validate_prepared_role(notebook_id, "reference")


@pytest.mark.asyncio
async def test_reference_validation_rejects_duplicate_prepared_marker(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    notebook_id = "reference-copy"
    reference = contracts[1]["reference"]
    client.notes.by_notebook[notebook_id] = [
        SimpleNamespace(
            id=f"reference-note-{index}",
            title=reference["note_title"],
            content=reference["note_body"],
        )
        for index in range(2)
    ]
    client.chat.seeded.add(notebook_id)
    client.chat.questions[notebook_id] = reference["question"]

    with pytest.raises(ContractError, match="ambiguous"):
        await manager.validate_prepared_role(notebook_id, "reference")


def test_github_env_refuses_partial_or_unprepared_publication() -> None:
    with pytest.raises(ManifestError):
        github_env_lines("full", [])
    row = new_copy_row(
        role="rpc",
        title=build_title("1", "1", "rpc-health-web", "rpc", "1" * 32),
    )
    row.update(status="confirmed", notebook_id="copy-one", candidate_notebook_id="copy-one")
    with pytest.raises(ManifestError):
        github_env_lines("rpc", [row])


@pytest.mark.asyncio
async def test_preexisting_exact_title_forces_new_nonce_before_one_dispatch(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    first = "1" * 32
    second = "2" * 32
    manager, client, _store, _clock = _manager(
        tmp_path,
        contracts,
        nonces=[first, second, "3" * 32, "4" * 32, "5" * 32],
    )
    collision = build_title("100", "2", "rpc-health-web", "rpc", first)
    client.notebooks.items["old-copy"] = SimpleNamespace(
        id="old-copy",
        title=collision,
        role=SharePermission.OWNER,
        created_at=datetime.now(timezone.utc),
    )
    manifest = await _provision(manager, tmp_path, mode="rpc")
    assert len(client.notebooks.copy_calls) == 1
    assert manifest["copies"][0]["title"].endswith(second)


@pytest.mark.asyncio
async def test_baseline_listing_failure_aborts_before_dispatch(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    client.notebooks.list_error = RuntimeError("listing failed with secret-id")
    with pytest.raises(RuntimeError):
        await _provision(manager, tmp_path, mode="rpc")
    assert client.notebooks.copy_calls == []


@pytest.mark.asyncio
async def test_timeout_after_commit_reconciles_without_second_copy(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    client.notebooks.copy_error = TimeoutError("response contained copy-id")
    client.notebooks.copy_error_commits = True
    manifest = await _provision(manager, tmp_path, mode="rpc")
    assert len(client.notebooks.copy_calls) == 1
    assert manifest["copies"][0]["status"] == "reconciled"


@pytest.mark.parametrize(
    "copy_error",
    [
        RateLimitError("429 response leaked notebook-id"),
        ServerError("500 response leaked notebook-id", status_code=500),
        AuthError("revoked auth response leaked notebook-id"),
    ],
    ids=["rate-limit", "server", "auth"],
)
@pytest.mark.asyncio
async def test_failed_copy_categories_never_trigger_a_second_dispatch(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
    copy_error: BaseException,
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    client.notebooks.copy_error = copy_error
    with pytest.raises(CopyUnresolvedError):
        await _provision(manager, tmp_path, mode="rpc")
    assert len(client.notebooks.copy_calls) == 1


@pytest.mark.asyncio
async def test_multiple_exact_post_dispatch_matches_are_unresolved_without_deletion(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    nonce = "1" * 32
    title = build_title("100", "2", "rpc-health-web", "rpc", nonce)
    manager, client, _store, _clock = _manager(tmp_path, contracts, nonces=[nonce])
    client.notebooks.copy_error = TimeoutError("lost")
    client.notebooks.copy_error_commits = True
    client.notebooks.items["duplicate-copy"] = SimpleNamespace(
        id="duplicate-copy",
        title=title,
        role=SharePermission.OWNER,
        created_at=datetime.now(timezone.utc),
    )
    client.notebooks.list_script.append([client.notebooks.items[TEMPLATE_ID]])
    with pytest.raises(CopyUnresolvedError):
        await _provision(manager, tmp_path, mode="rpc")
    assert len(client.notebooks.copy_calls) == 1
    assert client.notebooks.delete_calls == []


@pytest.mark.asyncio
async def test_list_visibility_lag_reconciles_within_budget(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    nonce = "1" * 32
    title = build_title("100", "2", "rpc-health-web", "rpc", nonce)
    manager, client, _store, clock = _manager(
        tmp_path,
        contracts,
        nonces=[nonce],
        reconcile_timeout=2,
    )
    client.notebooks.copy_error = TimeoutError("lost")
    recovered = SimpleNamespace(
        id="lagged-copy",
        title=title,
        role=SharePermission.OWNER,
        created_at=datetime.now(timezone.utc),
    )
    client.notebooks.items["lagged-copy"] = recovered
    client.sources.by_notebook["lagged-copy"] = deepcopy(client.sources.by_notebook[TEMPLATE_ID])
    client.artifacts.by_notebook["lagged-copy"] = deepcopy(
        client.artifacts.by_notebook[TEMPLATE_ID]
    )
    client.notes.by_notebook["lagged-copy"] = []
    client.notebooks.list_script.extend([[client.notebooks.items[TEMPLATE_ID]], [], []])
    manifest = await _provision(manager, tmp_path, mode="rpc")
    assert manifest["copies"][0]["notebook_id"] == "lagged-copy"
    assert len(client.notebooks.copy_calls) == 1
    assert clock.value >= 1.5


@pytest.mark.asyncio
async def test_list_visibility_beyond_budget_stays_unresolved(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, clock = _manager(
        tmp_path,
        contracts,
        nonces=["1" * 32],
        reconcile_timeout=1,
    )
    client.notebooks.copy_error = TimeoutError("lost")
    client.notebooks.list_script.extend([[client.notebooks.items[TEMPLATE_ID]], [], [], [], []])
    with pytest.raises(CopyUnresolvedError):
        await _provision(manager, tmp_path, mode="rpc")
    assert len(client.notebooks.copy_calls) == 1
    assert clock.value == 1


@pytest.mark.asyncio
async def test_cancellation_after_copy_commit_persists_reconciliation_for_cleanup(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, store, _clock = _manager(tmp_path, contracts)
    client.notebooks.copy_error = asyncio.CancelledError()
    client.notebooks.copy_error_commits = True
    with pytest.raises(asyncio.CancelledError):
        await _provision(manager, tmp_path, mode="rpc")
    manifest = store.read(template_id=TEMPLATE_ID)
    assert manifest["copies"][0]["status"] == "reconciled"
    assert len(client.notebooks.copy_calls) == 1
    assert (await manager.cleanup())["deleted"] == 1


@pytest.mark.asyncio
async def test_unconfirmed_zero_match_stays_unresolved_and_never_copies_twice(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, store, _clock = _manager(tmp_path, contracts)
    client.notebooks.copy_error = TimeoutError("lost")
    with pytest.raises(CopyUnresolvedError):
        await _provision(manager, tmp_path, mode="rpc")
    assert len(client.notebooks.copy_calls) == 1
    row = store.read(template_id=TEMPLATE_ID)["copies"][0]
    assert row["status"] == "intent"
    assert row["last_error_category"] == "COPY_UNRESOLVED"


@pytest.mark.asyncio
async def test_decode_like_empty_candidate_reconciles_committed_title(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    client.notebooks.returned_id = ""
    manifest = await _provision(manager, tmp_path, mode="rpc")
    assert len(client.notebooks.copy_calls) == 1
    assert manifest["copies"][0]["status"] == "reconciled"


@pytest.mark.asyncio
async def test_candidate_is_durable_before_live_confirmation(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    store = RecordingStore(tmp_path / "manifest.json")
    manager, _client, _store, _clock = _manager(tmp_path, contracts, store=store)
    await _provision(manager, tmp_path, mode="rpc")
    candidate_index = next(
        index
        for index, manifest in enumerate(store.snapshots)
        if manifest["copies"] and manifest["copies"][-1]["candidate_notebook_id"] is not None
    )
    confirmed_index = next(
        index
        for index, manifest in enumerate(store.snapshots)
        if manifest["copies"] and manifest["copies"][-1]["status"] == "confirmed"
    )
    assert candidate_index < confirmed_index
    assert store.snapshots[candidate_index]["copies"][-1]["notebook_id"] is None


@pytest.mark.parametrize("fail_when", ["candidate", "confirmation"])
@pytest.mark.asyncio
async def test_persistence_failure_attempts_immediate_guarded_deletion(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
    fail_when: str,
) -> None:
    store = RecordingStore(tmp_path / "manifest.json", fail_when=fail_when)
    manager, client, _store, _clock = _manager(tmp_path, contracts, store=store)
    with pytest.raises(PersistenceError):
        await _provision(manager, tmp_path, mode="rpc")
    assert len(client.notebooks.copy_calls) == 1
    assert client.notebooks.delete_calls == ["copy-1"]


@pytest.mark.asyncio
async def test_reconciliation_persistence_failure_attempts_immediate_guarded_deletion(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    store = RecordingStore(tmp_path / "manifest.json", fail_when="reconciliation")
    manager, client, _store, _clock = _manager(tmp_path, contracts, store=store)
    client.notebooks.copy_error = TimeoutError("lost response with notebook id")
    client.notebooks.copy_error_commits = True

    with pytest.raises(PersistenceError):
        await _provision(manager, tmp_path, mode="rpc")

    assert len(client.notebooks.copy_calls) == 1
    assert client.notebooks.delete_calls == ["copy-1"]
    assert store.read(template_id=TEMPLATE_ID)["copies"][0]["status"] == "intent"


@pytest.mark.asyncio
async def test_returned_title_mismatch_is_reconciled_after_candidate_persistence(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    store = RecordingStore(tmp_path / "manifest.json")
    manager, client, _store, _clock = _manager(tmp_path, contracts, store=store)
    client.notebooks.returned_title = "wrong returned title"
    manifest = await _provision(manager, tmp_path, mode="rpc")
    assert manifest["copies"][0]["status"] == "reconciled"
    candidate_snapshot = next(
        value
        for value in store.snapshots
        if value["copies"] and value["copies"][-1]["candidate_notebook_id"]
    )
    assert candidate_snapshot["copies"][-1]["status"] == "intent"


@pytest.mark.asyncio
async def test_candidate_read_failure_leaves_durable_hint_for_later_safe_cleanup(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, store, _clock = _manager(tmp_path, contracts)
    client.notebooks.get_errors["copy-1"] = RuntimeError("read failed with candidate-id")
    with pytest.raises(CopyUnresolvedError):
        await _provision(manager, tmp_path, mode="rpc")
    row = store.read(template_id=TEMPLATE_ID)["copies"][0]
    assert row["candidate_notebook_id"] == "copy-1"
    assert row["notebook_id"] is None
    client.notebooks.get_errors.clear()
    assert (await manager.cleanup())["deleted"] == 1


@pytest.mark.parametrize("role", [SharePermission.VIEWER, SharePermission.EDITOR, None])
@pytest.mark.asyncio
async def test_copy_requires_explicit_owner_and_ignores_optimistic_is_owner(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
    role: SharePermission | None,
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    client.notebooks.created_role = role
    with pytest.raises(CopyUnresolvedError):
        await _provision(manager, tmp_path, mode="rpc")
    assert client.notebooks.items["copy-1"].is_owner is True
    assert client.notebooks.delete_calls == []


@pytest.mark.asyncio
async def test_cleanup_processes_roles_in_reverse_order(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    manifest = await _provision(manager, tmp_path)
    expected = [str(row["notebook_id"]) for row in reversed(manifest["copies"])]
    result = await manager.cleanup()
    assert result == {"deleted": 3, "already_missing": 0, "failed": 0}
    assert client.notebooks.delete_calls == expected
    assert all(row["status"] == "deleted" for row in manager.store.read()["copies"])


@pytest.mark.asyncio
async def test_cleanup_treats_confirmed_missing_as_already_deleted_success(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    manifest = await _provision(manager, tmp_path, mode="rpc")
    client.notebooks.items.pop(str(manifest["copies"][0]["notebook_id"]))
    result = await manager.cleanup()
    assert result == {"deleted": 0, "already_missing": 1, "failed": 0}
    assert manager.store.read()["copies"][0]["status"] == "deleted"


@pytest.mark.asyncio
async def test_cleanup_retries_idempotent_delete_only_to_configured_bound(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, store, clock = _manager(tmp_path, contracts)
    manifest = await _provision(manager, tmp_path, mode="rpc")
    notebook_id = str(manifest["copies"][0]["notebook_id"])
    client.notebooks.delete_failures[notebook_id] = 2
    retrying = NotebookLifecycleManager(
        client,
        template_id=TEMPLATE_ID,
        store=store,
        template_contract=contracts[0],
        prepared_contract=contracts[1],
        retry_policy=RetryPolicy(attempts=3, base_delay=1, max_delay=2),
        reconcile_timeout=0,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    assert (await retrying.cleanup())["deleted"] == 1
    assert client.notebooks.delete_calls == [notebook_id, notebook_id, notebook_id]


@pytest.mark.asyncio
async def test_cleanup_requires_bounded_post_delete_disappearance_confirmation(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, clock = _manager(tmp_path, contracts)
    manifest = await _provision(manager, tmp_path, mode="rpc")
    notebook_id = str(manifest["copies"][0]["notebook_id"])
    client.notebooks.delete_noop_ids.add(notebook_id)
    manager.retry_policy = RetryPolicy(attempts=3, base_delay=0.5, max_delay=1)
    get_calls_before = len(client.notebooks.get_calls)
    clock_before = clock.value

    with pytest.raises(CleanupError, match="left 1 role"):
        await manager.cleanup()

    assert client.notebooks.delete_calls == [notebook_id]
    assert client.notebooks.get_calls[get_calls_before:] == [notebook_id] * 4
    assert clock.value - clock_before == 1.5
    assert notebook_id in client.notebooks.items
    assert manager.store.read()["copies"][0]["status"] == "delete_failed"


def _write_intent(
    manager: NotebookLifecycleManager,
    *,
    candidate: str | None = None,
) -> tuple[dict[str, Any], str]:
    manifest = new_manifest(
        run_id="55",
        run_attempt="1",
        lane="rpc-health-web",
        mode="rpc",
        account_slot="A",
        backend="web",
        template_fingerprint=FINGERPRINT,
    )
    title = build_title("55", "1", "rpc-health-web", "rpc", "f" * 32)
    row = new_copy_row(role="rpc", title=title)
    row["candidate_notebook_id"] = candidate
    manifest["copies"].append(row)
    manager.store.write(manifest, template_id=TEMPLATE_ID)
    return manifest, title


@pytest.mark.asyncio
async def test_intent_missing_candidate_falls_back_to_exact_owner_match(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    _manifest, title = _write_intent(manager, candidate="missing-candidate")
    client.notebooks.items["recovered-copy"] = SimpleNamespace(
        id="recovered-copy",
        title=title,
        role=SharePermission.OWNER,
        created_at=datetime.now(timezone.utc),
    )
    result = await manager.cleanup()
    assert result["deleted"] == 1
    assert client.notebooks.delete_calls == ["recovered-copy"]


@pytest.mark.asyncio
async def test_unsafe_candidate_and_separate_exact_copy_deletes_safe_copy_but_stays_red(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    _manifest, title = _write_intent(manager, candidate="unsafe-candidate")
    client.notebooks.items["unsafe-candidate"] = SimpleNamespace(
        id="unsafe-candidate",
        title="unrelated title",
        role=SharePermission.OWNER,
        created_at=datetime.now(timezone.utc),
    )
    client.notebooks.items["safe-copy"] = SimpleNamespace(
        id="safe-copy",
        title=title,
        role=SharePermission.OWNER,
        created_at=datetime.now(timezone.utc),
    )
    with pytest.raises(CleanupError):
        await manager.cleanup()
    assert client.notebooks.delete_calls == ["safe-copy"]
    assert "unsafe-candidate" in client.notebooks.items
    assert manager.store.read()["copies"][0]["status"] == "delete_failed"


@pytest.mark.parametrize("variant", ["zero", "multiple", "unreadable"])
@pytest.mark.asyncio
async def test_unresolved_intent_variants_stay_red_without_unsafe_deletion(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
    variant: str,
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    _manifest, title = _write_intent(manager)
    if variant == "multiple":
        for index in range(2):
            client.notebooks.items[f"duplicate-{index}"] = SimpleNamespace(
                id=f"duplicate-{index}",
                title=title,
                role=SharePermission.OWNER,
                created_at=datetime.now(timezone.utc),
            )
    elif variant == "unreadable":
        client.notebooks.list_error = RuntimeError("unreadable with id")
    with pytest.raises(CleanupError):
        await manager.cleanup()
    assert client.notebooks.delete_calls == []
    assert manager.store.read()["copies"][0]["status"] == "delete_failed"


@pytest.mark.asyncio
async def test_tampered_manifest_aborts_cleanup_before_any_delete(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, store, _clock = _manager(tmp_path, contracts)
    manifest, _title = _write_intent(manager, candidate="safe-candidate")
    manifest["copies"][0]["candidate_notebook_id"] = TEMPLATE_ID
    store.path.write_text(json.dumps(manifest))
    with pytest.raises(ManifestError):
        await manager.cleanup()
    assert client.notebooks.delete_calls == []


@pytest.mark.asyncio
async def test_cleanup_refuses_title_mismatch_non_owner_and_unknown_role(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    manifest = await _provision(manager, tmp_path, mode="rpc")
    notebook_id = str(manifest["copies"][0]["notebook_id"])
    client.notebooks.items[notebook_id].title = "changed title"
    client.notebooks.items[notebook_id].role = None
    with pytest.raises(CleanupError):
        await manager.cleanup()
    assert client.notebooks.delete_calls == []


@pytest.mark.asyncio
async def test_sweep_applies_every_owner_prefix_age_template_current_and_title_gate(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    candidates = {
        "eligible-copy": (
            build_title("1", "1", "rpc-health-web", "rpc", "1" * 32),
            old,
            SharePermission.OWNER,
        ),
        "legacy-copy": (
            build_title("8", "1", "nightly-web-windows", "reference", "8" * 32),
            old,
            SharePermission.OWNER,
        ),
        "viewer-copy": (
            build_title("2", "1", "rpc-health-web", "rpc", "2" * 32),
            old,
            SharePermission.VIEWER,
        ),
        "recent-copy": (
            build_title("3", "1", "rpc-health-web", "rpc", "3" * 32),
            recent,
            SharePermission.OWNER,
        ),
        "unknown-time": (
            build_title("4", "1", "rpc-health-web", "rpc", "4" * 32),
            None,
            SharePermission.OWNER,
        ),
        "current-copy": (
            build_title("9", "7", "rpc-health-web", "rpc", "5" * 32),
            old,
            SharePermission.OWNER,
        ),
        "bad-title-copy": ("notebooklm-py-ci/not/normative", old, SharePermission.OWNER),
        "outside-copy": ("ordinary notebook", old, SharePermission.OWNER),
    }
    for notebook_id, (title, created_at, role) in candidates.items():
        client.notebooks.items[notebook_id] = SimpleNamespace(
            id=notebook_id,
            title=title,
            role=role,
            is_owner=True,
            created_at=created_at,
        )
    result = await manager.sweep(
        current_run_id="9",
        current_run_attempt="7",
        now=datetime.now(timezone.utc),
    )
    assert result.eligible == result.deleted == 2
    assert client.notebooks.delete_calls == ["eligible-copy", "legacy-copy"]


@pytest.mark.asyncio
async def test_sweep_cap_aborts_before_deleting_anything(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    for index in range(2):
        notebook_id = f"stale-copy-{index}"
        client.notebooks.items[notebook_id] = SimpleNamespace(
            id=notebook_id,
            title=build_title(
                str(index + 1),
                "1",
                "rpc-health-web",
                "rpc",
                f"{index + 1:032x}",
            ),
            role=SharePermission.OWNER,
            created_at=old,
        )
    with pytest.raises(CleanupError, match="cap"):
        await manager.sweep(
            current_run_id=None,
            current_run_attempt=None,
            deletion_cap=1,
            now=datetime.now(timezone.utc),
        )
    assert client.notebooks.delete_calls == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_age": timedelta(0)}, "maximum age"),
        ({"current_run_id": "1", "current_run_attempt": None}, "supplied together"),
        ({"current_run_id": "not-decimal", "current_run_attempt": "1"}, "decimal"),
        ({"now": datetime.now()}, "timezone-aware"),
    ],
)
@pytest.mark.asyncio
async def test_sweep_rejects_unsafe_policy_or_partial_run_identity_before_listing(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
    kwargs: dict[str, Any],
    message: str,
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    client.notebooks.list_error = AssertionError("listing must not run")
    values: dict[str, Any] = {
        "current_run_id": "1",
        "current_run_attempt": "1",
        "now": datetime.now(timezone.utc),
    }
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        await manager.sweep(**values)


@pytest.mark.asyncio
async def test_template_contract_failure_names_family_but_never_ids(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    client.artifacts.by_notebook[TEMPLATE_ID] = [
        artifact for artifact in _artifacts() if artifact.kind != "audio"
    ]
    with pytest.raises(ContractError) as caught:
        await manager.validate_template()
    message = str(caught.value)
    assert "audio" in message
    assert TEMPLATE_ID not in message
    assert "artifact-" not in message


@pytest.mark.asyncio
async def test_template_validation_rejects_wrong_notebook_lookup(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    client.notebooks.items[TEMPLATE_ID].id = "different-notebook"
    with pytest.raises(ContractError, match="wrong notebook"):
        await manager.validate_template()


@pytest.mark.asyncio
async def test_missing_template_is_a_safe_template_access_failure(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    del client.notebooks.items[TEMPLATE_ID]

    with pytest.raises(ContractError, match="unavailable to the selected account") as caught:
        await manager.validate_template()

    assert caught.value.category == "TEMPLATE_ACCESS"
    assert isinstance(caught.value.__cause__, NotebookNotFoundError)
    assert TEMPLATE_ID not in str(caught.value)


@pytest.mark.asyncio
async def test_missing_copied_child_remains_a_reconciliation_failure(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, _client, _store, _clock = _manager(tmp_path, contracts)

    with pytest.raises(NotebookNotFoundError):
        await manager._validate_copy_shape("missing-copy")


@pytest.mark.asyncio
async def test_clean_preparation_deletes_note_backed_map_through_mind_map_api(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    notebook_id = "clean-target"
    client.sources.by_notebook[notebook_id] = [_source(1), _source(2), _source(3)]
    note_backed = SimpleNamespace(
        id="note-backed-map",
        kind="mind_map",
        is_completed=True,
        is_interactive_mind_map=False,
    )
    client.artifacts.by_notebook[notebook_id] = [note_backed]
    client.mind_maps.extra[notebook_id] = [
        SimpleNamespace(id=note_backed.id, kind=MindMapKind.NOTE_BACKED)
    ]

    await manager.prepare_clean_role(notebook_id, "generation")

    assert client.artifacts.delete_calls == []
    assert client.mind_maps.delete_calls == [(notebook_id, note_backed.id, MindMapKind.NOTE_BACKED)]


@pytest.mark.asyncio
async def test_preparation_resets_quiet_window_for_between_sample_late_child(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, clock = _manager(tmp_path, contracts)
    notebook_id = "clean-target"
    client.sources.by_notebook[notebook_id] = [_source(1), _source(2), _source(3)]
    late = SimpleNamespace(
        id="late-child",
        kind="audio",
        is_completed=True,
        is_interactive_mind_map=False,
    )
    client.artifacts.list_script.extend([[], [], [], [late], [], [], [], []])
    result = await manager.prepare_clean_role(notebook_id, "generation")
    assert result["ready_sources"] == 3
    assert clock.value == 150


@pytest.mark.asyncio
async def test_preparation_times_out_at_five_minute_deadline(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, clock = _manager(tmp_path, contracts)
    notebook_id = "clean-target"
    client.sources.by_notebook[notebook_id] = [_source(1), _source(2), _source(3)]
    residual = SimpleNamespace(
        id="residual-child",
        kind="audio",
        is_completed=True,
        is_interactive_mind_map=False,
    )
    client.artifacts.by_notebook[notebook_id] = [residual]
    client.artifacts.delete_error_ids.add("residual-child")
    with pytest.raises(RuntimeError):
        await manager.prepare_clean_role(notebook_id, "generation")
    assert clock.value == 0


@pytest.mark.asyncio
async def test_preparation_unready_sources_hit_exact_five_minute_deadline(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, clock = _manager(tmp_path, contracts)
    notebook_id = "clean-target"
    client.sources.by_notebook[notebook_id] = [
        _source(1),
        _source(2),
        _source(3, ready=False),
    ]
    with pytest.raises(ContractError, match="stable clean"):
        await manager.prepare_clean_role(notebook_id, "generation")
    assert clock.value == 300


@pytest.mark.asyncio
async def test_clean_role_accepts_minimum_ready_sources_with_unready_extra(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    notebook_id = "clean-target"
    client.sources.by_notebook[notebook_id] = [
        _source(1),
        _source(2),
        _source(3),
        _source(4, ready=False),
    ]

    prepared = await manager.prepare_clean_role(notebook_id, "generation")
    validated = await manager.validate_prepared_role(notebook_id, "generation")

    assert prepared["ready_sources"] == 3
    assert validated["ready_sources"] == 3


@pytest.mark.asyncio
async def test_final_fresh_read_catches_late_child_and_restarts_quiet_window(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, clock = _manager(tmp_path, contracts)
    notebook_id = "clean-target"
    client.sources.by_notebook[notebook_id] = [_source(1), _source(2), _source(3)]
    late = SimpleNamespace(
        id="final-late-child",
        kind="audio",
        is_completed=True,
        is_interactive_mind_map=False,
    )
    client.artifacts.list_script.extend([[], [], [], [], [], [late], [], [], [], []])
    result = await manager.prepare_clean_role(notebook_id, "rpc")
    assert result["ready_sources"] == 3
    assert clock.value == 180


@pytest.mark.asyncio
async def test_final_fresh_read_cannot_succeed_after_preparation_deadline(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, clock = _manager(tmp_path, contracts)
    notebook_id = "clean-target"
    client.sources.by_notebook[notebook_id] = [_source(1), _source(2), _source(3)]
    calls = 0
    original_list = client.artifacts.list

    async def delayed_final_list(target: str) -> list[SimpleNamespace]:
        nonlocal calls
        calls += 1
        if calls == 6:
            clock.value = 301
        return await original_list(target)

    client.artifacts.list = delayed_final_list  # type: ignore[method-assign]
    with pytest.raises(ContractError, match="five-minute deadline"):
        await manager.prepare_clean_role(notebook_id, "generation")
    assert clock.value == 301


@pytest.mark.asyncio
async def test_empty_chat_check_cannot_succeed_after_preparation_deadline(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, clock = _manager(tmp_path, contracts)
    notebook_id = "clean-target"
    client.sources.by_notebook[notebook_id] = [_source(1), _source(2), _source(3)]
    original_get_conversation_id = client.chat.get_conversation_id

    async def delayed_conversation_id(target: str) -> str | None:
        clock.value = 301
        return await original_get_conversation_id(target)

    client.chat.get_conversation_id = delayed_conversation_id  # type: ignore[method-assign]
    with pytest.raises(ContractError, match="five-minute deadline"):
        await manager.prepare_clean_role(notebook_id, "rpc")
    assert clock.value == 301


@pytest.mark.parametrize("operation", ["prepare", "validate"])
@pytest.mark.asyncio
async def test_clean_chat_read_does_not_swallow_turn_failures(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
    operation: str,
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    notebook_id = "clean-target"
    client.sources.by_notebook[notebook_id] = [_source(1), _source(2), _source(3)]
    client.chat.seeded.add(notebook_id)
    client.chat.turns_error = ChatError("turn read failed")

    with pytest.raises(ChatError, match="turn read failed"):
        if operation == "prepare":
            await manager.prepare_clean_role(notebook_id, "rpc")
        else:
            await manager.validate_prepared_role(notebook_id, "rpc")


@pytest.mark.asyncio
async def test_clean_inventory_rejects_control_character_child_id(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, _store, _clock = _manager(tmp_path, contracts)
    notebook_id = "clean-target"
    client.artifacts.by_notebook[notebook_id] = [
        SimpleNamespace(
            id="artifact\rhidden",
            kind="audio",
            is_completed=True,
            is_interactive_mind_map=False,
        )
    ]
    with pytest.raises(ContractError, match="malformed child ID"):
        await manager.prepare_clean_role(notebook_id, "generation")
    assert client.artifacts.delete_calls == []


@pytest.mark.asyncio
async def test_source_list_failure_is_fail_closed_before_prepared_state(
    tmp_path: Path,
    contracts: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    manager, client, store, _clock = _manager(tmp_path, contracts)
    client.sources.error = RuntimeError("source read failed with source-id")
    with pytest.raises(RuntimeError):
        await _provision(manager, tmp_path, mode="rpc")
    assert not store.path.exists()


def test_diagnostics_do_not_render_exception_bodies_or_resource_ids() -> None:
    error = RuntimeError("SID=secret-cookie notebook-id=copy-sensitive")
    rendered = _safe_exception_name(error)
    assert rendered == "RuntimeError"
    assert "secret-cookie" not in rendered
    assert "copy-sensitive" not in rendered


def test_web_chat_quota_marker_is_categorized_without_rendering_the_body() -> None:
    quota = ChatError("Chat request was rate limited or rejected by the API. secret-id")
    regression = ChatError("Chat response was malformed. secret-id")

    assert lifecycle._category_for(quota) == "QUOTA"
    assert lifecycle._category_for(regression) == "REGRESSION"
    assert _safe_exception_name(quota) == "ChatError"


def test_contract_files_are_versioned_and_contain_no_identity_handles() -> None:
    for name in ("e2e_template_contract.json", "e2e_prepared_role_contract.json"):
        raw = (REPO_ROOT / "tests" / "fixtures" / name).read_text()
        value = json.loads(raw)
        assert value["version"] == 1
        assert "notebook_id" not in raw
        assert "account_email" not in raw


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("poll_interval_seconds", 1),
        ("quiet_period_seconds", 91),
        ("preparation_deadline_seconds", 299),
    ],
)
def test_prepared_contract_pins_safety_timing(
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    contract = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "e2e_prepared_role_contract.json").read_text()
    )
    contract["clean_roles"][field] = value
    path = tmp_path / "prepared.json"
    path.write_text(json.dumps(contract))
    with pytest.raises(ContractError, match="shape"):
        load_prepared_contract(path)


def test_store_refuses_symlink_manifest(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "manifest.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    store = AtomicJSONStore(link, runner_temp=tmp_path)
    manifest = new_manifest(
        run_id="1",
        run_attempt="1",
        lane="nightly-web-ubuntu",
        mode="full",
        account_slot="A",
        backend="web",
        template_fingerprint=FINGERPRINT,
    )
    with pytest.raises(ManifestError, match="regular|reparse"):
        store.write(manifest)


def test_environment_block_contains_no_activation_for_partial_preparation(tmp_path: Path) -> None:
    path = tmp_path / "github-env"
    path.write_text("PREEXISTING=1\n")
    assert "NOTEBOOKLM_E2E_MANAGED_COPIES" not in path.read_text()
    assert os.fspath(path)


def test_cli_cleanup_absent_manifest_never_opens_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.delenv(TEMPLATE_ID_ENV, raising=False)

    def fail_from_storage(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("cleanup no-op must not open authentication")

    monkeypatch.setattr(lifecycle.NotebookLMClient, "from_storage", fail_from_storage)
    rc = lifecycle.main(
        [
            "cleanup",
            "--backend",
            "web",
            "--template-id-env",
            TEMPLATE_ID_ENV,
            "--manifest",
            str(tmp_path / "absent.json"),
        ]
    )
    assert rc == 0
    assert "nothing to clean" in capsys.readouterr().out


def test_cli_cleanup_absent_manifest_still_rejects_arbitrary_env_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    rc = lifecycle.main(
        [
            "cleanup",
            "--backend",
            "web",
            "--template-id-env",
            "ARBITRARY_ID",
            "--manifest",
            str(tmp_path / "absent.json"),
        ]
    )
    assert rc == 1
    assert "ERROR[CONFIGURATION]" in capsys.readouterr().err


def test_cli_metadata_is_strict_and_run_overrides_are_not_exposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ManifestError):
        lifecycle._metadata()

    monkeypatch.setenv(TEMPLATE_ID_ENV, TEMPLATE_ID)

    def fail_from_storage(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("invalid metadata must fail before authentication")

    monkeypatch.setattr(lifecycle.NotebookLMClient, "from_storage", fail_from_storage)
    rc = lifecycle.main(
        [
            "provision",
            "--backend",
            "web",
            "--template-id-env",
            TEMPLATE_ID_ENV,
            "--mode",
            "rpc",
            "--lane",
            "rpc-health-web",
            "--account-slot",
            "A",
            "--manifest",
            "manifest.json",
            "--github-env",
            "github-env",
        ]
    )
    assert rc == 1

    parser = lifecycle.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "provision",
                "--backend",
                "web",
                "--template-id-env",
                TEMPLATE_ID_ENV,
                "--mode",
                "rpc",
                "--lane",
                "rpc-health-web",
                "--account-slot",
                "A",
                "--manifest",
                "manifest.json",
                "--github-env",
                "github-env",
                "--run-id",
                "1",
            ]
        )


@pytest.mark.parametrize(
    "partial_args",
    [
        ["--role", "reference"],
        ["--manifest", "manifest.json"],
    ],
)
def test_cli_prepared_validation_requires_manifest_and_role_together(
    partial_args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEMPLATE_ID_ENV, TEMPLATE_ID)

    def fail_from_storage(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("invalid validation arguments must fail before authentication")

    monkeypatch.setattr(lifecycle.NotebookLMClient, "from_storage", fail_from_storage)
    rc = lifecycle.main(
        [
            "validate",
            "--backend",
            "web",
            "--template-id-env",
            TEMPLATE_ID_ENV,
            *partial_args,
        ]
    )
    assert rc == 1
