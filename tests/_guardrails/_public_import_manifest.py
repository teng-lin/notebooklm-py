"""Shared manifest of the documented public import surface (stability spec).

Lives in a ``_``-prefixed non-test module because two guardrail modules consume
it — ``test_public_surface_manifest.py`` (does every documented import still
resolve?) and ``test_public_surface.py`` (does ``notebooklm.auth.__all__`` still
equal what the docs promise?). ``tests/_guardrails/test_no_cross_test_imports.py``
forbids one ``test_*`` module importing another, so the shared symbol is hoisted
here rather than imported across (issue #1431/#1445).

This is the public import surface documented in the user-facing API docs. Keep
the manifest explicit: if docs add a new supported import path, add it here in
the same PR; if docs intentionally remove one, remove it here with the docs
change.
"""

from __future__ import annotations

_DOCUMENTED_PUBLIC_IMPORTS = {
    "notebooklm": [
        "ArtifactCreationCapability",
        "ArtifactListing",
        "ArtifactListingComponent",
        "ArtifactListingFailure",
        "ArtifactLookup",
        "ArtifactLookupStatus",
        "ArtifactType",
        "AudioFormat",
        "AudioLength",
        "AuthTokens",
        "ChatGoal",
        "ChatResponseLength",
        "ChatSession",
        "ChatSessionStatus",
        "ConnectionLimits",
        "correlation_id",
        "ExportType",
        "MagicArtifactType",
        "NextStepSuggestion",
        "NonIdempotentRetryError",
        "NotebookLMClient",
        "OperationTimeoutError",
        "PremiumFeatureInfo",
        "QuizDifficulty",
        "QuizQuantity",
        "QuizQuestionType",
        "ReportFormat",
        "RPCError",
        "SharePermission",
        "ShareViewLevel",
        "SourceType",
        "VideoFormat",
        "VideoStyle",
    ],
    "notebooklm.auth": [
        "AuthTokens",
        "convert_rookiepy_cookies_to_storage_state",
        "LockUnavailableError",
        "OPTIONAL_COOKIE_DOMAINS",
        "OPTIONAL_COOKIE_DOMAINS_BY_LABEL",
        "REQUIRED_COOKIE_DOMAINS",
    ],
    "notebooklm.config": [
        "DEFAULT_BASE_URL",
        "get_base_url",
    ],
    "notebooklm.log": [
        "install_redaction",
    ],
    "notebooklm.outcomes": [
        "BatchItemOutcome",
        "BatchOutcome",
        "CommitState",
        "LookupSuggestion",
        "OperationMetadata",
        "ReconciliationCandidate",
        "ReconciliationReport",
        "RecoveryAction",
        "SourceBatchItemOutcome",
    ],
    "notebooklm.options": [
        "AndroidBackendConfig",
        "AUTO",
        "AutoReadWindow",
        "ClientConfig",
        "FeatureOptions",
        "ReadWindow",
        "RetryOptions",
        "RpcEventCallback",
        "RuntimeOptions",
        "TimeoutOptions",
        "TransferOptions",
        "USE_DEFAULT",
        "UseDefault",
        "WebBackendConfig",
        "WebRequestOptions",
        "WebSessionHooks",
        "WebSessionOptions",
        "WebTransportOptions",
    ],
    "notebooklm.research": [
        "extract_report_urls",
        "normalize_url",
        "select_cited_sources",
    ],
    "notebooklm.rpc": [
        "resolve_rpc_id",
        "RPCMethod",
    ],
    "notebooklm.types": [
        "ArtifactCreationCapability",
        "ArtifactDownloadListing",
        "ArtifactDownloadRequest",
        "ArtifactDownloadSelection",
        "ArtifactListing",
        "ArtifactListingComponent",
        "ArtifactListingFailure",
        "ArtifactLookup",
        "ArtifactLookupStatus",
        "ConnectionLimits",
        "SourceDeleteOutcome",
    ],
    "notebooklm.urls": [
        "is_google_auth_redirect",
        "is_youtube_url",
    ],
    "notebooklm.downloads": [
        "DOWNLOAD_FORMAT_NAMES",
        "DOWNLOAD_REGISTRY",
        "DOWNLOAD_SPECS_BY_NAME",
        "DownloadFormatSpec",
        "DownloadRegistryEntry",
        "DownloadTypeSpec",
        "EXTENSION_MIME_TYPES",
        "FORMAT_EXTENSIONS",
        "resolve_download_format",
    ],
}
