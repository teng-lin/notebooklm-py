"""Public API for NotebookLM collections (``client.collections``).

Account-level sibling of ``client.labels``: a *collection* groups whole
notebooks (playlist-style) rather than sources within one notebook. On the wire
a collection is a label of type ``3`` with a null notebook parent, so both
facades share one semantic slice — this one binds :class:`LabelSetService` to
:attr:`LabelKind.COLLECTION`, and the web codec owns the three request
differences (null notebook slot, trailing type discriminator, ``[1, 3]`` options
tail) plus the account source path.

Like ``LabelsAPI`` it takes a narrow ``list_notebooks`` callable
(``client.notebooks.list``) — wired in ``_client_composition.py`` after
``NotebooksAPI`` is built — for the membership→``Notebook`` join in
``notebooks()``.
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import Awaitable, Callable

from ._deadline import RuntimeDeadlineFactory
from ._lookup import unwrap_or_raise
from ._semantic.backend import BackendAdapter, BackendError
from ._semantic.compat import project_backend_error, project_local_not_found
from ._semantic.operations import Operation
from ._semantic.projectors import project_collection
from ._semantic.records import LabelKind
from ._semantic.services.label import LabelSetService, require_member_ids
from .types import Collection, Notebook

logger = logging.getLogger(__name__)

# Narrow capability: just ``notebooks.list() -> list[Notebook]`` (account-level,
# no notebook-id argument — unlike labels' source list).
ListNotebooks = Callable[[], Awaitable[builtins.list[Notebook]]]


class CollectionsAPI:
    """Operations on NotebookLM collections (``client.collections``).

    Usage::

        async with NotebookLMClient.from_storage() as client:
            coll = await client.collections.create("Research Q3")
            await client.collections.add_notebooks(coll.id, [nb_id])
            members = await client.collections.notebooks(coll.id)  # -> [Notebook]
            await client.collections.rename(coll.id, "Research Q4")
            await client.collections.delete(coll.id)
    """

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        list_notebooks: ListNotebooks,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        """``list_notebooks`` is ``client.notebooks.list`` (wired in
        ``_client_composition.py`` after ``NotebooksAPI`` is constructed) — needed
        for the membership→``Notebook`` join in ``notebooks()``. Same client /
        bound loop, so no loop-affinity concern (ADR-0004)."""
        self._service = LabelSetService(
            backend, LabelKind.COLLECTION, deadline_factory=deadline_factory
        )
        self._list_notebooks = list_notebooks

    # -- read ---------------------------------------------------------------

    async def list(self) -> builtins.list[Collection]:
        """List all collections in the account (``LIST_LABELS``, type 3)."""
        public_error: Exception | None = None
        try:
            records = await self._service.list()
        except BackendError as error:
            # The backend deliberately exposes only the neutral BackendError
            # vocabulary. At this public compatibility facade, reconstruct the
            # exact pre-migration exception class and its structured diagnostics.
            public_error = project_backend_error(error)
        else:
            return [project_collection(record) for record in records]
        raise public_error

    async def get_or_none(self, collection_id: str) -> Collection | None:
        """Get a collection by id, returning ``None`` when absent (sanctioned None-on-miss)."""
        public_error: Exception | None = None
        try:
            record = await self._service.get(collection_id)
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return None if record is None else project_collection(record)
        raise public_error

    async def get(self, collection_id: str) -> Collection:
        """Get a collection by id; raises ``CollectionNotFoundError`` on miss (ADR-0019)."""
        return unwrap_or_raise(
            await self.get_or_none(collection_id),
            project_local_not_found(Operation.COLLECTION_GET, collection_id),
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
        the backend snapshots the ids, fires the create, re-lists, and returns
        the single collection whose id is new. Unlike ``labels.create`` this
        re-lists rather than parsing the create echo — the collection
        create-response shape was not captured on the wire, so the robust path is
        a fresh list. Raises ``CollectionError`` if zero or more than one new id
        appears (a concurrent create) — intentionally loud, mirroring the label
        precedent.

        Collections carry no emoji at creation (the wire has no emoji slot); set
        one later in the UI if desired.
        """
        public_error: Exception | None = None
        try:
            record = await self._service.create(name)
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return project_collection(record)
        raise public_error

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
        return await self._update(collection_id, name=name, return_object=return_object)

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
        a single re-fetch backs the ADR-0019 return/not-found contract (``le8sX``
        echoes ``[]``, carrying no collection).

        **Not atomic across ids** and ``NON_IDEMPOTENT_NO_RETRY`` — a mid-loop
        failure leaves the already-written ids assigned and then raises; re-issue
        with the remaining ids.
        """
        unique_ids = require_member_ids(notebook_ids, "add_notebooks", "notebook")
        logger.debug("Adding %d notebook(s) to collection %s", len(unique_ids), collection_id)
        return await self._update(
            collection_id,
            add_member_ids=unique_ids,
            return_object=return_object,
        )

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
        :func:`~notebooklm._web.codec.labels.build_update_collection_notebooks_params`.
        An earlier inferred shape (id moved to a second group) was a silent wire
        no-op; independently confirmed broken and then fixed on four accounts
        (thanks to contributors tomihe0720 and erricklong85-tech). Removing an
        already-absent member is a confirmed silent no-op (live-verified), so
        it is classified ``IDEMPOTENT_SET_OP`` — retry-safe like the label
        ``remove_sources`` precedent.
        """
        unique_ids = require_member_ids(notebook_ids, "remove_notebooks", "notebook")
        logger.debug("Removing %d notebook(s) from collection %s", len(unique_ids), collection_id)
        return await self._update(
            collection_id,
            remove_member_ids=unique_ids,
            return_object=return_object,
        )

    async def _update(
        self,
        collection_id: str,
        *,
        name: str | None = None,
        add_member_ids: tuple[str, ...] = (),
        remove_member_ids: tuple[str, ...] = (),
        return_object: bool = True,
    ) -> Collection | None:
        """Run one validated collection mutation and project its compatibility result."""
        public_error: Exception | None = None
        try:
            record = await self._service.update(
                collection_id,
                name=name,
                add_member_ids=add_member_ids,
                remove_member_ids=remove_member_ids,
                return_object=return_object,
            )
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return None if record is None else project_collection(record)
        raise public_error

    # -- delete -------------------------------------------------------------

    async def delete(self, collection_ids: str | builtins.list[str]) -> None:
        """Delete one or more collections (``DELETE_LABEL``, batch). Accepts a
        single id or a list. Deleting a collection does NOT delete its member
        notebooks (they simply leave the collection).

        An absent target is an idempotent no-op returning ``None`` (consistent
        with ``labels.delete`` / ``notebooks.delete`` and ADR-0019).
        """
        ids = (collection_ids,) if isinstance(collection_ids, str) else tuple(collection_ids)
        if not ids:
            return None
        public_error: Exception | None = None
        try:
            await self._service.delete(ids)
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return None
        raise public_error
