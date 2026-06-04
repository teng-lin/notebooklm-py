"""Targeted coverage tests for ``notebooklm._source.upload``.

These tests exercise the error handlers, edge-case branches, and
streaming/finalize paths in the upload pipeline that the existing
``test_sources_upload.py`` / ``test_source_upload_pipeline.py`` suites do
not reach. They directly drive the module-level helper functions plus the
``SourceUploadPipeline`` collaborator slots so the assertions reflect real
behaviour rather than tautologies.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import SplitResult, urlsplit

import httpx
import pytest

import notebooklm._source.upload as _upload_mod
from notebooklm._source.upload import (
    SourceUploadPipeline,
    _build_invalid_argument_source_limit_hint,
    _coerce_source_id_candidate,
    _default_port_for_scheme,
    _extract_register_file_source_id,
    _looks_like_id_string,
    _redact_upload_url,
    _redacted_upload_authority,
    _register_response_shape_label,
    _resolve_upload_content_type,
    _validate_resumable_upload_url,
)
from notebooklm.exceptions import (
    AuthError,
    NetworkError,
    ValidationError,
)
from notebooklm.rpc import RPCError
from notebooklm.types import Source, SourceAddError

# =============================================================================
# Module-level helper functions
# =============================================================================


def test_default_port_for_scheme_unknown_scheme_returns_none() -> None:
    """Non-http(s) schemes have no implicit default port (line 110)."""
    assert _default_port_for_scheme("https") == 443
    assert _default_port_for_scheme("http") == 80
    assert _default_port_for_scheme("ftp") is None


def test_redacted_upload_authority_returns_none_when_host_missing() -> None:
    """A URL with no hostname yields ``None`` authority (line 116)."""
    parsed = urlsplit("file:///local/path")
    assert parsed.hostname is None
    assert _redacted_upload_authority(parsed) is None


def test_redacted_upload_authority_brackets_ipv6_host() -> None:
    """IPv6 hosts are wrapped in brackets and keep the port suffix."""
    parsed = urlsplit("https://[2001:db8::1]:8443/upload")
    assert _redacted_upload_authority(parsed) == "[2001:db8::1]:8443"


def test_redact_upload_url_returns_placeholder_when_scheme_missing() -> None:
    """A scheme-less / authority-less URL redacts to the placeholder (line 134)."""
    assert _redact_upload_url("not-a-url") == "[REDACTED_UPLOAD_URL]"
    assert _redact_upload_url("///just/a/path") == "[REDACTED_UPLOAD_URL]"


def test_redact_upload_url_value_error_returns_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A urlsplit ValueError is swallowed into the redacted placeholder."""
    # ADR-0007 forbids string-target ``mock.patch`` on notebooklm internals;
    # patch the module-level ``urlsplit`` seam object-form instead.
    fake_urlsplit = MagicMock(side_effect=ValueError("bad url"))
    monkeypatch.setattr(_upload_mod, "urlsplit", fake_urlsplit)
    assert _redact_upload_url("https://example.com/x") == "[REDACTED_UPLOAD_URL]"
    fake_urlsplit.assert_called_once_with("https://example.com/x")


def test_validate_resumable_upload_url_value_error_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A urlsplit ValueError becomes a ValidationError (lines 146-147)."""

    def _boom(_url: str) -> SplitResult:
        raise ValueError("malformed")

    # ADR-0007 forbids string-target ``mock.patch`` on notebooklm internals;
    # patch the module-level ``urlsplit`` seam object-form instead.
    fake_urlsplit = MagicMock(side_effect=_boom)
    monkeypatch.setattr(_upload_mod, "urlsplit", fake_urlsplit)
    with pytest.raises(ValidationError, match="Upload URL is not valid"):
        _validate_resumable_upload_url("https://example.com/?upload_id=x")
    fake_urlsplit.assert_called_once_with("https://example.com/?upload_id=x")


def test_validate_resumable_upload_url_missing_host_raises() -> None:
    """An https URL with no host is rejected (line 154).

    ``https:///path`` parses with scheme ``https`` but ``hostname is None``,
    so it reaches the host-missing guard rather than the scheme guard.
    """
    with pytest.raises(ValidationError, match="must include a host"):
        _validate_resumable_upload_url("https:///upload/_/?upload_id=session")


def test_register_response_shape_label_all_branches() -> None:
    """Every shape label branch is exercised (lines 407, 414)."""
    assert _register_response_shape_label({"a": 1}) == "object"
    assert _register_response_shape_label([1, 2]) == "array"
    assert _register_response_shape_label("hi") == "string"
    assert _register_response_shape_label(None) == "null"
    assert _register_response_shape_label(123) == "int"


def test_looks_like_id_string_rejects_whitespace_and_slash() -> None:
    """Candidates containing space/tab/slash are not id-like (line 422)."""
    assert _looks_like_id_string("has space1") is False
    assert _looks_like_id_string("has\ttab1") is False
    assert _looks_like_id_string("path/to/1") is False
    # Sanity: a plausible id still passes.
    assert _looks_like_id_string("src_1234") is True


def test_coerce_source_id_candidate_rejects_overlong_string() -> None:
    """Strings longer than 1000 chars are rejected outright (line 380)."""
    assert _coerce_source_id_candidate("x" * 1001, "f.pdf") is None


def test_coerce_source_id_candidate_rejects_filename_echo() -> None:
    """A value equal to the filename is rejected (line 383)."""
    assert _coerce_source_id_candidate("report.pdf", "report.pdf") is None
    # Empty after strip is also rejected.
    assert _coerce_source_id_candidate("   ", "report.pdf") is None


def test_resolve_upload_content_type_blank_mime_raises() -> None:
    """A whitespace-only explicit mime_type is rejected (line 431)."""
    from pathlib import Path

    with pytest.raises(ValidationError, match="cannot be empty or whitespace-only"):
        _resolve_upload_content_type(Path("a.bin"), "   ")


def test_extract_register_file_source_id_ambiguous_field_candidates() -> None:
    """Two distinct context-matched SOURCE_IDs are ambiguous -> None (line 271).

    Each inner dict carries a matching ``SOURCE_NAME`` so both SOURCE_IDs are
    collected as field candidates; two distinct ids -> ambiguous -> None.
    """
    result = [
        {
            "SOURCE_NAME": "report.pdf",
            "SOURCE_ID": "11111111-2222-3333-4444-555555555555",
        },
        {
            "SOURCE_NAME": "report.pdf",
            "SOURCE_ID": "99999999-8888-7777-6666-555555555555",
        },
    ]
    assert _extract_register_file_source_id(result, "report.pdf") is None


def test_extract_register_file_source_id_ambiguous_row_candidates() -> None:
    """Two distinct contextual row SOURCE_IDs are ambiguous -> None (line 277).

    No SOURCE_ID/id field candidates exist, so extraction falls through to
    the contextual-row walk, which finds two filename-paired ids.
    """
    uuid_a = "11111111-2222-3333-4444-555555555555"
    uuid_b = "99999999-8888-7777-6666-555555555555"
    result = [
        [uuid_a, "report.pdf"],
        [uuid_b, "report.pdf"],
    ]
    assert _extract_register_file_source_id(result, "report.pdf") is None


def test_extract_register_file_source_id_skips_non_string_dict_keys() -> None:
    """Dict keys that are not strings are skipped during the walk (line 307)."""
    uuid = "11111111-2222-3333-4444-555555555555"
    result = {1: "ignored", ("tuple",): "ignored", "SOURCE_ID": uuid}
    assert _extract_register_file_source_id(result, "report.pdf") == uuid


# =============================================================================
# _build_invalid_argument_source_limit_hint()
# =============================================================================


class TestSourceLimitHint:
    """Cover each branch of the ADD_SOURCE_FILE status-code-3 hint builder."""

    @pytest.mark.asyncio
    async def test_limit_lookup_exception_logged_and_ignored(self) -> None:
        """A failing source-limit lookup must not mask the upload error (181-182)."""
        logger = MagicMock()

        async def _boom() -> int | None:
            raise RuntimeError("limit lookup down")

        hint = await _build_invalid_argument_source_limit_hint(
            source_count=None,
            get_source_limit=_boom,
            logger=logger,
        )
        # No count and no usable limit -> empty hint (line 219).
        assert hint == ""
        logger.debug.assert_called_once()

    @pytest.mark.asyncio
    async def test_nonpositive_limit_coerced_to_none(self) -> None:
        """A non-positive limit is treated as unavailable (line 188)."""

        async def _zero() -> int | None:
            return 0

        # count below floor + no usable limit -> empty (line 219).
        hint = await _build_invalid_argument_source_limit_hint(
            source_count=3,
            get_source_limit=_zero,
            logger=MagicMock(),
        )
        assert hint == ""

    @pytest.mark.asyncio
    async def test_count_at_or_above_limit_returns_at_limit_hint(self) -> None:
        """count >= limit yields the 'reached its limit' hint (around 191-197)."""

        async def _limit() -> int | None:
            return 100

        hint = await _build_invalid_argument_source_limit_hint(
            source_count=100,
            get_source_limit=_limit,
            logger=MagicMock(),
        )
        assert "100/100" in hint
        assert "per-notebook source limit" in hint

    @pytest.mark.asyncio
    async def test_count_below_limit_returns_below_limit_hint(self) -> None:
        """count < limit yields the 'below the advertised limit' hint (line 198)."""

        async def _limit() -> int | None:
            return 100

        hint = await _build_invalid_argument_source_limit_hint(
            source_count=10,
            get_source_limit=_limit,
            logger=MagicMock(),
        )
        assert "10/100" in hint
        assert "below" in hint

    @pytest.mark.asyncio
    async def test_count_above_floor_without_limit_returns_floor_hint(self) -> None:
        """count >= floor with no limit yields the tier-summary hint (204-205)."""
        hint = await _build_invalid_argument_source_limit_hint(
            source_count=75,
            get_source_limit=None,
            logger=MagicMock(),
        )
        assert "75 sources" in hint
        assert "50/100/300/600" in hint

    @pytest.mark.asyncio
    async def test_limit_only_without_count_returns_limit_hint(self) -> None:
        """A usable limit but no count yields the advertised-limit hint (212-213)."""

        async def _limit() -> int | None:
            return 300

        hint = await _build_invalid_argument_source_limit_hint(
            source_count=None,
            get_source_limit=_limit,
            logger=MagicMock(),
        )
        assert "Advertised source limit for this tier is 300" in hint


# =============================================================================
# SourceUploadPipeline collaborator/instance branches
# =============================================================================


class _Lifecycle:
    def __init__(self) -> None:
        self.asserted = 0

    def assert_bound_loop(self) -> None:
        self.asserted += 1


class _Drain:
    def operation_scope(self, _label: str):
        @asynccontextmanager
        async def scope() -> AsyncIterator[None]:
            yield None

        return scope()


def _make_pipeline(
    *,
    kernel: Any | None = None,
    rpc: Any | None = None,
    async_client_factory: Any | None = None,
    get_source_limit: Any | None = None,
) -> SourceUploadPipeline:
    auth = MagicMock()
    auth.authuser = 0
    auth.account_email = None
    return SourceUploadPipeline(
        rpc=rpc or MagicMock(),
        drain=_Drain(),
        lifecycle=_Lifecycle(),
        kernel=kernel if kernel is not None else MagicMock(),
        auth=auth,
        async_client_factory=async_client_factory,
        get_source_limit=get_source_limit,
    )


def test_live_cookies_uses_get_http_client_when_kernel_lacks_cookies() -> None:
    """A kernel without a ``cookies`` attribute falls back to get_http_client (521)."""

    class KernelWithHttpClient:
        # No ``cookies`` attribute on purpose.
        def __init__(self) -> None:
            jar = httpx.Cookies()
            jar.set("x", "y", domain="example.com")
            self._jar = jar
            self.get_http_client = MagicMock(return_value=MagicMock(cookies=jar))

    kernel = KernelWithHttpClient()
    pipeline = _make_pipeline(kernel=kernel)
    cookies = pipeline._live_cookies()
    assert cookies is kernel._jar
    kernel.get_http_client.assert_called_once()


def test_live_cookies_returns_empty_jar_when_cookies_is_none() -> None:
    """A kernel exposing ``cookies = None`` and no http client returns an empty jar (522-523)."""

    class KernelNoCookies:
        cookies = None
        get_http_client = None

    pipeline = _make_pipeline(kernel=KernelNoCookies())
    cookies = pipeline._live_cookies()
    assert isinstance(cookies, httpx.Cookies)
    assert len(cookies.jar) == 0


def test_live_cookies_casts_non_cookies_truthy_value() -> None:
    """A truthy non-Cookies ``cookies`` with no http client is cast through (line 524)."""
    sentinel = object()

    class KernelOddCookies:
        cookies = sentinel
        get_http_client = None

    pipeline = _make_pipeline(kernel=KernelOddCookies())
    assert pipeline._live_cookies() is sentinel


@pytest.mark.asyncio
async def test_list_sources_delegates_to_lister() -> None:
    """``list_sources`` proxies to the internal SourceLister (line 915)."""
    pipeline = _make_pipeline()
    expected = [Source(id="s1", title="a.pdf")]
    pipeline._lister.list = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    result = await pipeline.list_sources("nb_1")
    assert result == expected
    pipeline._lister.list.assert_awaited_once_with("nb_1")


@pytest.mark.asyncio
async def test_add_file_asserts_bound_loop_before_work(tmp_path) -> None:
    """``add_file`` calls assert_bound_loop before touching the semaphore (605-607)."""
    lifecycle = _Lifecycle()
    auth = MagicMock()
    auth.authuser = 0
    auth.account_email = None
    pipeline = SourceUploadPipeline(
        rpc=MagicMock(),
        drain=_Drain(),
        lifecycle=lifecycle,
        kernel=MagicMock(),
        auth=auth,
    )
    # Make assert_bound_loop the thing that fails so we prove it runs first,
    # before any filesystem resolution or semaphore allocation.
    lifecycle.assert_bound_loop = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("wrong loop")
    )
    with pytest.raises(RuntimeError, match="wrong loop"):
        await pipeline.add_file("nb_1", str(tmp_path / "missing.pdf"))
    lifecycle.assert_bound_loop.assert_called_once()


# =============================================================================
# register_file_source() probe / create branches
# =============================================================================


class TestRegisterFileSourceBranches:
    """Cover baseline-failure, probe, and missing-id recovery paths."""

    @pytest.mark.asyncio
    async def test_baseline_list_failure_logs_and_makes_baseline_unavailable(self) -> None:
        """A failing baseline list() leaves the baseline unavailable (772, 802-808).

        With baseline unavailable, a same-titled probe match is treated as an
        ambiguity rather than silently returned. We drive a create RPC failure
        (NetworkError) so idempotent_create runs the probe, which then finds a
        same-titled source and raises SourceAddError.
        """
        pipeline = _make_pipeline()
        logger = MagicMock()

        list_calls = {"n": 0}

        async def _list(_nb: str) -> list[Source]:
            list_calls["n"] += 1
            if list_calls["n"] == 1:
                # Baseline call fails -> baseline unavailable.
                raise RuntimeError("baseline boom")
            # Probe call returns a same-titled source.
            return [Source(id="pre_existing", title="report.pdf")]

        async def _rpc_call(*_a: Any, **_k: Any) -> Any:
            raise NetworkError("transport down")

        with pytest.raises(SourceAddError, match="baseline snapshot was unavailable"):
            await pipeline.register_file_source(
                "nb_1",
                "report.pdf",
                list_sources=_list,
                logger=logger,
                rpc_call=_rpc_call,
            )
        logger.debug.assert_called()

    @pytest.mark.asyncio
    async def test_probe_returns_none_when_no_match(self) -> None:
        """The probe returns None when no same-titled new source exists (line 828).

        Baseline succeeds (empty), the create RPC fails transiently so the
        probe runs, finds nothing, and idempotent_create exhausts retries and
        re-raises the transport error.
        """
        pipeline = _make_pipeline()

        async def _list(_nb: str) -> list[Source]:
            return []

        async def _rpc_call(*_a: Any, **_k: Any) -> Any:
            raise NetworkError("still down")

        with pytest.raises(NetworkError):
            await pipeline.register_file_source(
                "nb_1",
                "report.pdf",
                list_sources=_list,
                logger=MagicMock(),
                rpc_call=_rpc_call,
            )

    @pytest.mark.asyncio
    async def test_missing_id_recovered_by_probe(self) -> None:
        """A successful create with an untrustworthy id is recovered via probe (873, 890).

        The create RPC returns a shape with no trustworthy SOURCE_ID, so
        ``_create`` runs the probe which finds a freshly committed (not in
        baseline) source and returns its id.
        """
        pipeline = _make_pipeline()
        logger = MagicMock()

        list_calls = {"n": 0}

        async def _list(_nb: str) -> list[Source]:
            list_calls["n"] += 1
            if list_calls["n"] == 1:
                return []  # baseline: empty
            return [Source(id="fresh_src", title="report.pdf")]

        async def _rpc_call(*_a: Any, **_k: Any) -> Any:
            # Numeric-only response: no trustworthy SOURCE_ID extractable.
            return [[[1, 2, 3]]]

        result = await pipeline.register_file_source(
            "nb_1",
            "report.pdf",
            list_sources=_list,
            logger=logger,
            rpc_call=_rpc_call,
        )
        assert result == "fresh_src"
        # The "probe found a freshly committed source" info line fired.
        assert logger.info.called

    @pytest.mark.asyncio
    async def test_missing_id_probe_transport_failure_wrapped(self) -> None:
        """A probe transport failure after a successful create wraps to SourceAddError (around 876-889).

        The create RPC succeeds (no usable id), then the probe list() raises a
        transport error. Because the create already committed, this must NOT be
        re-POSTed; it is wrapped into SourceAddError.
        """
        pipeline = _make_pipeline()

        list_calls = {"n": 0}

        async def _list(_nb: str) -> list[Source]:
            list_calls["n"] += 1
            if list_calls["n"] == 1:
                return []  # baseline ok
            raise AuthError("probe auth failed")

        async def _rpc_call(*_a: Any, **_k: Any) -> Any:
            return [[[1, 2, 3]]]  # no usable id

        with pytest.raises(SourceAddError, match="did not provide a trustworthy"):
            await pipeline.register_file_source(
                "nb_1",
                "report.pdf",
                list_sources=_list,
                logger=MagicMock(),
                rpc_call=_rpc_call,
            )

    @pytest.mark.asyncio
    async def test_probe_multiple_new_matches_raises_ambiguity(self) -> None:
        """The probe raising on >1 new same-titled source surfaces ambiguity (line 819).

        Baseline is empty, the create RPC fails transiently so the probe runs;
        the probe then sees two new sources sharing the filename and raises
        SourceAddError rather than guessing.
        """
        pipeline = _make_pipeline()

        list_calls = {"n": 0}

        async def _list(_nb: str) -> list[Source]:
            list_calls["n"] += 1
            if list_calls["n"] == 1:
                return []  # baseline empty
            return [
                Source(id="new_a", title="report.pdf"),
                Source(id="new_b", title="report.pdf"),
            ]

        async def _rpc_call(*_a: Any, **_k: Any) -> Any:
            raise NetworkError("transport down")

        with pytest.raises(SourceAddError, match="probe found 2 new sources"):
            await pipeline.register_file_source(
                "nb_1",
                "report.pdf",
                list_sources=_list,
                logger=MagicMock(),
                rpc_call=_rpc_call,
            )

    @pytest.mark.asyncio
    async def test_missing_id_probe_ambiguity_propagates(self) -> None:
        """A SourceAddError raised by the post-create probe propagates (line 875).

        The create RPC succeeds with no usable id, then the probe finds two
        new same-titled sources and raises SourceAddError; ``_create`` must
        re-raise it unchanged rather than wrap it as a transport failure.
        """
        pipeline = _make_pipeline()

        list_calls = {"n": 0}

        async def _list(_nb: str) -> list[Source]:
            list_calls["n"] += 1
            if list_calls["n"] == 1:
                return []  # baseline empty
            return [
                Source(id="new_a", title="report.pdf"),
                Source(id="new_b", title="report.pdf"),
            ]

        async def _rpc_call(*_a: Any, **_k: Any) -> Any:
            return [[[1, 2, 3]]]  # no usable id -> triggers probe

        with pytest.raises(SourceAddError, match="probe found 2 new sources"):
            await pipeline.register_file_source(
                "nb_1",
                "report.pdf",
                list_sources=_list,
                logger=MagicMock(),
                rpc_call=_rpc_call,
            )

    @pytest.mark.asyncio
    async def test_rpc_error_with_invalid_argument_code_adds_limit_hint(self) -> None:
        """An RPCError with code 3 attaches a source-limit hint (845-849)."""
        pipeline = _make_pipeline()

        async def _list(_nb: str) -> list[Source]:
            return [Source(id=f"s{i}", title=f"f{i}.pdf") for i in range(60)]

        rpc_err = RPCError("invalid argument")
        rpc_err.rpc_code = 3  # type: ignore[attr-defined]

        async def _rpc_call(*_a: Any, **_k: Any) -> Any:
            raise rpc_err

        with pytest.raises(SourceAddError) as exc_info:
            await pipeline.register_file_source(
                "nb_1",
                "report.pdf",
                list_sources=_list,
                logger=MagicMock(),
                rpc_call=_rpc_call,
            )
        # The floor-based hint (>= 50 sources, no explicit limit) is appended.
        assert "50/100/300/600" in str(exc_info.value)


# =============================================================================
# upload_file_streaming() — file-object (non-Path) streaming + finalize
# =============================================================================


class TestUploadFileStreamingFileObject:
    """Drive the IO[bytes] branch of file_stream plus progress callbacks."""

    @pytest.mark.asyncio
    async def test_streams_file_object_with_progress(self, tmp_path) -> None:
        """A file-object source streams chunks and reports progress (1062-1063, 1086-1091)."""
        data = b"hello world payload"
        src = tmp_path / "payload.bin"
        src.write_bytes(data)
        file_obj = open(src, "rb")  # noqa: SIM115

        progress: list[tuple[int, int]] = []

        def _on_progress(done: int, total: int) -> None:
            progress.append((done, total))

        captured: dict[str, Any] = {}

        @asynccontextmanager
        async def _factory_cm(**_kwargs: Any):
            client = AsyncMock()

            async def _post(url: str, headers: dict[str, str], content: Any) -> Any:
                captured["headers"] = headers
                chunks = [chunk async for chunk in content]
                captured["body"] = b"".join(chunks)
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                return resp

            client.post = _post
            yield client

        factory = MagicMock(side_effect=lambda **kw: _factory_cm(**kw))
        pipeline = _make_pipeline(async_client_factory=factory)

        upload_url = "https://notebooklm.google.com/upload/_/?upload_id=session"
        try:
            await pipeline.upload_file_streaming(
                upload_url,
                file_obj,
                filename="payload.bin",
                on_progress=_on_progress,
                total_bytes=len(data),
            )
        finally:
            if not file_obj.closed:
                file_obj.close()

        assert captured["body"] == data
        # Initial 0-progress callback plus at least one chunk callback.
        assert progress[0] == (0, len(data))
        assert progress[-1] == (len(data), len(data))
        # Finalize headers are present.
        assert captured["headers"]["x-goog-upload-command"] == "upload, finalize"
        # The done-callback should have closed the caller's FD.
        assert file_obj.closed

    @pytest.mark.asyncio
    async def test_streams_file_object_without_progress(self, tmp_path) -> None:
        """A file-object source streams without a progress callback (branch 1089->1091)."""
        data = b"payload-no-progress"
        src = tmp_path / "payload.bin"
        src.write_bytes(data)
        file_obj = open(src, "rb")  # noqa: SIM115

        captured: dict[str, Any] = {}

        @asynccontextmanager
        async def _factory_cm(**_kwargs: Any):
            client = AsyncMock()

            async def _post(url: str, headers: dict[str, str], content: Any) -> Any:
                chunks = [chunk async for chunk in content]
                captured["body"] = b"".join(chunks)
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                return resp

            client.post = _post
            yield client

        factory = MagicMock(side_effect=lambda **kw: _factory_cm(**kw))
        pipeline = _make_pipeline(async_client_factory=factory)
        try:
            await pipeline.upload_file_streaming(
                "https://notebooklm.google.com/upload/_/?upload_id=session",
                file_obj,
                filename="payload.bin",
                total_bytes=len(data),
            )
        finally:
            if not file_obj.closed:
                file_obj.close()
        assert captured["body"] == data

    @pytest.mark.asyncio
    async def test_pre_wire_exception_closes_file_object(self, tmp_path) -> None:
        """An exception before the finalize task is wired closes the caller FD (1143-1146).

        We pass an invalid upload URL so ``_validate_resumable_upload_url``
        raises before ``close_wired`` is set. The except-handler must close the
        caller-supplied file object.
        """
        src = tmp_path / "payload.bin"
        src.write_bytes(b"data")
        file_obj = open(src, "rb")  # noqa: SIM115

        pipeline = _make_pipeline()
        with pytest.raises(ValidationError):
            await pipeline.upload_file_streaming(
                "http://insecure.example.com/?upload_id=x",  # not https -> validation fails
                file_obj,
                filename="payload.bin",
            )
        assert file_obj.closed
