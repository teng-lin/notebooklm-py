"""Backend-selected raw escape-hatch contracts."""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
import traceback
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.exceptions import DecodingError, RPCResponseTooLargeError
from notebooklm.raw import (
    AndroidRawAPI,
    GrpcUnaryMethod,
    GrpcUnaryStreamMethod,
    ReplayPolicy,
    WebRawAPI,
)
from notebooklm.rpc import RPCMethod

METHOD = "/example.raw.Service/GetThing"


class _Request:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def SerializeToString(self) -> bytes:
        return self.payload


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    @classmethod
    def FromString(cls, payload: bytes) -> _Response:
        return cls(payload)


class _FakeRpc:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def rpc_call(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return {"wire": "unchanged"}


class _FakeAndroidSession:
    def __init__(self) -> None:
        self.unary_calls: list[tuple[str, bytes, dict[str, Any]]] = []
        self.stream_calls: list[tuple[str, bytes, dict[str, Any]]] = []
        self.unary_response = b"unary-response"
        self.stream_responses = [b"one", b"two"]

    async def unary(self, method: str, request: bytes, **kwargs: Any) -> bytes:
        self.unary_calls.append((method, request, kwargs))
        return self.unary_response

    def stream(self, method: str, request: bytes, **kwargs: Any) -> AsyncIterator[bytes]:
        self.stream_calls.append((method, request, kwargs))

        async def iterate() -> AsyncIterator[bytes]:
            total = 0
            for item in self.stream_responses:
                total += kwargs["response_sizer"](item)
                cap = kwargs["max_response_bytes"]
                if cap is not None and total > cap:
                    raise RPCResponseTooLargeError(
                        "too large",
                        method_id=method,
                        limit_bytes=cap,
                        bytes_read=total,
                    )
                yield item

        return iterate()


def _auth() -> AuthTokens:
    return AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session")


@pytest.mark.asyncio
async def test_web_raw_call_is_a_thin_executor_delegate() -> None:
    rpc = _FakeRpc()
    raw = WebRawAPI(rpc)  # type: ignore[arg-type]

    result = await raw.call(
        RPCMethod.GET_NOTEBOOK,
        ["notebook-id"],
        allow_null=True,
        disable_internal_retries=True,
        read_timeout=12.5,
        raise_on_null_status=True,
    )

    assert result == {"wire": "unchanged"}
    assert rpc.calls == [
        {
            "method": RPCMethod.GET_NOTEBOOK,
            "params": ["notebook-id"],
            "allow_null": True,
            "disable_internal_retries": True,
            "read_timeout": 12.5,
            "raise_on_null_status": True,
        }
    ]


def test_client_installs_raw_namespace_for_each_backend() -> None:
    web = NotebookLMClient(_auth(), backend="web")
    android = NotebookLMClient(_auth(), backend="android")

    assert type(web.raw) is WebRawAPI
    assert web.raw._rpc is web._web_runtime.executor
    assert type(android.raw) is AndroidRawAPI
    assert android._android_runtime is not None
    assert android.raw._transport is android._android_runtime.session


def test_android_descriptor_defaults_fail_closed_and_rejects_target_shaped_paths() -> None:
    descriptor = GrpcUnaryMethod(METHOD, response_type=_Response)

    assert descriptor.replay_policy is ReplayPolicy.NEVER
    with pytest.raises(ValueError, match="targets and URLs are not accepted"):
        GrpcUnaryMethod(
            "https://attacker.invalid/example.raw.Service/GetThing",
            response_type=_Response,
        )
    with pytest.raises(ValueError, match="targets and URLs are not accepted"):
        GrpcUnaryMethod("//attacker.invalid/GetThing", response_type=_Response)


def test_descriptor_requires_one_response_codec() -> None:
    with pytest.raises(ValueError, match="response_type or response_deserializer is required"):
        GrpcUnaryMethod(METHOD)
    with pytest.raises(ValueError, match="not both"):
        GrpcUnaryMethod(
            METHOD,
            response_type=_Response,
            response_deserializer=_Response.FromString,
        )


@pytest.mark.parametrize("descriptor_type", [GrpcUnaryMethod, GrpcUnaryStreamMethod])
def test_descriptor_rejects_non_callable_explicit_codecs_before_dispatch(
    descriptor_type: type[GrpcUnaryMethod[Any, Any]] | type[GrpcUnaryStreamMethod[Any, Any]],
) -> None:
    with pytest.raises(TypeError, match="request_serializer must be callable"):
        descriptor_type(
            METHOD,
            response_type=_Response,
            request_serializer=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="response_deserializer must be callable"):
        descriptor_type(
            METHOD,
            response_deserializer=object(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "expected_replay_safe"),
    [(ReplayPolicy.NEVER, False), (ReplayPolicy.SAFE_READ, True)],
)
async def test_android_unary_maps_explicit_replay_policy_and_uses_constant_telemetry(
    policy: ReplayPolicy,
    expected_replay_safe: bool,
) -> None:
    session = _FakeAndroidSession()
    raw = AndroidRawAPI(session)  # type: ignore[arg-type]
    descriptor = GrpcUnaryMethod(METHOD, response_type=_Response, replay_policy=policy)

    response = await raw.unary(descriptor, _Request(b"request"), timeout=3.0)

    assert response.payload == b"unary-response"
    method, request, kwargs = session.unary_calls[0]
    assert method == METHOD
    assert request == b"request"
    assert kwargs["replay_safe"] is expected_replay_safe
    assert kwargs["raw_replay"].replay_safe is expected_replay_safe
    assert kwargs["telemetry_method"] == "raw.unary"
    assert kwargs["timeout"] == 3.0
    assert kwargs["request_serializer"](b"wire") == b"wire"
    assert kwargs["response_deserializer"](b"wire") == b"wire"


@pytest.mark.asyncio
async def test_android_raw_supports_explicit_bytes_codecs() -> None:
    session = _FakeAndroidSession()
    raw = AndroidRawAPI(session)  # type: ignore[arg-type]
    descriptor = GrpcUnaryMethod[dict[str, bytes], str](
        METHOD,
        request_serializer=lambda value: value["payload"],
        response_deserializer=lambda payload: payload.decode(),
    )

    result = await raw.unary(descriptor, {"payload": b"encoded"})

    assert result == "unary-response"
    assert session.unary_calls[0][1] == b"encoded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        "authorization",
        "Authorization",
        "AUTHORIZATION",
        "x-goog-ext-174067345-bin",
        "X-Goog-Ext-174067345-Bin",
        "x-goog-ext-202964622-bin",
        "X-GOOG-EXT-202964622-BIN",
    ],
)
async def test_android_raw_rejects_library_owned_metadata_case_insensitively(
    header: str,
) -> None:
    session = _FakeAndroidSession()
    raw = AndroidRawAPI(session)  # type: ignore[arg-type]
    descriptor = GrpcUnaryMethod(METHOD, response_type=_Response)
    value: str | bytes = b"value" if header.lower().endswith("-bin") else "value"

    with pytest.raises(ValueError, match="library-owned header"):
        await raw.unary(descriptor, _Request(b"request"), metadata=[(header, value)])

    assert session.unary_calls == []


@pytest.mark.asyncio
async def test_android_raw_normalizes_and_forwards_non_owned_metadata() -> None:
    session = _FakeAndroidSession()
    raw = AndroidRawAPI(session)  # type: ignore[arg-type]
    descriptor = GrpcUnaryMethod(METHOD, response_type=_Response)

    await raw.unary(
        descriptor,
        _Request(b"request"),
        metadata=[("X-Custom", "value"), ("x-custom-bin", b"binary")],
    )

    assert session.unary_calls[0][2]["metadata"] == (
        ("x-custom", "value"),
        ("x-custom-bin", b"binary"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["request", "response"])
async def test_caller_codec_exceptions_become_sanitized_decoding_errors(stage: str) -> None:
    secret = "codec-secret-must-not-escape"

    def fail(_value: Any) -> Any:
        local_secret = secret
        raise RuntimeError(local_secret)

    session = _FakeAndroidSession()
    raw = AndroidRawAPI(session)  # type: ignore[arg-type]
    descriptor = GrpcUnaryMethod(
        METHOD,
        request_serializer=fail if stage == "request" else lambda request: request.payload,
        response_deserializer=fail if stage == "response" else _Response.FromString,
    )

    with pytest.raises(DecodingError) as caught:
        await raw.unary(descriptor, _Request(b"request"))

    error = caught.value
    assert secret not in str(error)
    assert error.method_id == METHOD
    assert error.__cause__ is None
    assert error.__context__ is None
    frames = traceback.extract_tb(error.__traceback__)
    assert all(frame.name != "fail" for frame in frames)


@pytest.mark.asyncio
async def test_unary_stream_failure_does_not_retain_wire_secrets_in_traceback_locals() -> None:
    metadata_secret = "stream-metadata-secret"
    request_secret = b"stream-request-secret"
    response_secret = b"stream-response-secret"

    def fail_response(payload: bytes) -> _Response:
        assert payload == response_secret
        raise RuntimeError("decode failed")

    session = _FakeAndroidSession()
    session.stream_responses = [response_secret]
    raw = AndroidRawAPI(session)  # type: ignore[arg-type]
    descriptor = GrpcUnaryStreamMethod[
        _Request,
        _Response,
    ](METHOD, response_deserializer=fail_response)

    with pytest.raises(DecodingError) as caught:
        async for _item in raw.unary_stream(
            descriptor,
            _Request(request_secret),
            metadata=[("x-stream-secret", metadata_secret)],
        ):
            pass

    frame = caught.value.__traceback__
    inspected_raw_frame = False
    while frame is not None:
        frame_path = Path(frame.tb_frame.f_code.co_filename)
        if frame_path.name == "raw.py" and frame_path.parent.name == "notebooklm":
            inspected_raw_frame = True
            local_values = tuple(frame.tb_frame.f_locals.values())
            assert metadata_secret not in repr(local_values)
            assert request_secret not in local_values
            assert response_secret not in local_values
        frame = frame.tb_next
    assert inspected_raw_frame


@pytest.mark.asyncio
async def test_unary_stream_is_bounded_and_uses_constant_telemetry() -> None:
    session = _FakeAndroidSession()
    raw = AndroidRawAPI(session)  # type: ignore[arg-type]
    descriptor = GrpcUnaryStreamMethod(
        METHOD,
        response_type=_Response,
        replay_policy=ReplayPolicy.SAFE_READ,
    )

    with pytest.raises(RPCResponseTooLargeError) as caught:
        async for _item in raw.unary_stream(
            descriptor,
            _Request(b"request"),
            max_response_bytes=5,
        ):
            pass

    assert caught.value.limit_bytes == 5
    method, request, kwargs = session.stream_calls[0]
    assert method == METHOD
    assert request == b"request"
    assert kwargs["max_response_bytes"] == 5
    assert kwargs["telemetry_method"] == "raw.unary_stream"
    assert kwargs["replay_safe"] is True


@pytest.mark.asyncio
async def test_unary_stream_has_a_finite_default_response_cap() -> None:
    session = _FakeAndroidSession()
    raw = AndroidRawAPI(session)  # type: ignore[arg-type]
    descriptor = GrpcUnaryStreamMethod(METHOD, response_type=_Response)

    assert [item.payload async for item in raw.unary_stream(descriptor, _Request(b"request"))] == [
        b"one",
        b"two",
    ]
    cap = session.stream_calls[0][2]["max_response_bytes"]
    assert isinstance(cap, int)
    assert cap > 0


def test_android_raw_exposes_no_client_or_bidirectional_streaming() -> None:
    public = {
        name
        for name, member in inspect.getmembers(AndroidRawAPI, callable)
        if not name.startswith("_")
    }
    assert public == {"unary", "unary_stream"}


def test_android_client_construction_does_not_import_grpc_or_protobuf() -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = """
import json
import sys
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient

before = set(sys.modules)
NotebookLMClient(
    AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session"),
    backend="android",
)
new = set(sys.modules) - before
print(json.dumps(sorted(
    name for name in new if name == "grpc" or name.startswith("grpc.")
    or name == "google.protobuf" or name.startswith("google.protobuf.")
)))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "[]"


@pytest.mark.asyncio
async def test_unary_cancellation_is_not_reclassified_as_decoding_failure() -> None:
    class _CancellingSession(_FakeAndroidSession):
        async def unary(self, method: str, request: bytes, **kwargs: Any) -> bytes:
            raise asyncio.CancelledError

    raw = AndroidRawAPI(_CancellingSession())  # type: ignore[arg-type]
    descriptor = GrpcUnaryMethod(METHOD, response_type=_Response)

    with pytest.raises(asyncio.CancelledError):
        await raw.unary(descriptor, _Request(b"request"))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["request", "response"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_codec_cancellation_is_not_reclassified(
    stage: str,
    streaming: bool,
) -> None:
    def cancel(_value: Any) -> Any:
        raise asyncio.CancelledError

    descriptor_type = GrpcUnaryStreamMethod if streaming else GrpcUnaryMethod
    descriptor = descriptor_type(
        METHOD,
        request_serializer=cancel if stage == "request" else lambda request: request.payload,
        response_deserializer=cancel if stage == "response" else _Response.FromString,
    )
    raw = AndroidRawAPI(_FakeAndroidSession())  # type: ignore[arg-type]

    with pytest.raises(asyncio.CancelledError):
        if streaming:
            async for _item in raw.unary_stream(descriptor, _Request(b"request")):  # type: ignore[arg-type]
                pass
        else:
            await raw.unary(descriptor, _Request(b"request"))  # type: ignore[arg-type]
