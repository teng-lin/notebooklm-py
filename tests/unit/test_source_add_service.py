"""Unit tests for the private source add service."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app import source_add as cli_source_add
from notebooklm._app.errors import ErrorCategory, classify
from notebooklm._idempotency import _CreateResultKind, _IdempotentCreateResult
from notebooklm._sources import SourcesAPI, _validate_add_text_idempotency
from notebooklm._web.sources import WebSourcesAPI
from notebooklm._web.sources.add import SourceAddService, honor_requested_title_if_fresh
from notebooklm.exceptions import (
    AuthError,
    NetworkError,
    NonIdempotentRetryError,
    RateLimitError,
    ServerError,
    SourceAddError,
)
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
    """A source absent from the baseline and present at probe time is ours.

    ``list_sources`` is called twice: once for the pre-create baseline (empty)
    and once for the probe. Returning ``[created]`` from *both* would make the
    match ambiguous — see
    ``test_add_url_probe_ignores_a_source_present_before_the_create``.
    """
    created = Source(id="src_created", url="https://example.com")
    add_url_source = AsyncMock(side_effect=NetworkError("temporary network failure"))
    list_sources = AsyncMock(side_effect=[[], [created]])

    source = await service.add_url(
        "nb_1",
        created.url,
        add_youtube_source=AsyncMock(),
        add_url_source=add_url_source,
        list_sources=list_sources,
        wait_until_ready=AsyncMock(),
        extract_youtube_video_id=MagicMock(return_value=None),
        is_youtube_url=MagicMock(return_value=False),
        logger=logger,
    )

    assert source is created
    add_url_source.assert_awaited_once_with("nb_1", created.url)
    assert list_sources.await_count == 2, "baseline + probe"


@pytest.mark.asyncio
async def test_add_url_probe_ignores_a_source_present_before_the_create(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    """The probe must not adopt a same-URL source that predates the call (#2204).

    A URL is not unique within a notebook, so the ``source.url == url`` match
    alone cannot tell "my create landed" from "a copy was already there". Here
    the notebook holds the URL both before and after the failed create — i.e.
    nothing new appeared — so the probe must report no match and let
    ``idempotent_create`` retry rather than hand back the pre-existing id.
    """
    pre_existing = Source(id="src_pre_existing", url="https://example.com")
    add_url_source = AsyncMock(side_effect=NetworkError("temporary network failure"))

    with pytest.raises(NetworkError):
        await service.add_url(
            "nb_1",
            pre_existing.url,
            add_youtube_source=AsyncMock(),
            add_url_source=add_url_source,
            list_sources=AsyncMock(return_value=[pre_existing]),
            wait_until_ready=AsyncMock(),
            extract_youtube_video_id=MagicMock(return_value=None),
            is_youtube_url=MagicMock(return_value=False),
            logger=logger,
        )

    # Both attempts fired: the probe refused to claim the pre-existing source.
    assert add_url_source.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "baseline_failure",
    [
        # A drifted GET_NOTEBOOK: the strict decoder raises, not the transport.
        pytest.param(RPCError("baseline decode failed"), id="decode_failure"),
        # A transport failure. The probe deliberately re-RAISES this class; the
        # baseline capture deliberately swallows it, so pin that asymmetry.
        pytest.param(ServerError("baseline 503"), id="transport_failure"),
    ],
)
async def test_add_url_baseline_failure_makes_a_match_ambiguous(
    service: SourceAddService,
    logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
    baseline_failure: Exception,
) -> None:
    """No baseline + a match = ``SourceAddError``, not a guess (#2204).

    The baseline list is best-effort; when it fails, a probe match may or may
    not predate this add, and adopting it is exactly the failure mode the
    baseline exists to prevent. The error must carry enough to act on: what
    broke the baseline (as ``cause``), and which source is ambiguous.
    """
    existing = Source(
        id="src_ambiguous",
        title="Highly accurate protein structure prediction with AlphaFold",
        url="https://www.nature.com/articles/s41586-024-07487-w",
    )
    # First call = the baseline (fails); second = the probe.
    list_sources = AsyncMock(side_effect=[baseline_failure, [existing]])
    transport_error = NetworkError("temporary network failure")

    with (
        caplog.at_level(logging.WARNING, logger=logger.name),
        pytest.raises(SourceAddError) as raised,
    ):
        await service.add_url(
            "nb_1",
            existing.url,
            add_youtube_source=AsyncMock(),
            add_url_source=AsyncMock(side_effect=transport_error),
            list_sources=list_sources,
            wait_until_ready=AsyncMock(),
            extract_youtube_video_id=MagicMock(return_value=None),
            is_youtube_url=MagicMock(return_value=False),
            logger=logger,
        )

    # The message names the ambiguous source, so the caller told to "check the
    # notebook source list" knows which row to look at.
    assert "src_ambiguous" in str(raised.value)
    # ``cause`` carries what broke the baseline — otherwise nothing in the
    # process can explain why the snapshot was unavailable.
    assert raised.value.cause is baseline_failure
    # The transport error that triggered the probe survives as context.
    assert raised.value.__context__ is transport_error
    # An ambiguity IS an unconfirmed create (#2220 review): nothing threw
    # inside the probe, so this looks like an ordinary rejection — but the
    # server may hold a row either way, which is precisely what the marker
    # names. Unmarked, it classifies as the non-fatal per-item SOURCE_ADD and
    # a batch add would keep going, issuing more unresolvable writes.
    assert getattr(raised.value, "unconfirmed", False) is True
    assert classify(raised.value).category is ErrorCategory.RPC
    assert classify(raised.value).retriable is False
    # The action survives the real MCP/REST truncation slice (#2238).
    assert len(str(raised.value)) > 300
    assert "check the notebook source list before retrying" in str(raised.value)[:300].lower()
    # The swallow is visible at the default logger level (WARNING), not DEBUG.
    assert "baseline list() failed" in caplog.text


@pytest.mark.asyncio
async def test_add_url_probe_raises_on_multiple_new_matches(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    """Two *new* same-URL sources after the failure is ambiguity, not a match.

    The message must name both, since the caller is being told to go and
    disambiguate a URL that by definition appears in the list more than once.
    """
    url = "https://www.nature.com/articles/s41586-024-07487-w"
    list_sources = AsyncMock(
        side_effect=[
            [],
            [
                Source(id="src_a", title="Highly accurate protein structure prediction", url=url),
                Source(id="src_b", title="A second long duplicate source title", url=url),
            ],
        ]
    )

    with pytest.raises(SourceAddError, match="probe found 2 new sources") as raised:
        await service.add_url(
            "nb_1",
            url,
            add_youtube_source=AsyncMock(),
            add_url_source=AsyncMock(side_effect=NetworkError("temporary network failure")),
            list_sources=list_sources,
            wait_until_ready=AsyncMock(),
            extract_youtube_video_id=MagicMock(return_value=None),
            is_youtube_url=MagicMock(return_value=False),
            logger=logger,
        )

    assert "src_a" in str(raised.value)
    assert "src_b" in str(raised.value)
    # An ambiguity IS an unconfirmed create (#2220 review): nothing threw
    # inside the probe, so this looks like an ordinary rejection — but the
    # server may hold a row either way, which is precisely what the marker
    # names. Unmarked, it classifies as the non-fatal per-item SOURCE_ADD and
    # a batch add would keep going, issuing more unresolvable writes.
    assert getattr(raised.value, "unconfirmed", False) is True
    assert classify(raised.value).category is ErrorCategory.RPC
    # _describe_sources grows with every match; guidance must remain before it.
    assert len(str(raised.value)) > 300
    assert "check the notebook source list before retrying" in str(raised.value)[:300].lower()
    assert classify(raised.value).retriable is False


@pytest.mark.asyncio
async def test_add_url_baseline_failure_does_not_break_a_successful_add(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    """A failed baseline degrades the probe; it must not fail the add (#2204).

    The baseline is best-effort by design: it exists to disambiguate a *retry*,
    so a transient notebook read must not turn an otherwise perfectly good
    ``add_url`` into an error. Pins the resilience half of the swallow — the
    regression this catches is someone narrowing that ``except Exception`` to
    mirror the probe's transport re-raise, which would fail every add whenever
    the notebook read blips.
    """
    add_url_source = AsyncMock(return_value=source_response("ok", "Example"))

    source = await service.add_url(
        "nb_1",
        "https://example.com",
        add_youtube_source=AsyncMock(),
        add_url_source=add_url_source,
        list_sources=AsyncMock(side_effect=ServerError("baseline 503")),
        wait_until_ready=AsyncMock(),
        extract_youtube_video_id=MagicMock(return_value=None),
        is_youtube_url=MagicMock(return_value=False),
        logger=logger,
    )

    assert source.id == "src_ok"
    assert add_url_source.await_count == 1, "the add must not be retried"


@pytest.mark.asyncio
async def test_probed_result_without_proven_freshness_skips_the_rename() -> None:
    """The #1988 default still holds for a probe that cannot prove freshness.

    Both production call sites now pass ``probe_proves_freshness=True``, so the
    default branch would otherwise be unreachable and unpinned — and it is the
    only thing stopping a future third caller with an un-baselined probe from
    renaming a source it did not create.
    """
    probed = Source(id="not_mine", title="Someone Else's Title")
    rename = AsyncMock()

    result = await honor_requested_title_if_fresh(
        rename,
        "nb_1",
        _IdempotentCreateResult(probed, _CreateResultKind.PROBED),
        "Retitle me",
        logging.getLogger("tests.source_add"),
    )

    assert result is probed
    rename.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_url_probe_decode_failure_propagates_without_retrying(
    service: SourceAddService,
    logger: logging.Logger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A probe that cannot answer aborts the add instead of retrying (#2220).

    A decode failure leaves the probe unable to say whether the create landed.
    Returning "no match" there would re-issue ``ADD_SOURCE`` on no evidence,
    and this variant runs with ``disable_internal_retries=True`` precisely
    because a blind re-POST can duplicate. So the create must NOT fire twice.

    The raised error has to stay diagnosable end to end: the decode failure as
    ``cause``/``__cause__``, and the transport failure that triggered the probe
    as ``__context__`` (``idempotent_create`` awaits the probe inside its
    handler for that error).
    """
    add_url_source = AsyncMock(side_effect=NetworkError("temporary network failure"))
    probe_error = RPCError("probe decode failed")
    # Exactly two entries: baseline, then the probe that fails. A third list
    # call would mean the retry loop continued — StopIteration, not a pass.
    list_sources = AsyncMock(side_effect=[[], probe_error])

    with (
        caplog.at_level(logging.WARNING, logger=logger.name),
        pytest.raises(SourceAddError) as exc_info,
    ):
        await service.add_url(
            "nb_1",
            "https://example.com",
            add_youtube_source=AsyncMock(),
            add_url_source=add_url_source,
            list_sources=list_sources,
            wait_until_ready=AsyncMock(),
            extract_youtube_video_id=MagicMock(return_value=None),
            is_youtube_url=MagicMock(return_value=False),
            logger=logger,
        )

    assert "will not be retried" in caplog.text
    # The load-bearing assertion: one create, not two. Flip the probe back to
    # ``return None`` and this is what fails.
    assert add_url_source.await_count == 1
    assert exc_info.value.cause is probe_error
    assert exc_info.value.__cause__ is probe_error
    assert exc_info.value.__context__ is probe_error
    # The transport error that made the probe run at all is still reachable.
    assert isinstance(probe_error.__context__, NetworkError)


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
            # Nested template block per the Gemini-3.5 wire migration (#1546).
            "params": [
                [[None, ["Title", "content"], None, 2, None, None, None, None, None, None, 1]],
                "nb_1",
                [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
            ],
            "source_path": "/notebook/nb_1",
            "allow_null": False,
            "disable_internal_retries": False,
            "operation_variant": "text",
        }
    ]
    wait_until_ready.assert_awaited_once_with("nb_1", "src_text", timeout=9.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_error",
    [
        RateLimitError("quota exceeded", retry_after=30),
        AuthError("csrf token expired"),
        ServerError("upstream 503"),
        NetworkError("connection reset"),
    ],
    ids=["rate_limit", "auth", "server", "network"],
)
async def test_add_text_propagates_narrow_transport_errors_unwrapped(
    service: SourceAddService,
    logger: logging.Logger,
    transport_error: Exception,
) -> None:
    # ADR-0019 cross-cutting rule: typed transport errors propagate UNWRAPPED
    # so callers can catch RateLimitError (back-off via retry_after), AuthError
    # (re-login), ServerError (transient retry) — the same catch ordering
    # add_url and add_drive already follow. Before the fix, add_text's bare
    # ``except RPCError`` collapsed all of these into SourceAddError.
    with pytest.raises(type(transport_error)) as exc_info:
        await service.add_text(
            "nb_1",
            "Title",
            "content",
            rpc=SimpleNamespace(rpc_call=AsyncMock(side_effect=transport_error)),
            wait_until_ready=AsyncMock(),
            logger=logger,
        )

    assert exc_info.value is transport_error
    assert not isinstance(exc_info.value, SourceAddError)


@pytest.mark.asyncio
async def test_add_text_wraps_generic_rpc_error(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    # The residual broad RPCError (e.g. validation / decode-shaped failures)
    # still wraps into SourceAddError, with the original preserved on both the
    # ``cause`` attribute and the ``raise ... from`` chain.
    rpc_error = RPCError("text add failed")

    with pytest.raises(SourceAddError) as exc_info:
        await service.add_text(
            "nb_1",
            "Title",
            "content",
            rpc=SimpleNamespace(rpc_call=AsyncMock(side_effect=rpc_error)),
            wait_until_ready=AsyncMock(),
            logger=logger,
        )

    assert exc_info.value.cause is rpc_error
    assert exc_info.value.__cause__ is rpc_error
    assert "Failed to add text source 'Title'" in str(exc_info.value)


def test_add_text_refuses_idempotent_flag_before_service_admission() -> None:
    with pytest.raises(NonIdempotentRetryError):
        _validate_add_text_idempotency(True)


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
    msg = str(exc_info.value)
    assert "API returned no data for Drive source: Drive Doc" in msg
    # The message names the attempted mime and hints (not asserts) that the type
    # may not be importable via Drive, steering the user to the `file` upload path.
    assert "mime_type=" in msg
    assert "may not be importable" in msg
    assert "file" in msg
    assert "download" in msg.lower()


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
    # URL add migrated to the nested trailing block (#1546): spec gains a
    # trailing 1 and the flat [2],None,None tail becomes [2,None,None,[1,...,[1]]].
    assert rpc.calls[0]["params"][0][0][-1] == 1
    assert rpc.calls[0]["params"][2] == [
        2,
        None,
        None,
        [1, None, None, None, None, None, None, None, None, None, [1]],
    ]
    assert len(rpc.calls[0]["params"]) == 3
    assert rpc.calls[1]["disable_internal_retries"] is True
    assert rpc.calls[1]["allow_null"] is False
    assert rpc.calls[1]["params"][0][0][7] == ["https://youtu.be/video"]


@pytest.mark.asyncio
async def test_sources_api_add_url_uses_late_bound_facade_hooks() -> None:
    core = MagicMock()
    api = WebSourcesAPI(core, supervisor=core, uploader=MagicMock())
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
# #1960: honor an explicit ``title`` for backend-re-derived source types
# (YouTube / Drive / web page) via a best-effort post-add rename.
# ---------------------------------------------------------------------------


def _sources_api_with_mocked_adder() -> SourcesAPI:
    api = WebSourcesAPI(MagicMock(), supervisor=MagicMock(), uploader=MagicMock())
    api._adder = MagicMock()  # type: ignore[assignment]
    return api


@pytest.mark.asyncio
async def test_add_url_honors_title_via_post_add_rename() -> None:
    api = _sources_api_with_mocked_adder()
    api._adder.add_url = AsyncMock(return_value=Source(id="src_yt", title="Upstream Video Title"))
    api.rename = AsyncMock(return_value=Source(id="src_yt", title="My Title"))  # type: ignore[method-assign]

    result = await api.add_url("nb_1", "https://youtu.be/video", title="My Title")

    api.rename.assert_awaited_once_with("nb_1", "src_yt", "My Title")
    assert result.id == "src_yt"
    assert result.title == "My Title"


@pytest.mark.asyncio
async def test_add_drive_honors_title_via_post_add_rename() -> None:
    api = _sources_api_with_mocked_adder()
    api._adder.add_drive = AsyncMock(return_value=Source(id="d1", title="Drive Name"))
    api.rename = AsyncMock(return_value=Source(id="d1", title="My Title"))  # type: ignore[method-assign]

    result = await api.add_drive("nb_1", "file123", "My Title")

    api.rename.assert_awaited_once_with("nb_1", "d1", "My Title")
    assert result.title == "My Title"


@pytest.mark.asyncio
async def test_add_url_without_title_skips_rename() -> None:
    api = _sources_api_with_mocked_adder()
    api._adder.add_url = AsyncMock(return_value=Source(id="s1", title="Upstream"))
    api.rename = AsyncMock()  # type: ignore[method-assign]

    result = await api.add_url("nb_1", "https://example.com")

    api.rename.assert_not_awaited()
    assert result.title == "Upstream"


@pytest.mark.asyncio
async def test_add_drive_empty_title_skips_rename() -> None:
    api = _sources_api_with_mocked_adder()
    api._adder.add_drive = AsyncMock(return_value=Source(id="d1", title="Drive Name"))
    api.rename = AsyncMock()  # type: ignore[method-assign]

    result = await api.add_drive("nb_1", "file123", "")

    api.rename.assert_not_awaited()
    assert result.title == "Drive Name"


@pytest.mark.asyncio
async def test_add_url_title_matching_upstream_skips_rename() -> None:
    api = _sources_api_with_mocked_adder()
    api._adder.add_url = AsyncMock(return_value=Source(id="s1", title="Same Title"))
    api.rename = AsyncMock()  # type: ignore[method-assign]

    # A leading/trailing-whitespace-only difference is not a real retitle.
    result = await api.add_url("nb_1", "https://example.com", title="  Same Title  ")

    api.rename.assert_not_awaited()
    assert result.title == "Same Title"


@pytest.mark.asyncio
async def test_add_rename_failure_is_non_fatal(caplog: pytest.LogCaptureFixture) -> None:
    api = _sources_api_with_mocked_adder()
    api._adder.add_drive = AsyncMock(return_value=Source(id="d1", title="Drive Name"))
    api.rename = AsyncMock(side_effect=NetworkError("boom"))  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        result = await api.add_drive("nb_1", "file123", "My Title")

    # The add succeeded — a failed rename must not raise; the upstream title is kept.
    assert result.id == "d1"
    assert result.title == "Drive Name"
    api.rename.assert_awaited_once_with("nb_1", "d1", "My Title")
    assert "rename" in caplog.text.lower()


@pytest.mark.asyncio
async def test_add_url_honor_preserves_metadata_over_sparse_rename_echo() -> None:
    """UPDATE_SOURCE's echo can be sparse (id + title only); the honored result must keep
    the added source's url/type and only swap in the new title, not return the bare echo
    (which would drop url → kind='unknown'). #1960."""
    api = _sources_api_with_mocked_adder()
    added = Source(id="s1", title="Upstream Video Title", url="https://youtu.be/v", _type_code=5)
    api._adder.add_url = AsyncMock(return_value=added)
    # A sparse UPDATE_SOURCE echo: just id + the renamed title, no url/_type_code.
    api.rename = AsyncMock(return_value=Source(id="s1", title="My Title"))  # type: ignore[method-assign]

    result = await api.add_url("nb_1", "https://youtu.be/v", title="My Title")

    assert result.title == "My Title"  # requested title applied
    assert result.url == "https://youtu.be/v"  # preserved from the added source
    assert result._type_code == 5  # preserved — not dropped by the sparse echo


@pytest.mark.asyncio
async def test_add_text_does_not_rename() -> None:
    api = _sources_api_with_mocked_adder()
    api._adder.add_text = AsyncMock(return_value=Source(id="t1", title="My Notes"))
    api.rename = AsyncMock()  # type: ignore[method-assign]

    result = await api.add_text("nb_1", "My Notes", "content")

    # ``text`` sources honor ``title`` on the wire — no post-add rename.
    api.rename.assert_not_awaited()
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


def _drive_source(source_id: str, file_id: str, title: str = "Drive Doc") -> Source:
    """A Drive-backed row, built from the captured wire shape.

    Copied from the live ``GET_NOTEBOOK`` capture in
    ``tests/cassettes/web/sources_check_freshness_drive.yaml`` (the same shape
    ``tests/integration/test_sources_idempotency.py::_google_docs_source_row``
    uses): the Drive block sits at ``metadata[0]`` and **no** URL slot is
    populated — which is exactly why the pre-#2113 URL-based probe could never
    match one. Decoding a real row rather than setting the property keeps this
    honest: if the ``documentId`` slot ever moves, these tests notice.
    """
    metadata: list = [
        [file_id, "SCRUBBED_AONS", 12],
        911,
        [1769105469, 316769000],
        ["d4325602-2399-44c2-b45b-9df8f433189f", [1769105982, 178269000]],
        1,  # SourceType.GOOGLE_DOCS
        None,
        1,
    ]
    source = Source.from_api_response(
        [[source_id], title, metadata, [None, 2]],
        method_id=RPCMethod.GET_NOTEBOOK.value,
    )
    # Guard the fixture itself: a silently non-matching row would make the
    # probe return None and the test would fail for the wrong reason.
    assert source.drive_document_id == file_id, "fixture no longer decodes as a Drive row"
    return source


@pytest.mark.asyncio
async def test_add_drive_baseline_failure_makes_a_match_ambiguous(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    """The Drive twin of ``test_add_url_baseline_failure_makes_a_match_ambiguous``.

    This branch had **no** test at all before #2220's round-4 review — neither
    for the #2113 ambiguity behaviour nor for the marker. A ``documentId`` is
    not unique within a notebook, so without a baseline a match may predate the
    add; adopting it would report a create that never landed.
    """
    file_id = "drive_file_1"
    existing = _drive_source("src_pre_existing", file_id)
    rpc = SimpleNamespace(rpc_call=AsyncMock(side_effect=ServerError("commit lost")))

    with pytest.raises(SourceAddError, match="Cannot disambiguate Drive source") as raised:
        await service.add_drive(
            "nb_1",
            file_id,
            "Drive Doc",
            rpc=rpc,
            # baseline fails, then the probe finds the unattributable match
            list_sources=AsyncMock(side_effect=[RPCError("baseline decode failed"), [existing]]),
            wait_until_ready=AsyncMock(),
            logger=logger,
        )

    # Parity with add_url: the error names what broke the baseline, because the
    # caller reads "baseline snapshot failed" long after that read happened and
    # nothing else in the process can explain it.
    # The load-bearing assertion: ONE create. The finite ``side_effect`` list
    # would also fail a runaway loop, but with a StopIteration that says nothing
    # about what went wrong; this names it.
    assert rpc.rpc_call.await_count == 1
    assert isinstance(raised.value.cause, RPCError)
    assert "RPCError" in str(raised.value)
    # The create's outcome is unknown, so this must not be classified as a
    # per-item input rejection a batch add can isolate and continue past.
    assert getattr(raised.value, "unconfirmed", False) is True
    assert classify(raised.value).category is ErrorCategory.RPC
    assert classify(raised.value).retriable is False


@pytest.mark.asyncio
async def test_add_drive_probe_raises_on_multiple_new_matches(
    service: SourceAddService,
    logger: logging.Logger,
) -> None:
    """Two new rows sharing a ``documentId`` cannot be told apart — raise, don't pick.

    The repo's own cassette holds two source ids sharing one ``documentId``
    (#2113), so this is a real shape, not a hypothetical.
    """
    file_id = "drive_file_2"
    first = _drive_source("src_a", file_id)
    second = _drive_source("src_b", file_id)
    rpc = SimpleNamespace(rpc_call=AsyncMock(side_effect=ServerError("commit lost")))

    with pytest.raises(SourceAddError, match="probe found 2 new sources") as raised:
        await service.add_drive(
            "nb_1",
            file_id,
            "Drive Doc",
            rpc=rpc,
            list_sources=AsyncMock(side_effect=[[], [first, second]]),
            wait_until_ready=AsyncMock(),
            logger=logger,
        )

    # The load-bearing assertion: ONE create. The finite ``side_effect`` list
    # would also fail a runaway loop, but with a StopIteration that says nothing
    # about what went wrong; this names it.
    assert rpc.rpc_call.await_count == 1
    assert "src_a" in str(raised.value) or "2 new sources" in str(raised.value)
    assert getattr(raised.value, "unconfirmed", False) is True
    assert classify(raised.value).category is ErrorCategory.RPC
