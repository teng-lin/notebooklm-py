"""P10 R5.1c characterizations for ``notebook.suggest_prompts`` source scoping.

Pins the observable behaviour of the default-source path *before* the
``GET_NOTEBOOK`` read moves above the port (decision D1 option (a)), so the move
can only preserve it:

* the read order and the exact conditional — ``source_ids=None`` resolves,
  ``[]`` and an explicit list do not;
* the wire kwargs of both phases, which must stay byte-identical;
* the aggregate client-timeout budget: one :class:`RuntimeDeadline` identity
  shared by both natives, with the second phase seeing the consumed remainder;
* the ``notebooklm._notebooks`` warning surface of a malformed snapshot, and the
  partial-id tolerance that goes with it;
* the public exception a failing resolution raises.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from notebooklm._deadline import RuntimeDeadlineFactory
from notebooklm._notebook_payloads import build_get_notebook_params
from notebooklm._notebooks import NotebooksAPI
from notebooklm._runtime.contracts import RpcCaller
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.codec.suggestions import encode_prompt_suggestions
from notebooklm.exceptions import ServerError
from notebooklm.rpc import RPCMethod

NOTEBOOK_ID = "nb-r51c"

#: One well-formed ``GET_NOTEBOOK`` snapshot carrying two embedded source ids.
_SNAPSHOT = [["Notebook", [[["src_a"]], [["src_b"]]], NOTEBOOK_ID]]
#: One well-formed wrapped ``SUGGEST_PROMPTS`` envelope.
_SUGGESTIONS = [[["Title", "\n- Prompt."]]]


class _Executor:
    """Narrow ``rpc_call`` recorder that replays a scripted response sequence."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[RPCMethod, list[Any], dict[str, Any]]] = []
        self.on_call: Any = None

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append((method, params, kwargs))
        if self.on_call is not None:
            self.on_call(method)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def methods(self) -> list[RPCMethod]:
        return [method for method, _params, _kwargs in self.calls]


def _api(executor: _Executor, factory: RuntimeDeadlineFactory | None = None) -> NotebooksAPI:
    """Assemble the facade exactly as the composition root does (one factory)."""
    backend = WebRpcBackend(executor, deadline_factory=factory)  # type: ignore[arg-type]
    return NotebooksAPI(cast(RpcCaller, executor), _backend=backend, _deadline_factory=factory)


@pytest.mark.asyncio
async def test_default_scope_reads_the_notebook_before_the_suggestion_call() -> None:
    """``source_ids=None`` resolves through ``GET_NOTEBOOK`` first, then suggests."""
    executor = _Executor(_SNAPSHOT, _SUGGESTIONS)

    result = await _api(executor).suggest_prompts(NOTEBOOK_ID, mode=7, query="steer")

    assert [suggestion.title for suggestion in result] == ["Title"]
    assert executor.methods == [RPCMethod.GET_NOTEBOOK, RPCMethod.SUGGEST_PROMPTS]

    read_method, read_params, read_kwargs = executor.calls[0]
    assert read_method is RPCMethod.GET_NOTEBOOK
    assert read_params == build_get_notebook_params(NOTEBOOK_ID)
    assert read_kwargs == {
        "source_path": f"/notebook/{NOTEBOOK_ID}",
        "allow_null": False,
        "_is_retry": False,
        "disable_internal_retries": False,
        "operation_variant": None,
        "read_timeout": None,
        "raise_on_null_status": False,
        "_retry_deadline": None,
    }

    suggest_method, suggest_params, suggest_kwargs = executor.calls[1]
    assert suggest_method is RPCMethod.SUGGEST_PROMPTS
    # The ids decoded from the snapshot are what the encoder nests.
    assert suggest_params == encode_prompt_suggestions(
        NOTEBOOK_ID, ["src_a", "src_b"], mode=7, query="steer"
    )
    assert suggest_kwargs == {
        "source_path": f"/notebook/{NOTEBOOK_ID}",
        "allow_null": True,
        "_is_retry": False,
        "disable_internal_retries": False,
        "operation_variant": None,
        "read_timeout": None,
        "raise_on_null_status": False,
        "_retry_deadline": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source_ids", [["only"], []])
async def test_a_supplied_scope_never_reads_the_notebook(source_ids: list[str]) -> None:
    """An explicit list — including the empty one — skips resolution entirely."""
    executor = _Executor(_SUGGESTIONS)

    await _api(executor).suggest_prompts(NOTEBOOK_ID, source_ids=source_ids)

    assert executor.methods == [RPCMethod.SUGGEST_PROMPTS]
    assert executor.calls[0][1] == encode_prompt_suggestions(NOTEBOOK_ID, source_ids)


@pytest.mark.asyncio
async def test_both_phases_share_one_deadline_identity_and_consume_the_budget() -> None:
    """The client timeout is captured once and spans the read plus the suggestion."""
    clock = [100.0]
    executor = _Executor(_SNAPSHOT, _SUGGESTIONS)
    executor.on_call = lambda method: clock.__setitem__(
        0, 104.0 if method is RPCMethod.GET_NOTEBOOK else clock[0]
    )
    factory = RuntimeDeadlineFactory.fixed(10.0, monotonic=lambda: clock[0])

    await _api(executor, factory).suggest_prompts(NOTEBOOK_ID)

    read_deadline = executor.calls[0][2]["_retry_deadline"]
    assert read_deadline is not None
    assert executor.calls[1][2]["_retry_deadline"] is read_deadline
    assert executor.calls[0][2]["read_timeout"] == pytest.approx(10.0)
    assert executor.calls[1][2]["read_timeout"] == pytest.approx(6.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "expected_warnings"),
    [
        pytest.param(
            [{"unexpected": True}],
            [
                "get_source_ids: notebook_data[0] shape unexpected for "
                f"{NOTEBOOK_ID} (schema drift?). top-type=dict"
            ],
            id="top-level-not-a-list",
        ),
        pytest.param(
            [["Notebook"]],
            [
                "get_source_ids: notebook_info has no sources slot for "
                f"{NOTEBOOK_ID} (schema drift?). len=1"
            ],
            id="no-sources-slot",
        ),
        pytest.param(
            [["Notebook", "not-a-list", NOTEBOOK_ID]],
            [f"get_source_ids: notebook_info[1] not list for {NOTEBOOK_ID} (schema drift?). len=3"],
            id="sources-not-a-list",
        ),
        pytest.param([["Notebook", None, NOTEBOOK_ID]], [], id="sources-null-is-silent"),
        pytest.param(None, [], id="degenerate-payload-is-silent"),
    ],
)
async def test_malformed_snapshot_keeps_its_warning_surface_and_suggests_with_no_ids(
    caplog: pytest.LogCaptureFixture,
    snapshot: Any,
    expected_warnings: list[str],
) -> None:
    """Every shape mismatch keeps its exact ``notebooklm._notebooks`` warning."""
    executor = _Executor(snapshot, _SUGGESTIONS)

    with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
        await _api(executor).suggest_prompts(NOTEBOOK_ID)

    records = [record for record in caplog.records if record.name == "notebooklm._notebooks"]
    assert [record.getMessage() for record in records] == expected_warnings
    assert executor.methods == [RPCMethod.GET_NOTEBOOK, RPCMethod.SUGGEST_PROMPTS]
    assert executor.calls[1][1] == encode_prompt_suggestions(NOTEBOOK_ID, [])


@pytest.mark.asyncio
async def test_degenerate_source_entries_are_skipped_not_fatal() -> None:
    """Well-formed ids survive alongside entries the row adapter cannot read."""
    snapshot = [["Notebook", [[["src_a"]], [], "junk", [[None]]], NOTEBOOK_ID]]
    executor = _Executor(snapshot, _SUGGESTIONS)

    await _api(executor).suggest_prompts(NOTEBOOK_ID)

    assert executor.calls[1][1] == encode_prompt_suggestions(NOTEBOOK_ID, ["src_a"])


@pytest.mark.asyncio
async def test_resolution_failure_raises_the_read_phase_public_exception() -> None:
    """A failing ``GET_NOTEBOOK`` surfaces as its own public error, un-suggested."""
    failure = ServerError("boom", method_id=RPCMethod.GET_NOTEBOOK.value)
    executor = _Executor(failure, _SUGGESTIONS)

    with pytest.raises(ServerError) as caught:
        await _api(executor).suggest_prompts(NOTEBOOK_ID)

    assert type(caught.value) is ServerError
    assert str(caught.value) == "boom"
    assert caught.value.method_id == RPCMethod.GET_NOTEBOOK.value
    assert executor.methods == [RPCMethod.GET_NOTEBOOK]
