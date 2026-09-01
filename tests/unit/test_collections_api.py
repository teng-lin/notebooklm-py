"""Unit tests for CollectionsAPI over a mock RpcCaller (no HTTP/VCR — unit tier)."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from notebooklm._web.collections import WebCollectionsAPI
from notebooklm.exceptions import (
    CollectionError,
    CollectionNotFoundError,
    UnknownRPCMethodError,
)
from notebooklm.rpc import RPCMethod


def _collection_tuple(
    name: str, collection_id: str, *, emoji: str = "", nbs: list[str] | None = None
) -> list[Any]:
    # Populated member ids are BARE strings on the wire (PR #2009), not the
    # label-style [[id], ...] wrapped singletons.
    notebook_ids = list(nbs) if nbs else None
    return [name, notebook_ids, collection_id, emoji]


def _list_env(*tuples: list[Any]) -> list[Any]:
    # Collections' LIST echoes [None, [collection, ...]] — the set is at index 1
    # (unlike source labels' LIST_LABELS, which is [[label, ...]] at index 0).
    return [None, list(tuples)]


class FakeRpc:
    """Mock RpcCaller. ``sequences[method]`` is consumed FIFO (for re-list flows
    like ``create``); otherwise the static ``responses[method]`` is returned."""

    def __init__(
        self,
        responses: dict[RPCMethod, Any] | None = None,
        sequences: dict[RPCMethod, list[Any]] | None = None,
    ) -> None:
        self.calls: list[SimpleNamespace] = []
        self.responses = responses or {}
        self.sequences = {m: deque(v) for m, v in (sequences or {}).items()}

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
        raise_on_null_status: bool = False,
    ) -> Any:
        self.calls.append(
            SimpleNamespace(
                method=method,
                params=params,
                source_path=source_path,
                allow_null=allow_null,
                operation_variant=operation_variant,
                raise_on_null_status=raise_on_null_status,
            )
        )
        queue = self.sequences.get(method)
        if queue:
            return queue.popleft()
        return self.responses.get(method)

    def methods(self) -> list[RPCMethod]:
        return [c.method for c in self.calls]


def _api(
    responses: dict[RPCMethod, Any] | None = None,
    *,
    sequences: dict[RPCMethod, list[Any]] | None = None,
    notebooks: list[Any] | None = None,
):
    rpc = FakeRpc(responses, sequences)
    list_notebooks = AsyncMock(return_value=notebooks or [])
    return WebCollectionsAPI(rpc, list_notebooks=list_notebooks), rpc, list_notebooks


# -- read --------------------------------------------------------------------


async def test_list_decodes_index_one_envelope_and_account_path() -> None:
    api, rpc, _ = _api({RPCMethod.LIST_LABELS: _list_env(_collection_tuple("A", "c1", nbs=["n1"]))})
    collections = await api.list()
    assert [c.id for c in collections] == ["c1"]
    assert collections[0].notebook_ids == ["n1"]
    assert rpc.methods() == [RPCMethod.LIST_LABELS]
    # Account-level: source_path must be "/", never /notebook/<id>.
    assert rpc.calls[0].source_path == "/"
    assert rpc.calls[0].params[-1] == 3  # type-3 discriminator
    assert rpc.calls[0].allow_null is True


async def test_list_null_envelope_is_empty() -> None:
    # A zero-collection account may echo a null envelope → [] (not a raise).
    api, _, _ = _api({RPCMethod.LIST_LABELS: None})
    assert await api.list() == []


async def test_list_truthy_non_list_envelope_raises_drift() -> None:
    api, _, _ = _api({RPCMethod.LIST_LABELS: "unexpected-string"})
    with pytest.raises(UnknownRPCMethodError):
        await api.list()


async def test_get_or_none_and_get() -> None:
    api, _, _ = _api({RPCMethod.LIST_LABELS: _list_env(_collection_tuple("A", "c1"))})
    assert (await api.get_or_none("c1")).id == "c1"
    assert await api.get_or_none("missing") is None
    assert (await api.get("c1")).id == "c1"


async def test_get_raises_not_found_with_method_id() -> None:
    api, _, _ = _api({RPCMethod.LIST_LABELS: _list_env()})
    with pytest.raises(CollectionNotFoundError) as exc:
        await api.get("missing")
    assert exc.value.collection_id == "missing"
    assert exc.value.method_id == RPCMethod.LIST_LABELS.value


# -- create (re-list diff) ---------------------------------------------------


async def test_create_returns_the_new_id_via_relist() -> None:
    api, rpc, _ = _api(
        sequences={
            RPCMethod.LIST_LABELS: [
                _list_env(_collection_tuple("A", "c1")),  # before
                _list_env(_collection_tuple("A", "c1"), _collection_tuple("New", "c2")),  # after
            ]
        },
        responses={RPCMethod.CREATE_LABEL: None},
    )
    new = await api.create("New")
    assert new.id == "c2"
    assert new.name == "New"
    assert rpc.methods() == [RPCMethod.LIST_LABELS, RPCMethod.CREATE_LABEL, RPCMethod.LIST_LABELS]
    create_call = next(c for c in rpc.calls if c.method == RPCMethod.CREATE_LABEL)
    assert create_call.source_path == "/"
    assert create_call.params[5] == [["New"]]  # name at slot 5, no emoji
    assert create_call.params[-1] == 3


async def test_create_zero_new_raises_collection_error() -> None:
    api, _, _ = _api(
        sequences={
            RPCMethod.LIST_LABELS: [
                _list_env(_collection_tuple("A", "c1")),
                _list_env(_collection_tuple("A", "c1")),  # no new id
            ]
        },
        responses={RPCMethod.CREATE_LABEL: None},
    )
    with pytest.raises(CollectionError):
        await api.create("X")


async def test_create_multiple_new_raises_collection_error() -> None:
    api, _, _ = _api(
        sequences={
            RPCMethod.LIST_LABELS: [
                _list_env(),
                _list_env(_collection_tuple("A", "c1"), _collection_tuple("B", "c2")),
            ]
        },
        responses={RPCMethod.CREATE_LABEL: None},
    )
    with pytest.raises(CollectionError):
        await api.create("X")


# -- rename ------------------------------------------------------------------


async def test_rename_preserves_emoji_variant_none_and_account_path() -> None:
    api, rpc, _ = _api(
        {RPCMethod.LIST_LABELS: _list_env(_collection_tuple("Old", "c1", emoji="\U0001f4c1"))}
    )
    await api.rename("c1", "New")
    upd = next(c for c in rpc.calls if c.method == RPCMethod.UPDATE_LABEL)
    assert upd.operation_variant is None
    assert upd.params[1] is None  # null notebook parent
    assert upd.params[2] == "c1"
    assert upd.params[3] == [[["New", "\U0001f4c1"]]]  # name + preserved emoji
    assert upd.params[-1] == 3
    assert upd.source_path == "/"


async def test_rename_missing_raises_even_when_not_returning_object() -> None:
    api, _, _ = _api({RPCMethod.LIST_LABELS: _list_env(), RPCMethod.UPDATE_LABEL: []})
    with pytest.raises(CollectionNotFoundError):
        await api.rename("missing", "X", return_object=False)


# -- add / remove notebooks --------------------------------------------------


async def test_add_notebooks_single_is_one_update_plus_refetch() -> None:
    api, rpc, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(_collection_tuple("A", "c1", nbs=["n1"])),
            RPCMethod.UPDATE_LABEL: [],
        }
    )
    await api.add_notebooks("c1", ["n2"])
    assert rpc.methods() == [RPCMethod.UPDATE_LABEL, RPCMethod.LIST_LABELS]
    upd = rpc.calls[0]
    assert upd.operation_variant == "add_notebooks"
    assert upd.params[3] == [[None, None, None, [["n2"]]], []]  # captured add shape
    assert upd.source_path == "/"


async def test_add_notebooks_multi_issues_one_update_per_id() -> None:
    api, rpc, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(_collection_tuple("A", "c1")),
            RPCMethod.UPDATE_LABEL: [],
        }
    )
    await api.add_notebooks("c1", ["a", "b", "c"])
    updates = [c for c in rpc.calls if c.method == RPCMethod.UPDATE_LABEL]
    assert [u.operation_variant for u in updates] == ["add_notebooks"] * 3
    assert [u.params[3] for u in updates] == [
        [[None, None, None, [["a"]]], []],
        [[None, None, None, [["b"]]], []],
        [[None, None, None, [["c"]]], []],
    ]
    assert rpc.methods()[-1] == RPCMethod.LIST_LABELS  # one re-fetch after the loop


async def test_remove_notebooks_single_uses_wire_captured_group() -> None:
    api, rpc, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(_collection_tuple("A", "c1", nbs=["n1", "n2"])),
            RPCMethod.UPDATE_LABEL: [],
        }
    )
    await api.remove_notebooks("c1", ["n2"])
    assert rpc.methods() == [RPCMethod.UPDATE_LABEL, RPCMethod.LIST_LABELS]
    upd = rpc.calls[0]
    assert upd.operation_variant == "remove_notebooks"
    # Live-captured (PR #2009): the id stays in group0, shifted to slot [4]; it
    # does NOT move to a second group as originally (incorrectly) inferred.
    assert upd.params[3] == [[None, None, None, None, [["n2"]]], []]


async def test_add_and_remove_dedupe_preserving_order() -> None:
    api, rpc, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(_collection_tuple("A", "c1")),
            RPCMethod.UPDATE_LABEL: [],
        }
    )
    await api.add_notebooks("c1", ["n1", "n2", "n1"])
    updates = [c for c in rpc.calls if c.method == RPCMethod.UPDATE_LABEL]
    assert [u.params[3][0] for u in updates] == [
        [None, None, None, [["n1"]]],
        [None, None, None, [["n2"]]],
    ]


async def test_membership_mutations_raise_on_missing_collection() -> None:
    api, _, _ = _api({RPCMethod.LIST_LABELS: _list_env(), RPCMethod.UPDATE_LABEL: []})
    with pytest.raises(CollectionNotFoundError):
        await api.add_notebooks("missing", ["n1"], return_object=False)
    with pytest.raises(CollectionNotFoundError):
        await api.remove_notebooks("missing", ["n1"], return_object=False)


async def test_empty_membership_mutations_raise_before_any_rpc() -> None:
    api, rpc, _ = _api()
    with pytest.raises(ValueError):
        await api.add_notebooks("c1", [])
    with pytest.raises(ValueError):
        await api.remove_notebooks("c1", [])
    assert rpc.calls == []


async def test_add_notebooks_is_not_atomic_partial_failure_propagates() -> None:
    class _RaiseOnSecondUpdate(FakeRpc):
        async def rpc_call(
            self,
            method,
            params,
            source_path="/",
            allow_null=False,
            _is_retry=False,
            *,
            disable_internal_retries=False,
            operation_variant=None,
            raise_on_null_status=False,
        ):  # noqa: E501
            await super().rpc_call(
                method,
                params,
                source_path,
                allow_null,
                _is_retry,
                disable_internal_retries=disable_internal_retries,
                operation_variant=operation_variant,
                raise_on_null_status=raise_on_null_status,
            )
            if sum(c.method == RPCMethod.UPDATE_LABEL for c in self.calls) == 2:
                raise RuntimeError("wire blip on the 2nd add")
            return None

    rpc = _RaiseOnSecondUpdate()
    api = WebCollectionsAPI(rpc, list_notebooks=AsyncMock(return_value=[]))
    with pytest.raises(RuntimeError):
        await api.add_notebooks("c1", ["a", "b", "c"])
    updates = [c for c in rpc.calls if c.method == RPCMethod.UPDATE_LABEL]
    assert len(updates) == 2  # first applied, failed on second, never reached third
    assert all(c.method != RPCMethod.LIST_LABELS for c in rpc.calls)  # no final re-fetch


# -- delete ------------------------------------------------------------------


async def test_delete_absent_is_idempotent_noop_returns_none() -> None:
    api, rpc, _ = _api({RPCMethod.DELETE_LABEL: []})
    assert await api.delete("unknown") is None
    assert await api.delete(["c1", "c2"]) is None
    deletes = [c for c in rpc.calls if c.method == RPCMethod.DELETE_LABEL]
    assert deletes[0].params[2] == ["unknown"]  # str accepted, wrapped in a list
    assert deletes[0].params[-1] == 3
    assert deletes[0].source_path == "/"
    assert deletes[1].params[2] == ["c1", "c2"]


async def test_delete_empty_list_issues_no_rpc() -> None:
    api, rpc, _ = _api()
    assert await api.delete([]) is None
    assert rpc.calls == []


# -- notebooks join ----------------------------------------------------------


async def test_notebooks_join_membership_order_skips_missing() -> None:
    api, _, list_notebooks = _api(
        {RPCMethod.LIST_LABELS: _list_env(_collection_tuple("A", "c1", nbs=["n2", "n3", "nX"]))},
        notebooks=[SimpleNamespace(id="n1"), SimpleNamespace(id="n2"), SimpleNamespace(id="n3")],
    )
    out = await api.notebooks("c1")
    assert [nb.id for nb in out] == ["n2", "n3"]  # membership order; nX (deleted) skipped
    list_notebooks.assert_awaited_once_with()


async def test_notebooks_missing_collection_raises() -> None:
    api, _, _ = _api({RPCMethod.LIST_LABELS: _list_env()})
    with pytest.raises(CollectionNotFoundError):
        await api.notebooks("missing")
