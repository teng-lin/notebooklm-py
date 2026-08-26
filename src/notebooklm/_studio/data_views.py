"""Transport-neutral mind-map and data-table family behavior."""

from __future__ import annotations

import json

from .._backend import BackendAdapter, BackendError, rebind_operation, require_leaves
from .._deadline import RuntimeDeadline
from .._note_service import NoteService
from .._operations import Operation
from .._semantic.records import (
    ARTIFACT_GENERATE_DATA_TABLE_DEF,
    MIND_MAP_GENERATE_NOTE_DEF,
    NOTE_CREATE_DEF,
    NOTE_DELETE_DEF,
    NOTE_UPDATE_DEF,
    NOTEBOOK_GET_DEF,
    ArtifactRecord,
    DataTableGenerateRequest,
    DataTableGenerateResult,
    MindMapGenerateInput,
    MindMapGenerateResult,
)
from .catalog import StudioCatalog
from .generation import StudioGenerationInputs, _generation_budget

#: The title a generated tree carries when it names none of its own.
_DEFAULT_MIND_MAP_TITLE = "Mind Map"


def _derive_tree(tree_json: str) -> object:
    """Parse the serialized tree, keeping an unparseable payload as its text.

    Ported from the codec rather than re-derived: the ``except`` clause is
    ``json.JSONDecodeError`` alone, so a non-string payload — already
    ``json.dumps``-ed on its way across the port — round-trips to the same
    object the composite returned, and a string that is not JSON is passed
    through as itself.
    """

    try:
        return json.loads(tree_json)
    except json.JSONDecodeError:
        return tree_json


def _derive_title(tree: object) -> str:
    """Take the tree's own ``name`` when it has a usable one."""

    if isinstance(tree, dict):
        name = tree.get("name")
        if isinstance(name, str) and name:
            return name
    return _DEFAULT_MIND_MAP_TITLE


class DataTableFamilyService:
    """Data-table generation and complete catalog selection."""

    __slots__ = ("_backend", "_catalog", "_inputs")

    def __init__(
        self,
        backend: BackendAdapter,
        catalog: StudioCatalog,
        inputs: StudioGenerationInputs,
    ) -> None:
        self._backend = backend
        self._catalog = catalog
        self._inputs = inputs

    async def generate(
        self,
        request: DataTableGenerateRequest,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> DataTableGenerateResult:
        deadline = _generation_budget(self._inputs, deadline)
        return await self._backend.invoke(
            ARTIFACT_GENERATE_DATA_TABLE_DEF,
            await self._inputs.data_table(request, deadline=deadline),
            deadline=deadline,
        )

    async def list(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        return await self._catalog.list_records(notebook_id, "data_table", deadline=deadline)

    async def get(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactRecord | None:
        record = await self._catalog.get_record(notebook_id, artifact_id, deadline=deadline)
        return record if record is not None and record.family == "data_table" else None


class NoteBackedMindMapFamilyService:
    """Mind-map generation and dual-backing catalog selection.

    The catalog implements ADR-0019 partial availability: an ordinary RPC
    failure in the optional note-backed subfetch leaves interactive Studio
    maps available, while transport and decoding failures still surface.

    Generation is a workflow, not a leaf: the tree the generation native
    returns is not persisted server-side, so this service sequences the
    default-source read, the generation and the note allocation that stores it
    under one budget.
    """

    __slots__ = ("_backend", "_catalog", "_inputs", "_notes")

    def __init__(
        self,
        backend: BackendAdapter,
        catalog: StudioCatalog,
        inputs: StudioGenerationInputs,
    ) -> None:
        self._backend = backend
        self._catalog = catalog
        self._inputs = inputs
        self._notes = NoteService(backend)

    async def generate(
        self,
        value: MindMapGenerateInput,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> MindMapGenerateResult:
        """Resolve the request, generate a tree, then persist it as a note.

        ``source_ids=None`` means every source in the notebook and
        ``language=None`` the environment default; both are resolved by
        :class:`StudioGenerationInputs` above the port (P10 R5.1b), inside the
        ``try`` so a failing read keeps this workflow's public identity.

        The note allocation runs through :meth:`NoteService.create_note_record`,
        which owns the package's one cancellation-safe create: the finalizing
        ``note.update`` is shielded from an outer cancel and, if one arrives, a
        best-effort ``note.delete`` is scheduled after the shielded update
        settles, so a cancelled generation never leaves an orphan row holding a
        half-written mind map.
        """

        workflow = Operation.ARTIFACT_GENERATE_MIND_MAP
        require_leaves(
            self._backend,
            NOTEBOOK_GET_DEF.key,
            MIND_MAP_GENERATE_NOTE_DEF.key,
            NOTE_CREATE_DEF.key,
            NOTE_UPDATE_DEF.key,
            NOTE_DELETE_DEF.key,
        )
        deadline = _generation_budget(self._inputs, deadline)
        try:
            generated = await self._backend.invoke(
                MIND_MAP_GENERATE_NOTE_DEF,
                await self._inputs.mind_map(value, deadline=deadline),
                deadline=deadline,
            )
            tree_json = generated.tree_json
            if tree_json is None:
                # An absent leaf is a semantically empty generation, not a
                # failure: nothing is persisted and the caller sees no note.
                return MindMapGenerateResult()
            tree = _derive_tree(tree_json)
            note = await self._notes.create_note_record(
                value.notebook_id,
                title=_derive_title(tree),
                content=tree_json,
                deadline=deadline,
            )
        except BackendError as error:
            raise rebind_operation(error, workflow) from error.__cause__
        return MindMapGenerateResult(
            mind_map=tree,
            note_id=note.id or None,
            created_at=note.created_at,
        )

    async def list(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        return await self._catalog.list_records(notebook_id, "mind_map", deadline=deadline)

    async def get(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactRecord | None:
        record = await self._catalog.get_record(notebook_id, artifact_id, deadline=deadline)
        return record if record is not None and record.family == "mind_map" else None


__all__ = ["DataTableFamilyService", "NoteBackedMindMapFamilyService"]
