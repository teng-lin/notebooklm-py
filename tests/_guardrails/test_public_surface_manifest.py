"""Manifest / re-export-identity gates for the public shim modules and surface.

This is the *gate* half of the historical ``tests/unit/test_public_shims.py``
split (stages 2+3 of the test-guardrail consolidation): every check here is a
static surface contract — the documented public-import manifest, identity
re-export pins, frozen ``__all__`` ordering, removed-name guards, the
facade-delegates-via-reflection checks, and the auth first-party seam manifest.
The behavioural shim tests (functions that exercise runtime behaviour) stay in
``tests/unit/test_public_shims.py``.
"""

from __future__ import annotations

import ast
import enum
import importlib
import warnings
from pathlib import Path
from types import ModuleType

import pytest

from tests._baselines.registry import (
    BASELINES,
    UNGATED_PUBLIC_MODULES,
    Baseline,
    allowlist_extra_public_names,
    baseline_by_name,
)

pytestmark = pytest.mark.repo_lint

# ---------------------------------------------------------------------------
# Documented public import manifest (stability spec)
#
# This is the public import surface documented in the user-facing API docs.
# Keep this manifest explicit: if docs add a new supported import path, add it
# here in the same PR; if docs intentionally remove one, remove it here with
# the docs change.
# ---------------------------------------------------------------------------


_DOCUMENTED_PUBLIC_IMPORTS = {
    "notebooklm": [
        "ArtifactType",
        "AudioFormat",
        "AudioLength",
        "AuthTokens",
        "ChatGoal",
        "ChatResponseLength",
        "ConnectionLimits",
        "correlation_id",
        "ExportType",
        "NonIdempotentRetryError",
        "NotebookLMClient",
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


@pytest.mark.parametrize(
    ("module_name", "public_name"),
    [
        pytest.param(module_name, public_name, id=f"{module_name}:{public_name}")
        for module_name, public_names in _DOCUMENTED_PUBLIC_IMPORTS.items()
        for public_name in public_names
    ],
)
def test_documented_public_import_manifest_resolves(
    module_name: str,
    public_name: str,
) -> None:
    """Every documented public import must remain importable."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        module = __import__(module_name, fromlist=[public_name])

    sentinel = object()
    assert getattr(module, public_name, sentinel) is not sentinel


def test_public_import_manifest_has_no_duplicates() -> None:
    """The manifest should stay reviewable and deterministic."""
    for module_name, public_names in _DOCUMENTED_PUBLIC_IMPORTS.items():
        assert public_names == sorted(public_names, key=str.lower), (
            f"{module_name} manifest entries must be sorted case-insensitively"
        )
        assert len(public_names) == len(set(public_names)), (
            f"{module_name} manifest contains duplicate entries"
        )


def test_public_facade_imports_are_identity_reexports() -> None:
    """Compatibility facades must keep returning the canonical public objects."""
    import notebooklm
    import notebooklm._auth.tokens as private_tokens
    import notebooklm.auth as public_auth
    import notebooklm.rpc as public_rpc
    import notebooklm.rpc.overrides as rpc_overrides
    import notebooklm.rpc.types as rpc_types
    import notebooklm.types as public_types

    assert notebooklm.AuthTokens is public_auth.AuthTokens
    assert public_auth.AuthTokens is private_tokens.AuthTokens
    assert notebooklm.ConnectionLimits is public_types.ConnectionLimits
    assert public_rpc.RPCMethod is rpc_types.RPCMethod
    assert public_rpc.resolve_rpc_id is rpc_overrides.resolve_rpc_id


# The names de-blessed from ``notebooklm.rpc.__all__`` in #1589. They were
# removed from ``__all__`` (so the compat gate no longer advertises them) but
# remain importable as module attributes for back-compat — see
# ``scripts/api-compat-allowlist.json`` and ``docs/deprecations.md``.
_RPC_LEGACY_REEXPORTS = [
    # batchexecute endpoint URL constants + helpers
    "BATCHEXECUTE_URL",
    "QUERY_URL",
    "UPLOAD_URL",
    "get_batchexecute_url",
    "get_query_url",
    "get_upload_url",
    # artifact-variant constants
    "FLASHCARDS_VARIANT",
    "QUIZ_VARIANT",
    "INTERACTIVE_MIND_MAP_VARIANT",
    # artifact type-code + status helpers
    "ArtifactTypeCode",
    "ArtifactStatus",
    "artifact_status_to_str",
    # enum re-exports (also public via notebooklm / notebooklm.types)
    "AudioFormat",
    "AudioLength",
    "VideoFormat",
    "VideoStyle",
    "QuizQuantity",
    "QuizDifficulty",
    "InfographicOrientation",
    "InfographicDetail",
    "InfographicStyle",
    "SlideDeckFormat",
    "SlideDeckLength",
    "ReportFormat",
    "ChatGoal",
    "ChatResponseLength",
    "DriveMimeType",
    "ExportType",
    # batchexecute wire helpers
    "encode_rpc_request",
    "build_request_body",
    "nest_source_ids",
    "strip_anti_xssi",
    "parse_chunked_response",
    "extract_rpc_result",
    "collect_rpc_ids",
    "decode_response",
    "safe_index",
    # exception re-exports (also public via notebooklm / notebooklm.exceptions)
    "RPCError",
    "AuthError",
    "NetworkError",
    "RPCTimeoutError",
    "RateLimitError",
    "ServerError",
    "ClientError",
    "UnknownRPCMethodError",
    # error-code utilities
    "RPCErrorCode",
    "get_error_message_for_code",
]


def test_rpc_all_is_minimized_to_documented_power_user_imports() -> None:
    """``notebooklm.rpc.__all__`` stays frozen to the two blessed imports (#1589).

    Catches a name being re-blessed into ``__all__`` directly (the compat audit
    only catches this indirectly, via a now-stale allowlist entry).
    """
    import notebooklm.rpc as public_rpc

    assert public_rpc.__all__ == ["RPCMethod", "resolve_rpc_id"]


def test_rpc_legacy_reexports_stay_importable_but_unblessed() -> None:
    """The de-blessed RPC names remain importable as attributes (back-compat),
    while staying out of ``__all__``. Freezes the promise made in #1589 that this
    is a de-advertisement, not a removal — no existing gate covers importability.
    """
    import notebooklm.rpc as public_rpc

    assert len(_RPC_LEGACY_REEXPORTS) == 47
    assert len(_RPC_LEGACY_REEXPORTS) == len(set(_RPC_LEGACY_REEXPORTS)), (
        "_RPC_LEGACY_REEXPORTS must not contain duplicate names (a dup could mask a drop)"
    )
    missing = [name for name in _RPC_LEGACY_REEXPORTS if not hasattr(public_rpc, name)]
    assert missing == [], f"de-blessed names must stay importable from notebooklm.rpc: {missing}"
    re_blessed = [name for name in _RPC_LEGACY_REEXPORTS if name in public_rpc.__all__]
    assert re_blessed == [], f"de-blessed names must not return to __all__: {re_blessed}"


# ---------------------------------------------------------------------------
# notebooklm.research public surface
# ---------------------------------------------------------------------------


def test_research_module_exposes_documented_helpers():
    """notebooklm.research re-exports the three free helpers used by the CLI."""
    from notebooklm.research import (
        extract_report_urls,
        normalize_url,
        select_cited_sources,
    )

    assert callable(extract_report_urls)
    assert callable(normalize_url)
    assert callable(select_cited_sources)


def test_cited_source_selection_is_on_public_surface():
    """CitedSourceSelection lives in notebooklm.types and on the top-level package."""
    from notebooklm import CitedSourceSelection as TopLevel
    from notebooklm.types import CitedSourceSelection

    assert TopLevel is CitedSourceSelection


# ---------------------------------------------------------------------------
# RPC enums re-exported via notebooklm.types
#
# CLI modules import these enums from ``notebooklm.types`` (the public surface)
# rather than reaching into ``notebooklm.rpc`` directly. The re-exports must be
# the exact same objects as the canonical definitions in ``notebooklm.rpc.types``
# (identity, not just equality), so isinstance checks and equality both work
# regardless of which import path callers use.
#
# The explicit list below covers every public RPC enum re-exported by
# ``notebooklm.types`` (see ``notebooklm.types.__all__``). Keep this list in
# sync with the re-exports so any accidental shadowing in ``types.py`` —
# redefining instead of re-exporting — is caught immediately. ``ArtifactTypeCode``
# is intentionally excluded because it is imported by ``types.py`` for internal
# use but not part of the public ``__all__``.
# ---------------------------------------------------------------------------


_REEXPORTED_RPC_ENUMS = [
    "ArtifactStatus",
    "AudioFormat",
    "AudioLength",
    "ChatGoal",
    "ChatResponseLength",
    "DriveMimeType",
    "ExportType",
    "InfographicDetail",
    "InfographicOrientation",
    "InfographicStyle",
    "QuizDifficulty",
    "QuizQuantity",
    "ReportFormat",
    "ShareAccess",
    "SharePermission",
    "ShareViewLevel",
    "SlideDeckFormat",
    "SlideDeckLength",
    "SourceStatus",
    "VideoFormat",
    "VideoStyle",
]

# NOTE: the former hand-typed ``_FROZEN_TYPES_ALL`` snapshot of
# ``notebooklm.types.__all__`` is gone — it is now the regenerable ``types_all``
# baseline (``tests/fixtures/baselines/types_all.json``, derived by the
# ``types_all`` :class:`~tests._baselines.registry.Baseline`). The freeze test is
# ``test_baseline_matches_committed_file[types_all]`` plus the per-name
# ``hasattr`` check in ``test_types_all_contract_is_frozen_in_order`` below.

_TOP_LEVEL_TYPE_EXPORTS = [
    "AccountLimits",
    "Artifact",
    "ArtifactType",
    "AskResult",
    "AudioFormat",
    "AudioLength",
    "ChatGoal",
    "ChatMode",
    "ChatReference",
    "ChatResponseLength",
    "CitedSourceSelection",
    "ClientMetricsSnapshot",
    "ConnectionLimits",
    "ConversationTurn",
    "DriveMimeType",
    "ExportType",
    "GenerationState",
    "GenerationStatus",
    "InfographicDetail",
    "InfographicOrientation",
    "InfographicStyle",
    "Label",
    "MindMapResult",
    "Note",
    "Notebook",
    "NotebookDescription",
    "NotebookMetadata",
    "QuizDifficulty",
    "QuizQuantity",
    "ReportFormat",
    "ReportSuggestion",
    "ResearchSource",
    "ResearchStart",
    "ResearchStatus",
    "ResearchTask",
    "RpcTelemetryEvent",
    "ShareAccess",
    "SharedUser",
    "SharePermission",
    "ShareStatus",
    "ShareViewLevel",
    "SlideDeckFormat",
    "SlideDeckLength",
    "Source",
    "SourceFulltext",
    "SourceGuide",
    "SourceStatus",
    "SourceSummary",
    "SourceType",
    "SuggestedTopic",
    "UnknownTypeWarning",
    "VideoFormat",
    "VideoStyle",
]

_TYPES_EXCEPTION_REEXPORTS = [
    "SourceError",
    "SourceAddError",
    "SourceProcessingError",
    "SourceTimeoutError",
    "SourceNotFoundError",
    "ArtifactError",
    "ArtifactFeatureUnavailableError",
    "ArtifactNotFoundError",
    "ArtifactNotReadyError",
    "ArtifactParseError",
    "ArtifactDownloadError",
    "ArtifactTimeoutError",
    "ArtifactPendingTimeoutError",
    "ArtifactInProgressTimeoutError",
    "LabelError",
    "LabelNotFoundError",
]

_TOP_LEVEL_EXCEPTION_EXPORTS = [
    "AmbiguousResearchTaskError",
    "ArtifactDownloadError",
    "ArtifactError",
    "ArtifactFeatureUnavailableError",
    "ArtifactInProgressTimeoutError",
    "ArtifactNotFoundError",
    "ArtifactNotReadyError",
    "ArtifactParseError",
    "ArtifactPendingTimeoutError",
    "ArtifactTimeoutError",
    "AuthError",
    "AuthExtractionError",
    "ChatError",
    "ChatResponseParseError",
    "ClientError",
    "ConfigurationError",
    "DecodingError",
    "LabelError",
    "LabelNotFoundError",
    "MindMapError",
    "MindMapNotFoundError",
    "NetworkError",
    "NonIdempotentRetryError",
    "NotFoundError",
    "NoteError",
    "NoteNotFoundError",
    "NotebookError",
    "NotebookLimitError",
    "NotebookLMError",
    "NotebookNotFoundError",
    "RateLimitError",
    "ResearchError",
    "ResearchTaskMismatchError",
    "ResearchTimeoutError",
    "RPCError",
    "RPCResponseTooLargeError",
    "RPCTimeoutError",
    "ServerError",
    "SourceAddError",
    "SourceError",
    "SourceNotFoundError",
    "SourceProcessingError",
    "SourceTimeoutError",
    "UnknownRPCMethodError",
    "ValidationError",
    "WaitTimeoutError",
]

_TYPES_PRIVATE_HELPER_SEAMS = [
    "_SOURCE_TYPE_COMPAT_MAP",
    "_datetime_from_timestamp",
    "_extract_artifact_url",
    "_extract_audio_artifact_url",
    "_extract_infographic_artifact_url",
    "_extract_slide_deck_artifact_url",
    "_extract_source_url",
    "_extract_video_artifact_url",
    "_is_valid_artifact_url",
    "_warned_artifact_types",
    "_warned_source_types",
]

# Private helpers that are no longer imported by first-party code but
# must remain exportable through ``notebooklm.types`` for downstream
# compatibility. ``_extract_source_created_at`` moved here when the
# row-adapter migration (see ``_row_adapters.sources.SourceRow.created_at``)
# replaced its sole first-party consumer
# (``_source.listing._parse_source``).
_TYPES_PRIVATE_EXTERNAL_COMPAT_SEAMS: list[str] = [
    "_extract_source_created_at",
]

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _iter_types_private_helper_import_files() -> list[Path]:
    """Return first-party Python files that may import private notebooklm.types seams."""
    roots = (
        _PROJECT_ROOT / "src" / "notebooklm",
        _PROJECT_ROOT / "tests" / "unit",
    )
    paths: list[Path] = []
    for root in roots:
        assert root.exists(), f"tracked private seam scan root disappeared: {root}"
        paths.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(paths)


def _may_reference_private_types_seam(text: str) -> bool:
    """Cheap prefilter before AST parsing the private ``notebooklm.types`` audit."""
    return "types" in text and "_" in text


@pytest.mark.parametrize("enum_name", _REEXPORTED_RPC_ENUMS)
def test_rpc_enum_reexports_are_identical(enum_name: str) -> None:
    """notebooklm.types.<Enum> is the same object as notebooklm.rpc.types.<Enum>."""
    import notebooklm.rpc.types as rpc_types
    import notebooklm.types as public_types

    public_enum = getattr(public_types, enum_name)
    canonical_enum = getattr(rpc_types, enum_name)
    assert public_enum is canonical_enum, (
        f"notebooklm.types.{enum_name} must be the same object as "
        f"notebooklm.rpc.types.{enum_name} (identity, not equality)"
    )


def test_types_all_contract_is_frozen_in_order() -> None:
    """T13 type moves must preserve the exact public ``types.__all__`` ordering.

    The frozen ordering itself is the regenerable ``types_all`` baseline
    (asserted by ``test_baseline_matches_committed_file[types_all]``). This test
    keeps the per-name ``hasattr`` intent: every name in the committed order must
    resolve on ``notebooklm.types``.
    """
    import notebooklm.types as public_types

    committed = baseline_by_name("types_all").load()
    assert list(public_types.__all__) == committed
    for name in committed:
        assert hasattr(public_types, name), f"notebooklm.types.__all__ misses {name!r}"


@pytest.mark.parametrize("name", _TOP_LEVEL_TYPE_EXPORTS)
def test_top_level_type_exports_are_identity_reexports(name: str) -> None:
    """Top-level type exports must remain identical to notebooklm.types objects."""
    import notebooklm
    import notebooklm.types as public_types

    assert name in notebooklm.__all__, f"notebooklm.__all__ dropped {name!r}"
    assert getattr(notebooklm, name) is getattr(public_types, name)


@pytest.mark.parametrize("name", _TYPES_EXCEPTION_REEXPORTS)
def test_types_exception_reexports_are_canonical_identities(name: str) -> None:
    """notebooklm.types exception compatibility aliases point at exceptions.py."""
    import notebooklm.exceptions as canonical
    import notebooklm.types as public_types

    assert getattr(public_types, name) is getattr(canonical, name)


@pytest.mark.parametrize("name", _TOP_LEVEL_EXCEPTION_EXPORTS)
def test_top_level_exception_reexports_are_canonical_identities(name: str) -> None:
    """Top-level exception exports point directly at exceptions.py canonical classes."""
    import notebooklm
    import notebooklm.exceptions as canonical

    assert name in notebooklm.__all__, f"notebooklm.__all__ dropped {name!r}"
    assert getattr(notebooklm, name) is getattr(canonical, name)


def test_top_level_exception_identity_manifest_matches_public_exception_exports() -> None:
    """Every public top-level exception export must be covered by identity checks."""
    import notebooklm
    import notebooklm.exceptions as canonical

    public_exception_exports = {
        name
        for name in notebooklm.__all__
        if name in canonical.__all__
        and isinstance(getattr(canonical, name), type)
        and issubclass(getattr(canonical, name), BaseException)
    }

    assert set(_TOP_LEVEL_EXCEPTION_EXPORTS) == public_exception_exports


def test_rpc_helper_reexports_are_canonical_identities() -> None:
    """Status helper re-exports must stay identical to rpc.types helpers."""
    import notebooklm.rpc.types as rpc_types
    import notebooklm.types as public_types

    assert public_types.artifact_status_to_str is rpc_types.artifact_status_to_str
    assert public_types.source_status_to_str is rpc_types.source_status_to_str


def test_types_non_all_facade_attributes_are_frozen() -> None:
    """Freeze compatibility attributes that exist outside notebooklm.types.__all__."""
    import notebooklm.rpc.types as rpc_types
    import notebooklm.types as public_types

    assert "ArtifactTypeCode" not in public_types.__all__
    assert public_types.ArtifactTypeCode is rpc_types.ArtifactTypeCode
    assert "StudioContentType" not in public_types.__all__
    assert not hasattr(public_types, "StudioContentType")
    assert "RPCMethod" not in public_types.__all__
    assert not hasattr(public_types, "RPCMethod")


@pytest.mark.parametrize("name", _TYPES_PRIVATE_HELPER_SEAMS + _TYPES_PRIVATE_EXTERNAL_COMPAT_SEAMS)
def test_types_private_helper_seams_remain_importable(name: str) -> None:
    """Private imports from notebooklm.types stay live during T13 moves."""
    import notebooklm.types as public_types

    imported = getattr(__import__("notebooklm.types", fromlist=[name]), name)
    assert imported is getattr(public_types, name)
    assert name not in public_types.__all__


def test_types_private_helper_seam_manifest_matches_first_party_imports() -> None:
    """The private seam manifest tracks known first-party notebooklm.types imports."""

    def attribute_path(node: ast.AST) -> list[str]:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return list(reversed(parts))
        return []

    imported_private_names: set[str] = set()
    for path in _iter_types_private_helper_import_files():
        text = path.read_text(encoding="utf-8")
        if not _may_reference_private_types_seam(text):
            continue
        tree = ast.parse(text)
        type_module_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "notebooklm.types" and alias.asname:
                        type_module_aliases.add(alias.asname)
                continue
            if isinstance(node, ast.ImportFrom) and (
                node.module == "notebooklm.types"
                or (
                    node.level > 0
                    and node.module == "types"
                    and path.is_relative_to(_PROJECT_ROOT / "src" / "notebooklm")
                )
            ):
                imported_private_names.update(
                    alias.name
                    for alias in node.names
                    if alias.name.startswith("_") and not alias.name.startswith("__")
                )
                continue
            if isinstance(node, ast.ImportFrom) and (
                (node.module == "notebooklm" and any(alias.name == "types" for alias in node.names))
                or (
                    node.level > 0
                    and node.module is None
                    and path.is_relative_to(_PROJECT_ROOT / "src" / "notebooklm")
                )
            ):
                type_module_aliases.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "types"
                )
                continue
            if (
                isinstance(node, ast.Attribute)
                and node.attr.startswith("_")
                and not node.attr.startswith("__")
            ):
                qualifier = attribute_path(node.value)
                if qualifier == ["notebooklm", "types"] or (
                    len(qualifier) == 1 and qualifier[0] in type_module_aliases
                ):
                    imported_private_names.add(node.attr)

    assert imported_private_names == set(_TYPES_PRIVATE_HELPER_SEAMS)


def test_top_level_studio_content_type_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """from notebooklm import StudioContentType is removed in v0.5.0."""
    import notebooklm

    monkeypatch.delitem(notebooklm.__dict__, "StudioContentType", raising=False)
    assert "StudioContentType" not in notebooklm.__all__
    with pytest.raises(AttributeError):
        _ = notebooklm.StudioContentType  # type: ignore[attr-defined]


def test_top_level_default_storage_path_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """from notebooklm import DEFAULT_STORAGE_PATH is removed in v0.5.0."""
    import notebooklm

    monkeypatch.delitem(notebooklm.__dict__, "DEFAULT_STORAGE_PATH", raising=False)
    with pytest.raises(AttributeError):
        _ = notebooklm.DEFAULT_STORAGE_PATH  # type: ignore[attr-defined]
    assert "DEFAULT_STORAGE_PATH" not in notebooklm.__all__


def test_rpc_enum_reexport_list_matches_public_all() -> None:
    """The _REEXPORTED_RPC_ENUMS guard list must stay aligned with notebooklm.types.__all__.

    If a new enum is re-exported in ``types.py``'s ``__all__`` but not added
    here, this test fails — preventing silent gaps in the identity coverage.
    """
    import notebooklm.rpc.types as rpc_types
    import notebooklm.types as public_types

    declared = set(public_types.__all__)
    rpc_names = {name for name in dir(rpc_types) if not name.startswith("_")}
    expected = {
        name
        for name in declared & rpc_names
        if isinstance(getattr(rpc_types, name), type)
        and issubclass(getattr(rpc_types, name), enum.Enum)
    }

    listed = set(_REEXPORTED_RPC_ENUMS)
    missing = expected - listed
    extras = listed - expected
    assert not missing, (
        f"_REEXPORTED_RPC_ENUMS is missing newly re-exported enum(s): {sorted(missing)}"
    )
    assert not extras, (
        f"_REEXPORTED_RPC_ENUMS contains name(s) no longer re-exported: {sorted(extras)}"
    )


# ---------------------------------------------------------------------------
# notebooklm.config / notebooklm.urls / notebooklm.log public shims
# ---------------------------------------------------------------------------


def test_config_shim_exposes_documented_names(monkeypatch):
    # Guard against a NOTEBOOKLM_BASE_URL override leaking from the env,
    # so the assertion stays valid on developer machines and overridden CI.
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)
    from notebooklm import config

    assert config.get_base_url() == config.DEFAULT_BASE_URL
    assert config.DEFAULT_BASE_URL == "https://notebooklm.google.com"


def test_urls_shim_exposes_documented_names():
    from notebooklm.urls import is_youtube_url

    assert is_youtube_url("https://www.youtube.com/watch?v=x") is True


def test_log_shim_exposes_install_redaction():
    from notebooklm.log import install_redaction

    assert callable(install_redaction)


# ---------------------------------------------------------------------------
# API contract: public raw-RPC and documented facade imports
# ---------------------------------------------------------------------------


def test_rpc_method_uses_documented_power_user_import_path() -> None:
    """Raw-RPC examples use notebooklm.rpc.RPCMethod, not notebooklm.types."""
    from notebooklm.rpc import RPCMethod
    from notebooklm.rpc.types import RPCMethod as CanonicalRPCMethod

    assert RPCMethod is CanonicalRPCMethod


def test_rpc_method_is_not_reexported_from_notebooklm_types() -> None:
    """RPCMethod is intentionally not part of notebooklm.types in this phase."""
    import notebooklm.types as public_types

    assert "RPCMethod" not in public_types.__all__
    assert not hasattr(public_types, "RPCMethod")


def test_auth_cookie_domain_constants_are_facade_exports() -> None:
    """Cookie-domain tiers remain importable from notebooklm.auth."""
    from notebooklm.auth import (
        OPTIONAL_COOKIE_DOMAINS,
        OPTIONAL_COOKIE_DOMAINS_BY_LABEL,
        REQUIRED_COOKIE_DOMAINS,
    )

    assert isinstance(REQUIRED_COOKIE_DOMAINS, frozenset)
    assert isinstance(OPTIONAL_COOKIE_DOMAINS, frozenset)
    assert isinstance(OPTIONAL_COOKIE_DOMAINS_BY_LABEL, dict)
    assert frozenset().union(*OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values()) == OPTIONAL_COOKIE_DOMAINS


# ---------------------------------------------------------------------------
# notebooklm.auth first-party compatibility surface
#
# This is narrower than a future public API decision. It only freezes the names
# that current first-party modules, CLI code, tests, and docs may rely on while
# auth internals continue to live underneath ``notebooklm._auth``.
# Removing one of these names from ``notebooklm.auth`` requires a separate
# deprecation/migration plan, not an internal-module move PR.
#
# Underscored entries are compatibility-only for non-CLI first-party callers;
# the CLI boundary test still forbids CLI modules from importing private names
# out of ``notebooklm.auth``. Other auth names, such as ``flatten_cookie_map``,
# are intentionally outside this enforced move-safety manifest unless added by
# a separate public or first-party compatibility decision.
# ---------------------------------------------------------------------------


# NOTE: 22 of the 23 names de-blessed from ``auth.__all__`` in PR-1 (#1592) were
# removed from this manifest as a deliberate change (the docstring above sanctions
# removal via a dedicated plan); the 23rd, ``recover_psidts_in_memory``, was never
# in this list. First-party code now imports the de-blessed names from
# ``notebooklm._auth.<sub>``; they stay importable from ``notebooklm.auth`` for
# back-compat, guarded by ``test_auth_deblessed_names_stay_importable_but_unblessed``
# in ``tests/_guardrails/test_public_surface.py``.
_AUTH_FIRST_PARTY_COMPATIBILITY_NAMES = [
    "_auth_domain_priority",
    "_EXTRACTION_HINT",
    "_find_cookie_for_storage",
    "_has_valid_secondary_binding",
    "_is_allowed_auth_domain",
    "_is_allowed_cookie_domain",
    "_is_google_domain",
    "_rotate_cookies",
    "_run_refresh_cmd",
    "_SECONDARY_BINDING_WARNED",
    "_split_refresh_cmd",
    "_update_cookie_input",
    "_validate_required_cookies",
    "Account",
    "AuthTokens",
    "build_cookie_jar",
    "build_httpx_cookies_from_storage",
    "clear_account_metadata",
    "convert_rookiepy_cookies_to_storage_state",
    "enumerate_accounts",
    "extract_cookies_from_storage",
    "extract_cookies_with_domains",
    "extract_email_from_html",
    "fetch_tokens_with_domains",
    "get_account_email_for_storage",
    "get_authuser_for_storage",
    "GOOGLE_REGIONAL_CCTLDS",
    "OPTIONAL_COOKIE_DOMAINS",
    "OPTIONAL_COOKIE_DOMAINS_BY_LABEL",
    "read_account_metadata",
    "REQUIRED_COOKIE_DOMAINS",
    "write_account_metadata",
]


@pytest.mark.parametrize("name", _AUTH_FIRST_PARTY_COMPATIBILITY_NAMES)
def test_auth_first_party_compatibility_manifest_resolves(name: str) -> None:
    """Internal layout may move, but first-party callers keep notebooklm.auth."""
    import notebooklm.auth as auth

    assert hasattr(auth, name), f"notebooklm.auth.{name} disappeared"


def test_auth_first_party_compatibility_manifest_has_no_duplicates() -> None:
    """The enforced compatibility manifest should stay reviewable."""
    assert len(_AUTH_FIRST_PARTY_COMPATIBILITY_NAMES) == len(
        set(_AUTH_FIRST_PARTY_COMPATIBILITY_NAMES)
    )


def test_auth_cookie_policy_facade_delegates_to_private_module() -> None:
    """Policy constants/helpers live in _auth while notebooklm.auth stays compatible."""
    import notebooklm.auth as auth
    from notebooklm._auth import cookie_policy

    assert auth.REQUIRED_COOKIE_DOMAINS is cookie_policy.REQUIRED_COOKIE_DOMAINS
    assert auth.OPTIONAL_COOKIE_DOMAINS is cookie_policy.OPTIONAL_COOKIE_DOMAINS
    assert auth.OPTIONAL_COOKIE_DOMAINS_BY_LABEL is cookie_policy.OPTIONAL_COOKIE_DOMAINS_BY_LABEL
    assert auth.ALLOWED_COOKIE_DOMAINS is cookie_policy.ALLOWED_COOKIE_DOMAINS
    assert auth.GOOGLE_REGIONAL_CCTLDS is cookie_policy.GOOGLE_REGIONAL_CCTLDS
    assert auth.MINIMUM_REQUIRED_COOKIES is cookie_policy.MINIMUM_REQUIRED_COOKIES
    assert auth._auth_domain_priority is cookie_policy._auth_domain_priority
    assert auth._is_google_domain is cookie_policy._is_google_domain
    assert auth._is_allowed_auth_domain is cookie_policy._is_allowed_auth_domain
    assert auth._is_allowed_cookie_domain is cookie_policy._is_allowed_cookie_domain


def test_auth_cookie_conversion_facade_delegates_to_private_module() -> None:
    """Cookie conversion/jar helpers live in _auth while auth.py stays compatible."""
    import notebooklm.auth as auth
    from notebooklm._auth import cookies

    assert auth.normalize_cookie_map is cookies.normalize_cookie_map
    assert auth.flatten_cookie_map is cookies.flatten_cookie_map
    assert auth.convert_rookiepy_cookies_to_storage_state is (
        cookies.convert_rookiepy_cookies_to_storage_state
    )
    assert auth.extract_cookies_from_storage is cookies.extract_cookies_from_storage
    assert auth.extract_cookies_with_domains is cookies.extract_cookies_with_domains
    assert auth.load_httpx_cookies is cookies.load_httpx_cookies
    assert auth.build_httpx_cookies_from_storage is cookies.build_httpx_cookies_from_storage
    assert auth.build_cookie_jar is cookies.build_cookie_jar
    assert auth._cookie_is_http_only is cookies._cookie_is_http_only
    assert auth._cookie_map_from_jar is cookies._cookie_map_from_jar
    assert auth._cookie_to_storage_state is cookies._cookie_to_storage_state
    assert auth._load_storage_state is cookies._load_storage_state
    assert auth._storage_entry_to_cookie is cookies._storage_entry_to_cookie
    assert auth._cookie_key_variants is cookies._cookie_key_variants
    assert auth._find_cookie_for_storage is cookies._find_cookie_for_storage
    assert auth._replace_cookie_jar is cookies._replace_cookie_jar


def test_auth_paths_facade_delegates_to_private_module() -> None:
    """Env-var names + rotation-lock-path live in ``_auth.paths`` but stay
    reachable through ``notebooklm.auth`` for public + white-box callers."""
    import notebooklm.auth as auth
    from notebooklm._auth import paths

    # Env-var names de-blessed from notebooklm.auth.__all__ in #1592; kept
    # importable via the facade.
    assert auth.NOTEBOOKLM_REFRESH_CMD_ENV == paths.NOTEBOOKLM_REFRESH_CMD_ENV
    assert auth.NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV == paths.NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV
    assert auth.NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV == paths.NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV
    # White-box affordances.
    assert auth._REFRESH_ATTEMPTED_ENV == paths._REFRESH_ATTEMPTED_ENV
    assert auth._rotation_lock_path is paths._rotation_lock_path


def test_auth_extraction_facade_delegates_to_private_module() -> None:
    """WIZ field token extraction lives in ``_auth.extraction`` but stays
    reachable through ``notebooklm.auth`` (public surface + white-box)."""
    import notebooklm.auth as auth
    from notebooklm._auth import extraction

    # WIZ extractors de-blessed from notebooklm.auth.__all__ in #1592; kept
    # importable via the facade.
    assert auth.extract_csrf_from_html is extraction.extract_csrf_from_html
    assert auth.extract_session_id_from_html is extraction.extract_session_id_from_html
    assert auth.extract_wiz_field is extraction.extract_wiz_field
    # White-box affordances.
    assert auth._safe_url is extraction._safe_url
    assert auth._build_wiz_field_patterns is extraction._build_wiz_field_patterns


def test_auth_extract_email_from_html_still_routed_via_account_module() -> None:
    """Sanity check: ``extract_email_from_html`` was NOT moved by PR-B-low.

    It already lives in ``_auth.account`` (the post-tier-9 baseline), routed
    through ``_AUTH_ACCOUNT_FACADE_NAMES``. PR-B-low must not duplicate it
    into ``_auth.extraction``.
    """
    import notebooklm.auth as auth
    from notebooklm._auth import account, extraction

    assert auth.extract_email_from_html is account.extract_email_from_html
    assert not hasattr(extraction, "extract_email_from_html")


def test_auth_headers_facade_delegates_to_private_module() -> None:
    """``_resolve_token_route_kwargs`` lives in ``_auth.headers`` but stays
    reachable through ``notebooklm.auth`` for internal callers and tests."""
    import notebooklm.auth as auth
    from notebooklm._auth import headers

    assert auth._resolve_token_route_kwargs is headers._resolve_token_route_kwargs


def test_auth_subpackage_init_wires_new_seam_modules() -> None:
    """The ``_auth`` package re-exports the new seam modules so that
    ``from notebooklm._auth import extraction`` style imports keep working."""
    from notebooklm import _auth

    assert hasattr(_auth, "paths")
    assert hasattr(_auth, "extraction")
    assert hasattr(_auth, "headers")
    # Tier-10 PR-B-high additions:
    assert hasattr(_auth, "keepalive")
    assert hasattr(_auth, "refresh")


def test_auth_validation_is_identity_re_export() -> None:
    """ADR-0014 + Wave 4 T2.2: ``auth._validate_required_cookies`` is now a
    direct re-export of ``_auth.cookie_policy._validate_required_cookies``.

    Round-2 reviewer finding (codex/momus): the prior write-through that
    copy-forwarded ``MINIMUM_REQUIRED_COOKIES`` / ``_EXTRACTION_HINT`` /
    ``_has_valid_secondary_binding`` from ``auth.py`` into ``_cookie_policy``
    before delegation was a behaviour-change risk. Wave 4 T2.2 inverts the
    dependency: tests that need to rebind policy must patch
    ``_auth.cookie_policy.X`` directly. Identity is the contract that
    survives.
    """
    from notebooklm import auth
    from notebooklm._auth import cookie_policy

    assert auth._validate_required_cookies is cookie_policy._validate_required_cookies


def test_auth_keepalive_state_dicts_share_identity_with_seam() -> None:
    """``tests/conftest.py`` clears ``_LAST_POKE_ATTEMPT_MONOTONIC`` and
    ``_POKE_LOCKS_BY_LOOP`` on ``notebooklm.auth``. The dicts MUST be the same
    objects in the keepalive seam so mutations through the facade flow into
    the moved bodies that consume the dicts.
    """
    import notebooklm.auth as auth
    from notebooklm._auth import keepalive

    assert auth._LAST_POKE_ATTEMPT_MONOTONIC is keepalive._LAST_POKE_ATTEMPT_MONOTONIC
    assert auth._POKE_LOCKS_BY_LOOP is keepalive._POKE_LOCKS_BY_LOOP


def test_auth_subprocess_reexport_lets_tests_patch_run() -> None:
    """White-box tests patch ``auth_mod.subprocess.run`` to intercept the
    refresh-cmd subprocess. The re-exported ``subprocess`` module must be the
    standard library module shared with ``_auth.refresh``.
    """
    import subprocess

    import notebooklm.auth as auth
    from notebooklm._auth import refresh

    assert auth.subprocess is subprocess
    assert refresh.subprocess is subprocess


def test_auth_update_cookie_input_lives_in_cookies_module() -> None:
    """``_update_cookie_input`` was moved into ``_auth.cookies`` (cohesive
    with ``flatten_cookie_map`` which it consumes); the public facade keeps
    re-exporting it for the ``_AUTH_FIRST_PARTY_COMPATIBILITY_NAMES`` manifest.
    """
    import notebooklm.auth as auth
    from notebooklm._auth import cookies

    assert auth._update_cookie_input is cookies._update_cookie_input


# ---------------------------------------------------------------------------
# Tier-10 PR-A re-export identity pins for ``notebooklm._core`` were deleted
# in Phase 4 (v0.5.0) when the ``_core.py`` compatibility shim was removed.
# The seam split into ``_runtime.config``, ``_error_injection``, and
# ``_runtime.helpers`` is now the canonical surface — tests import directly
# from those modules (see ``tests/conftest.py``, ``tests/unit/test_vcr_config.py``,
# ``tests/unit/test_runtime_lifecycle.py``).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# D1 PR-2 retired ``_AuthFacadeModule`` and the four ``_AUTH_*_FACADE_NAMES``
# mirror tables (ADR-0003 → Superseded). The patch-and-execute tests that
# pinned the facade-mirror semantics are gone with the mechanism; the
# identity / re-export tests above still apply and stay.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Every discovered public top-level module must declare ``__all__``.
#
# ``scripts/audit_public_api_compat.py`` captures ``has_all`` per module but
# (historically) never asserted it, and the shim-pair sweep below only covered a
# hardcoded trio. A brand-new public top-level module could therefore ship
# without ``__all__`` and slip past both gates (issue #1493). This sweep mirrors
# the audit's module-discovery rule — every non-underscore top-level ``*.py``
# (minus the excluded entrypoints) plus the public ``rpc`` subpackage and the
# top-level ``notebooklm`` package — and requires each to declare ``__all__``.
# ---------------------------------------------------------------------------

# Mirrors scripts/audit_public_api_compat.py: EXCLUDED_TOP_LEVEL_MODULES /
# EXTRA_PUBLIC_PACKAGES. Keep in sync with the audit's discovery so the two
# gates agree on the public top-level surface.
_AUDIT_EXCLUDED_TOP_LEVEL_MODULES = {"__main__", "notebooklm_cli"}
_AUDIT_EXTRA_PUBLIC_PACKAGES = ("rpc",)
_NOTEBOOKLM_PACKAGE_DIR = Path(__file__).resolve().parents[2] / "src" / "notebooklm"


def _discover_public_top_level_modules() -> list[str]:
    """Discover the public top-level ``notebooklm`` modules the audit baselines.

    Identical discovery to ``scripts/audit_public_api_compat.py``'s
    ``discover_modules``: the ``notebooklm`` package itself, every
    non-underscore top-level ``*.py`` (minus the excluded entrypoints), and each
    public subpackage in ``EXTRA_PUBLIC_PACKAGES`` that ships an ``__init__``.
    """
    modules = {"notebooklm"}
    for path in _NOTEBOOKLM_PACKAGE_DIR.glob("*.py"):
        stem = path.stem
        if stem.startswith("_") or stem in _AUDIT_EXCLUDED_TOP_LEVEL_MODULES:
            continue
        modules.add(f"notebooklm.{stem}")
    for name in _AUDIT_EXTRA_PUBLIC_PACKAGES:
        if (_NOTEBOOKLM_PACKAGE_DIR / name / "__init__.py").is_file():
            modules.add(f"notebooklm.{name}")
    return sorted(modules)


_PUBLIC_TOP_LEVEL_MODULES = _discover_public_top_level_modules()


def test_public_top_level_module_discovery_is_non_trivial() -> None:
    """Guard the discovery itself: it must find the known anchor modules.

    A discovery bug that returned an empty/degenerate set would make the
    per-module ``__all__`` sweep vacuously pass. Pin a few stable anchors so a
    regression in ``_discover_public_top_level_modules`` is caught loudly.
    """
    found = set(_PUBLIC_TOP_LEVEL_MODULES)
    for anchor in ("notebooklm", "notebooklm.types", "notebooklm.client", "notebooklm.rpc"):
        assert anchor in found, f"discovery dropped the public anchor module {anchor!r}"
    # The excluded entrypoints must never be treated as public surface.
    assert "notebooklm.__main__" not in found
    assert "notebooklm.notebooklm_cli" not in found


def test_discovery_constants_match_the_audit_source() -> None:
    """The mirrored discovery constants must EQUAL the audit's, self-checked.

    ``_AUDIT_EXCLUDED_TOP_LEVEL_MODULES`` / ``_AUDIT_EXTRA_PUBLIC_PACKAGES`` are
    hand-copied from ``scripts/audit_public_api_compat.py`` so this gate and the
    audit agree on the public surface. Assert equality against the source of
    truth rather than relying on a "keep in sync" comment — an un-enforced copy
    is exactly the consistency-drift failure shape this gate exists to close
    (#1493 review).
    """
    import scripts.audit_public_api_compat as audit

    assert set(audit.EXCLUDED_TOP_LEVEL_MODULES) == _AUDIT_EXCLUDED_TOP_LEVEL_MODULES
    assert tuple(_AUDIT_EXTRA_PUBLIC_PACKAGES) == tuple(audit.EXTRA_PUBLIC_PACKAGES)


def test_all_enforcement_flags_a_module_without_all() -> None:
    """Probe: the ``__all__`` requirement catches a module lacking ``__all__``.

    A synthetic public-shaped module with no ``__all__`` would have evaded both
    gates before issue #1493. This pins that the enforcement predicate
    (``hasattr(module, "__all__")``) actually distinguishes the two cases, so
    the per-module sweep above is not vacuous.
    """
    without_all = ModuleType("notebooklm._probe_without_all")
    with_all = ModuleType("notebooklm._probe_with_all")
    with_all.__all__ = ["x"]  # type: ignore[attr-defined]
    with_all.x = 1  # type: ignore[attr-defined]

    assert not hasattr(without_all, "__all__")
    assert hasattr(with_all, "__all__")


@pytest.mark.parametrize("module_name", _PUBLIC_TOP_LEVEL_MODULES)
def test_public_top_level_module_declares_all(module_name: str) -> None:
    """Every public top-level module must declare ``__all__``.

    This is the assertion behind the audit's captured ``has_all`` flag: a public
    module without ``__all__`` ships an un-baselined surface. ``__all__`` must be
    a list/tuple of ``str`` so the audit and ``import *`` consumers see a
    well-formed export manifest.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        module = importlib.import_module(module_name)

    assert hasattr(module, "__all__"), (
        f"{module_name} must declare __all__ — every public top-level module "
        "defines its exported surface so the compat audit can baseline it "
        "(scripts/audit_public_api_compat.py)."
    )
    all_value = module.__all__
    assert isinstance(all_value, (list, tuple)), (
        f"{module_name}.__all__ must be a list/tuple, got {type(all_value).__name__}"
    )
    assert all(isinstance(name, str) for name in all_value), (
        f"{module_name}.__all__ must contain only str names"
    )
    assert len(all_value) == len(set(all_value)), (
        f"{module_name}.__all__ contains duplicate entries"
    )
    for name in all_value:
        assert hasattr(module, name), f"{module_name}.__all__ references missing attribute {name!r}"


# ---------------------------------------------------------------------------
# Additions gate (#1592 follow-on): freeze the FULL collected surface of every
# public module the audit discovers but that has no exact pin yet.
#
# The compat audit (scripts/audit_public_api_compat.py::compare_manifests) only
# flags REMOVED/CHANGED exports vs the last release tag — it never walks
# current-only names, so a name ADDED to a public ``__all__`` (or a brand-new
# public module) is invisible and the surface can silently regrow. This snapshot
# makes every such addition a deliberate, diff-visible, in-PR act.
#
# The four modules already exact-pinned elsewhere keep their own gates (NO dedup:
# ``EXPECTED_AUTH_ALL`` is also load-bearing for the auth snapshot test, and the
# no-cross-test-import guard forbids importing those lists into this module):
#   notebooklm.auth   -> EXPECTED_AUTH_ALL                       (test_public_surface.py)
#   notebooklm.client -> EXPECTED_CLIENT_ALL                     (test_public_surface.py)
#   notebooklm.rpc    -> test_rpc_all_is_minimized_to_documented_power_user_imports
#   notebooklm.types  -> ``types_all`` regenerable baseline (tests/_baselines)
#
# The ordered collected surface of each ungated module is now the regenerable
# ``ungated_surface`` baseline (``tests/fixtures/baselines/ungated_surface.json``).
# ``collect_public_surface`` (the derive helper) and the module set
# (``UNGATED_PUBLIC_MODULES``) both live in ``tests._baselines.registry`` so the
# gate and the regen path derive identically. The freeze test is
# ``test_baseline_matches_committed_file[ungated_surface]``.
# ---------------------------------------------------------------------------

_EXACT_PINNED_ELSEWHERE = {
    "notebooklm.auth",
    "notebooklm.client",
    "notebooklm.rpc",
    "notebooklm.types",
}


def test_ungated_public_surface_covers_exactly_the_unpinned_modules() -> None:
    """Completeness: every audit-discovered public module is addition-gated —
    either exact-pinned elsewhere (auth/client/rpc/types) or frozen in the
    ``ungated_surface`` baseline. This fails a BRAND-NEW public module that
    declares ``__all__`` (which the ``declares_all`` test alone would let pass)
    until it is added to ``UNGATED_PUBLIC_MODULES`` and the baseline regenerated.
    """
    committed_modules = set(baseline_by_name("ungated_surface").load())
    discovered = set(_PUBLIC_TOP_LEVEL_MODULES)
    assert discovered == committed_modules | _EXACT_PINNED_ELSEWHERE, (
        "A public top-level module is neither exact-pinned elsewhere nor frozen in "
        "the ungated_surface baseline. Add a new public module to "
        "tests._baselines.registry.UNGATED_PUBLIC_MODULES (or to an existing exact "
        "pin) and regenerate (`python scripts/regen_baselines.py`) so its additions "
        "are gated.\n"
        f"  discovered-not-gated: {sorted(discovered - committed_modules - _EXACT_PINNED_ELSEWHERE)}\n"
        f"  baselined-not-discovered: {sorted(committed_modules - discovered)}"
    )

    # The registry's regen seed and the committed baseline keys must agree, so the
    # parametrized freeze (keyed off the committed file) can't silently skip a
    # module that ``UNGATED_PUBLIC_MODULES`` intends to gate.
    assert committed_modules == set(UNGATED_PUBLIC_MODULES), (
        "ungated_surface baseline keys drifted from UNGATED_PUBLIC_MODULES; "
        "regenerate the baseline (`python scripts/regen_baselines.py`)."
    )

    # The 4 exact-pinned modules pin ``__all__`` ONLY; assert no allowlist extra
    # targets them — an extra would be a *collected* export their ``__all__``-pin
    # misses and this gate excludes (a latent bypass). If one ever does, add it to
    # ``UNGATED_PUBLIC_MODULES`` so its collected surface is baselined too.
    pinned_with_extras = _EXACT_PINNED_ELSEWHERE & set(allowlist_extra_public_names())
    assert not pinned_with_extras, (
        f"allowlist extra_public_names target exact-__all__-pinned modules "
        f"{sorted(pinned_with_extras)} whose pins don't cover extras; add them to "
        "UNGATED_PUBLIC_MODULES so their collected surface is baselined."
    )


# ---------------------------------------------------------------------------
# Regenerable-baseline freeze (ADR-0022).
#
# Every registered :class:`~tests._baselines.registry.Baseline` (``types_all``,
# ``ungated_surface``, ``cli_contract``) is frozen here: the committed JSON file
# must equal ``derive()``. A name added to (or removed from) a public surface
# fails until the committed file is regenerated in the SAME PR — that diff line
# is the deliberate, reviewed acknowledgement. Regenerate after an intended
# change with::
#
#     python scripts/regen_baselines.py
#
# CI never passes ``--update-baselines``; it only ever diffs (the dev-only-regen
# invariant). See ADR-0022.
# ---------------------------------------------------------------------------


def test_baseline_registry_is_non_trivial() -> None:
    """Guard the registry itself: the known baselines must be registered.

    A regression that emptied ``BASELINES`` would make the parametrized freeze
    below vacuously pass. Pin the stable names so that is caught loudly.
    """
    names = {baseline.name for baseline in BASELINES}
    assert {"types_all", "ungated_surface", "cli_contract"} <= names, names
    # Names are unique (parametrize ids + lookup rely on it).
    assert len(names) == len(BASELINES)


@pytest.mark.parametrize("baseline", BASELINES, ids=lambda b: b.name)
def test_baseline_matches_committed_file(baseline: Baseline, update_baselines: bool) -> None:
    """The committed baseline JSON must equal ``derive()`` (CI-mode assertion).

    With ``--update-baselines`` (dev only — see ``tests/conftest.py``), the
    ``update_baselines`` fixture is ``True`` and the test instead REWRITES the
    committed file from ``derive()`` and passes. CI must never set the flag.
    """
    if update_baselines:
        baseline.write()
        return

    assert baseline.path.is_file(), (
        f"committed baseline {baseline.path} is missing — regenerate with "
        "`python scripts/regen_baselines.py`"
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        derived = baseline.derive()
    committed = baseline.load()
    assert derived == committed, (
        f"{baseline.name} baseline ({baseline.path.name}) is stale. If the change "
        "is intentional, regenerate it in this PR (`python scripts/regen_baselines.py`) "
        "— that diff is the deliberate acknowledgement."
    )
    # The committed bytes must be exactly what ``write()`` would emit, so a
    # regen is a no-op on a fresh checkout (idempotency) and hand-edits that
    # happen to parse-equal but differ in formatting are caught.
    assert baseline.dump(committed) == baseline.path.read_text(encoding="utf-8"), (
        f"{baseline.name} baseline is not in canonical serialized form; "
        "regenerate it (`python scripts/regen_baselines.py`)."
    )


# ---------------------------------------------------------------------------
# __all__ contract tests for the public shim modules.
#
# Enforces, for each shim, that:
#   1. ``__all__`` exists.
#   2. Every name in ``__all__`` resolves via ``getattr``.
#   3. No name in ``__all__`` is private (leading underscore).
#   4. ``__all__`` is sorted case-insensitively (drift catcher).
#   5. ``__all__`` matches the actual re-exported public surface — no orphans,
#      no missing entries.
#   6. ``__all__`` contains no duplicate entries.
# ---------------------------------------------------------------------------


# (shim_module_name, internal_module_name)
# Note: notebooklm.research has targeted smoke tests in the research section
# of ``tests/unit/test_public_shims.py`` and is intentionally excluded from
# this generic contract sweep.
_SHIM_PAIRS = [
    ("notebooklm.config", "notebooklm._env"),
    ("notebooklm.urls", "notebooklm._url_utils"),
    ("notebooklm.log", "notebooklm._logging"),
]


def _actual_reexports(shim: ModuleType, internal: ModuleType) -> set[str]:
    """Return public names on ``shim`` that point at the same object on ``internal``.

    A name is considered "re-exported" when both modules expose an attribute
    of the same identity. This catches accidental shadowing (a shim defining
    its own value) as well as truly re-exported symbols.

    Note: names imported under ``typing.TYPE_CHECKING`` are not visible to
    ``dir()`` at runtime, so type-only re-exports won't be detected. None of
    the current shims use TYPE_CHECKING re-exports.
    """
    sentinel = object()
    names: set[str] = set()
    for name in dir(shim):
        if name.startswith("_"):
            continue
        shim_obj = getattr(shim, name, sentinel)
        internal_obj = getattr(internal, name, sentinel)
        if shim_obj is sentinel or internal_obj is sentinel:
            continue
        if shim_obj is internal_obj:
            names.add(name)
    return names


@pytest.mark.parametrize(
    ("shim_name", "internal_name"),
    _SHIM_PAIRS,
    ids=[shim for shim, _ in _SHIM_PAIRS],
)
def test_public_shim_all_contract(shim_name: str, internal_name: str) -> None:
    shim = importlib.import_module(shim_name)
    internal = importlib.import_module(internal_name)

    # 1. __all__ exists.
    assert hasattr(shim, "__all__"), f"{shim_name} is missing __all__"
    all_list = shim.__all__
    assert isinstance(all_list, list), (
        f"{shim_name}.__all__ must be a list, got {type(all_list).__name__}"
    )

    # 2. Every name in __all__ is importable.
    for name in all_list:
        assert hasattr(shim, name), f"{shim_name}.__all__ references missing attribute {name!r}"

    # 3. No private names in __all__.
    private = [n for n in all_list if n.startswith("_")]
    assert not private, f"{shim_name}.__all__ leaks private names: {private}"

    # 4. __all__ sorted case-insensitively (drift catcher).
    expected_order = sorted(all_list, key=str.lower)
    assert list(all_list) == expected_order, (
        f"{shim_name}.__all__ is not sorted case-insensitively.\n"
        f"  actual:   {list(all_list)}\n"
        f"  expected: {expected_order}"
    )

    # 5. __all__ matches the actual public surface of the shim.
    declared = set(all_list)
    reexported = _actual_reexports(shim, internal)
    missing = reexported - declared
    orphans = declared - reexported
    assert not missing, (
        f"{shim_name}.__all__ is missing names re-exported from {internal_name}: {sorted(missing)}"
    )
    assert not orphans, (
        f"{shim_name}.__all__ contains orphans not re-exported from {internal_name}: "
        f"{sorted(orphans)}"
    )

    # 6. Length sanity: no duplicates in __all__.
    assert len(all_list) == len(declared), (
        f"{shim_name}.__all__ contains duplicates: {sorted(all_list)}"
    )
