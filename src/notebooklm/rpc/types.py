"""RPC wire types, constants, and compatibility re-exports."""

from enum import Enum  # noqa: F401 - historical wildcard re-export
from typing import Any as _Any
from typing import Final

from .._env import DEFAULT_BASE_URL, get_base_url
from .._types.enums import (  # noqa: F401 - compatibility re-exports
    _ARTIFACT_STATUS_MAP,
    _DISCOVERY_MODE_MAP,
    _DRIVE_SOURCE_STATUS_MAP,
    _SHARE_PERMISSION_MAP,
    _SOURCE_STATUS_MAP,
    ARTIFACT_STATUS_SUGGESTED_WIRE_NAME,
    FLASHCARDS_VARIANT,
    INTERACTIVE_MIND_MAP_VARIANT,
    QUIZ_VARIANT,
    SOURCE_STATUS_LABELS,
    ArtifactStatus,
    ArtifactTypeCode,
    AudioFormat,
    AudioLength,
    ChatGoal,
    ChatResponseLength,
    DiscoveryMode,
    DriveMimeType,
    DriveSourceStatus,
    ExportType,
    GrpcStatusCode,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    MagicArtifactType,
    QuizDifficulty,
    QuizQuantity,
    QuizQuestionType,
    ReportFormat,
    ShareAccess,
    SharePermission,
    ShareViewLevel,
    SlideDeckFormat,
    SlideDeckLength,
    SourceStatus,
    VideoFormat,
    VideoStyle,
    artifact_status_to_str,
    discovery_mode_to_str,
    drive_source_status_to_str,
    normalize_grpc_status,
    normalize_rpc_code,
    share_permission_to_str,
    source_status_to_str,
)
from ._identifiers import RPCMethod as RPCMethod

# These names were historically importable from this module before the wire
# implementation moved under ``_web``. Keep them as lazy identity exports so
# importing the compatibility module does not eagerly import Web wire policy.
_LEGACY_OVERRIDE_EXPORTS = frozenset(
    {
        "_load_rpc_overrides",
        "_logged_override_hashes",
        "_parse_rpc_overrides",
        "resolve_rpc_id",
    }
)


def __getattr__(name: str) -> _Any:
    """Resolve the moved override helpers only when legacy code asks for them."""
    if name in _LEGACY_OVERRIDE_EXPORTS:
        from .._web.wire import overrides

        value = getattr(overrides, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the legacy override names in interactive discovery."""
    return sorted({*globals(), *_LEGACY_OVERRIDE_EXPORTS})


# URL path for the streamed-chat endpoint. Not a batchexecute RPC ID — kept
# as a module-level constant rather than an ``RPCMethod`` member so the enum
# only contains real RPC IDs that ``scripts/check_rpc_health.py`` can probe.
_QUERY_ENDPOINT_PATH: Final[str] = (
    "/_/LabsTailwindUi/data/google.internal.labs.tailwind.orchestration.v1."
    "LabsTailwindOrchestrationService/GenerateFreeFormStreamed"
)

# Backward-compatible default-host endpoint constants. Runtime code should use
# the lazy get_* helpers below so NOTEBOOKLM_BASE_URL is honored after import.
BATCHEXECUTE_URL = f"{DEFAULT_BASE_URL}/_/LabsTailwindUi/data/batchexecute"
QUERY_URL = f"{DEFAULT_BASE_URL}{_QUERY_ENDPOINT_PATH}"
UPLOAD_URL = f"{DEFAULT_BASE_URL}/upload/_/"


def get_batchexecute_url() -> str:
    """Return the NotebookLM batchexecute endpoint for the configured host."""
    return f"{get_base_url()}/_/LabsTailwindUi/data/batchexecute"


def get_query_url() -> str:
    """Return the NotebookLM streamed chat endpoint for the configured host."""
    return f"{get_base_url()}{_QUERY_ENDPOINT_PATH}"


def get_upload_url() -> str:
    """Return the NotebookLM upload endpoint for the configured host."""
    return f"{get_base_url()}/upload/_/"


# Preserve the historical wildcard-import surface.  ``resolve_rpc_id`` is
# resolved lazily through ``__getattr__``; all other names mirror the public
# globals Python exported before this module gained an explicit ``__all__``.
# RPCMethod keeps its old end position even though its implementation import is
# now dependency ordered at module scope.
__all__ = [name for name in globals() if not name.startswith("_") and name != "RPCMethod"]
__all__.extend(["RPCMethod", "resolve_rpc_id"])
