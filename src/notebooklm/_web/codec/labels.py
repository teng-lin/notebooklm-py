"""The one web codec authority for the shared label/collection wire surface.

Source labels and collections are the same product concept twice: a collection
is a label carrying a type discriminator (``3``) and no notebook parent, so both
reuse the four label RPC ids verbatim (``agX4Bc`` / ``I3xc3c`` / ``le8sX`` /
``GyzE7e``).  Splitting them across two modules split one wire surface, so every
request array and every response envelope for both dialects terminates here and
returns neutral :class:`~notebooklm._records.LabelRecord` values.

Three wire differences separate the dialects (owner-captured on the live
Gemini-Notebook UI, issues #2006/#2009):

1. the notebook slot is ``None`` for collections — they are account-level;
2. a type discriminator ``3`` rides the **last** slot of every collection
   request; and
3. the collection request-options wrapper ends in ``[1, 3]``, not ``[1]``.

The proven positional row grammar stays in ``_row_adapters.labels`` until the
plan's atomic row-adapter relocation.  This module must not grow backend, RPC
dispatch, HTTP, or feature-facade dependencies.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..._backend import BackendContractError
from ..._binding import CodecPayload
from ..._operations import Operation
from ..._records import (
    LabelAllocateInput,
    LabelAllocateResult,
    LabelDeleteInput,
    LabelDeleteResult,
    LabelGenerateInput,
    LabelGenerateResult,
    LabelGetInput,
    LabelGetResult,
    LabelKind,
    LabelListInput,
    LabelListResult,
    LabelMutateInput,
    LabelMutateResult,
    LabelRecord,
)
from ..._row_adapters.labels import CollectionRow, LabelRow
from ...exceptions import UnknownRPCMethodError
from ...rpc import RPCMethod

_SRC = "_web.codec.labels"

# Type discriminator marking a label RPC as operating on collections (type-3
# labels with a null notebook parent). Rides the last slot of every request.
_COLLECTION_TYPE = 3

# Collections are account-level: every collection RPC uses the home-page source
# path, not a ``/notebook/<id>`` path (they have no notebook scope).
_ACCOUNT_PATH = "/"


# -- request options ---------------------------------------------------------


def _opts() -> list[Any]:
    """Fresh request-options wrapper (arg ``[0]``) for the source-label dialect.

    Mirrors the ``[1, None*8, [1]]`` context block in ``_settings.py``; returned
    fresh per call so callers never alias a shared mutable list.
    """
    return [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]


def _collection_opts() -> list[Any]:
    """Fresh request-options wrapper for the collection dialect.

    Identical to :func:`_opts` except the trailing context list is ``[1, 3]``
    (not ``[1]``) — the collection-scope marker.
    """
    return [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1, 3]]]


def _collection_create_opts() -> list[Any]:
    """Fresh request-options wrapper for the collection CREATE only.

    Same as :func:`_collection_opts` except slot ``[2]`` is ``[1]`` instead of
    ``None`` — the original ``opts[2] is None`` shape reproducibly left nothing
    server-side (confirmed live on three independent accounts, PR #2009); this
    is the shape a live UI create actually sends.
    """
    return [2, None, [1], [1, None, None, None, None, None, None, None, None, None, [1, 3]]]


# -- requests: read ----------------------------------------------------------


def build_list_labels_params(notebook_id: str) -> list[Any]:
    """LIST_LABELS (``I3xc3c``) for the source labels of one notebook."""
    return [_opts(), notebook_id]


def build_list_collections_params() -> list[Any]:
    """LIST_LABELS (``I3xc3c``) for collections: ``[opts, None, 3]``.

    Response echoes ``[None, [ [name, [nb_id, ...], collection_id, emoji], ... ]]``
    — populated members are bare id strings, not label-style wrapped singletons
    (live-captured, PR #2009).
    """
    return [_collection_opts(), None, _COLLECTION_TYPE]


# -- requests: create / generate ---------------------------------------------


def build_generate_labels_params(notebook_id: str, *, replace_existing: bool = False) -> list[Any]:
    """CREATE_LABEL (``agX4Bc``) — AI grouping. ``replace_existing`` picks slot ``[4]``:

    ``True`` -> ``[]`` (wipe + regenerate every label, destructive, new ids);
    ``False`` -> ``[0]`` (incremental — only currently-unlabeled sources).
    """
    return [_opts(), notebook_id, None, None, ([] if replace_existing else [0])]


def build_create_label_params(notebook_id: str, name: str, emoji: str = "") -> list[Any]:
    """CREATE_LABEL (``agX4Bc``) — manual create. Scope slot ``[4]`` is ``None``;
    slot ``[5]`` carries the labels to create."""
    return [_opts(), notebook_id, None, None, None, [[name, emoji]]]


def build_create_collection_params(name: str) -> list[Any]:
    """CREATE_LABEL (``agX4Bc``) — manual collection create.

    ``[opts, None, None, None, None, [[name]], 3]``. Unlike a source label the
    create wire has **no emoji slot** (collections get an emoji via a later
    update, if at all).
    """
    return [_collection_create_opts(), None, None, None, None, [[name]], _COLLECTION_TYPE]


# -- requests: update --------------------------------------------------------


def build_update_label_params(
    notebook_id: str,
    label_id: str,
    *,
    name: str | None = None,
    emoji: str | None = None,
    add_source_id: str | None = None,
    remove_source_id: str | None = None,
) -> list[Any]:
    """UPDATE_LABEL (``le8sX``) for source labels. Fieldmask slot ``[3]`` =
    ``[[ name_emoji, sources_add, sources_remove ]]`` (a THREE-slot group):

    * ``name_emoji`` (slot ``[0]``) = ``[name, emoji]`` (positional). A rename
      sends a length-1 ``[name]``; the semantic service passes the current emoji
      explicitly so a rename never clobbers it.
    * ``sources_add`` (slot ``[1]``) = ``[[source_id]]`` — ASSIGNS one source.
    * ``sources_remove`` (slot ``[2]``) = ``[[source_id]]`` — UN-ASSIGNS one
      source (confirmed 2026-06-07). It does NOT delete the source.

    The wire honours only the FIRST id per group per call, so the builder is
    **singular** — the backend loops one call per id. When removing without
    adding, slot ``[1]`` is ``None`` so ``sources_remove`` keeps slot ``[2]``.
    """
    group: list[Any] = []
    if name is not None or emoji is not None:
        group.append([name] if emoji is None else [name, emoji])
    else:
        group.append(None)
    if add_source_id is not None:
        group.append([[add_source_id]])
    if remove_source_id is not None:
        if add_source_id is None:
            group.append(None)  # keep sources_remove at positional slot [2]
        group.append([[remove_source_id]])
    return [_opts(), notebook_id, label_id, [group]]


def build_rename_collection_params(
    collection_id: str, name: str, emoji: str | None = None
) -> list[Any]:
    """UPDATE_LABEL (``le8sX``) rename for collections.

    Fieldmask slot ``[3]`` = ``[[[name]]]`` (name-only, matching the captured UI
    rename) or ``[[[name, emoji]]]`` when ``emoji`` is supplied. CONFIRMED on the
    wire (live-captured, PR #2009): a length-1 ``name_emoji`` PRESERVES an
    existing emoji rather than clearing it (this settles the same open question
    for source labels).
    """
    name_emoji: list[Any] = [name] if emoji is None else [name, emoji]
    return [_collection_opts(), None, collection_id, [[name_emoji]], _COLLECTION_TYPE]


def build_update_collection_notebooks_params(
    collection_id: str,
    *,
    add_notebook_id: str | None = None,
    remove_notebook_id: str | None = None,
) -> list[Any]:
    """UPDATE_LABEL (``le8sX``) notebook membership for collections.

    Fieldmask slot ``[3]`` is a two-element list ``[group0, group1]`` where
    ``group1`` is always empty and both add and remove ride in **group0**
    (wire-captured, PR #2009): add puts the id at group-slot ``[3]``, remove at
    group-slot ``[4]`` — it does *not* move to a second group as originally
    (incorrectly) inferred, which made the original removal a silent wire no-op.

    * **add** (wire-captured): ``[[None, None, None, [[nb_id]]], []]``.
    * **remove** (wire-captured): ``[[None, None, None, None, [[nb_id]]], []]``.

    The wire honours only the FIRST id per call, so the builder is **singular**.
    """
    if add_notebook_id is not None:
        group0: list[Any] = [None, None, None, [[add_notebook_id]]]
    elif remove_notebook_id is not None:
        group0 = [None, None, None, None, [[remove_notebook_id]]]
    else:
        group0 = []
    return [_collection_opts(), None, collection_id, [group0, []], _COLLECTION_TYPE]


# -- requests: delete --------------------------------------------------------


def build_delete_labels_params(notebook_id: str, label_ids: Sequence[str]) -> list[Any]:
    """DELETE_LABEL (``GyzE7e``) for source labels — batch, array of ids."""
    return [_opts(), notebook_id, list(label_ids)]


def build_delete_collections_params(collection_ids: Sequence[str]) -> list[Any]:
    """DELETE_LABEL (``GyzE7e``) for collections — ``[opts, None, [id, ...], 3]``."""
    return [_collection_opts(), None, list(collection_ids), _COLLECTION_TYPE]


# -- responses ---------------------------------------------------------------


def _source_label_record(
    item: Any,
    *,
    notebook_id: str | None,
    method_id: str,
) -> LabelRecord:
    row = LabelRow.from_label_tuple(item, method_id=method_id)
    return LabelRecord(
        id=row.id,
        name=row.name,
        kind=LabelKind.SOURCE_LABEL,
        notebook_id=notebook_id,
        emoji=row.emoji or None,
        member_ids=row.source_ids,
    )


def _collection_record(item: Any, *, method_id: str) -> LabelRecord:
    row = CollectionRow.from_collection_tuple(item, method_id=method_id)
    return LabelRecord(
        id=row.id,
        name=row.name,
        kind=LabelKind.COLLECTION,
        notebook_id=None,
        emoji=row.emoji or None,
        member_ids=row.notebook_ids,
    )


def _label_set(
    result: Any,
    *,
    kind: LabelKind,
    notebook_id: str | None,
    method_id: str,
    index: int,
) -> tuple[LabelRecord, ...]:
    """Decode one label-set envelope at ``index`` into neutral records.

    An empty or absent label set decodes to ``()``; a present-but-malformed
    envelope is schema drift and raises rather than masking as empty.
    """
    if not result:
        return ()
    if not isinstance(result, list):
        raise UnknownRPCMethodError(
            message="label set envelope is not a list",
            method_id=method_id,
            source=_SRC,
        )
    raw = result[index] if len(result) > index else None
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise UnknownRPCMethodError(
            message="label set envelope malformed",
            method_id=method_id,
            source=_SRC,
        )
    if kind is LabelKind.COLLECTION:
        return tuple(_collection_record(item, method_id=method_id) for item in raw)
    return tuple(
        _source_label_record(item, notebook_id=notebook_id, method_id=method_id) for item in raw
    )


def decode_label_list(
    result: Any,
    *,
    kind: LabelKind,
    notebook_id: str | None,
    method_id: str,
) -> tuple[LabelRecord, ...]:
    """Decode a ``LIST_LABELS`` envelope for either dialect.

    The two dialects disagree on the envelope, not the row: source labels echo
    ``[[label, ...]]`` (index ``0``) while collections echo
    ``[None, [collection, ...]]`` (index ``1``).
    """
    index = 1 if kind is LabelKind.COLLECTION else 0
    return _label_set(
        result,
        kind=kind,
        notebook_id=notebook_id,
        method_id=method_id,
        index=index,
    )


def decode_label_create_echo(
    result: Any,
    *,
    notebook_id: str,
    method_id: str,
) -> tuple[LabelRecord, ...]:
    """Decode a source-label ``CREATE_LABEL`` echo (``[None, [label, ...]]``).

    ``agX4Bc`` echoes the full post-operation label set for both the manual and
    the auto-grouping modes. The collection dialect has no captured create echo,
    so its backend handler re-lists instead of decoding one here.
    """
    return _label_set(
        result,
        kind=LabelKind.SOURCE_LABEL,
        notebook_id=notebook_id,
        method_id=method_id,
        index=1,
    )


def decode_label(
    data: list[Any],
    *,
    notebook_id: str | None = None,
    method_id: str | None = None,
) -> LabelRecord:
    """Decode one strict source-label tuple for the retained P3 codec seam."""
    row = LabelRow.from_label_tuple(data, method_id=method_id)
    return LabelRecord(
        id=row.id,
        name=row.name,
        kind=LabelKind.SOURCE_LABEL,
        notebook_id=notebook_id,
        emoji=row.emoji or None,
        member_ids=tuple(row.source_ids),
    )


# -- contract guards ---------------------------------------------------------


def require_label_kind(actual: LabelKind, expected: LabelKind, operation: Operation) -> None:
    """Fail closed when a request addresses the other dialect's operation."""
    if actual is not expected:
        raise BackendContractError(
            f"{operation.value} requires a {expected.value} request, got {actual.value}",
            operation=operation,
        )


def require_notebook_scope(notebook_id: str | None, operation: Operation) -> str:
    """Source labels are notebook-scoped; a null scope is a contract error."""
    if notebook_id is None:
        raise BackendContractError(
            f"{operation.value} requires a notebook scope",
            operation=operation,
        )
    return notebook_id


def _notebook_path(notebook_id: str) -> str:
    return f"/notebook/{notebook_id}"


# -- row-facing payloads (P9.3) -----------------------------------------------
#
# Each returns the full request one codec row dispatches — params, route and
# option flags exactly as the P6.4 handlers passed them — and never a method.
# The guards run here, before any native call, so a wrong-dialect or unscoped
# request is still a ``BackendContractError`` raised ahead of the wire.


def encode_label_set_list(
    kind: LabelKind, notebook_id: str | None, operation: Operation
) -> CodecPayload:
    """Shared ``LIST_LABELS`` payload for either dialect (reads and composite readbacks)."""
    if kind is LabelKind.COLLECTION:
        # An account with zero collections may echo a null envelope, which
        # decodes to an empty set rather than raising.
        return CodecPayload(
            params=build_list_collections_params(),
            source_path=_ACCOUNT_PATH,
            allow_null=True,
        )
    scoped = require_notebook_scope(notebook_id, operation)
    return CodecPayload(
        params=build_list_labels_params(scoped),
        source_path=_notebook_path(scoped),
        allow_null=False,
    )


def encode_label_list(value: LabelListInput) -> CodecPayload:
    """Payload for the ``label.list`` codec row."""
    require_label_kind(value.kind, LabelKind.SOURCE_LABEL, Operation.LABEL_LIST)
    return encode_label_set_list(LabelKind.SOURCE_LABEL, value.notebook_id, Operation.LABEL_LIST)


def encode_collection_list(value: LabelListInput) -> CodecPayload:
    """Payload for the ``collection.list`` codec row."""
    require_label_kind(value.kind, LabelKind.COLLECTION, Operation.COLLECTION_LIST)
    return encode_label_set_list(LabelKind.COLLECTION, None, Operation.COLLECTION_LIST)


def encode_label_get(value: LabelGetInput) -> CodecPayload:
    """Payload for the ``label.get`` codec row (one set read; selection is in decode)."""
    require_label_kind(value.kind, LabelKind.SOURCE_LABEL, Operation.LABEL_GET)
    return encode_label_set_list(LabelKind.SOURCE_LABEL, value.notebook_id, Operation.LABEL_GET)


def encode_collection_get(value: LabelGetInput) -> CodecPayload:
    """Payload for the ``collection.get`` codec row."""
    require_label_kind(value.kind, LabelKind.COLLECTION, Operation.COLLECTION_GET)
    return encode_label_set_list(LabelKind.COLLECTION, None, Operation.COLLECTION_GET)


def encode_label_delete(value: LabelDeleteInput) -> CodecPayload:
    """Payload for the ``label.delete`` codec row (batch; absent ids are no-ops)."""
    require_label_kind(value.kind, LabelKind.SOURCE_LABEL, Operation.LABEL_DELETE)
    notebook_id = require_notebook_scope(value.notebook_id, Operation.LABEL_DELETE)
    return CodecPayload(
        params=build_delete_labels_params(notebook_id, value.label_ids),
        source_path=_notebook_path(notebook_id),
        allow_null=True,
    )


def encode_collection_delete(value: LabelDeleteInput) -> CodecPayload:
    """Payload for the ``collection.delete`` codec row."""
    require_label_kind(value.kind, LabelKind.COLLECTION, Operation.COLLECTION_DELETE)
    return CodecPayload(
        params=build_delete_collections_params(value.label_ids),
        source_path=_ACCOUNT_PATH,
        allow_null=True,
    )


def encode_label_generate(value: LabelGenerateInput) -> CodecPayload:
    """Payload for the ``label.generate`` codec row (CreateLabel auto-grouping mode)."""
    return CodecPayload(
        params=build_generate_labels_params(
            value.notebook_id,
            replace_existing=value.replace_existing,
        ),
        source_path=_notebook_path(value.notebook_id),
        allow_null=True,
    )


# -- row-facing decoders (P9.3) -----------------------------------------------


def decode_label_set_list_result(
    data: Any, *, kind: LabelKind, notebook_id: str | None
) -> LabelListResult:
    """Decode one ``LIST_LABELS`` echo for either dialect into the list result."""
    return LabelListResult(
        labels=decode_label_list(
            data,
            kind=kind,
            notebook_id=notebook_id,
            method_id=RPCMethod.LIST_LABELS.value,
        )
    )


def decode_label_list_result(value: LabelListInput, data: Any) -> LabelListResult:
    """Row decoder for ``label.list``."""
    return decode_label_set_list_result(
        data, kind=LabelKind.SOURCE_LABEL, notebook_id=value.notebook_id
    )


def decode_collection_list_result(value: LabelListInput, data: Any) -> LabelListResult:
    """Row decoder for ``collection.list``."""
    del value
    return decode_label_set_list_result(data, kind=LabelKind.COLLECTION, notebook_id=None)


def _select_label(listed: LabelListResult, label_id: str) -> LabelGetResult:
    """Select one group by exact id from the set read; never by name."""
    return LabelGetResult(
        label=next((label for label in listed.labels if label.id == label_id), None)
    )


def decode_label_get_result(value: LabelGetInput, data: Any) -> LabelGetResult:
    """Row decoder for ``label.get`` (list-then-select; an absent id is ``None``)."""
    listed = decode_label_set_list_result(
        data, kind=LabelKind.SOURCE_LABEL, notebook_id=value.notebook_id
    )
    return _select_label(listed, value.label_id)


def decode_collection_get_result(value: LabelGetInput, data: Any) -> LabelGetResult:
    """Row decoder for ``collection.get``."""
    listed = decode_label_set_list_result(data, kind=LabelKind.COLLECTION, notebook_id=None)
    return _select_label(listed, value.label_id)


def decode_label_delete_result(value: LabelDeleteInput, data: Any) -> LabelDeleteResult:
    """Row decoder for ``label.delete``/``collection.delete``; the echo carries nothing."""
    del value, data
    return LabelDeleteResult()


def decode_label_generate_result(value: LabelGenerateInput, data: Any) -> LabelGenerateResult:
    """Row decoder for ``label.generate``: the full post-operation set echo."""
    return LabelGenerateResult(
        labels=decode_label_create_echo(
            data,
            notebook_id=value.notebook_id,
            method_id=RPCMethod.CREATE_LABEL.value,
        )
    )


__all__ = [
    "decode_label_allocate",
    "decode_label_mutate",
    "encode_label_allocate",
    "encode_label_mutate",
    "label_mutate_variant",
    "build_create_collection_params",
    "build_create_label_params",
    "build_delete_collections_params",
    "build_delete_labels_params",
    "build_generate_labels_params",
    "build_list_collections_params",
    "build_list_labels_params",
    "build_rename_collection_params",
    "build_update_collection_notebooks_params",
    "build_update_label_params",
    "decode_collection_get_result",
    "decode_collection_list_result",
    "decode_label",
    "decode_label_create_echo",
    "decode_label_delete_result",
    "decode_label_generate_result",
    "decode_label_get_result",
    "decode_label_list",
    "decode_label_list_result",
    "decode_label_set_list_result",
    "encode_collection_delete",
    "encode_collection_get",
    "encode_collection_list",
    "encode_label_delete",
    "encode_label_generate",
    "encode_label_get",
    "encode_label_list",
    "encode_label_set_list",
    "require_label_kind",
    "require_notebook_scope",
]


# -- P9.2 primitive rows ------------------------------------------------------
#
# ``label.mutate`` and ``label.allocate`` are the single-native leaves the
# hoisted update/create workflows sequence above the port. Each encoder returns
# the same payload the P6.4 composite handler dispatched for that native and
# never names a method; the row's ``NativeCallSpec`` selects the variant.


def _mutate_form(value: LabelMutateInput) -> str:
    """Return the one set-op form a mutate request addresses, failing closed."""
    forms = [
        form
        for form, present in (
            ("field", value.name is not None or value.emoji is not None),
            ("add", value.add_member_id is not None),
            ("remove", value.remove_member_id is not None),
        )
        if present
    ]
    if len(forms) != 1:
        raise BackendContractError(
            "label.mutate requires exactly one of a field mask, an add, or a remove",
            operation=Operation.LABEL_MUTATE,
        )
    return forms[0]


def encode_label_mutate(value: LabelMutateInput) -> CodecPayload:
    """Payload for the ``label.mutate`` row: one ``UPDATE_LABEL`` set-op."""
    form = _mutate_form(value)
    if value.kind is LabelKind.COLLECTION:
        if form == "field":
            if value.name is None:
                raise BackendContractError(
                    "label.mutate has no emoji-only field mask for collections; a name is required",
                    operation=Operation.LABEL_MUTATE,
                )
            params = build_rename_collection_params(value.label_id, value.name, value.emoji)
        else:
            params = build_update_collection_notebooks_params(
                value.label_id,
                add_notebook_id=value.add_member_id,
                remove_notebook_id=value.remove_member_id,
            )
        return CodecPayload(params=params, source_path=_ACCOUNT_PATH, allow_null=True)
    notebook_id = require_notebook_scope(value.notebook_id, Operation.LABEL_MUTATE)
    return CodecPayload(
        params=build_update_label_params(
            notebook_id,
            value.label_id,
            name=value.name,
            emoji=value.emoji,
            add_source_id=value.add_member_id,
            remove_source_id=value.remove_member_id,
        ),
        source_path=_notebook_path(notebook_id),
        allow_null=True,
    )


def label_mutate_variant(value: LabelMutateInput) -> str | None:
    """The ``UPDATE_LABEL`` variant one mutate request dispatches under."""
    form = _mutate_form(value)
    if form == "field":
        return None
    member = "notebooks" if value.kind is LabelKind.COLLECTION else "sources"
    return f"{form}_{member}"


def decode_label_mutate(value: LabelMutateInput, data: Any) -> LabelMutateResult:
    """Row decoder for ``label.mutate``: ``le8sX`` echoes ``[]`` and no group."""
    del value, data
    return LabelMutateResult()


def encode_label_allocate(value: LabelAllocateInput) -> CodecPayload:
    """Payload for the ``label.allocate`` row: one manual ``CREATE_LABEL``."""
    if value.kind is LabelKind.COLLECTION:
        return CodecPayload(
            params=build_create_collection_params(value.name),
            source_path=_ACCOUNT_PATH,
            allow_null=True,
        )
    notebook_id = require_notebook_scope(value.notebook_id, Operation.LABEL_ALLOCATE)
    return CodecPayload(
        params=build_create_label_params(notebook_id, value.name, value.emoji),
        source_path=_notebook_path(notebook_id),
        allow_null=True,
    )


def decode_label_allocate(
    value: LabelAllocateInput,
    data: Any,
    *,
    method_id: str,
) -> LabelAllocateResult:
    """Row decoder for ``label.allocate``: the source-label echo, or empty."""
    if value.kind is LabelKind.COLLECTION:
        # The collection create echo was never captured on the wire; the
        # workflow re-lists to attribute the allocation.
        return LabelAllocateResult()
    notebook_id = require_notebook_scope(value.notebook_id, Operation.LABEL_ALLOCATE)
    return LabelAllocateResult(
        labels=decode_label_create_echo(
            data,
            notebook_id=notebook_id,
            method_id=method_id,
        )
    )
