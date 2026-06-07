"""Exact-payload tests for the source-label RPC param builders."""

from __future__ import annotations

from notebooklm._label.params import (
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


def test_generate_default_scope_is_unlabeled() -> None:
    assert build_generate_labels_params(NB) == [OPTS, NB, None, None, [0]]


def test_generate_scope_all_is_destructive_empty_slot() -> None:
    assert build_generate_labels_params(NB, scope="all") == [OPTS, NB, None, None, []]


def test_generate_scope_unlabeled_explicit() -> None:
    assert build_generate_labels_params(NB, scope="unlabeled") == [OPTS, NB, None, None, [0]]


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
