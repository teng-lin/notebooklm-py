"""Semantic service tests for P6.6 settings and suggestions."""

from __future__ import annotations

from dataclasses import replace

import pytest

from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import CallPolicy, Operation
from notebooklm._semantic.projectors import (
    project_account_limits,
    project_prompt_suggestions,
    project_report_suggestions,
    project_user_settings,
)
from notebooklm._semantic.records import (
    ARTIFACT_SUGGEST_REPORTS_DEF,
    NOTEBOOK_SUGGEST_PROMPTS_DEF,
    SETTINGS_GET_DEF,
    SETTINGS_GET_LIMITS_DEF,
    SETTINGS_SET_LANGUAGE_DEF,
    AccountLimitsRecord,
    ArtifactSuggestReportsResult,
    NotebookSuggestPromptsResult,
    PromptSuggestionRecord,
    ReportSuggestionRecord,
    SettingsGetLimitsResult,
    SettingsGetResult,
    SettingsSetLanguageResult,
    UserSettingsRecord,
)
from notebooklm._semantic.services.settings import SettingsService
from notebooklm._semantic.services.suggestion import SuggestionService
from notebooklm.exceptions import ValidationError
from notebooklm.types import AccountLimits, PromptSuggestion, ReportSuggestion, UserSettings
from tests._fixtures.recording_backend import RecordingBackend


def test_p66_operation_definitions_are_concrete_frozen_values() -> None:
    definitions = {
        SETTINGS_GET_DEF: (Operation.SETTINGS_GET, CallPolicy.READ),
        SETTINGS_GET_LIMITS_DEF: (Operation.SETTINGS_GET_LIMITS, CallPolicy.READ),
        SETTINGS_SET_LANGUAGE_DEF: (Operation.SETTINGS_SET_LANGUAGE, CallPolicy.MUTATION),
        NOTEBOOK_SUGGEST_PROMPTS_DEF: (
            Operation.NOTEBOOK_SUGGEST_PROMPTS,
            CallPolicy.STATEFUL_START,
        ),
        ARTIFACT_SUGGEST_REPORTS_DEF: (
            Operation.ARTIFACT_SUGGEST_REPORTS,
            CallPolicy.STATEFUL_START,
        ),
    }

    for definition, (operation, policy) in definitions.items():
        assert definition.key is operation
        assert definition.policy is policy
        assert replace(definition) == definition
        assert definition.input_type is not object
        assert definition.output_type is not object


@pytest.mark.asyncio
async def test_settings_service_returns_each_neutral_record_without_combining_calls() -> None:
    backend = RecordingBackend()
    limits = AccountLimitsRecord(200, 100, (6, 200, 100, 500000, 99), 99)
    backend.set_result(
        SETTINGS_GET_DEF,
        SettingsGetResult(UserSettingsRecord(limits, "fr")),
    )
    backend.set_result(SETTINGS_GET_LIMITS_DEF, SettingsGetLimitsResult(limits))
    backend.set_result(SETTINGS_SET_LANGUAGE_DEF, SettingsSetLanguageResult("ja"))
    service = SettingsService(backend)

    assert await service.get_user_settings() == UserSettingsRecord(
        limits=AccountLimitsRecord(200, 100, (6, 200, 100, 500000, 99), 99),
        output_language="fr",
    )
    assert await service.get_output_language() == "fr"
    assert await service.get_account_limits() == AccountLimitsRecord(
        200,
        100,
        (6, 200, 100, 500000, 99),
        99,
    )
    assert await service.set_output_language("ja") == "ja"
    assert [invocation.operation for invocation in backend.invocations] == [
        Operation.SETTINGS_GET,
        Operation.SETTINGS_GET,
        Operation.SETTINGS_GET_LIMITS,
        Operation.SETTINGS_SET_LANGUAGE,
    ]


def test_settings_records_project_to_the_public_models_the_facade_returns() -> None:
    """The public shapes this service used to build, now pinned where they are built.

    ``SettingsAPI`` owns the projection since P10 R6.3 (invariant I1). The
    end-to-end facade assertions live in
    ``test_user_settings_api.py::test_get_user_settings_fetches_once_returns_both``
    and ``::test_get_account_limits_calls_user_settings_rpc``; this pins the
    record-to-model equivalence for the same values the service test uses.
    """
    limits = AccountLimitsRecord(200, 100, (6, 200, 100, 500000, 99), 99)

    assert project_account_limits(limits) == AccountLimits(
        200,
        100,
        (6, 200, 100, 500000, 99),
        99,
    )
    assert project_user_settings(UserSettingsRecord(limits, "fr")) == UserSettings(
        limits=AccountLimits(200, 100, (6, 200, 100, 500000, 99), 99),
        output_language="fr",
    )


@pytest.mark.asyncio
async def test_suggestion_service_preserves_records_unknowns_and_deadline_identity() -> None:
    """R6.2: the service hands back records; the projections it used to build
    are asserted here on ``_projectors`` so the unknown-level passthrough and
    the field order this test pinned survive the move above the port.
    """
    backend = RecordingBackend()
    backend.set_result(
        NOTEBOOK_SUGGEST_PROMPTS_DEF,
        NotebookSuggestPromptsResult((PromptSuggestionRecord("Title", "Prompt"),)),
    )
    backend.set_result(
        ARTIFACT_SUGGEST_REPORTS_DEF,
        ArtifactSuggestReportsResult(
            (
                ReportSuggestionRecord(
                    "Report",
                    "Description",
                    "Prompt",
                    "unknown-level",
                ),
            )
        ),
    )
    service = SuggestionService(backend)
    deadline = RuntimeDeadline(timeout=4.0, started_at=10.0, monotonic=lambda: 11.0)

    prompts = await service.suggest_prompts(
        "nb",
        source_ids=["src"],
        mode=7,
        query=" steer ",
        deadline=deadline,
    )
    assert prompts == [PromptSuggestionRecord("Title", "Prompt")]
    assert project_prompt_suggestions(tuple(prompts)) == [PromptSuggestion("Title", "Prompt")]
    reports = await service.suggest_reports("nb", deadline=deadline)
    assert reports == [ReportSuggestionRecord("Report", "Description", "Prompt", "unknown-level")]
    assert project_report_suggestions(tuple(reports)) == [
        ReportSuggestion("Report", "Description", "Prompt", "unknown-level")
    ]
    assert all(invocation.deadline is deadline for invocation in backend.invocations)
    prompt_input = backend.invocations[0].value
    assert prompt_input.source_ids == ("src",)
    assert prompt_input.query == " steer "


@pytest.mark.asyncio
async def test_invalid_prompt_mode_fails_before_backend_invocation() -> None:
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_SUGGEST_PROMPTS_DEF, NotebookSuggestPromptsResult(()))

    with pytest.raises(ValidationError, match="inclusive range 1..10") as caught:
        await SuggestionService(backend).suggest_prompts("nb", mode=0)

    assert isinstance(caught.value.__cause__, ValueError)
    assert backend.invocations == []
