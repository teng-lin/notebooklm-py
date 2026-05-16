"""Unit tests for notebook operations."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._notebooks import NotebooksAPI, build_create_notebook_params
from notebooklm.exceptions import (
    NetworkError,
    NotebookLimitError,
    NotebookNotFoundError,
    RPCError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.types import AccountLimits, Notebook


def _make_api() -> NotebooksAPI:
    core = MagicMock()
    core.rpc_call = AsyncMock()
    return NotebooksAPI(core, sources_api=MagicMock())


def _owned_notebooks(count: int) -> list[Notebook]:
    return [Notebook(id=f"owned_{i}", title=f"Owned {i}", is_owner=True) for i in range(count)]


def _shared_notebooks(count: int) -> list[Notebook]:
    return [Notebook(id=f"shared_{i}", title=f"Shared {i}", is_owner=False) for i in range(count)]


def _create_invalid_argument_error(
    *, method_id: str = RPCMethod.CREATE_NOTEBOOK.value, rpc_code: int = 3
) -> RPCError:
    return RPCError(
        "RPC CCqFvf returned null result with status code 3 (Invalid argument).",
        method_id=method_id,
        rpc_code=rpc_code,
    )


def test_build_create_notebook_params_matches_live_payload() -> None:
    assert build_create_notebook_params("Daily News") == ["Daily News", None, None, [2], [1]]


def _set_account_limit(api: NotebooksAPI, limit: int | None) -> AsyncMock:
    mock = AsyncMock(return_value=AccountLimits(notebook_limit=limit))
    api._get_account_limits = mock  # type: ignore[method-assign]
    return mock


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
        api._core.rpc_call.return_value = [
            "Daily News",
            None,
            "new_notebook_id",
            None,
            None,
            [None, False, None, None, None, [1704067200, 0]],
        ]

        notebook = await api.create("Daily News")

        assert notebook.id == "new_notebook_id"
        api._core.rpc_call.assert_awaited_once_with(
            RPCMethod.CREATE_NOTEBOOK,
            build_create_notebook_params("Daily News"),
            disable_internal_retries=True,
        )

    @pytest.mark.asyncio
    async def test_create_invalid_argument_near_paid_limit_raises_limit_error(self):
        api = _make_api()
        original = _create_invalid_argument_error()
        api._core.rpc_call = AsyncMock(side_effect=original)
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
    async def test_create_invalid_argument_at_paid_limit_raises_limit_error(self):
        api = _make_api()
        original = _create_invalid_argument_error()
        api._core.rpc_call = AsyncMock(side_effect=original)
        _set_account_limit(api, 500)
        api.list = AsyncMock(return_value=_owned_notebooks(500))

        with pytest.raises(NotebookLimitError) as exc_info:
            await api.create("At Paid Limit")

        assert exc_info.value.current_count == 500
        assert exc_info.value.limit == 500

    @pytest.mark.asyncio
    async def test_create_invalid_argument_near_free_limit_raises_limit_error(self):
        api = _make_api()
        api._core.rpc_call = AsyncMock(side_effect=_create_invalid_argument_error())
        _set_account_limit(api, 100)
        api.list = AsyncMock(return_value=_owned_notebooks(100))

        with pytest.raises(NotebookLimitError) as exc_info:
            await api.create("Free Limit")

        assert exc_info.value.current_count == 100
        assert exc_info.value.limit == 100

    @pytest.mark.asyncio
    async def test_create_invalid_argument_uses_account_limit_not_free_boundary(self):
        api = _make_api()
        original = _create_invalid_argument_error()
        api._core.rpc_call = AsyncMock(side_effect=original)
        _set_account_limit(api, 500)
        api.list = AsyncMock(return_value=_owned_notebooks(100))

        with pytest.raises(RPCError) as exc_info:
            await api.create("Paid Account At Free Boundary")

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_create_invalid_argument_away_from_server_limit_preserves_rpc_error(self):
        api = _make_api()
        original = _create_invalid_argument_error()
        api._core.rpc_call = AsyncMock(side_effect=original)
        _set_account_limit(api, 500)
        api.list = AsyncMock(return_value=_owned_notebooks(250))

        with pytest.raises(RPCError) as exc_info:
            await api.create("Probably Bad Payload")

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_non_quota_rpc_code_preserves_rpc_error_without_listing(self):
        api = _make_api()
        original = _create_invalid_argument_error(rpc_code=13)
        api._core.rpc_call = AsyncMock(side_effect=original)
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
        api = _make_api()
        original = _create_invalid_argument_error(method_id=RPCMethod.GET_NOTEBOOK.value)
        api._core.rpc_call = AsyncMock(side_effect=original)
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
        api = _make_api()
        original = _create_invalid_argument_error()
        api._core.rpc_call = AsyncMock(side_effect=original)
        _set_account_limit(api, 500)
        api.list = AsyncMock(return_value=_owned_notebooks(20) + _shared_notebooks(479))

        with pytest.raises(RPCError) as exc_info:
            await api.create("Shared Notebooks Should Not Count")

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_account_limit_failure_preserves_original_create_error_without_listing(self):
        api = _make_api()
        original = _create_invalid_argument_error()
        api._core.rpc_call = AsyncMock(side_effect=original)
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
        api = _make_api()
        original = _create_invalid_argument_error()
        api._core.rpc_call = AsyncMock(side_effect=original)
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
        api = _make_api()
        original = _create_invalid_argument_error()
        api._core.rpc_call = AsyncMock(side_effect=original)
        _set_account_limit(api, None)
        api.list = AsyncMock(return_value=_owned_notebooks(500))

        with pytest.raises(RPCError) as exc_info:
            await api.create("No Limit")

        assert exc_info.value is original
        # only the baseline list runs.
        assert api.list.await_count == 1

    @pytest.mark.asyncio
    async def test_list_failure_preserves_original_create_error(self):
        api = _make_api()
        original = _create_invalid_argument_error()
        api._core.rpc_call = AsyncMock(side_effect=original)
        _set_account_limit(api, 500)
        api.list = AsyncMock(side_effect=NetworkError("list failed"))

        with pytest.raises(RPCError) as exc_info:
            await api.create("List Fails")

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_list_parse_bug_preserves_original_create_error(self):
        api = _make_api()
        original = _create_invalid_argument_error()
        api._core.rpc_call = AsyncMock(side_effect=original)
        _set_account_limit(api, 500)
        api.list = AsyncMock(side_effect=ValueError("bad notebook data"))

        with pytest.raises(RPCError) as exc_info:
            await api.create("List Parse Fails")

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_get_account_limits_uses_user_settings_rpc(self):
        api = _make_api()
        api._core.rpc_call = AsyncMock(return_value=[[None, [6, 500, 300, 500000, 2]]])

        limits = await api._get_account_limits()

        assert limits == AccountLimits(
            notebook_limit=500,
            source_limit=300,
            raw_limits=(6, 500, 300, 500000, 2),
        )
        api._core.rpc_call.assert_awaited_once_with(
            RPCMethod.GET_USER_SETTINGS,
            [None, [1, None, None, None, None, None, None, None, None, None, [1]]],
            source_path="/",
        )


class TestGetNotebookFailsClosed:
    """``NotebooksAPI.get`` raises ``NotebookNotFoundError`` on degenerate responses.

    The NotebookLM backend returns a *parseable but empty* payload for unknown
    notebook IDs rather than a typed error. Pre-fix, ``get()`` happily returned
    ``Notebook(id="", title="")`` and the CLI ``use`` command persisted that as
    saved state. The post-fix contract: detect the degenerate shape and raise.
    """

    @pytest.mark.asyncio
    async def test_get_returns_notebook_on_full_response(self):
        api = _make_api()
        # Realistic shape: [[title, ?, id, ?, ?, [None, False, ...]], ...]
        api._core.rpc_call = AsyncMock(
            return_value=[["My Notebook", None, "nb_real_123", None, None, [None, False]]]
        )

        notebook = await api.get("nb_real_123")

        assert notebook.id == "nb_real_123"
        assert notebook.title == "My Notebook"

    @pytest.mark.asyncio
    async def test_get_raises_on_empty_outer_list(self):
        """Server returned ``[]`` — no notebook at all."""
        api = _make_api()
        api._core.rpc_call = AsyncMock(return_value=[])

        with pytest.raises(NotebookNotFoundError) as exc_info:
            await api.get("nb_missing")

        assert exc_info.value.notebook_id == "nb_missing"
        assert exc_info.value.method_id == RPCMethod.GET_NOTEBOOK.value

    @pytest.mark.asyncio
    async def test_get_raises_on_none_response(self):
        api = _make_api()
        api._core.rpc_call = AsyncMock(return_value=None)

        with pytest.raises(NotebookNotFoundError):
            await api.get("nb_missing")

    @pytest.mark.asyncio
    async def test_get_raises_on_degenerate_empty_inner(self):
        """``[[]]`` — outer wrapper present but inner notebook payload empty."""
        api = _make_api()
        api._core.rpc_call = AsyncMock(return_value=[[]])

        with pytest.raises(NotebookNotFoundError) as exc_info:
            await api.get("nb_typo")

        assert "nb_typo" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_raises_when_id_and_title_both_blank(self):
        """Both id and title parsed to empty string → treat as not found."""
        api = _make_api()
        # Same shape as the happy path but with empty strings in both fields.
        api._core.rpc_call = AsyncMock(return_value=[["", None, "", None, None, [None, False]]])

        with pytest.raises(NotebookNotFoundError):
            await api.get("nb_typo")

    @pytest.mark.asyncio
    async def test_get_succeeds_when_title_present_but_id_blank(self):
        """Defensive: a present title alone is enough — not a degenerate payload.

        We only treat the response as "not found" when BOTH id and title are
        blank, so a parser-quirk that strips the id but keeps the title still
        returns a Notebook rather than raising.
        """
        api = _make_api()
        api._core.rpc_call = AsyncMock(
            return_value=[["Title Only", None, "", None, None, [None, False]]]
        )

        notebook = await api.get("nb_partial")

        assert notebook.title == "Title Only"

    def test_notebook_not_found_error_is_rpc_error(self):
        """``NotebookNotFoundError`` must be catchable as ``RPCError``."""
        assert issubclass(NotebookNotFoundError, RPCError)
        err = NotebookNotFoundError("nb_x", method_id="rwIQyf")
        assert err.notebook_id == "nb_x"
        assert err.method_id == "rwIQyf"
