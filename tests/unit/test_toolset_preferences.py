"""Unit tests for ``notebooks.get_toolset_preferences`` (``EEr8Nb``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from notebooklm import (
    DecodingError,
    ToolsetPreference,
    UnsupportedOperationError,
    ValidationError,
)
from notebooklm._android.notebooks import AndroidNotebooksAPI
from notebooklm._web.notebooks import WebNotebooksAPI
from notebooklm._web.params.notebooks import build_toolset_preferences_params
from notebooklm._web.rows.notebooks import ToolsetPreferenceRow, decode_toolset_preferences
from notebooklm.rpc import RPCMethod


class _FakeSupervisor:
    def spawn_child(self, coro: Any, *, name: str | None = None) -> Any:
        return None

    @asynccontextmanager
    async def operation_scope(self, label: str) -> AsyncIterator[None]:
        yield None


class _FakeRpcCaller:
    def __init__(self, rpc_return: Any = None) -> None:
        self.rpc_return = rpc_return
        self.calls: list[tuple[Any, Any, str, bool]] = []

    async def rpc_call(
        self,
        method: Any,
        params: Any,
        *,
        source_path: str = "/",
        allow_null: bool = False,
    ) -> Any:
        self.calls.append((method, params, source_path, allow_null))
        return self.rpc_return


class _FakeAndroidSession:
    def __init__(self) -> None:
        self.unary_calls: list[tuple[Any, ...]] = []

    def spawn_child(self, coro: Any, *, name: str | None = None) -> Any:
        return None

    async def unary(self, *args: Any, **kwargs: Any) -> Any:
        self.unary_calls.append(args)
        return None


def _make_web_notebooks(rpc_return: Any = None) -> tuple[WebNotebooksAPI, _FakeRpcCaller]:
    rpc = _FakeRpcCaller(rpc_return=rpc_return)
    api = WebNotebooksAPI(rpc, supervisor=_FakeSupervisor())  # type: ignore[arg-type]
    return api, rpc


def _make_android_notebooks() -> tuple[AndroidNotebooksAPI, _FakeAndroidSession]:
    session = _FakeAndroidSession()
    api = AndroidNotebooksAPI(session, None)  # type: ignore[arg-type]
    return api, session


class TestBuildToolsetPreferencesParams:
    def test_builds_single_slot_notebook_id_payload(self) -> None:
        assert build_toolset_preferences_params("nb_123") == ["nb_123"]

    @pytest.mark.parametrize("bad", ["", "   ", None, 42])
    def test_rejects_blank_or_non_string_notebook_id(self, bad: Any) -> None:
        with pytest.raises(ValueError, match="notebook_id"):
            build_toolset_preferences_params(bad)


class TestToolsetPreferenceRowPositions:
    def test_decodes_distinct_slots_and_detects_slot_swaps(self) -> None:
        row = ToolsetPreferenceRow(
            [
                "TOOLSET_ID_AT_0",
                "DISPLAY_NAME_AT_1",
                True,
                [["TOOL_A_AT_3_0_0"], ["TOOL_B_AT_3_1_0"]],
            ]
        )
        assert row.toolset_id == "TOOLSET_ID_AT_0"
        assert row.display_name == "DISPLAY_NAME_AT_1"
        assert row.enabled is True
        assert row.tools == ("TOOL_A_AT_3_0_0", "TOOL_B_AT_3_1_0")

        # Swap id <-> display_name and flip enabled to non-bool int 1:
        # id/display_name reflect their slots, and non-bool 1 must NOT coerce to True.
        swapped = ToolsetPreferenceRow(
            [
                "DISPLAY_NAME_AT_1",
                "TOOLSET_ID_AT_0",
                1,
                [["TOOL_A_AT_3_0_0"]],
            ]
        )
        assert swapped.toolset_id == "DISPLAY_NAME_AT_1"
        assert swapped.display_name == "TOOLSET_ID_AT_0"
        assert swapped.enabled is False


class TestDecodeToolsetPreferences:
    @pytest.mark.parametrize("empty_payload", [None, [], [None], [[]]])
    def test_empty_payloads_return_empty_list(self, empty_payload: Any) -> None:
        assert decode_toolset_preferences(empty_payload) == []

    def test_decodes_enabled_and_disabled_toolsets(self) -> None:
        raw = [
            [
                ["google-drive", "Google Drive", True, [["Search Drive"], ["Read Drive file"]]],
                ["workspace-mail", None, False, []],
            ]
        ]
        decoded = decode_toolset_preferences(raw)
        assert decoded == [
            ToolsetPreference(
                toolset_id="google-drive",
                display_name="Google Drive",
                enabled=True,
                tools=("Search Drive", "Read Drive file"),
            ),
            ToolsetPreference(
                toolset_id="workspace-mail",
                display_name=None,
                enabled=False,
                tools=(),
            ),
        ]

    @pytest.mark.parametrize(
        "malformed",
        [
            "not-a-list",
            ["not-a-row-list"],
            [["not-a-row"]],
            [[[]]],
            [[[None, "Google Drive", True, []]]],
            [[["   ", "Google Drive", True, []]]],
        ],
    )
    def test_malformed_envelopes_and_rows_raise_decoding_error(self, malformed: Any) -> None:
        with pytest.raises(DecodingError) as exc_info:
            decode_toolset_preferences(malformed)
        assert exc_info.value.method_id == RPCMethod.LIST_TOOLSET_PREFERENCES.value


class TestWebNotebooksToolsetPreferences:
    @pytest.mark.asyncio
    async def test_calls_eer8nb_with_notebook_source_path_and_allow_null(self) -> None:
        api, rpc = _make_web_notebooks(
            [[["google-drive", "Google Drive", True, [["Search Drive"], ["Read Drive file"]]]]]
        )

        prefs = await api.get_toolset_preferences("nb_alpha")

        assert rpc.calls == [
            (
                RPCMethod.LIST_TOOLSET_PREFERENCES,
                ["nb_alpha"],
                "/notebook/nb_alpha",
                True,
            )
        ]
        assert prefs == [
            ToolsetPreference(
                toolset_id="google-drive",
                display_name="Google Drive",
                enabled=True,
                tools=("Search Drive", "Read Drive file"),
            )
        ]

    @pytest.mark.asyncio
    async def test_null_response_returns_empty_list(self) -> None:
        api, _ = _make_web_notebooks(None)
        assert await api.get_toolset_preferences("nb_alpha") == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_id", ["", "   "])
    async def test_blank_notebook_id_raises_validation_error_before_rpc(self, bad_id: str) -> None:
        api, rpc = _make_web_notebooks()
        with pytest.raises(ValidationError, match="notebook_id"):
            await api.get_toolset_preferences(bad_id)
        assert rpc.calls == []


class TestAndroidNotebooksToolsetPreferences:
    @pytest.mark.asyncio
    async def test_raises_unsupported_operation_error_without_transport_call(self) -> None:
        api, session = _make_android_notebooks()
        with pytest.raises(UnsupportedOperationError, match="Android backend"):
            await api.get_toolset_preferences("nb_alpha")
        assert session.unary_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_id", ["", "   "])
    async def test_blank_notebook_id_raises_validation_error_on_android(self, bad_id: str) -> None:
        api, session = _make_android_notebooks()
        with pytest.raises(ValidationError, match="notebook_id"):
            await api.get_toolset_preferences(bad_id)
        assert session.unary_calls == []
