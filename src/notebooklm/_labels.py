"""Public API for NotebookLM source labels (``client.labels``).

Pure-RPC like ``SharingAPI``, but because ``sources()`` and the membership join
expand into ``Source`` objects, the constructor also takes a narrow
``list_sources`` callable (``client.sources.list``) — wired in ``_client_assembly.py``
after ``SourcesAPI`` is built (mirrors ``NotebooksAPI``). No ``LabelService``, no
``kind`` param, no artifact concepts — source labels only.
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from ._label.params import (
    build_create_label_params,
    build_delete_labels_params,
    build_generate_labels_params,
    build_list_labels_params,
    build_update_label_params,
)
from ._lookup import unwrap_or_raise
from ._runtime.contracts import RpcCaller
from .exceptions import LabelError, LabelNotFoundError, UnknownRPCMethodError
from .rpc import RPCMethod
from .types import Label, Source

logger = logging.getLogger(__name__)

# Narrow capability: just ``sources.list(notebook_id) -> list[Source]``.
ListSources = Callable[[str], Awaitable[list[Source]]]

_SRC = "_labels"


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

    def __init__(self, rpc: RpcCaller, *, list_sources: ListSources) -> None:
        """``list_sources`` is ``client.sources.list`` (wired in ``_client_assembly.py``
        after the ``SourcesAPI`` is constructed) — needed for the
        membership→Source join in ``sources()``. Same client/bound loop, so no
        loop-affinity concern (ADR-0004)."""
        self._rpc = rpc
        self._list_sources = list_sources

    # -- internal -----------------------------------------------------------

    def _labels_from_envelope(
        self, result: Any, *, notebook_id: str, method_id: str, index: int
    ) -> builtins.list[Label]:
        """Map a label-set envelope to ``Label`` objects.

        ``LIST_LABELS`` echoes ``[[label, ...]]`` (``index=0``); ``CREATE_LABEL``
        echoes ``[None, [label, ...]]`` (``index=1``). An empty/absent label set
        decodes to ``[]``; a present-but-malformed envelope raises.
        """
        if not result:
            return []
        if not isinstance(result, list):
            raise UnknownRPCMethodError(
                message="label set envelope is not a list",
                method_id=method_id,
                source=_SRC,
            )
        raw = result[index] if len(result) > index else None
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise UnknownRPCMethodError(
                message="label set envelope malformed",
                method_id=method_id,
                source=_SRC,
            )
        return [
            Label.from_api_response(tuple_, notebook_id=notebook_id, method_id=method_id)
            for tuple_ in raw
        ]

    # -- read ---------------------------------------------------------------

    async def list(self, notebook_id: str) -> builtins.list[Label]:
        """List all labels in a notebook (``LIST_LABELS``), with source membership."""
        result = await self._rpc.rpc_call(
            RPCMethod.LIST_LABELS,
            build_list_labels_params(notebook_id),
            source_path=f"/notebook/{notebook_id}",
        )
        return self._labels_from_envelope(
            result, notebook_id=notebook_id, method_id=RPCMethod.LIST_LABELS.value, index=0
        )

    async def get_or_none(self, notebook_id: str, label_id: str) -> Label | None:
        """Get a label by id, returning ``None`` when absent (sanctioned None-on-miss)."""
        for label in await self.list(notebook_id):
            if label.id == label_id:
                return label
        return None

    async def get(self, notebook_id: str, label_id: str) -> Label:
        """Get a label by id; raises ``LabelNotFoundError`` on miss (ADR-0019)."""
        return unwrap_or_raise(
            await self.get_or_none(notebook_id, label_id),
            LabelNotFoundError(label_id, method_id=RPCMethod.LIST_LABELS.value),
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
        — the param builder treats anything != ``"all"`` as ``"unlabeled"``, so a
        runtime-invalid value would otherwise silently build the (safe but
        unintended) ``"unlabeled"`` payload.
        """
        if scope not in ("all", "unlabeled"):
            raise ValueError(f"generate scope must be 'all' or 'unlabeled', got {scope!r}")
        result = await self._rpc.rpc_call(
            RPCMethod.CREATE_LABEL,
            build_generate_labels_params(notebook_id, scope=scope),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return self._labels_from_envelope(
            result, notebook_id=notebook_id, method_id=RPCMethod.CREATE_LABEL.value, index=1
        )

    async def create(self, notebook_id: str, name: str, emoji: str = "") -> Label:
        """Create an empty, manually-named label (``CREATE_LABEL`` slot[5]).

        Locates the new label by ID-diff, NOT by name (names may collide): snapshot
        the label ids, fire the create (whose echo is the full set), and return the
        single label whose id is new. Raises ``LabelError`` if zero or more than one
        new id appears — the ambiguity (a concurrent create) is intentionally loud,
        mirroring the ``ADD_SOURCE_FILE`` baseline-diff precedent.
        """
        before_ids = {label.id for label in await self.list(notebook_id)}
        result = await self._rpc.rpc_call(
            RPCMethod.CREATE_LABEL,
            build_create_label_params(notebook_id, name, emoji),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        after = self._labels_from_envelope(
            result, notebook_id=notebook_id, method_id=RPCMethod.CREATE_LABEL.value, index=1
        )
        new = [label for label in after if label.id not in before_ids]
        if len(new) != 1:
            raise LabelError(
                f"create(name={name!r}) expected exactly 1 new label, found {len(new)} "
                f"(concurrent label creation can cause this — retry from a fresh list)"
            )
        # ``new`` is a list[Label] (typed dataclass instances), not a decoded
        # RPC payload — positional RPC-row decode already happened in
        # ``_labels_from_envelope``/``LabelRow``. Tuple unpacking avoids the
        # type-blind single-level ``name[int]`` guardrail false-positive that a
        # ``new[0]`` index would trip, while asserting exactly-one semantics.
        (label,) = new  # exactly one (guarded); unpack avoids the name[int] ratchet
        return label

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
        current = await self.get_or_none(notebook_id, label_id)
        if current is None:
            raise LabelNotFoundError(label_id, method_id=RPCMethod.UPDATE_LABEL.value)
        effective_emoji = emoji
        if name is not None and emoji is None:
            # Preserve the existing emoji (preflight-derived) — see rpc.md §15.
            effective_emoji = current.emoji or ""
        await self._rpc.rpc_call(
            RPCMethod.UPDATE_LABEL,
            build_update_label_params(notebook_id, label_id, name=name, emoji=effective_emoji),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            operation_variant=None,  # default IDEMPOTENT_SET_OP (not "add_sources")
        )
        if not return_object:
            return None
        return await self.get(notebook_id, label_id)

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
        per-id writes, a single contract-load-bearing ``get_or_none`` re-fetch
        backs the ADR-0019 return/not-found contract (``le8sX`` echoes ``[]``,
        carrying no label; the existence check must raise on a missing label even
        when ``return_object=False``). The re-fetch is NOT removable — the label
        wire gives no return payload.

        **Not atomic across ids:** each id is a separate write, so a mid-loop RPC
        failure leaves the already-written ids assigned and then raises (this
        variant is ``NON_IDEMPOTENT_NO_RETRY`` — the transport does not auto-retry).
        The caller can re-issue with the remaining ids.
        """
        if not source_ids:
            raise ValueError("add_sources requires at least one source id")
        # Dedupe (order-preserving): one le8sX per id, so duplicates would be
        # redundant round-trips (and append-twice on the wire).
        unique_ids = list(dict.fromkeys(source_ids))
        logger.debug("Adding %d source(s) to label %s", len(unique_ids), label_id)
        for source_id in unique_ids:
            await self._rpc.rpc_call(
                RPCMethod.UPDATE_LABEL,
                build_update_label_params(notebook_id, label_id, add_source_id=source_id),
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
                operation_variant="add_sources",  # → NON_IDEMPOTENT_NO_RETRY (§4)
            )
        label = await self.get_or_none(notebook_id, label_id)
        if label is None:
            raise LabelNotFoundError(label_id, method_id=RPCMethod.UPDATE_LABEL.value)
        return label if return_object else None

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
        contract-load-bearing ``get_or_none`` re-fetch backs the ADR-0019
        return/not-found contract (``le8sX`` echoes ``[]``, carrying no label; the
        existence check must raise on a missing label even when
        ``return_object=False``).

        **Not atomic across ids**, but ``remove_sources`` is ``IDEMPOTENT_SET_OP``,
        so a mid-loop failure is safely recovered by re-calling with the full set
        (removing an already-absent member is a no-op).
        """
        if not source_ids:
            raise ValueError("remove_sources requires at least one source id")
        # Dedupe (order-preserving): one le8sX per id, so duplicates are
        # redundant round-trips.
        unique_ids = list(dict.fromkeys(source_ids))
        logger.debug("Removing %d source(s) from label %s", len(unique_ids), label_id)
        for source_id in unique_ids:
            await self._rpc.rpc_call(
                RPCMethod.UPDATE_LABEL,
                build_update_label_params(notebook_id, label_id, remove_source_id=source_id),
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
                operation_variant="remove_sources",  # → IDEMPOTENT_SET_OP (§4)
            )
        label = await self.get_or_none(notebook_id, label_id)
        if label is None:
            raise LabelNotFoundError(label_id, method_id=RPCMethod.UPDATE_LABEL.value)
        return label if return_object else None

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
        ids = [label_ids] if isinstance(label_ids, str) else list(label_ids)
        if not ids:
            return None
        await self._rpc.rpc_call(
            RPCMethod.DELETE_LABEL,
            build_delete_labels_params(notebook_id, ids),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return None
