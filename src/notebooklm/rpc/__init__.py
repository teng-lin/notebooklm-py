"""RPC protocol implementation for NotebookLM batchexecute API."""

# ``notebooklm.rpc.*`` is internal (see docs/stability.md). Only ``RPCMethod`` and
# ``resolve_rpc_id`` are blessed public power-user imports; they alone appear in
# ``__all__`` below.
#
# Every other imported name stays importable as a module attribute, because
# first-party code imports several through this facade (e.g. ``safe_index``) and
# external callers may already do ``from notebooklm.rpc import <name>``. But they
# are deliberately kept OUT of ``__all__`` so the public-API compat gate stops
# advertising them (#1589; one ``removed-export`` allowance each in
# scripts/api-compat-allowlist.json). The ``noqa: F401`` directives suppress the
# resulting "unused import" warnings on these re-export groups.
from .decoder import (  # noqa: F401
    AuthError,
    ClientError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCErrorCode,
    RPCTimeoutError,
    ServerError,
    UnknownRPCMethodError,
    collect_rpc_ids,
    decode_response,
    extract_rpc_result,
    get_error_message_for_code,
    parse_chunked_response,
    safe_index,
    strip_anti_xssi,
)
from .encoder import build_request_body, encode_rpc_request, nest_source_ids  # noqa: F401
from .overrides import resolve_rpc_id
from .types import (  # noqa: F401
    BATCHEXECUTE_URL,
    FLASHCARDS_VARIANT,
    INTERACTIVE_MIND_MAP_VARIANT,
    QUERY_URL,
    QUIZ_VARIANT,
    UPLOAD_URL,
    ArtifactStatus,
    ArtifactTypeCode,
    AudioFormat,
    AudioLength,
    ChatGoal,
    ChatResponseLength,
    DriveMimeType,
    ExportType,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    RPCMethod,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
    artifact_status_to_str,
    get_batchexecute_url,
    get_query_url,
    get_upload_url,
)

# Blessed public power-user surface only; see the banner comment above for why
# every other importable name is intentionally omitted.
__all__ = [
    "RPCMethod",
    "resolve_rpc_id",
]
