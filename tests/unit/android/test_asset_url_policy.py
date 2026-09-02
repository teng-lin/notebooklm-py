"""Host and URL admission policy for Android asset downloads.

Every one of these helpers sits in front of an outbound request carrying a
bearer token, so a permissive branch here is an SSRF/credential-leak shape, not
a cosmetic gap. The adapter suites exercise the happy path; these pin the
rejections exhaustively.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from notebooklm._android.assets import (
    _MAX_APPLICATION_REDIRECT_BYTES,
    _append_initial_alr,
    _bearer_for,
    _bounded_text,
    _close_clients_and_settle_tasks,
    _declared_content_length,
    _default_client_factory,
    _fsync_directory,
    _is_android_download_host,
    _safe_approved_host,
    _single_location,
    _validated_host,
)
from notebooklm._android.auth import BearerCredential

BEARER = "ya29.asset-secret"
APPROVED = "lh3.googleusercontent.com"


# ---------------------------------------------------------------------------
# _is_android_download_host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        pytest.param(APPROVED, id="primary-bearer-host"),
        pytest.param("contribution.usercontent.google.com", id="contribution-host"),
        pytest.param("r1---sn-abc.googlevideo.com", id="googlevideo-subdomain"),
        pytest.param("usercontent.google.com", id="usercontent-subdomain-of-google"),
    ],
)
def test_approved_download_hosts_are_admitted(host: str) -> None:
    assert _is_android_download_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("", id="empty"),
        pytest.param("evil.com", id="unrelated"),
        pytest.param("googlevideo.com", id="bare-suffix-is-not-a-subdomain"),
        pytest.param("notgooglevideo.com", id="suffix-without-the-dot"),
        pytest.param("evil.com.googlevideo.com.attacker.net", id="suffix-in-the-middle"),
        pytest.param("x" * 254, id="over-253-characters"),
        pytest.param("lh3.googleusercontent.com:443", id="port-in-the-host"),
        pytest.param("LH3.GOOGLEUSERCONTENT.COM", id="uppercase-is-not-normalised-here"),
        pytest.param("evil%2egooglevideo.com", id="percent-encoded-dot"),
        pytest.param("evil\\.googlevideo.com", id="backslash"),
        pytest.param("a..googlevideo.com", id="empty-label"),
        pytest.param("-a.googlevideo.com", id="label-starts-with-hyphen"),
        pytest.param("a-.googlevideo.com", id="label-ends-with-hyphen"),
        pytest.param(("x" * 64) + ".googlevideo.com", id="label-over-63-characters"),
    ],
)
def test_unapproved_download_hosts_are_rejected(host: str) -> None:
    assert _is_android_download_host(host) is False


# ---------------------------------------------------------------------------
# _safe_approved_host
# ---------------------------------------------------------------------------


def test_a_safe_host_is_reported_for_an_approved_url() -> None:
    assert _safe_approved_host(f"https://{APPROVED}/asset") == APPROVED


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://evil.com/asset", id="unapproved-host"),
        pytest.param("https://[oops/asset", id="unparseable"),
        pytest.param("not a url", id="not-a-url"),
    ],
)
def test_an_unapproved_or_unparseable_url_reports_a_placeholder(url: str) -> None:
    """The rejected URL is never echoed back into a log line."""
    assert _safe_approved_host(url) == "<rejected>"


# ---------------------------------------------------------------------------
# _validated_host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(f"https://{APPROVED}/asset", id="plain"),
        pytest.param(f"https://{APPROVED}:443/asset", id="explicit-default-port"),
        pytest.param(f"https://{APPROVED}/asset?alr=yes", id="with-query"),
    ],
)
def test_a_well_formed_approved_url_yields_its_host(url: str) -> None:
    assert _validated_host(url) == APPROVED


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(f"http://{APPROVED}/asset", id="not-https"),
        pytest.param(f"https://{APPROVED}:8443/asset", id="non-default-port"),
        pytest.param(f"https://user:pass@{APPROVED}/asset", id="embedded-credentials"),
        pytest.param(f"https://user@{APPROVED}/asset", id="embedded-username"),
        pytest.param(f"https://{APPROVED}/asset#fragment", id="fragment"),
        pytest.param("https://evil.com/asset", id="unapproved-host"),
        pytest.param("https:///asset", id="no-host"),
        pytest.param(f"https://{APPROVED}/a\x01b", id="control-character"),
        pytest.param(f"https://{APPROVED}/a\x7fb", id="delete-character"),
        pytest.param("https://[oops/asset", id="unparseable"),
        pytest.param(f"https://{APPROVED}:notaport/asset", id="invalid-port"),
    ],
)
def test_a_url_failing_any_admission_rule_is_rejected(url: str) -> None:
    assert _validated_host(url) is None


# ---------------------------------------------------------------------------
# _append_initial_alr
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param("https://h/a", "https://h/a?alr=yes", id="no-query"),
        pytest.param("https://h/a?x=1", "https://h/a?x=1&alr=yes", id="existing-query"),
        pytest.param("https://h/a?alr=yes", "https://h/a?alr=yes", id="already-set"),
    ],
)
def test_the_alr_parameter_is_added_once(url: str, expected: str) -> None:
    assert _append_initial_alr(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://h/a?alr=no", id="explicitly-disabled"),
        pytest.param("https://h/a?alr=", id="blank-value"),
        pytest.param("https://h/a?alr=yes&alr=yes", id="repeated-parameter"),
        pytest.param("https://h/a?alr=yes&alr=no", id="conflicting-values"),
    ],
)
def test_a_url_declaring_its_own_alr_is_not_rewritten(url: str) -> None:
    """Silently overriding the caller's redirect mode would change the hop."""
    assert _append_initial_alr(url) is None


def test_an_unparseable_url_yields_no_alr_variant() -> None:
    assert _append_initial_alr("https://[oops/a") is None


# ---------------------------------------------------------------------------
# _bearer_for
# ---------------------------------------------------------------------------


def test_the_bearer_is_attached_only_for_hosts_that_require_it() -> None:
    credential = BearerCredential(token=BEARER, generation=1)

    assert _bearer_for(APPROVED, credential) == {"Authorization": f"Bearer {BEARER}"}
    assert _bearer_for("contribution.usercontent.google.com", credential) == {
        "Authorization": f"Bearer {BEARER}"
    }


@pytest.mark.parametrize(
    ("host", "credential"),
    [
        pytest.param("r1.googlevideo.com", BearerCredential(BEARER, 1), id="media-cdn-host"),
        pytest.param("evil.com", BearerCredential(BEARER, 1), id="unapproved-host"),
        pytest.param(APPROVED, None, id="no-credential"),
    ],
)
def test_the_bearer_is_withheld_from_every_other_hop(
    host: str, credential: BearerCredential | None
) -> None:
    assert _bearer_for(host, credential) == {}


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


class _Headers:
    def __init__(self, values: dict[str, list[str]] | None = None) -> None:
        self._values = values or {}

    def get_list(self, name: str) -> list[str]:
        return self._values.get(name, [])

    def get(self, name: str, default: object = None) -> object:
        values = self._values.get(name)
        return values[0] if values else default


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        pytest.param({"location": ["https://h/next"]}, "https://h/next", id="single"),
        pytest.param({}, None, id="absent"),
        pytest.param({"location": ["https://h/a", "https://h/b"]}, None, id="ambiguous"),
    ],
)
def test_only_an_unambiguous_redirect_location_is_followed(
    values: dict[str, list[str]], expected: str | None
) -> None:
    assert _single_location(_Headers(values)) == expected


def test_headers_without_a_get_list_accessor_fall_back_to_a_plain_get() -> None:
    """Not every transport's header mapping exposes ``get_list``."""

    class _PlainHeaders:
        def __init__(self, value: str | None) -> None:
            self._value = value

        def get(self, name: str, default: object = None) -> object:
            return self._value if name == "location" else default

    assert _single_location(_PlainHeaders("https://h/next")) == "https://h/next"
    assert _single_location(_PlainHeaders(None)) is None


def test_an_empty_location_header_is_not_a_redirect() -> None:
    assert _single_location(_Headers({"location": [""]})) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("1024", 1024, id="numeric"),
        pytest.param("0", 0, id="zero"),
        pytest.param(None, None, id="absent"),
        # ``-1`` is the module's "declared but unusable" sentinel, distinct from
        # ``None`` ("not declared") — collapsing the two would let a malformed
        # header read as an absent one.
        pytest.param("not-a-number", -1, id="non-numeric"),
        pytest.param("-1", -1, id="negative"),
        pytest.param("12 34", -1, id="malformed"),
    ],
)
def test_a_declared_content_length_is_admitted_only_when_usable(
    raw: str | None, expected: int | None
) -> None:
    values = {} if raw is None else {"content-length": [raw]}

    assert _declared_content_length(_Headers(values)) == expected


class _Response:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_a_small_redirect_body_is_read_whole() -> None:
    assert await _bounded_text(_Response([b"ab", b"cd"])) == b"abcd"


@pytest.mark.asyncio
async def test_an_oversized_redirect_body_is_abandoned() -> None:
    """A hop that streams a large body is not a redirect page — stop reading."""
    oversized = b"x" * (_MAX_APPLICATION_REDIRECT_BYTES + 1)

    assert await _bounded_text(_Response([oversized])) is None


# ---------------------------------------------------------------------------
# _fsync_directory
# ---------------------------------------------------------------------------


def test_fsync_directory_makes_a_real_directory_durable(tmp_path: Path) -> None:
    _fsync_directory(tmp_path)


def test_fsync_directory_ignores_a_path_it_cannot_open(tmp_path: Path) -> None:
    """A platform without directory fsync must not fail the download."""
    _fsync_directory(tmp_path / "does-not-exist")


def test_fsync_directory_releases_the_descriptor_when_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem that refuses directory fsync must not leak the descriptor.

    ``os.open`` is stubbed to hand back a real descriptor because Windows
    cannot open a directory as a file at all — there the production code takes
    its early-return branch, and this release path would never run.
    """
    probe = tmp_path / "probe"
    probe.write_bytes(b"")
    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def _open_a_real_file(_path: object, _flags: int) -> int:
        descriptor = real_open(probe, os.O_RDONLY)
        opened.append(descriptor)
        return descriptor

    def _failing_fsync(_fd: int) -> None:
        raise OSError("fsync unsupported on this filesystem")

    def _tracking_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "open", _open_a_real_file)
    monkeypatch.setattr(os, "fsync", _failing_fsync)
    monkeypatch.setattr(os, "close", _tracking_close)

    _fsync_directory(tmp_path)

    assert closed == opened != []


# ---------------------------------------------------------------------------
# _close_clients_and_settle_tasks
# ---------------------------------------------------------------------------


class _Client:
    """Transport client whose ``aclose`` can be scripted to fail."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1
        if self.error is not None:
            raise self.error


async def _settled(result: object = None, error: BaseException | None = None):
    if error is not None:
        raise error
    return result


@pytest.mark.asyncio
async def test_closing_settles_every_client_and_task() -> None:
    clients = (_Client(), _Client())
    tasks = tuple(asyncio.ensure_future(_settled()) for _ in range(2))

    outcome = await _close_clients_and_settle_tasks(clients, tasks)

    assert [client.closed for client in clients] == [1, 1]
    assert outcome.process_exit is None
    assert outcome.close_failed is False


@pytest.mark.asyncio
async def test_a_failing_client_close_is_reported_without_stopping_the_sweep() -> None:
    """Every remaining client must still be closed."""
    failing = _Client(OSError("socket already gone"))
    healthy = _Client()

    outcome = await _close_clients_and_settle_tasks((failing, healthy), ())

    assert healthy.closed == 1
    assert outcome.close_failed is True
    assert outcome.process_exit is None


@pytest.mark.asyncio
async def test_a_cancelled_transfer_task_is_absorbed() -> None:
    async def _never() -> None:
        await asyncio.Event().wait()

    task = asyncio.ensure_future(_never())
    task.cancel()

    outcome = await _close_clients_and_settle_tasks((), (task,))

    assert outcome.close_failed is False
    assert outcome.process_exit is None


@pytest.mark.asyncio
async def test_a_failed_transfer_task_belongs_to_its_own_caller() -> None:
    """Its failure was already published; the close sweep must not re-raise it."""
    task = asyncio.ensure_future(_settled(error=OSError("transfer failed")))
    await asyncio.gather(task, return_exceptions=True)

    outcome = await _close_clients_and_settle_tasks((), (task,))

    assert outcome.close_failed is False
    assert outcome.process_exit is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit()], ids=["keyboard-interrupt", "system-exit"]
)
async def test_an_interpreter_exit_while_closing_a_client_is_retained(
    error: BaseException,
) -> None:
    """It is reported back rather than swallowed as an ordinary close failure."""
    later = _Client()

    outcome = await _close_clients_and_settle_tasks((_Client(error), later), ())

    assert type(outcome.process_exit) is type(error)
    assert later.closed == 1, "the sweep still finishes"


@pytest.mark.asyncio
async def test_only_the_first_interpreter_exit_is_retained() -> None:
    first = KeyboardInterrupt()
    second = SystemExit()

    outcome = await _close_clients_and_settle_tasks((_Client(first), _Client(second)), ())

    assert outcome.process_exit is first


# NOTE: the sibling ``except (KeyboardInterrupt, SystemExit)`` in the task loop
# has no test. CPython's ``Task.__step`` re-raises those two into the event loop
# as soon as the coroutine raises them, so an awaiter never observes them from a
# real task and the branch cannot be exercised without faking ``Task`` itself.


def test_the_default_client_factory_builds_a_non_redirecting_transport() -> None:
    """Redirects are followed manually so every hop is host-checked."""
    client = _default_client_factory()

    try:
        assert client.follow_redirects is False
    finally:
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()
