"""Credential-safe Android artifact asset transfer."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .._artifact._download_client import _is_trusted_download_host
from .._artifact._guarded_transfer import (
    FormatPolicy as _FormatPolicy,
)
from .._artifact._guarded_transfer import (
    TransferFailure,
    _await_advisory_cleanup,
    guarded_transfer,
)
from .._artifact._guarded_transfer import (
    TransferPolicy as _RepresentationPolicy,
)
from .._artifact._guarded_transfer import (
    TransferSuccess as _TransferSuccess,
)
from .._artifact.downloads import AssetDownloadService, DownloadResult
from .._curl_cffi_transport import resolve_transport_factory
from .._hop_credentials import CredentialPolicy, HopCredentials
from .._loop_affinity import assert_bound_loop
from .._loop_bound import EpochFenced
from .._runtime.call_supervisor import CallSupervisor
from ..exceptions import ArtifactDownloadError, AuthError
from .auth import BearerProvider
from .errors import sanitize_escaping_exception

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


def _prepare_transfer_url(url: str, policy: _RepresentationPolicy) -> str | None:
    initial_host = _validated_host(url)
    if initial_host != _PRIMARY_BEARER_HOST and initial_host not in policy.capability_initial_hosts:
        return None
    return _append_initial_alr(url)


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


class _StickyBearerPolicy:
    """Re-acquire a bearer on each eligible hop, permanently dropping it off-list."""

    def __init__(self, provider: BearerProvider, expected_epoch: int) -> None:
        self._provider = provider
        self._expected_epoch = expected_epoch
        self._bearer_allowed = True
        self.last_generation: int | None = None

    async def __call__(self, url: str) -> HopCredentials | None:
        host = _validated_host(url)
        if host not in _BEARER_HOSTS:
            self._bearer_allowed = False
        if not self._bearer_allowed or host not in _BEARER_HOSTS:
            self.last_generation = None
            return None
        self.last_generation = None
        credential = await self._provider.get(self._expected_epoch)
        self.last_generation = credential.generation
        return HopCredentials(headers={"Authorization": f"Bearer {credential.token}"})

    def invalidate_last(self) -> None:
        if self.last_generation is not None:
            self._provider.invalidate(self.last_generation)


class AndroidAssetDownloadService(EpochFenced, AssetDownloadService):
    """One manual response loop with per-hop bearer decisions and lifecycle fencing."""

    name = "android-assets"

    def __init__(
        self,
        *,
        bearer_provider: BearerProvider,
        supervisor: CallSupervisor,
        client_factory: Callable[[], Any] = _default_client_factory,
    ) -> None:
        AssetDownloadService.__init__(self, storage_path=None, chain=False)
        EpochFenced.__init__(
            self,
            "Android asset transfer belongs to a retired resource generation",
            assert_loop=True,
        )
        self._bearer_provider = bearer_provider
        self._supervisor = supervisor
        self._client_factory = client_factory
        self._tasks: set[asyncio.Task[Any]] = set()
        self._clients: set[Any] = set()

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        """Activate an empty resource registry for one client generation."""

        assert_bound_loop(loop)
        self.set_bound_loop(loop)
        self.activate(epoch)
        self._tasks.clear()
        self._clients.clear()

    async def prepare_close(self) -> None:
        """Fence publication and cancel forced-close transfer tasks."""

        if self._bound_loop is not None:
            assert_bound_loop(self._bound_loop)
        self.fence()
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
            self.assert_epoch(lease.epoch)
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
        client: Any | None = None
        try:
            prepared_url = _prepare_transfer_url(representation_url, policy)
            if prepared_url is None:
                return TransferFailure("url_policy", _safe_approved_host(representation_url), 0)
            self.assert_epoch(expected_epoch)
            try:
                client = self._client_factory()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                return TransferFailure("transport", _safe_approved_host(representation_url), 0)
            self._clients.add(client)
            return await self._transfer_on_client(
                client,
                prepared_url,
                output_path,
                policy,
                expected_epoch=expected_epoch,
                prepared=True,
            )
        finally:
            pending_error = sys.exc_info()[1]
            try:
                if client is not None:
                    await _await_advisory_cleanup(
                        client.aclose(),
                        pending_error=pending_error,
                    )
            finally:
                if client is not None:
                    self._clients.discard(client)
                del client, pending_error, policy, representation_url

    async def _transfer_on_client(
        self,
        client: Any,
        representation_url: str,
        output_path: str,
        policy: _RepresentationPolicy,
        *,
        expected_epoch: int,
        prepared: bool = False,
    ) -> _TransferSuccess | TransferFailure:
        prepared_url = (
            representation_url if prepared else _prepare_transfer_url(representation_url, policy)
        )
        if prepared_url is None:
            return TransferFailure("url_policy", _safe_approved_host(representation_url), 0)

        credential_for = _StickyBearerPolicy(self._bearer_provider, expected_epoch)
        try:
            return await guarded_transfer(
                client,
                prepared_url,
                output_path,
                policy=policy,
                credential_for=credential_for,
                validate_url=_validated_host,
                safe_host=_safe_approved_host,
                assert_active=lambda: self.assert_epoch(expected_epoch),
                chain=False,
            )
        except AuthError:
            credential_for.invalidate_last()
            raise

    async def download_urls_batch(
        self,
        urls_and_paths: list[tuple[str, str]],
        *,
        credential_policy_factory: Callable[[Any], CredentialPolicy] | None = None,
        on_auth_error: Callable[[str, AuthError], Awaitable[None]] | None = None,
    ) -> DownloadResult:
        """Download a batch through one epoch fence with fresh per-URL bearer policy."""

        if credential_policy_factory is not None or on_auth_error is not None:
            raise TypeError("Android batch download owns its credential and invalidation policy")

        service = self
        failure: BaseException | None = None
        result: DownloadResult | None = None
        try:
            result = await service._download_urls_batch_impl(urls_and_paths)
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            del self, service, urls_and_paths, credential_policy_factory, on_auth_error
        if failure is not None:
            raise failure
        assert result is not None
        return result

    async def _download_urls_batch_impl(
        self, urls_and_paths: list[tuple[str, str]]
    ) -> DownloadResult:
        expected_epoch = self._active_epoch
        if expected_epoch is None:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        async with self._supervisor.operation_scope(
            "android artifact batch download", expected_epoch=expected_epoch
        ) as lease:
            self.assert_epoch(lease.epoch)
            task = asyncio.current_task()
            if task is None:
                raise RuntimeError("Android asset transfer has no owning task.")
            self._tasks.add(task)
            client: Any | None = None
            try:
                client = self._client_factory()
                self._clients.add(client)
                policy = _REPRESENTATION_POLICIES["infographic"]
                current_policy: _StickyBearerPolicy | None = None

                def credential_policy_factory(_cookies: Any) -> CredentialPolicy:
                    nonlocal current_policy
                    current_policy = _StickyBearerPolicy(self._bearer_provider, lease.epoch)
                    return current_policy

                async def on_auth_error(_url: str, _error: AuthError) -> None:
                    if current_policy is not None:
                        current_policy.invalidate_last()

                return await super()._download_guarded_urls_batch(
                    client,
                    urls_and_paths,
                    policy=policy,
                    credential_policy_factory=credential_policy_factory,
                    on_auth_error=on_auth_error,
                    prepare_url=_prepare_transfer_url,
                    validate_url=_validated_host,
                    safe_host=_safe_approved_host,
                    assert_active=lambda: self.assert_epoch(lease.epoch),
                    failure_for=lambda outcome: self._public_transfer_error(policy, outcome),
                )
            finally:
                pending_error = sys.exc_info()[1]
                try:
                    if client is not None:
                        await _await_advisory_cleanup(
                            client.aclose(),
                            pending_error=pending_error,
                        )
                finally:
                    if client is not None:
                        self._clients.discard(client)
                    self._tasks.discard(task)
                    del client, pending_error

    @staticmethod
    def _public_transfer_error(
        policy: _RepresentationPolicy, outcome: TransferFailure
    ) -> ArtifactDownloadError:
        error = ArtifactDownloadError(
            policy.artifact_type,
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
        error.__cause__ = None
        error.__context__ = None
        return error


__all__ = ["AndroidAssetDownloadService", "RepresentationKind", "TransferFailure"]
