"""Android-selected user settings with an evidence-bounded compatibility seam."""

from __future__ import annotations

from .._settings import SettingsAPI
from ..types import AccountLimits, UserSettings


class AndroidSettingsAPI(SettingsAPI):
    """Expose the complete settings contract under Android selection.

    The recovered Android ``MutateAccount`` descriptor only exposes account
    consent flags. It does not name the output-language mutation or a complete
    account-limits projection. Delegating this account-scoped namespace keeps
    the public semantics exact without inventing protobuf fields or FQNs.
    """

    def __init__(self, compatibility: SettingsAPI) -> None:
        self._compatibility = compatibility

    async def set_output_language(self, language: str) -> str | None:
        return await self._compatibility.set_output_language(language)

    async def get_user_settings(self) -> UserSettings:
        return await self._compatibility.get_user_settings()

    async def get_output_language(self) -> str | None:
        return await self._compatibility.get_output_language()

    async def get_account_limits(self) -> AccountLimits:
        return await self._compatibility.get_account_limits()


__all__ = ["AndroidSettingsAPI"]
