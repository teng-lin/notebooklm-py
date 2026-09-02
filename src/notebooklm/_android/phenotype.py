"""Headless GMS Phenotype ``serverToken`` acquisition for the Android backend.

Adding a Google Play Book (an ``ExpertIntelligenceContent`` source, #2292) over
the Android gRPC surface requires one extra request-metadata header,
``x-goog-ext-202964622-bin`` — a ``ServerTokens`` proto carrying a per-account
Mendel *experiment token*. On a real device the app reads this token from Google
Play Services (the ``plugins.flutter.io/phenotype`` platform channel); a headless
client has no GMS, so the Android ``AddSources`` for a Play Book returns
``INTERNAL`` and the source sticks at ``preparing``.

This module reproduces the one GMS call that mints the token. It POSTs a
*single-package* registration to the private Phenotype/Heterodyne endpoint

    https://www.googleapis.com/experimentsandconfigs/v1/getExperimentsAndConfigs

with a bearer the client already mints (the ``experimentsandconfigs`` scope is in
:data:`notebooklm._android.auth._ANDROID_SCOPES`), reads the returned
``serverToken``, and wraps it into the header the notebooklm-pa backend expects.
The device-wide registration GMS normally sends (~2,862 packages) is **not**
required — the request's package list is repeated, so one entry suffices.

The minted token is server-side experiment state for ``(account, device)``: it
cannot be computed locally, only fetched, and it is long-lived (hours), so it is
cached with a conservative TTL and refreshed on demand.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from ..exceptions import MissingDependencyError, NotebookLMError

# A fixed, plausible device identity. Verified live: the experiment assignment
# that gates Play Books is keyed on the *account* (via the bearer), not on the
# device, so a constant synthetic profile mints a working token for any user and
# no per-account device data is needed. Overridable via
# ``PhenotypeTokenProvider(device_profile=...)`` for callers that prefer to
# present their real device.
_DEFAULT_ANDROID_ID = 0x39A7B21C4D5E6F80

# --- Private Phenotype/Heterodyne wire constants ---------------------------
#
# These track a specific Gemini Notebook (tailwind) + GMS build and drift when
# Google ships new ones; bump them from a fresh capture if the fetch starts
# returning an empty token. See docs/rpc-development.md.
_ENDPOINT = "https://www.googleapis.com/experimentsandconfigs/v1/getExperimentsAndConfigs?r=8&c=1"
_FETCH_REASON = 8
_CONFIG_CLASS = 1
_HOST_PACKAGE = "com.google.android.apps.labs.language.tailwind"
_MENDEL_PACKAGE = f"com.google.labs.language.tailwind.mobile#{_HOST_PACKAGE}"
_MENDEL_VERSION = 153888

# gRPC metadata keys forwarded to notebooklm-pa on the Play Books add path.
CLIENT_TYPE_HEADER = "x-goog-ext-174067345-bin"
EXPERIMENT_TOKEN_HEADER = "x-goog-ext-202964622-bin"
# Static {1:{1:3}} — client-type ANDROID marker the app always sends alongside.
_CLIENT_TYPE_VALUE = bytes.fromhex("0a020803")

_DEFAULT_TTL_SECONDS = 1800.0

_MISSING_TOKEN_MESSAGE = (
    "GMS Phenotype returned no experiment token for the Play Books package; "
    "the Android Play Books add path cannot be unlocked."
)


class PhenotypeError(NotebookLMError):
    """The Phenotype ``getExperimentsAndConfigs`` fetch could not be completed."""


@dataclass(frozen=True)
class AndroidDeviceProfile:
    """Device identity presented to Phenotype when minting the token.

    Defaults are a fixed, plausible Pixel-class profile. They feed the request's
    ``DeviceInfo`` and were verified to mint a working token; because the
    experiment assignment is account-keyed, the concrete device values do not
    matter as long as they are well-formed.
    """

    android_id: int = _DEFAULT_ANDROID_ID
    sdk_version: int = 34
    model: str = "Pixel 8"
    product: str = "shiba"
    device: str = "shiba"
    board: str = "shiba"
    brand: str = "google"
    manufacturer: str = "Google"
    hardware: str = "shiba"
    build_type: str = "user"
    build_id: str = "AP2A.240905.003"
    build_fingerprint: str = "google/shiba/shiba:14/AP2A.240905.003/12231197:user/release-keys"
    gms_version_string: str = "25.34.34"
    gms_version_code: int = 253434035
    language: str = "en"
    country: str = "US"
    radio_version: str = "g5300"

    @property
    def user_agent(self) -> str:
        return (
            f"com.google.android.gms/{self.gms_version_code} "
            f"(Linux; U; Android {self.sdk_version}; "
            f"{self.language}_{self.country}; {self.model}; "
            f"Build/{self.build_id})"
        )


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def _wrap_server_tokens(server_token_message: bytes) -> bytes:
    """Wrap one ``serverToken`` message as the ``ServerTokens`` header value."""

    return b"\x0a" + _varint(len(server_token_message)) + server_token_message


def _decode_server_token(server_token_b64url: str) -> bytes:
    padded = server_token_b64url.replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    return base64.b64decode(padded)


def _build_request(profile: AndroidDeviceProfile) -> bytes:
    from .proto.notebooklm.experiments.v1 import (  # noqa: PLC0415
        exptsandconfigs_pb2,
    )

    pb = cast(Any, exptsandconfigs_pb2)
    request = pb.HeterodyneRequest()
    header = request.header.clearcut_logger_header
    header.log_source = 4
    header.timestamp_millis = int(time.time() * 1000)
    info = header.device_info
    info.android_id = profile.android_id
    info.sdk_version = profile.sdk_version
    info.model = profile.model
    info.product = profile.product
    info.device = profile.device
    info.board = profile.board
    info.brand = profile.brand
    info.manufacturer = profile.manufacturer
    info.hardware = profile.hardware
    info.build_type = profile.build_type
    info.build_id = profile.build_id
    info.build_fingerprint = profile.build_fingerprint
    info.gms_version_string = profile.gms_version_string
    info.gms_version_code = profile.gms_version_code
    info.language = profile.language
    info.country = profile.country
    info.radio_version = profile.radio_version

    entry = request.data.add()
    entry.package_details.package_name = _MENDEL_PACKAGE
    entry.package_details.version = _MENDEL_VERSION
    entry.package_details.auth_token_index.index = 0

    request.fetch_reason = _FETCH_REASON
    request.config_class = _CONFIG_CLASS
    request.package_name = _HOST_PACKAGE
    return request.SerializeToString()


def _extract_server_token(response_bytes: bytes) -> bytes:
    from .proto.notebooklm.experiments.v1 import (  # noqa: PLC0415
        exptsandconfigs_pb2,
    )

    pb = cast(Any, exptsandconfigs_pb2)
    try:
        response = pb.HeterodyneResponse()
        response.ParseFromString(response_bytes)
        for config in response.heterodyne_config:
            if config.server_token:
                return _decode_server_token(config.server_token)
    except Exception as exc:
        raise PhenotypeError("GMS Phenotype returned a malformed experiment response.") from exc
    raise PhenotypeError(_MISSING_TOKEN_MESSAGE)


# Injectable POST seam: (url, body, headers) -> (status, response_bytes).
HttpPost = Callable[[str, bytes, dict[str, str]], Awaitable[tuple[int, bytes]]]


async def _default_http_post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
    try:
        import httpx  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised via MissingDependency
        raise MissingDependencyError(
            "The Android Play Books path needs httpx. Install: pip install 'notebooklm-py[android]'"
        ) from exc
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, content=body, headers=headers)
        return response.status_code, response.content


@dataclass
class _CachedToken:
    header_value: bytes = field(repr=False)
    expires_at: float


class PhenotypeTokenProvider:
    """Fetch, cache, and refresh the Play Books experiment-token metadata.

    One instance is shared across the Android client. Concurrent first callers
    may perform the same idempotent fetch more than once; the last successful
    result wins. The cached header is reused until its TTL lapses,
    :meth:`invalidate` is called, or the client lifecycle closes/reopens.
    """

    name = "android-phenotype"

    def __init__(
        self,
        device_profile: AndroidDeviceProfile | None = None,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        http_post: HttpPost | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._profile = device_profile or AndroidDeviceProfile()
        self._ttl = ttl_seconds
        self._http_post = http_post or _default_http_post
        self._monotonic = monotonic
        self._cached: _CachedToken | None = None

    def invalidate(self) -> None:
        """Drop the cached token so the next call re-fetches."""

        self._cached = None

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        """Start a client generation with no token from a previous account."""

        del loop, epoch
        self.invalidate()

    async def prepare_close(self) -> None:
        """Fence cached account-bound metadata before credentials are reloaded."""

        self.invalidate()

    async def close_resources(self) -> None:
        """Complete the transport-lifecycle protocol; no resource is retained."""

    async def experiment_metadata(
        self, bearer: str, *, force: bool = False
    ) -> tuple[tuple[str, bytes], ...]:
        """Return the gRPC metadata unlocking the Play Books add path.

        ``bearer`` is reused for the Phenotype POST (its all-scopes token
        already carries ``experimentsandconfigs``). Pass ``force=True`` to
        bypass the cache after a stale-token ``INTERNAL`` from ``AddSources``.
        """

        token_header = await self._server_token_header(bearer, force=force)
        return (
            (CLIENT_TYPE_HEADER, _CLIENT_TYPE_VALUE),
            (EXPERIMENT_TOKEN_HEADER, token_header),
        )

    async def _server_token_header(self, bearer: str, *, force: bool) -> bytes:
        # No lock: the fetch is idempotent, so the double-checked cache is
        # correct without loop-bound synchronisation — a rare concurrent first
        # fetch simply repeats work and the last writer wins.
        if not force:
            cached = self._cached
            if cached is not None and cached.expires_at > self._monotonic():
                return cached.header_value
        header_value = await self._fetch(bearer)
        self._cached = _CachedToken(
            header_value=header_value,
            expires_at=self._monotonic() + self._ttl,
        )
        return header_value

    async def _fetch(self, bearer: str) -> bytes:
        body = _build_request(self._profile)
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/x-protobuf",
            "User-Agent": self._profile.user_agent,
        }
        try:
            status, response_bytes = await self._http_post(_ENDPOINT, body, headers)
        except (PhenotypeError, MissingDependencyError):
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed failure
            raise PhenotypeError(
                f"GMS Phenotype fetch failed to reach the endpoint: {exc}"
            ) from exc
        if status != 200:
            raise PhenotypeError(
                f"GMS Phenotype fetch returned HTTP {status}; "
                "cannot unlock the Play Books add path."
            )
        server_token = _extract_server_token(response_bytes)
        return _wrap_server_tokens(server_token)
