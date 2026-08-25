"""Transport-neutral records for the migrated Source domain."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, unique
from pathlib import Path

from ._operations import CallPolicy, Operation, OperationDef, OperationTier
from ._types.documents import StructuredDocument


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """Neutral source value returned by list/get backends."""

    id: str
    title: str | None = None
    url: str | None = None
    kind: str = "unknown"
    unrecognized_kind: int | str | None = None
    kind_present: bool = True
    created_at: datetime | None = None
    status: str = "unknown"
    drive_document_id: str | None = None
    drive_status: str | None = None
    download_url: str | None = None
    viewer_url: str | None = None
    content_mime: str | None = None
    word_count: int | None = None
    revision_id: str | None = None
    revision_timestamp: datetime | None = None
    last_modified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SourceListInput:
    """Validated semantic source-list request."""

    notebook_id: str
    strict: bool = False
    statuses: frozenset[str] | None = None
    kinds: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class SourceListResult:
    """Source listing in backend order after semantic filtering."""

    sources: tuple[SourceRecord, ...]


@dataclass(frozen=True, slots=True)
class SourceGetInput:
    """Notebook and source identities requested by source get."""

    notebook_id: str
    source_id: str


@dataclass(frozen=True, slots=True)
class SourceGetResult:
    """Source get result; ``None`` is the semantic not-found state."""

    source: SourceRecord | None


@dataclass(frozen=True, slots=True)
class SourceAddUrlInput:
    """One URL-source request, including the hidden YouTube variant."""

    notebook_id: str
    url: str
    wait: bool = False
    wait_timeout: float = 120.0
    requested_title: str | None = None
    finalize_source: SourceRecord | None = field(default=None, repr=False)


@unique
class SourceAddCommitState(str, Enum):
    """How confidently a URL-source write is attributed to this call."""

    CREATED = "created"
    RECONCILED = "reconciled"
    FAILED = "failed"
    UNKNOWN = "unknown"


@unique
class SourceAddTitleState(str, Enum):
    """Best-effort requested-title outcome after URL registration."""

    NOT_REQUESTED = "not_requested"
    UNCHANGED = "unchanged"
    RENAMED = "renamed"
    RENAME_FAILED = "rename_failed"
    NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True, slots=True)
class SourceAddUrlReceipt:
    """Safe internal evidence for commit and title uncertainty."""

    commit_state: SourceAddCommitState
    title_state: SourceAddTitleState
    outcome_unknown: bool = False


@dataclass(frozen=True, slots=True)
class SourceAddUrlResult:
    """Neutral URL-source result plus its reconciliation receipt."""

    source: SourceRecord
    receipt: SourceAddUrlReceipt


@unique
class SourceAddFailureKind(str, Enum):
    """Closed public failure vocabulary for URL-source compatibility replay."""

    SOURCE_ADD = "source_add"
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_PROCESSING = "source_processing"
    SOURCE_TIMEOUT = "source_timeout"
    AUTH = "auth"
    CHAT = "chat"
    CHAT_RESPONSE_PARSE = "chat_response_parse"
    CLIENT = "client"
    DECODING = "decoding"
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    RESPONSE_TOO_LARGE = "response_too_large"
    RPC = "rpc"
    RPC_TIMEOUT = "rpc_timeout"
    SERVER = "server"
    UNKNOWN_RPC_METHOD = "unknown_rpc_method"
    BUILTIN_CONNECTION = "builtin_connection"
    BUILTIN_BROKEN_PIPE = "builtin_broken_pipe"
    BUILTIN_CONNECTION_ABORTED = "builtin_connection_aborted"
    BUILTIN_CONNECTION_REFUSED = "builtin_connection_refused"
    BUILTIN_CONNECTION_RESET = "builtin_connection_reset"
    BUILTIN_OS = "builtin_os"
    BUILTIN_INDEX = "builtin_index"
    BUILTIN_KEY = "builtin_key"
    BUILTIN_RUNTIME = "builtin_runtime"
    BUILTIN_TIMEOUT = "builtin_timeout"
    BUILTIN_TYPE = "builtin_type"
    BUILTIN_VALUE = "builtin_value"
    HTTPX_STATUS = "httpx_status"
    HTTPX_REQUEST = "httpx_request"
    HTTPX_TRANSPORT = "httpx_transport"
    HTTPX_TIMEOUT = "httpx_timeout"
    HTTPX_CONNECT_TIMEOUT = "httpx_connect_timeout"
    HTTPX_READ_TIMEOUT = "httpx_read_timeout"
    HTTPX_WRITE_TIMEOUT = "httpx_write_timeout"
    HTTPX_POOL_TIMEOUT = "httpx_pool_timeout"
    HTTPX_NETWORK = "httpx_network"
    HTTPX_CONNECT = "httpx_connect"
    HTTPX_READ = "httpx_read"
    HTTPX_WRITE = "httpx_write"
    HTTPX_CLOSE = "httpx_close"
    HTTPX_PROXY = "httpx_proxy"
    HTTPX_PROTOCOL = "httpx_protocol"
    HTTPX_LOCAL_PROTOCOL = "httpx_local_protocol"
    HTTPX_REMOTE_PROTOCOL = "httpx_remote_protocol"
    HTTPX_UNSUPPORTED_PROTOCOL = "httpx_unsupported_protocol"
    HTTPX_TOO_MANY_REDIRECTS = "httpx_too_many_redirects"
    HTTPX_DECODING = "httpx_decoding"
    TRANSPORT_AUTH_EXPIRED = "transport_auth_expired"
    TRANSPORT_RATE_LIMITED = "transport_rate_limited"
    TRANSPORT_SERVER = "transport_server"


ScalarExceptionArg = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class SourceAddFailureRecord:
    """Serializable evidence needed to reconstruct one bounded public error graph."""

    kind: SourceAddFailureKind
    message: str
    args: tuple[ScalarExceptionArg, ...] = ()
    url: str | None = None
    unconfirmed: bool = False
    source_id: str | None = None
    stage: str | None = None
    method_id: str | int | None = None
    raw_response: str | None = None
    rpc_code: str | int | None = None
    found_ids: tuple[str | int, ...] = ()
    recoverable: bool | None = None
    retry_after: int | None = None
    status_code: int | None = None
    timeout_seconds: float | None = None
    limit_bytes: int | None = None
    bytes_read: int | None = None
    status: int | None = None
    timeout: float | None = None
    last_status: int | None = None
    path: tuple[int, ...] | None = None
    source: str | None = None
    data_at_failure: str | None = None
    request_method: str | None = None
    request_url: str | None = None
    original_error: SourceAddFailureRecord | None = None
    cause: SourceAddFailureRecord | None = None
    context: SourceAddFailureRecord | None = None
    cause_is_original: bool = False
    cause_original_is_original_error: bool = False
    context_is_cause: bool = False
    context_is_original: bool = False
    explicit_cause: bool = False
    suppress_context: bool = False


@dataclass(frozen=True, slots=True)
class SourceUrlBatchItemRecord:
    """One positional batch-URL outcome without public model dependencies."""

    url: str = field(repr=False)
    source: SourceRecord | None = None
    error: SourceAddFailureRecord | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (self.source is None) == (self.error is None):
            raise ValueError("exactly one of source or error must be set")


@dataclass(frozen=True, slots=True)
class SourceAddUrlBatchInput:
    """Validated URLs sent in one non-replayed batch mutation."""

    notebook_id: str
    urls: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class SourceAddUrlBatchResult:
    """Positional batch URL outcomes in request order."""

    items: tuple[SourceUrlBatchItemRecord, ...]


@dataclass(frozen=True, slots=True)
class SourceAddTextInput:
    """Pasted-text source request."""

    notebook_id: str
    title: str = field(repr=False)
    content: str = field(repr=False)
    wait: bool = False
    wait_timeout: float = 120.0
    idempotent: bool = False


@dataclass(frozen=True, slots=True)
class SourceAddTextResult:
    """Created pasted-text source."""

    source: SourceRecord


@dataclass(frozen=True, slots=True)
class SourceAddDriveInput:
    """Native Google Drive source request."""

    notebook_id: str
    file_id: str = field(repr=False)
    title: str = field(repr=False)
    mime_type: str
    wait: bool = False
    wait_timeout: float = 120.0
    finalize_source: SourceRecord | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SourceAddDriveResult:
    """Created or exactly reconciled native Drive source."""

    source: SourceRecord


@unique
class SourceFileInputKind(str, Enum):
    """How bytes reach the existing file-upload pipeline."""

    LOCAL = "local"
    DRIVE_DOWNLOAD = "drive_download"


SourceProgressCallback = Callable[[int, int], object]


@dataclass(frozen=True, slots=True)
class SourceAddFileInput:
    """Local-file or Drive-download upload request."""

    notebook_id: str
    kind: SourceFileInputKind
    file_path: str | Path | None = field(default=None, repr=False)
    document_id: str | None = field(default=None, repr=False)
    mime_type: str | None = None
    title: str | None = field(default=None, repr=False)
    wait: bool = False
    wait_timeout: float = 120.0
    on_progress: SourceProgressCallback | None = field(default=None, repr=False, compare=False)
    finalize_source: SourceRecord | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SourceAddFileResult:
    """Uploaded file source."""

    source: SourceRecord
    transient_error_types: tuple[int | None, ...] | None = None
    deferred_title: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SourceFileRegistrationRecord:
    """Decoded file-registration response without retaining its wire envelope."""

    source_id: str | None
    response_shape: str


@dataclass(frozen=True, slots=True)
class SourceDeleteInput:
    notebook_id: str
    source_id: str


@dataclass(frozen=True, slots=True)
class SourceDeleteResult:
    """Successful idempotent source deletion."""


@dataclass(frozen=True, slots=True)
class SourceUpdateInput:
    notebook_id: str
    source_id: str
    new_title: str = field(repr=False)
    return_object: bool = True


@dataclass(frozen=True, slots=True)
class SourceUpdateResult:
    source: SourceRecord | None


@dataclass(frozen=True, slots=True)
class SourcePatchTitleInput:
    """One ``UPDATE_SOURCE`` title set-op (P9.2 primitive)."""

    notebook_id: str
    source_id: str
    new_title: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SourcePatchTitleResult:
    """Decoded mutation echo; ``None`` asks the workflow to hydrate by id."""

    source: SourceRecord | None


@dataclass(frozen=True, slots=True)
class SourceRefreshInput:
    notebook_id: str
    source_id: str


@dataclass(frozen=True, slots=True)
class SourceRefreshResult:
    """Successful source refresh."""


@dataclass(frozen=True, slots=True)
class SourceFreshnessInput:
    notebook_id: str
    source_id: str


@dataclass(frozen=True, slots=True)
class SourceFreshnessResult:
    fresh: bool


@dataclass(frozen=True, slots=True)
class SourceWaitSnapshotInput:
    """Request one source snapshot for facade-owned readiness polling."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class SourceWaitSnapshotResult:
    """One decoded notebook snapshot used by a single facade poll tick."""

    sources: tuple[SourceRecord, ...]


@dataclass(frozen=True, slots=True)
class SourceGuideInput:
    notebook_id: str
    source_id: str


@dataclass(frozen=True, slots=True)
class SourceGuideRecord:
    summary: str = field(default="", repr=False)
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceGuideResult:
    guide: SourceGuideRecord


@dataclass(frozen=True, slots=True)
class SourceFulltextInput:
    notebook_id: str
    source_id: str
    output_format: str = "text"


@dataclass(frozen=True, slots=True)
class SourceFulltextRecord:
    source_id: str
    title: str
    content: str = field(repr=False)
    kind: str = "unknown"
    unrecognized_kind: int | str | None = None
    kind_present: bool = True
    url: str | None = field(default=None, repr=False)
    char_count: int = 0
    document: StructuredDocument = field(default_factory=StructuredDocument, repr=False)


@dataclass(frozen=True, slots=True)
class SourceFulltextResult:
    fulltext: SourceFulltextRecord


SOURCE_PATCH_TITLE_DEF: OperationDef[SourcePatchTitleInput, SourcePatchTitleResult] = OperationDef(
    Operation.SOURCE_PATCH_TITLE,
    CallPolicy.MUTATION,
    SourcePatchTitleInput,
    SourcePatchTitleResult,
    tier=OperationTier.PRIMITIVE,
)
