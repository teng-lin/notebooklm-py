"""Unit tests for user settings parsing."""

from unittest.mock import AsyncMock

import pytest

from notebooklm._settings import (
    SettingsAPI,
    _extract_language,
    build_get_user_settings_params,
    build_get_user_tier_params,
    extract_account_limits,
    extract_account_tier,
)
from notebooklm.exceptions import UnknownRPCMethodError
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


@pytest.mark.parametrize(
    ("tier_string", "expected_plan"),
    [
        ("NOTEBOOKLM_TIER_STANDARD", "Standard"),
        ("NOTEBOOKLM_TIER_PLUS", "Google AI Plus"),
        ("NOTEBOOKLM_TIER_PRO", "Google AI Pro"),
        ("NOTEBOOKLM_TIER_PRO_DASHER_END_USER", "Google Workspace Pro"),
        ("NOTEBOOKLM_TIER_ULTRA", "Google AI Ultra"),
    ],
)
def test_extract_account_tier_maps_all_known_plan_names(tier_string, expected_plan):
    """Every tier in ``_TIER_PLAN_NAMES`` must round-trip through the parser.

    Locks in the plan-name lookup table so future tier additions can't
    silently drift between :func:`extract_account_tier` and the
    :class:`AccountTier` ``plan_name`` mapping.
    """
    response = [[[[None, "1", 627], [[1613, [None, tier_string]]], 0]]]

    assert extract_account_tier(response) == AccountTier(
        tier=tier_string,
        plan_name=expected_plan,
    )


def test_extract_account_tier_against_recorded_cassette_shape():
    """Parser handles the real GET_USER_TIER envelope recorded against the live API.

    Mirrors the deeply-nested response shape captured by the live API in
    ``tests/cassettes/settings_get_user_tier.yaml`` so the unit test fails
    fast if the parser drifts away from the live wire format — without
    requiring the cassette to be present.
    """
    # Shape mirrors ``[[[[None, "1", 627], [[1613, [None, "<tier>"]]], 0]]]``
    # which is what the wrb.fr envelope's inner JSON decodes to.
    response = [[[[None, "1", 627], [[1613, [None, "NOTEBOOKLM_TIER_PRO"]]], 0]]]

    result = extract_account_tier(response)

    assert result.tier == "NOTEBOOKLM_TIER_PRO"
    assert result.plan_name == "Google AI Pro"


@pytest.mark.asyncio
async def test_get_account_limits_calls_user_settings_rpc():
    from _fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock(return_value=[[None, [6, 200, 100, 500000, 1]]]))
    api = SettingsAPI(core.rpc_executor)

    limits = await api.get_account_limits()

    assert limits == AccountLimits(
        notebook_limit=200,
        source_limit=100,
        raw_limits=(6, 200, 100, 500000, 1),
    )
    core.rpc_executor.rpc_call.assert_awaited_once_with(
        RPCMethod.GET_USER_SETTINGS,
        [None, [1, None, None, None, None, None, None, None, None, None, [1]]],
        source_path="/",
    )


@pytest.mark.asyncio
async def test_get_account_tier_calls_user_tier_rpc():
    from _fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock(return_value=[[[[None, "NOTEBOOKLM_TIER_STANDARD"]]]]))
    api = SettingsAPI(core.rpc_executor)

    tier = await api.get_account_tier()

    assert tier == AccountTier(tier="NOTEBOOKLM_TIER_STANDARD", plan_name="Standard")
    core.rpc_executor.rpc_call.assert_awaited_once_with(
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


# ---------------------------------------------------------------------------
# Language extraction: optional-slot (None) vs envelope drift (raise).
#
# Wire shapes recorded against the live API (tests/cassettes/settings_*):
#   GET_USER_SETTINGS inner: [[null,[..limits..],[true,null,null,true,["fr"]],
#                             [[1]],[true,1,3,2]]]   -> language at [0][2][4][0]
#   SET_USER_SETTINGS inner: [null,[..limits..],[true,null,null,true,["en"]],
#                             [[1]],[true,1,3,2]]    -> language at [2][4][0]
# The settings-flags block (GET [0][2] / SET [2]) is structurally mandatory;
# the language slot ([4], then the ["code"] unwrap [0]) is routinely optional.
# ---------------------------------------------------------------------------

_GET_WIRE_PREFIX = (0, 2)
_GET_WIRE_TAIL = (4, 0)
_SET_WIRE_PREFIX = (2,)
_SET_WIRE_TAIL = (4, 0)


def _get_response(flags_block):
    """Wrap a settings-flags block in the GET_USER_SETTINGS envelope."""
    return [[None, [6, 500, 300, 500000], flags_block, [[1]], [True, 1, 3, 2]]]


def _set_response(flags_block):
    """Wrap a settings-flags block in the SET_USER_SETTINGS envelope."""
    return [None, [6, 500, 300, 500000], flags_block, [[1]], [True, 1, 3, 2]]


def test_extract_language_returns_code_from_get_wire_shape():
    response = _get_response([True, None, None, True, ["fr"]])

    assert (
        _extract_language(
            response,
            _GET_WIRE_PREFIX,
            _GET_WIRE_TAIL,
            method_id="ZwVcOc",
            source="test",
        )
        == "fr"
    )


def test_extract_language_returns_code_from_set_wire_shape():
    response = _set_response([True, None, None, True, ["en"]])

    assert (
        _extract_language(
            response,
            _SET_WIRE_PREFIX,
            _SET_WIRE_TAIL,
            method_id="hT54vc",
            source="test",
        )
        == "en"
    )


@pytest.mark.parametrize(
    "flags_block",
    [
        [True, None, None, True, [""]],  # empty language code (user reset to default)
        [True, None, None, True, []],  # empty language wrapper
        [True, None, None, True],  # language slot omitted (trailing-optional absent)
        [True],  # heavily truncated flags block
        [],  # empty flags block
    ],
    ids=["empty-code", "empty-wrapper", "slot-absent", "truncated", "empty-block"],
)
def test_extract_language_legitimate_absent_returns_none(flags_block):
    """A user with no language set yields ``None`` (must not raise).

    The optional language slot ([4] + its [0] unwrap) lives at the tail of an
    otherwise-intact envelope; its absence is indistinguishable from drift at
    that exact position, so it degrades to ``None`` per the optional-language
    contract.
    """
    assert (
        _extract_language(
            _get_response(flags_block),
            _GET_WIRE_PREFIX,
            _GET_WIRE_TAIL,
            method_id="ZwVcOc",
            source="test",
        )
        is None
    )


def test_extract_language_tail_negative_index_returns_none():
    """A negative tail index degrades to ``None`` rather than from-the-end wrapping.

    The tail loop bound-checks both ends, so a hypothetical future caller
    passing a negative index can't silently read from the end of a list.
    """
    # Flags block has a populated [4] slot; a negative tail index must still
    # not wrap around to it.
    response = _get_response([True, None, None, True, ["fr"]])

    assert (
        _extract_language(
            response,
            _GET_WIRE_PREFIX,
            (-1, 0),
            method_id="ZwVcOc",
            source="test",
        )
        is None
    )


@pytest.mark.parametrize(
    "response",
    [
        None,  # no payload at all
        [],  # empty GET envelope (no result[0])
        [None],  # result[0] present but result[0][2] (flags block) missing
        [[None, [6, 500, 300, 500000]]],  # envelope truncated before flags block
        [42],  # result[0] is a non-subscriptable scalar
    ],
    ids=["none", "empty", "no-flags-block", "truncated-envelope", "scalar-inner"],
)
def test_extract_language_envelope_drift_raises(response):
    """Genuine drift in the mandatory settings envelope raises, not silent None.

    The envelope prefix (GET ``result[0][2]``) is structurally mandatory in
    every healthy response, so descent failure there is real schema drift and
    surfaces as :class:`UnknownRPCMethodError` rather than degrading to ``None``.
    """
    with pytest.raises(UnknownRPCMethodError):
        _extract_language(
            response,
            _GET_WIRE_PREFIX,
            _GET_WIRE_TAIL,
            method_id="ZwVcOc",
            source="test",
        )


@pytest.mark.parametrize(
    "response",
    [
        None,  # no payload at all
        [],  # empty SET envelope (no result[2])
        [None, [6, 500, 300, 500000]],  # truncated before the flags block at [2]
        42,  # non-subscriptable scalar payload
    ],
    ids=["none", "empty", "truncated-envelope", "scalar"],
)
def test_extract_language_set_prefix_envelope_drift_raises(response):
    """Drift in the SET envelope prefix (``result[2]``) raises, like the GET path.

    The SET response has a shorter mandatory prefix (``(2,)`` vs GET's
    ``(0, 2)``); this pins that its drift surfaces as a typed error too.
    """
    with pytest.raises(UnknownRPCMethodError):
        _extract_language(
            response,
            _SET_WIRE_PREFIX,
            _SET_WIRE_TAIL,
            method_id="hT54vc",
            source="test",
        )


@pytest.mark.asyncio
async def test_get_output_language_returns_code_from_wire_shape():
    from _fixtures.fake_core import make_fake_core

    core = make_fake_core(
        rpc_call=AsyncMock(return_value=_get_response([True, None, None, True, ["zh_Hans"]]))
    )
    api = SettingsAPI(core.rpc_executor)

    assert await api.get_output_language() == "zh_Hans"


@pytest.mark.asyncio
async def test_get_output_language_absent_language_returns_none():
    """End-to-end: a user with no language set gets ``None`` (no raise)."""
    from _fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock(return_value=_get_response([True, None, None, True])))
    api = SettingsAPI(core.rpc_executor)

    assert await api.get_output_language() is None


@pytest.mark.asyncio
async def test_get_output_language_envelope_drift_raises():
    """End-to-end: mandatory-envelope drift surfaces as a typed error."""
    from _fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock(return_value=[None]))
    api = SettingsAPI(core.rpc_executor)

    with pytest.raises(UnknownRPCMethodError):
        await api.get_output_language()


@pytest.mark.asyncio
async def test_set_output_language_returns_confirmed_code():
    from _fixtures.fake_core import make_fake_core

    core = make_fake_core(
        rpc_call=AsyncMock(return_value=_set_response([True, None, None, True, ["en"]]))
    )
    api = SettingsAPI(core.rpc_executor)

    assert await api.set_output_language("en") == "en"


@pytest.mark.asyncio
async def test_set_output_language_absent_confirmation_returns_none():
    """If the SET response omits the language slot, return ``None`` (no raise)."""
    from _fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock(return_value=_set_response([True, None, None, True])))
    api = SettingsAPI(core.rpc_executor)

    assert await api.set_output_language("en") is None
