"""P9.3 settings/suggestions: the first codec rows dispatch exactly as the handlers did.

The four converted operations are ``encode → one native call → decode`` rows in
``_web/bindings/settings.py``.  These tests pin the oracles the plan names for a
conversion: the identical keyword set reaches the runtime (including explicit
``False``/``None`` values), failure projection is byte-for-byte what ``invoke()``
produced for handler rows, and the ``dispatched`` marker set by ``WebTransport``
reaches the neutral ``BackendError`` so ``may_have_committed`` can read it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import CodecBinding, DeadlineMode
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._semantic.records import (
    ARTIFACT_SUGGEST_REPORTS_DEF,
    SETTINGS_GET_DEF,
    SETTINGS_GET_LIMITS_DEF,
    SETTINGS_SET_LANGUAGE_DEF,
    ArtifactSuggestReportsInput,
    SettingsGetInput,
    SettingsGetLimitsInput,
    SettingsSetLanguageInput,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import settings as settings_rows
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_SETTINGS_RESPONSE = [[None, [True, 200, 100, None, 99], [None, None, None, None, ["fr"]]]]


@dataclass
class _Call:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _RecordingExecutor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method=method, params=params, kwargs=kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_settings_rows_replace_their_handlers_in_the_registry_and_table() -> None:
    converted = {
        Operation.SETTINGS_GET: settings_rows.SETTINGS_GET,
        Operation.SETTINGS_GET_LIMITS: settings_rows.SETTINGS_GET_LIMITS,
        Operation.SETTINGS_SET_LANGUAGE: settings_rows.SETTINGS_SET_LANGUAGE,
        Operation.ARTIFACT_SUGGEST_REPORTS: settings_rows.ARTIFACT_SUGGEST_REPORTS,
        # P10 R5.1c: this deferred-product row joined the codec rows once its
        # default-source read moved above the port.
        Operation.NOTEBOOK_SUGGEST_PROMPTS: settings_rows.NOTEBOOK_SUGGEST_PROMPTS,
    }
    # Domain-scoped: other P9.3 domains add their own rows to WEB_BINDING_ROWS.
    assert {op: settings_rows.SETTINGS_ROWS[op] for op in converted} == converted
    for operation, row in converted.items():
        assert WEB_BINDING_ROWS[operation] is row
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.row is row
        assert isinstance(row, CodecBinding)
        assert row.definition is binding.definition
        assert row.deadline is DeadlineMode.INHERIT
        assert row.native.is_constant
        assert row.forward_disable_internal_retries is False
    for name in (
        "_settings_get",
        "_settings_get_limits",
        "_settings_set_language",
        "_artifact_suggest_reports",
        "_notebook_suggest_prompts",
    ):
        assert not hasattr(WebRpcBackend, name)
    backend = build_web_backend(_RecordingExecutor())
    assert backend._bindings[Operation.SETTINGS_GET] is settings_rows.SETTINGS_GET


@pytest.mark.asyncio
async def test_settings_rows_forward_the_identical_keyword_set() -> None:
    executor = _RecordingExecutor(
        _SETTINGS_RESPONSE,
        _SETTINGS_RESPONSE,
        [None, None, [None, None, None, None, ["ja"]]],
        [[["Title", "Description", "Prompt", None, None, "Advanced"]]],
    )
    backend = build_web_backend(executor)

    settings = await backend.invoke(SETTINGS_GET_DEF, SettingsGetInput(), deadline=None)
    limits = await backend.invoke(SETTINGS_GET_LIMITS_DEF, SettingsGetLimitsInput(), deadline=None)
    language = await backend.invoke(
        SETTINGS_SET_LANGUAGE_DEF, SettingsSetLanguageInput("ja"), deadline=None
    )
    reports = await backend.invoke(
        ARTIFACT_SUGGEST_REPORTS_DEF, ArtifactSuggestReportsInput("nb"), deadline=None
    )

    assert settings.settings.output_language == "fr"
    assert settings.settings.limits.notebook_limit == 200
    assert limits.limits.notebook_limit == 200
    assert language.output_language == "ja"
    assert [item.title for item in reports.suggestions] == ["Title"]

    get, get_limits, set_language, suggest = executor.calls
    assert get.method is RPCMethod.GET_USER_SETTINGS
    assert get.params == [None, [1, None, None, None, None, None, None, None, None, None, [1]]]
    assert get_limits.method is RPCMethod.GET_USER_SETTINGS
    assert set_language.method is RPCMethod.SET_USER_SETTINGS
    assert set_language.params == [[[None, [[None, None, None, None, ["ja"]]]]]]
    assert suggest.method is RPCMethod.GET_SUGGESTED_REPORTS
    assert suggest.params == [[2], "nb"]
    for call in (get, get_limits, set_language):
        assert call.kwargs == {
            "source_path": "/",
            "allow_null": False,
            "_is_retry": False,
            "disable_internal_retries": False,
            "operation_variant": None,
            "read_timeout": None,
            "raise_on_null_status": False,
            "_retry_deadline": None,
        }
    assert suggest.kwargs["source_path"] == "/notebook/nb"
    assert suggest.kwargs["allow_null"] is True
    assert suggest.kwargs["raise_on_null_status"] is False
    assert suggest.kwargs["operation_variant"] is None


@pytest.mark.asyncio
async def test_codec_row_read_timeout_is_clamped_to_the_shared_deadline() -> None:
    executor = _RecordingExecutor(_SETTINGS_RESPONSE)
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    await backend.invoke(SETTINGS_GET_DEF, SettingsGetInput(), deadline=deadline)

    (call,) = executor.calls
    assert call.kwargs["read_timeout"] == pytest.approx(4.0)
    assert call.kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_codec_row_server_error_translates_like_a_handler_and_is_dispatched() -> None:
    executor = _RecordingExecutor(ServerError("boom", method_id=RPCMethod.SET_USER_SETTINGS.value))
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            SETTINGS_SET_LANGUAGE_DEF, SettingsSetLanguageInput("ja"), deadline=None
        )

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.SETTINGS_SET_LANGUAGE
    assert error.reason is BackendErrorReason.SERVER
    assert error.message == "boom"
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.SET_USER_SETTINGS.value
    assert "public_error_failure" in error.diagnostics
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert isinstance(error.__cause__, ServerError)


@pytest.mark.asyncio
async def test_codec_row_timeout_after_expiry_becomes_a_dispatched_deadline_error() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(
        RPCTimeoutError("slow", method_id=RPCMethod.GET_USER_SETTINGS.value)
    )
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(SETTINGS_GET_DEF, SettingsGetInput(), deadline=deadline)

    error = caught.value
    assert error.operation is Operation.SETTINGS_GET
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.outcome_unknown is False  # READ policy
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.diagnostics is not None
    assert error.diagnostics["timeout"] == 5.0
    assert error.diagnostics["method_id"] == RPCMethod.GET_USER_SETTINGS.value
    assert "public_error_failure" in error.diagnostics
    assert isinstance(error.__cause__, RPCTimeoutError)


@pytest.mark.asyncio
async def test_codec_row_pre_dispatch_expiry_is_not_dispatched() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            SETTINGS_SET_LANGUAGE_DEF, SettingsSetLanguageInput("ja"), deadline=deadline
        )

    assert executor.calls == []
    assert caught.value.dispatched is False
    assert may_have_committed(caught.value) is False
