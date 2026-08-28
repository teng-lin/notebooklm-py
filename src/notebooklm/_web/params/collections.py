"""Stable request payload builders for the account-level *collection* RPCs.

A collection is a source-``Label`` of type ``3`` with **no notebook parent**, so
these builders reuse the four label RPCs (``I3xc3c`` / ``agX4Bc`` / ``le8sX`` /
``GyzE7e``) with three wire differences vs. :mod:`notebooklm._web.params.labels`
(owner-captured on the live Gemini-Notebook UI, issue #2006):

1. ``notebook_id`` (label slot ``[1]``) is ``None`` — collections are
   account-level, not notebook-scoped.
2. A type discriminator ``3`` rides in the **last** slot of every request.
3. The request-options wrapper (arg ``[0]``) ends in ``[1, 3]`` instead of the
   label wrapper's ``[1]``.

Every builder returns a **fresh** structure per call so callers never alias a
shared mutable wrapper (cf. :func:`notebooklm._web.params.labels._opts`).
"""

from __future__ import annotations

from typing import Any

# Type discriminator that marks a label RPC as operating on collections (type 3
# labels with a null notebook parent). Rides the last slot of every request.
_COLLECTION_TYPE = 3


def _opts() -> list[Any]:
    """Fresh request-options wrapper (arg ``[0]`` of every collection RPC).

    Identical to the label wrapper except the trailing context list is
    ``[1, 3]`` (not ``[1]``) — the collection-scope marker. Returned fresh so
    callers never alias a shared mutable list.
    """
    return [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1, 3]]]


def build_list_collections_params() -> list[Any]:
    """LIST_LABELS (``I3xc3c``) for collections: ``[opts, None, 3]``.

    Response echoes ``[None, [ [name, [nb_id, ...], collection_id, emoji], ... ]]``
    — populated members are bare id strings, not label-style wrapped singletons
    (live-captured, PR #2009; see :class:`notebooklm._types.collections.Collection`).
    """
    return [_opts(), None, _COLLECTION_TYPE]


def _opts_create() -> list[Any]:
    """Fresh request-options wrapper for CREATE only.

    Same as :func:`_opts` except slot ``[2]`` is ``[1]`` instead of ``None`` —
    the original slot-``[7]``/``opts[2] is None`` shape reproducibly left
    nothing server-side (confirmed live on three independent accounts, PR
    #2009); this is the shape a live UI create actually sends.
    """
    return [2, None, [1], [1, None, None, None, None, None, None, None, None, None, [1, 3]]]


def build_create_collection_params(name: str) -> list[Any]:
    """CREATE_LABEL (``agX4Bc``) for collections — manual create.

    ``[opts, None, None, None, None, [[name]], 3]``. Unlike a source label, the
    create wire carries the name at slot ``[5]`` and has **no emoji slot**
    (collections get an emoji via a later update, if at all).
    """
    return [_opts_create(), None, None, None, None, [[name]], _COLLECTION_TYPE]


def build_rename_collection_params(
    collection_id: str, name: str, emoji: str | None = None
) -> list[Any]:
    """UPDATE_LABEL (``le8sX``) rename for collections.

    Fieldmask slot ``[3]`` = ``[[[name]]]`` (name-only, matching the captured UI
    rename) or ``[[[name, emoji]]]`` when ``emoji`` is supplied. CONFIRMED on the
    wire (live-captured, PR #2009): a length-1 ``name_emoji`` PRESERVES an
    existing emoji rather than clearing it (settles the same open question for
    labels). The API layer still passes the current emoji explicitly — belt and
    suspenders, and it means ``rename()`` doesn't depend on this confirmation.
    """
    name_emoji: list[Any] = [name] if emoji is None else [name, emoji]
    return [_opts(), None, collection_id, [[name_emoji]], _COLLECTION_TYPE]


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
    (incorrectly) inferred, which made the original ``remove_notebooks`` a
    silent wire no-op.

    * **add** (wire-captured): ``[[None, None, None, [[nb_id]]], []]``.
    * **remove** (wire-captured): ``[[None, None, None, None, [[nb_id]]], []]``.

    The wire honours only the FIRST id per call, so the builder is
    **singular** — pass at most one ``add_notebook_id`` and one
    ``remove_notebook_id`` (the API layer loops one call per id).
    """
    if add_notebook_id is not None:
        group0: list[Any] = [None, None, None, [[add_notebook_id]]]
    elif remove_notebook_id is not None:
        group0 = [None, None, None, None, [[remove_notebook_id]]]
    else:
        group0 = []
    return [_opts(), None, collection_id, [group0, []], _COLLECTION_TYPE]


def build_delete_collections_params(collection_ids: list[str]) -> list[Any]:
    """DELETE_LABEL (``GyzE7e``) for collections — batch, array of ids.

    ``[opts, None, [collection_id, ...], 3]``.
    """
    return [_opts(), None, list(collection_ids), _COLLECTION_TYPE]
