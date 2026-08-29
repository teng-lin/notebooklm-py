"""Credential-safe Android artifact asset transfer."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
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
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_BEARER_HOST = "lh3.googleusercontent.com"


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
    return host if _is_trusted_download_host(host) else "<rejected>"


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
        or not _is_trusted_download_host(host)
    ):
        return None
    return host


def _append_initial_alr(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        alr_values = [
            value
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key == "alr"
        ]
    except ValueError:
        return None
    if alr_values:
        return url if alr_values == ["yes"] else None
    query = f"{parsed.query}&alr=yes" if parsed.query else "alr=yes"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def _bearer_for(host: str, credential: BearerCredential) -> dict[str, str]:
    if host == _BEARER_HOST:
        return {"Authorization": f"Bearer {credential.token}"}
    return {}


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
        failure: BaseException | None = None
        outcome: _TransferSuccess | TransferFailure | None = None
        try:
            outcome = await service._download_impl(url, output_path)
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            del self, service, url
        if failure is not None:
            raise failure
        assert outcome is not None
        if isinstance(outcome, TransferFailure):
            public_error = ArtifactDownloadError(
                "infographic",
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
                    expected_epoch=lease.epoch,
                )
            finally:
                self._tasks.discard(task)

    async def _transfer_worker(
        self,
        representation_url: str,
        output_path: str,
        *,
        expected_epoch: int,
    ) -> _TransferSuccess | TransferFailure:
        current_url: str | None = representation_url
        client: Any | None = None
        credential: BearerCredential | None = None
        staging: Path | None = None
        try:
            initial_host = _validated_host(representation_url)
            current_url = _append_initial_alr(representation_url)
            if initial_host != _BEARER_HOST or current_url is None:
                return TransferFailure(
                    "url_policy",
                    _safe_approved_host(representation_url),
                    0,
                )

            self._assert_epoch(expected_epoch)
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
                headers = _bearer_for(host, credential)
                response_cm: Any | None = None
                response: Any | None = None
                try:
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
                        if status == 401 and host == _BEARER_HOST:
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
                    if media_type != "image/png":
                        return TransferFailure("content_type", host, hop)

                    destination = Path(output_path)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    fd, staging_name = tempfile.mkstemp(
                        prefix=f".{destination.name}.",
                        suffix=".part",
                        dir=destination.parent,
                    )
                    staging = Path(staging_name)
                    total = 0
                    prefix = bytearray()
                    with os.fdopen(fd, "wb") as handle:
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            if len(prefix) < len(_PNG_SIGNATURE):
                                prefix.extend(chunk[: len(_PNG_SIGNATURE) - len(prefix)])
                            handle.write(chunk)
                            total += len(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if total == 0:
                        return TransferFailure("empty", host, hop)
                    if bytes(prefix) != _PNG_SIGNATURE:
                        return TransferFailure("signature", host, hop)
                    self._assert_epoch(expected_epoch)
                    os.replace(staging, destination)
                    staging = None
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
            del credential, client, current_url, representation_url, staging

    async def download_urls_batch(self, urls_and_paths: list[tuple[str, str]]) -> DownloadResult:
        """Keep B4's one-representation transfer boundary explicit."""

        del urls_and_paths
        raise UnsupportedOperationError(
            "Android artifact batch download is not supported by the Android backend."
        )


__all__ = ["AndroidAssetDownloadService", "TransferFailure"]
