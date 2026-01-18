"""User settings API."""

import logging
from collections.abc import Sequence

from ._core import ClientCore
from .rpc import RPCMethod

logger = logging.getLogger(__name__)


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

        # Params structure: [null,[1,null,null,null,null,null,null,null,null,null,[1]]]
        params = [None, [1, None, None, None, None, None, None, None, None, None, [1]]]

        result = await self._core.rpc_call(
            RPCMethod.GET_USER_SETTINGS,
            params,
            source_path="/",
        )

        current_language = _extract_nested_value(result, self._GET_SETTINGS_PATH)
        self._log_language_result(current_language, "Current output language")
        return current_language

    def _log_language_result(self, language: str | None, success_prefix: str) -> None:
        """Log the result of a language operation."""
        if language:
            logger.debug("%s: %s", success_prefix, language)
        else:
            logger.debug("Could not parse language from response")
