"""Public API for NotebookLM source labels (``client.labels``).

A compatibility facade over the semantic label slice: it validates public
arguments, delegates every wire-reaching operation to :class:`LabelSetService`
bound to :attr:`LabelKind.SOURCE_LABEL`, and projects neutral records onto the
public :class:`~notebooklm.types.Label` model. Because ``sources()`` expands
membership into ``Source`` objects, the constructor also takes a narrow
``list_sources`` callable (``client.sources.list``) — wired in
``_client_composition.py`` after ``SourcesAPI`` is built (mirrors ``NotebooksAPI``).

Collections are the same wire surface with a different discriminator; see
:mod:`notebooklm._collections`.
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from ._backend import BackendAdapter, BackendError
from ._deadline import RuntimeDeadlineFactory
from ._label_service import LabelSetService, require_member_ids
from ._lookup import unwrap_or_raise
from ._operations import Operation
from ._semantic.compat import project_backend_error, project_local_not_found
from ._semantic.projectors import project_label
from ._semantic.records import LabelKind
from .types import Label, Source

logger = logging.getLogger(__name__)

# Narrow capability: just ``sources.list(notebook_id) -> list[Source]``.
ListSources = Callable[[str], Awaitable[list[Source]]]


class LabelsAPI:
    """Operations on NotebookLM source labels (``client.labels``).

    Usage::

        async with NotebookLMClient.from_storage() as client:
            labels = await client.labels.generate(nb)              # AI grouping
            mine = await client.labels.create(nb, "Papers", "\U0001f4c4")  # manual
            await client.labels.add_sources(nb, mine.id, [src_id])
            members = await client.labels.sources(nb, mine.id)     # group -> Sources
            await client.labels.delete(nb, [mine.id])
    """

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        list_sources: ListSources,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        """``list_sources`` is ``client.sources.list`` (wired in
        ``_client_composition.py`` after ``SourcesAPI`` is constructed) — needed for the
        membership→Source join in ``sources()``. Same client/bound loop, so no
        loop-affinity concern (ADR-0004)."""
        self._service = LabelSetService(
            backend, LabelKind.SOURCE_LABEL, deadline_factory=deadline_factory
        )
        self._list_sources = list_sources

    # -- read ---------------------------------------------------------------

    async def list(self, notebook_id: str) -> builtins.list[Label]:
        """List all labels in a notebook (``LIST_LABELS``), with source membership."""
        public_error: Exception | None = None
        try:
            records = await self._service.list(notebook_id)
        except BackendError as error:
            # The backend deliberately exposes only the neutral BackendError
            # vocabulary. At this public compatibility facade, reconstruct the
            # exact pre-migration exception class and its structured diagnostics.
            public_error = project_backend_error(error)
        else:
            return [project_label(record) for record in records]
        raise public_error

    async def get_or_none(self, notebook_id: str, label_id: str) -> Label | None:
        """Get a label by id, returning ``None`` when absent (sanctioned None-on-miss)."""
        public_error: Exception | None = None
        try:
            record = await self._service.get(label_id, notebook_id)
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return None if record is None else project_label(record)
        raise public_error

    async def get(self, notebook_id: str, label_id: str) -> Label:
        """Get a label by id; raises ``LabelNotFoundError`` on miss (ADR-0019)."""
        return unwrap_or_raise(
            await self.get_or_none(notebook_id, label_id),
            project_local_not_found(Operation.LABEL_GET, label_id),
        )

    async def sources(self, notebook_id: str, label_id: str) -> builtins.list[Source]:
        """Expand a label to its ``Source`` objects — the group-as-collection accessor.

        Read-only convenience: one ``get(label)`` + one
        ``self._list_sources(nb)``, joined client-side (two reads, not N+1). Raises
        ``LabelNotFoundError`` if the label is absent. Order follows the label's
        ``source_ids`` (membership order), not notebook order. A member id missing
        from the source list (concurrent deletion between the two reads) is
        skipped, not raised — a benign race, not schema drift.
        """
        label = await self.get(notebook_id, label_id)
        by_id = {source.id: source for source in await self._list_sources(notebook_id)}
        return [by_id[sid] for sid in label.source_ids if sid in by_id]

    # -- generate / create --------------------------------------------------

    async def generate(
        self, notebook_id: str, *, scope: Literal["all", "unlabeled"] = "unlabeled"
    ) -> builtins.list[Label]:
        """AI-group sources into topic labels — the UI's "Auto-label" (first run) /
        "Reorganize" (re-run) action, wire ``CREATE_LABEL``.

        ``scope='unlabeled'`` (default, safe) labels only currently-unlabeled
        sources, preserving existing labels; ``scope='all'`` WIPES + regenerates
        EVERY label with new ids (destructive — the CLI gates it behind
        ``--yes/-y``). Returns the full post-op label set (``agX4Bc`` echoes it).

        Raises ``ValueError`` on an unrecognized ``scope`` BEFORE issuing any RPC
        — the semantic request carries a boolean replace flag, so a
        runtime-invalid value would otherwise silently build the (safe but
        unintended) ``"unlabeled"`` request.
        """
        if scope not in ("all", "unlabeled"):
            raise ValueError(f"generate scope must be 'all' or 'unlabeled', got {scope!r}")
        public_error: Exception | None = None
        try:
            records = await self._service.generate(notebook_id, replace_existing=scope == "all")
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return [project_label(record) for record in records]
        raise public_error

    async def create(self, notebook_id: str, name: str, emoji: str = "") -> Label:
        """Create an empty, manually-named label (``CREATE_LABEL`` slot[5]).

        Locates the new label by ID-diff, NOT by name (names may collide): the
        backend snapshots the label ids, fires the create (whose echo is the full
        set), and returns the single label whose id is new. Raises ``LabelError``
        if zero or more than one new id appears — the ambiguity (a concurrent
        create) is intentionally loud, mirroring the ``ADD_SOURCE_FILE``
        baseline-diff precedent.
        """
        public_error: Exception | None = None
        try:
            record = await self._service.create(name, notebook_id, emoji=emoji)
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return project_label(record)
        raise public_error

    # -- mutate (all UPDATE_LABEL) ------------------------------------------

    async def update(
        self,
        notebook_id: str,
        label_id: str,
        *,
        name: str | None = None,
        emoji: str | None = None,
        return_object: bool = True,
    ) -> Label | None:
        """Set name and/or emoji (``UPDATE_LABEL``).

        Raises ``ValueError`` if BOTH ``name`` and ``emoji`` are ``None`` (no-op
        fieldmask) BEFORE issuing any RPC. The existence preflight runs in both
        ``return_object`` modes and raises ``LabelNotFoundError`` on a missing
        target (ADR-0019). When only ``name`` is given, the current emoji is
        carried over from the preflight so a rename never clobbers the emoji.
        """
        if name is None and emoji is None:
            raise ValueError("update requires name and/or emoji")
        return await self._update(
            notebook_id,
            label_id,
            name=name,
            emoji=emoji,
            return_object=return_object,
        )

    async def rename(
        self, notebook_id: str, label_id: str, name: str, *, return_object: bool = True
    ) -> Label | None:
        """Rename a label (``UPDATE_LABEL``); preserves the existing emoji."""
        return await self.update(notebook_id, label_id, name=name, return_object=return_object)

    async def set_emoji(
        self, notebook_id: str, label_id: str, emoji: str, *, return_object: bool = True
    ) -> Label | None:
        """Set a label's emoji (``UPDATE_LABEL``)."""
        return await self.update(notebook_id, label_id, emoji=emoji, return_object=return_object)

    async def add_sources(
        self,
        notebook_id: str,
        label_id: str,
        source_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Label | None:
        """Add source(s) to a label (``UPDATE_LABEL``, variant ``'add_sources'``).

        APPEND semantics: existing members preserved; pass only the IDs to add.
        Does NOT remove the sources from any other label (labels may overlap).

        Raises ``ValueError`` on an empty ``source_ids`` BEFORE issuing any RPC.

        Issues **one ``le8sX`` call per source id** — the server honours only the
        first id of ``sources_add`` per call (confirmed 2026-06-07, rpc.md), so a
        single multi-id call would silently add only the first source. After all
        per-id writes, a single contract-load-bearing re-fetch backs the ADR-0019
        return/not-found contract (``le8sX`` echoes ``[]``, carrying no label; the
        existence check must raise on a missing label even when
        ``return_object=False``). The re-fetch is NOT removable — the label
        wire gives no return payload.

        **Not atomic across ids:** each id is a separate write, so a mid-loop RPC
        failure leaves the already-written ids assigned and then raises (this
        variant is ``NON_IDEMPOTENT_NO_RETRY`` — the transport does not auto-retry).
        The caller can re-issue with the remaining ids.
        """
        unique_ids = require_member_ids(source_ids, "add_sources", "source")
        logger.debug("Adding %d source(s) to label %s", len(unique_ids), label_id)
        return await self._update(
            notebook_id,
            label_id,
            add_member_ids=unique_ids,
            return_object=return_object,
        )

    async def remove_sources(
        self,
        notebook_id: str,
        label_id: str,
        source_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Label | None:
        """Un-assign source(s) from a label (``UPDATE_LABEL``, variant
        ``'remove_sources'``).

        Removal is **label-scoped un-assignment**: it removes the membership only,
        it does NOT delete the source from the notebook, and a source that also
        belongs to another label stays in that other label (overlap preserved).
        Removing a source that is not a member is a silent no-op (set-op
        semantics, confirmed 2026-06-07, rpc.md).

        Raises ``ValueError`` on an empty ``source_ids`` BEFORE issuing any RPC.

        Issues **one ``le8sX`` call per source id** — the server honours only the
        first id of ``sources_remove`` per call, so a single multi-id call would
        silently remove only the first source. After all per-id writes, a single
        contract-load-bearing re-fetch backs the ADR-0019 return/not-found
        contract (``le8sX`` echoes ``[]``, carrying no label; the existence check
        must raise on a missing label even when ``return_object=False``).

        **Not atomic across ids**, but ``remove_sources`` is ``IDEMPOTENT_SET_OP``,
        so a mid-loop failure is safely recovered by re-calling with the full set
        (removing an already-absent member is a no-op).
        """
        unique_ids = require_member_ids(source_ids, "remove_sources", "source")
        logger.debug("Removing %d source(s) from label %s", len(unique_ids), label_id)
        return await self._update(
            notebook_id,
            label_id,
            remove_member_ids=unique_ids,
            return_object=return_object,
        )

    async def _update(
        self,
        notebook_id: str,
        label_id: str,
        *,
        name: str | None = None,
        emoji: str | None = None,
        add_member_ids: tuple[str, ...] = (),
        remove_member_ids: tuple[str, ...] = (),
        return_object: bool = True,
    ) -> Label | None:
        """Run one validated label mutation and project its compatibility result."""
        public_error: Exception | None = None
        try:
            record = await self._service.update(
                label_id,
                notebook_id,
                name=name,
                emoji=emoji,
                add_member_ids=add_member_ids,
                remove_member_ids=remove_member_ids,
                return_object=return_object,
            )
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return None if record is None else project_label(record)
        raise public_error

    # -- delete -------------------------------------------------------------

    async def delete(self, notebook_id: str, label_ids: str | builtins.list[str]) -> None:
        """Delete one or more labels (``DELETE_LABEL``, batch). Accepts a single id
        or a list. Deleting a label does NOT delete its sources (they become
        unlabeled).

        An absent target is an idempotent no-op returning ``None`` (consistent
        with ``sources.delete``/``notebooks.delete`` and ADR-0019). This is a
        separate axis from the transport-retry idempotency class, which stays
        ``NON_IDEMPOTENT_NO_RETRY`` (conservative; already-absent retry behavior is
        wire-unverified, §15).
        """
        ids = (label_ids,) if isinstance(label_ids, str) else tuple(label_ids)
        if not ids:
            return None
        public_error: Exception | None = None
        try:
            await self._service.delete(ids, notebook_id)
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return None
        raise public_error
