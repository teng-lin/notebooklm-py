"""Public-contract tests for Android-selected user settings."""

from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from notebooklm._android.settings import AndroidSettingsAPI
from notebooklm._settings import SettingsAPI
from notebooklm.types import AccountLimits, UserSettings


def _api() -> tuple[AndroidSettingsAPI, AsyncMock]:
    compatibility = AsyncMock(spec=SettingsAPI)
    return AndroidSettingsAPI(compatibility), compatibility


def test_android_settings_is_complete_without_opening_android_transport() -> None:
    api, compatibility = _api()

    assert isinstance(api, SettingsAPI)
    assert AndroidSettingsAPI.__abstractmethods__ == frozenset()
    assert compatibility.mock_calls == []


@pytest.mark.asyncio
async def test_android_settings_preserves_complete_compatibility_semantics() -> None:
    api, compatibility = _api()
    limits = AccountLimits(
        notebook_limit=500,
        source_limit=300,
        raw_limits=(6, 500, 300, 500000, 2),
        tier=2,
    )
    user_settings = UserSettings(limits=limits, output_language="fr")
    compatibility.set_output_language.return_value = "ja"
    compatibility.get_user_settings.return_value = user_settings
    compatibility.get_output_language.return_value = "fr"
    compatibility.get_account_limits.return_value = limits

    assert await api.set_output_language("ja") == "ja"
    assert await api.get_user_settings() == user_settings
    assert await api.get_output_language() == "fr"
    assert await api.get_account_limits() == limits

    assert compatibility.method_calls == [
        call.set_output_language("ja"),
        call.get_user_settings(),
        call.get_output_language(),
        call.get_account_limits(),
    ]


@pytest.mark.asyncio
async def test_android_settings_preserves_empty_language_behavior() -> None:
    api, compatibility = _api()
    compatibility.set_output_language.return_value = None

    assert await api.set_output_language("") is None
    compatibility.set_output_language.assert_awaited_once_with("")
