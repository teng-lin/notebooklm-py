"""Characterization for the P6.4 labels + collections semantic slice.

Labels and collections migrate together because they are one wire surface: they
share ``agX4Bc`` / ``I3xc3c`` / ``le8sX`` / ``GyzE7e`` verbatim and differ only
by an explicit type discriminator. This module pins what the migration must not
change — per-call RPC inventories, the discriminated request shapes, exact-ID
selection, the not-found and ambiguity contracts, and the neutral records and
diagnostics that flow across the semantic boundary.

``tests/unit/test_labels_api.py`` and ``tests/unit/test_collections_api.py``
remain the per-facade behavior gates; this module pins the *seam* between them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from notebooklm._backend import (
    BackendContractError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
)
from notebooklm._collections import CollectionsAPI
from notebooklm._label_service import LabelSetService, require_member_ids
from notebooklm._labels import LabelsAPI
from notebooklm._operations import CallPolicy, Operation
from notebooklm._semantic.compat import project_backend_error
from notebooklm._semantic.projectors import project_collection, project_label
from notebooklm._semantic.records import (
    COLLECTION_CREATE_DEF,
    COLLECTION_DELETE_DEF,
    COLLECTION_GET_DEF,
    COLLECTION_LIST_DEF,
    COLLECTION_UPDATE_DEF,
    LABEL_CREATE_DEF,
    LABEL_DELETE_DEF,
    LABEL_GENERATE_DEF,
    LABEL_GET_DEF,
    LABEL_LIST_DEF,
    LABEL_UPDATE_DEF,
    LabelGetInput,
    LabelKind,
    LabelRecord,
    LabelUpdateInput,
)
from notebooklm._web.codec.labels import decode_label_create_echo, decode_label_list
from notebooklm.exceptions import (
    CollectionError,
    CollectionNotFoundError,
    LabelError,
    LabelNotFoundError,
    UnknownRPCMethodError,
)
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_NB = "nb_1"


def _label_tuple(
    name: str, label_id: str, *, emoji: str = "", src: list[str] | None = None
) -> list[Any]:
    return [name, [[s] for s in src] if src else None, label_id, emoji]


def _collection_tuple(
    name: str, collection_id: str, *, emoji: str = "", nbs: list[str] | None = None
) -> list[Any]:
    # Populated collection members are BARE id strings, not the label's wrapped
    # singletons — the one row-grammar difference between the two dialects.
    return [name, list(nbs) if nbs else None, collection_id, emoji]


class _FakeRpc:
    """Records every dispatch the semantic backend performs, in order."""

    def __init__(
        self,
        responses: dict[RPCMethod, Any] | None = None,
        sequences: dict[RPCMethod, list[Any]] | None = None,
    ) -> None:
        self.calls: list[SimpleNamespace] = []
        self.responses = responses or {}
        self.sequences = {method: list(rows) for method, rows in (sequences or {}).items()}

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            SimpleNamespace(
                method=method,
                params=params,
                source_path=source_path,
                allow_null=allow_null,
                operation_variant=kwargs.get("operation_variant"),
            )
        )
        queue = self.sequences.get(method)
        if queue:
            return queue.pop(0)
        return self.responses.get(method)

    def methods(self) -> list[RPCMethod]:
        return [call.method for call in self.calls]


def _labels(
    responses: dict[RPCMethod, Any] | None = None,
    *,
    sources: list[Any] | None = None,
) -> tuple[LabelsAPI, _FakeRpc]:
    rpc = _FakeRpc(responses)
    api = LabelsAPI(
        build_web_backend(rpc),
        list_sources=AsyncMock(return_value=sources or []),
    )
    return api, rpc


def _collections(
    responses: dict[RPCMethod, Any] | None = None,
    *,
    sequences: dict[RPCMethod, list[Any]] | None = None,
    notebooks: list[Any] | None = None,
) -> tuple[CollectionsAPI, _FakeRpc]:
    rpc = _FakeRpc(responses, sequences)
    api = CollectionsAPI(
        build_web_backend(rpc),
        list_notebooks=AsyncMock(return_value=notebooks or []),
    )
    return api, rpc


# -- one operation family, two discriminated keys ----------------------------


def test_both_dialects_share_record_types_and_split_only_on_the_discriminator() -> None:
    pairs = (
        (LABEL_LIST_DEF, COLLECTION_LIST_DEF, CallPolicy.READ),
        (LABEL_GET_DEF, COLLECTION_GET_DEF, CallPolicy.READ),
        (LABEL_CREATE_DEF, COLLECTION_CREATE_DEF, CallPolicy.MUTATION),
        (LABEL_UPDATE_DEF, COLLECTION_UPDATE_DEF, CallPolicy.MUTATION),
        (LABEL_DELETE_DEF, COLLECTION_DELETE_DEF, CallPolicy.MUTATION),
    )
    for label_def, collection_def, policy in pairs:
        assert label_def.input_type is collection_def.input_type
        assert label_def.output_type is collection_def.output_type
        assert label_def.policy is collection_def.policy is policy
        assert label_def.key is not collection_def.key
    # Auto-grouping is a source-label-only wire mode; it has no collection twin.
    assert LABEL_GENERATE_DEF.key is Operation.LABEL_GENERATE
    assert LABEL_GENERATE_DEF.policy is CallPolicy.STATEFUL_START


def test_service_binds_one_kind_for_its_whole_lifetime() -> None:
    backend = build_web_backend(_FakeRpc())
    assert LabelSetService(backend, LabelKind.SOURCE_LABEL).kind is LabelKind.SOURCE_LABEL
    assert LabelSetService(backend, LabelKind.COLLECTION).kind is LabelKind.COLLECTION


async def test_backend_rejects_a_request_addressed_to_the_other_dialect() -> None:
    """P9.2: the update workflows are service-owned, so the backend refuses them
    outright and the dialect is the service's identity; the leaf reads still
    fail closed when a request addresses the other dialect's operation."""
    backend = build_web_backend(_FakeRpc())
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            COLLECTION_UPDATE_DEF,
            LabelUpdateInput(LabelKind.SOURCE_LABEL, "l1", _NB, name="X"),
            deadline=None,
        )
    with pytest.raises(BackendContractError, match="requires a collection request"):
        await backend.invoke(
            COLLECTION_GET_DEF,
            LabelGetInput(LabelKind.SOURCE_LABEL, "l1", _NB),
            deadline=None,
        )


async def test_collection_update_has_no_emoji_only_field_mask() -> None:
    rpc = _FakeRpc()
    service = LabelSetService(build_web_backend(rpc), LabelKind.COLLECTION)
    with pytest.raises(BackendContractError, match="a name is required") as caught:
        await service.update("c1", emoji="\U0001f525")
    assert caught.value.operation is Operation.COLLECTION_UPDATE
    assert rpc.calls == []


# -- codec: one authority, two envelopes -------------------------------------


def test_codec_decodes_both_envelopes_into_the_same_neutral_shape() -> None:
    labels = decode_label_list(
        [[_label_tuple("A", "l1", emoji="\U0001f4c4", src=["s1", "s2"])]],
        kind=LabelKind.SOURCE_LABEL,
        notebook_id=_NB,
        method_id=RPCMethod.LIST_LABELS.value,
    )
    collections = decode_label_list(
        [None, [_collection_tuple("A", "c1", emoji="\U0001f4c4", nbs=["s1", "s2"])]],
        kind=LabelKind.COLLECTION,
        notebook_id=None,
        method_id=RPCMethod.LIST_LABELS.value,
    )
    assert labels == (
        LabelRecord(
            id="l1",
            name="A",
            kind=LabelKind.SOURCE_LABEL,
            notebook_id=_NB,
            emoji="\U0001f4c4",
            member_ids=("s1", "s2"),
        ),
    )
    assert collections == (
        LabelRecord(
            id="c1",
            name="A",
            kind=LabelKind.COLLECTION,
            notebook_id=None,
            emoji="\U0001f4c4",
            member_ids=("s1", "s2"),
        ),
    )


def test_codec_treats_an_absent_set_as_empty_and_a_malformed_one_as_drift() -> None:
    for kind in LabelKind:
        assert (
            decode_label_list(
                None, kind=kind, notebook_id=_NB, method_id=RPCMethod.LIST_LABELS.value
            )
            == ()
        )
        with pytest.raises(UnknownRPCMethodError):
            decode_label_list(
                "drifted", kind=kind, notebook_id=_NB, method_id=RPCMethod.LIST_LABELS.value
            )
    assert (
        decode_label_create_echo(
            [None, None], notebook_id=_NB, method_id=RPCMethod.CREATE_LABEL.value
        )
        == ()
    )


def test_projectors_refuse_to_cross_the_discriminator() -> None:
    label = LabelRecord(id="l1", name="A", kind=LabelKind.SOURCE_LABEL, notebook_id=_NB)
    collection = LabelRecord(id="c1", name="A", kind=LabelKind.COLLECTION)
    assert project_label(label).notebook_id == _NB
    assert project_collection(collection).notebook_ids == []
    with pytest.raises(ValueError, match="as a source Label"):
        project_label(collection)
    with pytest.raises(ValueError, match="as a Collection"):
        project_collection(label)


# -- per-call RPC inventories ------------------------------------------------


async def test_label_reads_issue_exactly_one_list_labels_on_the_notebook_path() -> None:
    api, rpc = _labels({RPCMethod.LIST_LABELS: [[_label_tuple("A", "l1")]]})
    await api.list(_NB)
    await api.get_or_none(_NB, "l1")
    await api.get(_NB, "l1")
    assert rpc.methods() == [RPCMethod.LIST_LABELS] * 3
    assert {call.source_path for call in rpc.calls} == {f"/notebook/{_NB}"}
    assert {call.allow_null for call in rpc.calls} == {False}


async def test_collection_reads_issue_one_list_labels_on_the_account_path() -> None:
    api, rpc = _collections({RPCMethod.LIST_LABELS: [None, [_collection_tuple("A", "c1")]]})
    await api.list()
    await api.get_or_none("c1")
    await api.get("c1")
    assert rpc.methods() == [RPCMethod.LIST_LABELS] * 3
    assert {call.source_path for call in rpc.calls} == {"/"}
    # An account with zero collections may echo a null envelope, which must
    # decode to an empty set rather than raising.
    assert {call.allow_null for call in rpc.calls} == {True}
    assert {call.params[-1] for call in rpc.calls} == {3}


async def test_label_create_settles_from_the_echo_but_collection_create_re_lists() -> None:
    label_api, label_rpc = _labels(
        {
            RPCMethod.LIST_LABELS: [[_label_tuple("A", "l1")]],
            RPCMethod.CREATE_LABEL: [None, [_label_tuple("A", "l1"), _label_tuple("New", "l2")]],
        }
    )
    assert (await label_api.create(_NB, "New")).id == "l2"
    assert label_rpc.methods() == [RPCMethod.LIST_LABELS, RPCMethod.CREATE_LABEL]

    collection_api, collection_rpc = _collections(
        sequences={
            RPCMethod.LIST_LABELS: [
                [None, [_collection_tuple("A", "c1")]],
                [None, [_collection_tuple("A", "c1"), _collection_tuple("New", "c2")]],
            ]
        }
    )
    assert (await collection_api.create("New")).id == "c2"
    assert collection_rpc.methods() == [
        RPCMethod.LIST_LABELS,
        RPCMethod.CREATE_LABEL,
        RPCMethod.LIST_LABELS,
    ]


async def test_field_update_preflights_and_reads_back_only_when_asked() -> None:
    api, rpc = _labels({RPCMethod.LIST_LABELS: [[_label_tuple("Old", "l1", emoji="\U0001f4c4")]]})
    await api.rename(_NB, "l1", "New")
    assert rpc.methods() == [RPCMethod.LIST_LABELS, RPCMethod.UPDATE_LABEL, RPCMethod.LIST_LABELS]
    # The preflight emoji rides the field mask so a rename never clobbers it.
    assert rpc.calls[1].params[3] == [[["New", "\U0001f4c4"]]]
    assert rpc.calls[1].operation_variant is None

    api, rpc = _labels({RPCMethod.LIST_LABELS: [[_label_tuple("Old", "l1")]]})
    assert await api.rename(_NB, "l1", "New", return_object=False) is None
    assert rpc.methods() == [RPCMethod.LIST_LABELS, RPCMethod.UPDATE_LABEL]


async def test_membership_writes_are_one_call_per_id_plus_one_mandatory_readback() -> None:
    api, rpc = _labels(
        {
            RPCMethod.LIST_LABELS: [[_label_tuple("A", "l1", src=["s1"])]],
            RPCMethod.UPDATE_LABEL: [],
        }
    )
    # ``return_object=False`` still re-reads: le8sX echoes [] and carries no
    # label, so that read is the only evidence the target exists.
    assert await api.add_sources(_NB, "l1", ["s1", "s2", "s1"], return_object=False) is None
    assert rpc.methods() == [
        RPCMethod.UPDATE_LABEL,
        RPCMethod.UPDATE_LABEL,
        RPCMethod.LIST_LABELS,
    ]
    assert [call.operation_variant for call in rpc.calls[:2]] == ["add_sources"] * 2
    assert [call.params[3] for call in rpc.calls[:2]] == [
        [[None, [["s1"]]]],
        [[None, [["s2"]]]],
    ]

    api, rpc = _collections({RPCMethod.LIST_LABELS: [None, [_collection_tuple("A", "c1")]]})
    await api.remove_notebooks("c1", ["n1"])
    assert rpc.methods() == [RPCMethod.UPDATE_LABEL, RPCMethod.LIST_LABELS]
    assert rpc.calls[0].operation_variant == "remove_notebooks"
    assert rpc.calls[0].params[3] == [[None, None, None, None, [["n1"]]], []]
    assert rpc.calls[0].source_path == "/"


async def test_empty_batches_and_invalid_arguments_never_reach_the_wire() -> None:
    api, rpc = _labels()
    assert await api.delete(_NB, []) is None
    with pytest.raises(ValueError):
        await api.update(_NB, "l1")
    with pytest.raises(ValueError):
        await api.add_sources(_NB, "l1", [])
    with pytest.raises(ValueError):
        await api.generate(_NB, scope="bogus")  # type: ignore[arg-type]
    collections_api, collections_rpc = _collections()
    assert await collections_api.delete([]) is None
    with pytest.raises(ValueError):
        await collections_api.remove_notebooks("c1", [])
    assert rpc.calls == collections_rpc.calls == []


def test_membership_validation_dedupes_and_names_the_domain_noun() -> None:
    assert require_member_ids(["a", "b", "a"], "add_sources", "source") == ("a", "b")
    with pytest.raises(ValueError, match="add_notebooks requires at least one notebook id"):
        require_member_ids([], "add_notebooks", "notebook")


# -- not-found and ambiguity travel as neutral evidence ----------------------


@pytest.mark.parametrize(
    ("kind", "reason", "expected"),
    [
        (LabelKind.SOURCE_LABEL, BackendErrorReason.LABEL_NOT_FOUND, LabelNotFoundError),
        (LabelKind.COLLECTION, BackendErrorReason.LABEL_NOT_FOUND, CollectionNotFoundError),
        (LabelKind.SOURCE_LABEL, BackendErrorReason.LABEL_AMBIGUOUS_CREATE, LabelError),
        (LabelKind.COLLECTION, BackendErrorReason.LABEL_AMBIGUOUS_CREATE, CollectionError),
    ],
)
def test_the_discriminator_picks_the_public_exception_class(
    kind: LabelKind,
    reason: BackendErrorReason,
    expected: type[Exception],
) -> None:
    projected = project_backend_error(
        BackendError(
            "boom",
            operation=Operation.LABEL_UPDATE,
            diagnostics={
                "label_kind": kind.value,
                "label_id": "x1",
                "method_id": RPCMethod.UPDATE_LABEL.value,
            },
            reason=reason,
        )
    )
    assert type(projected) is expected


def test_a_label_error_without_a_discriminator_fails_closed() -> None:
    with pytest.raises(BackendContractError, match="invalid label kind discriminator"):
        project_backend_error(
            BackendError(
                "boom",
                operation=Operation.LABEL_GET,
                diagnostics={"label_id": "x1"},
                reason=BackendErrorReason.LABEL_NOT_FOUND,
            )
        )


async def test_mutations_raise_not_found_with_the_method_that_proved_absence() -> None:
    api, _ = _labels({RPCMethod.LIST_LABELS: [[]], RPCMethod.UPDATE_LABEL: []})
    with pytest.raises(LabelNotFoundError) as preflight:
        await api.rename(_NB, "missing", "X", return_object=False)
    assert preflight.value.label_id == "missing"
    assert preflight.value.method_id == RPCMethod.UPDATE_LABEL.value

    with pytest.raises(LabelNotFoundError) as membership:
        await api.add_sources(_NB, "missing", ["s1"], return_object=False)
    assert membership.value.method_id == RPCMethod.UPDATE_LABEL.value

    collections_api, _ = _collections({RPCMethod.LIST_LABELS: [None, []]})
    with pytest.raises(CollectionNotFoundError) as collection_miss:
        await collections_api.rename("missing", "X", return_object=False)
    assert collection_miss.value.collection_id == "missing"


async def test_ambiguous_creates_stay_loud_in_both_dialects() -> None:
    api, _ = _labels(
        {
            RPCMethod.LIST_LABELS: [[]],
            RPCMethod.CREATE_LABEL: [None, [_label_tuple("A", "l1"), _label_tuple("B", "l2")]],
        }
    )
    with pytest.raises(LabelError, match="expected exactly 1 new label, found 2"):
        await api.create(_NB, "X")

    collections_api, _ = _collections({RPCMethod.LIST_LABELS: [None, []]})
    with pytest.raises(CollectionError, match="expected exactly 1 new collection, found 0"):
        await collections_api.create("X")


async def test_transport_drift_is_replayed_as_the_public_decoding_error() -> None:
    api, _ = _labels({RPCMethod.LIST_LABELS: "drifted"})
    with pytest.raises(UnknownRPCMethodError) as drift:
        await api.list(_NB)
    assert drift.value.method_id == RPCMethod.LIST_LABELS.value

    collections_api, _ = _collections({RPCMethod.LIST_LABELS: [None, "drifted"]})
    with pytest.raises(UnknownRPCMethodError):
        await collections_api.list()


# -- membership joins stay client-side ---------------------------------------


async def test_membership_joins_follow_membership_order_and_skip_absent_members() -> None:
    api, rpc = _labels(
        {RPCMethod.LIST_LABELS: [[_label_tuple("A", "l1", src=["s2", "s1", "gone"])]]},
        sources=[SimpleNamespace(id="s1"), SimpleNamespace(id="s2")],
    )
    assert [source.id for source in await api.sources(_NB, "l1")] == ["s2", "s1"]
    assert rpc.methods() == [RPCMethod.LIST_LABELS]

    collections_api, collections_rpc = _collections(
        {RPCMethod.LIST_LABELS: [None, [_collection_tuple("A", "c1", nbs=["n2", "n1", "gone"])]]},
        notebooks=[SimpleNamespace(id="n1"), SimpleNamespace(id="n2")],
    )
    assert [nb.id for nb in await collections_api.notebooks("c1")] == ["n2", "n1"]
    assert collections_rpc.methods() == [RPCMethod.LIST_LABELS]
