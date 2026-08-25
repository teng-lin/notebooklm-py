"""Unit tests for notebook operations."""

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm import _notebooks as notebooks_module
from notebooklm._notebook_payloads import (
    build_create_notebook_params as canonical_build_create_notebook_params,
)
from notebooklm._notebook_payloads import build_get_notebook_params
from notebooklm._notebooks import NotebooksAPI, build_create_notebook_params
from notebooklm._sources import SourcesAPI
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.exceptions import (
    NetworkError,
    NotebookLimitError,
    NotebookNotFoundError,
    RPCError,
    ServerError,
    ValidationError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.types import (
    Notebook,
    NotebookMetadata,
    SharePermission,
    Source,
    SourceType,
)
from tests._fixtures.web_backend import build_web_backend


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


def _make_api(rpc_call: AsyncMock | None = None) -> NotebooksAPI:
    core = _make_core(rpc_call)
    return NotebooksAPI(
        sources_api=MagicMock(),
        _backend=build_web_backend(core.rpc_executor),
    )


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


def test_notebook_payload_builders_keep_their_compatibility_import_path() -> None:
    """Only the create builder keeps a facade-level import path.

    R6.2 removed ``_notebooks``'s ``build_get_notebook_params`` import: the raw
    read moved onto the ``NOTEBOOK_GET`` codec row, so re-exporting the builder
    from the facade would be a pure alias with no consumer (P10 migration rule
    1 forbids those). ``_notebook_payloads`` is its one home.
    """
    assert build_create_notebook_params is canonical_build_create_notebook_params
    assert not hasattr(notebooks_module, "build_get_notebook_params")


def test_direct_notebooks_api_construction_remains_supported() -> None:
    api = NotebooksAPI()

    assert api._sources is None


@pytest.mark.asyncio
async def test_get_metadata_uses_injected_semantic_source_lister() -> None:
    core = _make_core()
    core.rpc_executor.rpc_call.return_value = [
        [
            "Architecture",
            [_source_entry("src_1", title="Design Paper", metadata=[None, 11, None, None, 3])],
            "nb_123",
        ]
    ]
    backend = build_web_backend(core.rpc_executor)
    sources = SourcesAPI(core.rpc_executor, uploader=MagicMock(), _backend=backend)
    api = NotebooksAPI(sources_api=sources, _backend=backend)

    metadata = await api.get_metadata("nb_123")

    assert metadata.notebook == Notebook(id="nb_123", title="Architecture", sources_count=1)
    assert len(metadata.sources) == 1
    assert metadata.sources[0].kind == SourceType.PDF
    assert metadata.sources[0].title == "Design Paper"
    assert core.rpc_executor.rpc_call.await_count == 2


@pytest.mark.asyncio
async def test_injected_metadata_lister_uses_late_bound_rpc_executor_call() -> None:
    core = _make_core()
    backend = build_web_backend(core.rpc_executor)
    sources = SourcesAPI(core.rpc_executor, uploader=MagicMock(), _backend=backend)
    api = NotebooksAPI(sources_api=sources, _backend=backend)
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
    api = NotebooksAPI(sources_api=source_lister)
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
    api = NotebooksAPI(sources_api=source_lister)
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
async def test_get_metadata_cancels_and_drains_sibling_when_one_read_fails() -> None:
    source_lister = MagicMock()
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def get_notebook(_notebook_id: str) -> Notebook:
        await sibling_started.wait()
        raise RuntimeError("notebook read failed")

    async def list_sources(_notebook_id: str) -> list[Source]:
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    source_lister.list = AsyncMock(side_effect=list_sources)
    api = NotebooksAPI(sources_api=source_lister)
    api.get = AsyncMock(side_effect=get_notebook)

    with pytest.raises(RuntimeError, match="notebook read failed"):
        await api.get_metadata("nb_123")

    assert sibling_cancelled.is_set()


@pytest.mark.asyncio
async def test_get_metadata_warns_when_notebook_reports_sources_but_listing_is_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_lister = MagicMock()
    source_lister.list = AsyncMock(return_value=[])
    api = NotebooksAPI(sources_api=source_lister)
    api.get = AsyncMock(return_value=Notebook(id="nb_123", title="Sparse", sources_count=2))

    with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
        metadata = await api.get_metadata("nb_123")

    assert metadata.sources == []
    assert "Notebook nb_123 reports 2 sources but listing returned empty" in caplog.text


@pytest.mark.asyncio
async def test_get_metadata_does_not_warn_when_empty_notebook_listing_is_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_lister = MagicMock()
    source_lister.list = AsyncMock(return_value=[])
    api = NotebooksAPI(sources_api=source_lister)
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


def _notebook_row(notebook: Notebook) -> list[Any]:
    role = notebook.role.value if notebook.role is not None else None
    return [notebook.title, [], notebook.id, notebook.emoji, None, [role, False, True]]


def _create_rpc(
    *,
    create_result: Any = None,
    create_error: Exception | None = None,
    list_results: list[list[Notebook] | Exception] | None = None,
    account_limit: int | None = None,
    settings_error: Exception | None = None,
) -> AsyncMock:
    """Build the executor seam exercised by backend-owned create reconciliation."""
    pending_lists = list(list_results or [[]])

    async def dispatch(method: RPCMethod, _params: Any, **_kwargs: Any) -> Any:
        if method is RPCMethod.LIST_NOTEBOOKS:
            if not pending_lists:
                raise AssertionError("unexpected LIST_NOTEBOOKS call")
            item = pending_lists.pop(0)
            if isinstance(item, Exception):
                raise item
            return [[_notebook_row(notebook) for notebook in item]]
        if method is RPCMethod.CREATE_NOTEBOOK:
            if create_error is not None:
                raise create_error
            return create_result
        if method is RPCMethod.GET_USER_SETTINGS:
            if settings_error is not None:
                raise settings_error
            return [[None, [6, account_limit, 300, 500000, 2]]]
        raise AssertionError(f"unexpected RPC method: {method}")

    return AsyncMock(side_effect=dispatch)


def _assert_equivalent_rpc_error(actual: RPCError, expected: RPCError) -> None:
    assert type(actual) is type(expected)
    assert str(actual) == str(expected)
    assert actual.method_id == expected.method_id
    assert actual.rpc_code == expected.rpc_code


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
    pre_existing = Notebook(id="nb_pre_existing", title="Quarterly Review")
    rpc_call = _create_rpc(
        create_error=transport_error,
        list_results=[baseline_failure, [pre_existing]],
    )
    api = _make_api(rpc_call=rpc_call)

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
    # The public boundary reconstructs exceptions from bounded neutral evidence;
    # object identity does not cross it, while the reviewed cause/context graph does.
    # The action survives the 300-char truncation the MCP/REST surfaces apply.
    assert "check your notebook list before retrying" in str(raised.value)[:300].lower()
    # An ambiguity IS an unconfirmed create (#2220): nothing threw inside the
    # probe, so this looks like an ordinary rejection — but the server may hold
    # a notebook either way, which is precisely what the marker names.
    assert getattr(raised.value, "unconfirmed", False) is True
    # One create attempt: the ambiguity aborts the loop, it does not re-issue.
    create_calls = [
        call for call in rpc_call.await_args_list if call.args[0] is RPCMethod.CREATE_NOTEBOOK
    ]
    assert len(create_calls) == 1
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
        result = [
            "Daily News",
            None,
            "new_notebook_id",
            None,
            None,
            [None, False, None, None, None, [1704067200, 0]],
        ]
        rpc_call = _create_rpc(create_result=result)
        api = _make_api(rpc_call=rpc_call)

        notebook = await api.create("Daily News")

        assert notebook.id == "new_notebook_id"
        create_call = rpc_call.await_args_list[-1]
        assert create_call.args == (
            RPCMethod.CREATE_NOTEBOOK,
            build_create_notebook_params("Daily News"),
        )
        assert create_call.kwargs["disable_internal_retries"] is True

    @pytest.mark.asyncio
    async def test_create_retains_and_caches_volunteered_chat_session(self) -> None:
        result = [
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
        api = _make_api(rpc_call=_create_rpc(create_result=result))

        notebook = await api.create("Session Notebook")

        assert [session.id for session in notebook.chat_sessions] == ["chat-session-1"]
        assert api._take_created_chat_session_id("nb-session") == "chat-session-1"
        assert api._take_created_chat_session_id("nb-session") is None

    @pytest.mark.asyncio
    async def test_create_invalid_argument_near_paid_limit_raises_limit_error(self):
        original = _create_invalid_argument_error()
        notebooks = _owned_notebooks(499)
        rpc_call = _create_rpc(
            create_error=original,
            list_results=[notebooks, notebooks],
            account_limit=500,
        )
        api = _make_api(rpc_call=rpc_call)

        with pytest.raises(NotebookLimitError) as exc_info:
            await api.create("Daily News")

        assert exc_info.value.current_count == 499
        assert exc_info.value.limit == 500
        _assert_equivalent_rpc_error(exc_info.value.original_error, original)
        assert exc_info.value.__cause__ is exc_info.value.original_error
        assert exc_info.value.__context__ is exc_info.value.original_error
        assert exc_info.value.__suppress_context__ is True
        assert "499/500" in str(exc_info.value)
        assert [call.args[0] for call in rpc_call.await_args_list].count(
            RPCMethod.GET_USER_SETTINGS
        ) == 1
        assert [call.args[0] for call in rpc_call.await_args_list].count(
            RPCMethod.LIST_NOTEBOOKS
        ) == 2

    @pytest.mark.asyncio
    async def test_quota_check_counts_owned_notebooks_the_user_has_shared(self):
        """#2125: sharing a notebook must not remove it from ``owned_count``.

        The pre-fix decoder derived ``is_owner`` from the "has any sharing"
        slot, so every notebook the account owned *and had shared* dropped out
        of this count and ``NotebookLimitError`` was never raised. Here all 500
        notebooks are owned-and-shared, so a correct count reaches the limit.
        """
        original = _create_invalid_argument_error()
        owned_and_shared = _owned_but_shared_notebooks(500)
        assert all(nb.role is SharePermission.OWNER for nb in owned_and_shared)
        api = _make_api(
            rpc_call=_create_rpc(
                create_error=original,
                list_results=[owned_and_shared, owned_and_shared],
                account_limit=500,
            )
        )

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
        unstated = [Notebook(id=f"nb_{i}", title=f"N{i}") for i in range(500)]
        assert all(nb.role is None for nb in unstated)
        api = _make_api(
            rpc_call=_create_rpc(
                create_error=_create_invalid_argument_error(),
                list_results=[unstated, unstated],
                account_limit=500,
            )
        )

        with pytest.raises(NotebookLimitError) as exc_info:
            await api.create("Unknown Roles")

        assert exc_info.value.current_count == 500

    @pytest.mark.asyncio
    async def test_quota_check_excludes_notebooks_shared_with_the_user(self):
        """Notebooks owned by *someone else* still must not count toward quota."""
        notebooks = _owned_notebooks(100) + _shared_notebooks(400)
        api = _make_api(
            rpc_call=_create_rpc(
                create_error=_create_invalid_argument_error(),
                list_results=[notebooks, notebooks],
                account_limit=500,
            )
        )

        with pytest.raises(RPCError) as exc_info:
            await api.create("Mostly Someone Else's")

        assert not isinstance(exc_info.value, NotebookLimitError)

    @pytest.mark.asyncio
    async def test_create_invalid_argument_at_paid_limit_raises_limit_error(self):
        original = _create_invalid_argument_error()
        notebooks = _owned_notebooks(500)
        api = _make_api(
            rpc_call=_create_rpc(
                create_error=original,
                list_results=[notebooks, notebooks],
                account_limit=500,
            )
        )

        with pytest.raises(NotebookLimitError) as exc_info:
            await api.create("At Paid Limit")

        assert exc_info.value.current_count == 500
        assert exc_info.value.limit == 500

    @pytest.mark.asyncio
    async def test_create_invalid_argument_near_free_limit_raises_limit_error(self):
        notebooks = _owned_notebooks(100)
        api = _make_api(
            rpc_call=_create_rpc(
                create_error=_create_invalid_argument_error(),
                list_results=[notebooks, notebooks],
                account_limit=100,
            )
        )

        with pytest.raises(NotebookLimitError) as exc_info:
            await api.create("Free Limit")

        assert exc_info.value.current_count == 100
        assert exc_info.value.limit == 100

    @pytest.mark.asyncio
    async def test_create_invalid_argument_uses_account_limit_not_free_boundary(self):
        original = _create_invalid_argument_error()
        notebooks = _owned_notebooks(100)
        api = _make_api(
            rpc_call=_create_rpc(
                create_error=original,
                list_results=[notebooks, notebooks],
                account_limit=500,
            )
        )

        with pytest.raises(RPCError) as exc_info:
            await api.create("Paid Account At Free Boundary")

        _assert_equivalent_rpc_error(exc_info.value, original)

    @pytest.mark.asyncio
    async def test_create_invalid_argument_away_from_server_limit_preserves_rpc_error(self):
        original = _create_invalid_argument_error()
        notebooks = _owned_notebooks(250)
        api = _make_api(
            rpc_call=_create_rpc(
                create_error=original,
                list_results=[notebooks, notebooks],
                account_limit=500,
            )
        )

        with pytest.raises(RPCError) as exc_info:
            await api.create("Probably Bad Payload")

        _assert_equivalent_rpc_error(exc_info.value, original)

    @pytest.mark.asyncio
    async def test_non_quota_rpc_code_preserves_rpc_error_without_listing(self):
        original = _create_invalid_argument_error(rpc_code=13)
        rpc_call = _create_rpc(create_error=original, list_results=[_owned_notebooks(500)])
        api = _make_api(rpc_call=rpc_call)

        with pytest.raises(RPCError) as exc_info:
            await api.create("Internal Failure")

        _assert_equivalent_rpc_error(exc_info.value, original)
        assert not any(
            invoked.args[0] is RPCMethod.GET_USER_SETTINGS for invoked in rpc_call.await_args_list
        )
        # baseline list runs once before CREATE_NOTEBOOK; no
        # quota-check list because the RPC code (13) is not the
        # quota-exhausted code (3).
        assert [item.args[0] for item in rpc_call.await_args_list].count(
            RPCMethod.LIST_NOTEBOOKS
        ) == 1

    @pytest.mark.asyncio
    async def test_non_create_method_preserves_rpc_error_without_listing(self):
        original = _create_invalid_argument_error(method_id=RPCMethod.GET_NOTEBOOK.value)
        rpc_call = _create_rpc(create_error=original, list_results=[_owned_notebooks(500)])
        api = _make_api(rpc_call=rpc_call)

        with pytest.raises(RPCError) as exc_info:
            await api.create("Unexpected Method")

        _assert_equivalent_rpc_error(exc_info.value, original)
        assert not any(
            invoked.args[0] is RPCMethod.GET_USER_SETTINGS for invoked in rpc_call.await_args_list
        )
        # baseline list runs once before CREATE_NOTEBOOK; no
        # quota-check list because the failing method isn't CREATE_NOTEBOOK.
        assert [item.args[0] for item in rpc_call.await_args_list].count(
            RPCMethod.LIST_NOTEBOOKS
        ) == 1

    @pytest.mark.asyncio
    async def test_shared_notebooks_do_not_trigger_owned_quota_error(self):
        original = _create_invalid_argument_error()
        notebooks = _owned_notebooks(20) + _shared_notebooks(479)
        api = _make_api(
            rpc_call=_create_rpc(
                create_error=original,
                list_results=[notebooks, notebooks],
                account_limit=500,
            )
        )

        with pytest.raises(RPCError) as exc_info:
            await api.create("Shared Notebooks Should Not Count")

        _assert_equivalent_rpc_error(exc_info.value, original)

    @pytest.mark.asyncio
    async def test_account_limit_failure_preserves_original_create_error_without_listing(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        original = _create_invalid_argument_error()
        rpc_call = _create_rpc(
            create_error=original,
            list_results=[_owned_notebooks(500)],
            settings_error=NetworkError("settings failed"),
        )
        api = _make_api(rpc_call=rpc_call)

        with (
            caplog.at_level(logging.DEBUG, logger="notebooklm._notebooks"),
            pytest.raises(RPCError) as exc_info,
        ):
            await api.create("Settings Fails")

        _assert_equivalent_rpc_error(exc_info.value, original)
        # only the baseline list runs; the quota-check list is
        # skipped because account-limit lookup itself failed.
        assert [item.args[0] for item in rpc_call.await_args_list].count(
            RPCMethod.LIST_NOTEBOOKS
        ) == 1
        record = next(
            item for item in caplog.records if "Could not fetch account limits" in item.message
        )
        assert record.exc_info is not None

    @pytest.mark.asyncio
    async def test_account_limit_rpc_error_preserves_original_create_error_without_listing(self):
        original = _create_invalid_argument_error()
        rpc_call = _create_rpc(
            create_error=original,
            list_results=[_owned_notebooks(500)],
            settings_error=RPCError("settings failed"),
        )
        api = _make_api(rpc_call=rpc_call)

        with pytest.raises(RPCError) as exc_info:
            await api.create("Settings RPC Fails")

        _assert_equivalent_rpc_error(exc_info.value, original)
        # only the baseline list runs.
        assert [item.args[0] for item in rpc_call.await_args_list].count(
            RPCMethod.LIST_NOTEBOOKS
        ) == 1

    @pytest.mark.asyncio
    async def test_missing_account_limit_preserves_original_create_error_without_listing(self):
        original = _create_invalid_argument_error()
        rpc_call = _create_rpc(
            create_error=original,
            list_results=[_owned_notebooks(500)],
            account_limit=None,
        )
        api = _make_api(rpc_call=rpc_call)

        with pytest.raises(RPCError) as exc_info:
            await api.create("No Limit")

        _assert_equivalent_rpc_error(exc_info.value, original)
        # only the baseline list runs.
        assert [item.args[0] for item in rpc_call.await_args_list].count(
            RPCMethod.LIST_NOTEBOOKS
        ) == 1

    @pytest.mark.asyncio
    async def test_list_failure_preserves_original_create_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        original = _create_invalid_argument_error()
        rpc_call = _create_rpc(
            create_error=original,
            list_results=[[], NetworkError("list failed")],
            account_limit=500,
        )
        api = _make_api(rpc_call=rpc_call)

        with (
            caplog.at_level(logging.DEBUG, logger="notebooklm._notebooks"),
            pytest.raises(RPCError) as exc_info,
        ):
            await api.create("List Fails")

        _assert_equivalent_rpc_error(exc_info.value, original)
        record = next(item for item in caplog.records if "Could not list notebooks" in item.message)
        assert record.exc_info is not None

    @pytest.mark.asyncio
    async def test_list_parse_bug_preserves_original_create_error(self):
        original = _create_invalid_argument_error()
        rpc_call = _create_rpc(
            create_error=original,
            list_results=[[], ValueError("bad notebook data")],
            account_limit=500,
        )
        api = _make_api(rpc_call=rpc_call)

        with pytest.raises(RPCError) as exc_info:
            await api.create("List Parse Fails")

        _assert_equivalent_rpc_error(exc_info.value, original)

    @pytest.mark.asyncio
    async def test_quota_detection_uses_user_settings_rpc(self):
        original = _create_invalid_argument_error()
        rpc_call = _create_rpc(
            create_error=original,
            list_results=[[], _owned_notebooks(100)],
            account_limit=500,
        )
        api = _make_api(rpc_call=rpc_call)

        with pytest.raises(RPCError):
            await api.create("Daily News")

        settings_call = next(
            item for item in rpc_call.await_args_list if item.args[0] is RPCMethod.GET_USER_SETTINGS
        )
        assert settings_call.args[1] == [
            None,
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]
        assert settings_call.kwargs["source_path"] == "/"


class TestUpdateNotebook:
    @pytest.mark.asyncio
    async def test_rename_preserves_legacy_empty_title_wire_behavior(self) -> None:
        rpc_call = AsyncMock(
            side_effect=[
                None,
                [["", None, "nb-1", "", None, None, None, None]],
            ]
        )
        api = _make_api(rpc_call=rpc_call)

        renamed = await api.rename("nb-1", "")

        assert renamed.title == ""
        assert rpc_call.await_args_list[0].args[1] == [
            "nb-1",
            [[None, None, None, [None, ""]]],
        ]

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
        assert first.kwargs["source_path"] == "/"
        assert first.kwargs["allow_null"] is True

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
    async def test_update_readback_not_found_reconstructs_public_error(self) -> None:
        api = _make_api(rpc_call=AsyncMock(side_effect=[None, []]))

        with pytest.raises(NotebookNotFoundError) as caught:
            await api.rename("nb-missing", "Renamed")

        assert caught.value.notebook_id == "nb-missing"
        assert caught.value.method_id == RPCMethod.GET_NOTEBOOK.value

    @pytest.mark.asyncio
    async def test_update_requires_at_least_one_property(self) -> None:
        core = _make_core()
        api = NotebooksAPI(
            sources_api=MagicMock(),
            _backend=build_web_backend(core.rpc_executor),
        )

        with pytest.raises(ValidationError, match="At least one"):
            await api.update("nb-1")

        core.rpc_executor.rpc_call.assert_not_awaited()


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
    async def test_get_or_none_does_not_decode_source_ids_from_not_found_payload(self, caplog):
        api = _make_api(rpc_call=AsyncMock(return_value=[None]))

        with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
            notebook = await api.get_or_none("nb_missing")

        assert notebook is None
        assert "get_source_ids" not in caplog.text

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

    Mirrors the ``_artifact/listing.py::list_raw`` fail-loud pattern (#1485):
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
