"""Transport-neutral records and operation definitions for labels and collections."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique

from ._operations import CallPolicy, Operation, OperationDef, OperationTier


@unique
class LabelKind(str, Enum):
    """Closed discriminator over the one shared label/collection wire surface.

    A collection is a label with a distinct type discriminator and no notebook
    parent, so both facades share four RPC ids verbatim. Every neutral label
    value carries this discriminator explicitly instead of relying on a null
    ``notebook_id`` to imply it.
    """

    SOURCE_LABEL = "source_label"
    COLLECTION = "collection"


@dataclass(frozen=True, slots=True)
class LabelRecord:
    """Neutral member-grouping value shared by source labels and collections.

    ``member_ids`` is the group's membership in backend order: source ids for a
    :attr:`LabelKind.SOURCE_LABEL`, notebook ids for a
    :attr:`LabelKind.COLLECTION`. ``notebook_id`` is the notebook scope and is
    ``None`` for account-level collections.
    """

    id: str
    name: str
    kind: LabelKind
    notebook_id: str | None = None
    emoji: str | None = None
    member_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LabelListInput:
    """Kind-discriminated request for one label set."""

    kind: LabelKind
    notebook_id: str | None = None


@dataclass(frozen=True, slots=True)
class LabelListResult:
    """Label set in backend order."""

    labels: tuple[LabelRecord, ...]


@dataclass(frozen=True, slots=True)
class LabelGetInput:
    """Exact-id selection request within one label set."""

    kind: LabelKind
    label_id: str
    notebook_id: str | None = None


@dataclass(frozen=True, slots=True)
class LabelGetResult:
    """Label get result; ``None`` is the semantic not-found state."""

    label: LabelRecord | None


@dataclass(frozen=True, slots=True)
class LabelGenerateInput:
    """Auto-grouping request; only source labels have a generation mode.

    ``replace_existing`` is the destructive mode: every existing label is wiped
    and regenerated with new ids. The safe default groups only the sources that
    are not labelled yet.
    """

    notebook_id: str
    replace_existing: bool = False


@dataclass(frozen=True, slots=True)
class LabelGenerateResult:
    """Full post-generation label set echoed by the backend."""

    labels: tuple[LabelRecord, ...]


@dataclass(frozen=True, slots=True)
class LabelCreateInput:
    """Manual creation request for one empty, named group."""

    kind: LabelKind
    name: str
    notebook_id: str | None = None
    emoji: str = ""


@dataclass(frozen=True, slots=True)
class LabelCreateResult:
    """The single group this call is proven to have created."""

    label: LabelRecord


@dataclass(frozen=True, slots=True)
class LabelUpdateInput:
    """Field and/or membership mutation for one group.

    A field mutation (``name`` and/or ``emoji``) and a membership mutation
    (``add_member_ids`` / ``remove_member_ids``) are separate wire field masks
    with different reconciliation duties, so exactly one form is requested per
    call. ``return_object`` selects whether the caller wants the group read
    back; the not-found contract holds in both modes.
    """

    kind: LabelKind
    label_id: str
    notebook_id: str | None = None
    name: str | None = None
    emoji: str | None = None
    add_member_ids: tuple[str, ...] = ()
    remove_member_ids: tuple[str, ...] = ()
    return_object: bool = True


@dataclass(frozen=True, slots=True)
class LabelUpdateResult:
    """Group read back after its mutation, or ``None`` when not requested."""

    label: LabelRecord | None


@dataclass(frozen=True, slots=True)
class LabelMutateInput:
    """One ``UPDATE_LABEL`` set-op (P9.2 primitive).

    Exactly one form is requested per call: a field mask (``name`` and/or
    ``emoji``), one member append (``add_member_id``), or one member removal
    (``remove_member_id``). The wire honours only the first id of a set-op
    group per call, so the hoisted workflows issue one call per member.
    """

    kind: LabelKind
    label_id: str
    notebook_id: str | None = None
    name: str | None = None
    emoji: str | None = None
    add_member_id: str | None = None
    remove_member_id: str | None = None


@dataclass(frozen=True, slots=True)
class LabelMutateResult:
    """Successful set-op; ``le8sX`` echoes no group, so nothing is carried."""


@dataclass(frozen=True, slots=True)
class LabelAllocateInput:
    """Manual ``CREATE_LABEL`` allocation of one named group (P9.2 primitive)."""

    kind: LabelKind
    name: str
    notebook_id: str | None = None
    emoji: str = ""


@dataclass(frozen=True, slots=True)
class LabelAllocateResult:
    """The post-allocation set echo (source labels) or empty (collections).

    ``agX4Bc`` echoes the whole post-operation source-label set; the collection
    dialect has no captured echo, so its allocation result is empty and the
    workflow re-lists instead.
    """

    labels: tuple[LabelRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class LabelDeleteInput:
    """Batch deletion request; an absent id is an idempotent no-op."""

    kind: LabelKind
    label_ids: tuple[str, ...]
    notebook_id: str | None = None


@dataclass(frozen=True, slots=True)
class LabelDeleteResult:
    """Successful idempotent group deletion."""


# One neutral operation family, two discriminated public facades. Both keys of
# each pair share the input/output record types and the web codec beneath them;
# only ``LabelKind`` selects the wire dialect.
LABEL_LIST_DEF: OperationDef[LabelListInput, LabelListResult] = OperationDef(
    Operation.LABEL_LIST,
    CallPolicy.READ,
    LabelListInput,
    LabelListResult,
)
LABEL_GET_DEF: OperationDef[LabelGetInput, LabelGetResult] = OperationDef(
    Operation.LABEL_GET,
    CallPolicy.READ,
    LabelGetInput,
    LabelGetResult,
)
LABEL_GENERATE_DEF: OperationDef[LabelGenerateInput, LabelGenerateResult] = OperationDef(
    Operation.LABEL_GENERATE,
    CallPolicy.STATEFUL_START,
    LabelGenerateInput,
    LabelGenerateResult,
)
LABEL_CREATE_DEF: OperationDef[LabelCreateInput, LabelCreateResult] = OperationDef(
    Operation.LABEL_CREATE,
    CallPolicy.MUTATION,
    LabelCreateInput,
    LabelCreateResult,
)
LABEL_UPDATE_DEF: OperationDef[LabelUpdateInput, LabelUpdateResult] = OperationDef(
    Operation.LABEL_UPDATE,
    CallPolicy.MUTATION,
    LabelUpdateInput,
    LabelUpdateResult,
)
LABEL_MUTATE_DEF: OperationDef[LabelMutateInput, LabelMutateResult] = OperationDef(
    Operation.LABEL_MUTATE,
    CallPolicy.MUTATION,
    LabelMutateInput,
    LabelMutateResult,
    tier=OperationTier.PRIMITIVE,
)
LABEL_ALLOCATE_DEF: OperationDef[LabelAllocateInput, LabelAllocateResult] = OperationDef(
    Operation.LABEL_ALLOCATE,
    CallPolicy.MUTATION,
    LabelAllocateInput,
    LabelAllocateResult,
    tier=OperationTier.PRIMITIVE,
)
LABEL_DELETE_DEF: OperationDef[LabelDeleteInput, LabelDeleteResult] = OperationDef(
    Operation.LABEL_DELETE,
    CallPolicy.MUTATION,
    LabelDeleteInput,
    LabelDeleteResult,
)
COLLECTION_LIST_DEF: OperationDef[LabelListInput, LabelListResult] = OperationDef(
    Operation.COLLECTION_LIST,
    CallPolicy.READ,
    LabelListInput,
    LabelListResult,
)
COLLECTION_GET_DEF: OperationDef[LabelGetInput, LabelGetResult] = OperationDef(
    Operation.COLLECTION_GET,
    CallPolicy.READ,
    LabelGetInput,
    LabelGetResult,
)
COLLECTION_CREATE_DEF: OperationDef[LabelCreateInput, LabelCreateResult] = OperationDef(
    Operation.COLLECTION_CREATE,
    CallPolicy.MUTATION,
    LabelCreateInput,
    LabelCreateResult,
)
COLLECTION_UPDATE_DEF: OperationDef[LabelUpdateInput, LabelUpdateResult] = OperationDef(
    Operation.COLLECTION_UPDATE,
    CallPolicy.MUTATION,
    LabelUpdateInput,
    LabelUpdateResult,
)
COLLECTION_DELETE_DEF: OperationDef[LabelDeleteInput, LabelDeleteResult] = OperationDef(
    Operation.COLLECTION_DELETE,
    CallPolicy.MUTATION,
    LabelDeleteInput,
    LabelDeleteResult,
)


__all__ = [
    "COLLECTION_CREATE_DEF",
    "COLLECTION_DELETE_DEF",
    "COLLECTION_GET_DEF",
    "COLLECTION_LIST_DEF",
    "COLLECTION_UPDATE_DEF",
    "LABEL_CREATE_DEF",
    "LABEL_ALLOCATE_DEF",
    "LABEL_DELETE_DEF",
    "LABEL_GENERATE_DEF",
    "LABEL_GET_DEF",
    "LABEL_LIST_DEF",
    "LABEL_MUTATE_DEF",
    "LABEL_UPDATE_DEF",
    "LabelCreateInput",
    "LabelAllocateInput",
    "LabelAllocateResult",
    "LabelCreateResult",
    "LabelDeleteInput",
    "LabelDeleteResult",
    "LabelGenerateInput",
    "LabelGenerateResult",
    "LabelGetInput",
    "LabelGetResult",
    "LabelKind",
    "LabelListInput",
    "LabelListResult",
    "LabelMutateInput",
    "LabelMutateResult",
    "LabelRecord",
    "LabelUpdateInput",
    "LabelUpdateResult",
]
