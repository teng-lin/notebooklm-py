"""Tests for the public shim modules and the documented public-import surface."""

from __future__ import annotations

import ast
import enum
import importlib
import warnings
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

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


def test_research_select_cited_sources_returns_public_dataclass():
    """select_cited_sources returns the public CitedSourceSelection dataclass."""
    from notebooklm.research import select_cited_sources
    from notebooklm.types import CitedSourceSelection

    result = select_cited_sources([], "")
    assert isinstance(result, CitedSourceSelection)
    assert result.used_fallback is True


def test_research_api_backward_compat_classmethod_delegates():
    """notebooklm._research.ResearchAPI.select_cited_sources still works."""
    from notebooklm._research import ResearchAPI
    from notebooklm.types import CitedSourceSelection

    result = ResearchAPI.select_cited_sources([], "")
    assert isinstance(result, CitedSourceSelection)


def test_research_api_extract_report_urls_backward_compat_classmethod_delegates(
    monkeypatch: pytest.MonkeyPatch,
):
    """notebooklm._research.ResearchAPI.extract_report_urls still works."""
    import notebooklm.research as research_module
    from notebooklm._research import ResearchAPI

    report = "See [Example](https://Example.com/path/)."
    sentinel = {"delegated"}
    calls: list[str] = []

    def fake_extract_report_urls(value: str) -> set[str]:
        calls.append(value)
        return sentinel

    monkeypatch.setattr(research_module, "extract_report_urls", fake_extract_report_urls)

    assert ResearchAPI.extract_report_urls(report) is sentinel
    assert calls == [report]


def test_research_api_reexports_cited_source_selection_for_back_compat():
    """notebooklm._research.CitedSourceSelection continues to resolve."""
    from notebooklm._research import CitedSourceSelection as Legacy
    from notebooklm.types import CitedSourceSelection

    assert Legacy is CitedSourceSelection


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

_FROZEN_TYPES_ALL = [
    "CitedSourceSelection",
    "ConnectionLimits",
    "ClientMetricsSnapshot",
    "RpcTelemetryEvent",
    "Notebook",
    "NotebookDescription",
    "NotebookMetadata",
    "SuggestedTopic",
    "Source",
    "SourceFulltext",
    "SourceSummary",
    "Artifact",
    "GenerationStatus",
    "ReportSuggestion",
    "Note",
    "ConversationTurn",
    "ChatReference",
    "AskResult",
    "ChatMode",
    "SharedUser",
    "ShareStatus",
    # Research / mind-map / source-guide typed returns (issue #1209).
    "ResearchStatus",
    "ResearchSource",
    "ResearchTask",
    "ResearchStart",
    "MindMap",
    "MindMapKind",
    "MindMapResult",
    "SourceGuide",
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
    "UnknownTypeWarning",
    "SourceType",
    "ArtifactType",
    "ArtifactStatus",
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
    "SourceStatus",
    "ShareAccess",
    "ShareViewLevel",
    "SharePermission",
    "artifact_status_to_str",
    "source_status_to_str",
]

_TOP_LEVEL_TYPE_EXPORTS = [
    "AccountLimits",
    "AccountTier",
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
    "GenerationStatus",
    "InfographicDetail",
    "InfographicOrientation",
    "InfographicStyle",
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
]

_TOP_LEVEL_EXCEPTION_EXPORTS = [
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
    """T13 type moves must preserve the exact public types.__all__ ordering."""
    import notebooklm.types as public_types

    assert list(public_types.__all__) == _FROZEN_TYPES_ALL
    for name in _FROZEN_TYPES_ALL:
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


def test_types_private_state_seams_are_live_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warning-dedup sets and compat map must remain shared between canonical
    owners in ``_types/`` and their ``notebooklm.types`` re-exports."""
    import notebooklm.types as public_types
    from notebooklm._types import artifacts as _artifact_types_seam
    from notebooklm._types import sources as _source_types_seam
    from notebooklm.types import (
        _SOURCE_TYPE_COMPAT_MAP,
        Artifact,
        ArtifactType,
        Source,
        SourceType,
        UnknownTypeWarning,
    )

    assert _SOURCE_TYPE_COMPAT_MAP is public_types._SOURCE_TYPE_COMPAT_MAP
    source_warnings: set[int] = set()
    artifact_warnings: set[tuple[int | None, int | None]] = set()
    # ADR-0007 seam-aliased object-target form: patch the canonical owners in
    # ``_types/{sources,artifacts}`` directly. ``notebooklm.types._warned_*``
    # are re-exports of those canonical objects, so the test must target the
    # canonical home — patching the public facade would rebind only the
    # facade alias and not the module-global the parser reads.
    monkeypatch.setattr(_source_types_seam, "_warned_source_types", source_warnings)
    monkeypatch.setattr(_artifact_types_seam, "_warned_artifact_types", artifact_warnings)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UnknownTypeWarning)
        assert Source(id="source", _type_code=7654321).kind is SourceType.UNKNOWN
        assert (
            Artifact(id="artifact", title="Artifact", _artifact_type=7654322, status=3).kind
            is ArtifactType.UNKNOWN
        )

    assert 7654321 in source_warnings
    assert (7654322, None) in artifact_warnings


def test_facade_unknown_type_warning_filter_suppresses_parser_warnings() -> None:
    """Filters using notebooklm.types.UnknownTypeWarning still catch parser warnings."""
    import notebooklm.types as public_types

    public_types._warned_source_types.clear()
    public_types._warned_artifact_types.clear()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.filterwarnings("ignore", category=public_types.UnknownTypeWarning)

        assert (
            public_types.Source(id="source", _type_code=8765432).kind
            is public_types.SourceType.UNKNOWN
        )
        assert (
            public_types.Artifact(
                id="artifact",
                title="Artifact",
                _artifact_type=8765433,
                status=3,
            ).kind
            is public_types.ArtifactType.UNKNOWN
        )

    assert caught == []
    assert 8765432 in public_types._warned_source_types
    assert (8765433, None) in public_types._warned_artifact_types


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
    "advance_cookie_snapshot_after_save",
    "ALLOWED_COOKIE_DOMAINS",
    "authuser_query",
    "AuthTokens",
    "build_cookie_jar",
    "build_httpx_cookies_from_storage",
    "clear_account_metadata",
    "convert_rookiepy_cookies_to_storage_state",
    "CookieSaveResult",
    "CookieSnapshot",
    "CookieSnapshotKey",
    "CookieSnapshotValue",
    "enumerate_accounts",
    "extract_cookies_from_storage",
    "extract_cookies_with_domains",
    "extract_csrf_from_html",
    "extract_email_from_html",
    "extract_session_id_from_html",
    "extract_wiz_field",
    "fetch_tokens",
    "fetch_tokens_with_domains",
    "format_authuser_value",
    "get_account_email_for_storage",
    "get_authuser_for_storage",
    "GOOGLE_REGIONAL_CCTLDS",
    "KEEPALIVE_ROTATE_URL",
    "load_auth_from_storage",
    "load_httpx_cookies",
    "MINIMUM_REQUIRED_COOKIES",
    "normalize_cookie_map",
    "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV",
    "NOTEBOOKLM_REFRESH_CMD_ENV",
    "NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV",
    "OPTIONAL_COOKIE_DOMAINS",
    "OPTIONAL_COOKIE_DOMAINS_BY_LABEL",
    "read_account_metadata",
    "REQUIRED_COOKIE_DOMAINS",
    "save_cookies_to_storage",
    "snapshot_cookie_jar",
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

    # Public-surface env-var names (listed in notebooklm.auth.__all__).
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

    # Public-surface (listed in notebooklm.auth.__all__).
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


def test_auth_validation_uses_cookie_policy_rebindings_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation rebindings must target the canonical home in ``_auth.cookie_policy``.

    Wave 4 T2.2 of the session-decoupling plan removed the auth.py
    write-through that previously copy-forwarded facade-level rebinds into
    ``_cookie_policy``. Tests that want to rebind policy names patch the
    canonical module directly.
    """
    from notebooklm import auth
    from notebooklm._auth import cookie_policy

    monkeypatch.setattr(cookie_policy, "MINIMUM_REQUIRED_COOKIES", {"SID"})
    monkeypatch.setattr(cookie_policy, "_has_valid_secondary_binding", lambda names: True)

    # ``auth._validate_required_cookies`` is the same object as
    # ``cookie_policy._validate_required_cookies`` (see identity test
    # above), so calling either reaches the canonical implementation
    # which observes the rebind.
    auth._validate_required_cookies({"SID"})


def test_auth_validation_extraction_hint_lives_on_cookie_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validator's error message uses ``_cookie_policy._EXTRACTION_HINT``."""
    from notebooklm import auth
    from notebooklm._auth import cookie_policy

    monkeypatch.setattr(cookie_policy, "MINIMUM_REQUIRED_COOKIES", {"SID", "SIDTS"})
    monkeypatch.setattr(cookie_policy, "_EXTRACTION_HINT", "custom extraction hint")

    with pytest.raises(ValueError, match="custom extraction hint"):
        auth._validate_required_cookies({"SID"})


@pytest.mark.asyncio
async def test_client_rpc_call_forwards_supported_kwargs() -> None:
    """NotebookLMClient.rpc_call forwards its supported kwargs to the executor.

    After the v0.6.0 cut, the public wrapper exposes only the supported
    surface (``method``, ``params``, ``allow_null``, and the keyword-only
    ``disable_internal_retries``); the previously-deprecated
    ``source_path`` / ``_is_retry`` / ``operation_variant`` kwargs were
    removed and are no longer forwarded by this layer.
    """
    from _fixtures.fake_core import make_fake_core
    from notebooklm import NotebookLMClient
    from notebooklm.auth import AuthTokens
    from notebooklm.rpc import RPCMethod

    client = NotebookLMClient(
        AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="session",
        )
    )
    # ADR-0007 constructor injection: substitute the whole executor
    # collaborator with the seam fixture's fake instead of mutating
    # ``client._rpc_executor.rpc_call`` after the fact. The public
    # ``rpc_call`` wrapper reads ``self._rpc_executor``, so swapping the
    # executor exercises the same forwarding path.
    fake = make_fake_core(rpc_call=AsyncMock(return_value={"ok": True}))
    client._rpc_executor = fake.rpc_executor

    result = await client.rpc_call(
        RPCMethod.CREATE_NOTEBOOK,
        ["My Notebook"],
        allow_null=True,
        disable_internal_retries=True,
    )

    assert result == {"ok": True}
    fake.rpc_executor.rpc_call.assert_awaited_once_with(
        method=RPCMethod.CREATE_NOTEBOOK,
        params=["My Notebook"],
        allow_null=True,
        disable_internal_retries=True,
    )


@pytest.mark.asyncio
async def test_client_rpc_call_forwards_default_arguments() -> None:
    """The default-shape call forwards minimal kwargs and inherits executor defaults."""
    from _fixtures.fake_core import make_fake_core
    from notebooklm import NotebookLMClient
    from notebooklm.auth import AuthTokens
    from notebooklm.rpc import RPCMethod

    client = NotebookLMClient(
        AuthTokens(
            cookies={"SID": "test"},
            csrf_token="csrf",
            session_id="session",
        )
    )
    # No async context is needed: ADR-0007 constructor injection swaps the
    # whole executor collaborator for the seam fixture's fake before any
    # real transport initialization can be required.
    fake = make_fake_core(rpc_call=AsyncMock(return_value=[]))
    client._rpc_executor = fake.rpc_executor

    result = await client.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    assert result == []
    # The wrapper forwards only the kwargs it owns; the rest of
    # RpcExecutor.rpc_call's signature (source_path, _is_retry,
    # operation_variant) keeps its module-level defaults.
    fake.rpc_executor.rpc_call.assert_awaited_once_with(
        method=RPCMethod.LIST_NOTEBOOKS,
        params=[],
        allow_null=False,
        disable_internal_retries=False,
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
# Note: notebooklm.research has targeted smoke tests in the research section above
# and is intentionally excluded from this generic contract sweep.
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
# identity / re-export tests below still apply and stay.
# ---------------------------------------------------------------------------


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
