"""Exact-payload tests for the account-level collection RPC param builders.

These literals are the wire-shape anchor for collections. Collections reuse the
label RPC ids, so they cannot own ``rpc_golden`` fixtures (those are keyed by
``RPCMethod`` name, one per method); this module is the regression contract
instead. The ``create``/remove-membership shapes were corrected against a live
capture (PR #2009, independently confirmed on four accounts) — the original
issue #2006 capture for those two shapes reproducibly failed on the wire.
"""

from __future__ import annotations

from notebooklm._web.params.collections import (
    _opts,
    _opts_create,
    build_create_collection_params,
    build_delete_collections_params,
    build_list_collections_params,
    build_rename_collection_params,
    build_update_collection_notebooks_params,
)

# The collection options wrapper — label wrapper but trailing context [1, 3].
OPTS = [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1, 3]]]
# CREATE-only variant: slot [2] is [1] instead of None (live-captured, PR #2009).
OPTS_CREATE = [2, None, [1], [1, None, None, None, None, None, None, None, None, None, [1, 3]]]
CID = "coll_1"
NBID = "nb_1"


def test_opts_is_fresh_each_call() -> None:
    a = _opts()
    b = _opts()
    assert a == b == OPTS
    assert a is not b
    assert a[3] is not b[3]  # nested wrapper not aliased either


def test_opts_tail_is_collection_marker() -> None:
    # The [1, 3] tail is what distinguishes a collection call from a label call.
    assert _opts()[3][-1] == [1, 3]


def test_list_collections() -> None:
    assert build_list_collections_params() == [OPTS, None, 3]


def test_create_opts_is_fresh_each_call() -> None:
    a = _opts_create()
    b = _opts_create()
    assert a == b == OPTS_CREATE
    assert a is not b
    assert a[3] is not b[3]


def test_create_collection_name_at_slot_five_no_emoji() -> None:
    # Live-captured (PR #2009): name at slot [5] with OPTS_CREATE, not slot [7]
    # with the plain OPTS — the original issue #2006 capture reproducibly left
    # nothing server-side.
    assert build_create_collection_params("Research Q3") == [
        OPTS_CREATE,
        None,
        None,
        None,
        None,
        [["Research Q3"]],
        3,
    ]


def test_rename_collection_name_only_length_one() -> None:
    # Matches the captured UI rename: [[[new_name]]] with the type-3 tail.
    assert build_rename_collection_params(CID, "New Name") == [
        OPTS,
        None,
        CID,
        [[["New Name"]]],
        3,
    ]


def test_rename_collection_with_emoji_preserved() -> None:
    assert build_rename_collection_params(CID, "New Name", "\U0001f4c1") == [
        OPTS,
        None,
        CID,
        [[["New Name", "\U0001f4c1"]]],
        3,
    ]


def test_add_notebook_wire_captured_shape() -> None:
    # Owner capture: [ [null,null,null,[[nbid]]], [] ] with the type-3 tail.
    assert build_update_collection_notebooks_params(CID, add_notebook_id=NBID) == [
        OPTS,
        None,
        CID,
        [[None, None, None, [[NBID]]], []],
        3,
    ]


def test_remove_notebook_wire_captured_shape() -> None:
    # Live-captured (PR #2009): the un-assign id stays in group0 and shifts
    # slot [3] -> [4]; it does NOT move to a second group as originally
    # (incorrectly) inferred from issue #2006 — that inference was a silent
    # wire no-op.
    assert build_update_collection_notebooks_params(CID, remove_notebook_id=NBID) == [
        OPTS,
        None,
        CID,
        [[None, None, None, None, [[NBID]]], []],
        3,
    ]


def test_delete_collections_batch() -> None:
    assert build_delete_collections_params(["c1", "c2"]) == [OPTS, None, ["c1", "c2"], 3]


def test_delete_copies_the_id_list() -> None:
    ids = ["c1"]
    out = build_delete_collections_params(ids)
    assert out[2] == ids
    assert out[2] is not ids
