"""User settings API."""

import logging

from ._backend import BackendAdapter
from ._backend_compat import project_backend_call
from ._projectors import project_account_limits, project_user_settings
from ._settings_service import SettingsService
from .types import AccountLimits, UserSettings

logger = logging.getLogger(__name__)


class SettingsAPI:
    """Operations on NotebookLM user settings.

    Provides methods for managing global user settings like output language.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            lang = await client.settings.get_output_language()
            await client.settings.set_output_language("zh_Hans")
    """

    def __init__(self, _backend: BackendAdapter) -> None:
        """Initialize the settings API from the client-owned semantic backend."""
        self._service = SettingsService(_backend)

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
        current_language = await project_backend_call(self._service.set_output_language(language))
        self._log_language_result(current_language, "Output language is now")
        return current_language

    async def get_user_settings(self) -> UserSettings:
        """Fetch user settings once, returning both limits and output language.

        A single ``GET_USER_SETTINGS`` response carries both payloads, so callers
        that need both (e.g. MCP ``server_info``) should use this instead of
        firing ``get_account_limits`` and ``get_output_language`` separately.

        Returns:
            UserSettings with parsed account limits and output language.
        """
        logger.debug("Fetching user settings")
        settings = project_user_settings(
            await project_backend_call(self._service.get_user_settings())
        )
        self._log_limits(settings.limits)
        self._log_language_result(settings.output_language, "Current output language")
        return settings

    async def get_output_language(self) -> str | None:
        """Get the current output language setting.

        Fetches user settings from the server and extracts the language code.

        Returns:
            The current language code (e.g., "en", "ja", "zh_Hans"),
            or None if not set or couldn't be parsed.
        """
        logger.debug("Fetching user settings")
        language = await project_backend_call(self._service.get_output_language())
        self._log_language_result(language, "Current output language")
        return language

    async def get_account_limits(self) -> AccountLimits:
        """Get account-level limits advertised by NotebookLM user settings.

        Returns:
            AccountLimits with parsed notebook/source limits when present.
        """
        logger.debug("Fetching user settings")
        limits = project_account_limits(
            await project_backend_call(self._service.get_account_limits())
        )
        self._log_limits(limits)
        return limits

    @staticmethod
    def _log_limits(limits: AccountLimits) -> None:
        if limits.notebook_limit is not None:
            logger.debug("Notebook limit from user settings: %s", limits.notebook_limit)
        else:
            logger.debug("Could not parse account limits from response")

    @staticmethod
    def _log_language_result(language: str | None, success_prefix: str) -> None:
        """Log the result of a language operation."""
        if language:
            logger.debug("%s: %s", success_prefix, language)
        else:
            logger.debug("Could not parse language from response")
