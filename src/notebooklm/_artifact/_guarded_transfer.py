"""Backend-neutral guarded streaming transfer for generated artifact assets."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from .._hop_credentials import CredentialPolicy, HopCredentials
from ..exceptions import AuthError

MAX_DOWNLOAD_REDIRECTS = 20
MAX_APPLICATION_REDIRECT_BYTES = 8_192


@dataclass(frozen=True)
class FormatPolicy:
    """Allowed content types and magic-byte checks for one representation format."""

    media_types: frozenset[str]
    signature_checks: tuple[tuple[bytes, int], ...]
    suffix_replacement: tuple[str, str] | None = None


@dataclass(frozen=True)
class TransferPolicy:
    """Bounded content policy for one artifact representation."""

    artifact_type: str
    formats: tuple[FormatPolicy, ...]
    max_bytes: int
    capability_initial_hosts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TransferFailure:
    """Bounded failure receipt that cannot retain a capability URL or response."""

    code: str
    approved_host: str
    hop: int


@dataclass(frozen=True)
class TransferSuccess:
    output_path: str
    byte_count: int


def _clear_client_cookies(client: Any) -> None:
    """Keep every manually validated hop free of ambient response cookies."""

    for owner in (client, getattr(client, "_curl", None)):
        cookies = getattr(owner, "cookies", None)
        clear = getattr(cookies, "clear", None)
        if callable(clear):
            clear()


async def _capture_advisory_cleanup(cleanup: Awaitable[Any]) -> BaseException | None:
    """Observe every cleanup outcome without leaving an unhandled task error."""

    try:
        await cleanup
    except BaseException as error:
        return error
    return None


async def _await_advisory_cleanup(
    cleanup: Awaitable[Any],
    *,
    pending_error: BaseException | None,
) -> None:
    """Finish advisory cleanup while preserving cancellation and exit precedence."""

    cleanup_task = asyncio.create_task(_capture_advisory_cleanup(cleanup))
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            cleanup_error = await asyncio.shield(cleanup_task)
            break
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error

    process_exit = (
        cleanup_error if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)) else None
    )
    cleanup_cancellation = (
        cleanup_error if isinstance(cleanup_error, asyncio.CancelledError) else None
    )
    preserve_pending = isinstance(
        pending_error,
        (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
    )
    del cleanup, cleanup_error, cleanup_task, pending_error
    if process_exit is not None:
        raise process_exit
    if preserve_pending:
        return
    if cancellation is not None:
        raise cancellation
    if cleanup_cancellation is not None:
        raise cleanup_cancellation


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


def _format_for_media_type(policy: TransferPolicy, media_type: str) -> FormatPolicy | None:
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


async def _bounded_text(response: Any) -> bytes | None:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_APPLICATION_REDIRECT_BYTES:
            return None
    return bytes(body)


async def _credentials_for_hop(
    credential_for: CredentialPolicy,
    url: str,
    assert_active: Callable[[], None],
) -> HopCredentials | None:
    """Acquire one hop credential without retaining it on acquisition failure."""

    credentials = None
    try:
        credentials = await credential_for(url)
        assert_active()
        return credentials
    finally:
        del assert_active, credential_for, credentials, url


def _auth_error(status: int, approved_host: str) -> AuthError:
    return AuthError(
        f"Authentication required for {approved_host} (HTTP {status}) -- try `notebooklm login`"
    )


async def guarded_transfer(
    client: Any,
    url: str,
    output_path: str,
    *,
    policy: TransferPolicy,
    credential_for: CredentialPolicy,
    validate_url: Callable[[str], str | None],
    safe_host: Callable[[str], str],
    assert_active: Callable[[], None],
    chain: bool = False,
    max_redirects: int = MAX_DOWNLOAD_REDIRECTS,
) -> TransferSuccess | TransferFailure:
    """Transfer one representation with per-hop URL, credential, and content checks."""

    current_url: str | None = url
    staging: Path | None = None
    try:
        for hop in range(max_redirects + 1):
            assert_active()
            assert current_url is not None
            host = validate_url(current_url)
            if host is None:
                return TransferFailure("url_policy", safe_host(current_url), hop)

            credentials = await _credentials_for_hop(credential_for, current_url, assert_active)

            response_cm: Any | None = None
            response: Any | None = None
            lifecycle_failure: BaseException | None = None
            try:
                _clear_client_cookies(client)
                headers = {} if credentials is None else dict(credentials.headers)
                response_cm = client.stream(
                    "GET",
                    current_url,
                    headers=headers,
                    follow_redirects=False,
                )
                response = await response_cm.__aenter__()
                status = int(response.status_code)

                if status in {301, 302, 303, 307, 308}:
                    location = _single_location(response.headers)
                    if location is None:
                        return TransferFailure("redirect", host, hop)
                    current_url = urljoin(current_url, location)
                    continue
                if status in {401, 403}:
                    public_error = _auth_error(status, host)
                    if chain and isinstance(response, httpx.Response):
                        raw_error = httpx.HTTPStatusError(
                            f"HTTP {status}", request=response.request, response=response
                        )
                        raise public_error from raw_error
                    raise public_error from None
                if status != 200:
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
                    prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
                )
                staging = Path(staging_name)
                total = 0
                prefix = bytearray()
                signatures = format_policy.signature_checks
                required_prefix = max(
                    (offset + len(signature) for signature, offset in signatures),
                    default=0,
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
                try:
                    assert_active()
                except BaseException as error:
                    # Lifecycle fences are not transport failures. Defer the
                    # raise until the response has been closed and credentials
                    # have been deleted by the hop finalizer below.
                    lifecycle_failure = error
                if lifecycle_failure is None:
                    os.replace(staging, destination)
                    staging = None
                    _fsync_directory(destination.parent)
                    return TransferSuccess(str(destination), total)
            except asyncio.CancelledError:
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except AuthError:
                raise
            except BaseException:
                return TransferFailure("transport", host, hop)
            finally:
                pending_error = sys.exc_info()[1]
                try:
                    if response_cm is not None:
                        await _await_advisory_cleanup(
                            response_cm.__aexit__(None, None, None),
                            pending_error=pending_error,
                        )
                finally:
                    _clear_client_cookies(client)
                    del credentials, pending_error, response, response_cm
            if lifecycle_failure is not None:
                raise lifecycle_failure
        return TransferFailure("too_many_hops", safe_host(current_url or ""), max_redirects)
    finally:
        if staging is not None:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
        del current_url, policy, staging, url


__all__ = [
    "FormatPolicy",
    "MAX_DOWNLOAD_REDIRECTS",
    "TransferFailure",
    "TransferPolicy",
    "TransferSuccess",
    "guarded_transfer",
]
