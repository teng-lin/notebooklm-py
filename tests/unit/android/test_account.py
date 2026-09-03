"""Exact schema, strict projection, and lifecycle tests for Android account bootstrap."""

from __future__ import annotations

import ast
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from google.protobuf.descriptor import FieldDescriptor

import notebooklm
from notebooklm import _client_assembly
from notebooklm._android.account import (
    GET_OR_CREATE_ACCOUNT_METHOD,
    AndroidAccountAPI,
)
from notebooklm._android.codecs.account import AndroidAccount, decode_account
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    account_pb2,
)
from notebooklm._android.session import AndroidSession
from notebooklm.exceptions import DecodingError

ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"


def _fields(message_type: type[Any]) -> dict[str, tuple[int, int, str | None]]:
    return {
        field.name: (
            field.number,
            field.type,
            None if field.message_type is None else field.message_type.full_name,
        )
        for field in message_type.DESCRIPTOR.fields
    }


def test_exact_account_proto_package_fields_and_wire_are_minimal() -> None:
    boolean = FieldDescriptor.TYPE_BOOL
    int32 = FieldDescriptor.TYPE_INT32
    string = FieldDescriptor.TYPE_STRING
    message = FieldDescriptor.TYPE_MESSAGE
    package = ORCHESTRATION_PACKAGE

    assert account_pb2.DESCRIPTOR.package == package
    assert [dependency.name for dependency in account_pb2.DESCRIPTOR.dependencies] == [
        "labs/language/tailwind/common/protos/metadata.proto"
    ]
    assert account_pb2.DESCRIPTOR.services_by_name == {}
    assert set(account_pb2.DESCRIPTOR.message_types_by_name) == {
        "UserInfo",
        "OutputLanguage",
        "PremiumUserInfo",
        "TierLimits",
        "Account",
        "GetOrCreateAccountRequest",
        "GetOrCreateAccountResponse",
        "AccountMutation_ChangePropertyMutation",
        "AccountMutation",
        "MutateAccountRequest",
    }
    assert _fields(account_pb2.OutputLanguage) == {"language_code": (1, string, None)}
    assert _fields(account_pb2.UserInfo) == {
        "accepted_tos": (1, boolean, None),
        "opted_in_to_marketing_emails": (4, boolean, None),
        "output_language": (5, message, f"{package}.OutputLanguage"),
        "is_eea_user": (9, boolean, None),
    }
    assert _fields(account_pb2.PremiumUserInfo) == {"is_premium_user": (1, boolean, None)}
    assert _fields(account_pb2.TierLimits) == {
        "account_type": (1, int32, None),
        "max_projects": (2, int32, None),
        "max_sources_per_project": (3, int32, None),
        "max_words_per_source": (4, int32, None),
        "subscription_tier": (5, int32, None),
    }
    assert _fields(account_pb2.Account) == {
        "tier_limits": (2, message, f"{package}.TierLimits"),
        "user_info": (3, message, f"{package}.UserInfo"),
        "premium_user_info": (5, message, f"{package}.PremiumUserInfo"),
    }
    assert _fields(account_pb2.GetOrCreateAccountRequest) == {
        "request_context": (
            1,
            message,
            "labs.language.tailwind.common.protos.RequestContext",
        )
    }
    assert _fields(account_pb2.GetOrCreateAccountResponse) == {
        "account": (1, message, f"{package}.Account")
    }

    assert account_pb2.GetOrCreateAccountRequest().SerializeToString() == b""
    assert _fields(account_pb2.AccountMutation_ChangePropertyMutation) == {
        "new_user_info": (1, message, f"{package}.UserInfo")
    }
    assert _fields(account_pb2.AccountMutation) == {
        "change_property": (
            2,
            message,
            f"{package}.AccountMutation_ChangePropertyMutation",
        )
    }
    assert list(account_pb2.AccountMutation.DESCRIPTOR.oneofs_by_name) == ["mutation"]
    assert _fields(account_pb2.MutateAccountRequest) == {
        "mutations": (1, message, f"{package}.AccountMutation"),
        "request_context": (
            2,
            message,
            "labs.language.tailwind.common.protos.RequestContext",
        ),
    }
    mutation = account_pb2.MutateAccountRequest(
        mutations=[
            account_pb2.AccountMutation(
                change_property=account_pb2.AccountMutation_ChangePropertyMutation(
                    new_user_info=account_pb2.UserInfo(
                        output_language=account_pb2.OutputLanguage(language_code="ja")
                    )
                )
            )
        ]
    )
    assert mutation.SerializeToString().hex() == "0a0a12080a062a040a026a61"
    response = account_pb2.GetOrCreateAccountResponse(
        account=account_pb2.Account(
            user_info=account_pb2.UserInfo(
                accepted_tos=True,
                opted_in_to_marketing_emails=True,
                is_eea_user=True,
            ),
            premium_user_info=account_pb2.PremiumUserInfo(is_premium_user=True),
        )
    )
    assert response.SerializeToString().hex() == "0a0c1a060801200148012a020801"


def test_projection_is_frozen_and_preserves_false_flags() -> None:
    response = account_pb2.GetOrCreateAccountResponse(
        account=account_pb2.Account(
            user_info=account_pb2.UserInfo(),
            premium_user_info=account_pb2.PremiumUserInfo(),
        )
    )
    projected = decode_account(response, method_id=GET_OR_CREATE_ACCOUNT_METHOD)
    assert projected == AndroidAccount(
        accepted_tos=False,
        opted_in_to_marketing_emails=False,
        is_eea_user=False,
        is_premium_user=False,
    )
    with pytest.raises(FrozenInstanceError):
        projected.accepted_tos = True  # type: ignore[misc]


@pytest.mark.parametrize("missing", ["account", "user_info", "premium_user_info"])
def test_projection_rejects_absent_required_message_blocks(missing: str) -> None:
    account = account_pb2.Account()
    if missing != "user_info":
        account.user_info.SetInParent()
    if missing != "premium_user_info":
        account.premium_user_info.SetInParent()
    response = account_pb2.GetOrCreateAccountResponse()
    if missing != "account":
        response.account.CopyFrom(account)

    with pytest.raises(DecodingError) as raised:
        decode_account(response, method_id=GET_OR_CREATE_ACCOUNT_METHOD)
    assert raised.value.method_id == GET_OR_CREATE_ACCOUNT_METHOD


@dataclass(frozen=True)
class _Lease:
    epoch: int


class _FakeSession:
    epoch = 37

    def __init__(self, response: Any) -> None:
        self.response = response
        self.operation_scopes: list[tuple[str, int | None]] = []
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    @asynccontextmanager
    async def operation_scope(
        self,
        label: str,
        *,
        expected_epoch: int | None = None,
    ) -> AsyncIterator[_Lease]:
        self.operation_scopes.append((label, expected_epoch))
        yield _Lease(self.epoch)

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        return self.response


async def test_adapter_uses_one_outer_epoch_lease_and_conservative_non_replay() -> None:
    response = account_pb2.GetOrCreateAccountResponse(
        account=account_pb2.Account(
            user_info=account_pb2.UserInfo(accepted_tos=True, is_eea_user=True),
            premium_user_info=account_pb2.PremiumUserInfo(is_premium_user=True),
        )
    )
    session = _FakeSession(response)
    result = await AndroidAccountAPI(cast(AndroidSession, session)).get_or_create_account()

    assert result == AndroidAccount(
        accepted_tos=True,
        opted_in_to_marketing_emails=False,
        is_eea_user=True,
        is_premium_user=True,
    )
    assert session.operation_scopes == [("account.get_or_create_account", None)]
    assert len(session.calls) == 1
    method, request, kwargs = session.calls[0]
    assert method == GET_OR_CREATE_ACCOUNT_METHOD
    assert request.HasField("request_context")
    assert kwargs == {
        "replay_safe": False,
        "response_type": account_pb2.GetOrCreateAccountResponse,
        "expected_epoch": session.epoch,
    }


def test_adapter_import_is_protobuf_lazy_and_remains_outside_public_assembly() -> None:
    root = Path(__file__).resolve().parents[3]
    tree = ast.parse((root / "src/notebooklm/_android/account.py").read_text(encoding="utf-8"))
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    rendered = " ".join(ast.unparse(node) for node in imports)
    assert "google.protobuf" not in rendered
    assert "._android.proto" not in rendered
    assert "_pb2" not in rendered

    assert inspect.iscoroutinefunction(AndroidAccountAPI.get_or_create_account)
    assert "AndroidAccountAPI" not in vars(notebooklm)
    assert "AndroidAccountAPI" not in vars(_client_assembly)
    assembly_tree = ast.parse(inspect.getsource(_client_assembly._assemble_client))
    assert not any(
        isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and node.attr == "account"
        for node in ast.walk(assembly_tree)
    )
