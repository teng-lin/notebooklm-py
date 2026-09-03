"""Retry classes for every Android gRPC method used by the SDK.

The web idempotency registry remains the source of truth for operation
semantics. This Android projection is keyed by the concrete gRPC method name:
safe reads (plus the idempotent generation cancel) are replayable, while
mutations and inference kickoffs are not. The AST guardrail keeps every
``replay_safe=`` literal at Android call sites equal to this table.
"""

from __future__ import annotations

_ORCHESTRATION = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
_SHARING = "labs.language.tailwind.sharing.LabsTailwindSharingService"


def _orchestration(method: str) -> str:
    return f"/{_ORCHESTRATION}/{method}"


ANDROID_RETRY_MANIFEST: dict[str, bool] = {
    _orchestration("ActOnSources"): False,
    _orchestration("AddSources"): False,
    _orchestration("AddSourcesAsync"): False,
    _orchestration("AddTentativeSources"): False,
    _orchestration("AppendSource"): False,
    _orchestration("CancelDiscoverSourcesJob"): False,
    _orchestration("CancelGeneration"): True,
    _orchestration("CheckSourceFreshness"): True,
    _orchestration("CopyArtifactsAsync"): False,
    _orchestration("CopyProject"): False,
    _orchestration("CopySourcesAsync"): False,
    _orchestration("CreateArtifact"): False,
    _orchestration("CreateLabel"): False,
    _orchestration("CreateNote"): False,
    _orchestration("CreateProject"): False,
    _orchestration("DeleteArtifact"): False,
    _orchestration("DeleteChatTurns"): False,
    _orchestration("DeleteLabels"): False,
    _orchestration("DeleteNotes"): False,
    _orchestration("DeleteProjects"): False,
    _orchestration("DeleteSources"): False,
    _orchestration("DeriveArtifact"): False,
    _orchestration("DiscoverSources"): False,
    _orchestration("DiscoverSourcesAsync"): False,
    _orchestration("DiscoverSourcesManifold"): False,
    _orchestration("ExportToDrive"): False,
    _orchestration("FinishDiscoverSourcesRun"): False,
    _orchestration("GenerateArtifact"): False,
    _orchestration("GenerateDocumentGuides"): True,
    _orchestration("GenerateFreeFormStreamed"): False,
    _orchestration("GenerateNotebookGuide"): False,
    _orchestration("GeneratePromptSuggestions"): True,
    _orchestration("GenerateReportSuggestions"): True,
    _orchestration("GetArtifact"): True,
    _orchestration("GetArtifactCustomizationChoices"): True,
    _orchestration("GetChatSessionStatus"): True,
    _orchestration("GetLabels"): True,
    _orchestration("GetNotes"): True,
    _orchestration("GetOrCreateAccount"): False,
    _orchestration("GetProject"): True,
    _orchestration("ListArtifacts"): True,
    _orchestration("ListChatSessions"): True,
    _orchestration("ListChatTurns"): True,
    _orchestration("ListDiscoverSourcesJob"): True,
    _orchestration("ListExpertIntelligenceContent"): True,
    _orchestration("ListRecentlyViewedProjects"): True,
    _orchestration("LoadSource"): True,
    _orchestration("MutateAccount"): False,
    _orchestration("MutateLabel"): False,
    _orchestration("MutateNote"): False,
    _orchestration("MutateProject"): False,
    _orchestration("MutateSource"): False,
    _orchestration("NextStepSuggestions"): True,
    _orchestration("RefreshSource"): False,
    _orchestration("RemoveRecentlyViewedProject"): False,
    _orchestration("RetrieveRelevantChunks"): True,
    _orchestration("UpdateArtifact"): False,
    f"/{_SHARING}/GetProjectDetails": True,
    f"/{_SHARING}/ShareProject": False,
}


def replay_safe_for(method: str, declared: bool) -> bool:
    """Apply the manifest ceiling without overriding an explicit no-replay request."""

    # AndroidSession is a generic transport seam and its focused tests (plus
    # future raw callers) legitimately use method names outside the production
    # SDK manifest. Unknown can run once, but it can never inherit replay safety.
    return declared and ANDROID_RETRY_MANIFEST.get(method, False)


__all__ = ["ANDROID_RETRY_MANIFEST", "replay_safe_for"]
