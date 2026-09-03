"""Public-contract tests for native Android account settings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import pytest

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import account_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._android.settings import (
    GET_OR_CREATE_ACCOUNT_METHOD,
    MUTATE_ACCOUNT_METHOD,
    AndroidSettingsAPI,
    _decode_account,
)
from notebooklm._settings import SettingsAPI
from notebooklm.exceptions import DecodingError
from notebooklm.types import AccountLimits, UserSettings


@dataclass(frozen=True)
class _Lease:
    epoch: int


class _FakeSession:
    epoch = 17

    def __init__(self, *, omit_account: bool = False) -> None:
        self.language = "fr"
        self.omit_account = omit_account
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.operation_scopes: list[tuple[str, int | None]] = []

    @asynccontextmanager
    async def operation_scope(
        self,
        label: str,
        *,
        expected_epoch: int | None = None,
    ) -> AsyncIterator[_Lease]:
        self.operation_scopes.append((label, expected_epoch))
        yield _Lease(self.epoch)

    def _account(self) -> account_pb2.Account:
        return account_pb2.Account(
            tier_limits=account_pb2.TierLimits(
                account_type=6,
                max_projects=500,
                max_sources_per_project=300,
                max_words_per_source=500_000,
                subscription_tier=2,
            ),
            user_info=account_pb2.UserInfo(
                output_language=account_pb2.OutputLanguage(language_code=self.language)
            ),
        )

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        assert kwargs["expected_epoch"] == self.epoch
        assert request.HasField("request_context")
        if method == GET_OR_CREATE_ACCOUNT_METHOD:
            assert kwargs["replay_safe"] is False
            assert kwargs["response_type"] is account_pb2.GetOrCreateAccountResponse
            if self.omit_account:
                return account_pb2.GetOrCreateAccountResponse()
            return account_pb2.GetOrCreateAccountResponse(account=self._account())
        if method == MUTATE_ACCOUNT_METHOD:
            assert kwargs["replay_safe"] is False
            assert kwargs["response_type"] is account_pb2.Account
            (mutation,) = request.mutations
            assert mutation.WhichOneof("mutation") == "change_property"
            self.language = mutation.change_property.new_user_info.output_language.language_code
            return self._account()
        raise AssertionError(f"unexpected method: {method}")


def _api(server: _FakeSession) -> AndroidSettingsAPI:
    return AndroidSettingsAPI(cast(AndroidSession, server))


def test_android_settings_is_complete_without_opening_transport() -> None:
    server = _FakeSession()
    api = _api(server)
    assert isinstance(api, SettingsAPI)
    assert AndroidSettingsAPI.__abstractmethods__ == frozenset()
    assert server.calls == []


async def test_all_settings_reads_are_native_and_preserve_complete_semantics() -> None:
    server = _FakeSession()
    api = _api(server)
    limits = AccountLimits(
        notebook_limit=500,
        source_limit=300,
        raw_limits=(6, 500, 300, 500_000, 2),
        tier=2,
    )
    expected = UserSettings(limits=limits, output_language="fr")

    assert await api.get_user_settings() == expected
    assert await api.get_output_language() == "fr"
    assert await api.get_account_limits() == limits
    assert [method for method, _request, _kwargs in server.calls] == [
        GET_OR_CREATE_ACCOUNT_METHOD,
        GET_OR_CREATE_ACCOUNT_METHOD,
        GET_OR_CREATE_ACCOUNT_METHOD,
    ]
    assert server.operation_scopes == [
        ("settings.get_user_settings", None),
        ("settings.get_output_language", None),
        ("settings.get_account_limits", None),
    ]


async def test_set_output_language_uses_exact_nested_mutation_and_response() -> None:
    server = _FakeSession()
    api = _api(server)

    assert await api.set_output_language("ja") == "ja"
    assert server.language == "ja"
    assert server.operation_scopes == [("settings.set_output_language", None)]
    method, request, kwargs = server.calls[0]
    assert method == MUTATE_ACCOUNT_METHOD
    assert request.mutations[0].change_property.new_user_info.output_language.language_code == "ja"
    assert kwargs["expected_epoch"] == 17


async def test_empty_language_is_a_noop_and_missing_account_is_drift() -> None:
    server = _FakeSession()
    api = _api(server)
    assert await api.set_output_language("") is None
    assert server.calls == []
    assert server.operation_scopes == []

    with pytest.raises(DecodingError, match="omitted account"):
        await _api(_FakeSession(omit_account=True)).get_user_settings()


def test_explicit_zero_limits_remain_distinct_from_absent_limits() -> None:
    absent = _decode_account(account_pb2.Account(tier_limits=account_pb2.TierLimits()))
    account = account_pb2.Account(
        tier_limits=account_pb2.TierLimits(
            account_type=0,
            max_projects=0,
            max_sources_per_project=0,
            max_words_per_source=0,
            subscription_tier=0,
        )
    )

    assert account.tier_limits.HasField("max_projects")
    assert account.tier_limits.HasField("max_sources_per_project")
    decoded = _decode_account(account)

    assert absent.limits == AccountLimits(raw_limits=())
    assert decoded.limits == AccountLimits(
        notebook_limit=0,
        source_limit=0,
        raw_limits=(0, 0, 0, 0, 0),
        tier=None,
    )
