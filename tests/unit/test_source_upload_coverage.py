"""Targeted coverage tests for ``notebooklm._web.sources.upload``.
These tests exercise the error handlers, edge-case branches, and
streaming/finalize paths in the upload pipeline that the existing
``test_sources_upload.py`` / ``test_source_upload_pipeline.py`` suites do
not reach. They directly drive the module-level helper functions plus the
``SourceUploadPipeline`` collaborator slots so the assertions reflect real
behaviour rather than tautologies.
"""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import SplitResult, urlsplit

import httpx
import pytest

import notebooklm._web.sources._upload_decode as _upload_decode_mod
import notebooklm._web.sources.drive_import as drive_import_mod
from notebooklm._app.errors import ErrorCategory, classify
from notebooklm._app.source_batch import batch_item_is_fatal
from notebooklm._curl_cffi_transport import CurlCffiAsyncClient
from notebooklm._sources import SourcesAPI
from notebooklm._types.enums import SourceStatus
from notebooklm._web.sources.upload import (
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
    module_logger,
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
    """Non-http(s) schemes have no implicit default port ."""
    assert _default_port_for_scheme("https") == 443
    assert _default_port_for_scheme("http") == 80
    assert _default_port_for_scheme("ftp") is None


def test_redacted_upload_authority_returns_none_when_host_missing() -> None:
    """A URL with no hostname yields ``None`` authority ."""
    parsed = urlsplit("file:///local/path")
    assert parsed.hostname is None
    assert _redacted_upload_authority(parsed) is None


def test_redacted_upload_authority_brackets_ipv6_host() -> None:
    """IPv6 hosts are wrapped in brackets and keep the port suffix."""
    parsed = urlsplit("https://[2001:db8::1]:8443/upload")
    assert _redacted_upload_authority(parsed) == "[2001:db8::1]:8443"


def test_redact_upload_url_returns_placeholder_when_scheme_missing() -> None:
    """A scheme-less / authority-less URL redacts to the placeholder ."""
    assert _redact_upload_url("not-a-url") == "[REDACTED_UPLOAD_URL]"
    assert _redact_upload_url("///just/a/path") == "[REDACTED_UPLOAD_URL]"


def test_redact_upload_url_value_error_returns_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A urlsplit ValueError is swallowed into the redacted placeholder."""
    # ADR-0007 forbids string-target ``mock.patch`` on notebooklm internals;
    # patch the ``urlsplit`` seam object-form on ``_upload_decode`` (where the
    # helper now resolves it after the extraction).
    fake_urlsplit = MagicMock(side_effect=ValueError("bad url"))
    monkeypatch.setattr(_upload_decode_mod, "urlsplit", fake_urlsplit)
    assert _redact_upload_url("https://example.com/x") == "[REDACTED_UPLOAD_URL]"
    fake_urlsplit.assert_called_once_with("https://example.com/x")


def test_validate_resumable_upload_url_value_error_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A urlsplit ValueError becomes a ValidationError ."""

    def _boom(_url: str) -> SplitResult:
        raise ValueError("malformed")

    # ADR-0007 forbids string-target ``mock.patch`` on notebooklm internals;
    # patch the ``urlsplit`` seam object-form on ``_upload_decode`` (where the
    # helper now resolves it after the extraction).
    fake_urlsplit = MagicMock(side_effect=_boom)
    monkeypatch.setattr(_upload_decode_mod, "urlsplit", fake_urlsplit)
    with pytest.raises(ValidationError, match="Upload URL is not valid"):
        _validate_resumable_upload_url("https://example.com/?upload_id=x")
    fake_urlsplit.assert_called_once_with("https://example.com/?upload_id=x")


def test_validate_resumable_upload_url_missing_host_raises() -> None:
    """An https URL with no host is rejected .
    ``https:///path`` parses with scheme ``https`` but ``hostname is None``,
    so it reaches the host-missing guard rather than the scheme guard.
    """
    with pytest.raises(ValidationError, match="must include a host"):
        _validate_resumable_upload_url("https:///upload/_/?upload_id=session")


def test_validate_resumable_upload_url_rejects_explicit_port_zero() -> None:
    """An explicit ``:0`` is a stated port, not an absent one.

    ``urlsplit`` returns the *int* ``0`` for ``https://host:0/``, and ``0`` is
    falsy — so ``parsed.port or _default_port_for_scheme(...)`` silently folded
    it into 443 and the URL passed the port check. A server-supplied
    ``X-Goog-Upload-URL`` could therefore name a port the caller never agreed
    to and still be trusted. Absent and explicit ports must stay distinct.
    """
    with pytest.raises(ValidationError, match="host is not trusted"):
        _validate_resumable_upload_url(
            "https://notebooklm.google.com:0/upload/_/?upload_id=session"
        )


def test_validate_resumable_upload_url_accepts_explicit_default_port() -> None:
    """The port-0 fix must not reject an explicitly stated ``:443``."""
    url = "https://notebooklm.google.com:443/upload/_/?upload_id=session"
    assert _validate_resumable_upload_url(url) == url


def test_register_response_shape_label_all_branches() -> None:
    """Every shape label branch is exercised ."""
    assert _register_response_shape_label({"a": 1}) == "object"
    assert _register_response_shape_label([1, 2]) == "array"
    assert _register_response_shape_label("hi") == "string"
    assert _register_response_shape_label(None) == "null"
    assert _register_response_shape_label(123) == "int"


def test_looks_like_id_string_rejects_whitespace_and_slash() -> None:
    """Candidates containing space/tab/slash are not id-like ."""
    assert _looks_like_id_string("has space1") is False
    assert _looks_like_id_string("has\ttab1") is False
    assert _looks_like_id_string("path/to/1") is False
    # Sanity: a plausible id still passes.
    assert _looks_like_id_string("src_1234") is True


def test_coerce_source_id_candidate_rejects_overlong_string() -> None:
    """Strings longer than 1000 chars are rejected outright ."""
    assert _coerce_source_id_candidate("x" * 1001, "f.pdf") is None


def test_coerce_source_id_candidate_rejects_filename_echo() -> None:
    """A value equal to the filename is rejected ."""
    assert _coerce_source_id_candidate("report.pdf", "report.pdf") is None
    # Empty after strip is also rejected.
    assert _coerce_source_id_candidate("   ", "report.pdf") is None


def test_resolve_upload_content_type_blank_mime_raises() -> None:
    """A whitespace-only explicit mime_type is rejected ."""
    from pathlib import Path

    with pytest.raises(ValidationError, match="cannot be empty or whitespace-only"):
        _resolve_upload_content_type(Path("a.bin"), "   ")


def test_resolve_upload_content_type_md_uses_markdown_when_mimetypes_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``.md`` resolves to ``text/markdown`` even when ``mimetypes`` misses.

    Python 3.10 and hosts without a system MIME table return ``None`` for
    ``.md``; the pinned override prevents a fall back to
    ``application/octet-stream``, which NotebookLM cannot infer a parser for
    (processing then fails server-side with status=ERROR).
    """
    import mimetypes
    from pathlib import Path

    monkeypatch.setattr(mimetypes, "guess_type", lambda _name: (None, None))
    assert _resolve_upload_content_type(Path("notes.md"), None) == "text/markdown"
    assert _resolve_upload_content_type(Path("NOTES.MARKDOWN"), None) == "text/markdown"


def test_resolve_upload_content_type_unknown_suffix_falls_back_to_octet_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A suffix with no MIME guess and no override still falls back to octet-stream."""
    import mimetypes
    from pathlib import Path

    monkeypatch.setattr(mimetypes, "guess_type", lambda _name: (None, None))
    assert _resolve_upload_content_type(Path("blob.unknownext"), None) == "application/octet-stream"


def test_resolve_upload_content_type_override_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pinned override sits last in precedence: explicit mime > guess > override.

    The override only fills the gap when ``mimetypes`` misses (#1627); it must not
    shadow an explicit mime_type or a successful guess, and it keys on the final
    suffix only (so ``notes.md.txt`` is not mistaken for markdown).
    """
    import mimetypes
    from pathlib import Path

    # Explicit mime_type wins over the pinned ``.md`` override.
    assert _resolve_upload_content_type(Path("notes.md"), "text/plain") == "text/plain"
    # A successful guess wins over the override (sentinel proves the guess path ran).
    monkeypatch.setattr(mimetypes, "guess_type", lambda _name: ("application/x-sentinel", None))
    assert _resolve_upload_content_type(Path("notes.md"), None) == "application/x-sentinel"
    # On a miss, the override keys on the actual suffix: ``.md.txt`` -> ``.txt`` -> octet-stream.
    monkeypatch.setattr(mimetypes, "guess_type", lambda _name: (None, None))
    assert _resolve_upload_content_type(Path("notes.md.txt"), None) == "application/octet-stream"


def test_extract_register_file_source_id_ambiguous_field_candidates() -> None:
    """Two distinct context-matched SOURCE_IDs are ambiguous -> None .
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
    """Two distinct contextual row SOURCE_IDs are ambiguous -> None .
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
    """Dict keys that are not strings are skipped during the walk ."""
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
        # No count and no usable limit -> empty hint .
        assert hint == ""
        logger.debug.assert_called_once()

    @pytest.mark.asyncio
    async def test_nonpositive_limit_coerced_to_none(self) -> None:
        """A non-positive limit is treated as unavailable ."""

        async def _zero() -> int | None:
            return 0

        # count below floor + no usable limit -> empty .
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
        """count < limit yields the 'below the advertised limit' hint ."""

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
class _Supervisor:
    def __init__(self) -> None:
        self.asserted = 0

    def assert_bound_loop(self) -> None:
        self.asserted += 1

    def operation_scope(self, _label: str):
        @asynccontextmanager
        async def scope() -> AsyncIterator[SimpleNamespace]:
            yield SimpleNamespace(epoch=1)

        return scope()

    async def spawn_child(self, label: str, factory: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(factory(), name=label)


class _Kernel:
    def __init__(self) -> None:
        self.jar = httpx.Cookies()
        self.get_http_client = MagicMock(return_value=SimpleNamespace(cookies=self.jar))


def _make_pipeline(
    *,
    kernel: Any | None = None,
    rpc: Any | None = None,
    supervisor: Any | None = None,
    async_client_factory: Any | None = None,
    get_source_limit: Any | None = None,
    auth: Any | None = None,
    record_upload_queue_wait: Any | None = None,
) -> SourceUploadPipeline:
    if auth is None:
        auth = MagicMock()
        auth.authuser = 0
        auth.account_email = None
    pipeline = SourceUploadPipeline(
        rpc=rpc or MagicMock(),
        supervisor=supervisor or _Supervisor(),
        kernel=kernel if kernel is not None else _Kernel(),
        auth=auth,
        async_client_factory=async_client_factory,
        get_source_limit=get_source_limit,
        record_upload_queue_wait=record_upload_queue_wait,
    )
    pipeline._active_epoch = 1
    pipeline._closing = False
    pipeline._registry_lock = asyncio.Lock()
    return pipeline


def test_live_cookies_uses_get_http_client_when_kernel_lacks_cookies() -> None:
    """The active upload epoch is forwarded to the kernel cookie owner."""

    class KernelWithHttpClient:
        # No ``cookies`` attribute on purpose.
        def __init__(self) -> None:
            jar = httpx.Cookies()
            jar.set("x", "y", domain="example.com")
            self._jar = jar
            self.get_http_client = MagicMock(return_value=MagicMock(cookies=jar))

    kernel = KernelWithHttpClient()
    pipeline = _make_pipeline(kernel=kernel)
    cookies = pipeline._live_cookies(1)
    assert cookies is kernel._jar
    kernel.get_http_client.assert_called_once_with(expected_epoch=1)


def test_live_cookies_rejects_a_retired_epoch_before_reading_kernel() -> None:
    """A stale workflow cannot read cookies from the replacement transport."""
    kernel = _Kernel()
    pipeline = _make_pipeline(kernel=kernel)

    with pytest.raises(RuntimeError, match="upload generation is retired"):
        pipeline._live_cookies(2)

    kernel.get_http_client.assert_not_called()


def test_live_cookies_rejects_after_transport_is_fenced() -> None:
    """Close fencing prevents cookie reads even when the epoch scalar matches."""
    kernel = _Kernel()
    pipeline = _make_pipeline(kernel=kernel)
    pipeline._closing = True

    with pytest.raises(RuntimeError, match="upload generation is retired"):
        pipeline._live_cookies(1)

    kernel.get_http_client.assert_not_called()


@pytest.mark.asyncio
async def test_list_sources_delegates_to_lister() -> None:
    """``list_sources`` proxies to the internal SourceLister ."""
    pipeline = _make_pipeline()
    expected = [Source(id="s1", title="a.pdf")]
    pipeline._lister.list = AsyncMock(return_value=expected)  # type: ignore[method-assign]
    result = await pipeline.list_sources("nb_1")
    assert result == expected
    pipeline._lister.list.assert_awaited_once_with("nb_1")


@pytest.mark.asyncio
async def test_add_file_asserts_bound_loop_before_work(tmp_path) -> None:
    """``add_file`` calls assert_bound_loop before touching the semaphore (605-607)."""
    supervisor = _Supervisor()
    auth = MagicMock()
    auth.authuser = 0
    auth.account_email = None
    pipeline = SourceUploadPipeline(
        rpc=MagicMock(),
        supervisor=supervisor,
        kernel=MagicMock(),
        auth=auth,
    )
    # Make assert_bound_loop the thing that fails so we prove it runs first,
    # before any filesystem resolution or semaphore allocation.
    supervisor.assert_bound_loop = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("wrong loop")
    )
    with pytest.raises(RuntimeError, match="wrong loop"):
        await pipeline.add_file(
            "nb_1",
            str(tmp_path / "missing.pdf"),
            finalize_uploaded=SourcesAPI._finalize_uploaded_file,
        )
    supervisor.assert_bound_loop.assert_called_once()


def test_get_download_semaphore_asserts_bound_loop_before_building(tmp_path) -> None:
    """``get_download_semaphore`` asserts loop ownership before building the primitive.

    This is the Drive auto-route download seam (#1884): a cross-loop
    ``add_drive_file`` must fail before it can acquire the lazy semaphore on the
    wrong loop (or start a fetch), mirroring ``add_file``'s upload-seam guard.
    """
    supervisor = _Supervisor()
    auth = MagicMock()
    auth.authuser = 0
    auth.account_email = None
    pipeline = SourceUploadPipeline(
        rpc=MagicMock(), supervisor=supervisor, kernel=MagicMock(), auth=auth
    )
    supervisor.assert_bound_loop = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("wrong loop")
    )
    with pytest.raises(RuntimeError, match="wrong loop"):
        pipeline.get_download_semaphore()
    supervisor.assert_bound_loop.assert_called_once()
    # The primitive was NOT built — the guard fired first.
    assert pipeline._download_semaphore is None


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

        with pytest.raises(SourceAddError, match="pre-create baseline snapshot failed") as exc_info:
            await pipeline.register_file_source(
                "nb_1",
                "report.pdf",
                list_sources=_list,
                logger=logger,
                rpc_call=_rpc_call,
            )
        # The marker is what keeps an unresolved create out of the non-fatal,
        # per-item SOURCE_ADD bucket (#2220). Asserted here rather than only on
        # the message, because message-only assertions are exactly why the
        # sibling gap survived two review rounds.
        assert getattr(exc_info.value, "unconfirmed", False) is True
        assert classify(exc_info.value).category is ErrorCategory.RPC
        assert classify(exc_info.value).retriable is False
        # Parity with add_url/add_drive: the baseline's own failure is retained
        # as the cause and named in the message, because the caller reads
        # "baseline snapshot failed" long after that read happened.
        assert isinstance(exc_info.value.cause, RuntimeError)
        assert "RuntimeError" in str(exc_info.value)
        # WARNING, not DEBUG (#2220): the ``notebooklm`` logger defaults to
        # WARNING, so the old DEBUG record never reached a handler and the
        # degraded baseline was invisible.
        logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_probe_returns_none_when_no_match(self) -> None:
        """The probe returns None when no same-titled new source exists .
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
    async def test_missing_id_probe_decode_failure_is_unconfirmed(self) -> None:
        """The probe's *other* call site also refuses to guess (#2220).

        ``_create`` calls ``_probe()`` directly when the register RPC returns
        200 but carries no trustworthy SOURCE_ID — there, the probe is the only
        way to learn the id at all. A decode failure there used to fall through
        to "probe found no unambiguous new source", which reads as *"nothing was
        created"*; in fact the register RPC succeeded and a row may well exist.
        Only ONE register may be issued, and the error must be marked
        unconfirmed so adapters do not advertise a retry.
        """
        pipeline = _make_pipeline()
        rpc_calls = {"n": 0}
        list_calls = {"n": 0}

        async def _list(_nb: str) -> list[Source]:
            list_calls["n"] += 1
            if list_calls["n"] == 1:
                return []  # baseline ok
            raise RPCError("probe decode failed")

        async def _rpc_call(*_a: Any, **_k: Any) -> Any:
            rpc_calls["n"] += 1
            return [[[1, 2, 3]]]  # 200, but no usable id

        with pytest.raises(SourceAddError, match="Cannot confirm file source") as exc_info:
            await pipeline.register_file_source(
                "nb_1",
                "report.pdf",
                list_sources=_list,
                logger=MagicMock(),
                rpc_call=_rpc_call,
            )

        assert rpc_calls["n"] == 1, "the register must not be re-issued"
        assert getattr(exc_info.value, "unconfirmed", False) is True
        # The wording must not claim the registration failed — it returned 200.
        assert "may or may not have committed" in str(exc_info.value)

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

        with pytest.raises(SourceAddError, match="did not provide a trustworthy") as exc_info:
            await pipeline.register_file_source(
                "nb_1",
                "report.pdf",
                list_sources=_list,
                logger=MagicMock(),
                rpc_call=_rpc_call,
            )

        # The register RPC already returned 200, so a row may exist and this
        # probe could not check — an unconfirmed create (#2220). The marker has
        # to survive the wrap: ``_probe`` marks the AuthError it re-raises, but
        # this handler builds a NEW SourceAddError, and the marker lives on the
        # object that propagates, not on its ``cause``. Asserting only the
        # message (as this test used to) is why the gap went unnoticed.
        assert getattr(exc_info.value, "unconfirmed", False) is True
        classified = classify(exc_info.value)
        assert classified.category is ErrorCategory.RPC
        assert classified.retriable is False
        assert batch_item_is_fatal(exc_info.value) is True

    @pytest.mark.asyncio
    async def test_probe_multiple_new_matches_raises_ambiguity(self) -> None:
        """The probe raising on >1 new same-titled source surfaces ambiguity .
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

        with pytest.raises(SourceAddError, match="probe found 2 new sources") as exc_info:
            await pipeline.register_file_source(
                "nb_1",
                "report.pdf",
                list_sources=_list,
                logger=MagicMock(),
                rpc_call=_rpc_call,
            )

        # An ambiguity is an unconfirmed create (#2220): nothing threw, but the
        # server may hold a row either way. The marker keeps it out of the
        # non-fatal per-item SOURCE_ADD bucket a batch add would continue past.
        assert getattr(exc_info.value, "unconfirmed", False) is True
        assert classify(exc_info.value).category is ErrorCategory.RPC
        assert classify(exc_info.value).retriable is False

    @pytest.mark.asyncio
    async def test_missing_id_probe_ambiguity_propagates(self) -> None:
        """A SourceAddError raised by the post-create probe propagates .
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

        with pytest.raises(SourceAddError, match="probe found 2 new sources") as exc_info:
            await pipeline.register_file_source(
                "nb_1",
                "report.pdf",
                list_sources=_list,
                logger=MagicMock(),
                rpc_call=_rpc_call,
            )

        # An ambiguity is an unconfirmed create (#2220): nothing threw, but the
        # server may hold a row either way. The marker keeps it out of the
        # non-fatal per-item SOURCE_ADD bucket a batch add would continue past.
        assert getattr(exc_info.value, "unconfirmed", False) is True
        assert classify(exc_info.value).category is ErrorCategory.RPC
        assert classify(exc_info.value).retriable is False

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

        client = AsyncMock()

        async def _post(url: str, headers: dict[str, str], content: Any) -> Any:
            captured["headers"] = headers
            chunks = [chunk async for chunk in content]
            captured["body"] = b"".join(chunks)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        client.post = _post
        client.__aenter__.return_value = client
        factory = MagicMock(return_value=client)
        pipeline = _make_pipeline(async_client_factory=factory)
        upload_url = "https://notebooklm.google.com/upload/_/?upload_id=session"
        try:
            await pipeline.upload_file_streaming(
                upload_url,
                file_obj,
                filename="payload.bin",
                on_progress=_on_progress,
                total_bytes=len(data),
                expected_epoch=1,
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

        client = AsyncMock()

        async def _post(url: str, headers: dict[str, str], content: Any) -> Any:
            chunks = [chunk async for chunk in content]
            captured["body"] = b"".join(chunks)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        client.post = _post
        client.__aenter__.return_value = client
        factory = MagicMock(return_value=client)
        pipeline = _make_pipeline(async_client_factory=factory)
        try:
            await pipeline.upload_file_streaming(
                "https://notebooklm.google.com/upload/_/?upload_id=session",
                file_obj,
                filename="payload.bin",
                total_bytes=len(data),
                expected_epoch=1,
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
                expected_epoch=1,
            )
        assert file_obj.closed


# =============================================================================
# Transport registry: epoch fencing, snapshots, and teardown settlement
# =============================================================================
def test_live_cookies_public_accessor_hands_out_the_upload_leg_jar() -> None:
    """``live_cookies`` is the seam ``add_drive_file`` authenticates through (#1884).

    It must hand back the *same* jar object the upload leg posts with — a copy
    would go stale the moment keepalive rotation refreshes a cookie, which is
    the whole reason the Drive download does not read the on-disk cookies.
    """
    kernel = _Kernel()
    pipeline = _make_pipeline(kernel=kernel)

    assert pipeline.live_cookies(1) is kernel.jar
    kernel.get_http_client.assert_called_once_with(expected_epoch=1)


def test_live_cookies_public_accessor_enforces_the_same_epoch_fence() -> None:
    """The public accessor must not be a fence bypass for the private one.

    ``add_drive_file`` runs a long server-side download; if the client is closed
    and reopened underneath it, the stale workflow must not read the replacement
    transport's cookies.
    """
    kernel = _Kernel()
    pipeline = _make_pipeline(kernel=kernel)
    pipeline._closing = True

    with pytest.raises(RuntimeError, match="upload generation is retired"):
        pipeline.live_cookies(1)

    kernel.get_http_client.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_transport_resources_reports_nothing_without_a_registry_lock() -> None:
    """A pipeline that never opened (or already closed) has no lock to take.

    Reading the registries without the lock would race a late child
    registration, so the snapshot must report *nothing* rather than an
    unsynchronised view — and must leave the registries themselves untouched
    for ``close_resources``' own ``finally`` to clear.
    """
    pipeline = _make_pipeline()
    client = MagicMock(spec=httpx.AsyncClient)
    pipeline._transport_clients.add(client)
    task = asyncio.create_task(asyncio.sleep(0))
    pipeline._transport_tasks.add(task)
    pipeline._registry_lock = None

    assert await pipeline._snapshot_transport_resources() == ([], [])
    assert pipeline._transport_clients == {client}
    assert pipeline._transport_tasks == {task}

    await task


@pytest.mark.asyncio
async def test_close_resources_raises_the_client_failure_after_clearing_registries() -> None:
    """A failed ``aclose`` surfaces, but only *after* the registries are dropped.

    ``close_resources`` is the partial-open / rollback path: if the raise
    escaped before the ``finally``, a retried close would re-settle handles the
    first attempt already tore down.
    """
    pipeline = _make_pipeline()
    failure = OSError("socket teardown failed")
    client = MagicMock(spec=httpx.AsyncClient)
    client.aclose = AsyncMock(side_effect=failure)
    pipeline._transport_clients.add(client)

    with pytest.raises(OSError) as exc_info:
        await pipeline.close_resources()

    assert exc_info.value is failure
    client.aclose.assert_awaited_once()
    assert pipeline._transport_clients == set()
    assert pipeline._transport_tasks == set()
    assert pipeline._registry_lock is None
    assert pipeline._closing is True
    assert pipeline._active_epoch is None


@pytest.mark.parametrize(
    "exc_type",
    [KeyboardInterrupt, SystemExit, RuntimeError],
    ids=["keyboard-interrupt", "system-exit", "ordinary-failure"],
)
@pytest.mark.asyncio
async def test_close_clients_keeps_the_first_failure_and_still_closes_the_rest(
    exc_type: type[BaseException],
) -> None:
    """One client's teardown failure must not skip a sibling, nor overwrite the first.

    Both slots (``process_exit`` and ``first_failure``) latch the earliest
    value; a later failure of the same kind is discarded so the reported cause
    is the one that actually started the teardown cascade.
    """
    first, second = exc_type("first"), exc_type("second")
    first_client = MagicMock(spec=httpx.AsyncClient)
    first_client.aclose = AsyncMock(side_effect=first)
    second_client = MagicMock(spec=httpx.AsyncClient)
    second_client.aclose = AsyncMock(side_effect=second)

    result = await SourceUploadPipeline._close_clients([first_client, second_client])

    assert result is first
    second_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_clients_prefers_a_late_process_exit_over_an_earlier_failure() -> None:
    """A process-exit signal outranks an ordinary failure regardless of order.

    Returning the ``OSError`` would let ``prepare_close`` swallow a Ctrl-C into
    an ordinary teardown error and keep the interpreter alive.
    """
    ordinary = OSError("ordinary teardown failure")
    interrupt = KeyboardInterrupt("ctrl-c")
    first_client = MagicMock(spec=httpx.AsyncClient)
    first_client.aclose = AsyncMock(side_effect=ordinary)
    second_client = MagicMock(spec=httpx.AsyncClient)
    second_client.aclose = AsyncMock(side_effect=interrupt)

    assert await SourceUploadPipeline._close_clients([first_client, second_client]) is interrupt


def test_begin_transport_operation_rejects_a_caller_with_no_owning_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a current task there is nothing teardown could cancel.

    Admitting the workflow anyway would leave in-flight upload I/O that
    ``prepare_close`` can neither find nor interrupt.
    """
    pipeline = _make_pipeline()
    monkeypatch.setattr(asyncio, "current_task", lambda *_a, **_k: None)

    with pytest.raises(RuntimeError, match="upload transport is not open"):
        pipeline._begin_transport_operation(1)

    assert pipeline._transport_tasks == set()


def test_track_transport_client_refuses_to_publish_without_the_registry_lock() -> None:
    """A client published after the lock is dropped would never be closed.

    ``close_resources`` clears ``_registry_lock`` last; anything registered
    after that point is invisible to every subsequent snapshot, so registration
    must fail loudly instead of leaking the connection.
    """
    pipeline = _make_pipeline()
    pipeline._registry_lock = None
    client = MagicMock(spec=httpx.AsyncClient)

    with pytest.raises(RuntimeError, match="upload transport is not open"):
        pipeline._track_transport_client(client, 1)

    assert client not in pipeline._transport_clients


@pytest.mark.asyncio
async def test_spawn_transport_child_reports_a_missing_owning_task_as_an_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unregisterable child must fail *before* it runs any I/O.

    The child registers itself before its first await precisely so teardown can
    find it; with no owning task to register, the body must never start.
    """
    pipeline = _make_pipeline()
    factory = AsyncMock()
    monkeypatch.setattr(asyncio, "current_task", lambda *_a, **_k: None)

    outcome = await (await pipeline._spawn_transport_child("child", factory, expected_epoch=1))

    assert isinstance(outcome.error, RuntimeError)
    assert "upload child has no owning task" in str(outcome.error)
    factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_spawn_transport_child_reports_a_dropped_registry_lock_as_an_outcome() -> None:
    """A child spawned after the registry is torn down must not start its body.

    The error is returned as an outcome rather than raised, so the parent's
    ``await`` settles normally and the failure travels through the same
    ``outcome.error`` channel as every other child failure.
    """
    pipeline = _make_pipeline()
    pipeline._registry_lock = None
    factory = AsyncMock()

    outcome = await (await pipeline._spawn_transport_child("child", factory, expected_epoch=1))

    assert isinstance(outcome.error, RuntimeError)
    assert "upload transport is not open" in str(outcome.error)
    factory.assert_not_awaited()
    assert pipeline._transport_tasks == set()


# =============================================================================
# Admission slots and account routing
# =============================================================================
@pytest.mark.parametrize(
    "with_recorder",
    [True, False],
    ids=["records-queue-wait", "no-recorder-wired"],
)
@pytest.mark.asyncio
async def test_upload_slot_holds_a_permit_and_records_the_wait_only_when_wired(
    with_recorder: bool,
) -> None:
    """The slot always gates concurrency; the queue-wait metric is optional.

    The recorder is an injected observability hook, so a pipeline built without
    one must still acquire and release the permit rather than skipping the slot.
    """
    waits: list[float] = []
    pipeline = _make_pipeline(record_upload_queue_wait=waits.append if with_recorder else None)
    semaphore = pipeline.get_upload_semaphore()
    free_permits = semaphore._value

    async with pipeline._upload_slot():
        assert semaphore._value == free_permits - 1

    assert semaphore._value == free_permits
    assert len(waits) == (1 if with_recorder else 0)
    if with_recorder:
        # An ELAPSED wait, not an absolute clock reading. ``>= 0.0`` passed
        # either way, so a recorder that reported ``monotonic()`` instead of
        # ``monotonic() - start`` went undetected; an uncontended slot settles
        # in microseconds, so any wall-clock timestamp blows this bound.
        assert 0.0 <= waits[0] < 1.0


def test_get_download_semaphore_is_cached_and_separate_from_the_upload_pool() -> None:
    """The Drive download pool must be its own primitive (#1884).

    ``add_drive_file`` downloads and then calls ``add_file``, which takes an
    *upload* permit; sharing one pool would let a full download pool deadlock
    against the upload slot it is waiting to enter.
    """
    pipeline = _make_pipeline()

    download_semaphore = pipeline.get_download_semaphore()

    assert download_semaphore is pipeline.get_download_semaphore()
    assert download_semaphore is not pipeline.get_upload_semaphore()
    assert download_semaphore._value == pipeline._max_concurrent_uploads


@pytest.mark.parametrize(
    ("authuser", "account_email", "expected"),
    [
        (0, None, "0"),
        (3, None, "3"),
        (3, "person@example.com", "person@example.com"),
        (3, "   ", "3"),
    ],
    ids=["index-zero", "index-only", "email-wins-over-index", "blank-email-falls-back"],
)
def test_authuser_value_matches_the_upload_leg_routing(
    authuser: int, account_email: str | None, expected: str
) -> None:
    """The Drive leg must route to the same account as the upload leg.

    A mismatch serves ``authuser=0``'s view of Drive while registering the
    source against a different account — a silent wrong-file upload.
    """
    pipeline = _make_pipeline(auth=SimpleNamespace(authuser=authuser, account_email=account_email))

    assert pipeline.authuser_value() == expected
    assert pipeline.authuser_value() == pipeline._authuser_header()


# =============================================================================
# drive_download_scope() — the cross-backend Drive fetch seam
# =============================================================================
class _RecordingDriveFetcher:
    """Stand-in for ``DriveFetcher`` that records how the scope wired it up."""

    seen: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        _RecordingDriveFetcher.seen = dict(kwargs)

    async def __call__(self, ref: Any) -> Any:
        _RecordingDriveFetcher.seen["file_id"] = ref.file_id
        _RecordingDriveFetcher.seen["cookies"] = _RecordingDriveFetcher.seen["cookies_provider"]()
        return drive_import_mod.DriveDownload(
            _RecordingDriveFetcher.seen["download_path"], "paper.pdf", "application/pdf"
        )


@pytest.mark.asyncio
async def test_drive_download_scope_routes_the_account_and_unlinks_the_temp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The scope owns routing, live cookies, admission, and temp cleanup.

    Consumers only ever see (path, filename, mime); everything that makes the
    download authentic — the selected account and the post-rotation jar — is
    the scope's job, and the temp file must not outlive it.
    """
    downloaded = tmp_path / "paper.pdf"
    downloaded.write_bytes(b"%PDF-1.4")
    _RecordingDriveFetcher.seen = {"download_path": downloaded}

    def _fetcher(**kwargs: Any) -> _RecordingDriveFetcher:
        kwargs["download_path"] = downloaded
        return _RecordingDriveFetcher(**kwargs)

    monkeypatch.setattr(drive_import_mod, "DriveFetcher", _fetcher)
    kernel = _Kernel()
    pipeline = _make_pipeline(
        kernel=kernel, auth=SimpleNamespace(authuser=4, account_email="person@example.com")
    )
    file_id = "1AbCdEfGhIjKlMnOpQrStUvWxYz01234"

    async with pipeline.drive_download_scope(file_id) as (path, filename, content_type):
        assert (path, filename, content_type) == (downloaded, "paper.pdf", "application/pdf")
        assert path.exists()
        # The download permit is held for the whole scope, not just the fetch.
        assert pipeline.get_download_semaphore()._value == pipeline._max_concurrent_uploads - 1

    assert not downloaded.exists()
    assert pipeline.get_download_semaphore()._value == pipeline._max_concurrent_uploads
    assert _RecordingDriveFetcher.seen["file_id"] == file_id
    assert _RecordingDriveFetcher.seen["authuser"] == "person@example.com"
    assert _RecordingDriveFetcher.seen["cookies"] is kernel.jar


@pytest.mark.asyncio
async def test_drive_download_scope_unlinks_the_temp_file_when_the_body_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A failed upload must not leave the downloaded Drive bytes on disk.

    The temp file is unlinked in a ``finally``; without it every failed
    ``add_drive_file`` would leak a full copy of the user's document.
    """
    downloaded = tmp_path / "paper.pdf"
    downloaded.write_bytes(b"%PDF-1.4")

    def _fetcher(**kwargs: Any) -> _RecordingDriveFetcher:
        kwargs["download_path"] = downloaded
        return _RecordingDriveFetcher(**kwargs)

    monkeypatch.setattr(drive_import_mod, "DriveFetcher", _fetcher)
    pipeline = _make_pipeline()

    with pytest.raises(NetworkError, match="upload leg failed"):
        async with pipeline.drive_download_scope("1AbCdEfGhIjKlMnOpQrStUvWxYz01234"):
            raise NetworkError("upload leg failed")

    assert not downloaded.exists()
    # The permit is released too, or a failed download would leak admission.
    assert pipeline.get_download_semaphore()._value == pipeline._max_concurrent_uploads


# =============================================================================
# add_file() — filesystem resolution and descriptor ownership
# =============================================================================
@pytest.mark.asyncio
async def test_add_file_rejects_a_directory_that_passes_the_pure_mime_gate(tmp_path) -> None:
    """A directory named like a document must be rejected after resolution.

    The pure argument gate only sees the suffix, so ``report.pdf`` as a
    *directory* survives it; the awaited filesystem check is the only thing
    standing between that path and an ``open()`` that would raise ``IsADirectoryError``
    from deep inside the upload slot.
    """
    directory = tmp_path / "report.pdf"
    directory.mkdir()
    pipeline = _make_pipeline()

    with pytest.raises(ValidationError, match="Not a regular file"):
        await pipeline.add_file(
            "nb_1",
            directory,
            finalize_uploaded=SourcesAPI._finalize_uploaded_file,
        )


@pytest.mark.asyncio
async def test_add_file_closes_the_descriptor_when_the_size_probe_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``open`` and ``fstat`` are paired, so a failed ``fstat`` must close the handle.

    They run together on a worker thread; without the ``except BaseException``
    close the descriptor would leak on every stat failure, and nothing later in
    ``_add_file_admitted`` has a reference to close it.
    """
    source_file = tmp_path / "report.pdf"
    source_file.write_bytes(b"%PDF-1.4")
    pipeline = _make_pipeline()
    pipeline.register_file_source = AsyncMock()  # type: ignore[method-assign]
    real_fstat = os.fstat
    probed: dict[str, int] = {}

    def _failing_fstat(fd: int) -> Any:
        probed["fd"] = fd
        raise OSError(5, "simulated fstat failure")

    monkeypatch.setattr(os, "fstat", _failing_fstat)
    try:
        with pytest.raises(OSError, match="simulated fstat failure"):
            await pipeline.add_file(
                "nb_1",
                source_file,
                finalize_uploaded=SourcesAPI._finalize_uploaded_file,
            )
    finally:
        monkeypatch.setattr(os, "fstat", real_fstat)

    # Nothing was registered server-side — the failure is pre-registration.
    pipeline.register_file_source.assert_not_awaited()
    # The descriptor the failing probe was handed is no longer open.
    with pytest.raises(OSError):
        real_fstat(probed["fd"])


# =============================================================================
# register_file_source() — probe with an unavailable baseline
# =============================================================================
@pytest.mark.asyncio
async def test_probe_with_an_unavailable_baseline_is_silent_when_no_title_matches() -> None:
    """An unavailable baseline is only an ambiguity when a same-titled source EXISTS.

    The disambiguation guard must not fire on an empty match list: doing so
    would convert every transport blip on a notebook that has no same-titled
    source into an unresolvable ``SourceAddError`` instead of letting
    ``idempotent_create`` retry the register.
    """
    pipeline = _make_pipeline()
    list_calls = {"n": 0}

    async def _list(_nb: str) -> list[Source]:
        list_calls["n"] += 1
        if list_calls["n"] == 1:
            raise RuntimeError("baseline boom")
        return [Source(id="unrelated", title="something-else.pdf")]

    async def _rpc_call(*_a: Any, **_k: Any) -> Any:
        raise NetworkError("transport down")

    with pytest.raises(NetworkError, match="transport down"):
        await pipeline.register_file_source(
            "nb_1",
            "report.pdf",
            list_sources=_list,
            logger=MagicMock(),
            rpc_call=_rpc_call,
        )

    # The probe really ran (baseline call plus at least one probe call).
    assert list_calls["n"] > 1


# =============================================================================
# Source-lifecycle delegation to the shared lister / poller
# =============================================================================
@pytest.mark.asyncio
async def test_get_source_delegates_to_the_lister_with_the_pipeline_list_seam() -> None:
    """The lister must receive *this* pipeline's ``list_sources``, not its own.

    ``WebSourcesAPI`` swaps in a shared lister; passing the bound seam is what
    keeps one owner for the source-lifecycle verbs instead of two parallel
    listing paths.
    """
    pipeline = _make_pipeline()
    expected = Source(id="s1", title="a.pdf")
    pipeline._lister.get = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    assert await pipeline.get_source("nb_1", "s1") is expected

    pipeline._lister.get.assert_awaited_once_with("nb_1", "s1", list_sources=pipeline.list_sources)


@pytest.mark.parametrize(
    ("method_name", "timeout"),
    [("wait_until_ready", 7.0), ("wait_until_registered", 3.0)],
    ids=["wait-until-ready", "wait-until-registered"],
)
@pytest.mark.asyncio
async def test_wait_verbs_forward_every_polling_knob_and_injected_clock(
    method_name: str, timeout: float
) -> None:
    """Both wait verbs hand the poller its full injected environment.

    The poller owns no clock, sleep, or source getter of its own — dropping any
    of them silently changes the backoff schedule or makes the poll read a
    different notebook's sources.
    """
    from time import monotonic

    pipeline = _make_pipeline()
    expected = Source(id="s1", title="a.pdf", status=SourceStatus.READY)
    delegate = AsyncMock(return_value=expected)
    setattr(pipeline._poller, method_name, delegate)

    result = await getattr(pipeline, method_name)(
        "nb_1",
        "s1",
        timeout=timeout,
        initial_interval=0.25,
        max_interval=2.0,
        backoff_factor=3.0,
        transient_error_types=(1, None),
    )

    assert result is expected
    call = delegate.await_args
    assert call.args == ("nb_1", "s1")
    assert call.kwargs["timeout"] == timeout
    assert call.kwargs["initial_interval"] == 0.25
    assert call.kwargs["max_interval"] == 2.0
    assert call.kwargs["backoff_factor"] == 3.0
    assert call.kwargs["transient_error_types"] == (1, None)
    assert call.kwargs["get_source"] == pipeline.get_source
    assert call.kwargs["sleep"] is asyncio.sleep
    assert call.kwargs["monotonic"] is monotonic
    assert call.kwargs["logger"] is module_logger


# =============================================================================
# upload_file_streaming() — curl_cffi transport, FD ownership, cancellation
# =============================================================================
_UPLOAD_URL = "https://notebooklm.google.com/upload/_/?upload_id=session"


class _FakeCurlClient(CurlCffiAsyncClient):
    """A ``CurlCffiAsyncClient`` by type only — the real ctor needs a curl session.

    ``upload_file_streaming`` selects the low-level path with ``isinstance``
    (not duck-typing, because a mock auto-spawns any attribute), so the fake has
    to genuinely be one.
    """

    def __init__(self, **_kwargs: Any) -> None:
        self.stream_upload_calls: list[dict[str, Any]] = []
        self.post_calls = 0

    async def __aenter__(self) -> _FakeCurlClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def stream_upload(
        self, url: str, source: Any, *, total_bytes: int, headers: Any, **_kwargs: Any
    ) -> httpx.Response:
        self.stream_upload_calls.append(
            {"url": url, "source": source, "total_bytes": total_bytes, "headers": dict(headers)}
        )
        return httpx.Response(200, request=httpx.Request("POST", url))

    async def post(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
        self.post_calls += 1
        raise AssertionError("the curl transport must not buffer through the generator path")


@pytest.mark.parametrize("as_path", [True, False], ids=["path-source", "file-object-source"])
@pytest.mark.parametrize("with_progress", [True, False], ids=["with-progress", "no-progress"])
@pytest.mark.asyncio
async def test_curl_transport_streams_the_body_from_disk_instead_of_the_generator(
    tmp_path, as_path: bool, with_progress: bool
) -> None:
    """The curl backend must be handed the raw handle, never the async generator.

    libcurl streams the request body itself; feeding it the chunk generator
    would defeat the whole point (no full-file buffer) and the generator's
    per-chunk progress never fires, so the completion callback is synthesised
    from ``total_bytes`` instead.
    """
    data = b"curl-streamed-payload"
    source_file = tmp_path / "payload.bin"
    source_file.write_bytes(data)
    file_obj: Any = source_file if as_path else open(source_file, "rb")  # noqa: SIM115
    progress: list[tuple[int, int]] = []
    client = _FakeCurlClient()
    pipeline = _make_pipeline(async_client_factory=MagicMock(return_value=client))

    try:
        await pipeline.upload_file_streaming(
            _UPLOAD_URL,
            file_obj,
            filename="payload.bin",
            on_progress=(lambda done, total: progress.append((done, total)))
            if with_progress
            else None,
            total_bytes=len(data),
            expected_epoch=1,
        )
    finally:
        if not as_path and not file_obj.closed:
            file_obj.close()

    assert client.post_calls == 0
    (call,) = client.stream_upload_calls
    assert call["source"] is file_obj
    assert call["url"] == _UPLOAD_URL
    assert call["total_bytes"] == len(data)
    assert call["headers"]["x-goog-upload-command"] == "upload, finalize"
    assert call["headers"]["x-goog-upload-offset"] == "0"
    if with_progress:
        assert progress == [(0, len(data)), (len(data), len(data))]
    else:
        assert progress == []


class _CloseExplodingFile:
    """A caller-supplied handle whose ``close()`` always fails."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)
        self.close_attempts = 0

    def read(self, size: int) -> bytes:
        return self._buffer.read(size)

    def close(self) -> None:
        self.close_attempts += 1
        raise OSError("descriptor close failed")


@pytest.mark.asyncio
async def test_a_failing_caller_fd_close_is_logged_and_never_fails_the_upload() -> None:
    """A close failure on the caller's handle must not turn a good upload into an error.

    The bytes are already finalised server-side by then; both close attempts —
    the in-task ``finally`` and the idempotent done-callback fallback — are
    best-effort and only get logged.
    """
    data = b"payload-with-a-bad-close"
    file_obj = _CloseExplodingFile(data)
    logger = MagicMock()
    body: dict[str, bytes] = {}

    class _CollectingClient:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, url: str, headers: Any, content: Any) -> httpx.Response:
            body["sent"] = b"".join([chunk async for chunk in content])
            return httpx.Response(200, request=httpx.Request("POST", url))

    pipeline = _make_pipeline(async_client_factory=MagicMock(return_value=_CollectingClient()))

    await pipeline.upload_file_streaming(
        _UPLOAD_URL,
        file_obj,
        filename="payload.bin",
        total_bytes=len(data),
        logger=logger,
        expected_epoch=1,
    )
    await asyncio.sleep(0)  # let the done-callback fallback run

    assert body["sent"] == data
    assert file_obj.close_attempts == 2
    messages = [call.args[0] for call in logger.debug.call_args_list]
    assert any("Caller FD close in finalize failed" in message for message in messages)
    assert any("Caller FD close in finalize-done failed" in message for message in messages)


class _HangingEnterClient:
    """A client whose ``__aenter__`` never returns, so the body never starts."""

    def __init__(self, entered: asyncio.Event) -> None:
        self._entered = entered

    async def __aenter__(self) -> Any:
        self._entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover - the wait never returns

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _RecordingSupervisor(_Supervisor):
    """A supervisor that keeps every spawned child addressable by its label prefix."""

    def __init__(self) -> None:
        super().__init__()
        self.children: dict[str, asyncio.Task[Any]] = {}

    async def spawn_child(self, label: str, factory: Any) -> asyncio.Task[Any]:
        task = await super().spawn_child(label, factory)
        self.children[label.split(":", 1)[0]] = task
        return task


@pytest.mark.asyncio
async def test_cancelling_before_the_body_starts_surfaces_a_failed_scotty_cancel(
    tmp_path,
) -> None:
    """A Scotty cancel that fails must be reported, not silently dropped.

    The session was never finalised, so a swallowed cancel failure would leave
    an orphaned resumable-upload session on Google's side with nothing in the
    caller's traceback to say so.
    """
    entered = asyncio.Event()
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    pipeline = _make_pipeline(
        async_client_factory=MagicMock(return_value=_HangingEnterClient(entered))
    )
    pipeline.cancel_upload_session = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("scotty rejected the cancel")
    )

    task = asyncio.create_task(
        pipeline.upload_file_streaming(_UPLOAD_URL, payload, expected_epoch=1)
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(ValueError, match="scotty rejected the cancel"):
        await task

    pipeline.cancel_upload_session.assert_awaited_once()
    assert pipeline.cancel_upload_session.await_args.kwargs["_expected_epoch"] == 1


@pytest.mark.asyncio
async def test_a_second_cancellation_during_the_scotty_cancel_cancels_that_child_too(
    tmp_path,
) -> None:
    """A caller who cancels again must not leave the cancel child running.

    The original ``CancelledError`` is what the caller sees; the in-flight
    Scotty cancel is torn down and awaited first so close/reopen cannot race a
    surviving child task.
    """
    entered = asyncio.Event()
    cancel_started = asyncio.Event()

    async def _hanging_cancel(*_args: Any, **_kwargs: Any) -> None:
        cancel_started.set()
        await asyncio.Event().wait()

    supervisor = _RecordingSupervisor()
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    pipeline = _make_pipeline(
        supervisor=supervisor,
        async_client_factory=MagicMock(return_value=_HangingEnterClient(entered)),
    )
    pipeline.cancel_upload_session = _hanging_cancel  # type: ignore[method-assign]

    task = asyncio.create_task(
        pipeline.upload_file_streaming(_UPLOAD_URL, payload, expected_epoch=1)
    )
    await entered.wait()
    task.cancel()
    await cancel_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert supervisor.children["upload-finalize"].cancelled()
    assert supervisor.children["upload-cancel"].cancelled()


class _FenceAfterFirstChildSupervisor(_Supervisor):
    """Refuses the second child, as ``CallSupervisor`` does once a close fenced it."""

    def __init__(self) -> None:
        super().__init__()
        self.spawns = 0

    async def spawn_child(self, label: str, factory: Any) -> asyncio.Task[Any]:
        self.spawns += 1
        if self.spawns > 1:
            raise RuntimeError(f"NotebookLMClient is not accepting child work ({label}).")
        return await super().spawn_child(label, factory)


@pytest.mark.asyncio
async def test_a_fenced_generation_skips_the_scotty_cancel_and_keeps_the_cancellation(
    tmp_path,
) -> None:
    """After a forced close, teardown is local-only — no outbound cancel POST.

    The reopened client owns new cookies and a new transport; emitting a Scotty
    cancel against those resources would authenticate the dead generation's
    teardown with the live generation's session. The caller must still see the
    original cancellation, not the supervisor's ``RuntimeError``.
    """
    entered = asyncio.Event()
    supervisor = _FenceAfterFirstChildSupervisor()
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    pipeline = _make_pipeline(
        supervisor=supervisor,
        async_client_factory=MagicMock(return_value=_HangingEnterClient(entered)),
    )
    pipeline.cancel_upload_session = AsyncMock()  # type: ignore[method-assign]

    task = asyncio.create_task(
        pipeline.upload_file_streaming(_UPLOAD_URL, payload, expected_epoch=1)
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert supervisor.spawns == 2
    pipeline.cancel_upload_session.assert_not_awaited()
    # ``raise cancelled from None`` — the fencing RuntimeError is not chained on.
    assert exc_info.value.__cause__ is None


class _BlockingPostClient:
    """Enters cleanly (so ``finalize_started`` flips) then blocks inside ``post``."""

    def __init__(
        self, posting: asyncio.Event, release: asyncio.Event, failure: BaseException | None
    ) -> None:
        self._posting = posting
        self._release = release
        self._failure = failure

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, url: str, headers: Any, content: Any) -> httpx.Response:
        self._posting.set()
        await self._release.wait()
        if self._failure is not None:
            raise self._failure
        return httpx.Response(200, request=httpx.Request("POST", url))


@pytest.mark.asyncio
async def test_cancelling_after_the_body_started_logs_the_late_finalize_failure(
    tmp_path,
) -> None:
    """Once bytes are on the wire the finalize is left to settle, then logged.

    Cancelling mid-body must not emit a Scotty cancel (the session may already
    be finalised), so the pipeline waits the child out. Its failure is
    information only — the caller's ``CancelledError`` still wins.
    """
    posting, release = asyncio.Event(), asyncio.Event()
    failure = NetworkError("finalize rejected after cancellation")
    client = _BlockingPostClient(posting, release, failure)
    logger = MagicMock()
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    pipeline = _make_pipeline(async_client_factory=MagicMock(return_value=client))
    pipeline.cancel_upload_session = AsyncMock()  # type: ignore[method-assign]

    task = asyncio.create_task(
        pipeline.upload_file_streaming(_UPLOAD_URL, payload, logger=logger, expected_epoch=1)
    )
    await posting.wait()
    task.cancel()
    await asyncio.sleep(0)  # let the parent reach the settle-the-child await
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    pipeline.cancel_upload_session.assert_not_awaited()
    late = [
        call
        for call in logger.debug.call_args_list
        if "failed before cancellation propagated" in call.args[0]
    ]
    assert [call.args[1] for call in late] == [failure]


class _WrapperFailsAfterBodySupervisor(_Supervisor):
    """A supervisor whose child wrapper fails *after* the child body settled.

    ``CallSupervisor.spawn_child``'s wrapper re-raises a settlement failure when
    the body itself did not fail, so the task handed back can carry an ordinary
    exception that the child's own ``except BaseException`` never saw.
    """

    async def spawn_child(self, label: str, factory: Any) -> asyncio.Task[Any]:
        async def _wrapper() -> Any:
            await factory()
            raise RuntimeError("child settlement failed")

        return asyncio.create_task(_wrapper(), name=label)


@pytest.mark.asyncio
async def test_a_child_task_failing_outside_its_own_catch_still_loses_to_the_cancellation(
    tmp_path,
) -> None:
    """A finalize task that raises while being settled is logged, never re-raised.

    Replacing the caller's ``CancelledError`` with a teardown-time failure would
    make a cancelled upload look like a transport error and invite a retry of
    bytes that may already have landed.
    """
    posting, release = asyncio.Event(), asyncio.Event()
    client = _BlockingPostClient(posting, release, None)
    logger = MagicMock()
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    pipeline = _make_pipeline(
        supervisor=_WrapperFailsAfterBodySupervisor(),
        async_client_factory=MagicMock(return_value=client),
    )
    # The done-callback reads the same failed result; keep it off stderr.
    # The handler is process-wide loop state, so it is saved and restored in a
    # ``finally``: leaving it installed means pytest-asyncio's own teardown
    # appends any later unhandled task exception to ``loop_errors`` after this
    # test's assertions have run, silently discarding it.
    loop_errors: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        task = asyncio.create_task(
            pipeline.upload_file_streaming(_UPLOAD_URL, payload, logger=logger, expected_epoch=1)
        )
        await posting.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        logged = [
            call.args[1]
            for call in logger.debug.call_args_list
            if "failed before cancellation propagated" in call.args[0]
        ]
        assert len(logged) == 1
        assert isinstance(logged[0], RuntimeError)
        assert str(logged[0]) == "child settlement failed"
    finally:
        loop.set_exception_handler(previous_handler)


class _HangingSecondSpawnSupervisor(_Supervisor):
    """Blocks the second ``spawn_child`` so a cancellation lands before a task exists."""

    def __init__(self, spawning: asyncio.Event) -> None:
        super().__init__()
        self._spawning = spawning
        self.spawns = 0

    async def spawn_child(self, label: str, factory: Any) -> asyncio.Task[Any]:
        self.spawns += 1
        if self.spawns > 1:
            self._spawning.set()
            await asyncio.Event().wait()
        return await super().spawn_child(label, factory)


@pytest.mark.asyncio
async def test_a_cancellation_while_the_cancel_child_is_being_admitted_has_nothing_to_tear_down(
    tmp_path,
) -> None:
    """Cancelling inside ``spawn_child`` leaves no cancel task to cancel.

    Admission can block (the supervisor gates children behind a drain
    condition), so the second cancellation can arrive before ``cancel_task`` is
    ever bound — the handler must not trip over the ``None`` on its way to
    re-raising the original cancellation.
    """
    entered = asyncio.Event()
    spawning = asyncio.Event()
    supervisor = _HangingSecondSpawnSupervisor(spawning)
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    pipeline = _make_pipeline(
        supervisor=supervisor,
        async_client_factory=MagicMock(return_value=_HangingEnterClient(entered)),
    )
    pipeline.cancel_upload_session = AsyncMock()  # type: ignore[method-assign]

    task = asyncio.create_task(
        pipeline.upload_file_streaming(_UPLOAD_URL, payload, expected_epoch=1)
    )
    await entered.wait()
    task.cancel()
    await spawning.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert supervisor.spawns == 2
    pipeline.cancel_upload_session.assert_not_awaited()
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_cancelling_after_the_body_started_settles_a_successful_finalize_quietly(
    tmp_path,
) -> None:
    """A finalize that succeeds after the cancellation is not a failure to report.

    The upload really did land, so the only correct outcome is the caller's
    ``CancelledError`` with no misleading "finalize failed" record next to it.
    """
    posting, release = asyncio.Event(), asyncio.Event()
    supervisor = _RecordingSupervisor()
    logger = MagicMock()
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    pipeline = _make_pipeline(
        supervisor=supervisor,
        async_client_factory=MagicMock(return_value=_BlockingPostClient(posting, release, None)),
    )
    pipeline.cancel_upload_session = AsyncMock()  # type: ignore[method-assign]

    task = asyncio.create_task(
        pipeline.upload_file_streaming(_UPLOAD_URL, payload, logger=logger, expected_epoch=1)
    )
    await posting.wait()
    task.cancel()
    await asyncio.sleep(0)  # let the parent reach the settle-the-child await
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The finalize POST completed cleanly — nothing to log, nothing to cancel.
    assert supervisor.children["upload-finalize"].result().error is None
    pipeline.cancel_upload_session.assert_not_awaited()
    assert not [
        call
        for call in logger.debug.call_args_list
        if "failed before cancellation propagated" in call.args[0]
    ]


@pytest.mark.asyncio
async def test_a_failing_close_on_the_pre_wire_path_never_masks_the_original_error() -> None:
    """The pre-wire fallback close is best-effort; the real rejection must survive.

    Nothing has been wired to close the caller's handle yet, so this branch owns
    it — but letting its ``OSError`` escape would replace the actionable
    ``ValidationError`` with a descriptor-teardown error.
    """
    file_obj = _CloseExplodingFile(b"data")
    logger = MagicMock()
    pipeline = _make_pipeline()

    with pytest.raises(ValidationError, match="Upload URL"):
        await pipeline.upload_file_streaming(
            "http://insecure.example.com/?upload_id=x",  # not https -> rejected pre-wire
            file_obj,
            filename="payload.bin",
            logger=logger,
            expected_epoch=1,
        )

    assert file_obj.close_attempts == 1
    messages = [call.args[0] for call in logger.debug.call_args_list]
    assert any("Caller FD close on pre-wire exception failed" in message for message in messages)
