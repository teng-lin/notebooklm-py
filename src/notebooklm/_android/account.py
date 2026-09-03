"""Private Android adapter for the exact account bootstrap RPC."""

from __future__ import annotations

from typing import Any, cast

from .codecs.account import AndroidAccount, decode_account
from .session import AndroidSession

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_OR_CREATE_ACCOUNT_METHOD = f"/{_SERVICE}/GetOrCreateAccount"


def _proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import account_pb2

    return cast(Any, account_pb2)


def _request_context() -> Any:
    from .upload import android_request_context

    return android_request_context()


class AndroidAccountAPI:
    """Direct-test-only account bootstrap adapter; not a public client namespace."""

    def __init__(self, session: AndroidSession) -> None:
        self._transport = session

    async def get_or_create_account(self) -> AndroidAccount:
        """Return the exact account flags without replaying a possibly creating call."""

        proto = _proto()
        async with self._transport.operation_scope("account.get_or_create_account") as lease:
            response = await self._transport.unary(
                GET_OR_CREATE_ACCOUNT_METHOD,
                proto.GetOrCreateAccountRequest(request_context=_request_context()),
                replay_safe=False,
                response_type=proto.GetOrCreateAccountResponse,
                expected_epoch=lease.epoch,
            )
            return decode_account(response, method_id=GET_OR_CREATE_ACCOUNT_METHOD)


__all__ = ["AndroidAccountAPI", "GET_OR_CREATE_ACCOUNT_METHOD"]
