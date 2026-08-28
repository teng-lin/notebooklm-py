"""Backend-neutral user-settings namespace contract."""

from abc import ABC, abstractmethod

from .types import AccountLimits, UserSettings


class SettingsAPI(ABC):
    """Operations on NotebookLM user settings."""

    @abstractmethod
    async def set_output_language(self, language: str) -> str | None:
        """Set the account's output language."""

    @abstractmethod
    async def get_user_settings(self) -> UserSettings:
        """Fetch account limits and output language in one request."""

    @abstractmethod
    async def get_output_language(self) -> str | None:
        """Fetch the current output language in its own request."""

    @abstractmethod
    async def get_account_limits(self) -> AccountLimits:
        """Fetch account limits in their own request."""


__all__ = ["SettingsAPI"]
