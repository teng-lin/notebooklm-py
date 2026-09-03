"""Stateful wire, lifecycle, and frontend-shaped tests for organization adapters."""

from __future__ import annotations

import ast
import asyncio
import builtins
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from notebooklm._android.collections import AndroidCollectionsAPI
from notebooklm._android.labels import AndroidLabelsAPI
from notebooklm._android.organization import (
    COLLECTION_TYPE,
    CREATE_LABEL_METHOD,
    DELETE_LABELS_METHOD,
    GET_LABELS_METHOD,
    MUTATE_LABEL_METHOD,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    organization_pb2,
    read_pb2,
)
from notebooklm._android.proto.notebooklm.android.wire.v1 import (
    organization_mutations_pb2,
)
from notebooklm._android.session import AndroidSession
from notebooklm._client_metrics import ClientMetrics
from notebooklm._collections import CollectionsAPI
from notebooklm._labels import LabelsAPI
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm.exceptions import (
    AuthError,
    CollectionError,
    CollectionNotFoundError,
    DecodingError,
    LabelError,
    LabelNotFoundError,
    NetworkError,
    NotebookNotFoundError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
)
from notebooklm.types import Collection, Label, Notebook, Source

NB = "00000000-0000-4000-8000-000000000100"
NB_A = "00000000-0000-4000-8000-000000000201"
NB_B = "00000000-0000-4000-8000-000000000202"
NB_MISSING = "00000000-0000-4000-8000-000000000299"
LABEL_A = "00000000-0000-4000-8000-000000000301"
LABEL_B = "00000000-0000-4000-8000-000000000302"
LABEL_MISSING = "00000000-0000-4000-8000-000000000399"
COLLECTION_A = "00000000-0000-4000-8000-000000000401"
COLLECTION_B = "00000000-0000-4000-8000-000000000402"
COLLECTION_MISSING = "00000000-0000-4000-8000-000000000499"
SOURCE_A = "00000000-0000-4000-8000-000000000501"
SOURCE_B = "00000000-0000-4000-8000-000000000502"
SOURCE_MISSING = "00000000-0000-4000-8000-000000000599"


@dataclass(frozen=True)
class _Lease:
    epoch: int


class FakeOrganizationServer:
    """Stateful server retaining the measured label/collection wire differences."""

    epoch = 7

    def __init__(self) -> None:
        self.calls: builtins.list[tuple[str, Any, dict[str, Any]]] = []
        self.operation_scopes: builtins.list[tuple[str, int | None]] = []
        self.labels: dict[str, dict[str, Label]] = {
            NB: {
                LABEL_A: Label(
                    id=LABEL_A,
                    name="Papers",
                    notebook_id=NB,
                    emoji="📄",
                    source_ids=[SOURCE_A],
                )
            }
        }
        self.collections: dict[str, Collection] = {
            COLLECTION_A: Collection(
                id=COLLECTION_A,
                name="Research",
                emoji="📁",
                notebook_ids=[NB_A],
            )
        }
        self.next_label_ids = [LABEL_B]
        self.next_collection_ids = [COLLECTION_B]
        self.concurrent_label_ids: builtins.list[str] = []
        self.concurrent_collection_ids: builtins.list[str] = []
        self.failures: dict[int, BaseException] = {}
        self.create_response_override: Any | None = None
        self.ignore_mutations = False

    @asynccontextmanager
    async def operation_scope(
        self,
        label: str,
        *,
        expected_epoch: int | None = None,
    ) -> AsyncIterator[_Lease]:
        self.operation_scopes.append((label, expected_epoch))
        yield _Lease(self.epoch)

    @staticmethod
    def _label_row(label: Label) -> Any:
        return organization_mutations_pb2.OrganizationRecordWire(
            name=label.name,
            member_ids=[
                read_pb2.SourceId(id=source_id).SerializeToString()
                for source_id in label.source_ids
            ],
            id=label.id,
            emoji=label.emoji or "",
        )

    @staticmethod
    def _collection_row(collection: Collection) -> Any:
        return organization_mutations_pb2.OrganizationRecordWire(
            name=collection.name,
            member_ids=[notebook_id.encode() for notebook_id in collection.notebook_ids],
            id=collection.id,
            emoji=collection.emoji or "",
        )

    @staticmethod
    def _created_label_row(label: Label) -> Any:
        return organization_pb2.LabelAndSources(
            label=label.name,
            source_ids=[read_pb2.SourceId(id=source_id) for source_id in label.source_ids],
            label_id=label.id,
            emoji=label.emoji or "",
        )

    @staticmethod
    def _created_collection_row(collection: Collection) -> Any:
        return organization_pb2.NotebookCollection(
            name=collection.name,
            notebook_ids=collection.notebook_ids,
            id=collection.id,
            emoji=collection.emoji or "",
        )

    def _response(self) -> Any:
        return organization_mutations_pb2.GetLabelsWireResponse(
            labels=[
                self._label_row(label)
                for labels in self.labels.values()
                for label in labels.values()
            ],
            collections=[self._collection_row(value) for value in self.collections.values()],
        )

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        failure = self.failures.get(len(self.calls))
        if failure is not None:
            raise failure
        assert kwargs["expected_epoch"] == self.epoch

        if method == GET_LABELS_METHOD:
            assert kwargs == {
                "replay_safe": True,
                "response_type": organization_mutations_pb2.GetLabelsWireResponse,
                "expected_epoch": self.epoch,
            }
            return self._response()

        response_types = {
            CREATE_LABEL_METHOD: organization_pb2.CreateLabelResponse,
            MUTATE_LABEL_METHOD: organization_pb2.MutateLabelResponse,
            DELETE_LABELS_METHOD: organization_pb2.DeleteLabelsResponse,
        }
        assert kwargs["response_type"] is response_types[method]
        assert kwargs["expected_epoch"] == self.epoch
        assert kwargs["replay_safe"] is False
        assert "operation_variant" not in kwargs
        assert request.HasField("request_context")
        if method == CREATE_LABEL_METHOD:
            if request.HasField("auto_create"):
                assert request.auto_create.HasField("regenerate_all")
                bucket = self.labels.setdefault(request.project_id, {})
                if request.auto_create.regenerate_all:
                    bucket.clear()
                bucket[LABEL_B] = Label(
                    id=LABEL_B,
                    name="Generated",
                    notebook_id=request.project_id,
                    emoji="✨",
                    source_ids=[SOURCE_B],
                )
                return organization_pb2.CreateLabelResponse()
            properties = request.manual_create.properties
            if request.label_type == COLLECTION_TYPE:
                created_collections: builtins.list[Collection] = []
                for resource_id in self.next_collection_ids:
                    collection = Collection(
                        id=resource_id,
                        name=properties.name,
                        emoji=properties.emoji or None,
                    )
                    self.collections[resource_id] = collection
                    created_collections.append(collection)
                for resource_id in self.concurrent_collection_ids:
                    self.collections[resource_id] = Collection(
                        id=resource_id,
                        name="Concurrent",
                    )
                if self.create_response_override is not None:
                    return self.create_response_override
                return organization_pb2.CreateLabelResponse(
                    notebook_collections=[
                        self._created_collection_row(collection)
                        for collection in created_collections
                    ]
                )
            else:
                bucket = self.labels.setdefault(request.project_id, {})
                created: builtins.list[Label] = []
                for resource_id in self.next_label_ids:
                    label = Label(
                        id=resource_id,
                        name=properties.name,
                        notebook_id=request.project_id,
                        emoji=properties.emoji or None,
                    )
                    bucket[resource_id] = label
                    created.append(label)
                for resource_id in self.concurrent_label_ids:
                    bucket[resource_id] = Label(
                        id=resource_id,
                        name="Concurrent",
                        notebook_id=request.project_id,
                    )
                if self.create_response_override is not None:
                    return self.create_response_override
                return organization_pb2.CreateLabelResponse(
                    label_and_sources=[self._created_label_row(label) for label in created]
                )
            return organization_pb2.CreateLabelResponse()

        if method == MUTATE_LABEL_METHOD:
            if self.ignore_mutations:
                return organization_pb2.MutateLabelResponse()
            target: Label | Collection
            if request.label_type == COLLECTION_TYPE:
                target = self.collections[request.label_id]
            else:
                target = self.labels[request.project_id][request.label_id]
            mutation = request.mutations[0]
            if mutation.HasField("properties"):
                target.name = mutation.properties.name
                target.emoji = mutation.properties.emoji or None
            for field, add, member_attr in (
                ("add_sources", True, "source_ids"),
                ("remove_sources", False, "source_ids"),
                ("add_notebooks", True, "notebook_ids"),
                ("remove_notebooks", False, "notebook_ids"),
            ):
                if not mutation.HasField(field):
                    continue
                (member_id,) = getattr(mutation, field).member_ids
                members = getattr(target, member_attr)
                if add and member_id not in members:
                    members.append(member_id)
                elif not add and member_id in members:
                    members.remove(member_id)
            return organization_pb2.MutateLabelResponse()

        if method == DELETE_LABELS_METHOD:
            if request.label_type == COLLECTION_TYPE:
                for resource_id in request.label_ids:
                    self.collections.pop(resource_id, None)
            else:
                bucket = self.labels.setdefault(request.project_id, {})
                for resource_id in request.label_ids:
                    bucket.pop(resource_id, None)
            return organization_pb2.DeleteLabelsResponse()
        raise AssertionError(f"unexpected method: {method}")


def _apis(
    server: FakeOrganizationServer,
) -> tuple[AndroidLabelsAPI, AndroidCollectionsAPI]:
    sources = [Source(id=SOURCE_A, title="A"), Source(id=SOURCE_B, title="B")]
    notebooks = [Notebook(id=NB_A, title="A"), Notebook(id=NB_B, title="B")]

    async def list_sources(_notebook_id: str) -> builtins.list[Source]:
        return sources

    async def list_notebooks() -> builtins.list[Notebook]:
        return notebooks

    transport = cast(AndroidSession, server)
    return (
        AndroidLabelsAPI(
            transport,
            list_sources=list_sources,
        ),
        AndroidCollectionsAPI(transport, list_notebooks=list_notebooks),
    )


def test_adapters_are_concrete_and_module_imports_keep_protobuf_lazy() -> None:
    server = FakeOrganizationServer()
    labels, collections = _apis(server)
    assert isinstance(labels, LabelsAPI)
    assert isinstance(collections, CollectionsAPI)
    assert server.calls == []
    assert server.operation_scopes == []
    assert CollectionsAPI.__abstractmethods__ == {
        "list",
        "create",
        "_send_update",
        "_send_mutate_member",
    }
    assert all(
        inspect.iscoroutinefunction(getattr(AndroidCollectionsAPI, name))
        for name in CollectionsAPI.__abstractmethods__
    )
    assert all(
        inspect.iscoroutinefunction(getattr(AndroidLabelsAPI, name))
        for name in LabelsAPI.__abstractmethods__
    )

    root = Path(__file__).resolve().parents[3]
    for relative in (
        "src/notebooklm/_android/organization.py",
        "src/notebooklm/_android/labels.py",
        "src/notebooklm/_android/collections.py",
        "src/notebooklm/_android/codecs/organization.py",
    ):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        rendered = " ".join(ast.unparse(node) for node in imports)
        assert "google.protobuf" not in rendered
        assert "._android.proto" not in rendered
        assert "_pb2" not in rendered


async def test_list_both_modes_decodes_heterogeneous_members_and_one_epoch_each() -> None:
    server = FakeOrganizationServer()
    labels, collections = _apis(server)
    assert await labels.list(NB) == [server.labels[NB][LABEL_A]]
    assert await collections.list() == [server.collections[COLLECTION_A]]

    assert server.operation_scopes == [("labels.list", None), ("collections.list", None)]
    first_request = server.calls[0][1]
    second_request = server.calls[1][1]
    assert first_request == organization_pb2.GetLabelsRequest(project_id=NB)
    assert second_request == organization_pb2.GetLabelsRequest(label_type=COLLECTION_TYPE)
    assert [call[2]["expected_epoch"] for call in server.calls] == [7, 7]


async def test_get_and_membership_joins_preserve_order_and_skip_missing() -> None:
    server = FakeOrganizationServer()
    server.labels[NB][LABEL_A].source_ids[:] = [SOURCE_B, SOURCE_MISSING, SOURCE_A]
    server.collections[COLLECTION_A].notebook_ids[:] = [NB_B, NB_MISSING, NB_A]
    labels, collections = _apis(server)

    assert [source.id for source in await labels.sources(NB, LABEL_A)] == [SOURCE_B, SOURCE_A]
    assert [notebook.id for notebook in await collections.notebooks(COLLECTION_A)] == [NB_B, NB_A]
    assert await labels.get_or_none(NB, LABEL_MISSING) is None
    assert await collections.get_or_none(COLLECTION_MISSING) is None
    with pytest.raises(LabelNotFoundError):
        await labels.get(NB, LABEL_MISSING)
    with pytest.raises(CollectionNotFoundError):
        await collections.get(COLLECTION_MISSING)


async def test_label_and_collection_create_use_exact_response_rows() -> None:
    server = FakeOrganizationServer()
    labels, collections = _apis(server)

    created_label = await labels.create(NB, "Duplicate-safe", "🧪")
    created_collection = await collections.create("Duplicate-safe")
    assert created_label.id == LABEL_B
    assert created_collection.id == COLLECTION_B
    assert [method for method, _request, _kwargs in server.calls] == [
        CREATE_LABEL_METHOD,
        GET_LABELS_METHOD,
        CREATE_LABEL_METHOD,
    ]
    assert server.operation_scopes == [("labels.create", None), ("collections.create", None)]

    label_create = server.calls[0][1]
    assert label_create.project_id == NB
    assert label_create.label_type == 0
    assert label_create.manual_create.properties.name == "Duplicate-safe"
    assert label_create.manual_create.properties.emoji == "🧪"
    collection_create = server.calls[2][1]
    assert collection_create.project_id == ""
    assert collection_create.label_type == COLLECTION_TYPE
    assert collection_create.manual_create.properties.name == "Duplicate-safe"
    assert not collection_create.manual_create.properties.HasField("emoji")


@pytest.mark.parametrize("kind", ["label", "collection"])
@pytest.mark.parametrize(
    "error",
    [NetworkError("response lost"), RateLimitError("throttled"), ServerError("unavailable")],
)
async def test_manual_create_transport_loss_is_unconfirmed_and_sent_once(
    kind: str,
    error: RPCError,
) -> None:
    server = FakeOrganizationServer()
    server.failures[2 if kind == "collection" else 1] = error
    labels, collections = _apis(server)

    with pytest.raises(type(error)) as raised:
        if kind == "label":
            await labels.create(NB, "Requested")
        else:
            await collections.create("Requested")

    assert raised.value is error
    assert getattr(raised.value, "unconfirmed", False) is True
    expected_methods = (
        [GET_LABELS_METHOD, CREATE_LABEL_METHOD] if kind == "collection" else [CREATE_LABEL_METHOD]
    )
    assert [method for method, _request, _kwargs in server.calls] == expected_methods


async def test_label_create_ignores_unrelated_concurrent_post_state() -> None:
    server = FakeOrganizationServer()
    server.concurrent_label_ids = [LABEL_MISSING]
    labels, _collections = _apis(server)

    created = await labels.create(NB, "Requested", "🧪")

    assert created.id == LABEL_B
    assert created.name == "Requested"
    assert set(server.labels[NB]) == {LABEL_A, LABEL_B, LABEL_MISSING}
    assert [method for method, _request, _kwargs in server.calls] == [CREATE_LABEL_METHOD]


async def test_collection_create_selects_new_row_from_cumulative_response() -> None:
    server = FakeOrganizationServer()
    server.create_response_override = organization_pb2.CreateLabelResponse(
        notebook_collections=[
            server._created_collection_row(server.collections[COLLECTION_A]),
            organization_pb2.NotebookCollection(
                name="Requested",
                id=COLLECTION_B,
            ),
        ]
    )
    _labels, collections = _apis(server)

    created = await collections.create("Requested")

    assert created.id == COLLECTION_B
    assert [method for method, _request, _kwargs in server.calls] == [
        GET_LABELS_METHOD,
        CREATE_LABEL_METHOD,
    ]


async def test_collection_create_ignores_unrelated_concurrent_post_state() -> None:
    server = FakeOrganizationServer()
    server.concurrent_collection_ids = [COLLECTION_MISSING]
    _labels, collections = _apis(server)

    created = await collections.create("Requested")

    assert created.id == COLLECTION_B
    assert created.name == "Requested"
    assert set(server.collections) == {COLLECTION_A, COLLECTION_B, COLLECTION_MISSING}
    assert [method for method, _request, _kwargs in server.calls] == [
        GET_LABELS_METHOD,
        CREATE_LABEL_METHOD,
    ]


@pytest.mark.parametrize("kind", ["label", "collection"])
@pytest.mark.parametrize("new_count", [0, 2])
async def test_create_response_rejects_zero_or_multiple_rows(kind: str, new_count: int) -> None:
    server = FakeOrganizationServer()
    server.next_label_ids = [LABEL_B, LABEL_MISSING][:new_count]
    server.next_collection_ids = [COLLECTION_B, COLLECTION_MISSING][:new_count]
    labels, collections = _apis(server)
    if kind == "label":
        with pytest.raises(LabelError) as raised:
            await labels.create(NB, "ambiguous")
    else:
        with pytest.raises(CollectionError) as raised:
            await collections.create("ambiguous")
    assert getattr(raised.value, "unconfirmed", False) is True


@pytest.mark.parametrize(
    "row",
    [
        organization_pb2.LabelAndSources(label="Wrong", label_id=LABEL_B, emoji="🧪"),
        organization_pb2.LabelAndSources(
            label="Requested",
            label_id=LABEL_B,
            emoji="wrong",
        ),
        organization_pb2.LabelAndSources(
            label="Requested",
            label_id=LABEL_B,
            emoji="🧪",
            source_ids=[read_pb2.SourceId(id=SOURCE_A)],
        ),
    ],
    ids=["wrong-name", "wrong-emoji", "unexpected-member"],
)
async def test_label_create_rejects_uncorrelated_direct_response(row: Any) -> None:
    server = FakeOrganizationServer()
    server.create_response_override = organization_pb2.CreateLabelResponse(label_and_sources=[row])
    labels, _collections = _apis(server)

    with pytest.raises(DecodingError, match="requested empty label") as caught:
        await labels.create(NB, "Requested", "🧪")

    assert caught.value.method_id == CREATE_LABEL_METHOD
    assert getattr(caught.value, "unconfirmed", False) is True
    assert [method for method, _request, _kwargs in server.calls] == [CREATE_LABEL_METHOD]


@pytest.mark.parametrize(
    "row",
    [
        organization_pb2.NotebookCollection(name="Wrong", id=COLLECTION_B),
        organization_pb2.NotebookCollection(
            name="Requested",
            id=COLLECTION_B,
            emoji="unexpected",
        ),
        organization_pb2.NotebookCollection(
            name="Requested",
            id=COLLECTION_B,
            notebook_ids=[NB_A],
        ),
    ],
    ids=["wrong-name", "wrong-emoji", "unexpected-member"],
)
async def test_collection_create_rejects_uncorrelated_direct_response(row: Any) -> None:
    server = FakeOrganizationServer()
    server.create_response_override = organization_pb2.CreateLabelResponse(
        notebook_collections=[row]
    )
    _labels, collections = _apis(server)

    with pytest.raises(DecodingError, match="requested empty collection") as caught:
        await collections.create("Requested")

    assert caught.value.method_id == CREATE_LABEL_METHOD
    assert getattr(caught.value, "unconfirmed", False) is True
    assert [method for method, _request, _kwargs in server.calls] == [
        GET_LABELS_METHOD,
        CREATE_LABEL_METHOD,
    ]


async def test_property_mutations_preserve_other_field_and_verify_readback() -> None:
    server = FakeOrganizationServer()
    labels, collections = _apis(server)

    renamed = await labels.rename(NB, LABEL_A, "Renamed")
    assert renamed is not None and renamed.name == "Renamed" and renamed.emoji == "📄"
    cleared = await labels.set_emoji(NB, LABEL_A, "")
    assert cleared is not None and cleared.name == "Renamed" and cleared.emoji is None
    renamed_collection = await collections.rename(COLLECTION_A, "Archive")
    assert renamed_collection is not None
    assert (renamed_collection.name, renamed_collection.emoji) == ("Archive", "📁")

    writes = [request for method, request, _kwargs in server.calls if method == MUTATE_LABEL_METHOD]
    assert writes[0].mutations[0].properties.name == "Renamed"
    assert writes[0].mutations[0].properties.emoji == "📄"
    assert writes[1].mutations[0].properties.name == "Renamed"
    assert writes[1].mutations[0].properties.HasField("emoji")
    assert writes[1].mutations[0].properties.emoji == ""
    assert writes[2].label_type == COLLECTION_TYPE
    assert writes[2].mutations[0].properties.emoji == "📁"


async def test_label_membership_is_one_member_per_rpc_deduped_and_read_back() -> None:
    server = FakeOrganizationServer()
    labels, _collections = _apis(server)

    added = await labels.add_sources(NB, LABEL_A, [SOURCE_B, SOURCE_B])
    assert added is not None and added.source_ids == [SOURCE_A, SOURCE_B]
    removed = await labels.remove_sources(NB, LABEL_A, [SOURCE_A, SOURCE_A])
    assert removed is not None and removed.source_ids == [SOURCE_B]
    mutations = [
        request.mutations[0]
        for method, request, _kwargs in server.calls
        if method == MUTATE_LABEL_METHOD
    ]
    assert len(mutations) == 2
    assert mutations[0].add_sources.member_ids == [SOURCE_B]
    assert mutations[1].remove_sources.member_ids == [SOURCE_A]
    assert server.operation_scopes == [
        ("labels.add_sources", None),
        ("labels.remove_sources", None),
    ]


async def test_collection_membership_is_one_member_per_rpc_and_non_atomic_on_failure() -> None:
    server = FakeOrganizationServer()
    _labels, collections = _apis(server)
    updated = await collections.add_notebooks(
        COLLECTION_A,
        [NB_B, NB_B],
        return_object=False,
    )
    assert updated is None
    assert server.collections[COLLECTION_A].notebook_ids == [NB_A, NB_B]
    writes = [call for call in server.calls if call[0] == MUTATE_LABEL_METHOD]
    assert len(writes) == 1
    assert writes[0][1].mutations[0].add_notebooks.member_ids == [NB_B]

    failing = FakeOrganizationServer()
    failing.failures[2] = RPCError("failed", method_id=MUTATE_LABEL_METHOD)
    _labels, collections = _apis(failing)
    with pytest.raises(RPCError):
        await collections.add_notebooks(COLLECTION_A, [NB_B, NB_MISSING])
    assert failing.collections[COLLECTION_A].notebook_ids == [NB_A, NB_B]
    assert [method for method, _request, _kwargs in failing.calls] == [
        MUTATE_LABEL_METHOD,
        MUTATE_LABEL_METHOD,
    ]
    assert failing.operation_scopes == [("collections.add_notebooks", None)]


async def test_collection_membership_not_found_maps_only_after_absence_readback() -> None:
    present = FakeOrganizationServer()
    ambiguous = RPCError("not found", method_id=MUTATE_LABEL_METHOD, rpc_code=5)
    present.failures[1] = ambiguous
    _labels, collections = _apis(present)

    with pytest.raises(RPCError) as caught:
        await collections.add_notebooks(COLLECTION_A, [NB_B])

    assert caught.value is ambiguous
    assert type(caught.value) is RPCError
    assert [method for method, _request, _kwargs in present.calls] == [
        MUTATE_LABEL_METHOD,
        GET_LABELS_METHOD,
    ]
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in present.calls] == [7, 7]
    assert present.operation_scopes == [("collections.add_notebooks", None)]

    absent = FakeOrganizationServer()
    absent.collections.pop(COLLECTION_A)
    absent.failures[1] = RPCError("not found", method_id=MUTATE_LABEL_METHOD, rpc_code=5)
    _labels, collections = _apis(absent)

    with pytest.raises(CollectionNotFoundError) as caught_miss:
        await collections.remove_notebooks(COLLECTION_A, [NB_A])

    assert caught_miss.value.collection_id == COLLECTION_A
    assert [method for method, _request, _kwargs in absent.calls] == [
        MUTATE_LABEL_METHOD,
        GET_LABELS_METHOD,
    ]
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in absent.calls] == [7, 7]
    assert absent.operation_scopes == [("collections.remove_notebooks", None)]


async def test_collection_membership_not_found_preserves_write_error_if_readback_fails() -> None:
    server = FakeOrganizationServer()
    ambiguous = RPCError("membership not found", method_id=MUTATE_LABEL_METHOD, rpc_code=5)
    server.failures[1] = ambiguous
    server.failures[2] = RPCError("readback failed", method_id=GET_LABELS_METHOD, rpc_code=13)
    _labels, collections = _apis(server)

    with pytest.raises(RPCError) as caught:
        await collections.add_notebooks(COLLECTION_A, [NB_B])

    assert caught.value is ambiguous
    assert type(caught.value) is RPCError
    assert [method for method, _request, _kwargs in server.calls] == [
        MUTATE_LABEL_METHOD,
        GET_LABELS_METHOD,
    ]
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in server.calls] == [7, 7]
    assert server.operation_scopes == [("collections.add_notebooks", None)]


@pytest.mark.parametrize(
    "readback_failure",
    [
        NetworkError("readback connection failed", method_id=GET_LABELS_METHOD),
        RPCTimeoutError(
            "readback timed out",
            timeout_seconds=1,
            method_id=GET_LABELS_METHOD,
        ),
    ],
    ids=["network", "timeout"],
)
async def test_collection_membership_not_found_preserves_write_error_if_readback_transport_fails(
    readback_failure: NetworkError,
) -> None:
    server = FakeOrganizationServer()
    ambiguous = RPCError("membership not found", method_id=MUTATE_LABEL_METHOD, rpc_code=5)
    server.failures[1] = ambiguous
    server.failures[2] = readback_failure
    _labels, collections = _apis(server)

    with pytest.raises(RPCError) as caught:
        await collections.add_notebooks(COLLECTION_A, [NB_B])

    assert caught.value is ambiguous
    assert type(caught.value) is RPCError
    assert [method for method, _request, _kwargs in server.calls] == [
        MUTATE_LABEL_METHOD,
        GET_LABELS_METHOD,
    ]
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in server.calls] == [7, 7]
    assert server.operation_scopes == [("collections.add_notebooks", None)]


async def test_label_membership_not_found_maps_only_after_absence_readback() -> None:
    present = FakeOrganizationServer()
    ambiguous = RPCError("not found", method_id=MUTATE_LABEL_METHOD, rpc_code=5)
    present.failures[1] = ambiguous
    labels, _collections = _apis(present)

    with pytest.raises(RPCError) as caught:
        await labels.add_sources(NB, LABEL_A, [SOURCE_B])

    assert caught.value is ambiguous
    assert type(caught.value) is RPCError
    assert [method for method, _request, _kwargs in present.calls] == [
        MUTATE_LABEL_METHOD,
        GET_LABELS_METHOD,
    ]
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in present.calls] == [7, 7]
    assert present.operation_scopes == [("labels.add_sources", None)]

    absent = FakeOrganizationServer()
    absent.labels[NB].pop(LABEL_A)
    absent.failures[1] = RPCError("not found", method_id=MUTATE_LABEL_METHOD, rpc_code=5)
    labels, _collections = _apis(absent)

    with pytest.raises(LabelNotFoundError) as caught_miss:
        await labels.remove_sources(NB, LABEL_A, [SOURCE_A])

    assert caught_miss.value.label_id == LABEL_A
    assert [method for method, _request, _kwargs in absent.calls] == [
        MUTATE_LABEL_METHOD,
        GET_LABELS_METHOD,
    ]
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in absent.calls] == [7, 7]
    assert absent.operation_scopes == [("labels.remove_sources", None)]


@pytest.mark.parametrize(
    "readback_failure",
    [
        RPCError("readback failed", method_id=GET_LABELS_METHOD, rpc_code=13),
        NetworkError("readback connection failed", method_id=GET_LABELS_METHOD),
        RPCTimeoutError(
            "readback timed out",
            timeout_seconds=1,
            method_id=GET_LABELS_METHOD,
        ),
    ],
    ids=["rpc", "network", "timeout"],
)
async def test_label_membership_not_found_preserves_write_error_if_readback_fails(
    readback_failure: NetworkError | RPCError,
) -> None:
    server = FakeOrganizationServer()
    ambiguous = RPCError("membership not found", method_id=MUTATE_LABEL_METHOD, rpc_code=5)
    server.failures[1] = ambiguous
    server.failures[2] = readback_failure
    labels, _collections = _apis(server)

    with pytest.raises(RPCError) as caught:
        await labels.add_sources(NB, LABEL_A, [SOURCE_B])

    assert caught.value is ambiguous
    assert type(caught.value) is RPCError
    assert [method for method, _request, _kwargs in server.calls] == [
        MUTATE_LABEL_METHOD,
        GET_LABELS_METHOD,
    ]
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in server.calls] == [7, 7]
    assert server.operation_scopes == [("labels.add_sources", None)]


async def test_delete_filters_absent_ids_batches_existing_and_reads_back_absence() -> None:
    server = FakeOrganizationServer()
    labels, collections = _apis(server)
    await labels.delete(NB, [LABEL_MISSING, LABEL_A, LABEL_A])
    await collections.delete([COLLECTION_MISSING, COLLECTION_A])
    assert LABEL_A not in server.labels[NB]
    assert COLLECTION_A not in server.collections

    deletes = [
        request for method, request, _kwargs in server.calls if method == DELETE_LABELS_METHOD
    ]
    assert deletes[0].project_id == NB
    assert deletes[0].label_ids == [LABEL_A]
    assert deletes[1].label_type == COLLECTION_TYPE
    assert deletes[1].label_ids == [COLLECTION_A]
    assert server.operation_scopes == [("labels.delete", None), ("collections.delete", None)]


async def test_generate_uses_native_presence_sensitive_scope_and_returns_readback() -> None:
    server = FakeOrganizationServer()
    labels, _collections = _apis(server)

    incremental = await labels.generate(NB)
    assert [label.id for label in incremental] == [LABEL_A, LABEL_B]
    incremental_request = server.calls[0][1]
    assert incremental_request.WhichOneof("create_mode") == "auto_create"
    assert incremental_request.auto_create.HasField("regenerate_all")
    assert incremental_request.auto_create.regenerate_all is False

    regenerated = await labels.generate(NB, scope="all")
    assert [label.id for label in regenerated] == [LABEL_B]
    regenerate_request = server.calls[2][1]
    assert regenerate_request.WhichOneof("create_mode") == "auto_create"
    assert regenerate_request.auto_create.HasField("regenerate_all")
    assert regenerate_request.auto_create.regenerate_all is True
    assert [method for method, _request, _kwargs in server.calls] == [
        CREATE_LABEL_METHOD,
        GET_LABELS_METHOD,
        CREATE_LABEL_METHOD,
        GET_LABELS_METHOD,
    ]
    assert server.operation_scopes == [("labels.generate", None), ("labels.generate", None)]
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in server.calls] == [7, 7, 7, 7]


@pytest.mark.parametrize(
    "read_error",
    [
        NetworkError("readback lost"),
        AuthError("readback auth rejected", rpc_code=16),
        RPCError("readback project missing", rpc_code=5),
        DecodingError("readback malformed"),
        ValueError("unexpected projection failure"),
    ],
    ids=["network", "auth", "not-found", "decoding", "unexpected"],
)
async def test_generate_failed_post_write_readback_is_unconfirmed_without_resend(
    read_error: Exception,
) -> None:
    server = FakeOrganizationServer()
    server.failures[2] = read_error
    labels, _collections = _apis(server)

    with pytest.raises(type(read_error)) as raised:
        await labels.generate(NB)

    if isinstance(read_error, RPCError) and read_error.rpc_code == 5:
        assert isinstance(raised.value, NotebookNotFoundError)
    else:
        assert raised.value is read_error
    assert getattr(raised.value, "unconfirmed", False) is True
    assert [method for method, _request, _kwargs in server.calls] == [
        CREATE_LABEL_METHOD,
        GET_LABELS_METHOD,
    ]


async def test_generate_validation_and_empty_operations_avoid_android_transport() -> None:
    server = FakeOrganizationServer()
    labels, collections = _apis(server)
    with pytest.raises(ValueError, match="generate scope"):
        await labels.generate(NB, scope=cast(Any, "invalid"))
    with pytest.raises(ValueError):
        await labels.update(NB, LABEL_A)
    with pytest.raises(ValueError):
        await labels.add_sources(NB, LABEL_A, [])
    with pytest.raises(ValueError):
        await collections.remove_notebooks(COLLECTION_A, [])
    assert await labels.delete(NB, []) is None
    assert await collections.delete([]) is None
    assert server.calls == []
    assert server.operation_scopes == []


async def test_status_five_maps_to_public_miss_and_retired_epoch_stops_later_io() -> None:
    server = FakeOrganizationServer()
    server.labels[NB].pop(LABEL_A)
    server.failures[1] = RPCError("missing", method_id=MUTATE_LABEL_METHOD, rpc_code=5)
    labels, _collections = _apis(server)
    with pytest.raises(LabelNotFoundError) as caught:
        await labels.add_sources(NB, LABEL_A, [SOURCE_B])
    assert caught.value.label_id == LABEL_A
    assert [method for method, _request, _kwargs in server.calls] == [
        MUTATE_LABEL_METHOD,
        GET_LABELS_METHOD,
    ]

    retired = FakeOrganizationServer()
    retired.failures[1] = RuntimeError("retired resource generation")
    labels, _collections = _apis(retired)
    with pytest.raises(RuntimeError, match="retired resource generation"):
        await labels.create(NB, "uncertain")
    assert [method for method, _request, _kwargs in retired.calls] == [CREATE_LABEL_METHOD]


async def test_real_supervisor_outer_lease_keeps_create_alive_during_graceful_drain() -> None:
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        max_concurrent_rpcs=None,
    )
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)

    class _SupervisedServer(FakeOrganizationServer):
        epoch = 1

        def __init__(self) -> None:
            super().__init__()
            self.write_started = asyncio.Event()
            self.release_write = asyncio.Event()

        def operation_scope(self, label: str, *, expected_epoch: int | None = None):
            return supervisor.operation_scope(label, expected_epoch=expected_epoch)

        async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
            if method == CREATE_LABEL_METHOD:
                self.write_started.set()
                await self.release_write.wait()
            return await super().unary(method, request, **kwargs)

    server = _SupervisedServer()
    _labels, collections = _apis(server)
    create_task = asyncio.create_task(collections.create("during-drain"))
    await server.write_started.wait()
    await supervisor.stop_accepting(1)
    idle_task = asyncio.create_task(supervisor.wait_for_idle(1, timeout=1.0))
    await asyncio.sleep(0)
    assert not idle_task.done()

    server.release_write.set()
    created = await create_task
    await idle_task
    assert created.id == COLLECTION_B
    assert [method for method, _request, _kwargs in server.calls] == [
        GET_LABELS_METHOD,
        CREATE_LABEL_METHOD,
    ]
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in server.calls] == [1, 1]


async def test_readback_mismatch_is_decode_failure_for_properties_and_membership() -> None:
    server = FakeOrganizationServer()
    server.ignore_mutations = True
    labels, collections = _apis(server)
    with pytest.raises(DecodingError, match="properties"):
        await labels.rename(NB, LABEL_A, "not-applied")

    server.calls.clear()
    server.operation_scopes.clear()
    with pytest.raises(DecodingError, match="membership"):
        await collections.add_notebooks(COLLECTION_A, [NB_B])


def test_strict_codecs_reject_malformed_ids_without_echoing_them() -> None:
    from notebooklm._android.codecs.organization import decode_collections, decode_labels

    response = organization_mutations_pb2.GetLabelsWireResponse(
        labels=[organization_mutations_pb2.OrganizationRecordWire(id="not-an-id")]
    )
    with pytest.raises(DecodingError, match="malformed label ID") as caught:
        decode_labels(response, NB, method_id=GET_LABELS_METHOD)
    assert "not-an-id" not in str(caught.value)

    response = organization_mutations_pb2.GetLabelsWireResponse(
        collections=[
            organization_mutations_pb2.OrganizationRecordWire(
                id=COLLECTION_A,
                member_ids=[b"not-an-id"],
            )
        ]
    )
    with pytest.raises(DecodingError, match="malformed notebook member ID"):
        decode_collections(response, method_id=GET_LABELS_METHOD)


# ---------------------------------------------------------------------------
# Read-back and write-failure branches shared by both organization adapters
# ---------------------------------------------------------------------------


class _VanishingResourceServer(FakeOrganizationServer):
    """Drops the mutated label/collection right after the write lands.

    Models a concurrent delete landing between the mutation and the read-back,
    which is the only way the adapters' post-write ``is None`` guards fire.
    """

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        response = await super().unary(method, request, **kwargs)
        if method == MUTATE_LABEL_METHOD:
            self.labels[NB].pop(LABEL_A, None)
            self.collections.pop(COLLECTION_A, None)
        return response


class _DeleteThenReportMissingServer(FakeOrganizationServer):
    """Applies the delete, then answers ``NOT_FOUND`` — a benign double-delete."""

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        response = await super().unary(method, request, **kwargs)
        if method == DELETE_LABELS_METHOD:
            raise RPCError("already deleted", method_id=DELETE_LABELS_METHOD, rpc_code=5)
        return response


async def test_label_get_returns_the_matching_row() -> None:
    labels, _collections = _apis(FakeOrganizationServer())

    label = await labels.get(NB, LABEL_A)

    assert (label.id, label.name, label.emoji) == (LABEL_A, "Papers", "📄")


async def test_collection_get_returns_the_matching_row() -> None:
    _labels, collections = _apis(FakeOrganizationServer())

    collection = await collections.get(COLLECTION_A)

    assert (collection.id, collection.name) == (COLLECTION_A, "Research")


async def test_label_sources_rejects_an_absent_label() -> None:
    labels, _collections = _apis(FakeOrganizationServer())

    with pytest.raises(LabelNotFoundError):
        await labels.sources(NB, LABEL_MISSING)


async def test_collection_notebooks_rejects_an_absent_collection() -> None:
    _labels, collections = _apis(FakeOrganizationServer())

    with pytest.raises(CollectionNotFoundError):
        await collections.notebooks(COLLECTION_MISSING)


@pytest.mark.parametrize("kind", ["label", "collection"])
async def test_create_treats_an_undecodable_echo_as_unconfirmed(kind: str) -> None:
    """A malformed created row cannot be reported as a clean success."""
    server = FakeOrganizationServer()
    if kind == "label":
        server.create_response_override = organization_pb2.CreateLabelResponse(
            label_and_sources=[organization_pb2.LabelAndSources(label="X", label_id="not-a-uuid")]
        )
    else:
        server.create_response_override = organization_pb2.CreateLabelResponse(
            notebook_collections=[organization_pb2.NotebookCollection(name="X", id="not-a-uuid")]
        )
    labels, collections = _apis(server)

    with pytest.raises(DecodingError) as caught:
        if kind == "label":
            await labels.create(NB, "Requested")
        else:
            await collections.create("Requested")

    assert getattr(caught.value, "unconfirmed", False) is True


async def test_label_update_rejects_an_absent_label() -> None:
    labels, _collections = _apis(FakeOrganizationServer())

    with pytest.raises(LabelNotFoundError):
        await labels.update(NB, LABEL_MISSING, name="New")


async def test_collection_update_rejects_an_absent_collection() -> None:
    _labels, collections = _apis(FakeOrganizationServer())

    with pytest.raises(CollectionNotFoundError):
        await collections.rename(COLLECTION_MISSING, "New")


@pytest.mark.parametrize("kind", ["label", "collection"])
async def test_property_write_not_found_maps_to_the_typed_miss(kind: str) -> None:
    """The property write has already proven the resource existed on read."""
    server = FakeOrganizationServer()
    server.failures[2] = RPCError("gone", method_id=MUTATE_LABEL_METHOD, rpc_code=5)
    labels, collections = _apis(server)

    expected = LabelNotFoundError if kind == "label" else CollectionNotFoundError
    with pytest.raises(expected):
        if kind == "label":
            await labels.update(NB, LABEL_A, name="New")
        else:
            await collections.rename(COLLECTION_A, "New")


@pytest.mark.parametrize("kind", ["label", "collection"])
async def test_property_write_other_rpc_errors_propagate_unchanged(kind: str) -> None:
    server = FakeOrganizationServer()
    denied = RPCError("denied", method_id=MUTATE_LABEL_METHOD, rpc_code=7)
    server.failures[2] = denied
    labels, collections = _apis(server)

    with pytest.raises(RPCError) as caught:
        if kind == "label":
            await labels.update(NB, LABEL_A, name="New")
        else:
            await collections.rename(COLLECTION_A, "New")

    assert caught.value is denied


@pytest.mark.parametrize("kind", ["label", "collection"])
async def test_property_write_read_back_absence_maps_to_the_typed_miss(kind: str) -> None:
    labels, collections = _apis(_VanishingResourceServer())

    expected = LabelNotFoundError if kind == "label" else CollectionNotFoundError
    with pytest.raises(expected) as caught:
        if kind == "label":
            await labels.update(NB, LABEL_A, name="New")
        else:
            await collections.rename(COLLECTION_A, "New")

    assert caught.value.method_id == MUTATE_LABEL_METHOD


@pytest.mark.parametrize("kind", ["label", "collection"])
async def test_property_write_that_does_not_stick_is_reported_as_drift(kind: str) -> None:
    server = FakeOrganizationServer()
    server.ignore_mutations = True
    labels, collections = _apis(server)

    with pytest.raises(DecodingError, match="did not read back the requested properties"):
        if kind == "label":
            await labels.update(NB, LABEL_A, name="Renamed")
        else:
            await collections.rename(COLLECTION_A, "Renamed")


@pytest.mark.parametrize("kind", ["label", "collection"])
async def test_membership_read_back_absence_maps_to_the_typed_miss(kind: str) -> None:
    labels, collections = _apis(_VanishingResourceServer())

    expected = LabelNotFoundError if kind == "label" else CollectionNotFoundError
    with pytest.raises(expected):
        if kind == "label":
            await labels.add_sources(NB, LABEL_A, [SOURCE_B])
        else:
            await collections.add_notebooks(COLLECTION_A, [NB_B])


@pytest.mark.parametrize("kind", ["label", "collection"])
async def test_membership_write_that_does_not_stick_is_reported_as_drift(kind: str) -> None:
    server = FakeOrganizationServer()
    server.ignore_mutations = True
    labels, collections = _apis(server)

    with pytest.raises(DecodingError, match="did not read back the requested state"):
        if kind == "label":
            await labels.add_sources(NB, LABEL_A, [SOURCE_B])
        else:
            await collections.add_notebooks(COLLECTION_A, [NB_B])


async def test_label_membership_other_rpc_errors_skip_the_absence_probe() -> None:
    """Only ``NOT_FOUND`` is ambiguous — anything else propagates immediately."""
    server = FakeOrganizationServer()
    denied = RPCError("denied", method_id=MUTATE_LABEL_METHOD, rpc_code=7)
    server.failures[1] = denied
    labels, _collections = _apis(server)

    with pytest.raises(RPCError) as caught:
        await labels.add_sources(NB, LABEL_A, [SOURCE_B])

    assert caught.value is denied
    assert [method for method, _request, _kwargs in server.calls] == [MUTATE_LABEL_METHOD]


@pytest.mark.parametrize("kind", ["label", "collection"])
async def test_delete_of_only_unknown_ids_issues_no_write(kind: str) -> None:
    server = FakeOrganizationServer()
    labels, collections = _apis(server)

    if kind == "label":
        await labels.delete(NB, [LABEL_MISSING])
    else:
        await collections.delete([COLLECTION_MISSING])

    assert [method for method, _request, _kwargs in server.calls] == [GET_LABELS_METHOD]


@pytest.mark.parametrize("kind", ["label", "collection"])
async def test_delete_tolerates_a_not_found_answer_once_absence_is_proven(kind: str) -> None:
    server = _DeleteThenReportMissingServer()
    labels, collections = _apis(server)

    if kind == "label":
        await labels.delete(NB, LABEL_A)
        assert LABEL_A not in server.labels[NB]
    else:
        await collections.delete(COLLECTION_A)
        assert COLLECTION_A not in server.collections


@pytest.mark.parametrize("kind", ["label", "collection"])
async def test_delete_propagates_non_not_found_rpc_errors(kind: str) -> None:
    server = FakeOrganizationServer()
    denied = RPCError("denied", method_id=DELETE_LABELS_METHOD, rpc_code=7)
    server.failures[2] = denied
    labels, collections = _apis(server)

    with pytest.raises(RPCError) as caught:
        if kind == "label":
            await labels.delete(NB, LABEL_A)
        else:
            await collections.delete(COLLECTION_A)

    assert caught.value is denied


@pytest.mark.parametrize("kind", ["label", "collection"])
async def test_delete_that_does_not_remove_the_row_is_reported_as_drift(kind: str) -> None:
    """A swallowed ``NOT_FOUND`` still has to prove absence on read-back."""
    server = FakeOrganizationServer()
    server.failures[2] = RPCError("gone", method_id=DELETE_LABELS_METHOD, rpc_code=5)
    labels, collections = _apis(server)

    with pytest.raises(DecodingError, match="did not read back absence"):
        if kind == "label":
            await labels.delete(NB, LABEL_A)
        else:
            await collections.delete(COLLECTION_A)
