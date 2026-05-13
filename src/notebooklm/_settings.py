"""User settings API."""

import logging
from collections.abc import Sequence
from typing import Any

from ._core import ClientCore
from .rpc import RPCMethod
from .types import AccountLimits, AccountTier

logger = logging.getLogger(__name__)

_ACCOUNT_LIMITS_PATH = (0, 1)
_NOTEBOOK_LIMIT_INDEX = 1
_SOURCE_LIMIT_INDEX = 2
_TIER_PREFIX = "NOTEBOOKLM_TIER_"
_TIER_PLAN_NAMES = {
    "NOTEBOOKLM_TIER_STANDARD": "Standard",
    "NOTEBOOKLM_TIER_PLUS": "Google AI Plus",
    "NOTEBOOKLM_TIER_PRO": "Google AI Pro",
    "NOTEBOOKLM_TIER_PRO_DASHER_END_USER": "Google Workspace Pro",
    "NOTEBOOKLM_TIER_ULTRA": "Google AI Ultra",
}


def build_get_user_settings_params() -> list[Any]:
    """Build GET_USER_SETTINGS params without sharing a mutable list."""
    return [
        None,
        [1, None, None, None, None, None, None, None, None, None, [1]],
    ]


def build_get_user_tier_params() -> list[Any]:
    """Build GET_USER_TIER params for the NotebookLM homepage context."""
    return [
        [
            [
                [None, "1", 627],
                [None, None, None, None, None, None, None, None, None, [None, None, 2]],
                1,
            ]
        ]
    ]


def _extract_nested_value(data: list | None, path: Sequence[int]) -> str | None:
    """Extract a value from nested lists by following an index path.

    Args:
        data: The nested list structure to extract from.
        path: Sequence of indices to follow (e.g., [2, 4, 0] for data[2][4][0]).

    Returns:
        The extracted string value, or None if the path is invalid or value is empty.
    """
    try:
        result = data
        for idx in path:
            result = result[idx]  # type: ignore[index]
        return result or None  # type: ignore[return-value]
    except (TypeError, IndexError):
        return None


def _extract_nested_list(data: list | None, path: Sequence[int]) -> list[Any] | None:
    """Extract a nested list by following an index path."""
    result: Any = data
    try:
        for idx in path:
            if not isinstance(result, list):
                return None
            result = result[idx]
    except IndexError:
        return None
    return result if isinstance(result, list) else None


def _positive_int(value: Any) -> int | None:
    """Return value only when it is a positive int, excluding bools."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def extract_account_limits(data: list | None) -> AccountLimits:
    """Extract account-level limits from GET_USER_SETTINGS response data."""
    limits = _extract_nested_list(data, _ACCOUNT_LIMITS_PATH)
    if limits is None:
        return AccountLimits()

    raw_limits = tuple(limits)
    notebook_limit = (
        _positive_int(limits[_NOTEBOOK_LIMIT_INDEX])
        if len(limits) > _NOTEBOOK_LIMIT_INDEX
        else None
    )
    source_limit = (
        _positive_int(limits[_SOURCE_LIMIT_INDEX]) if len(limits) > _SOURCE_LIMIT_INDEX else None
    )
    return AccountLimits(
        notebook_limit=notebook_limit,
        source_limit=source_limit,
        raw_limits=raw_limits,
    )


def _find_tier_string(value: Any) -> str | None:
    """Find the first NotebookLM tier string in a nested response."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str) and item.startswith(_TIER_PREFIX):
            return item
        if isinstance(item, list):
            stack.extend(reversed(item))
    return None


def extract_account_tier(data: list | None) -> AccountTier:
    """Extract the NotebookLM subscription tier from GET_USER_TIER response data."""
    tier = _find_tier_string(data)
    return AccountTier(tier=tier, plan_name=_TIER_PLAN_NAMES.get(tier) if tier else None)


class SettingsAPI:
    """Operations on NotebookLM user settings.

    Provides methods for managing global user settings like output language.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            lang = await client.settings.get_output_language()
            await client.settings.set_output_language("zh_Hans")
    """

    # Response paths for extracting language code from different RPC responses
    _SET_LANGUAGE_PATH = (2, 4, 0)  # result[2][4][0]
    _GET_SETTINGS_PATH = (0, 2, 4, 0)  # result[0][2][4][0]

    def __init__(self, core: ClientCore) -> None:
        """Initialize the settings API.

        Args:
            core: The core client infrastructure.
        """
        self._core = core

    async def set_output_language(self, language: str) -> str | None:
        """Set the output language for artifact generation.

        This is a global setting that affects all notebooks in your account.

        Note: Use get_output_language() to read the current setting.
        Empty strings are rejected (they would reset to default, not read current).

        Args:
            language: Language code (e.g., "en", "zh_Hans", "ja").
                     Must be a non-empty valid language code.

        Returns:
            The language that was set, or None if the response couldn't be parsed.
        """
        if not language:
            logger.warning(
                "Empty string not supported - use get_output_language() to read the current setting. "
                "Passing empty string to the API would reset the language to default, not read it."
            )
            return None

        logger.debug("Setting output language: %s", language)

        # Params structure: [[[null,[[null,null,null,null,["language_code"]]]]]]
        params = [[[None, [[None, None, None, None, [language]]]]]]

        result = await self._core.rpc_call(
            RPCMethod.SET_USER_SETTINGS,
            params,
            source_path="/",
        )

        current_language = _extract_nested_value(result, self._SET_LANGUAGE_PATH)
        self._log_language_result(current_language, "Output language is now")
        return current_language

    async def get_output_language(self) -> str | None:
        """Get the current output language setting.

        Fetches user settings from the server and extracts the language code.

        Returns:
            The current language code (e.g., "en", "ja", "zh_Hans"),
            or None if not set or couldn't be parsed.
        """
        logger.debug("Fetching user settings to get output language")

        result = await self._core.rpc_call(
            RPCMethod.GET_USER_SETTINGS,
            build_get_user_settings_params(),
            source_path="/",
        )

        current_language = _extract_nested_value(result, self._GET_SETTINGS_PATH)
        self._log_language_result(current_language, "Current output language")
        return current_language

    async def get_account_limits(self) -> AccountLimits:
        """Get account-level limits advertised by NotebookLM user settings.

        Returns:
            AccountLimits with parsed notebook/source limits when present.
        """
        logger.debug("Fetching user settings to get account limits")

        result = await self._core.rpc_call(
            RPCMethod.GET_USER_SETTINGS,
            build_get_user_settings_params(),
            source_path="/",
        )

        limits = extract_account_limits(result)
        if limits.notebook_limit is not None:
            logger.debug("Notebook limit from user settings: %s", limits.notebook_limit)
        else:
            logger.debug("Could not parse account limits from response")
        return limits

    async def get_account_tier(self) -> AccountTier:
        """Get the NotebookLM subscription tier for the current account.

        Returns:
            AccountTier with the raw tier string and a friendly plan name when known.
        """
        logger.debug("Fetching user tier")

        result = await self._core.rpc_call(
            RPCMethod.GET_USER_TIER,
            build_get_user_tier_params(),
            source_path="/",
        )

        tier = extract_account_tier(result)
        if tier.tier:
            logger.debug("NotebookLM account tier: %s", tier.tier)
        else:
            logger.debug("Could not parse account tier from response")
        return tier

    def _log_language_result(self, language: str | None, success_prefix: str) -> None:
        """Log the result of a language operation."""
        if language:
            logger.debug("%s: %s", success_prefix, language)
        else:
            logger.debug("Could not parse language from response")
