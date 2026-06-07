"""Unit tests for LabelsAPI over a mock RpcCaller (no HTTP/VCR — unit tier)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from notebooklm._labels import LabelsAPI
from notebooklm.exceptions import LabelError, LabelNotFoundError, UnknownRPCMethodError
from notebooklm.rpc import RPCMethod


def _label_tuple(
    name: str, label_id: str, *, emoji: str = "", src: list[str] | None = None
) -> list[Any]:
    sources = [[s] for s in src] if src else None
    return [name, sources, label_id, emoji]


def _list_env(*tuples: list[Any]) -> list[Any]:
    return [list(tuples)]  # LIST_LABELS echoes [[label, ...]]


def _create_env(*tuples: list[Any]) -> list[Any]:
    return [None, list(tuples)]  # CREATE_LABEL echoes [None, [label, ...]]


class FakeRpc:
    def __init__(self, responses: dict[RPCMethod, Any] | None = None) -> None:
        self.calls: list[SimpleNamespace] = []
        self.responses = responses or {}

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
    ) -> Any:
        self.calls.append(
            SimpleNamespace(
                method=method,
                params=params,
                source_path=source_path,
                allow_null=allow_null,
                operation_variant=operation_variant,
            )
        )
        return self.responses.get(method)

    def methods(self) -> list[RPCMethod]:
        return [c.method for c in self.calls]


def _api(responses: dict[RPCMethod, Any] | None = None, sources: list[Any] | None = None):
    rpc = FakeRpc(responses)
    list_sources = AsyncMock(return_value=sources or [])
    return LabelsAPI(rpc, list_sources=list_sources), rpc, list_sources


# -- read --------------------------------------------------------------------


async def test_list_decodes_list_envelope() -> None:
    api, rpc, _ = _api({RPCMethod.LIST_LABELS: _list_env(_label_tuple("A", "l1", src=["s1"]))})
    labels = await api.list("nb")
    assert [label.id for label in labels] == ["l1"]
    assert labels[0].source_ids == ["s1"]
    assert labels[0].notebook_id == "nb"
    assert rpc.methods() == [RPCMethod.LIST_LABELS]
    assert rpc.calls[0].source_path == "/notebook/nb"


async def test_generate_decodes_create_envelope_and_default_scope() -> None:
    api, rpc, _ = _api(
        {RPCMethod.CREATE_LABEL: _create_env(_label_tuple("A", "l1"), _label_tuple("B", "l2"))}
    )
    labels = await api.generate("nb")
    assert {label.id for label in labels} == {"l1", "l2"}
    assert rpc.methods() == [RPCMethod.CREATE_LABEL]
    assert rpc.calls[0].params[4] == [0]  # default scope="unlabeled"


async def test_generate_scope_all_is_destructive_slot() -> None:
    api, rpc, _ = _api({RPCMethod.CREATE_LABEL: _create_env()})
    await api.generate("nb", scope="all")
    assert rpc.calls[0].params[4] == []


async def test_generate_rejects_invalid_scope_before_any_rpc() -> None:
    # A runtime-invalid scope must raise ValueError BEFORE the wire — the param
    # builder treats anything != "all" as "unlabeled", so without this guard an
    # invalid value would silently build the (unintended) "unlabeled" payload.
    api, rpc, _ = _api({RPCMethod.CREATE_LABEL: _create_env()})
    with pytest.raises(ValueError):
        await api.generate("nb", scope="bogus")  # type: ignore[arg-type]
    assert rpc.calls == []


async def test_list_truthy_non_list_envelope_raises_drift() -> None:
    # A present-but-non-list envelope is schema drift, not an empty label set —
    # it must raise rather than masking the drift as ``[]``.
    api, _, _ = _api({RPCMethod.LIST_LABELS: "unexpected-string"})
    with pytest.raises(UnknownRPCMethodError):
        await api.list("nb")


async def test_list_and_create_envelopes_differ() -> None:
    # Same label payload under each envelope must both decode to the same label.
    tuple_ = _label_tuple("A", "l1")
    api_l, _, _ = _api({RPCMethod.LIST_LABELS: _list_env(tuple_)})
    api_c, _, _ = _api({RPCMethod.CREATE_LABEL: _create_env(tuple_)})
    assert (await api_l.list("nb"))[0].id == (await api_c.generate("nb"))[0].id == "l1"


async def test_get_or_none_and_get() -> None:
    api, _, _ = _api({RPCMethod.LIST_LABELS: _list_env(_label_tuple("A", "l1"))})
    assert (await api.get_or_none("nb", "l1")).id == "l1"
    assert await api.get_or_none("nb", "missing") is None
    assert (await api.get("nb", "l1")).id == "l1"


async def test_get_raises_label_not_found_with_method_id() -> None:
    api, _, _ = _api({RPCMethod.LIST_LABELS: _list_env()})
    with pytest.raises(LabelNotFoundError) as exc:
        await api.get("nb", "missing")
    assert exc.value.label_id == "missing"
    assert exc.value.method_id == RPCMethod.LIST_LABELS.value


# -- create id-diff ----------------------------------------------------------


async def test_create_returns_the_new_id() -> None:
    api, rpc, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(_label_tuple("A", "l1")),
            RPCMethod.CREATE_LABEL: _create_env(_label_tuple("A", "l1"), _label_tuple("New", "l2")),
        }
    )
    new = await api.create("nb", "New")
    assert new.id == "l2"
    assert rpc.methods() == [RPCMethod.LIST_LABELS, RPCMethod.CREATE_LABEL]


async def test_create_zero_new_raises_label_error() -> None:
    api, _, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(_label_tuple("A", "l1")),
            RPCMethod.CREATE_LABEL: _create_env(_label_tuple("A", "l1")),  # no new id
        }
    )
    with pytest.raises(LabelError):
        await api.create("nb", "X")


async def test_create_multiple_new_raises_label_error() -> None:
    api, _, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(),  # empty baseline
            RPCMethod.CREATE_LABEL: _create_env(_label_tuple("A", "l1"), _label_tuple("B", "l2")),
        }
    )
    with pytest.raises(LabelError):
        await api.create("nb", "X")


# -- mutate ------------------------------------------------------------------


async def test_rename_preserves_emoji_and_variant_none() -> None:
    api, rpc, _ = _api(
        {RPCMethod.LIST_LABELS: _list_env(_label_tuple("Old", "l1", emoji="\U0001f4c4"))}
    )
    await api.rename("nb", "l1", "New")
    upd = next(c for c in rpc.calls if c.method == RPCMethod.UPDATE_LABEL)
    assert upd.operation_variant is None
    assert upd.params[3] == [[["New", "\U0001f4c4"]]]  # name + preserved emoji


async def test_set_emoji_sends_null_name_slot_variant_none() -> None:
    api, rpc, _ = _api({RPCMethod.LIST_LABELS: _list_env(_label_tuple("A", "l1"))})
    await api.set_emoji("nb", "l1", "\U0001f525")
    upd = next(c for c in rpc.calls if c.method == RPCMethod.UPDATE_LABEL)
    assert upd.operation_variant is None
    assert upd.params[3] == [[[None, "\U0001f525"]]]


async def test_add_sources_single_id_is_one_update_plus_refetch() -> None:
    api, rpc, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(_label_tuple("A", "l1", src=["s1"])),
            RPCMethod.UPDATE_LABEL: [],
        }
    )
    await api.add_sources("nb", "l1", ["s2"])
    assert rpc.methods() == [RPCMethod.UPDATE_LABEL, RPCMethod.LIST_LABELS]
    upd = rpc.calls[0]
    assert upd.operation_variant == "add_sources"
    assert upd.params[3] == [[None, [["s2"]]]]


async def test_add_sources_multi_id_issues_one_update_per_id() -> None:
    # The wire honours only the first id per call, so a 3-id add MUST be 3
    # le8sX calls (one per source), then ONE preflight re-fetch.
    api, rpc, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(_label_tuple("A", "l1", src=["a", "b", "c"])),
            RPCMethod.UPDATE_LABEL: [],
        }
    )
    await api.add_sources("nb", "l1", ["a", "b", "c"])
    assert rpc.methods() == [
        RPCMethod.UPDATE_LABEL,
        RPCMethod.UPDATE_LABEL,
        RPCMethod.UPDATE_LABEL,
        RPCMethod.LIST_LABELS,
    ]
    updates = [c for c in rpc.calls if c.method == RPCMethod.UPDATE_LABEL]
    assert [u.operation_variant for u in updates] == ["add_sources"] * 3
    assert [u.params[3] for u in updates] == [
        [[None, [["a"]]]],
        [[None, [["b"]]]],
        [[None, [["c"]]]],
    ]


async def test_remove_sources_single_id_is_one_update_plus_refetch() -> None:
    api, rpc, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(_label_tuple("A", "l1", src=["s1", "s2"])),
            RPCMethod.UPDATE_LABEL: [],
        }
    )
    await api.remove_sources("nb", "l1", ["s2"])
    assert rpc.methods() == [RPCMethod.UPDATE_LABEL, RPCMethod.LIST_LABELS]
    upd = rpc.calls[0]
    assert upd.operation_variant == "remove_sources"
    assert upd.params[3] == [[None, None, [["s2"]]]]


async def test_remove_sources_multi_id_issues_one_update_per_id() -> None:
    api, rpc, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(_label_tuple("A", "l1", src=["a"])),
            RPCMethod.UPDATE_LABEL: [],
        }
    )
    await api.remove_sources("nb", "l1", ["a", "b", "c"])
    assert rpc.methods() == [
        RPCMethod.UPDATE_LABEL,
        RPCMethod.UPDATE_LABEL,
        RPCMethod.UPDATE_LABEL,
        RPCMethod.LIST_LABELS,
    ]
    updates = [c for c in rpc.calls if c.method == RPCMethod.UPDATE_LABEL]
    assert [u.operation_variant for u in updates] == ["remove_sources"] * 3
    assert [u.params[3] for u in updates] == [
        [[None, None, [["a"]]]],
        [[None, None, [["b"]]]],
        [[None, None, [["c"]]]],
    ]


async def test_remove_sources_of_non_member_does_not_raise() -> None:
    # Removing an absent member is a no-op on the wire; the API still succeeds
    # (the preflight finds the label, just unchanged).
    api, _, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(_label_tuple("A", "l1", src=["s1"])),
            RPCMethod.UPDATE_LABEL: [],
        }
    )
    out = await api.remove_sources("nb", "l1", ["not-a-member"])
    assert out is not None and out.id == "l1"


async def test_mutations_raise_on_missing_even_when_not_returning_object() -> None:
    api, _, _ = _api({RPCMethod.LIST_LABELS: _list_env(), RPCMethod.UPDATE_LABEL: []})
    with pytest.raises(LabelNotFoundError):
        await api.rename("nb", "missing", "X", return_object=False)
    with pytest.raises(LabelNotFoundError):
        await api.update("nb", "missing", emoji="\U0001f525", return_object=False)
    with pytest.raises(LabelNotFoundError):
        await api.add_sources("nb", "missing", ["s1"], return_object=False)
    with pytest.raises(LabelNotFoundError):
        await api.remove_sources("nb", "missing", ["s1"], return_object=False)


async def test_noop_mutations_raise_value_error_before_any_rpc() -> None:
    api, rpc, _ = _api()
    with pytest.raises(ValueError):
        await api.update("nb", "l1")  # both name and emoji None
    with pytest.raises(ValueError):
        await api.add_sources("nb", "l1", [])
    with pytest.raises(ValueError):
        await api.remove_sources("nb", "l1", [])
    assert rpc.calls == []  # nothing reached the wire


# -- delete ------------------------------------------------------------------


async def test_delete_absent_is_idempotent_noop_returns_none() -> None:
    api, rpc, _ = _api({RPCMethod.DELETE_LABEL: []})
    assert await api.delete("nb", "unknown") is None
    assert await api.delete("nb", ["l1", "l2"]) is None
    deletes = [c for c in rpc.calls if c.method == RPCMethod.DELETE_LABEL]
    assert deletes[0].params[2] == ["unknown"]  # str accepted, wrapped in a list
    assert deletes[0].allow_null is True
    assert deletes[1].params[2] == ["l1", "l2"]


async def test_delete_empty_list_issues_no_rpc() -> None:
    api, rpc, _ = _api()
    assert await api.delete("nb", []) is None
    assert rpc.calls == []


# -- sources join ------------------------------------------------------------


async def test_sources_join_membership_order_skips_missing() -> None:
    api, _, list_sources = _api(
        {RPCMethod.LIST_LABELS: _list_env(_label_tuple("A", "l1", src=["s2", "s3", "sX"]))},
        sources=[SimpleNamespace(id="s1"), SimpleNamespace(id="s2"), SimpleNamespace(id="s3")],
    )
    out = await api.sources("nb", "l1")
    assert [s.id for s in out] == ["s2", "s3"]  # membership order; sX (deleted) skipped
    list_sources.assert_awaited_once_with("nb")


async def test_sources_missing_label_raises() -> None:
    api, _, _ = _api({RPCMethod.LIST_LABELS: _list_env()})
    with pytest.raises(LabelNotFoundError):
        await api.sources("nb", "missing")


# -- non-atomicity contract (per-id loop) ------------------------------------


async def test_add_sources_is_not_atomic_partial_failure_propagates() -> None:
    """Multi-id add is non-atomic: a mid-loop RPC failure leaves the earlier ids
    written and propagates the error without the final re-fetch."""

    class _RaiseOnSecondUpdate(FakeRpc):
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
        ) -> Any:
            await super().rpc_call(
                method,
                params,
                source_path,
                allow_null,
                _is_retry,
                disable_internal_retries=disable_internal_retries,
                operation_variant=operation_variant,
            )
            if sum(c.method == RPCMethod.UPDATE_LABEL for c in self.calls) == 2:
                raise RuntimeError("wire blip on the 2nd add")
            return None

    rpc = _RaiseOnSecondUpdate()
    api = LabelsAPI(rpc, list_sources=AsyncMock(return_value=[]))
    with pytest.raises(RuntimeError):
        await api.add_sources("nb", "l1", ["s1", "s2", "s3"])

    updates = [c for c in rpc.calls if c.method == RPCMethod.UPDATE_LABEL]
    assert len(updates) == 2  # first applied, failed on the second, never reached the third
    assert all(c.method != RPCMethod.LIST_LABELS for c in rpc.calls)  # no final re-fetch


async def test_add_sources_dedupes_ids_preserving_order() -> None:
    """Duplicate ids collapse to one le8sX each (order preserved) — no redundant calls."""
    api, rpc, _ = _api(
        {
            RPCMethod.LIST_LABELS: _list_env(_label_tuple("A", "l1")),
            RPCMethod.UPDATE_LABEL: [],
        }
    )
    await api.add_sources("nb", "l1", ["s1", "s2", "s1"])
    updates = [c for c in rpc.calls if c.method == RPCMethod.UPDATE_LABEL]
    assert [u.params[3] for u in updates] == [[[None, [["s1"]]]], [[None, [["s2"]]]]]
