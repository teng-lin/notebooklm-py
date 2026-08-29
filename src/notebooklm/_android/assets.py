"""Credential-safe Android artifact asset transfer."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from .._artifact._download_client import _is_trusted_download_host
from .._artifact.downloads import AssetDownloadService, DownloadResult
from .._curl_cffi_transport import resolve_transport_factory
from .._loop_affinity import assert_bound_loop
from .._runtime.call_supervisor import CallSupervisor
from ..exceptions import ArtifactDownloadError, UnsupportedOperationError
from .auth import BearerCredential, BearerProvider
from .errors import sanitize_escaping_exception

logger = logging.getLogger(__name__)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_HOPS = 8
_MAX_APPLICATION_REDIRECT_BYTES = 8_192
_MIB = 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PRIMARY_BEARER_HOST = "lh3.googleusercontent.com"
_BEARER_HOSTS = frozenset(
    {
        "contribution.usercontent.google.com",
        _PRIMARY_BEARER_HOST,
    }
)
_ANDROID_DOWNLOAD_HOST_SUFFIXES = (".googlevideo.com", ".usercontent.google.com")
_SLIDE_CAPABILITY_INITIAL_HOSTS = frozenset({"contribution.usercontent.google.com"})

RepresentationKind = Literal[
    "infographic",
    "audio",
    "video",
    "slide_pdf",
    "slide_pptx",
]


@dataclass(frozen=True)
class _FormatPolicy:
    media_types: frozenset[str]
    signature_checks: tuple[tuple[bytes, int], ...]
    suffix_replacement: tuple[str, str] | None = None


@dataclass(frozen=True)
class _RepresentationPolicy:
    artifact_type: str
    formats: tuple[_FormatPolicy, ...]
    max_bytes: int
    capability_initial_hosts: frozenset[str] = frozenset()


_REPRESENTATION_POLICIES: dict[RepresentationKind, _RepresentationPolicy] = {
    "infographic": _RepresentationPolicy(
        artifact_type="infographic",
        formats=(_FormatPolicy(frozenset({"image/png"}), ((_PNG_SIGNATURE, 0),)),),
        max_bytes=100 * _MIB,
    ),
    "audio": _RepresentationPolicy(
        artifact_type="audio",
        formats=(
            _FormatPolicy(frozenset({"audio/mp4"}), ((b"ftyp", 4),)),
            _FormatPolicy(
                frozenset({"audio/wav", "audio/x-wav"}),
                ((b"RIFF", 0), (b"WAVE", 8)),
                (".m4a", ".wav"),
            ),
        ),
        max_bytes=512 * _MIB,
    ),
    "video": _RepresentationPolicy(
        artifact_type="video",
        formats=(_FormatPolicy(frozenset({"video/mp4"}), ((b"ftyp", 4),)),),
        max_bytes=2 * 1024 * _MIB,
    ),
    "slide_pdf": _RepresentationPolicy(
        artifact_type="slide_deck",
        formats=(
            _FormatPolicy(
                frozenset({"application/octet-stream", "application/pdf"}),
                ((b"%PDF-", 0),),
            ),
        ),
        max_bytes=512 * _MIB,
        capability_initial_hosts=_SLIDE_CAPABILITY_INITIAL_HOSTS,
    ),
    "slide_pptx": _RepresentationPolicy(
        artifact_type="slide_deck",
        formats=(
            _FormatPolicy(
                frozenset(
                    {
                        "application/octet-stream",
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    }
                ),
                ((b"PK\x03\x04", 0),),
            ),
        ),
        max_bytes=512 * _MIB,
        capability_initial_hosts=_SLIDE_CAPABILITY_INITIAL_HOSTS,
    ),
}


@dataclass(frozen=True)
class TransferFailure:
    """Bounded failure receipt that cannot retain a secret URL or response."""

    code: str
    approved_host: str
    hop: int


@dataclass(frozen=True)
class _TransferSuccess:
    output_path: str
    byte_count: int


@dataclass(frozen=True)
class _CloseOutcome:
    process_exit: BaseException | None
    close_failed: bool


def _default_client_factory() -> Any:
    factory = resolve_transport_factory()
    return factory(cookies=None, follow_redirects=False, timeout=60.0)


def _safe_approved_host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return "<rejected>"
    return host if _is_android_download_host(host) else "<rejected>"


def _is_android_download_host(host: str) -> bool:
    if (
        not host
        or len(host) > 253
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in host)
    ):
        return False
    labels = host.split(".")
    if any(
        not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
        for label in labels
    ):
        return False
    return _is_trusted_download_host(host) or any(
        host.endswith(suffix) and host != suffix.removeprefix(".")
        for suffix in _ANDROID_DOWNLOAD_HOST_SUFFIXES
    )


def _validated_host(url: str) -> str | None:
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (443 if port is None else port) != 443
        or not _is_android_download_host(host)
    ):
        return None
    return host


def _append_initial_alr(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        alr_values = [
            value for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key == "alr"
        ]
    except ValueError:
        return None
    if alr_values:
        return url if alr_values == ["yes"] else None
    query = f"{parsed.query}&alr=yes" if parsed.query else "alr=yes"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def _bearer_for(host: str, credential: BearerCredential | None) -> dict[str, str]:
    if host in _BEARER_HOSTS and credential is not None:
        return {"Authorization": f"Bearer {credential.token}"}
    return {}


def _clear_client_cookies(client: Any) -> None:
    """Keep each manually validated hop free of ambient or response-issued cookies."""

    owners = (client, getattr(client, "_curl", None))
    for owner in owners:
        cookies = getattr(owner, "cookies", None)
        clear = getattr(cookies, "clear", None)
        if callable(clear):
            clear()


def _format_for_media_type(
    policy: _RepresentationPolicy,
    media_type: str,
) -> _FormatPolicy | None:
    return next((item for item in policy.formats if media_type in item.media_types), None)


def _declared_content_length(headers: Any) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return -1
    return value if value >= 0 else -1


def _fsync_directory(path: Path) -> None:
    """Make the atomic rename durable where the platform exposes directory fsync."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


async def _close_clients_and_settle_tasks(
    clients: tuple[Any, ...],
    tasks: tuple[asyncio.Task[Any], ...],
) -> _CloseOutcome:
    """Break active I/O and observe every retired transfer before reopen."""

    process_exit: BaseException | None = None
    close_failed = False
    for client in clients:
        try:
            await client.aclose()
        except (KeyboardInterrupt, SystemExit) as error:
            if process_exit is None:
                process_exit = sanitize_escaping_exception(error)
        except BaseException:
            close_failed = True

    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except (KeyboardInterrupt, SystemExit) as error:
            if process_exit is None:
                process_exit = sanitize_escaping_exception(error)
        except BaseException:
            # The original caller owns its already-sanitized public failure.
            pass
    return _CloseOutcome(process_exit, close_failed)


async def _bounded_text(response: Any) -> bytes | None:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAX_APPLICATION_REDIRECT_BYTES:
            return None
    return bytes(body)


def _single_location(headers: Any) -> str | None:
    get_list = getattr(headers, "get_list", None)
    values = get_list("location") if callable(get_list) else []
    if not values:
        value = headers.get("location")
        values = [] if value is None else [value]
    if len(values) != 1:
        return None
    location, *_unexpected = values
    return location or None


class AndroidAssetDownloadService(AssetDownloadService):
    """One manual response loop with per-hop bearer decisions and lifecycle fencing."""

    name = "android-assets"

    def __init__(
        self,
        *,
        bearer_provider: BearerProvider,
        supervisor: CallSupervisor,
        client_factory: Callable[[], Any] = _default_client_factory,
    ) -> None:
        super().__init__(storage_path=None)
        self._bearer_provider = bearer_provider
        self._supervisor = supervisor
        self._client_factory = client_factory
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._active_epoch: int | None = None
        self._closing = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._clients: set[Any] = set()

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        """Activate an empty resource registry for one client generation."""

        assert_bound_loop(loop)
        self._bound_loop = loop
        self._active_epoch = epoch
        self._closing = False
        self._tasks.clear()
        self._clients.clear()

    async def prepare_close(self) -> None:
        """Fence publication and cancel forced-close transfer tasks."""

        if self._bound_loop is not None:
            assert_bound_loop(self._bound_loop)
        self._closing = True
        self._active_epoch = None
        current = asyncio.current_task()
        for task in tuple(self._tasks):
            if task is not current:
                task.cancel()

    async def close_resources(self) -> None:
        """Close active I/O and settle every retired transfer before returning."""

        if self._bound_loop is not None:
            assert_bound_loop(self._bound_loop)
        current = asyncio.current_task()
        tasks = tuple(task for task in self._tasks if task is not current)
        clients = tuple(self._clients)
        for task in tasks:
            task.cancel()

        settlement = asyncio.create_task(_close_clients_and_settle_tasks(clients, tasks))
        cancellation: asyncio.CancelledError | None = None
        try:
            while True:
                try:
                    outcome = await asyncio.shield(settlement)
                    break
                except asyncio.CancelledError as error:
                    if cancellation is None:
                        cancellation = error
                    continue
        finally:
            self._clients.clear()
            self._tasks.clear()
        del clients, current, self, settlement, tasks
        if outcome.process_exit is not None:
            raise sanitize_escaping_exception(outcome.process_exit) from None
        if cancellation is not None:
            raise sanitize_escaping_exception(cancellation) from None
        if outcome.close_failed:
            raise RuntimeError("Android asset transport close failed.") from None

    def _assert_epoch(self, expected_epoch: int) -> None:
        assert_bound_loop(self._bound_loop)
        if self._closing or self._active_epoch != expected_epoch:
            raise RuntimeError(
                "Android asset transfer belongs to a retired resource generation "
                f"(expected={expected_epoch}, active={self._active_epoch})."
            )

    async def download_url(self, url: str, output_path: str) -> str:
        """Download one PNG without retaining credentials in an escaping traceback."""

        service = self
        result: str | None = None
        failure: BaseException | None = None
        try:
            result = await service._download_public(
                url,
                output_path,
                policy=_REPRESENTATION_POLICIES["infographic"],
            )
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            del self, service, url
        if failure is not None:
            raise failure
        assert result is not None
        return result

    async def download_representation(
        self,
        url: str,
        output_path: str,
        *,
        representation: RepresentationKind,
    ) -> str:
        """Transfer one exact artifact representation under its MIME/signature policy."""

        service = self
        result: str | None = None
        failure: BaseException | None = None
        try:
            policy = _REPRESENTATION_POLICIES.get(representation)
            if policy is None:
                raise ValueError(f"Unsupported Android artifact representation: {representation}")
            result = await service._download_public(url, output_path, policy=policy)
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            del self, service, url
        if failure is not None:
            raise failure
        assert result is not None
        return result

    async def _download_public(
        self,
        representation_url: str,
        output_path: str,
        *,
        policy: _RepresentationPolicy,
    ) -> str:
        """Run one policy-selected transfer without retaining its capability URL."""

        service = self
        failure: BaseException | None = None
        outcome: _TransferSuccess | TransferFailure | None = None
        artifact_type = policy.artifact_type
        try:
            outcome = await service._download_impl(
                representation_url,
                output_path,
                policy,
            )
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            del self, service, representation_url, policy
        if failure is not None:
            raise failure
        assert outcome is not None
        if isinstance(outcome, TransferFailure):
            public_error = ArtifactDownloadError(
                artifact_type,
                details=(
                    "Android transfer failed "
                    f"(code={outcome.code}, host={outcome.approved_host}, hop={outcome.hop})."
                ),
                cause=None,
                status_code=(
                    int(outcome.code.removeprefix("http_"))
                    if outcome.code.startswith("http_")
                    else None
                ),
            )
            public_error.__cause__ = None
            public_error.__context__ = None
            raise public_error from None
        return outcome.output_path

    async def _download_impl(
        self,
        representation_url: str,
        output_path: str,
        policy: _RepresentationPolicy,
    ) -> _TransferSuccess | TransferFailure:
        expected_epoch = self._active_epoch
        if expected_epoch is None:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        async with self._supervisor.operation_scope(
            "android artifact download",
            expected_epoch=expected_epoch,
        ) as lease:
            self._assert_epoch(lease.epoch)
            task = asyncio.current_task()
            if task is None:
                raise RuntimeError("Android asset transfer has no owning task.")
            self._tasks.add(task)
            try:
                return await self._transfer_worker(
                    representation_url,
                    output_path,
                    policy,
                    expected_epoch=lease.epoch,
                )
            finally:
                self._tasks.discard(task)

    async def _transfer_worker(
        self,
        representation_url: str,
        output_path: str,
        policy: _RepresentationPolicy,
        *,
        expected_epoch: int,
    ) -> _TransferSuccess | TransferFailure:
        current_url: str | None = representation_url
        client: Any | None = None
        credential: BearerCredential | None = None
        bearer_allowed = False
        staging: Path | None = None
        try:
            initial_host = _validated_host(representation_url)
            if initial_host == _PRIMARY_BEARER_HOST or initial_host in (
                policy.capability_initial_hosts
            ):
                current_url = _append_initial_alr(representation_url)
                bearer_allowed = True
            else:
                current_url = None
            if current_url is None:
                return TransferFailure(
                    "url_policy",
                    _safe_approved_host(representation_url),
                    0,
                )
            assert initial_host is not None

            self._assert_epoch(expected_epoch)
            if initial_host in _BEARER_HOSTS:
                credential = await self._bearer_provider.get(expected_epoch)
                self._assert_epoch(expected_epoch)
            try:
                client = self._client_factory()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                return TransferFailure("transport", initial_host, 0)
            self._clients.add(client)

            for hop in range(_MAX_HOPS + 1):
                self._assert_epoch(expected_epoch)
                assert current_url is not None
                host = _validated_host(current_url)
                if host is None:
                    return TransferFailure(
                        "url_policy",
                        _safe_approved_host(current_url),
                        hop,
                    )
                if host not in _BEARER_HOSTS:
                    bearer_allowed = False
                headers = _bearer_for(host, credential if bearer_allowed else None)
                response_cm: Any | None = None
                response: Any | None = None
                try:
                    _clear_client_cookies(client)
                    response_cm = client.stream(
                        "GET",
                        current_url,
                        headers=headers,
                        follow_redirects=False,
                    )
                    response = await response_cm.__aenter__()
                    status = int(response.status_code)
                    logger.debug("Android asset hop host=%s hop=%d status=%d", host, hop, status)

                    if status in _REDIRECT_STATUSES:
                        location = _single_location(response.headers)
                        if location is None:
                            return TransferFailure("redirect", host, hop)
                        current_url = urljoin(current_url, location)
                        continue
                    if status != 200:
                        if status == 401 and headers and credential is not None:
                            self._bearer_provider.invalidate(credential.generation)
                        return TransferFailure(f"http_{status}", host, hop)

                    content_type = response.headers.get("content-type", "")
                    media_type = content_type.split(";", 1)[0].strip().lower()
                    if media_type == "text/plain":
                        body = await _bounded_text(response)
                        if body is None:
                            return TransferFailure("application_redirect_size", host, hop)
                        try:
                            redirect_url = body.decode("utf-8", errors="strict").strip()
                        except UnicodeDecodeError:
                            return TransferFailure("application_redirect_encoding", host, hop)
                        if not redirect_url or any(ord(char) < 32 for char in redirect_url):
                            return TransferFailure("application_redirect", host, hop)
                        current_url = redirect_url
                        continue
                    format_policy = _format_for_media_type(policy, media_type)
                    if format_policy is None:
                        return TransferFailure("content_type", host, hop)
                    declared_length = _declared_content_length(response.headers)
                    if declared_length is not None and (
                        declared_length < 0 or declared_length > policy.max_bytes
                    ):
                        return TransferFailure("size", host, hop)

                    destination = Path(output_path)
                    if (
                        format_policy.suffix_replacement is not None
                        and destination.suffix.lower() == format_policy.suffix_replacement[0]
                    ):
                        destination = destination.with_suffix(format_policy.suffix_replacement[1])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    fd, staging_name = tempfile.mkstemp(
                        prefix=f".{destination.name}.",
                        suffix=".part",
                        dir=destination.parent,
                    )
                    staging = Path(staging_name)
                    total = 0
                    prefix = bytearray()
                    signatures = format_policy.signature_checks
                    required_prefix = max(
                        offset + len(signature) for signature, offset in signatures
                    )
                    with os.fdopen(fd, "wb") as handle:
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            if total + len(chunk) > policy.max_bytes:
                                return TransferFailure("size", host, hop)
                            if len(prefix) < required_prefix:
                                prefix.extend(chunk[: required_prefix - len(prefix)])
                            handle.write(chunk)
                            total += len(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if total == 0:
                        return TransferFailure("empty", host, hop)
                    if not all(
                        bytes(prefix[offset : offset + len(signature)]) == signature
                        for signature, offset in signatures
                    ):
                        return TransferFailure("signature", host, hop)
                    self._assert_epoch(expected_epoch)
                    os.replace(staging, destination)
                    staging = None
                    _fsync_directory(destination.parent)
                    logger.debug(
                        "Android asset complete host=%s hop=%d bytes=%d",
                        host,
                        hop,
                        total,
                    )
                    return _TransferSuccess(str(destination), total)
                except asyncio.CancelledError:
                    raise
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException:
                    return TransferFailure("transport", host, hop)
                finally:
                    if response_cm is not None:
                        try:
                            await response_cm.__aexit__(None, None, None)
                        except (KeyboardInterrupt, SystemExit):
                            raise
                        except BaseException:
                            pass
                    _clear_client_cookies(client)
                    del headers, response, response_cm
            return TransferFailure(
                "too_many_hops",
                _safe_approved_host(current_url),
                _MAX_HOPS,
            )
        finally:
            if staging is not None:
                try:
                    staging.unlink(missing_ok=True)
                except OSError:
                    pass
            if client is not None:
                try:
                    await client.aclose()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException:
                    pass
                self._clients.discard(client)
            del credential, client, current_url, policy, representation_url, staging

    async def download_urls_batch(self, urls_and_paths: list[tuple[str, str]]) -> DownloadResult:
        """Keep the artifact contract's one-representation transfer boundary explicit."""

        del urls_and_paths
        raise UnsupportedOperationError(
            "Android artifact batch download is not supported by the Android backend."
        )


__all__ = ["AndroidAssetDownloadService", "RepresentationKind", "TransferFailure"]
