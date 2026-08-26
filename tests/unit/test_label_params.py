"""Exact-payload tests for the source-label half of the shared label wire codec."""

from __future__ import annotations

from notebooklm._web.codec.labels import (
    _opts,
    build_create_label_params,
    build_delete_labels_params,
    build_generate_labels_params,
    build_list_labels_params,
    build_update_label_params,
)

OPTS = [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]
NB = "nb_1"
LID = "label_1"


def test_opts_is_fresh_each_call() -> None:
    a = _opts()
    b = _opts()
    assert a == b == OPTS
    assert a is not b
    assert a[3] is not b[3]  # nested wrapper not aliased either


def test_generate_defaults_to_the_incremental_scope() -> None:
    assert build_generate_labels_params(NB) == [OPTS, NB, None, None, [0]]


def test_generate_replace_existing_is_destructive_empty_slot() -> None:
    assert build_generate_labels_params(NB, replace_existing=True) == [OPTS, NB, None, None, []]


def test_generate_incremental_scope_explicit() -> None:
    assert build_generate_labels_params(NB, replace_existing=False) == [OPTS, NB, None, None, [0]]


def test_create_label_with_emoji() -> None:
    assert build_create_label_params(NB, "Topic", "\U0001f4c1") == [
        OPTS,
        NB,
        None,
        None,
        None,
        [["Topic", "\U0001f4c1"]],
    ]


def test_create_label_default_empty_emoji() -> None:
    assert build_create_label_params(NB, "Topic") == [OPTS, NB, None, None, None, [["Topic", ""]]]


def test_list_labels() -> None:
    assert build_list_labels_params(NB) == [OPTS, NB]


def test_update_rename_sends_length_one_name() -> None:
    assert build_update_label_params(NB, LID, name="New") == [OPTS, NB, LID, [[["New"]]]]


def test_update_name_and_emoji() -> None:
    assert build_update_label_params(NB, LID, name="New", emoji="\U0001f4c1") == [
        OPTS,
        NB,
        LID,
        [[["New", "\U0001f4c1"]]],
    ]


def test_update_emoji_only_sends_null_name_slot() -> None:
    assert build_update_label_params(NB, LID, emoji="\U0001f4c1") == [
        OPTS,
        NB,
        LID,
        [[[None, "\U0001f4c1"]]],
    ]


def test_update_add_single_source_wraps_the_id() -> None:
    # The builder is SINGULAR: one id, double-nested in the sources_add slot[1].
    assert build_update_label_params(NB, LID, add_source_id="s1") == [
        OPTS,
        NB,
        LID,
        [[None, [["s1"]]]],
    ]


def test_update_remove_single_source_uses_third_slot() -> None:
    # sources_remove rides slot[3][0][2]; with no add, slot[1] is None so the
    # remove group keeps its positional third slot.
    assert build_update_label_params(NB, LID, remove_source_id="s1") == [
        OPTS,
        NB,
        LID,
        [[None, None, [["s1"]]]],
    ]


def test_update_name_and_add_source() -> None:
    assert build_update_label_params(NB, LID, name="New", add_source_id="s1") == [
        OPTS,
        NB,
        LID,
        [[["New"], [["s1"]]]],
    ]


def test_update_name_and_remove_source() -> None:
    assert build_update_label_params(NB, LID, name="New", remove_source_id="s1") == [
        OPTS,
        NB,
        LID,
        [[["New"], None, [["s1"]]]],
    ]


def test_delete_labels_batch() -> None:
    assert build_delete_labels_params(NB, ["l1", "l2"]) == [OPTS, NB, ["l1", "l2"]]


def test_delete_copies_the_id_list() -> None:
    ids = ["l1"]
    out = build_delete_labels_params(NB, ids)
    assert out[2] == ids
    assert out[2] is not ids


# -- P9.3 row-facing payloads -------------------------------------------------
#
# The codec rows dispatch these exact payloads; the route and option flags are
# what the P6.4 handlers passed to ``_rpc_call``.


def test_row_payloads_for_source_labels_carry_the_notebook_route() -> None:
    from notebooklm._backend import BackendContractError
    from notebooklm._binding import CodecPayload
    from notebooklm._semantic.records import (
        LabelDeleteInput,
        LabelGenerateInput,
        LabelGetInput,
        LabelKind,
        LabelListInput,
    )
    from notebooklm._web.codec.labels import (
        encode_label_delete,
        encode_label_generate,
        encode_label_get,
        encode_label_list,
    )

    assert encode_label_list(LabelListInput(LabelKind.SOURCE_LABEL, NB)) == CodecPayload(
        params=[OPTS, NB], source_path=f"/notebook/{NB}", allow_null=False
    )
    assert encode_label_get(LabelGetInput(LabelKind.SOURCE_LABEL, LID, NB)) == CodecPayload(
        params=[OPTS, NB], source_path=f"/notebook/{NB}", allow_null=False
    )
    assert encode_label_delete(
        LabelDeleteInput(LabelKind.SOURCE_LABEL, (LID, "label_2"), NB)
    ) == CodecPayload(
        params=[OPTS, NB, [LID, "label_2"]], source_path=f"/notebook/{NB}", allow_null=True
    )
    assert encode_label_generate(LabelGenerateInput(NB)) == CodecPayload(
        params=[OPTS, NB, None, None, [0]], source_path=f"/notebook/{NB}", allow_null=True
    )
    assert encode_label_generate(LabelGenerateInput(NB, replace_existing=True)).params == [
        OPTS,
        NB,
        None,
        None,
        [],
    ]
    try:
        encode_label_list(LabelListInput(LabelKind.SOURCE_LABEL))
    except BackendContractError as exc:
        assert "requires a notebook scope" in exc.message
    else:  # pragma: no cover - guard
        raise AssertionError("unscoped source-label list must be a contract error")


def test_row_payloads_for_collections_carry_the_account_route() -> None:
    from notebooklm._binding import CodecPayload
    from notebooklm._semantic.records import (
        LabelDeleteInput,
        LabelGetInput,
        LabelKind,
        LabelListInput,
    )
    from notebooklm._web.codec.labels import (
        _collection_opts,
        encode_collection_delete,
        encode_collection_get,
        encode_collection_list,
    )

    collection_opts = _collection_opts()
    assert encode_collection_list(LabelListInput(LabelKind.COLLECTION)) == CodecPayload(
        params=[collection_opts, None, 3], source_path="/", allow_null=True
    )
    assert encode_collection_get(LabelGetInput(LabelKind.COLLECTION, "c1")) == CodecPayload(
        params=[collection_opts, None, 3], source_path="/", allow_null=True
    )
    assert encode_collection_delete(
        LabelDeleteInput(LabelKind.COLLECTION, ("c1", "c2"))
    ) == CodecPayload(
        params=[collection_opts, None, ["c1", "c2"], 3], source_path="/", allow_null=True
    )
