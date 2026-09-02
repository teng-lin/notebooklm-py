"""HTTP cassette seam for the Android Phenotype protobuf POST.

The Android gRPC cassette records NotebookLM C-core calls. Play Books add has
one auxiliary HTTP request to ``getExperimentsAndConfigs`` before ``AddSources``;
this helper scopes VCR.py to that one POST so OAuth minting and profile traffic
cannot accidentally enter the cassette. Both protobuf bodies are decoded,
exhaustively scalar-redacted, and reserialized before persistence.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import vcr

from notebooklm._android import phenotype

from .android_grpc_cassette import ProtoRedactor

_PHENOTYPE_METHOD = "/notebooklm.experiments.v1.PhenotypeHttp/GetExperimentsAndConfigs"
_SAFE_REQUEST_HEADERS = frozenset({"content-length", "content-type", "user-agent"})
_SAFE_RESPONSE_HEADERS = frozenset({"content-length", "content-type"})


def _body_bytes(value: Any, *, location: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("latin1")
    raise TypeError(f"{location} must be bytes or text")


def _set_content_length(headers: Any, length: int) -> None:
    for key in tuple(headers):
        if str(key).casefold() == "content-length":
            headers[key] = [str(length)] if isinstance(headers[key], list) else str(length)
            return


def _retain_headers(headers: Any, allowed: frozenset[str]) -> None:
    for key in tuple(headers):
        if str(key).casefold() not in allowed:
            del headers[key]


def _normalize_user_agent(headers: Any) -> None:
    safe_value = phenotype.AndroidDeviceProfile().user_agent
    for key in tuple(headers):
        if str(key).casefold() == "user-agent":
            headers[key] = [safe_value] if isinstance(headers[key], list) else safe_value


def _validate_phenotype_request(message: Any) -> None:
    """Pin non-secret protocol constants before generic scalar redaction."""

    if (
        message.fetch_reason != phenotype._FETCH_REASON
        or message.config_class != phenotype._CONFIG_CLASS
        or message.package_name != phenotype._HOST_PACKAGE
        or message.header.clearcut_logger_header.log_source != 4
        or len(message.data) != 1
    ):
        raise ValueError("Phenotype cassette refused unexpected request constants")
    details = message.data[0].package_details
    if (
        details.package_name != phenotype._MENDEL_PACKAGE
        or details.version != phenotype._MENDEL_VERSION
        or not details.HasField("auth_token_index")
        or details.auth_token_index.index != 0
    ):
        raise ValueError("Phenotype cassette refused unexpected package registration")


def _scrub_phenotype_request(request: Any) -> Any:
    from notebooklm._android.proto.notebooklm.experiments.v1 import exptsandconfigs_pb2

    message = exptsandconfigs_pb2.HeterodyneRequest.FromString(
        _body_bytes(request.body, location="Phenotype request body")
    )
    clean = ProtoRedactor(trust_placeholders=True)(
        _PHENOTYPE_METHOD,
        "request",
        message,
    )
    request.body = clean.SerializeToString(deterministic=True)
    _retain_headers(request.headers, _SAFE_REQUEST_HEADERS)
    _normalize_user_agent(request.headers)
    _set_content_length(request.headers, len(request.body))
    return request


def _scrub_phenotype_response(response: dict[str, Any]) -> dict[str, Any]:
    from notebooklm._android.proto.notebooklm.experiments.v1 import exptsandconfigs_pb2

    body = response.get("body", {})
    status = response.get("status", {}).get("code")
    if status != 200:
        body["string"] = b""
        _retain_headers(response.get("headers", {}), _SAFE_RESPONSE_HEADERS)
        _set_content_length(response.get("headers", {}), 0)
        return response
    content = _body_bytes(body.get("string", b""), location="Phenotype response body")
    message = exptsandconfigs_pb2.HeterodyneResponse.FromString(content)
    clean = ProtoRedactor(trust_placeholders=True)(
        _PHENOTYPE_METHOD,
        "response",
        message,
    )
    body["string"] = clean.SerializeToString(deterministic=True)
    _retain_headers(response.get("headers", {}), _SAFE_RESPONSE_HEADERS)
    _set_content_length(response.get("headers", {}), len(body["string"]))
    return response


class PhenotypeHttpPost:
    """One-use HTTP callback bound to a sanitized Phenotype cassette."""

    def __init__(self, recorder: vcr.VCR, cassette_name: str) -> None:
        self._recorder = recorder
        self._cassette_name = cassette_name
        self._used = False
        self._interaction_count: int | None = None

    async def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        if url != phenotype._ENDPOINT:
            raise ValueError("Phenotype cassette refused an unexpected endpoint")
        from notebooklm._android.proto.notebooklm.experiments.v1 import exptsandconfigs_pb2

        _validate_phenotype_request(exptsandconfigs_pb2.HeterodyneRequest.FromString(body))
        normalized_headers = {key.casefold(): value for key, value in headers.items()}
        if set(normalized_headers) != {"authorization", "content-type", "user-agent"}:
            raise ValueError("Phenotype cassette refused unexpected request headers")
        if (
            not normalized_headers["authorization"].startswith("Bearer ")
            or normalized_headers["authorization"] == "Bearer "
            or normalized_headers["content-type"] != "application/x-protobuf"
            or not normalized_headers["user-agent"]
        ):
            raise ValueError("Phenotype cassette refused malformed request headers")
        if self._used:
            raise RuntimeError("Phenotype cassette permits exactly one HTTP interaction")
        self._used = True
        with self._recorder.use_cassette(self._cassette_name) as cassette:
            response = await phenotype._default_http_post(url, body, headers)
            self._interaction_count = len(cassette)
            return response

    def assert_consumed(self) -> None:
        if not self._used:
            raise AssertionError("Phenotype cassette expected exactly one HTTP interaction")
        if self._interaction_count != 1:
            raise AssertionError(
                "Phenotype cassette expected exactly one HTTP interaction, "
                f"found {self._interaction_count}"
            )


def build_phenotype_http_post(cassette_path: Path) -> PhenotypeHttpPost:
    """Return an HTTP callback bound only to one sanitized Phenotype cassette."""

    record = os.environ.get("NOTEBOOKLM_ANDROID_GRPC_RECORD", "").casefold() in {
        "1",
        "true",
        "yes",
    }
    recorder = vcr.VCR(
        cassette_library_dir=str(cassette_path.parent),
        record_mode="all" if record else "none",
        match_on=[
            "method",
            "scheme",
            "host",
            "port",
            "path",
            "query",
            "body",
            "headers",
        ],
        before_record_request=_scrub_phenotype_request,
        before_record_response=_scrub_phenotype_response,
        filter_headers=["Authorization"],
        decode_compressed_response=True,
    )
    return PhenotypeHttpPost(recorder, cassette_path.name)


__all__ = ["PhenotypeHttpPost", "build_phenotype_http_post"]
