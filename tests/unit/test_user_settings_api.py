"""Unit tests for user settings parsing."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._settings import (
    SettingsAPI,
    build_get_user_settings_params,
    build_get_user_tier_params,
    extract_account_limits,
    extract_account_tier,
)
from notebooklm.rpc import RPCMethod
from notebooklm.types import AccountLimits, AccountTier


def test_build_get_user_settings_params_returns_fresh_params():
    first = build_get_user_settings_params()
    second = build_get_user_settings_params()

    assert first == [None, [1, None, None, None, None, None, None, None, None, None, [1]]]
    assert first is not second
    assert first[1] is not second[1]


def test_build_get_user_tier_params_returns_fresh_params():
    first = build_get_user_tier_params()
    second = build_get_user_tier_params()

    assert first == [
        [
            [
                [None, "1", 627],
                [None, None, None, None, None, None, None, None, None, [None, None, 2]],
                1,
            ]
        ]
    ]
    assert first is not second
    assert first[0] is not second[0]


def test_get_user_tier_params_match_captured_request_shape():
    params = build_get_user_tier_params()

    assert params[0][0][0] == [None, "1", 627]
    assert params[0][0][1][9] == [None, None, 2]
    assert params[0][0][2] == 1


def test_extract_account_limits_from_user_settings_response():
    limits = extract_account_limits([[None, [6, 500, 300, 500000, 2]]])

    assert limits == AccountLimits(
        notebook_limit=500,
        source_limit=300,
        raw_limits=(6, 500, 300, 500000, 2),
    )


def test_extract_account_limits_preserves_raw_limit_positions():
    limits = extract_account_limits([[None, [True, 100, "source-limit", None]]])

    assert limits.notebook_limit == 100
    assert limits.source_limit is None
    assert limits.raw_limits == (True, 100, "source-limit", None)


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        [[None]],
        [[None, None]],
        [[None, ["tier", "500"]]],
        [[None, [True, False, "300"]]],
    ],
)
def test_extract_account_limits_returns_empty_for_malformed_response(response):
    limits = extract_account_limits(response)

    assert limits.notebook_limit is None
    assert limits.source_limit is None


def test_extract_account_tier_from_nested_response():
    response = [[[[None, "1", 627], [[1613, [None, "NOTEBOOKLM_TIER_PRO"]]], 0]]]

    assert extract_account_tier(response) == AccountTier(
        tier="NOTEBOOKLM_TIER_PRO",
        plan_name="Google AI Pro",
    )


def test_extract_account_tier_returns_empty_for_malformed_response():
    assert extract_account_tier([[["no tier here"]]]) == AccountTier()


@pytest.mark.parametrize("response", [None, []])
def test_extract_account_tier_handles_empty_response(response):
    assert extract_account_tier(response) == AccountTier()


def test_extract_account_tier_preserves_unknown_tier_string():
    response = [[["NOTEBOOKLM_TIER_FUTURE"]]]

    assert extract_account_tier(response) == AccountTier(
        tier="NOTEBOOKLM_TIER_FUTURE",
        plan_name=None,
    )


@pytest.mark.asyncio
async def test_get_account_limits_calls_user_settings_rpc():
    core = MagicMock()
    core.rpc_call = AsyncMock(return_value=[[None, [6, 200, 100, 500000, 1]]])
    api = SettingsAPI(core)

    limits = await api.get_account_limits()

    assert limits == AccountLimits(
        notebook_limit=200,
        source_limit=100,
        raw_limits=(6, 200, 100, 500000, 1),
    )
    core.rpc_call.assert_awaited_once_with(
        RPCMethod.GET_USER_SETTINGS,
        [None, [1, None, None, None, None, None, None, None, None, None, [1]]],
        source_path="/",
    )


@pytest.mark.asyncio
async def test_get_account_tier_calls_user_tier_rpc():
    core = MagicMock()
    core.rpc_call = AsyncMock(return_value=[[[[None, "NOTEBOOKLM_TIER_STANDARD"]]]])
    api = SettingsAPI(core)

    tier = await api.get_account_tier()

    assert tier == AccountTier(tier="NOTEBOOKLM_TIER_STANDARD", plan_name="Standard")
    core.rpc_call.assert_awaited_once_with(
        RPCMethod.GET_USER_TIER,
        [
            [
                [
                    [None, "1", 627],
                    [None, None, None, None, None, None, None, None, None, [None, None, 2]],
                    1,
                ]
            ]
        ],
        source_path="/",
    )
