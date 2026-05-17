# Phase 11 T13.0 - Promotion Audit And Contract Freeze

Date: 2026-05-17
Branch: `architecture-remediation/t13-0-promotion-audit-contract-freeze`
Base: `origin/main` (`c5d1ce3`, after `#746` merged)
Status: open/blocked only on final handoff readiness

## Blockers

- `#745` / T12.8 and `#746` are merged into `origin/main`.
- `#750` is a documentation-only handoff PR on `main`. This T13.0 slice does not edit the same handoff file and does not block on it; re-check if later T13/T14 work touches that file or relies on the final docs contract.
- This checkout does not contain `.sisyphus/phases/architecture-remediation/state.json`, so the required `jq` and `rg` state refresh commands cannot complete on this base. Treat the missing state file as a recorded refresh gap.
- No runtime source behavior changed in this slice.

## Refreshed Snapshot

- `src/notebooklm/types.py`: 1543 lines.
- `src/notebooklm/__init__.py`: 290 lines.
- `tests/unit/test_types.py`: 1513 lines before this task.
- `tests/unit/test_public_shims.py`: 681 lines before this task.
- `tests/unit/test_init_order.py`: 877 lines.
- `tests/unit/test_source_status.py`: 443 lines.
- `tests/unit/test_sharing_types.py`: 303 lines.
- No `src/notebooklm/_types/` package exists on this base.
- T13.0 conflict check was clean for owned target files on this worktree.

## Frozen `notebooklm.types.__all__`

```python
[
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
    "SourceError",
    "SourceAddError",
    "SourceProcessingError",
    "SourceTimeoutError",
    "SourceNotFoundError",
    "ArtifactError",
    "ArtifactNotFoundError",
    "ArtifactNotReadyError",
    "ArtifactParseError",
    "ArtifactDownloadError",
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
```

## Frozen Top-Level Type Exports

`notebooklm.__all__` must continue to include and resolve these names as identity re-exports from `notebooklm.types`:

`AccountLimits`, `AccountTier`, `Artifact`, `ArtifactType`, `AskResult`, `AudioFormat`, `AudioLength`, `ChatGoal`, `ChatMode`, `ChatReference`, `ChatResponseLength`, `CitedSourceSelection`, `ClientMetricsSnapshot`, `ConnectionLimits`, `ConversationTurn`, `DriveMimeType`, `ExportType`, `GenerationStatus`, `InfographicDetail`, `InfographicOrientation`, `InfographicStyle`, `Note`, `Notebook`, `NotebookDescription`, `NotebookMetadata`, `QuizDifficulty`, `QuizQuantity`, `ReportFormat`, `ReportSuggestion`, `RpcTelemetryEvent`, `ShareAccess`, `SharedUser`, `SharePermission`, `ShareStatus`, `ShareViewLevel`, `SlideDeckFormat`, `SlideDeckLength`, `Source`, `SourceFulltext`, `SourceStatus`, `SourceSummary`, `SourceType`, `SuggestedTopic`, `UnknownTypeWarning`, `VideoFormat`, `VideoStyle`.

`StudioContentType` remains a deprecated top-level shim only. `from notebooklm import StudioContentType` warns once, returns canonical `notebooklm.rpc.types.ArtifactTypeCode`, and caches the global.

## Frozen Identities

Exception re-exports from `notebooklm.types` must be identity aliases of `notebooklm.exceptions`: `SourceError`, `SourceAddError`, `SourceProcessingError`, `SourceTimeoutError`, `SourceNotFoundError`, `ArtifactError`, `ArtifactNotFoundError`, `ArtifactNotReadyError`, `ArtifactParseError`, `ArtifactDownloadError`.

Top-level exception exports in `notebooklm.__all__` must be identity aliases of `notebooklm.exceptions`: `ArtifactDownloadError`, `ArtifactError`, `ArtifactNotFoundError`, `ArtifactNotReadyError`, `ArtifactParseError`, `AuthError`, `AuthExtractionError`, `ChatError`, `ClientError`, `ConfigurationError`, `DecodingError`, `NetworkError`, `NonIdempotentRetryError`, `NotebookError`, `NotebookLimitError`, `NotebookLMError`, `NotebookNotFoundError`, `RateLimitError`, `ResearchTaskMismatchError`, `RPCError`, `RPCTimeoutError`, `ServerError`, `SourceAddError`, `SourceError`, `SourceNotFoundError`, `SourceProcessingError`, `SourceTimeoutError`, `UnknownRPCMethodError`, `ValidationError`.

RPC enum re-exports from `notebooklm.types` must be identity aliases of `notebooklm.rpc.types`: `ArtifactStatus`, `AudioFormat`, `AudioLength`, `ChatGoal`, `ChatResponseLength`, `DriveMimeType`, `ExportType`, `InfographicDetail`, `InfographicOrientation`, `InfographicStyle`, `QuizDifficulty`, `QuizQuantity`, `ReportFormat`, `ShareAccess`, `SharePermission`, `ShareViewLevel`, `SlideDeckFormat`, `SlideDeckLength`, `SourceStatus`, `VideoFormat`, `VideoStyle`.

RPC helper re-exports must remain identity aliases of `notebooklm.rpc.types.artifact_status_to_str` and `notebooklm.rpc.types.source_status_to_str`.

## Frozen Non-`__all__` Facade Attributes

- `notebooklm.types.ArtifactTypeCode` is present and is `notebooklm.rpc.types.ArtifactTypeCode`.
- `notebooklm.types.StudioContentType` is absent.
- `notebooklm.types.RPCMethod` is absent.
- `AccountLimits` and `AccountTier` remain present on `notebooklm.types` and top-level `notebooklm`, even though they are not in `notebooklm.types.__all__` on this base.

## Frozen Private Helper Seams

First-party imports from `notebooklm.types` remain live aliases for:

`_SOURCE_TYPE_COMPAT_MAP`, `_datetime_from_timestamp`, `_extract_artifact_url`, `_extract_audio_artifact_url`, `_extract_infographic_artifact_url`, `_extract_slide_deck_artifact_url`, `_extract_source_created_at`, `_extract_source_url`, `_extract_video_artifact_url`, `_is_valid_artifact_url`, `_warned_artifact_types`, `_warned_source_types`.

The live state objects `_SOURCE_TYPE_COMPAT_MAP`, `_warned_artifact_types`, and `_warned_source_types` must remain the objects used by parsing and warning de-duplication.

The helper seam manifest is checked against current first-party imports from `src/notebooklm/` and `tests/` so later slices cannot add a private import from `notebooklm.types` without updating the freeze.

## Frozen `__module__` Policy

Default policy for public dataclasses and user-facing enums that move in T13.1-T13.3: preserve `__module__ == "notebooklm.types"`.

Pinned movable classes/enums: `AccountLimits`, `AccountTier`, `Artifact`, `ArtifactType`, `AskResult`, `ChatMode`, `ChatReference`, `CitedSourceSelection`, `ClientMetricsSnapshot`, `ConnectionLimits`, `ConversationTurn`, `GenerationStatus`, `Note`, `Notebook`, `NotebookDescription`, `NotebookMetadata`, `ReportSuggestion`, `RpcTelemetryEvent`, `SharedUser`, `ShareStatus`, `Source`, `SourceFulltext`, `SourceSummary`, `SourceType`, `SuggestedTopic`.

Representative dataclass pickle round trips are pinned for common, notebook, source, artifact, note, chat, and sharing instances.

## Direct `notebooklm.types` Consumers

Current source consumers include `__init__.py`, `_artifact_downloads.py`, `_artifact_formatters.py`, `_artifact_generation.py`, `_artifact_listing.py`, `_artifact_polling.py`, `_artifacts.py`, `_chat.py`, `_chat_protocol.py`, `_core.py`, `_mind_map.py`, `_notebook_metadata.py`, `_notebooks.py`, `_notes.py`, `_research.py`, `_settings.py`, `_sharing.py`, `_source_add.py`, `_source_content.py`, `_source_listing.py`, `_source_polling.py`, `_source_upload.py`, `_sources.py`, `client.py`, and `research.py`.

Docs mentioning public type imports or raw RPC paths: `docs/python-api.md`, `docs/stability.md`, `docs/development.md`, `docs/rpc-development.md`, `docs/rpc-reference.md`, `docs/troubleshooting.md`, and `docs/configuration.md`.

Existing unit/integration consumers are broad; later T13 verification must include at least the targeted type/public-shim/source/sharing/init-order tests from this task plus service-specific unit tests for any modules changed in each slice.

## T13.1-T13.5 Target File Freeze

- T13.1 common skeleton: `src/notebooklm/_types/__init__.py`, `src/notebooklm/_types/common.py`, `src/notebooklm/types.py`, `tests/unit/test_public_shims.py`, `tests/unit/test_types.py`, `tests/unit/test_observability.py`, `tests/unit/test_user_settings_api.py`, `tests/unit/test_exceptions.py`, `tests/integration/concurrency/test_pool_tuning.py`, `tests/integration/concurrency/test_max_concurrent_rpcs.py`, `tests/unit/test_env_base_url.py`.
- T13.2 source/notebook split: `src/notebooklm/_types/sources.py`, `src/notebooklm/_types/notebooks.py`, `src/notebooklm/types.py`, `src/notebooklm/_sources.py`, `src/notebooklm/_source_listing.py`, `src/notebooklm/_source_polling.py`, `src/notebooklm/_source_add.py`, `src/notebooklm/_source_upload.py`, `src/notebooklm/_source_content.py`, `src/notebooklm/_notebook_metadata.py`, `src/notebooklm/_notebooks.py`, `src/notebooklm/cli/source.py`, `src/notebooklm/cli/notebook.py`, and related source/notebook unit tests.
- T13.3 artifact/note/chat/sharing split: `src/notebooklm/_types/artifacts.py`, `src/notebooklm/_types/notes.py`, `src/notebooklm/_types/chat.py`, `src/notebooklm/_types/sharing.py`, `src/notebooklm/types.py`, `src/notebooklm/_artifacts.py`, `src/notebooklm/_artifact_listing.py`, `src/notebooklm/_artifact_polling.py`, `src/notebooklm/_artifact_generation.py`, `src/notebooklm/_artifact_downloads.py`, `src/notebooklm/_artifact_formatters.py`, `src/notebooklm/_notes.py`, `src/notebooklm/_chat.py`, `src/notebooklm/_chat_protocol.py`, `src/notebooklm/_sharing.py`, `src/notebooklm/_mind_map.py`, and related unit tests.
- T13.4 type boundary guardrails: static guardrail tests only after `_types` exists; do not force unlanded boundaries.
- T13.5 final verification: run the full T13-focused test matrix plus `ruff`, `mypy`, and any upstream-added Phase 10 handoff checks.

## Verification Matrix For This Freeze

```bash
rtk uv run --extra dev pytest -n auto tests/unit/test_public_shims.py tests/unit/test_types.py tests/unit/test_source_status.py tests/unit/test_sharing_types.py tests/unit/test_init_order.py
rtk uv run --extra dev python - <<'PY'
import notebooklm
import notebooklm.exceptions as exc
import notebooklm.types as t
import notebooklm.rpc.types as rpc_types

for name in [
    "SourceError", "SourceAddError", "SourceProcessingError",
    "SourceTimeoutError", "SourceNotFoundError", "ArtifactError",
    "ArtifactNotFoundError", "ArtifactNotReadyError", "ArtifactParseError",
    "ArtifactDownloadError",
]:
    assert getattr(t, name) is getattr(exc, name)
assert t.artifact_status_to_str is rpc_types.artifact_status_to_str
assert t.source_status_to_str is rpc_types.source_status_to_str
for name in notebooklm.__all__:
    getattr(notebooklm, name)
PY
rtk uv run --extra dev ruff check src/notebooklm/types.py src/notebooklm/__init__.py tests/unit/test_public_shims.py tests/unit/test_types.py tests/unit/test_source_status.py tests/unit/test_sharing_types.py tests/unit/test_init_order.py
rtk uv run --extra dev mypy src/notebooklm
```
