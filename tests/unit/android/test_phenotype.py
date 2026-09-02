"""Offline tests for the headless GMS Phenotype token provider (#2302)."""

from __future__ import annotations

import asyncio
import base64
from typing import Any, cast

import pytest

from notebooklm._android.phenotype import (
    CLIENT_TYPE_HEADER,
    EXPERIMENT_TOKEN_HEADER,
    AndroidDeviceProfile,
    PhenotypeError,
    PhenotypeTokenProvider,
    _build_request,
    _decode_server_token,
    _wrap_server_tokens,
)
from notebooklm._android.proto.notebooklm.experiments.v1 import exptsandconfigs_pb2
from notebooklm.exceptions import MissingDependencyError

pb = cast(Any, exptsandconfigs_pb2)

# A realistic serverToken message: {1: client_type=3, 2: packed experiment blob}.
_SERVER_TOKEN_MSG = bytes.fromhex("080312060d01020304ff")
_SERVER_TOKEN_B64URL = (
    base64.b64encode(_SERVER_TOKEN_MSG).decode().replace("+", "-").replace("/", "_")
)


def _response_bytes(server_token: str = _SERVER_TOKEN_B64URL) -> bytes:
    response = pb.HeterodyneResponse()
    config = response.heterodyne_config.add()
    config.package_details.package_name = "com.google.labs.language.tailwind.mobile"
    config.server_token = server_token
    return response.SerializeToString()


def _make_post(status: int = 200, body: bytes | None = None, record: list | None = None):
    async def _post(url: str, content: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        if record is not None:
            record.append((url, content, headers))
        return status, body if body is not None else _response_bytes()

    return _post


def test_device_profile_defaults_and_user_agent() -> None:
    profile = AndroidDeviceProfile()
    assert profile.model == "Pixel 8"
    assert "com.google.android.gms/" in profile.user_agent
    assert f"Android {profile.sdk_version}" in profile.user_agent


def test_device_profile_accepts_custom_android_id() -> None:
    profile = AndroidDeviceProfile(android_id=-1, sdk_version=35)
    assert profile.android_id == -1
    assert profile.sdk_version == 35


def test_build_request_is_single_package_with_explicit_auth_index() -> None:
    body = _build_request(AndroidDeviceProfile())
    request = pb.HeterodyneRequest()
    request.ParseFromString(body)
    assert len(request.data) == 1
    entry = request.data[0]
    assert entry.package_details.package_name.startswith(
        "com.google.labs.language.tailwind.mobile#"
    )
    assert entry.package_details.version == 153888
    # The auth-token index must be serialized even at its zero default (#2302).
    assert entry.package_details.HasField("auth_token_index")
    assert request.fetch_reason == 8
    assert request.config_class == 1


def test_wrap_and_decode_server_token_roundtrip() -> None:
    wrapped = _wrap_server_tokens(_SERVER_TOKEN_MSG)
    assert wrapped[0] == 0x0A  # field 1, length-delimited
    assert wrapped[2:] == _SERVER_TOKEN_MSG
    assert _decode_server_token(_SERVER_TOKEN_B64URL) == _SERVER_TOKEN_MSG


@pytest.mark.asyncio
async def test_experiment_metadata_returns_both_headers() -> None:
    provider = PhenotypeTokenProvider(http_post=_make_post())
    metadata = dict(await provider.experiment_metadata("bearer-token"))
    assert CLIENT_TYPE_HEADER in metadata
    assert metadata[CLIENT_TYPE_HEADER] == bytes.fromhex("0a020803")
    assert metadata[EXPERIMENT_TOKEN_HEADER] == _wrap_server_tokens(_SERVER_TOKEN_MSG)


@pytest.mark.asyncio
async def test_bearer_is_forwarded_in_authorization_header() -> None:
    record: list = []
    provider = PhenotypeTokenProvider(http_post=_make_post(record=record))
    await provider.experiment_metadata("secret-bearer")
    _, _, headers = record[0]
    assert headers["Authorization"] == "Bearer secret-bearer"
    assert headers["Content-Type"] == "application/x-protobuf"
    assert "Content-Encoding" not in headers  # uncompressed body


@pytest.mark.asyncio
async def test_token_is_cached_until_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    record: list = []
    provider = PhenotypeTokenProvider(
        http_post=_make_post(record=record),
        ttl_seconds=100.0,
        monotonic=lambda: now[0],
    )
    await provider.experiment_metadata("b")
    await provider.experiment_metadata("b")
    assert len(record) == 1  # second call served from cache
    now[0] += 101.0
    await provider.experiment_metadata("b")
    assert len(record) == 2  # cache expired -> refetch


@pytest.mark.asyncio
async def test_force_and_invalidate_bypass_cache() -> None:
    record: list = []
    provider = PhenotypeTokenProvider(http_post=_make_post(record=record))
    await provider.experiment_metadata("b")
    await provider.experiment_metadata("b", force=True)
    assert len(record) == 2
    provider.invalidate()
    await provider.experiment_metadata("b")
    assert len(record) == 3


@pytest.mark.asyncio
async def test_lifecycle_invalidates_account_bound_cache() -> None:
    record: list = []
    provider = PhenotypeTokenProvider(http_post=_make_post(record=record))
    await provider.open(asyncio.get_running_loop(), 1)
    await provider.experiment_metadata("account-a")
    await provider.experiment_metadata("account-a")
    assert len(record) == 1

    await provider.prepare_close()
    await provider.open(asyncio.get_running_loop(), 2)
    await provider.experiment_metadata("account-b")
    await provider.close_resources()
    assert len(record) == 2


@pytest.mark.asyncio
async def test_non_200_raises_phenotype_error() -> None:
    provider = PhenotypeTokenProvider(http_post=_make_post(status=403, body=b""))
    with pytest.raises(PhenotypeError, match="HTTP 403"):
        await provider.experiment_metadata("b")


@pytest.mark.asyncio
async def test_empty_server_token_raises_phenotype_error() -> None:
    provider = PhenotypeTokenProvider(http_post=_make_post(body=_response_bytes(server_token="")))
    with pytest.raises(PhenotypeError, match="no experiment token"):
        await provider.experiment_metadata("b")


@pytest.mark.asyncio
async def test_malformed_response_raises_phenotype_error() -> None:
    provider = PhenotypeTokenProvider(http_post=_make_post(body=b"\x0a"))
    with pytest.raises(PhenotypeError, match="malformed experiment response"):
        await provider.experiment_metadata("b")


@pytest.mark.asyncio
async def test_transport_failure_is_wrapped() -> None:
    async def _boom(url: str, content: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        raise ConnectionError("network down")

    provider = PhenotypeTokenProvider(http_post=_boom)
    with pytest.raises(PhenotypeError, match="failed to reach the endpoint"):
        await provider.experiment_metadata("b")


@pytest.mark.asyncio
async def test_missing_dependency_propagates() -> None:
    async def _missing(url: str, content: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        raise MissingDependencyError("no httpx")

    provider = PhenotypeTokenProvider(http_post=_missing)
    with pytest.raises(MissingDependencyError):
        await provider.experiment_metadata("b")
