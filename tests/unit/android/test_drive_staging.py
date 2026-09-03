"""Drive staging transfer: status mapping, malformed metadata, and cleanup fencing.

``tests/unit/android/test_source_upload.py`` drives this collaborator through
the whole ``add_file`` graph, which can only reach the responses a healthy
Drive returns. These tests build :class:`DriveStagingTransfer` directly so the
refusal and malformed-response arms -- each one a place where a wrong answer
would either strand a credential or publish a raw Drive payload -- can be
pinned individually.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from notebooklm._android.auth import BearerCredential
from notebooklm._android.drive_staging import (
    DRIVE_API_ORIGIN,
    DriveStagingTransfer,
    map_staging_status,
)
from notebooklm.exceptions import (
    AuthError,
    RateLimitError,
    ServerError,
    ValidationError,
)

BEARER = "ya29.drive-staging-secret"
GENERATION = 31
FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz"


class _Lease:
    def __init__(self, epoch: int) -> None:
        self.epoch = epoch


class _Transport:
    """Records the operation labels staging opens, and hands out one lease."""

    def __init__(self, *, epoch: int = 1) -> None:
        self.epoch = epoch
        self.labels: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str) -> AsyncIterator[_Lease]:
        self.labels.append(label)
        yield _Lease(self.epoch)


class _Bearer:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[int] = []
        self.invalidations: list[int] = []

    async def get(self, expected_epoch: int) -> BearerCredential:
        self.calls.append(expected_epoch)
        if self.error is not None:
            raise self.error
        return BearerCredential(BEARER, generation=GENERATION)

    def invalidate(self, generation: int) -> None:
        self.invalidations.append(generation)


class _Response:
    """A Drive reply whose ``json()`` can be scripted to fail or return junk."""

    def __init__(
        self,
        status_code: int,
        *,
        payload: Any = None,
        json_error: BaseException | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self) -> Any:
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.deletes: list[tuple[str, dict[str, str]]] = []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> _Client:
        self.entered += 1
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.exited += 1

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes,
        follow_redirects: bool,
    ) -> _Response:
        self.posts.append((url, dict(headers)))
        return self.response

    async def delete(
        self,
        url: str,
        *,
        headers: dict[str, str],
        follow_redirects: bool,
    ) -> _Response:
        self.deletes.append((url, dict(headers)))
        return self.response


async def _bounded(awaitable: Awaitable[Any], _deadline: Any) -> Any:
    return await awaitable


def _transfer(
    response: _Response,
    *,
    bearer: _Bearer | None = None,
) -> tuple[DriveStagingTransfer, _Bearer, _Client, list[Any]]:
    """Build the collaborator over recording fakes, as the pipeline builds it."""

    provider = bearer if bearer is not None else _Bearer()
    client = _Client(response)
    tracked: list[Any] = []

    @asynccontextmanager
    async def _slot() -> AsyncIterator[None]:
        yield

    transfer = DriveStagingTransfer(
        transport=_Transport(),
        bearer_provider=provider,
        client_factory=lambda: lambda **_kwargs: client,
        upload_slot=_slot,
        assert_epoch=lambda _epoch: None,
        track_client=tracked.append,
        untrack_client=lambda item: tracked.remove(item) if item in tracked else None,
        upload_timeout=30.0,
        http_timeout=None,
        monotonic=time.monotonic,
        bounded=_bounded,
    )
    return transfer, provider, client, tracked


# ---------------------------------------------------------------------------
# map_staging_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected", "fragment"),
    [
        pytest.param(401, AuthError, "reauthenticate", id="401-expired-credential"),
        pytest.param(429, RateLimitError, "retry after a delay", id="429-throttled"),
        pytest.param(403, ValidationError, "needs Drive access", id="403-refused"),
        pytest.param(404, ValidationError, "HTTP 404", id="404-generic-4xx"),
        pytest.param(302, ValidationError, "HTTP 302", id="302-unfollowed-redirect"),
        pytest.param(500, ServerError, "retry later", id="500-server"),
        pytest.param(503, ServerError, "retry later", id="503-unavailable"),
    ],
)
def test_a_refusing_drive_status_maps_to_its_public_exception(
    status: int,
    expected: type[Exception],
    fragment: str,
) -> None:
    """Callers retry by public exception type, so each arm has to be exact.

    Collapsing 429 or 5xx into ``ValidationError`` would turn a retriable
    condition into a permanent one.
    """
    with pytest.raises(expected) as raised:
        map_staging_status(status, "report.docx")

    assert fragment in str(raised.value)
    assert "report.docx" in str(raised.value) or status == 401


def test_a_server_status_carries_the_code_for_the_caller_to_act_on() -> None:
    """``ServerError.status_code`` is what distinguishes a 503 from a 500."""
    with pytest.raises(ServerError) as raised:
        map_staging_status(503, "report.docx")

    assert raised.value.status_code == 503


@pytest.mark.parametrize("status", [200, 201, 204, 299], ids=lambda code: f"http-{code}")
def test_an_accepted_drive_status_is_passed_through_untouched(status: int) -> None:
    """Anything below 300 is a successful upload; raising here would break staging."""
    assert map_staging_status(status, "report.docx") is None


# ---------------------------------------------------------------------------
# DriveStagingTransfer.stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rejected_staging_credential_is_invalidated_before_the_error_escapes(
    tmp_path: Path,
) -> None:
    """A 401 means the cached bearer is dead.

    Without the invalidation the next staging attempt reuses the same rejected
    token and fails identically, so the caller can never recover by retrying.
    """
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")
    transfer, bearer, client, tracked = _transfer(_Response(401))

    with pytest.raises(AuthError):
        await transfer.stage(path, "report.docx", "application/vnd.ms-word")

    assert bearer.invalidations == [GENERATION]
    assert len(client.posts) == 1
    assert tracked == [], "the client is untracked even on the failure path"


@pytest.mark.asyncio
async def test_a_non_401_refusal_keeps_the_cached_credential(tmp_path: Path) -> None:
    """403 is a quota/scope problem, not a bad token; discarding it is wasteful."""
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")
    transfer, bearer, _, _ = _transfer(_Response(403))

    with pytest.raises(ValidationError):
        await transfer.stage(path, "report.docx", "application/vnd.ms-word")

    assert bearer.invalidations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "json_error",
    [
        pytest.param(ValueError("Expecting value: line 1 column 1"), id="not-json"),
        pytest.param(TypeError("the body is not decodable"), id="undecodable-body"),
    ],
)
async def test_unparseable_drive_metadata_is_reported_without_the_raw_body(
    tmp_path: Path,
    json_error: BaseException,
) -> None:
    """A 200 with an HTML error page must not surface as a bare ``ValueError``.

    ``from None`` also matters: the raw decode error can quote the response
    body, which is Drive-controlled content.
    """
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")
    transfer, _, _, _ = _transfer(_Response(200, json_error=json_error))

    with pytest.raises(ValidationError, match="malformed metadata while staging report.docx"):
        await transfer.stage(path, "report.docx", "application/vnd.ms-word")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="no-id-key"),
        pytest.param({"id": ""}, id="empty-id"),
        pytest.param({"id": None}, id="null-id"),
        pytest.param({"id": 12345}, id="non-string-id"),
        pytest.param(["not", "a", "mapping"], id="list-body"),
        pytest.param("a bare string", id="string-body"),
    ],
)
async def test_a_staging_reply_without_a_usable_file_id_is_refused(
    tmp_path: Path,
    payload: Any,
) -> None:
    """The id is handed straight to the Drive import and to ``unstage``.

    Letting a non-string or empty id through would import nothing and then
    issue a DELETE against a nonsense path.
    """
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")
    transfer, _, _, _ = _transfer(_Response(200, payload=payload))

    with pytest.raises(ValidationError, match="did not return a file id while staging"):
        await transfer.stage(path, "report.docx", "application/vnd.ms-word")


@pytest.mark.asyncio
async def test_a_well_formed_staging_reply_yields_the_new_file_id(tmp_path: Path) -> None:
    """The success arm, so the refusal tests above cannot pass by never staging."""
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 docx payload")
    transfer, bearer, client, tracked = _transfer(_Response(200, payload={"id": FILE_ID}))

    assert await transfer.stage(path, "report.docx", "application/vnd.ms-word") == FILE_ID

    assert bearer.invalidations == []
    assert client.posts[0][1]["Authorization"] == f"Bearer {BEARER}"
    assert tracked == []


# ---------------------------------------------------------------------------
# DriveStagingTransfer.unstage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cleanup_that_fails_before_a_client_exists_is_warned_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bearer can expire between the import and the cleanup DELETE.

    ``unstage`` runs in the caller's success path, so a failure here must never
    replace a completed add -- and the ``finally`` must tolerate the client
    never having been built.
    """
    transfer, bearer, client, tracked = _transfer(
        _Response(204),
        bearer=_Bearer(error=AuthError("android bearer expired")),
    )

    with caplog.at_level("WARNING", logger="notebooklm._android.drive_staging"):
        assert await transfer.unstage(FILE_ID) is None

    assert bearer.calls == [1]
    assert client.deletes == [], "no DELETE was ever dispatched"
    assert tracked == []
    assert FILE_ID in caplog.text
    assert "AuthError" in caplog.text


@pytest.mark.asyncio
async def test_a_successful_cleanup_deletes_the_exact_staged_id() -> None:
    """The id is URL-quoted into the path; a wrong target silently leaks the file."""
    transfer, _, client, tracked = _transfer(_Response(204))

    await transfer.unstage(FILE_ID)

    assert client.deletes[0][0] == (
        f"{DRIVE_API_ORIGIN}/drive/v3/files/{FILE_ID}?supportsAllDrives=true"
    )
    assert client.deletes[0][1]["Authorization"] == f"Bearer {BEARER}"
    assert tracked == []


@pytest.mark.asyncio
async def test_an_already_deleted_staged_file_is_not_reported_as_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """404 is the desired end state, so it must not produce a warning to chase."""
    transfer, _, _, _ = _transfer(_Response(404))

    with caplog.at_level("WARNING", logger="notebooklm._android.drive_staging"):
        await transfer.unstage(FILE_ID)

    assert caplog.text == ""
