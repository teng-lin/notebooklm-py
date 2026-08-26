"""P6.6 migration sentinels for settings, limits, and suggestions."""

from __future__ import annotations

import inspect
from dataclasses import fields
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._notebooks import NotebooksAPI
from notebooklm._settings import SettingsAPI
from notebooklm.rpc import RPCMethod
from notebooklm.types import AccountLimits, PromptSuggestion, ReportSuggestion, UserSettings
from tests._fixtures.fake_core import make_fake_core
from tests._fixtures.web_backend import build_web_backend
from tests._helpers.signature_inspection import signature_parameters


def test_p66_public_method_signatures_are_frozen() -> None:
    settings_methods = (
        SettingsAPI.get_user_settings,
        SettingsAPI.get_output_language,
        SettingsAPI.get_account_limits,
    )
    assert all(list(signature_parameters(method)) == ["self"] for method in settings_methods)
    assert list(signature_parameters(SettingsAPI.set_output_language)) == [
        "self",
        "language",
    ]

    prompt = signature_parameters(NotebooksAPI.suggest_prompts)
    assert list(prompt) == ["self", "notebook_id", "source_ids", "mode", "query"]
    assert prompt["source_ids"].kind is inspect.Parameter.KEYWORD_ONLY
    assert prompt["source_ids"].default is None
    assert prompt["mode"].default == 4
    assert prompt["query"].default is None
    assert list(signature_parameters(ArtifactsAPI.suggest_reports)) == [
        "self",
        "notebook_id",
    ]


def test_p66_public_model_fields_and_mutability_are_frozen() -> None:
    assert [field.name for field in fields(AccountLimits)] == [
        "notebook_limit",
        "source_limit",
        "raw_limits",
        "tier",
    ]
    assert [field.name for field in fields(UserSettings)] == ["limits", "output_language"]
    assert [field.name for field in fields(PromptSuggestion)] == ["title", "prompt"]
    assert [field.name for field in fields(ReportSuggestion)] == [
        "title",
        "description",
        "prompt",
        "audience_level",
    ]
    assert AccountLimits.__dataclass_params__.frozen is True
    assert UserSettings.__dataclass_params__.frozen is True
    assert PromptSuggestion.__dataclass_params__.frozen is True
    assert ReportSuggestion.__dataclass_params__.frozen is False


@pytest.mark.asyncio
async def test_settings_public_calls_each_keep_one_account_routed_rpc() -> None:
    response = [[None, [6, 200, 100, 500000, 1], [True, None, None, True, ["fr"]]]]
    rpc_call = AsyncMock(side_effect=[response, response, response])
    api = SettingsAPI(build_web_backend(MagicMock(rpc_call=rpc_call)))

    await api.get_user_settings()
    await api.get_output_language()
    await api.get_account_limits()

    assert [call.args[0] for call in rpc_call.await_args_list] == [
        RPCMethod.GET_USER_SETTINGS,
        RPCMethod.GET_USER_SETTINGS,
        RPCMethod.GET_USER_SETTINGS,
    ]
    assert all(call.kwargs["source_path"] == "/" for call in rpc_call.await_args_list)
    assert all(
        call.kwargs["disable_internal_retries"] is False for call in rpc_call.await_args_list
    )


@pytest.mark.asyncio
async def test_prompt_recency_is_exactly_conditional_and_report_has_none() -> None:
    prompt_response = [[["Title", "Prompt"]]]
    rpc_call = AsyncMock(
        side_effect=[
            prompt_response,
            prompt_response,
            [["Notebook", [[["src-default"]]], "nb"]],
            prompt_response,
            [["Report", "Description", None, None, "Prompt", 2]],
        ]
    )
    executor = MagicMock(rpc_call=rpc_call)
    backend = build_web_backend(executor)
    notebooks = NotebooksAPI(_backend=backend)

    await notebooks.suggest_prompts("nb", source_ids=["src-pinned"])
    await notebooks.suggest_prompts("nb", source_ids=[])
    await notebooks.suggest_prompts("nb")

    core = make_fake_core(rpc_call=rpc_call)
    artifacts = ArtifactsAPI(
        drain=core,
        lifecycle=core,
        notebooks=MagicMock(),
        _backend=backend,
    )
    await artifacts.suggest_reports("nb")

    methods = [call.args[0] for call in rpc_call.await_args_list]
    assert methods == [
        RPCMethod.SUGGEST_PROMPTS,
        RPCMethod.SUGGEST_PROMPTS,
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.SUGGEST_PROMPTS,
        RPCMethod.GET_SUGGESTED_REPORTS,
    ]
    assert methods.count(RPCMethod.GET_NOTEBOOK) == 1
    assert all(
        call.kwargs["disable_internal_retries"] is False for call in rpc_call.await_args_list
    )
