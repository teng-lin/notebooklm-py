"""Migration sentinels for the P6.5 semantic sharing slice.

The execution authority moved to ``WebRpcBackend`` + ``SharingService``, but the
public signatures, the position-sensitive ``SHARE_NOTEBOOK`` / ``MutateProject``
payloads, the per-call RPC inventory, the individual-user validation rules, and
the exact ``ShareStatus`` projection are facade contracts. A migration PR that
changes a row below without changing this file is out of contract.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._operations import CallPolicy, Operation
from notebooklm._semantic.projectors import (
    _SHARE_ACCESS,
    _SHARE_PERMISSIONS,
    _SHARE_VIEW_LEVELS,
    project_share_status,
)
from notebooklm._semantic.records import (
    LEGACY_SHARE_ARTIFACT_DEF,
    SHARING_GET_DEF,
    SHARING_PATCH_VIEW_LEVEL_DEF,
    SHARING_SET_PUBLIC_DEF,
    SHARING_SET_VIEW_LEVEL_DEF,
    SHARING_UPDATE_USERS_DEF,
    LegacyShareArtifactInput,
    LegacyShareArtifactResult,
    ShareAccessLevel,
    SharePermissionLevel,
    ShareStatusRecord,
    ShareViewScope,
)
from notebooklm._semantic.services.sharing import SharingService
from notebooklm._sharing import _PERMISSION_LEVELS, _VIEW_SCOPES, SharingAPI
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import primitives as primitive_rows
from notebooklm._web.bindings import sharing as sharing_rows
from notebooklm._web.codec.sharing import decode_share_status
from notebooklm._web.registry import (
    WEB_OPERATION_REGISTRY,
    WEB_SERVICE_OWNED_OPERATIONS,
    WEB_SUPPORTED_OPERATIONS,
)
from notebooklm.exceptions import RPCError, ServerError
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import ShareAccess, SharePermission, ShareViewLevel

SHARE_STATUS_PAYLOAD: list[Any] = [
    [
        ["owner@example.com", 1, [], ["Owner", "https://avatar/owner"]],
        ["viewer@example.com", 3, [], ["Viewer", "https://avatar/viewer"]],
    ],
    [True],
    1000,
    True,
]


def _api(rpc_call: AsyncMock) -> SharingAPI:
    executor = MagicMock(rpc_call=rpc_call)
    backend = WebRpcBackend(executor)
    return SharingAPI(_backend=backend)


def _methods(rpc_call: AsyncMock) -> list[RPCMethod]:
    return [item.args[0] for item in rpc_call.await_args_list]


def test_sharing_public_signatures_are_frozen() -> None:
    assert list(inspect.signature(SharingAPI.get_status).parameters) == ["self", "notebook_id"]
    assert list(inspect.signature(SharingAPI.set_public).parameters) == [
        "self",
        "notebook_id",
        "public",
    ]
    assert list(inspect.signature(SharingAPI.set_view_level).parameters) == [
        "self",
        "notebook_id",
        "level",
    ]
    add_user = inspect.signature(SharingAPI.add_user).parameters
    assert list(add_user) == [
        "self",
        "notebook_id",
        "email",
        "permission",
        "notify",
        "welcome_message",
    ]
    assert add_user["permission"].default is SharePermission.VIEWER
    assert add_user["notify"].default is True
    assert add_user["welcome_message"].default == ""
    set_users = inspect.signature(SharingAPI.set_users).parameters
    assert list(set_users) == [
        "self",
        "notebook_id",
        "grants",
        "notify",
        "welcome_message",
    ]
    assert set_users["notify"].default is True
    assert set_users["welcome_message"].default == ""
    assert list(inspect.signature(SharingAPI.update_user).parameters) == [
        "self",
        "notebook_id",
        "email",
        "permission",
    ]
    assert list(inspect.signature(SharingAPI.remove_user).parameters) == [
        "self",
        "notebook_id",
        "email",
    ]


def test_sharing_facade_takes_only_the_semantic_backend() -> None:
    """The domain is fully migrated: no RpcCaller reaches this facade."""
    parameters = inspect.signature(SharingAPI.__init__).parameters
    assert list(parameters) == ["self", "_backend", "_deadline_factory"]
    assert parameters["_backend"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["_deadline_factory"].kind is inspect.Parameter.KEYWORD_ONLY


def test_every_sharing_operation_has_one_registered_web_binding() -> None:
    """Each sharing operation is either a codec leaf or a service-owned workflow."""
    workflows = {
        Operation.SHARING_SET_PUBLIC: SHARING_SET_PUBLIC_DEF,
        Operation.SHARING_SET_VIEW_LEVEL: SHARING_SET_VIEW_LEVEL_DEF,
        Operation.SHARING_UPDATE_USERS: SHARING_UPDATE_USERS_DEF,
    }
    for operation, definition in workflows.items():
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.service_owned is True
        assert binding.definition is definition
        assert binding.row is None
        assert operation in WEB_SERVICE_OWNED_OPERATIONS
        assert operation not in WEB_SUPPORTED_OPERATIONS
    # P9.3: the two leaves are codec rows, not handler names.
    leaves = {
        Operation.SHARING_GET: (sharing_rows.SHARING_GET, SHARING_GET_DEF, CallPolicy.READ),
        Operation.SHARING_PATCH_VIEW_LEVEL: (
            primitive_rows.SHARING_PATCH_VIEW_LEVEL,
            SHARING_PATCH_VIEW_LEVEL_DEF,
            CallPolicy.MUTATION,
        ),
        Operation.LEGACY_SHARE_ARTIFACT: (
            sharing_rows.LEGACY_SHARE_ARTIFACT,
            LEGACY_SHARE_ARTIFACT_DEF,
            CallPolicy.MUTATION,
        ),
    }
    for operation, (row, definition, policy) in leaves.items():
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.row is row
        assert row.definition is definition
        assert definition.policy is policy
        assert operation in WEB_SUPPORTED_OPERATIONS


@pytest.mark.asyncio
async def test_legacy_share_artifact_preserves_status_three_null_success() -> None:
    rpc_call = AsyncMock(return_value=None)
    backend = WebRpcBackend(
        MagicMock(rpc_call=rpc_call),
    )

    result = await backend.invoke(
        LEGACY_SHARE_ARTIFACT_DEF,
        LegacyShareArtifactInput("nb_123", public=True, artifact_id="artifact_456"),
        deadline=None,
    )

    assert result == LegacyShareArtifactResult(public=True, artifact_id="artifact_456")
    rpc_call.assert_awaited_once()
    request = rpc_call.await_args
    assert request.args == (RPCMethod.SHARE_ARTIFACT, [[1], "nb_123", "artifact_456"])
    assert request.kwargs["source_path"] == "/notebook/nb_123"
    assert request.kwargs["allow_null"] is True
    assert request.kwargs["raise_on_null_status"] is False


@pytest.mark.asyncio
async def test_get_status_issues_one_read_and_projects_every_decoded_field() -> None:
    rpc_call = AsyncMock(side_effect=[SHARE_STATUS_PAYLOAD])
    api = _api(rpc_call)

    status = await api.get_status("nb_123")

    assert _methods(rpc_call) == [RPCMethod.GET_SHARE_STATUS]
    assert rpc_call.await_args_list[0].args[1] == ["nb_123", [2]]
    assert rpc_call.await_args_list[0].kwargs["source_path"] == "/notebook/nb_123"
    assert status.notebook_id == "nb_123"
    assert status.is_public is True
    assert status.access is ShareAccess.ANYONE_WITH_LINK
    assert status.view_level is ShareViewLevel.FULL_NOTEBOOK
    assert status.share_url == "https://notebook.google.com/notebook/nb_123"
    assert status.max_individuals_share_limit == 1000
    assert status.is_public_sharing_allowed is True
    assert [(user.email, user.permission) for user in status.shared_users] == [
        ("owner@example.com", SharePermission.OWNER),
        ("viewer@example.com", SharePermission.VIEWER),
    ]
    assert status.shared_users[0].display_name == "Owner"
    assert status.shared_users[0].avatar_url == "https://avatar/owner"


@pytest.mark.asyncio
async def test_projected_shared_users_are_not_shared_between_calls() -> None:
    rpc_call = AsyncMock(side_effect=[SHARE_STATUS_PAYLOAD, SHARE_STATUS_PAYLOAD])
    api = _api(rpc_call)

    first = await api.get_status("nb_123")
    second = await api.get_status("nb_123")
    first.shared_users.clear()

    assert len(second.shared_users) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("public", "access_code"),
    [(True, ShareAccess.ANYONE_WITH_LINK), (False, ShareAccess.RESTRICTED)],
)
async def test_set_public_pins_visibility_envelope_and_post_mutation_read(
    public: bool,
    access_code: ShareAccess,
) -> None:
    rpc_call = AsyncMock(side_effect=[[], SHARE_STATUS_PAYLOAD])
    api = _api(rpc_call)

    await api.set_public("nb_123", public)

    assert _methods(rpc_call) == [RPCMethod.SHARE_NOTEBOOK, RPCMethod.GET_SHARE_STATUS]
    assert rpc_call.await_args_list[0].args[1] == [
        [["nb_123", None, [access_code.value], [access_code.value, ""]]],
        1,
        None,
        [2],
    ]
    assert rpc_call.await_args_list[0].kwargs["allow_null"] is True
    assert rpc_call.await_args_list[0].kwargs["source_path"] == "/notebook/nb_123"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "level",
    [ShareViewLevel.CHAT_ONLY, ShareViewLevel.FULL_NOTEBOOK],
)
async def test_set_view_level_pins_payload_and_reports_the_level_it_just_set(
    level: ShareViewLevel,
) -> None:
    rpc_call = AsyncMock(side_effect=[None, SHARE_STATUS_PAYLOAD])
    api = _api(rpc_call)

    status = await api.set_view_level("nb_123", level)

    assert _methods(rpc_call) == [RPCMethod.RENAME_NOTEBOOK, RPCMethod.GET_SHARE_STATUS]
    assert rpc_call.await_args_list[0].args[1] == [
        "nb_123",
        [[None, None, None, None, None, None, None, None, [[level.value]]]],
    ]
    assert rpc_call.await_args_list[0].kwargs["allow_null"] is True
    assert status.view_level is level
    # #2130: the view-level override must not drop any other decoded field.
    assert status.max_individuals_share_limit == 1000
    assert status.is_public_sharing_allowed is True
    assert len(status.shared_users) == 2


@pytest.mark.asyncio
async def test_add_user_sends_one_grant_entry_with_the_no_message_block() -> None:
    rpc_call = AsyncMock(side_effect=[[], SHARE_STATUS_PAYLOAD])
    api = _api(rpc_call)

    await api.add_user("nb_123", "new@example.com", SharePermission.VIEWER, notify=True)

    assert _methods(rpc_call) == [RPCMethod.SHARE_NOTEBOOK, RPCMethod.GET_SHARE_STATUS]
    assert rpc_call.await_args_list[0].args[1] == [
        [
            [
                "nb_123",
                [["new@example.com", None, SharePermission.VIEWER.value]],
                None,
                [1, ""],
            ]
        ],
        1,
        None,
        [2],
    ]


@pytest.mark.asyncio
async def test_set_users_batches_grants_and_carries_the_welcome_message_block() -> None:
    rpc_call = AsyncMock(side_effect=[[], SHARE_STATUS_PAYLOAD])
    api = _api(rpc_call)

    await api.set_users(
        "nb_123",
        [
            ("viewer@example.com", SharePermission.VIEWER),
            ("editor@example.com", SharePermission.EDITOR),
        ],
        notify=False,
        welcome_message="Welcome, team!",
    )

    assert rpc_call.await_args_list[0].args[1] == [
        [
            [
                "nb_123",
                [
                    ["viewer@example.com", None, SharePermission.VIEWER.value],
                    ["editor@example.com", None, SharePermission.EDITOR.value],
                ],
                None,
                [0, "Welcome, team!"],
            ]
        ],
        0,
        None,
        [2],
    ]


@pytest.mark.asyncio
async def test_update_user_upserts_one_grant_without_notifying() -> None:
    rpc_call = AsyncMock(side_effect=[[], SHARE_STATUS_PAYLOAD])
    api = _api(rpc_call)

    await api.update_user("nb_123", "user@example.com", SharePermission.EDITOR)

    assert rpc_call.await_args_list[0].args[1] == [
        [
            [
                "nb_123",
                [["user@example.com", None, SharePermission.EDITOR.value]],
                None,
                [1, ""],
            ]
        ],
        0,
        None,
        [2],
    ]


@pytest.mark.asyncio
async def test_remove_user_sends_the_removal_code_with_flag_zero_and_empty_message() -> None:
    """``[0, ""]``, not the grant path's ``[1, ""]`` — the captured removal
    payload has always sent flag 0 with an empty message."""
    rpc_call = AsyncMock(side_effect=[[], SHARE_STATUS_PAYLOAD])
    api = _api(rpc_call)

    await api.remove_user("nb_123", "removed@example.com")

    assert _methods(rpc_call) == [RPCMethod.SHARE_NOTEBOOK, RPCMethod.GET_SHARE_STATUS]
    assert rpc_call.await_args_list[0].args[1] == [
        [
            [
                "nb_123",
                [["removed@example.com", None, SharePermission._REMOVE.value]],
                None,
                [0, ""],
            ]
        ],
        0,
        None,
        [2],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("grants", "message"),
    [
        ([], "Must provide at least one user grant"),
        ([("owner@example.com", SharePermission.OWNER)], "Cannot assign OWNER permission"),
        (
            [("gone@example.com", SharePermission._REMOVE)],
            r"Use remove_user\(\) instead",
        ),
        (
            [
                ("dup@example.com", SharePermission.VIEWER),
                ("dup@example.com", SharePermission.EDITOR),
            ],
            "Duplicate email in grants",
        ),
    ],
)
async def test_invalid_grants_fail_before_any_rpc(
    grants: list[tuple[str, SharePermission]],
    message: str,
) -> None:
    rpc_call = AsyncMock(side_effect=AssertionError("no RPC may be issued"))
    api = _api(rpc_call)

    with pytest.raises(ValueError, match=message):
        await api.set_users("nb_123", grants)

    assert rpc_call.await_args_list == []


@pytest.mark.asyncio
async def test_case_variant_grantees_stay_distinct() -> None:
    rpc_call = AsyncMock(side_effect=[[], SHARE_STATUS_PAYLOAD])
    api = _api(rpc_call)

    await api.set_users(
        "nb_123",
        [
            ("Dup@example.com", SharePermission.VIEWER),
            ("dup@example.com", SharePermission.EDITOR),
        ],
        notify=False,
    )

    assert rpc_call.await_args_list[0].args[1][0][0][1] == [
        ["Dup@example.com", None, SharePermission.VIEWER.value],
        ["dup@example.com", None, SharePermission.EDITOR.value],
    ]


@pytest.mark.asyncio
async def test_sharing_operations_issue_no_notebook_read() -> None:
    """No sharing call bumps ``lastViewedTime``: none of them read GET_NOTEBOOK."""
    rpc_call = AsyncMock(
        side_effect=[
            SHARE_STATUS_PAYLOAD,
            [],
            SHARE_STATUS_PAYLOAD,
            None,
            SHARE_STATUS_PAYLOAD,
            [],
            SHARE_STATUS_PAYLOAD,
            [],
            SHARE_STATUS_PAYLOAD,
        ]
    )
    api = _api(rpc_call)

    await api.get_status("nb_123")
    await api.set_public("nb_123", True)
    await api.set_view_level("nb_123", ShareViewLevel.CHAT_ONLY)
    await api.add_user("nb_123", "new@example.com")
    await api.remove_user("nb_123", "new@example.com")

    assert RPCMethod.GET_NOTEBOOK not in _methods(rpc_call)
    assert _methods(rpc_call).count(RPCMethod.GET_SHARE_STATUS) == 5


@pytest.mark.asyncio
async def test_backend_failures_surface_as_the_pre_migration_public_exceptions() -> None:
    rpc_call = AsyncMock(
        side_effect=ServerError("bad gateway", status_code=502, method_id="JFMDGd")
    )
    api = _api(rpc_call)

    with pytest.raises(ServerError) as caught:
        await api.get_status("nb_123")

    assert caught.value.status_code == 502
    assert caught.value.method_id == "JFMDGd"
    assert str(caught.value) == "bad gateway"


@pytest.mark.asyncio
async def test_a_failed_share_write_is_not_retried_and_never_reads_back() -> None:
    """The mutation is not idempotent-by-probe: a rejected write stops there."""
    rpc_call = AsyncMock(side_effect=RPCError("share rejected", method_id="QDyure"))
    api = _api(rpc_call)

    with pytest.raises(RPCError):
        await api.add_user("nb_123", "new@example.com")

    assert _methods(rpc_call) == [RPCMethod.SHARE_NOTEBOOK]


@pytest.mark.asyncio
async def test_debug_diagnostics_keep_the_pre_migration_logger_and_grantee_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rpc_call = AsyncMock(side_effect=[[], SHARE_STATUS_PAYLOAD])
    api = _api(rpc_call)

    with caplog.at_level(logging.DEBUG, logger="notebooklm._sharing"):
        await api.add_user("nb_123", "typo@example.com", SharePermission.EDITOR)

    records = [record for record in caplog.records if record.name == "notebooklm._sharing"]
    assert any(
        "typo@example.com" in record.getMessage() and "EDITOR" in record.getMessage()
        for record in records
    )


def test_neutral_vocabularies_round_trip_every_wire_code() -> None:
    """A lossy label would let a record rewrite the value the codec read."""
    decoded = decode_share_status(
        [[["gone@example.com", SharePermission._REMOVE.value, [], ["Gone", None]]], [False], 1000],
        "nb_123",
    )

    assert decoded.access is ShareAccessLevel.RESTRICTED
    assert decoded.view_level is ShareViewScope.FULL_NOTEBOOK
    assert decoded.shared_users[0].permission is SharePermissionLevel.REMOVE
    assert set(SharePermissionLevel) == {
        SharePermissionLevel.OWNER,
        SharePermissionLevel.EDITOR,
        SharePermissionLevel.VIEWER,
        SharePermissionLevel.REMOVE,
    }
    assert len(SharePermissionLevel) == len(SharePermission)


def test_sharing_service_holds_no_wire_vocabulary() -> None:
    source = inspect.getsource(SharingService)
    assert "RPCMethod" not in source
    assert "SHARE_NOTEBOOK" not in source


def test_sharing_service_returns_records_and_never_a_public_or_legacy_type() -> None:
    """P10 R6.3 / invariant I1: the service speaks ``ShareStatusRecord`` only.

    Sharing decides who can read a notebook, so the two directions of the
    boundary are pinned by name rather than by an import audit alone: no
    public model or ``Legacy*`` mapping record (an I9 exemption owned by
    ``_backend_compat``/the projectors) may appear on the service's surface.
    """
    for name in (
        "get_status",
        "set_public",
        "set_view_level",
        "set_users",
        "update_user",
        "remove_user",
    ):
        method = getattr(SharingService, name)
        annotation = inspect.signature(method).return_annotation
        assert annotation == "ShareStatusRecord", (name, annotation)

    source = inspect.getsource(SharingService)
    assert "ShareStatus\n" not in source and "-> ShareStatus:" not in source
    assert "project_share_status" not in source
    assert "Legacy" not in source


def test_every_neutral_sharing_value_has_exactly_one_public_projection() -> None:
    """Total, injective mappings both ways across the projection boundary.

    A missing entry would raise ``KeyError`` on a real notebook; a duplicated
    target would silently widen or narrow someone's access. Both maps are
    asserted total and one-to-one so neither failure can land unnoticed.
    """
    assert _SHARE_ACCESS == {
        ShareAccessLevel.RESTRICTED: ShareAccess.RESTRICTED,
        ShareAccessLevel.ANYONE_WITH_LINK: ShareAccess.ANYONE_WITH_LINK,
    }
    assert _SHARE_VIEW_LEVELS == {
        ShareViewScope.FULL_NOTEBOOK: ShareViewLevel.FULL_NOTEBOOK,
        ShareViewScope.CHAT_ONLY: ShareViewLevel.CHAT_ONLY,
    }
    assert _SHARE_PERMISSIONS == {
        SharePermissionLevel.OWNER: SharePermission.OWNER,
        SharePermissionLevel.EDITOR: SharePermission.EDITOR,
        SharePermissionLevel.VIEWER: SharePermission.VIEWER,
        SharePermissionLevel.REMOVE: SharePermission._REMOVE,
    }
    # The facade's inbound direction, exactly inverse to the outbound maps.
    assert {public: neutral for neutral, public in _SHARE_PERMISSIONS.items()} == _PERMISSION_LEVELS
    assert {public: neutral for neutral, public in _SHARE_VIEW_LEVELS.items()} == _VIEW_SCOPES

    for neutral_enum, mapping, public_enum in (
        (ShareAccessLevel, _SHARE_ACCESS, ShareAccess),
        (ShareViewScope, _SHARE_VIEW_LEVELS, ShareViewLevel),
        (SharePermissionLevel, _SHARE_PERMISSIONS, SharePermission),
    ):
        assert set(mapping) == set(neutral_enum), neutral_enum
        assert set(mapping.values()) == set(public_enum), public_enum
        assert len(set(mapping.values())) == len(mapping), neutral_enum


def test_share_url_default_is_derived_only_for_a_public_notebook() -> None:
    """The one defaulted field in the projection, pinned in both directions."""
    record = ShareStatusRecord(
        notebook_id="nb 123/x",
        is_public=True,
        access=ShareAccessLevel.ANYONE_WITH_LINK,
        view_level=ShareViewScope.FULL_NOTEBOOK,
    )

    assert (
        project_share_status(record).share_url
        == "https://notebook.google.com/notebook/nb%20123%2Fx"
    )
    assert project_share_status(replace(record, share_url="https://given")).share_url == (
        "https://given"
    )
    assert (
        project_share_status(
            replace(record, is_public=False, access=ShareAccessLevel.RESTRICTED)
        ).share_url
        is None
    )
