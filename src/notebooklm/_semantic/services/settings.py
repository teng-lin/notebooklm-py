"""Transport-neutral semantic service for settings and account limits."""

from __future__ import annotations

from typing import cast

from ..._deadline import RuntimeDeadline
from ..backend import BackendAdapter
from ..records import (
    SETTINGS_GET_DEF,
    SETTINGS_GET_LIMITS_DEF,
    SETTINGS_SET_LANGUAGE_DEF,
    AccountLimitsRecord,
    SettingsGetInput,
    SettingsGetLimitsInput,
    SettingsSetLanguageInput,
    UserSettingsRecord,
)


class SettingsService:
    """Invoke typed account operations and return their neutral records.

    Neutral per P10 invariant I1: the record to public-model projection is
    :class:`~notebooklm._settings.SettingsAPI`'s job, so nothing here names
    ``_projectors`` or ``notebooklm.types``.
    """

    __slots__ = ("_backend",)

    def __init__(self, backend: BackendAdapter) -> None:
        self._backend = backend

    async def get_user_settings(
        self,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> UserSettingsRecord:
        result = await self._backend.invoke(
            SETTINGS_GET_DEF,
            SettingsGetInput(),
            deadline=deadline,
        )
        return result.settings

    async def get_output_language(
        self,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> str | None:
        result = await self._backend.invoke(
            SETTINGS_GET_DEF,
            SettingsGetInput(),
            deadline=deadline,
        )
        return cast(str | None, result.settings.output_language)

    async def get_account_limits(
        self,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> AccountLimitsRecord:
        result = await self._backend.invoke(
            SETTINGS_GET_LIMITS_DEF,
            SettingsGetLimitsInput(),
            deadline=deadline,
        )
        return result.limits

    async def set_output_language(
        self,
        language: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> str | None:
        result = await self._backend.invoke(
            SETTINGS_SET_LANGUAGE_DEF,
            SettingsSetLanguageInput(language),
            deadline=deadline,
        )
        return cast(str | None, result.output_language)


__all__ = ["SettingsService"]
