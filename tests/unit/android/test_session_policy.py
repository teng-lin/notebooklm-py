from __future__ import annotations

import pytest

from notebooklm._android.session import (
    _ANDROID_IDEMPOTENCY_POLICIES,
    _MUTATE_LABEL_METHOD,
    _ORCHESTRATION_SERVICE,
    _SHARING_SERVICE,
    _method_allows_replay,
    _resolve_android_idempotency_policy,
)
from notebooklm._web.policy import IDEMPOTENCY_REGISTRY
from notebooklm.rpc.types import RPCMethod

_WEB_METHOD_BY_ANDROID_NAME = {
    "ActOnSources": RPCMethod.GENERATE_MIND_MAP,
    "AddSources": RPCMethod.ADD_SOURCE,
    "AddSourcesAsync": RPCMethod.ADD_SOURCES_ASYNC,
    "AddTentativeSources": RPCMethod.ADD_SOURCE_FILE,
    "AppendSource": RPCMethod.APPEND_SOURCE,
    "CancelDiscoverSourcesJob": RPCMethod.CANCEL_RESEARCH,
    "CancelGeneration": RPCMethod.CANCEL_GENERATION,
    "CheckSourceFreshness": RPCMethod.CHECK_SOURCE_FRESHNESS,
    "CopyArtifactsAsync": RPCMethod.COPY_ARTIFACTS,
    "CopyProject": RPCMethod.COPY_NOTEBOOK,
    "CopySourcesAsync": RPCMethod.COPY_SOURCES,
    "CreateArtifact": RPCMethod.CREATE_ARTIFACT,
    "CreateLabel": RPCMethod.CREATE_LABEL,
    "CreateNote": RPCMethod.CREATE_NOTE,
    "CreateProject": RPCMethod.CREATE_NOTEBOOK,
    "DeleteArtifact": RPCMethod.DELETE_ARTIFACT,
    "DeleteChatTurns": RPCMethod.DELETE_CONVERSATION,
    "DeleteLabels": RPCMethod.DELETE_LABEL,
    "DeleteNotes": RPCMethod.DELETE_NOTE,
    "DeleteProjects": RPCMethod.DELETE_NOTEBOOK,
    "DeleteSources": RPCMethod.DELETE_SOURCE,
    "DeriveArtifact": RPCMethod.REVISE_SLIDE,
    "DiscoverSources": RPCMethod.DISCOVER_SOURCES,
    "DiscoverSourcesAsync": RPCMethod.START_DEEP_RESEARCH,
    "DiscoverSourcesManifold": RPCMethod.START_FAST_RESEARCH,
    "ExportToDrive": RPCMethod.EXPORT_ARTIFACT,
    "FinishDiscoverSourcesRun": RPCMethod.IMPORT_RESEARCH,
    "GenerateArtifact": RPCMethod.RETRY_ARTIFACT,
    "GenerateDocumentGuides": RPCMethod.GET_SOURCE_GUIDE,
    "GenerateNotebookGuide": RPCMethod.SUMMARIZE,
    "GeneratePromptSuggestions": RPCMethod.SUGGEST_PROMPTS,
    "GenerateReportSuggestions": RPCMethod.GET_SUGGESTED_REPORTS,
    "GetArtifact": RPCMethod.GET_INTERACTIVE_HTML,
    "GetArtifactCustomizationChoices": RPCMethod.GET_CUSTOMIZATION_CHOICES,
    "GetChatSessionStatus": RPCMethod.GET_CHAT_SESSION_STATUS,
    "GetLabels": RPCMethod.LIST_LABELS,
    "GetNotes": RPCMethod.GET_NOTES_AND_MIND_MAPS,
    "GetOrCreateAccount": RPCMethod.GET_USER_SETTINGS,
    "GetProject": RPCMethod.GET_NOTEBOOK,
    "ListArtifacts": RPCMethod.LIST_ARTIFACTS,
    "ListChatSessions": RPCMethod.GET_LAST_CONVERSATION_ID,
    "ListChatTurns": RPCMethod.GET_CONVERSATION_TURNS,
    "ListDiscoverSourcesJob": RPCMethod.POLL_RESEARCH,
    "ListExpertIntelligenceContent": RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT,
    "ListRecentlyViewedProjects": RPCMethod.LIST_NOTEBOOKS,
    "LoadSource": RPCMethod.GET_SOURCE,
    "MutateAccount": RPCMethod.SET_USER_SETTINGS,
    "MutateLabel": RPCMethod.UPDATE_LABEL,
    "MutateNote": RPCMethod.UPDATE_NOTE,
    "MutateProject": RPCMethod.RENAME_NOTEBOOK,
    "MutateSource": RPCMethod.UPDATE_SOURCE,
    "NextStepSuggestions": RPCMethod.SUGGEST_NEXT_STEPS,
    "RefreshSource": RPCMethod.REFRESH_SOURCE,
    "RemoveRecentlyViewedProject": RPCMethod.REMOVE_RECENTLY_VIEWED,
    "RetrieveRelevantChunks": RPCMethod.RETRIEVE_RELEVANT_CHUNKS,
    "UpdateArtifact": RPCMethod.RENAME_ARTIFACT,
}
_SHARING_METHODS = {
    "GetProjectDetails": RPCMethod.GET_SHARE_STATUS,
    "ShareProject": RPCMethod.SHARE_NOTEBOOK,
}


def test_android_policy_table_is_total_and_matches_every_web_registry_row() -> None:
    expected_methods = {
        **{
            f"{_ORCHESTRATION_SERVICE}{name}": method
            for name, method in _WEB_METHOD_BY_ANDROID_NAME.items()
        },
        **{f"{_SHARING_SERVICE}{name}": method for name, method in _SHARING_METHODS.items()},
    }
    streamed = f"{_ORCHESTRATION_SERVICE}GenerateFreeFormStreamed"
    assert set(_ANDROID_IDEMPOTENCY_POLICIES) == {*expected_methods, streamed}
    for android_method, web_method in expected_methods.items():
        assert _resolve_android_idempotency_policy(android_method).value == (
            IDEMPOTENCY_REGISTRY.get_entry(web_method).policy.value
        )
    assert _method_allows_replay(streamed) is False


def test_mutate_label_variants_exactly_match_web_and_fail_closed() -> None:
    variants = {"add_sources", "remove_sources", "add_notebooks", "remove_notebooks"}
    assert set(_ANDROID_IDEMPOTENCY_POLICIES[_MUTATE_LABEL_METHOD].variants or {}) == variants
    for variant in variants:
        assert _resolve_android_idempotency_policy(_MUTATE_LABEL_METHOD, variant).value == (
            IDEMPOTENCY_REGISTRY.get_entry(
                RPCMethod.UPDATE_LABEL,
                operation_variant=variant,
            ).policy.value
        )
    with pytest.raises(ValueError, match="Unknown Android RPC operation_variant"):
        _resolve_android_idempotency_policy(_MUTATE_LABEL_METHOD, "new_variant")
    with pytest.raises(ValueError, match="has no idempotency policy"):
        _resolve_android_idempotency_policy("/new.Service/NewMethod")
