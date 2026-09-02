"""Unit tests for notebook operations."""

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._web.notebooks import WebNotebooksAPI
from notebooklm._web.params.notebooks import (
    build_copy_notebook_params,
    build_create_notebook_params,
    build_get_notebook_params,
)
from notebooklm._web.sources.listing import SourceLister
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.exceptions import (
    DecodingError,
    NetworkError,
    NotebookLimitError,
    NotebookNotFoundError,
    RateLimitError,
    RPCError,
    ServerError,
    ValidationError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.types import (
    AccountLimits,
    Notebook,
    NotebookMetadata,
    SharePermission,
    Source,
    SourceType,
)


def _make_core(rpc_call: AsyncMock | None = None):
    """Return a fake collaborator core with a pre-wired ``rpc_call``.

    ADR-0007 substrate: built via :func:`make_fake_core` so the resulting
    bag-of-attributes satisfies the capability Protocols without re-
    introducing the forbidden post-construction AsyncMock attribute-
    assignment pattern. Callers that need to control the dispatch
    behaviour pass a pre-built ``rpc_call`` here.
    """
    from tests._fixtures.fake_core import make_fake_core

    return make_fake_core(rpc_call=rpc_call if rpc_call is not None else AsyncMock())


def _make_api(rpc_call: AsyncMock | None = None) -> WebNotebooksAPI:
    core = _make_core(rpc_call)
    return WebNotebooksAPI(core.rpc_executor, sources_api=MagicMock())


def _source_entry(
    source_id: str,
    *,
    title: str = "Source",
    metadata: list[Any] | None = None,
) -> list[Any]:
    return [
        [source_id],
        title,
        metadata or [None, 11, [1704067200, 0], None, 5],
        [None, 2],
    ]


def _owned_notebooks(count: int) -> list[Notebook]:
    return [Notebook(id=f"owned_{i}", title=f"Owned {i}", is_owner=True) for i in range(count)]


def _shared_notebooks(count: int) -> list[Notebook]:
    return [
        Notebook(id=f"shared_{i}", title=f"Shared {i}", role=SharePermission.VIEWER)
        for i in range(count)
    ]


def _owned_but_shared_notebooks(count: int) -> list[Notebook]:
    """Notebooks the account OWNS and has shared with a collaborator.

    Parsed from the live row shape rather than constructed with an explicit
    ``is_owner``: ``meta[0] == 1`` (userRole OWNER) with ``meta[1] is True``
    (the notebook has sharing) is exactly the combination the pre-#2125 decoder
    mis-read as "not owned".
    """
    return [
        Notebook.from_api_response(
            [
                f"Owned & Shared {i}",
                [],
                f"owned_shared_{i}",
                "\U0001f4d3",
                None,
                [1, True, True, None, None, None, 1, False, None],
            ]
        )
        for i in range(count)
    ]


def _create_invalid_argument_error(
    *, method_id: str = RPCMethod.CREATE_NOTEBOOK.value, rpc_code: int = 3
) -> RPCError:
    return RPCError(
        "The server rejected this request (invalid argument).",
        method_id=method_id,
        rpc_code=rpc_code,
    )


def test_build_create_notebook_params_matches_live_payload() -> None:
    # Nested trailing block per the Gemini-3.5 wire-format migration (#1546).
    assert build_create_notebook_params("Daily News") == [
        "Daily News",
        None,
        None,
        [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    ]


def test_build_copy_notebook_params_matches_live_payload() -> None:
    assert build_copy_notebook_params("nb_source", "Copied Notebook") == [
        [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
        "nb_source",
        "Copied Notebook",
    ]


def test_build_get_notebook_params_matches_live_payload() -> None:
    # #1549: the read-path tail also migrated to the nested template block.
    # Live-verified forward-compatible (decoded notebook + sources byte-identical
    # to the old flat ``[2]`` on an un-migrated account). The trailing ``None, 0``
    # is unchanged — only position 2 migrates.
    assert build_get_notebook_params("nb_abc") == [
        "nb_abc",
        None,
        [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
        None,
        0,
    ]


def test_direct_web_notebooks_api_construction_remains_supported() -> None:
    core = _make_core()
    api = WebNotebooksAPI(core.rpc_executor)

    assert hasattr(api, "_sources")
    assert isinstance(api._sources, SourceLister)


@pytest.mark.asyncio
async def test_direct_notebooks_api_get_metadata_uses_phase8_source_lister() -> None:
    core = _make_core()
    core.rpc_executor.rpc_call.return_value = [
        [
            "Architecture",
            [_source_entry("src_1", title="Design Paper", metadata=[None, 11, None, None, 3])],
            "nb_123",
        ]
    ]
    api = WebNotebooksAPI(core.rpc_executor)

    metadata = await api.get_metadata("nb_123")

    assert metadata.notebook == Notebook(id="nb_123", title="Architecture", sources_count=1)
    assert len(metadata.sources) == 1
    assert metadata.sources[0].kind == SourceType.PDF
    assert metadata.sources[0].title == "Design Paper"
    assert core.rpc_executor.rpc_call.await_count == 2


@pytest.mark.asyncio
async def test_direct_notebooks_api_metadata_lister_uses_late_bound_rpc_executor_call() -> None:
    core = _make_core()
    api = WebNotebooksAPI(core.rpc_executor)
    replacement_rpc = AsyncMock(
        return_value=[
            [
                "Late Bound",
                [_source_entry("src_1", title="Design Paper", metadata=[None, 11, None, None, 3])],
                "nb_123",
            ]
        ]
    )
    core.rpc_executor.rpc_call = replacement_rpc

    metadata = await api.get_metadata("nb_123")

    assert metadata.title == "Late Bound"
    assert metadata.sources[0].kind == SourceType.PDF
    assert replacement_rpc.await_count == 2


@pytest.mark.asyncio
async def test_client_wires_sources_api_into_notebooks_as_structural_lister() -> None:
    auth = AuthTokens(
        cookies={"SID": "test_sid", "__Secure-1PSIDTS": "test_1psidts", "HSID": "test_hsid"},
        csrf_token="test_csrf",
        session_id="test_session",
    )
    client = NotebookLMClient(auth)
    client.notebooks.get = AsyncMock(
        return_value=Notebook(id="nb_123", title="Client", sources_count=1)
    )
    client.sources.list = AsyncMock(return_value=[Source(id="src_1", title="Paper", _type_code=3)])

    metadata = await client.notebooks.get_metadata("nb_123")

    assert metadata.notebook.title == "Client"
    assert metadata.sources[0].kind == SourceType.PDF
    client.sources.list.assert_awaited_once_with("nb_123")


@pytest.mark.asyncio
async def test_get_metadata_uses_injected_source_lister_and_builds_summaries() -> None:
    core = _make_core()
    source_lister = MagicMock()
    source_lister.list = AsyncMock(
        return_value=[
            Source(
                id="src_1",
                title="Architecture Notes",
                url="https://example.com/notes",
                _type_code=5,  # SourceType.WEB_PAGE
            )
        ]
    )
    api = WebNotebooksAPI(core.rpc_executor, sources_api=source_lister)
    api.get = AsyncMock(return_value=Notebook(id="nb_123", title="Architecture", sources_count=1))

    metadata = await api.get_metadata("nb_123")

    assert isinstance(metadata, NotebookMetadata)
    assert metadata.notebook == Notebook(id="nb_123", title="Architecture", sources_count=1)
    assert len(metadata.sources) == 1
    assert metadata.sources[0].kind == SourceType.WEB_PAGE
    assert metadata.sources[0].title == "Architecture Notes"
    assert metadata.sources[0].url == "https://example.com/notes"
    api.get.assert_awaited_once_with("nb_123")
    source_lister.list.assert_awaited_once_with("nb_123")


@pytest.mark.asyncio
async def test_get_metadata_fetches_notebook_and_sources_concurrently() -> None:
    core = _make_core()
    source_lister = MagicMock()
    get_started = asyncio.Event()
    list_started = asyncio.Event()
    release = asyncio.Event()

    async def get_notebook(notebook_id: str) -> Notebook:
        assert notebook_id == "nb_123"
        get_started.set()
        await list_started.wait()
        await release.wait()
        return Notebook(id="nb_123", title="Concurrent", sources_count=1)

    async def list_sources(notebook_id: str) -> list[Source]:
        assert notebook_id == "nb_123"
        list_started.set()
        await get_started.wait()
        await release.wait()
        return [Source(id="src_1", title="Paper", _type_code=3)]  # SourceType.PDF

    source_lister.list = AsyncMock(side_effect=list_sources)
    api = WebNotebooksAPI(core.rpc_executor, sources_api=source_lister)
    api.get = AsyncMock(side_effect=get_notebook)

    metadata_task = asyncio.create_task(api.get_metadata("nb_123"))
    await asyncio.wait_for(get_started.wait(), timeout=1)
    await asyncio.wait_for(list_started.wait(), timeout=1)
    assert not metadata_task.done()

    release.set()
    metadata = await metadata_task

    assert metadata.notebook.title == "Concurrent"
    assert metadata.sources[0].kind == SourceType.PDF


@pytest.mark.asyncio
async def test_get_metadata_warns_when_notebook_reports_sources_but_listing_is_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    core = _make_core()
    source_lister = MagicMock()
    source_lister.list = AsyncMock(return_value=[])
    api = WebNotebooksAPI(core.rpc_executor, sources_api=source_lister)
    api.get = AsyncMock(return_value=Notebook(id="nb_123", title="Sparse", sources_count=2))

    with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
        metadata = await api.get_metadata("nb_123")

    assert metadata.sources == []
    assert "Notebook nb_123 reports 2 sources but listing returned empty" in caplog.text


@pytest.mark.asyncio
async def test_get_metadata_does_not_warn_when_empty_notebook_listing_is_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    core = _make_core()
    source_lister = MagicMock()
    source_lister.list = AsyncMock(return_value=[])
    api = WebNotebooksAPI(core.rpc_executor, sources_api=source_lister)
    api.get = AsyncMock(return_value=Notebook(id="nb_123", title="Empty", sources_count=0))

    with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
        metadata = await api.get_metadata("nb_123")

    assert metadata.sources == []
    assert caplog.records == []


def test_share_method_removed_in_v080() -> None:
    """NotebooksAPI.share() was removed in v0.8.0 (#1363).

    The deprecated no-behavior-change wrapper over ``client.sharing.set_public``
    is gone; the SHARE_ARTIFACT payload wiring it forwarded to lives in
    ``ShareManager.share`` (independently tested). Callers use
    ``client.sharing.set_public`` for the toggle and ``get_share_url`` for the
    deep-link URL.
    """
    api = _make_api()

    assert not hasattr(api, "share")
    with pytest.raises(AttributeError):
        api.share  # type: ignore[attr-defined]  # noqa: B018


def test_get_share_url_remains_sync_url_formatter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)
    api = _make_api()

    url = api.get_share_url("nb_123", artifact_id="art_456")

    assert isinstance(url, str)
    assert url == "https://notebook.google.com/notebook/nb_123?artifactId=art_456"


def _set_account_limit(api: WebNotebooksAPI, limit: int | None) -> AsyncMock:
    mock = AsyncMock(return_value=AccountLimits(notebook_limit=limit))
    api._get_account_limits = mock  # type: ignore[method-assign]
    return mock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "baseline_failure",
    [
        # A drifted LIST_NOTEBOOKS: the strict decoder raises, not the transport.
        pytest.param(RPCError("baseline decode failed"), id="decode_failure"),
        # A transport failure. The probe deliberately re-RAISES this class; the
        # baseline capture deliberately swallows it, so pin that asymmetry.
        pytest.param(ServerError("baseline 503"), id="transport_failure"),
    ],
)
async def test_create_baseline_failure_makes_a_match_ambiguous(
    caplog: pytest.LogCaptureFixture,
    baseline_failure: Exception,
) -> None:
    """No baseline + a match = ``RPCError``, not a guess (#2232).

    The notebook twin of ``test_add_url_baseline_failure_makes_a_match_ambiguous``.
    Titles are not unique in NotebookLM, so without a baseline a probe match may
    predate the create; adopting it hands the caller someone else's notebook
    under the name of one they think they just made, and every subsequent write
    in that session lands there. The error must carry enough to act on: what
    broke the baseline, and which notebook is ambiguous.
    """
    transport_error = NetworkError("temporary network failure")
    api = _make_api(rpc_call=AsyncMock(side_effect=transport_error))
    pre_existing = Notebook(id="nb_pre_existing", title="Quarterly Review")
    # First call = the baseline (fails); second = the probe.
    api.list = AsyncMock(side_effect=[baseline_failure, [pre_existing]])  # type: ignore[method-assign]

    with (
        caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"),
        pytest.raises(RPCError) as raised,
    ):
        await api.create("Quarterly Review")

    # Not the pre-existing notebook, under any guise.
    assert "disambiguate" in str(raised.value)
    assert pre_existing.id in str(raised.value)
    # The message names what broke the baseline — otherwise nothing reaching the
    # caller can explain why the snapshot was unavailable.
    assert type(baseline_failure).__name__ in str(raised.value)
    # The transport error that triggered the probe survives as context, and
    # ``__cause__`` is deliberately left unset so the traceback keeps printing
    # it — ``idempotent_create`` promises both halves stay visible.
    assert raised.value.__context__ is transport_error
    assert raised.value.__cause__ is None
    # The action survives the 300-char truncation the MCP/REST surfaces apply.
    assert "check your notebook list before retrying" in str(raised.value)[:300].lower()
    # An ambiguity IS an unconfirmed create (#2220): nothing threw inside the
    # probe, so this looks like an ordinary rejection — but the server may hold
    # a notebook either way, which is precisely what the marker names.
    assert getattr(raised.value, "unconfirmed", False) is True
    # One create attempt: the ambiguity aborts the loop, it does not re-issue.
    assert api._rpc.rpc_call.await_count == 1
    # The swallow is visible at the default logger level (WARNING), not DEBUG.
    assert "baseline list() failed" in caplog.text


class TestCreateNotebookQuotaDetection:
    @pytest.mark.asyncio
    async def test_create_uses_canonical_payload(self):
        # ``create`` now snapshots the notebook list as a baseline
        # before issuing CREATE_NOTEBOOK so the probe-then-retry wrapper
        # can detect a server-side commit on a transient transport
        # failure. Stub ``list`` so the canonical-payload assertion only
        # observes the CREATE_NOTEBOOK call.
        api = _make_api()
        api.list = AsyncMock(return_value=[])  # baseline empty
        api._rpc.rpc_call.return_value = [
            "Daily News",
            None,
            "new_notebook_id",
            None,
            None,
            [None, False, None, None, None, [1704067200, 0]],
        ]

        notebook = await api.create("Daily News")

        assert notebook.id == "new_notebook_id"
        api._rpc.rpc_call.assert_awaited_once_with(
            RPCMethod.CREATE_NOTEBOOK,
            build_create_notebook_params("Daily News"),
            disable_internal_retries=True,
        )

    @pytest.mark.asyncio
    async def test_create_retains_and_caches_volunteered_chat_session(self) -> None:
        api = _make_api()
        api.list = AsyncMock(return_value=[])
        api._rpc.rpc_call.return_value = [
            "Session Notebook",
            None,
            "nb-session",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            [["chat-session-1"]],
        ]

        notebook = await api.create("Session Notebook")

        assert [session.id for session in notebook.chat_sessions] == ["chat-session-1"]
        assert api._take_created_chat_session_id("nb-session") == "chat-session-1"
        assert api._take_created_chat_session_id("nb-session") is None

    @pytest.mark.asyncio
    async def test_create_invalid_argument_near_paid_limit_raises_limit_error(self):
        original = _create_invalid_argument_error()
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        account_limits = _set_account_limit(api, 500)
        api.list = AsyncMock(return_value=_owned_notebooks(499))

        with pytest.raises(NotebookLimitError) as exc_info:
            await api.create("Daily News")

        assert exc_info.value.current_count == 499
        assert exc_info.value.limit == 500
        assert exc_info.value.original_error is original
        assert "499/500" in str(exc_info.value)
        account_limits.assert_awaited_once()
        # ``create`` calls ``list`` twice on an RPC failure path:
        # once for the baseline snapshot, once for the quota check.
        assert api.list.await_count == 2

    @pytest.mark.asyncio
    async def test_quota_check_counts_owned_notebooks_the_user_has_shared(self):
        """#2125: sharing a notebook must not remove it from ``owned_count``.

        The pre-fix decoder derived ``is_owner`` from the "has any sharing"
        slot, so every notebook the account owned *and had shared* dropped out
        of this count and ``NotebookLimitError`` was never raised. Here all 500
        notebooks are owned-and-shared, so a correct count reaches the limit.
        """
        original = _create_invalid_argument_error()
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        _set_account_limit(api, 500)
        owned_and_shared = _owned_but_shared_notebooks(500)
        assert all(nb.role is SharePermission.OWNER for nb in owned_and_shared)
        api.list = AsyncMock(return_value=owned_and_shared)

        with pytest.raises(NotebookLimitError) as exc_info:
            await api.create("At Paid Limit")

        assert exc_info.value.current_count == 500

    @pytest.mark.asyncio
    async def test_quota_check_counts_rows_with_no_stated_role(self):
        """An unstated role counts as owned — matching ``is_owner``'s soft-degrade.

        Pinned deliberately: it means widespread protocol drift would inflate
        ``owned_count`` rather than deflate it. That direction is the safe one
        here — this path only runs to reclassify an already-failed create, so
        the worst case is a more specific error message, never a blocked create.
        """
        api = _make_api(rpc_call=AsyncMock(side_effect=_create_invalid_argument_error()))
        _set_account_limit(api, 500)
        unstated = [Notebook(id=f"nb_{i}", title=f"N{i}") for i in range(500)]
        assert all(nb.role is None for nb in unstated)
        api.list = AsyncMock(return_value=unstated)

        with pytest.raises(NotebookLimitError) as exc_info:
            await api.create("Unknown Roles")

        assert exc_info.value.current_count == 500

    @pytest.mark.asyncio
    async def test_quota_check_excludes_notebooks_shared_with_the_user(self):
        """Notebooks owned by *someone else* still must not count toward quota."""
        api = _make_api(rpc_call=AsyncMock(side_effect=_create_invalid_argument_error()))
        _set_account_limit(api, 500)
        api.list = AsyncMock(return_value=_owned_notebooks(100) + _shared_notebooks(400))

        with pytest.raises(RPCError) as exc_info:
            await api.create("Mostly Someone Else's")

        assert not isinstance(exc_info.value, NotebookLimitError)

    @pytest.mark.asyncio
    async def test_create_invalid_argument_at_paid_limit_raises_limit_error(self):
        original = _create_invalid_argument_error()
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        _set_account_limit(api, 500)
        api.list = AsyncMock(return_value=_owned_notebooks(500))

        with pytest.raises(NotebookLimitError) as exc_info:
            await api.create("At Paid Limit")

        assert exc_info.value.current_count == 500
        assert exc_info.value.limit == 500

    @pytest.mark.asyncio
    async def test_create_invalid_argument_near_free_limit_raises_limit_error(self):
        api = _make_api(rpc_call=AsyncMock(side_effect=_create_invalid_argument_error()))
        _set_account_limit(api, 100)
        api.list = AsyncMock(return_value=_owned_notebooks(100))

        with pytest.raises(NotebookLimitError) as exc_info:
            await api.create("Free Limit")

        assert exc_info.value.current_count == 100
        assert exc_info.value.limit == 100

    @pytest.mark.asyncio
    async def test_create_invalid_argument_uses_account_limit_not_free_boundary(self):
        original = _create_invalid_argument_error()
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        _set_account_limit(api, 500)
        api.list = AsyncMock(return_value=_owned_notebooks(100))

        with pytest.raises(RPCError) as exc_info:
            await api.create("Paid Account At Free Boundary")

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_create_invalid_argument_away_from_server_limit_preserves_rpc_error(self):
        original = _create_invalid_argument_error()
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        _set_account_limit(api, 500)
        api.list = AsyncMock(return_value=_owned_notebooks(250))

        with pytest.raises(RPCError) as exc_info:
            await api.create("Probably Bad Payload")

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_non_quota_rpc_code_preserves_rpc_error_without_listing(self):
        original = _create_invalid_argument_error(rpc_code=13)
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        api._get_account_limits = AsyncMock(  # type: ignore[method-assign]
            return_value=AccountLimits(notebook_limit=500)
        )
        api.list = AsyncMock(return_value=_owned_notebooks(500))

        with pytest.raises(RPCError) as exc_info:
            await api.create("Internal Failure")

        assert exc_info.value is original
        api._get_account_limits.assert_not_awaited()
        # baseline list runs once before CREATE_NOTEBOOK; no
        # quota-check list because the RPC code (13) is not the
        # quota-exhausted code (3).
        assert api.list.await_count == 1

    @pytest.mark.asyncio
    async def test_non_create_method_preserves_rpc_error_without_listing(self):
        original = _create_invalid_argument_error(method_id=RPCMethod.GET_NOTEBOOK.value)
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        api._get_account_limits = AsyncMock(  # type: ignore[method-assign]
            return_value=AccountLimits(notebook_limit=500)
        )
        api.list = AsyncMock(return_value=_owned_notebooks(500))

        with pytest.raises(RPCError) as exc_info:
            await api.create("Unexpected Method")

        assert exc_info.value is original
        api._get_account_limits.assert_not_awaited()
        # baseline list runs once before CREATE_NOTEBOOK; no
        # quota-check list because the failing method isn't CREATE_NOTEBOOK.
        assert api.list.await_count == 1

    @pytest.mark.asyncio
    async def test_shared_notebooks_do_not_trigger_owned_quota_error(self):
        original = _create_invalid_argument_error()
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        _set_account_limit(api, 500)
        api.list = AsyncMock(return_value=_owned_notebooks(20) + _shared_notebooks(479))

        with pytest.raises(RPCError) as exc_info:
            await api.create("Shared Notebooks Should Not Count")

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_account_limit_failure_preserves_original_create_error_without_listing(self):
        original = _create_invalid_argument_error()
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        api._get_account_limits = AsyncMock(  # type: ignore[method-assign]
            side_effect=NetworkError("settings failed")
        )
        api.list = AsyncMock(return_value=_owned_notebooks(500))

        with pytest.raises(RPCError) as exc_info:
            await api.create("Settings Fails")

        assert exc_info.value is original
        # only the baseline list runs; the quota-check list is
        # skipped because account-limit lookup itself failed.
        assert api.list.await_count == 1

    @pytest.mark.asyncio
    async def test_account_limit_rpc_error_preserves_original_create_error_without_listing(self):
        original = _create_invalid_argument_error()
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        api._get_account_limits = AsyncMock(  # type: ignore[method-assign]
            side_effect=RPCError("settings failed")
        )
        api.list = AsyncMock(return_value=_owned_notebooks(500))

        with pytest.raises(RPCError) as exc_info:
            await api.create("Settings RPC Fails")

        assert exc_info.value is original
        # only the baseline list runs.
        assert api.list.await_count == 1

    @pytest.mark.asyncio
    async def test_missing_account_limit_preserves_original_create_error_without_listing(self):
        original = _create_invalid_argument_error()
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        _set_account_limit(api, None)
        api.list = AsyncMock(return_value=_owned_notebooks(500))

        with pytest.raises(RPCError) as exc_info:
            await api.create("No Limit")

        assert exc_info.value is original
        # only the baseline list runs.
        assert api.list.await_count == 1

    @pytest.mark.asyncio
    async def test_list_failure_preserves_original_create_error(self):
        original = _create_invalid_argument_error()
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        _set_account_limit(api, 500)
        api.list = AsyncMock(side_effect=NetworkError("list failed"))

        with pytest.raises(RPCError) as exc_info:
            await api.create("List Fails")

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_list_parse_bug_preserves_original_create_error(self):
        original = _create_invalid_argument_error()
        api = _make_api(rpc_call=AsyncMock(side_effect=original))
        _set_account_limit(api, 500)
        api.list = AsyncMock(side_effect=ValueError("bad notebook data"))

        with pytest.raises(RPCError) as exc_info:
            await api.create("List Parse Fails")

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_get_account_limits_uses_user_settings_rpc(self):
        api = _make_api(rpc_call=AsyncMock(return_value=[[None, [6, 500, 300, 500000, 2]]]))

        limits = await api._get_account_limits()

        assert limits == AccountLimits(
            notebook_limit=500,
            source_limit=300,
            raw_limits=(6, 500, 300, 500000, 2),
            tier=2,
        )
        api._rpc.rpc_call.assert_awaited_once_with(
            RPCMethod.GET_USER_SETTINGS,
            [None, [1, None, None, None, None, None, None, None, None, None, [1]]],
            source_path="/",
        )


class TestCopyNotebook:
    @pytest.mark.asyncio
    async def test_copy_uses_live_wire_shape_and_decodes_project(self) -> None:
        rpc_call = AsyncMock(return_value=["Copied Notebook", [], "nb_copy"])
        api = _make_api(rpc_call=rpc_call)

        notebook = await api.copy("nb_source", "Copied Notebook")

        assert notebook.id == "nb_copy"
        assert notebook.title == "Copied Notebook"
        rpc_call.assert_awaited_once_with(
            RPCMethod.COPY_NOTEBOOK,
            build_copy_notebook_params("nb_source", "Copied Notebook"),
            source_path="/notebook/nb_source",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("notebook_id", "title", "message"),
        [("", "Copy", "notebook_id"), ("nb_source", "   ", "title")],
    )
    async def test_copy_rejects_empty_identifiers_or_titles(
        self, notebook_id: str, title: str, message: str
    ) -> None:
        api = _make_api()

        with pytest.raises(ValidationError, match=message):
            await api.copy(notebook_id, title)

        api._rpc.rpc_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_copy_fails_closed_when_response_has_no_id(self) -> None:
        api = _make_api(rpc_call=AsyncMock(return_value=["Copied Notebook", []]))

        with pytest.raises(DecodingError, match="did not contain a notebook id"):
            await api.copy("nb_source", "Copied Notebook")

    @pytest.mark.asyncio
    async def test_copy_fails_closed_when_response_reuses_source_id(self) -> None:
        api = _make_api(rpc_call=AsyncMock(return_value=["Copied Notebook", [], "nb_source"]))

        with pytest.raises(DecodingError, match="reused the source notebook id"):
            await api.copy("nb_source", "Copied Notebook")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failure", "expected_rpc_code"),
        [
            pytest.param(NetworkError("lost response"), None, id="network-error"),
            pytest.param(
                ServerError(
                    "lost response",
                    method_id=RPCMethod.COPY_NOTEBOOK.value,
                    rpc_code=14,
                ),
                14,
                id="server-error",
            ),
            pytest.param(
                RateLimitError(
                    "ambiguous throttle response",
                    method_id=RPCMethod.COPY_NOTEBOOK.value,
                    rpc_code=8,
                ),
                8,
                id="rate-limit-error",
            ),
        ],
    )
    async def test_copy_marks_lost_response_as_unconfirmed_without_retry(
        self,
        failure: NetworkError | RateLimitError | ServerError,
        expected_rpc_code: int | None,
    ) -> None:
        rpc_call = AsyncMock(side_effect=failure)
        api = _make_api(rpc_call=rpc_call)

        with pytest.raises(RPCError, match="list notebooks.*manually") as caught:
            await api.copy("nb_source", "Copied Notebook")

        assert getattr(caught.value, "unconfirmed", False) is True
        assert caught.value.method_id == RPCMethod.COPY_NOTEBOOK.value
        assert caught.value.rpc_code == expected_rpc_code
        assert caught.value.__cause__ is failure
        rpc_call.assert_awaited_once_with(
            RPCMethod.COPY_NOTEBOOK,
            build_copy_notebook_params("nb_source", "Copied Notebook"),
            source_path="/notebook/nb_source",
        )


class TestUpdateNotebook:
    @pytest.mark.asyncio
    async def test_rename_preserves_title_only_wire_shape(self) -> None:
        rpc_call = AsyncMock(
            side_effect=[
                None,
                [["Renamed", None, "nb-1", "", None, None, None, None]],
            ]
        )
        api = _make_api(rpc_call=rpc_call)

        await api.rename("nb-1", "Renamed")

        assert rpc_call.await_args_list[0].args[1] == [
            "nb-1",
            [[None, None, None, [None, "Renamed"]]],
        ]

    @pytest.mark.asyncio
    async def test_set_emoji_uses_change_property_tag_three(self) -> None:
        rpc_call = AsyncMock(
            side_effect=[
                None,
                [["Notebook", None, "nb-1", "🧬", None, None, None, None]],
            ]
        )
        api = _make_api(rpc_call=rpc_call)

        notebook = await api.set_emoji("nb-1", "🧬")

        assert notebook.emoji == "🧬"
        first = rpc_call.await_args_list[0]
        assert first.args == (
            RPCMethod.RENAME_NOTEBOOK,
            ["nb-1", [[None, None, None, [None, None, "🧬"]]]],
        )
        assert first.kwargs == {
            "source_path": "/",
            "allow_null": True,
            "raise_on_null_status": True,
        }

    @pytest.mark.asyncio
    async def test_update_title_and_emoji_in_one_mutation(self) -> None:
        rpc_call = AsyncMock(
            side_effect=[
                None,
                [["Renamed", None, "nb-1", "📖", None, None, None, None]],
            ]
        )
        api = _make_api(rpc_call=rpc_call)

        notebook = await api.update("nb-1", title="Renamed", emoji="📖")

        assert (notebook.title, notebook.emoji) == ("Renamed", "📖")
        assert rpc_call.await_args_list[0].args[1] == [
            "nb-1",
            [[None, None, None, [None, "Renamed", "📖"]]],
        ]

    @pytest.mark.asyncio
    async def test_update_requires_at_least_one_property(self) -> None:
        api = _make_api()

        with pytest.raises(ValidationError, match="At least one"):
            await api.update("nb-1")

        api._rpc.rpc_call.assert_not_awaited()


class TestGetNotebookFailsClosed:
    """``NotebooksAPI.get`` raises ``NotebookNotFoundError`` on degenerate responses.

    The NotebookLM backend returns a *parseable but empty* payload for unknown
    notebook IDs rather than a typed error. Pre-fix, ``get()`` happily returned
    ``Notebook(id="", title="")`` and the CLI ``use`` command persisted that as
    saved state. The post-fix contract: detect the degenerate shape and raise.
    """

    @pytest.mark.asyncio
    async def test_get_returns_notebook_on_full_response(self):
        # Realistic shape: [[title, ?, id, ?, ?, [None, False, ...]], ...]
        api = _make_api(
            rpc_call=AsyncMock(
                return_value=[["My Notebook", None, "nb_real_123", None, None, [None, False]]]
            )
        )

        notebook = await api.get("nb_real_123")

        assert notebook.id == "nb_real_123"
        assert notebook.title == "My Notebook"

    @pytest.mark.asyncio
    async def test_get_raises_on_empty_outer_list(self):
        """Server returned ``[]`` — no notebook at all."""
        api = _make_api(rpc_call=AsyncMock(return_value=[]))

        with pytest.raises(NotebookNotFoundError) as exc_info:
            await api.get("nb_missing")

        assert exc_info.value.notebook_id == "nb_missing"
        assert exc_info.value.method_id == RPCMethod.GET_NOTEBOOK.value

    @pytest.mark.asyncio
    async def test_get_raises_on_none_response(self):
        api = _make_api(rpc_call=AsyncMock(return_value=None))

        with pytest.raises(NotebookNotFoundError):
            await api.get("nb_missing")

    @pytest.mark.asyncio
    async def test_get_raises_on_degenerate_empty_inner(self):
        """``[[]]`` — outer wrapper present but inner notebook payload empty."""
        api = _make_api(rpc_call=AsyncMock(return_value=[[]]))

        with pytest.raises(NotebookNotFoundError) as exc_info:
            await api.get("nb_typo")

        assert "nb_typo" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_raises_when_id_and_title_both_blank(self):
        """Both id and title parsed to empty string → treat as not found."""
        # Same shape as the happy path but with empty strings in both fields.
        api = _make_api(
            rpc_call=AsyncMock(return_value=[["", None, "", None, None, [None, False]]])
        )

        with pytest.raises(NotebookNotFoundError):
            await api.get("nb_typo")

    @pytest.mark.asyncio
    async def test_get_succeeds_when_title_present_but_id_blank(self):
        """Defensive: a present title alone is enough — not a degenerate payload.

        We only treat the response as "not found" when BOTH id and title are
        blank, so a parser-quirk that strips the id but keeps the title still
        returns a Notebook rather than raising.
        """
        api = _make_api(
            rpc_call=AsyncMock(return_value=[["Title Only", None, "", None, None, [None, False]]])
        )

        notebook = await api.get("nb_partial")

        assert notebook.title == "Title Only"

    def test_notebook_not_found_error_is_rpc_error(self):
        """``NotebookNotFoundError`` must be catchable as ``RPCError``."""
        assert issubclass(NotebookNotFoundError, RPCError)
        err = NotebookNotFoundError("nb_x", method_id="rwIQyf")
        assert err.notebook_id == "nb_x"
        assert err.method_id == "rwIQyf"


class TestListNotebooksPayloadDispatch:
    """``list()`` wrapped-envelope dispatch — absence soft, malformed raises.

    Mirrors the ``_web/artifact/listing.py::list_raw`` fail-loud pattern (#1485):
    an empty/``None`` payload and a ``None`` row-list slot are legitimate "no
    notebooks" shapes, while a truthy payload that doesn't match the
    ``[[row, ...]]`` envelope is schema drift — it used to flow garbage rows
    into ``Notebook.from_api_response`` and silently fabricate empty-id
    notebooks.
    """

    @pytest.mark.asyncio
    async def test_wrapped_envelope_parses_rows(self):
        api = _make_api(
            rpc_call=AsyncMock(
                return_value=[[["Notebook A", [], "nb_a", "📓"], ["Notebook B", [], "nb_b", "📓"]]]
            )
        )

        notebooks = await api.list()

        assert [(nb.id, nb.title) for nb in notebooks] == [
            ("nb_a", "Notebook A"),
            ("nb_b", "Notebook B"),
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [None, []])
    async def test_empty_payload_is_soft_empty(self, payload):
        api = _make_api(rpc_call=AsyncMock(return_value=payload))
        assert await api.list() == []

    @pytest.mark.asyncio
    async def test_null_row_list_slot_is_soft_empty(self):
        """A ``None`` where the row list belongs is absence, not drift."""
        api = _make_api(rpc_call=AsyncMock(return_value=[None]))
        assert await api.list() == []

    @pytest.mark.asyncio
    async def test_truthy_non_list_payload_raises_decoding_error(self):
        from notebooklm.exceptions import DecodingError

        api = _make_api(rpc_call=AsyncMock(return_value="garbage"))
        with pytest.raises(DecodingError):
            await api.list()

    @pytest.mark.asyncio
    async def test_truthy_non_list_row_slot_raises_decoding_error(self):
        """A moved wrapper (non-list where the row list belongs) is drift."""
        from notebooklm.exceptions import DecodingError

        api = _make_api(rpc_call=AsyncMock(return_value=["garbage", "rows"]))
        with pytest.raises(DecodingError):
            await api.list()
