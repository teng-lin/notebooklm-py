"""Android implementation of the public account-settings contract."""

from __future__ import annotations

from typing import Any, cast

from .._settings import SettingsAPI
from ..exceptions import DecodingError
from ..types import AccountLimits, UserSettings
from .session import AndroidSession

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_OR_CREATE_ACCOUNT_METHOD = f"/{_SERVICE}/GetOrCreateAccount"
MUTATE_ACCOUNT_METHOD = f"/{_SERVICE}/MutateAccount"


def _proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import account_pb2

    return cast(Any, account_pb2)


def _request_context() -> Any:
    from .upload import android_request_context

    return android_request_context()


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _non_negative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _decode_account(account: Any) -> UserSettings:
    language = None
    if account.HasField("user_info") and account.user_info.HasField("output_language"):
        language = account.user_info.output_language.language_code or None

    limits = AccountLimits()
    if account.HasField("tier_limits"):
        wire = account.tier_limits
        field_names = (
            "account_type",
            "max_projects",
            "max_sources_per_project",
            "max_words_per_source",
            "subscription_tier",
        )
        values = [getattr(wire, name) if wire.HasField(name) else None for name in field_names]
        while values:
            trailing = values.pop()
            if trailing is not None:
                values.append(trailing)
                break
        limits = AccountLimits(
            notebook_limit=(
                _non_negative_int(wire.max_projects) if wire.HasField("max_projects") else None
            ),
            source_limit=(
                _non_negative_int(wire.max_sources_per_project)
                if wire.HasField("max_sources_per_project")
                else None
            ),
            raw_limits=tuple(values),
            tier=(
                _positive_int(wire.subscription_tier)
                if wire.HasField("subscription_tier")
                else None
            ),
        )
    return UserSettings(limits=limits, output_language=language)


class AndroidSettingsAPI(SettingsAPI):
    """Native output-language and quota settings over the account RPCs."""

    def __init__(self, session: AndroidSession) -> None:
        self._transport = session

    async def _get_user_settings(self, *, expected_epoch: int) -> UserSettings:
        proto = _proto()
        response = await self._transport.unary(
            GET_OR_CREATE_ACCOUNT_METHOD,
            proto.GetOrCreateAccountRequest(request_context=_request_context()),
            replay_safe=False,
            response_type=proto.GetOrCreateAccountResponse,
            expected_epoch=expected_epoch,
        )
        if not response.HasField("account"):
            raise DecodingError(
                "Android GetOrCreateAccount response omitted account",
                method_id=GET_OR_CREATE_ACCOUNT_METHOD,
            )
        return _decode_account(response.account)

    async def set_output_language(self, language: str) -> str | None:
        if not language:
            return None
        proto = _proto()
        request = proto.MutateAccountRequest(
            mutations=[
                proto.AccountMutation(
                    change_property=proto.AccountMutation_ChangePropertyMutation(
                        new_user_info=proto.UserInfo(
                            output_language=proto.OutputLanguage(language_code=language)
                        )
                    )
                )
            ],
            request_context=_request_context(),
        )
        async with self._transport.operation_scope("settings.set_output_language") as lease:
            account = await self._transport.unary(
                MUTATE_ACCOUNT_METHOD,
                request,
                replay_safe=False,
                response_type=proto.Account,
                expected_epoch=lease.epoch,
            )
            return _decode_account(account).output_language

    async def get_user_settings(self) -> UserSettings:
        async with self._transport.operation_scope("settings.get_user_settings") as lease:
            return await self._get_user_settings(expected_epoch=lease.epoch)

    async def get_output_language(self) -> str | None:
        async with self._transport.operation_scope("settings.get_output_language") as lease:
            return (await self._get_user_settings(expected_epoch=lease.epoch)).output_language

    async def get_account_limits(self) -> AccountLimits:
        async with self._transport.operation_scope("settings.get_account_limits") as lease:
            return (await self._get_user_settings(expected_epoch=lease.epoch)).limits


__all__ = [
    "AndroidSettingsAPI",
    "GET_OR_CREATE_ACCOUNT_METHOD",
    "MUTATE_ACCOUNT_METHOD",
]
