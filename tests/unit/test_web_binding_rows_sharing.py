"""P9.3 sharing leaves and the remaining P9.4 custom rows.

``SHARING_GET`` and ``LEGACY_SHARE_ARTIFACT`` are ``encode → one native call →
decode`` rows in ``_web/bindings/sharing.py``. These tests pin the conversion
oracles: the identical keyword set reaches the runtime (including explicit
``False``/``None`` values and the notebook route), the payload builders are
unchanged, failure projection is what ``invoke()`` produced for handler rows,
and the ``dispatched`` marker reaches the neutral ``BackendError``. The
view-level and user-grant composites stay custom rows; public visibility is
service-owned since P9.2-5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import CodecBinding, CodecPayload, DeadlineMode
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._records import (
    LEGACY_SHARE_ARTIFACT_DEF,
    SHARING_GET_DEF,
    LegacyShareArtifactInput,
    LegacyShareArtifactResult,
    ShareAccessLevel,
    SharePermissionLevel,
    ShareViewScope,
    SharingGetInput,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import sharing as sharing_rows
from notebooklm._web.codec import sharing as sharing_codec
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_SHARE_STATUS_PAYLOAD: list[Any] = [
    [
        ["owner@example.com", 1, None, ["Owner", "https://avatar/owner"]],
        ["viewer@example.com", 3, None, ["Viewer", None]],
    ],
    [True],
    1000,
    True,
]

_BASE_KWARGS = {
    "allow_null": False,
    "_is_retry": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "read_timeout": None,
    "raise_on_null_status": False,
    "_retry_deadline": None,
}


@dataclass
class _Call:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _RecordingExecutor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method=method, params=params, kwargs=kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


# --- registry partition ------------------------------------------------------


def test_sharing_leaves_are_codec_rows_and_composites_are_custom_rows() -> None:
    converted = {
        Operation.SHARING_GET: sharing_rows.SHARING_GET,
        Operation.LEGACY_SHARE_ARTIFACT: sharing_rows.LEGACY_SHARE_ARTIFACT,
    }
    custom = {
        Operation.SHARING_SET_VIEW_LEVEL: sharing_rows.SHARING_SET_VIEW_LEVEL,
        Operation.SHARING_UPDATE_USERS: sharing_rows.SHARING_UPDATE_USERS,
    }
    assert dict(sharing_rows.SHARING_ROWS) == {**converted, **custom}
    for operation, row in converted.items():
        assert WEB_BINDING_ROWS[operation] is row
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.handler_name is None
        assert binding.row is row
        assert isinstance(row, CodecBinding)
        assert row.definition is binding.definition
        assert row.deadline is DeadlineMode.INHERIT
        assert row.native.is_constant
        assert row.forward_disable_internal_retries is False
        assert row.map_error is None
    assert sharing_rows.SHARING_GET.native.select(None).method is RPCMethod.GET_SHARE_STATUS
    assert sharing_rows.LEGACY_SHARE_ARTIFACT.native.select(None).method is (
        RPCMethod.SHARE_ARTIFACT
    )
    for name in (
        "_sharing_get",
        "_legacy_share_artifact",
        "_sharing_status",
        "_sharing_set_public",
        "_sharing_set_view_level",
        "_sharing_update_users",
    ):
        assert not hasattr(WebRpcBackend, name)
    # P9.4: the two remaining composites are custom rows, not handler names.
    for operation, row in custom.items():
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.handler_name is None
        assert binding.row is row
        assert WEB_BINDING_ROWS[operation] is row
    backend = build_web_backend(_RecordingExecutor())
    assert backend._bindings[Operation.SHARING_GET] is sharing_rows.SHARING_GET
    assert backend._bindings[Operation.LEGACY_SHARE_ARTIFACT] is sharing_rows.LEGACY_SHARE_ARTIFACT


# --- payload goldens -----------------------------------------------------------


def test_sharing_get_payload_golden() -> None:
    payload = sharing_codec.encode_sharing_get(SharingGetInput("nb_123"))
    assert payload == CodecPayload(params=["nb_123", [2]], source_path="/notebook/nb_123")
    assert payload.allow_null is False
    assert payload.raise_on_null_status is False
    assert payload.attempt_timeout is None


@pytest.mark.parametrize(
    ("public", "artifact_id", "expected"),
    [
        (True, "artifact_456", [[1], "nb_123", "artifact_456"]),
        (False, "artifact_456", [[0], "nb_123", "artifact_456"]),
        (True, None, [[1], "nb_123"]),
        (False, "", [[0], "nb_123"]),
    ],
)
def test_legacy_share_artifact_payload_golden(
    public: bool, artifact_id: str | None, expected: list[Any]
) -> None:
    payload = sharing_codec.encode_legacy_share_artifact(
        LegacyShareArtifactInput("nb_123", public=public, artifact_id=artifact_id)
    )
    assert payload.params == expected
    assert payload.source_path == "/notebook/nb_123"
    assert payload.allow_null is True
    assert payload.raise_on_null_status is False
    assert payload.attempt_timeout is None


def test_legacy_share_artifact_decoder_echoes_the_requested_state() -> None:
    value = LegacyShareArtifactInput("nb_123", public=False, artifact_id="a1")
    assert sharing_codec.decode_legacy_share_artifact(value, None) == LegacyShareArtifactResult(
        public=False, artifact_id="a1"
    )


# --- dispatch oracles ------------------------------------------------------------


@pytest.mark.asyncio
async def test_sharing_rows_forward_the_identical_keyword_set() -> None:
    executor = _RecordingExecutor(_SHARE_STATUS_PAYLOAD, None)
    backend = build_web_backend(executor)

    status = await backend.invoke(SHARING_GET_DEF, SharingGetInput("nb_123"), deadline=None)
    legacy = await backend.invoke(
        LEGACY_SHARE_ARTIFACT_DEF,
        LegacyShareArtifactInput("nb_123", public=True, artifact_id="artifact_456"),
        deadline=None,
    )

    record = status.status
    assert record.notebook_id == "nb_123"
    assert record.is_public is True
    assert record.access is ShareAccessLevel.ANYONE_WITH_LINK
    assert record.view_level is ShareViewScope.FULL_NOTEBOOK
    assert record.max_individuals_share_limit == 1000
    assert record.is_public_sharing_allowed is True
    assert [(user.email, user.permission) for user in record.shared_users] == [
        ("owner@example.com", SharePermissionLevel.OWNER),
        ("viewer@example.com", SharePermissionLevel.VIEWER),
    ]
    assert legacy == LegacyShareArtifactResult(public=True, artifact_id="artifact_456")

    get, share = executor.calls
    assert get.method is RPCMethod.GET_SHARE_STATUS
    assert get.params == ["nb_123", [2]]
    assert get.kwargs == {**_BASE_KWARGS, "source_path": "/notebook/nb_123"}
    assert share.method is RPCMethod.SHARE_ARTIFACT
    assert share.params == [[1], "nb_123", "artifact_456"]
    assert share.kwargs == {
        **_BASE_KWARGS,
        "source_path": "/notebook/nb_123",
        "allow_null": True,
    }


@pytest.mark.asyncio
async def test_codec_row_read_timeout_is_clamped_to_the_shared_deadline() -> None:
    executor = _RecordingExecutor(_SHARE_STATUS_PAYLOAD)
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    await backend.invoke(SHARING_GET_DEF, SharingGetInput("nb_123"), deadline=deadline)

    (call,) = executor.calls
    assert call.kwargs["read_timeout"] == pytest.approx(4.0)
    assert call.kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_codec_row_server_error_translates_like_a_handler_and_is_dispatched() -> None:
    executor = _RecordingExecutor(ServerError("boom", method_id=RPCMethod.SHARE_ARTIFACT.value))
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            LEGACY_SHARE_ARTIFACT_DEF,
            LegacyShareArtifactInput("nb_123", public=True, artifact_id=None),
            deadline=None,
        )

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.LEGACY_SHARE_ARTIFACT
    assert error.reason is BackendErrorReason.SERVER
    assert error.message == "boom"
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.SHARE_ARTIFACT.value
    assert "public_error_failure" in error.diagnostics
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert isinstance(error.__cause__, ServerError)


@pytest.mark.asyncio
async def test_codec_row_timeout_after_expiry_becomes_a_dispatched_deadline_error() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(
        RPCTimeoutError("slow", method_id=RPCMethod.GET_SHARE_STATUS.value)
    )
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(SHARING_GET_DEF, SharingGetInput("nb_123"), deadline=deadline)

    error = caught.value
    assert error.operation is Operation.SHARING_GET
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.outcome_unknown is False  # READ policy
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.diagnostics is not None
    assert error.diagnostics["timeout"] == 5.0
    assert error.diagnostics["method_id"] == RPCMethod.GET_SHARE_STATUS.value
    assert "public_error_failure" in error.diagnostics
    assert isinstance(error.__cause__, RPCTimeoutError)


@pytest.mark.asyncio
async def test_codec_row_pre_dispatch_expiry_is_not_dispatched() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            LEGACY_SHARE_ARTIFACT_DEF,
            LegacyShareArtifactInput("nb_123", public=True, artifact_id=None),
            deadline=deadline,
        )

    assert executor.calls == []
    assert caught.value.outcome_unknown is False
    assert caught.value.dispatched is False
    assert may_have_committed(caught.value) is False
