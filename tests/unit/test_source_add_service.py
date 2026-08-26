"""Unit tests for the private source add service."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app import source_add as cli_source_add
from notebooklm._semantic.records import (
    SourceAddCommitState,
    SourceAddDriveResult,
    SourceAddTextResult,
    SourceAddTitleState,
    SourceAddUrlReceipt,
    SourceAddUrlResult,
    SourceRecord,
)
from notebooklm._sources import SourcesAPI
from notebooklm._web.bindings.sources import _honor_requested_title
from notebooklm.exceptions import (
    NetworkError,
)
from notebooklm.types import Source


def source_response(source_id: str, title: str = "Source") -> Source:
    return Source(id="src_" + source_id, title=title)


@pytest.mark.asyncio
async def test_sources_api_add_url_uses_only_the_semantic_service() -> None:
    api = _sources_api_with_mocked_adder()
    _mock_url_service(api, SourceRecord(id="ready", title="Video"))
    api._extract_youtube_video_id = MagicMock(return_value="video")  # type: ignore[method-assign]
    api._add_youtube_source = AsyncMock(return_value=source_response("yt", "Video"))  # type: ignore[method-assign]
    api._add_url_source = AsyncMock()  # type: ignore[method-assign]
    api.wait_until_ready = AsyncMock(  # type: ignore[method-assign]
        return_value=Source(id="ready", title="Video")
    )

    result = await api.add_url("nb_1", "https://youtu.be/video", wait=True, wait_timeout=3.0)

    assert result.id == "ready"
    api._source_service.add_url.assert_awaited_once()
    assert api._source_service.add_url.await_args.kwargs["wait"] is True
    assert api._source_service.add_url.await_args.kwargs["wait_timeout"] == 3.0
    assert api._source_service.add_url.await_args.kwargs["deadline"] is None
    api.wait_until_ready.assert_awaited_once_with("nb_1", "ready", timeout=3.0)
    api._add_youtube_source.assert_not_awaited()
    api._add_url_source.assert_not_awaited()


# ---------------------------------------------------------------------------
# #1960: honor an explicit ``title`` for backend-re-derived source types
# (YouTube / Drive / web page) via a best-effort post-add rename.
# ---------------------------------------------------------------------------


def _sources_api_with_mocked_adder() -> SourcesAPI:
    api = SourcesAPI(MagicMock(), uploader=MagicMock(), _backend=MagicMock())
    api._source_service = MagicMock()  # type: ignore[assignment]
    return api


def _url_result(source: SourceRecord) -> SourceAddUrlResult:
    return SourceAddUrlResult(
        source,
        SourceAddUrlReceipt(SourceAddCommitState.CREATED, SourceAddTitleState.NOT_REQUESTED),
    )


def _mock_url_service(api: SourcesAPI, source: SourceRecord) -> None:
    api._source_service.add_url = AsyncMock(return_value=_url_result(source))


@pytest.mark.asyncio
async def test_add_url_honors_title_via_post_add_rename() -> None:
    api = _sources_api_with_mocked_adder()
    _mock_url_service(api, SourceRecord(id="src_yt", title="My Title"))

    result = await api.add_url("nb_1", "https://youtu.be/video", title="My Title")

    assert api._source_service.add_url.await_args.kwargs["requested_title"] == "My Title"
    assert result.id == "src_yt"
    assert result.title == "My Title"


@pytest.mark.asyncio
async def test_add_drive_honors_title_via_post_add_rename() -> None:
    api = _sources_api_with_mocked_adder()
    api._source_service.add_drive = AsyncMock(  # type: ignore[union-attr]
        return_value=SourceAddDriveResult(SourceRecord(id="d1", title="My Title"))
    )

    result = await api.add_drive("nb_1", "file123", "My Title")

    api._source_service.add_drive.assert_awaited_once()  # type: ignore[union-attr]
    assert api._source_service.add_drive.await_args.args[:3] == (  # type: ignore[union-attr]
        "nb_1",
        "file123",
        "My Title",
    )
    assert result.title == "My Title"


@pytest.mark.asyncio
async def test_waited_url_renames_only_after_facade_readiness() -> None:
    api = _sources_api_with_mocked_adder()
    events: list[str] = []

    async def add_url(*args: object, **kwargs: object) -> SourceAddUrlResult:
        events.append("create")
        return _url_result(SourceRecord(id="u1", title="Upstream"))

    async def wait_until_ready(*args: object, **kwargs: object) -> Source:
        events.append("wait")
        return Source(id="u1", title="Upstream")

    async def finalize(*args: object, **kwargs: object) -> SourceAddUrlResult:
        events.append("rename")
        return _url_result(SourceRecord(id="u1", title="Requested"))

    api._source_service.add_url = AsyncMock(side_effect=add_url)
    api._source_service.finalize_title = AsyncMock(side_effect=finalize)
    api.wait_until_ready = AsyncMock(side_effect=wait_until_ready)  # type: ignore[method-assign]

    result = await api.add_url("nb_1", "https://example.com", title="Requested", wait=True)

    assert events == ["create", "wait", "rename"]
    assert result.title == "Requested"


@pytest.mark.asyncio
async def test_waited_drive_renames_only_after_facade_readiness() -> None:
    api = _sources_api_with_mocked_adder()
    events: list[str] = []

    async def add_drive(*args: object, **kwargs: object) -> SourceAddDriveResult:
        events.append("create")
        return SourceAddDriveResult(SourceRecord(id="d1", title="Upstream"))

    async def wait_until_ready(*args: object, **kwargs: object) -> Source:
        events.append("wait")
        return Source(id="d1", title="Upstream")

    async def finalize(*args: object, **kwargs: object) -> SourceAddDriveResult:
        events.append("rename")
        return SourceAddDriveResult(SourceRecord(id="d1", title="Requested"))

    api._source_service.add_drive = AsyncMock(side_effect=add_drive)  # type: ignore[union-attr]
    api._source_service.finalize_drive_title = AsyncMock(side_effect=finalize)  # type: ignore[union-attr]
    api.wait_until_ready = AsyncMock(side_effect=wait_until_ready)  # type: ignore[method-assign]

    result = await api.add_drive("nb_1", "drive-id", "Requested", wait=True)

    assert events == ["create", "wait", "rename"]
    assert result.title == "Requested"


@pytest.mark.asyncio
async def test_add_url_without_title_skips_rename() -> None:
    api = _sources_api_with_mocked_adder()
    _mock_url_service(api, SourceRecord(id="s1", title="Upstream"))

    result = await api.add_url("nb_1", "https://example.com")

    assert api._source_service.add_url.await_args.kwargs["requested_title"] is None
    assert result.title == "Upstream"


@pytest.mark.asyncio
async def test_add_drive_empty_title_skips_rename() -> None:
    api = _sources_api_with_mocked_adder()
    api._source_service.add_drive = AsyncMock(  # type: ignore[union-attr]
        return_value=SourceAddDriveResult(SourceRecord(id="d1", title="Drive Name"))
    )

    result = await api.add_drive("nb_1", "file123", "")

    assert api._source_service.add_drive.await_args.args[2] == ""  # type: ignore[union-attr]
    assert result.title == "Drive Name"


@pytest.mark.asyncio
async def test_add_url_title_matching_upstream_skips_rename() -> None:
    api = _sources_api_with_mocked_adder()
    _mock_url_service(api, SourceRecord(id="s1", title="Same Title"))

    # A leading/trailing-whitespace-only difference is not a real retitle.
    result = await api.add_url("nb_1", "https://example.com", title="  Same Title  ")

    assert api._source_service.add_url.await_args.kwargs["requested_title"] == "  Same Title  "
    assert result.title == "Same Title"


@pytest.mark.asyncio
async def test_add_rename_failure_is_non_fatal(caplog: pytest.LogCaptureFixture) -> None:
    """The surviving ``SOURCE_ADD_FILE`` helper still swallows a failed rename.

    P10 R3.4 retired ``honor_requested_title_if_fresh`` with the last probed
    registration row; the freshness wrapper had no production caller left, and
    the non-fatal contract it delegated lives in ``_honor_requested_title``,
    which now sits in ``_web/bindings/sources.py`` beside the only row that
    reaches it.
    """
    source = Source(id="d1", title="Drive Name")
    rename = AsyncMock(side_effect=NetworkError("boom"))

    with caplog.at_level(logging.WARNING):
        result = await _honor_requested_title(
            rename,
            "nb_1",
            source,
            "My Title",
            logging.getLogger("tests.source_add"),
        )

    # The add succeeded — a failed rename must not raise; the upstream title is kept.
    assert result.id == "d1"
    assert result.title == "Drive Name"
    rename.assert_awaited_once_with("nb_1", "d1", "My Title")
    assert "rename" in caplog.text.lower()


@pytest.mark.asyncio
async def test_add_url_honor_preserves_metadata_over_sparse_rename_echo() -> None:
    """UPDATE_SOURCE's echo can be sparse (id + title only); the honored result must keep
    the added source's url/type and only swap in the new title, not return the bare echo
    (which would drop url → kind='unknown'). #1960."""
    api = _sources_api_with_mocked_adder()
    _mock_url_service(
        api,
        SourceRecord(
            id="s1",
            title="My Title",
            url="https://youtu.be/v",
            kind="web_page",
        ),
    )

    result = await api.add_url("nb_1", "https://youtu.be/v", title="My Title")

    assert result.title == "My Title"  # requested title applied
    assert result.url == "https://youtu.be/v"  # preserved from the added source
    assert result._type_code == 5  # preserved — not dropped by the sparse echo


@pytest.mark.asyncio
async def test_add_text_does_not_rename() -> None:
    api = _sources_api_with_mocked_adder()
    api._source_service.add_text = AsyncMock(  # type: ignore[union-attr]
        return_value=SourceAddTextResult(SourceRecord(id="t1", title="My Notes"))
    )
    api.rename = AsyncMock()  # type: ignore[method-assign]

    result = await api.add_text("nb_1", "My Notes", "content")

    # ``text`` sources honor ``title`` on the wire — no post-add rename.
    api.rename.assert_not_awaited()
    api._source_service.add_text.assert_awaited_once()  # type: ignore[union-attr]
    assert result.title == "My Notes"


# ---------------------------------------------------------------------------
# CLI service layer: SSRF guard on `source add --url`
#
# These tests target ``notebooklm._app.source_add.validate_url`` and
# the routing inside ``build_source_add_plan``. They replace the previous
# ``startswith(("http://", "https://"))`` prefix check, which let
# ``file:///etc/passwd`` and ``http://169.254.169.254/`` through.
# ---------------------------------------------------------------------------


class TestValidateUrlScheme:
    """Scheme allowlist: only http/https accepted, even with --allow-internal."""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/foo",
            "gopher://example.com/",
            "data:text/plain,hello",
            "javascript:alert(1)",
        ],
    )
    def test_disallowed_schemes_are_rejected_strict(self, url: str) -> None:
        with pytest.raises(cli_source_add.SourceAddValidationError) as exc_info:
            cli_source_add.validate_url(url, allow_internal=False)

        msg = str(exc_info.value)
        assert "scheme" in msg.lower()
        assert "http and https" in msg.lower()

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/foo",
        ],
    )
    def test_disallowed_schemes_still_rejected_with_allow_internal(self, url: str) -> None:
        """``--allow-internal`` is for INTERNAL HOSTS, not for unsafe schemes.

        ``file://`` would let the CLI read arbitrary local files; ``ftp://``
        could probe internal services. Neither should be unlocked by the
        internal-host opt-in.
        """
        with pytest.raises(cli_source_add.SourceAddValidationError) as exc_info:
            cli_source_add.validate_url(url, allow_internal=True)

        assert "scheme" in str(exc_info.value).lower()

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",
            "https://example.com",
            "https://example.com/path?q=1",
            "https://sub.example.co.uk:8443/page",
            "HTTPS://Example.Com/",  # mixed case scheme — urlsplit lowercases via .scheme
        ],
    )
    def test_public_http_https_urls_pass(self, url: str) -> None:
        # No raise — the call returns None on success.
        cli_source_add.validate_url(url, allow_internal=False)

    def test_empty_url_is_rejected(self) -> None:
        with pytest.raises(cli_source_add.SourceAddValidationError):
            cli_source_add.validate_url("", allow_internal=False)

    def test_url_without_host_is_rejected(self) -> None:
        with pytest.raises(cli_source_add.SourceAddValidationError) as exc_info:
            cli_source_add.validate_url("http:///path", allow_internal=False)

        assert "no host" in str(exc_info.value).lower()


class TestValidateUrlInternalHost:
    """Host policy: reject private/loopback/link-local IPs + localhost names."""

    @pytest.mark.parametrize(
        "url",
        [
            # Loopback IPv4
            "http://127.0.0.1",
            "http://127.0.0.1:8080/foo",
            # Private RFC1918 ranges
            "http://10.0.0.1",
            "http://172.16.0.1",
            "http://192.168.1.1",
            # Link-local (the classic SSRF target — cloud metadata IP)
            "http://169.254.169.254/latest/meta-data/",
            # Unspecified bind-all addresses
            "http://0.0.0.0:8080/",
            "http://[::]/",
            # IPv6 loopback (urlsplit strips brackets via .hostname)
            "http://[::1]/",
            # IPv4-mapped IPv6 must classify by the mapped IPv4 address.
            "http://[::ffff:127.0.0.1]/",
            "http://[::ffff:10.0.0.1]/",
            # Alternate local spellings accepted by URL/network stacks.
            "http://localhost.",
            "http://LOCALHOST./",
            "http://app.localhost/",
            "http://localhost.localdomain/",
            "http://app.localhost.localdomain/",
            "http://127.1",
            "http://2130706433",
            "http://127.0.0.1.",
            # DNS literal "localhost"
            "http://localhost",
            "https://localhost:3000/",
            "http://LOCALHOST/",  # case-insensitive match
        ],
    )
    def test_internal_hosts_rejected_strict(self, url: str) -> None:
        with pytest.raises(cli_source_add.SourceAddValidationError) as exc_info:
            cli_source_add.validate_url(url, allow_internal=False)

        msg = str(exc_info.value).lower()
        assert "internal" in msg or "local" in msg
        assert "--allow-internal" in str(exc_info.value)

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8080/api",
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:3000/health",
            "http://localhost.",
            "http://app.localhost/",
            "http://localhost.localdomain/",
            "http://app.localhost.localdomain/",
            "http://127.1",
            "http://2130706433",
            "http://127.0.0.1.",
            "http://[::ffff:127.0.0.1]/",
            "http://0.0.0.0:8080/",
            "http://[::1]/",
        ],
    )
    def test_internal_hosts_pass_with_allow_internal(self, url: str) -> None:
        """``--allow-internal`` opts into private/loopback/link-local hosts."""
        cli_source_add.validate_url(url, allow_internal=True)

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",
            "https://google.com",
            "https://api.notebooklm.google.com/foo",
            "http://1.1.1.1",  # public IP — must pass
            "http://8.8.8.8/dns-query",  # public IP
        ],
    )
    def test_public_dns_and_public_ips_pass_strict(self, url: str) -> None:
        cli_source_add.validate_url(url, allow_internal=False)

    def test_dns_validation_does_not_resolve(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Guard against accidentally introducing DNS resolution.

        The validator must reject ``localhost`` by literal match, NOT by
        resolving it (resolving would be flaky in CI and would leak the
        caller's interest in the URL).
        """
        import socket

        def _explode(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("validate_url must not resolve DNS at validation time")

        monkeypatch.setattr(socket, "gethostbyname", _explode)
        monkeypatch.setattr(socket, "getaddrinfo", _explode)

        # Public DNS name — must NOT resolve.
        cli_source_add.validate_url("https://example.com/", allow_internal=False)
        # ``localhost`` rejection — must come from the literal match.
        with pytest.raises(cli_source_add.SourceAddValidationError):
            cli_source_add.validate_url("http://localhost/", allow_internal=False)


class TestBuildSourceAddPlanUrlRouting:
    """``build_source_add_plan`` routes URL-shaped content through validate_url."""

    def _make_validate_path(self) -> Callable[[str, bool], Path]:
        return MagicMock(return_value=Path("/tmp/x"))

    def _make_looks_path(self) -> Callable[[str], bool]:
        return MagicMock(return_value=False)

    def test_public_http_url_is_detected_as_url(self) -> None:
        plan = cli_source_add.build_source_add_plan(
            content="https://example.com/article",
            source_type=None,
            title=None,
            mime_type=None,
            follow_symlinks=False,
            validate_path=self._make_validate_path(),
            looks_path_shaped=self._make_looks_path(),
        )
        assert plan.detected_type == "url"

    def test_internal_url_is_rejected_during_auto_detect(self) -> None:
        with pytest.raises(cli_source_add.SourceAddValidationError):
            cli_source_add.build_source_add_plan(
                content="http://127.0.0.1:8080/admin",
                source_type=None,
                title=None,
                mime_type=None,
                follow_symlinks=False,
                validate_path=self._make_validate_path(),
                looks_path_shaped=self._make_looks_path(),
            )

    def test_internal_url_accepted_with_allow_internal(self) -> None:
        plan = cli_source_add.build_source_add_plan(
            content="http://127.0.0.1:8080/admin",
            source_type=None,
            title=None,
            mime_type=None,
            follow_symlinks=False,
            validate_path=self._make_validate_path(),
            looks_path_shaped=self._make_looks_path(),
            allow_internal=True,
        )
        assert plan.detected_type == "url"

    def test_explicit_internal_url_accepted_with_allow_internal(self) -> None:
        plan = cli_source_add.build_source_add_plan(
            content="http://127.0.0.1:8080/admin",
            source_type="url",
            title=None,
            mime_type=None,
            follow_symlinks=False,
            validate_path=self._make_validate_path(),
            looks_path_shaped=self._make_looks_path(),
            allow_internal=True,
        )
        assert plan.detected_type == "url"

    def test_file_scheme_is_rejected_even_with_allow_internal(self) -> None:
        """``--allow-internal`` must NOT unlock ``file://``."""
        with pytest.raises(cli_source_add.SourceAddValidationError):
            cli_source_add.build_source_add_plan(
                content="file:///etc/passwd",
                source_type=None,
                title=None,
                mime_type=None,
                follow_symlinks=False,
                validate_path=self._make_validate_path(),
                looks_path_shaped=self._make_looks_path(),
                allow_internal=True,
            )

    def test_explicit_type_url_still_validates(self) -> None:
        """``--type url file:///etc/passwd`` must NOT bypass the gate.

        Pre-fix, the prefix check only ran in the auto-detect branch — an
        explicit ``--type url`` skipped validation entirely. The new gate
        runs in both branches.
        """
        with pytest.raises(cli_source_add.SourceAddValidationError):
            cli_source_add.build_source_add_plan(
                content="file:///etc/passwd",
                source_type="url",
                title=None,
                mime_type=None,
                follow_symlinks=False,
                validate_path=self._make_validate_path(),
                looks_path_shaped=self._make_looks_path(),
            )

    def test_explicit_type_youtube_still_validates(self) -> None:
        with pytest.raises(cli_source_add.SourceAddValidationError):
            cli_source_add.build_source_add_plan(
                content="http://169.254.169.254/latest/meta-data/",
                source_type="youtube",
                title=None,
                mime_type=None,
                follow_symlinks=False,
                validate_path=self._make_validate_path(),
                looks_path_shaped=self._make_looks_path(),
            )

    def test_non_url_content_falls_through_to_text(self) -> None:
        """Bare strings (no ``://``) must NOT be parsed as URLs."""
        plan = cli_source_add.build_source_add_plan(
            content="hello world",
            source_type=None,
            title=None,
            mime_type=None,
            follow_symlinks=False,
            validate_path=self._make_validate_path(),
            looks_path_shaped=self._make_looks_path(),
        )
        assert plan.detected_type == "text"

    def test_youtube_url_still_routes_to_youtube_type(self) -> None:
        plan = cli_source_add.build_source_add_plan(
            content="https://www.youtube.com/watch?v=abc123",
            source_type=None,
            title=None,
            mime_type=None,
            follow_symlinks=False,
            validate_path=self._make_validate_path(),
            looks_path_shaped=self._make_looks_path(),
        )
        assert plan.detected_type == "youtube"
