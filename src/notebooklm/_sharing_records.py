"""Transport-neutral records and operation definitions for notebook Sharing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique

from ._operations import CallPolicy, Operation, OperationDef


@unique
class SharePermissionLevel(str, Enum):
    """Backend-neutral collaborator permission vocabulary."""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"
    REMOVE = "remove"


@unique
class ShareAccessLevel(str, Enum):
    """Backend-neutral notebook link visibility."""

    RESTRICTED = "restricted"
    ANYONE_WITH_LINK = "anyone_with_link"


@unique
class ShareViewScope(str, Enum):
    """Backend-neutral viewer scope."""

    FULL_NOTEBOOK = "full_notebook"
    CHAT_ONLY = "chat_only"


@dataclass(frozen=True, slots=True)
class SharedUserRecord:
    """Neutral collaborator row."""

    email: str
    permission: SharePermissionLevel
    display_name: str | None = None
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class ShareStatusRecord:
    """Neutral decoded sharing configuration."""

    notebook_id: str
    is_public: bool
    access: ShareAccessLevel
    view_level: ShareViewScope
    shared_users: tuple[SharedUserRecord, ...] = ()
    share_url: str | None = None
    max_individuals_share_limit: int | None = None
    is_public_sharing_allowed: bool | None = None


@dataclass(frozen=True, slots=True)
class SharingGetInput:
    """Notebook identity requested by the sharing-status read."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class SharingGetResult:
    """Current sharing configuration for one notebook."""

    status: ShareStatusRecord


@dataclass(frozen=True, slots=True)
class SharingSetPublicInput:
    """Requested public-link visibility for one notebook."""

    notebook_id: str
    public: bool


@dataclass(frozen=True, slots=True)
class SharingSetPublicResult:
    """Sharing configuration read back after the visibility mutation."""

    status: ShareStatusRecord


@dataclass(frozen=True, slots=True)
class SharingSetViewLevelInput:
    """Requested viewer scope for one notebook."""

    notebook_id: str
    view_level: ShareViewScope


@dataclass(frozen=True, slots=True)
class SharingSetViewLevelResult:
    """Sharing configuration read back after the viewer-scope mutation."""

    status: ShareStatusRecord


@dataclass(frozen=True, slots=True)
class SharingUserGrant:
    """One requested individual-user permission entry."""

    email: str
    permission: SharePermissionLevel


@dataclass(frozen=True, slots=True)
class SharingUpdateUsersInput:
    """One individual-user permission batch and its notification policy."""

    notebook_id: str
    grants: tuple[SharingUserGrant, ...]
    notify: bool = True
    welcome_message: str = ""


@dataclass(frozen=True, slots=True)
class SharingUpdateUsersResult:
    """Sharing configuration read back after the grant mutation."""

    status: ShareStatusRecord


@dataclass(frozen=True, slots=True)
class SharingMutateInput:
    """One ``SHARE_NOTEBOOK`` set-op (P9.2 primitive): visibility or grants.

    Exactly one form is requested per call: ``public`` sets link visibility,
    ``grants`` upserts/removes individual users (with ``notify`` and
    ``welcome_message`` as the grant options).
    """

    notebook_id: str
    public: bool | None = None
    grants: tuple[SharingUserGrant, ...] = ()
    notify: bool = True
    welcome_message: str = ""


@dataclass(frozen=True, slots=True)
class SharingMutateResult:
    """Successful share mutation; the workflow reads the status back itself."""


@dataclass(frozen=True, slots=True)
class LegacyShareArtifactInput:
    """Legacy notebook/artifact share-link state requested by compatibility internals."""

    notebook_id: str
    public: bool = True
    artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyShareArtifactResult:
    """Caller-owned share-link state echoed after the set-state mutation."""

    public: bool
    artifact_id: str | None = None


SHARING_MUTATE_DEF: OperationDef[SharingMutateInput, SharingMutateResult] = OperationDef(
    Operation.SHARING_MUTATE,
    CallPolicy.MUTATION,
    SharingMutateInput,
    SharingMutateResult,
)
SHARING_GET_DEF: OperationDef[SharingGetInput, SharingGetResult] = OperationDef(
    Operation.SHARING_GET,
    CallPolicy.READ,
    SharingGetInput,
    SharingGetResult,
)
SHARING_SET_PUBLIC_DEF: OperationDef[SharingSetPublicInput, SharingSetPublicResult] = OperationDef(
    Operation.SHARING_SET_PUBLIC,
    CallPolicy.MUTATION,
    SharingSetPublicInput,
    SharingSetPublicResult,
)
SHARING_SET_VIEW_LEVEL_DEF: OperationDef[SharingSetViewLevelInput, SharingSetViewLevelResult] = (
    OperationDef(
        Operation.SHARING_SET_VIEW_LEVEL,
        CallPolicy.MUTATION,
        SharingSetViewLevelInput,
        SharingSetViewLevelResult,
    )
)
SHARING_UPDATE_USERS_DEF: OperationDef[SharingUpdateUsersInput, SharingUpdateUsersResult] = (
    OperationDef(
        Operation.SHARING_UPDATE_USERS,
        CallPolicy.MUTATION,
        SharingUpdateUsersInput,
        SharingUpdateUsersResult,
    )
)
LEGACY_SHARE_ARTIFACT_DEF: OperationDef[LegacyShareArtifactInput, LegacyShareArtifactResult] = (
    OperationDef(
        Operation.LEGACY_SHARE_ARTIFACT,
        CallPolicy.MUTATION,
        LegacyShareArtifactInput,
        LegacyShareArtifactResult,
    )
)

__all__ = [
    "LEGACY_SHARE_ARTIFACT_DEF",
    "SHARING_GET_DEF",
    "SHARING_MUTATE_DEF",
    "SHARING_SET_PUBLIC_DEF",
    "SHARING_SET_VIEW_LEVEL_DEF",
    "SHARING_UPDATE_USERS_DEF",
    "LegacyShareArtifactInput",
    "LegacyShareArtifactResult",
    "ShareAccessLevel",
    "SharePermissionLevel",
    "ShareStatusRecord",
    "ShareViewScope",
    "SharedUserRecord",
    "SharingGetInput",
    "SharingGetResult",
    "SharingSetPublicInput",
    "SharingSetPublicResult",
    "SharingSetViewLevelInput",
    "SharingSetViewLevelResult",
    "SharingUpdateUsersInput",
    "SharingUpdateUsersResult",
    "SharingMutateInput",
    "SharingMutateResult",
    "SharingUserGrant",
]
