"""Backend-neutral user-settings namespace contract."""

import contextlib
from abc import ABC, abstractmethod

from ._runtime.call_supervisor import OperationLease
from ._usage import RawUsageSummary, UsageAccount, decode_usage_summary
from .types import AccountLimits, UsageSummary, UsageSummaryStatus, UserSettings


class SettingsAPI(ABC):
    """Operations on NotebookLM user settings."""

    @abstractmethod
    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease | None]:
        """Return the backend's scope for one multi-call workflow."""
        raise NotImplementedError

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

    async def get_usage(self) -> UsageSummary:
        """Fetch the live compute-meter snapshot when the account enables it.

        The account-owned eligibility bit is authoritative.  A disabled account
        avoids the quota-summary RPC entirely; eligible accounts delegate
        response validation and public projection to the neutral decoder.
        """
        async with self._operation_scope("settings.get_usage") as lease:
            account = await self._get_usage_account(lease=lease)
            if not account.compute_metering_enabled:
                return UsageSummary(status=UsageSummaryStatus.DISABLED)
            return decode_usage_summary(await self._list_quota_summary(lease=lease))

    @abstractmethod
    async def _get_usage_account(self, *, lease: OperationLease | None) -> UsageAccount:
        """Read the account meter-eligibility bit for :meth:`get_usage`."""

    @abstractmethod
    async def _list_quota_summary(self, *, lease: OperationLease | None) -> RawUsageSummary:
        """Read a presence-aware decoded ``ListQuotaSummary`` response."""


__all__ = ["SettingsAPI"]
