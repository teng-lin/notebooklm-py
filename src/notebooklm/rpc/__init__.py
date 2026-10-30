"""RPC identifiers with lazy Web wire compatibility exports."""

from __future__ import annotations

from typing import Any

from .types import (  # noqa: F401
    ARTIFACT_STATUS_SUGGESTED_WIRE_NAME,
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
    GrpcStatusCode,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    QuizDifficulty,
    QuizQuantity,
    QuizQuestionType,
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
    normalize_grpc_status,
)

_LAZY_WEB_EXPORTS = {
    "AuthError": ("notebooklm._web.wire.decoder", "AuthError"),
    "ClientError": ("notebooklm._web.wire.decoder", "ClientError"),
    "NetworkError": ("notebooklm._web.wire.decoder", "NetworkError"),
    "RateLimitError": ("notebooklm._web.wire.decoder", "RateLimitError"),
    "RPCError": ("notebooklm._web.wire.decoder", "RPCError"),
    "RPCErrorCode": ("notebooklm._web.wire.decoder", "RPCErrorCode"),
    "RPCTimeoutError": ("notebooklm._web.wire.decoder", "RPCTimeoutError"),
    "ServerError": ("notebooklm._web.wire.decoder", "ServerError"),
    "UnknownRPCMethodError": ("notebooklm._web.wire.decoder", "UnknownRPCMethodError"),
    "collect_rpc_ids": ("notebooklm._web.wire.decoder", "collect_rpc_ids"),
    "decode_response": ("notebooklm._web.wire.decoder", "decode_response"),
    "extract_rpc_result": ("notebooklm._web.wire.decoder", "extract_rpc_result"),
    "get_error_message_for_code": (
        "notebooklm._web.wire.decoder",
        "get_error_message_for_code",
    ),
    "parse_chunked_response": ("notebooklm._web.wire.decoder", "parse_chunked_response"),
    "safe_index": ("notebooklm._web.wire.decoder", "safe_index"),
    "strip_anti_xssi": ("notebooklm._web.wire.decoder", "strip_anti_xssi"),
    "build_request_body": ("notebooklm._web.wire.encoder", "build_request_body"),
    "encode_rpc_request": ("notebooklm._web.wire.encoder", "encode_rpc_request"),
    "nest_source_ids": ("notebooklm._web.wire.encoder", "nest_source_ids"),
    "resolve_rpc_id": ("notebooklm._web.wire.overrides", "resolve_rpc_id"),
}


def __getattr__(name: str) -> Any:
    """Resolve historical Web wire names only when explicitly requested."""
    target = _LAZY_WEB_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_WEB_EXPORTS})


__all__ = ["RPCMethod", "resolve_rpc_id"]
