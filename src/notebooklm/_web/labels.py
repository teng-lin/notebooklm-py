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
from typing import Any, Literal

from .._idempotency import call_unconfirmed_on_transport_loss
from .._labels import LabelsAPI, ListSources
from ..exceptions import LabelError, UnknownRPCMethodError
from ..rpc import RPCMethod
from ..types import Label
from .contracts import RpcCaller
from .params.labels import (
    build_create_label_params,
    build_delete_labels_params,
    build_generate_labels_params,
    build_list_labels_params,
    build_update_label_params,
)

# Preserve the historical logger key across the whole-module move.
logger = logging.getLogger("notebooklm._labels")

_SRC = "_labels"


class WebLabelsAPI(LabelsAPI):
    """Operations on NotebookLM source labels (``client.labels``).

    Usage::

        async with NotebookLMClient.from_storage() as client:
            labels = await client.labels.generate(nb)              # AI grouping
            mine = await client.labels.create(nb, "Papers", "\U0001f4c4")  # manual
            await client.labels.add_sources(nb, mine.id, [src_id])
            members = await client.labels.sources(nb, mine.id)     # group -> Sources
            await client.labels.delete(nb, [mine.id])
    """

    _list_method_id = RPCMethod.LIST_LABELS.value
    _mutation_method_id = RPCMethod.UPDATE_LABEL.value
    _property_readback_miss_method_id = RPCMethod.LIST_LABELS.value
    _delete_method_id = RPCMethod.DELETE_LABEL.value

    def __init__(self, rpc: RpcCaller, *, list_sources: ListSources) -> None:
        """``list_sources`` is ``client.sources.list`` (wired in ``_client_assembly.py``
        after the ``SourcesAPI`` is constructed) — needed for the
        membership→Source join in ``sources()``. Same client/bound loop, so no
        loop-affinity concern (ADR-0004)."""
        super().__init__(list_sources=list_sources)
        self._rpc = rpc

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
        result = await call_unconfirmed_on_transport_loss(
            lambda: self._rpc.rpc_call(
                RPCMethod.CREATE_LABEL,
                build_generate_labels_params(notebook_id, scope=scope),
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
                # #2290: a status-tagged null is a server rejection, not an empty success.
                raise_on_null_status=True,
            ),
            method=RPCMethod.CREATE_LABEL,
            what="the automatic label generation",
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
        result = await call_unconfirmed_on_transport_loss(
            lambda: self._rpc.rpc_call(
                RPCMethod.CREATE_LABEL,
                build_create_label_params(notebook_id, name, emoji),
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
                # #2290: a status-tagged null is a server rejection, not an empty success.
                raise_on_null_status=True,
            ),
            method=RPCMethod.CREATE_LABEL,
            what="the manual label create",
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

    async def _send_update(
        self,
        operation: Literal["properties", "delete"],
        notebook_id: str,
        label_ids: builtins.list[str],
        *,
        name: str | None = None,
        emoji: str | None = None,
        current: Label | None = None,
    ) -> None:
        if operation == "delete":
            await self._rpc.rpc_call(
                RPCMethod.DELETE_LABEL,
                build_delete_labels_params(notebook_id, label_ids),
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
            )
            return
        (label_id,) = label_ids
        assert current is not None
        effective_emoji = emoji
        if name is not None and emoji is None:
            effective_emoji = current.emoji or ""
        await self._rpc.rpc_call(
            RPCMethod.UPDATE_LABEL,
            build_update_label_params(notebook_id, label_id, name=name, emoji=effective_emoji),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
            operation_variant=None,  # default IDEMPOTENT_SET_OP (not "add_sources")
        )

    async def _send_mutate_member(
        self,
        notebook_id: str,
        label_id: str,
        source_id: str,
        *,
        operation: Literal["add_sources", "remove_sources"],
    ) -> None:
        await self._rpc.rpc_call(
            RPCMethod.UPDATE_LABEL,
            build_update_label_params(
                notebook_id,
                label_id,
                add_source_id=source_id if operation == "add_sources" else None,
                remove_source_id=source_id if operation == "remove_sources" else None,
            ),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            raise_on_null_status=True,
            operation_variant=operation,
        )


__all__ = ["WebLabelsAPI"]
