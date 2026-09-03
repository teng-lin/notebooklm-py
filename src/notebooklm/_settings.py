"""Backend-neutral user-settings namespace contract."""

import contextlib
from abc import ABC, abstractmethod

from ._runtime.call_supervisor import OperationLease
from .types import AccountLimits, UserSettings


class SettingsAPI(ABC):
    """Operations on NotebookLM user settings."""

    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease | None]:
        """Return the backend's scope for one multi-call workflow."""

        return contextlib.nullcontext(None)

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
