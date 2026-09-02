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
        "PremiumFeatureInfo",
        "QuizDifficulty",
        "QuizQuantity",
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
        "ConnectionLimits",
    ],
    "notebooklm.urls": [
        "is_google_auth_redirect",
        "is_youtube_url",
    ],
}
