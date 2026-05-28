"""Unit tests for the private source add service."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._source_add import SourceAddService
from notebooklm._sources import SourcesAPI
from notebooklm.cli.services import source_add as cli_source_add
from notebooklm.exceptions import NetworkError, NonIdempotentRetryError, SourceAddError
from notebooklm.rpc import RPCError, RPCMethod
from notebooklm.types import Source


class RecordingRpc:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "method": method,
                "params": params,
                "source_path": source_path,
                "allow_null": allow_null,
                "disable_internal_retries": disable_internal_retries,
                "operation_variant": operation_variant,
            }
        )
        return self.response


@pytest.fixture
def service() -> SourceAddService:
    return SourceAddService()


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("tests.source_add")


def source_response(source_id: str, title: str = "Source") -> list[Any]:
    return [[[["src_" + source_id], title, [None, 0], [None, 2]]]]


@pytest.mark.asyncio
async def test_add_url_routes_youtube_through_late_bound_hook(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    add_youtube_source = AsyncMock(return_value=source_response("yt", "Video"))
    add_url_source = AsyncMock()

    source = await service.add_url(
        "nb_1",
        "https://youtu.be/video",
        add_youtube_source=add_youtube_source,
        add_url_source=add_url_source,
        list_sources=AsyncMock(return_value=[]),
        wait_until_ready=AsyncMock(),
        extract_youtube_video_id=MagicMock(return_value="video"),
        is_youtube_url=MagicMock(return_value=True),
        logger=logger,
    )

    assert source.id == "src_yt"
    add_youtube_source.assert_awaited_once_with("nb_1", "https://youtu.be/video")
    add_url_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_url_probe_returns_existing_after_transport_error(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    existing = Source(id="src_existing", url="https://example.com")
    add_url_source = AsyncMock(side_effect=NetworkError("temporary network failure"))

    source = await service.add_url(
        "nb_1",
        existing.url,
        add_youtube_source=AsyncMock(),
        add_url_source=add_url_source,
        list_sources=AsyncMock(return_value=[existing]),
        wait_until_ready=AsyncMock(),
        extract_youtube_video_id=MagicMock(return_value=None),
        is_youtube_url=MagicMock(return_value=False),
        logger=logger,
    )

    assert source is existing
    add_url_source.assert_awaited_once_with("nb_1", existing.url)


@pytest.mark.asyncio
async def test_add_url_wraps_generic_rpc_error(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    rpc_error = RPCError("bad result")

    with pytest.raises(SourceAddError) as exc_info:
        await service.add_url(
            "nb_1",
            "https://example.com",
            add_youtube_source=AsyncMock(),
            add_url_source=AsyncMock(side_effect=rpc_error),
            list_sources=AsyncMock(return_value=[]),
            wait_until_ready=AsyncMock(),
            extract_youtube_video_id=MagicMock(return_value=None),
            is_youtube_url=MagicMock(return_value=False),
            logger=logger,
        )

    assert exc_info.value.url == "https://example.com"
    assert exc_info.value.cause is rpc_error


@pytest.mark.asyncio
async def test_add_text_uses_exact_rpc_shape_and_wait_hook(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    rpc = RecordingRpc(source_response("text", "Title"))
    ready = Source(id="src_text", title="Title")
    wait_until_ready = AsyncMock(return_value=ready)

    result = await service.add_text(
        "nb_1",
        "Title",
        "content",
        wait=True,
        wait_timeout=9.0,
        rpc=rpc,
        wait_until_ready=wait_until_ready,
        logger=logger,
    )

    assert result is ready
    assert rpc.calls == [
        {
            "method": RPCMethod.ADD_SOURCE,
            "params": [
                [[None, ["Title", "content"], None, None, None, None, None, None]],
                "nb_1",
                [2],
                None,
                None,
            ],
            "source_path": "/notebook/nb_1",
            "allow_null": False,
            "disable_internal_retries": False,
            "operation_variant": "text",
        }
    ]
    wait_until_ready.assert_awaited_once_with("nb_1", "src_text", timeout=9.0)


@pytest.mark.asyncio
async def test_add_text_refuses_idempotent_flag(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    with pytest.raises(NonIdempotentRetryError):
        await service.add_text(
            "nb_1",
            "Title",
            "content",
            idempotent=True,
            rpc=SimpleNamespace(rpc_call=AsyncMock()),
            wait_until_ready=AsyncMock(),
            logger=logger,
        )


@pytest.mark.asyncio
async def test_add_drive_uses_exact_rpc_shape_and_wait_hook(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    rpc = RecordingRpc(source_response("drive", "Drive Doc"))
    ready = Source(id="src_drive", title="Drive Doc")
    wait_until_ready = AsyncMock(return_value=ready)

    result = await service.add_drive(
        "nb_1",
        "drive_file",
        "Drive Doc",
        mime_type="application/pdf",
        wait=True,
        wait_timeout=7.0,
        rpc=rpc,
        list_sources=AsyncMock(return_value=[]),
        wait_until_ready=wait_until_ready,
        logger=logger,
    )

    assert result is ready
    # add_drive now wraps with idempotent_create, which requires
    # disable_internal_retries=True at the RPC layer (the wrapper owns
    # probe-then-retry recovery). operation_variant="drive" routes the
    # call through the registry's PROBE_THEN_CREATE entry.
    assert rpc.calls == [
        {
            "method": RPCMethod.ADD_SOURCE,
            "params": [
                [
                    [
                        ["drive_file", "application/pdf", 1, "Drive Doc"],
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        1,
                    ]
                ],
                "nb_1",
                [2],
                [1, None, None, None, None, None, None, None, None, None, [1]],
            ],
            "source_path": "/notebook/nb_1",
            "allow_null": True,
            "disable_internal_retries": True,
            "operation_variant": "drive",
        }
    ]
    wait_until_ready.assert_awaited_once_with("nb_1", "src_drive", timeout=7.0)


@pytest.mark.asyncio
async def test_add_drive_raises_source_add_error_on_null_result(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    with pytest.raises(SourceAddError) as exc_info:
        await service.add_drive(
            "nb_1",
            "drive_file",
            "Drive Doc",
            rpc=RecordingRpc(None),
            list_sources=AsyncMock(return_value=[]),
            wait_until_ready=AsyncMock(),
            logger=logger,
        )

    assert exc_info.value.url == "Drive Doc"
    assert "API returned no data for Drive source: Drive Doc" in str(exc_info.value)


@pytest.mark.asyncio
async def test_add_drive_preserves_rpc_error_propagation(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    # A non-transport RPCError (e.g. validation) must propagate through
    # idempotent_create as a SourceAddError, just like add_url does. The
    # cause chain preserves the original RPCError for callers that need it.
    rpc_error = RPCError("drive add failed")

    with pytest.raises(SourceAddError) as exc_info:
        await service.add_drive(
            "nb_1",
            "drive_file",
            "Drive Doc",
            rpc=SimpleNamespace(rpc_call=AsyncMock(side_effect=rpc_error)),
            list_sources=AsyncMock(return_value=[]),
            wait_until_ready=AsyncMock(),
            logger=logger,
        )

    assert exc_info.value.cause is rpc_error


def test_extract_youtube_video_id_uses_injected_parser_and_helpers(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    parsed = SimpleNamespace(hostname="www.youtube.com", path="/watch", query="v=video_123")
    parse_url = MagicMock(return_value=parsed)
    extract_video_id = MagicMock(return_value="video_123")
    is_valid = MagicMock(return_value=True)

    result = service.extract_youtube_video_id(
        " https://www.youtube.com/watch?v=video_123 ",
        parse_url=parse_url,
        extract_video_id_from_parsed_url=extract_video_id,
        is_valid_video_id=is_valid,
        logger=logger,
    )

    assert result == "video_123"
    parse_url.assert_called_once_with("https://www.youtube.com/watch?v=video_123")
    extract_video_id.assert_called_once_with(parsed, "www.youtube.com")
    is_valid.assert_called_once_with("video_123")


def test_extract_youtube_video_id_parse_error_returns_none(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    result = service.extract_youtube_video_id(
        "https://www.youtube.com/watch?v=video_123",
        parse_url=MagicMock(side_effect=ValueError("parse error")),
        extract_video_id_from_parsed_url=MagicMock(),
        is_valid_video_id=MagicMock(),
        logger=logger,
    )

    assert result is None


@pytest.mark.asyncio
async def test_raw_url_helpers_disable_internal_retries(service: SourceAddService) -> None:
    rpc = RecordingRpc(source_response("url", "URL"))

    await service.add_url_source("nb_1", "https://example.com", rpc=rpc)
    await service.add_youtube_source("nb_1", "https://youtu.be/video", rpc=rpc)

    assert rpc.calls[0]["disable_internal_retries"] is True
    assert rpc.calls[0]["params"][0][0][2] == ["https://example.com"]
    assert rpc.calls[1]["disable_internal_retries"] is True
    assert rpc.calls[1]["allow_null"] is False
    assert rpc.calls[1]["params"][0][0][7] == ["https://youtu.be/video"]


@pytest.mark.asyncio
async def test_sources_api_add_url_uses_late_bound_facade_hooks() -> None:
    core = MagicMock()
    api = SourcesAPI(core, uploader=MagicMock())
    api._extract_youtube_video_id = MagicMock(return_value="video")  # type: ignore[method-assign]
    api._add_youtube_source = AsyncMock(return_value=source_response("yt", "Video"))  # type: ignore[method-assign]
    api._add_url_source = AsyncMock()  # type: ignore[method-assign]
    api.list = AsyncMock(return_value=[])  # type: ignore[method-assign]
    api.wait_until_ready = AsyncMock(return_value=Source(id="ready"))  # type: ignore[method-assign]

    result = await api.add_url("nb_1", "https://youtu.be/video", wait=True, wait_timeout=3.0)

    assert result.id == "ready"
    api._add_youtube_source.assert_awaited_once_with("nb_1", "https://youtu.be/video")
    api._add_url_source.assert_not_awaited()
    api.wait_until_ready.assert_awaited_once_with("nb_1", "src_yt", timeout=3.0)


# ---------------------------------------------------------------------------
# CLI service layer: SSRF guard on `source add --url`
#
# These tests target ``notebooklm.cli.services.source_add.validate_url`` and
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
