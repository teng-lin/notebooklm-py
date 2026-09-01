"""Public API for NotebookLM collections (``client.collections``).

Account-level sibling of ``client.labels``: a *collection* groups whole
notebooks (playlist-style) rather than sources within one notebook. On the wire
a collection is a source-``Label`` of type ``3`` with a null notebook parent, so
this API reuses the four label RPCs (``LIST_LABELS`` / ``CREATE_LABEL`` /
``UPDATE_LABEL`` / ``DELETE_LABEL``) via the collection-specific param builders
in :mod:`notebooklm._web.params.collections`, always with ``source_path="/"`` (the
account/home context — collections have no notebook scope).

Like ``WebLabelsAPI`` it takes a narrow ``list_notebooks`` callable
(``client.notebooks.list``) — wired in ``_client_assembly.py`` after
``NotebooksAPI`` is built — for the membership→``Notebook`` join in
``notebooks()``.
"""

from __future__ import annotations

import builtins
import logging
from typing import Any

from .._collections import CollectionsAPI, ListNotebooks
from .._lookup import unwrap_or_raise
from ..exceptions import CollectionError, CollectionNotFoundError, UnknownRPCMethodError
from ..rpc import RPCMethod
from ..types import Collection, Notebook
from .contracts import RpcCaller
from .params.collections import (
    build_create_collection_params,
    build_delete_collections_params,
    build_list_collections_params,
    build_rename_collection_params,
    build_update_collection_notebooks_params,
)

# Preserve the historical logger key across the whole-module move.
logger = logging.getLogger("notebooklm._collections")

_SRC = "_collections"

# Collections are account-level: every RPC uses the home-page source path, not a
# ``/notebook/<id>`` path (they have no notebook scope). Compare the home-page
# mutation context in ``_web/notebooks.py::WebNotebooksAPI.update``.
_ACCOUNT_PATH = "/"


class WebCollectionsAPI(CollectionsAPI):
    """Operations on NotebookLM collections (``client.collections``).

    Usage::

        async with NotebookLMClient.from_storage() as client:
            coll = await client.collections.create("Research Q3")
            await client.collections.add_notebooks(coll.id, [nb_id])
            members = await client.collections.notebooks(coll.id)  # -> [Notebook]
            await client.collections.rename(coll.id, "Research Q4")
            await client.collections.delete(coll.id)
    """

    def __init__(self, rpc: RpcCaller, *, list_notebooks: ListNotebooks) -> None:
        """``list_notebooks`` is ``client.notebooks.list`` (wired in
        ``_client_assembly.py`` after ``NotebooksAPI`` is constructed) — needed
        for the membership→``Notebook`` join in ``notebooks()``. Same client /
        bound loop, so no loop-affinity concern (ADR-0004)."""
        self._rpc = rpc
        self._list_notebooks = list_notebooks

    # -- internal -----------------------------------------------------------

    def _collections_from_envelope(
        self, result: Any, *, method_id: str, index: int
    ) -> builtins.list[Collection]:
        """Map a collection-set envelope to ``Collection`` objects.

        Collections' ``LIST`` echoes ``[None, [collection, ...]]`` (``index=1``)
        — a leading-null wrapper, unlike source labels' ``LIST_LABELS`` which
        echoes ``[[label, ...]]`` (``index=0``). An empty/absent set decodes to
        ``[]``; a present-but-malformed envelope raises.
        """
        if not result:
            return []
        if not isinstance(result, list):
            raise UnknownRPCMethodError(
                message="collection set envelope is not a list",
                method_id=method_id,
                source=_SRC,
            )
        raw = result[index] if len(result) > index else None
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise UnknownRPCMethodError(
                message="collection set envelope malformed",
                method_id=method_id,
                source=_SRC,
            )
        return [Collection.from_api_response(tuple_, method_id=method_id) for tuple_ in raw]

    # -- read ---------------------------------------------------------------

    async def list(self) -> builtins.list[Collection]:
        """List all collections in the account (``LIST_LABELS``, type 3).

        ``allow_null=True`` because an account with zero collections may echo a
        null envelope, which decodes to ``[]`` rather than raising.
        """
        result = await self._rpc.rpc_call(
            RPCMethod.LIST_LABELS,
            build_list_collections_params(),
            source_path=_ACCOUNT_PATH,
            allow_null=True,
        )
        return self._collections_from_envelope(
            result, method_id=RPCMethod.LIST_LABELS.value, index=1
        )

    async def get_or_none(self, collection_id: str) -> Collection | None:
        """Get a collection by id, returning ``None`` when absent (sanctioned None-on-miss)."""
        for collection in await self.list():
            if collection.id == collection_id:
                return collection
        return None

    async def get(self, collection_id: str) -> Collection:
        """Get a collection by id; raises ``CollectionNotFoundError`` on miss (ADR-0019)."""
        return unwrap_or_raise(
            await self.get_or_none(collection_id),
            CollectionNotFoundError(collection_id, method_id=RPCMethod.LIST_LABELS.value),
        )

    async def notebooks(self, collection_id: str) -> builtins.list[Notebook]:
        """Expand a collection to its ``Notebook`` objects — the group-as-list accessor.

        Read-only convenience: one ``get(collection)`` + one
        ``self._list_notebooks()``, joined client-side (two reads, not N+1).
        Raises ``CollectionNotFoundError`` if the collection is absent. Order
        follows the collection's ``notebook_ids`` (membership order). A member id
        missing from the notebook list is skipped, not raised.

        Two causes of a skipped member, both benign (not schema drift):
        a notebook deleted between the two reads (a race), OR a member the
        backing ``notebooks.list`` (``ListRecentlyViewedProjects``) does not
        return — its completeness for *owned* notebooks is unconfirmed
        (see ``NotebooksAPI.list``). So this expansion can under-count vs. the
        authoritative ``len(collection.notebook_ids)`` reported by ``list()``;
        use ``notebook_ids`` when an exhaustive membership set is required.
        """
        collection = await self.get(collection_id)
        by_id = {nb.id: nb for nb in await self._list_notebooks()}
        return [by_id[nid] for nid in collection.notebook_ids if nid in by_id]

    # -- create -------------------------------------------------------------

    async def create(self, name: str) -> Collection:
        """Create an empty, named collection (``CREATE_LABEL``, type 3).

        Locates the new collection by ID-diff, NOT by name (names may collide):
        snapshot the ids, fire the create, re-list, and return the single
        collection whose id is new. Unlike ``labels.create`` this re-lists rather
        than parsing the create echo — the collection create-response shape was
        not captured on the wire, so the robust path is a fresh ``list()``.
        Raises ``CollectionError`` if zero or more than one new id appears (a
        concurrent create) — intentionally loud, mirroring the label precedent.

        Collections carry no emoji at creation (the wire has no emoji slot); set
        one later in the UI if desired.
        """
        before_ids = {collection.id for collection in await self.list()}
        await self._rpc.rpc_call(
            RPCMethod.CREATE_LABEL,
            build_create_collection_params(name),
            source_path=_ACCOUNT_PATH,
            allow_null=True,
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
        )
        after = await self.list()
        new = [collection for collection in after if collection.id not in before_ids]
        if len(new) != 1:
            raise CollectionError(
                f"create(name={name!r}) expected exactly 1 new collection, found {len(new)} "
                f"(a concurrent create, or read-after-write lag on the re-list, can cause "
                f"this — retry from a fresh list)"
            )
        (collection,) = new  # exactly one (guarded); unpack avoids the name[int] ratchet
        return collection

    # -- mutate (all UPDATE_LABEL) ------------------------------------------

    async def rename(
        self, collection_id: str, name: str, *, return_object: bool = True
    ) -> Collection | None:
        """Rename a collection (``UPDATE_LABEL``); preserves the existing emoji.

        The existence preflight raises ``CollectionNotFoundError`` on a missing
        target (ADR-0019) and supplies the current emoji so the rename never
        clobbers a UI-set emoji. A name-only rename is CONFIRMED to preserve
        the existing emoji server-side (live-captured, PR #2009), so this is
        belt-and-suspenders rather than a hedge against unverified behavior.
        """
        current = await self.get_or_none(collection_id)
        if current is None:
            raise CollectionNotFoundError(collection_id, method_id=RPCMethod.UPDATE_LABEL.value)
        await self._rpc.rpc_call(
            RPCMethod.UPDATE_LABEL,
            build_rename_collection_params(collection_id, name, current.emoji or ""),
            source_path=_ACCOUNT_PATH,
            allow_null=True,
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
            operation_variant=None,  # default IDEMPOTENT_SET_OP (rename/set)
        )
        if not return_object:
            return None
        return await self.get(collection_id)

    async def add_notebooks(
        self,
        collection_id: str,
        notebook_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Collection | None:
        """Add notebook(s) to a collection (``UPDATE_LABEL``, variant ``'add_notebooks'``).

        APPEND semantics: existing members preserved; pass only the ids to add. A
        notebook may belong to multiple collections, so this does not remove it
        from any other collection.

        Raises ``ValueError`` on empty ``notebook_ids`` BEFORE issuing any RPC.
        Issues **one ``le8sX`` call per notebook id** — the server honours only
        the first id per call (mirrors the confirmed label behaviour), so a
        single multi-id call would silently add only the first. After the writes,
        a single ``get_or_none`` re-fetch backs the ADR-0019 return/not-found
        contract (``le8sX`` echoes ``[]``, carrying no collection).

        **Not atomic across ids** and ``NON_IDEMPOTENT_NO_RETRY`` — a mid-loop
        failure leaves the already-written ids assigned and then raises; re-issue
        with the remaining ids.
        """
        if not notebook_ids:
            raise ValueError("add_notebooks requires at least one notebook id")
        unique_ids = list(dict.fromkeys(notebook_ids))
        logger.debug("Adding %d notebook(s) to collection %s", len(unique_ids), collection_id)
        for notebook_id in unique_ids:
            await self._rpc.rpc_call(
                RPCMethod.UPDATE_LABEL,
                build_update_collection_notebooks_params(
                    collection_id, add_notebook_id=notebook_id
                ),
                source_path=_ACCOUNT_PATH,
                allow_null=True,
                # #2290: a status-tagged null is a server rejection, not an empty success.
                raise_on_null_status=True,
                operation_variant="add_notebooks",  # → NON_IDEMPOTENT_NO_RETRY
            )
        collection = await self.get_or_none(collection_id)
        if collection is None:
            raise CollectionNotFoundError(collection_id, method_id=RPCMethod.UPDATE_LABEL.value)
        return collection if return_object else None

    async def remove_notebooks(
        self,
        collection_id: str,
        notebook_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Collection | None:
        """Un-assign notebook(s) from a collection (``UPDATE_LABEL``, variant
        ``'remove_notebooks'``).

        Removal un-assigns membership only: it does NOT delete the notebook, and
        a notebook that also belongs to another collection stays there.

        Raises ``ValueError`` on empty ``notebook_ids`` BEFORE issuing any RPC.
        Issues **one ``le8sX`` call per notebook id** and re-fetches once for the
        ADR-0019 return/not-found contract, mirroring ``add_notebooks``.

        **Wire shape (live-captured, PR #2009):** the un-assign fieldmask keeps
        the notebook id in the SAME group as add, shifted one slot (``[3]`` ->
        ``[4]``) — see
        :func:`~notebooklm._web.params.collections.build_update_collection_notebooks_params`.
        An earlier inferred shape (id moved to a second group) was a silent wire
        no-op; independently confirmed broken and then fixed on four accounts
        (thanks to contributors tomihe0720 and erricklong85-tech). Removing an
        already-absent member is a confirmed silent no-op (live-verified), so
        it is classified ``IDEMPOTENT_SET_OP`` — retry-safe like the label
        ``remove_sources`` precedent.
        """
        if not notebook_ids:
            raise ValueError("remove_notebooks requires at least one notebook id")
        unique_ids = list(dict.fromkeys(notebook_ids))
        logger.debug("Removing %d notebook(s) from collection %s", len(unique_ids), collection_id)
        for notebook_id in unique_ids:
            await self._rpc.rpc_call(
                RPCMethod.UPDATE_LABEL,
                build_update_collection_notebooks_params(
                    collection_id, remove_notebook_id=notebook_id
                ),
                source_path=_ACCOUNT_PATH,
                allow_null=True,
                # #2290: a status-tagged null is a server rejection, not an empty success.
                raise_on_null_status=True,
                operation_variant="remove_notebooks",  # → IDEMPOTENT_SET_OP
            )
        collection = await self.get_or_none(collection_id)
        if collection is None:
            raise CollectionNotFoundError(collection_id, method_id=RPCMethod.UPDATE_LABEL.value)
        return collection if return_object else None

    # -- delete -------------------------------------------------------------

    async def delete(self, collection_ids: str | builtins.list[str]) -> None:
        """Delete one or more collections (``DELETE_LABEL``, batch). Accepts a
        single id or a list. Deleting a collection does NOT delete its member
        notebooks (they simply leave the collection).

        An absent target is an idempotent no-op returning ``None`` (consistent
        with ``labels.delete`` / ``notebooks.delete`` and ADR-0019).
        """
        ids = [collection_ids] if isinstance(collection_ids, str) else list(collection_ids)
        if not ids:
            return None
        await self._rpc.rpc_call(
            RPCMethod.DELETE_LABEL,
            build_delete_collections_params(ids),
            source_path=_ACCOUNT_PATH,
            allow_null=True,
        )
        return None


__all__ = ["WebCollectionsAPI"]
