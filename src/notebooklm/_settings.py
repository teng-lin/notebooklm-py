"""User settings API."""

import logging

from ._core import ClientCore
from .rpc import RPCMethod

logger = logging.getLogger(__name__)


class SettingsAPI:
    """Operations on NotebookLM user settings.

    Provides methods for managing global user settings like output language.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            lang = await client.settings.get_output_language()
            await client.settings.set_output_language("zh_Hans")
    """

    def __init__(self, core: ClientCore):
        """Initialize the settings API.

        Args:
            core: The core client infrastructure.
        """
        self._core = core

    async def set_output_language(self, language: str) -> str | None:
        """Set the output language for artifact generation.

        This is a global setting that affects all notebooks in your account.

        Args:
            language: Language code (e.g., "en", "zh_Hans", "ja").
                     Use empty string "" to read current setting without changing.

        Returns:
            The current language setting after the call, or None if not set.
        """
        logger.debug("Setting output language: %s", language)

        # Params structure from RPC capture:
        # [[[null,[[null,null,null,null,["language_code"]]]]]]
        params = [[[None, [[None, None, None, None, [language]]]]]]

        result = await self._core.rpc_call(
            RPCMethod.SET_OUTPUT_LANGUAGE,
            params,
            source_path="/",  # Use root path for global setting
        )

        # Response structure from discovery doc:
        # [null, [limits], [True, null, null, True, ["zh_Hans"]], ...]
        # Language is at response[2][4][0]
        current_language = self._extract_language_from_response(result)
        if current_language:
            logger.debug("Output language is now: %s", current_language)
        else:
            logger.debug("Could not parse language from response")
        return current_language

    def _extract_language_from_response(self, result: list | None) -> str | None:
        """Extract language code from RPC response.

        Expected structure: result[2][4][0] contains the language code.
        Returns None if structure doesn't match (logs at debug level for debugging).
        """
        try:
            # The API can return an empty string for the language code, which we treat as None.
            lang_code = result[2][4][0]  # type: ignore[index]
            return lang_code or None
        except (TypeError, IndexError):
            logger.debug(
                "Could not parse language from response due to unexpected structure: %s",
                result,
            )
            return None

    async def get_output_language(self) -> str | None:
        """Get the current output language setting.

        Returns:
            The current language code, or None if not set (defaults to "en").
        """
        # Call with empty string to read without changing
        return await self.set_output_language("")
