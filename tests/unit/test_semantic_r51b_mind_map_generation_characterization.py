"""P10 R5.1b characterizations for the two ``mind_map.generate_*`` source scopes.

Pins the observable behaviour of both mind-map generation families *before*
their conditional ``GET_NOTEBOOK`` read moves above the port (decision D1 option
(a)), so the move can only preserve it:

* the read order and the exact conditional — ``source_ids=None`` resolves,
  ``[]`` and an explicit list do not;
* the encoded ``GENERATE_MIND_MAP`` / ``CREATE_ARTIFACT`` params and the wire
  kwargs of every phase, which must stay byte-identical (digest-pinned);
* the note-backed language default, which resolves ``None`` through
  ``NOTEBOOKLM_HL`` and passes an explicit value straight through;
* the diagnostics surface of a malformed snapshot, which for *both* mind-map
  families is silence — unlike the prompt-suggestion and Studio generation
  families, these two log nothing at all on ``notebooklm._notebooks``;
* the aggregate client-timeout budget: one :class:`RuntimeDeadline` identity
  shared by the read and the generation native;
* the public exceptions — the one a failing resolution raises, and the
  feature-unavailable identity the interactive family raises when
  ``CREATE_ARTIFACT`` allocates no id.

Three surfaces reach these rows and all three are pinned here:
``client.mind_maps.generate(kind=NOTE_BACKED)`` through :class:`NoteService`,
``client.mind_maps.generate(kind=INTERACTIVE)`` through
:class:`MindMapFamilyService`, and ``client.artifacts.generate_mind_map``
through :class:`NoteBackedMindMapFamilyService`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, cast

import pytest

from notebooklm._backend_compat import project_backend_call
from notebooklm._deadline import RuntimeDeadlineFactory
from notebooklm._mind_maps_api import MindMapsAPI
from notebooklm._note_service import NoteService
from notebooklm._notebook_payloads import build_get_notebook_params
from notebooklm._records import MindMapGenerateInput
from notebooklm._rpc_executor import RpcExecutor
from notebooklm._studio import MindMapFamilyService, NoteBackedMindMapFamilyService, StudioCatalog
from notebooklm._types.mind_maps import MindMapKind
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.codec.artifact_payloads import (
    build_interactive_mind_map_artifact_params,
    build_mind_map_params,
)
from notebooklm.exceptions import ArtifactFeatureUnavailableError, ServerError
from notebooklm.rpc import RPCMethod

NOTEBOOK_ID = "nb-r51b"

#: One well-formed ``GET_NOTEBOOK`` snapshot carrying two embedded source ids.
_SNAPSHOT: Any = [["Notebook", [[["src_a"]], [["src_b"]]], NOTEBOOK_ID]]
#: ``GENERATE_MIND_MAP``'s wrapped optional JSON leaf.
_GENERATED: Any = [['{"name": "Tree"}']]
#: ``CREATE_NOTE``'s allocated identity, then ``UPDATE_NOTE``'s empty echo.
_NOTE_CREATED: Any = [["note-1"]]
_NOTE_UPDATED: Any = None
#: ``CREATE_ARTIFACT``'s allocated interactive identity.
_ARTIFACT_CREATED: Any = [["mm-1"]]

#: SHA-256 of the two generation bodies this slice must not move. Recomputed
#: from the encoder rather than from the row, so the digest survives the hoist.
_GENERATE_MIND_MAP_DIGEST = "726d423b79261584f7003683fbd1a78f4600e391a36ebc52629c414deaf17be5"
_CREATE_ARTIFACT_DIGEST = "17d7553224e613e7a6ed25584d8a371c9c5ec0d5d05fd354e4e4e209339eb7d7"

_BASE_KWARGS: dict[str, Any] = {
    "allow_null": False,
    "_is_retry": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "read_timeout": None,
    "raise_on_null_status": False,
    "_retry_deadline": None,
}


def _kwargs(**overrides: Any) -> dict[str, Any]:
    return {**_BASE_KWARGS, "source_path": f"/notebook/{NOTEBOOK_ID}", **overrides}


def _digest(params: list[Any]) -> str:
    """Stable digest of one encoded request body, independent of the encoder."""
    return hashlib.sha256(json.dumps(params, separators=(",", ":")).encode()).hexdigest()


@dataclass
class _Call:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _Executor:
    """Narrow ``rpc_call`` recorder that replays a scripted response sequence."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []
        self.on_call: Any = None

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method=method, params=params, kwargs=kwargs))
        if self.on_call is not None:
            self.on_call(method)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def methods(self) -> list[RPCMethod]:
        return [call.method for call in self.calls]


async def _never_waited(notebook_id: str, mind_map_id: str) -> object:  # pragma: no cover
    raise AssertionError("wait=False must not poll for completion")


def _backend(
    executor: _Executor,
    factory: RuntimeDeadlineFactory | None = None,
) -> WebRpcBackend:
    return WebRpcBackend(cast(RpcExecutor, executor), deadline_factory=factory)


def _mind_maps(
    executor: _Executor,
    factory: RuntimeDeadlineFactory | None = None,
) -> MindMapsAPI:
    """Assemble ``client.mind_maps`` exactly as the composition root does."""
    backend = _backend(executor, factory)
    return MindMapsAPI(
        notes=NoteService(backend),
        studio=MindMapFamilyService(
            backend,
            StudioCatalog(backend, deadline_factory=factory),
            wait_for_completion=_never_waited,
            deadline_factory=factory,
        ),
    )


def _artifact_family(
    executor: _Executor,
    factory: RuntimeDeadlineFactory | None = None,
) -> NoteBackedMindMapFamilyService:
    """The service behind ``client.artifacts.generate_mind_map``."""
    backend = _backend(executor, factory)
    return NoteBackedMindMapFamilyService(
        backend,
        StudioCatalog(backend, deadline_factory=factory),
        deadline_factory=factory,
    )


# --- note-backed family: client.mind_maps.generate(kind=NOTE_BACKED) ---------------


@pytest.mark.asyncio
async def test_note_backed_default_scope_reads_the_notebook_before_generating() -> None:
    """``source_ids=None`` resolves through ``GET_NOTEBOOK`` first, then generates."""
    executor = _Executor(_SNAPSHOT, _GENERATED, _NOTE_CREATED, _NOTE_UPDATED)

    result = await _mind_maps(executor).generate(
        NOTEBOOK_ID, None, kind=MindMapKind.NOTE_BACKED, language="fr", instructions="Focus"
    )

    assert result.id == "note-1"
    assert executor.methods == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.GENERATE_MIND_MAP,
        RPCMethod.CREATE_NOTE,
        RPCMethod.UPDATE_NOTE,
    ]

    read, generate = executor.calls[0], executor.calls[1]
    assert read.params == build_get_notebook_params(NOTEBOOK_ID)
    assert read.kwargs == _kwargs()
    # The ids decoded from the snapshot are what the generation encoder nests.
    assert generate.params == build_mind_map_params(
        ["src_a", "src_b"], language="fr", instructions="Focus"
    )
    assert generate.kwargs == _kwargs(allow_null=True)
    assert _digest(generate.params) == _GENERATE_MIND_MAP_DIGEST


@pytest.mark.asyncio
@pytest.mark.parametrize("source_ids", [["only"], []])
async def test_note_backed_supplied_scope_never_reads_the_notebook(
    source_ids: list[str],
) -> None:
    """An explicit list — including the empty one — skips resolution entirely."""
    executor = _Executor(_GENERATED, _NOTE_CREATED, _NOTE_UPDATED)

    await _mind_maps(executor).generate(
        NOTEBOOK_ID, source_ids, kind=MindMapKind.NOTE_BACKED, language="en"
    )

    assert executor.methods == [
        RPCMethod.GENERATE_MIND_MAP,
        RPCMethod.CREATE_NOTE,
        RPCMethod.UPDATE_NOTE,
    ]
    assert executor.calls[0].params == build_mind_map_params(
        source_ids, language="en", instructions=None
    )


@pytest.mark.asyncio
async def test_note_backed_language_none_resolves_the_environment_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``language=None`` is expanded below the port today from ``NOTEBOOKLM_HL``."""
    monkeypatch.setenv("NOTEBOOKLM_HL", "de")
    executor = _Executor(_GENERATED, _NOTE_CREATED, _NOTE_UPDATED)

    await _mind_maps(executor).generate(
        NOTEBOOK_ID, ["s1"], kind=MindMapKind.NOTE_BACKED, language=None
    )

    assert executor.calls[0].params == build_mind_map_params(
        ["s1"], language="de", instructions=None
    )


@pytest.mark.asyncio
async def test_note_backed_malformed_snapshot_is_silent_and_generates_with_no_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The mind-map families report nothing about a snapshot they cannot read."""
    executor = _Executor([{"unexpected": True}], _GENERATED, _NOTE_CREATED, _NOTE_UPDATED)

    with caplog.at_level(logging.WARNING):
        await _mind_maps(executor).generate(NOTEBOOK_ID, None, kind=MindMapKind.NOTE_BACKED)

    assert [record for record in caplog.records if record.name == "notebooklm._notebooks"] == []
    assert executor.calls[1].params == build_mind_map_params([], language="en", instructions=None)


@pytest.mark.asyncio
async def test_note_backed_degenerate_source_entries_are_skipped_not_fatal() -> None:
    """Well-formed ids survive alongside entries the row adapter cannot read."""
    snapshot = [["Notebook", [[["src_a"]], [], "junk", [[None]]], NOTEBOOK_ID]]
    executor = _Executor(snapshot, _GENERATED, _NOTE_CREATED, _NOTE_UPDATED)

    await _mind_maps(executor).generate(NOTEBOOK_ID, None, kind=MindMapKind.NOTE_BACKED)

    assert executor.calls[1].params == build_mind_map_params(
        ["src_a"], language="en", instructions=None
    )


@pytest.mark.asyncio
async def test_note_backed_resolution_failure_raises_the_read_phase_exception() -> None:
    """A failing ``GET_NOTEBOOK`` surfaces as its own error, nothing generated."""
    executor = _Executor(ServerError("boom", method_id=RPCMethod.GET_NOTEBOOK.value))

    with pytest.raises(ServerError) as caught:
        await _mind_maps(executor).generate(NOTEBOOK_ID, None, kind=MindMapKind.NOTE_BACKED)

    assert type(caught.value) is ServerError
    assert caught.value.method_id == RPCMethod.GET_NOTEBOOK.value
    assert executor.methods == [RPCMethod.GET_NOTEBOOK]


@pytest.mark.asyncio
async def test_note_backed_read_and_generation_share_one_deadline_identity() -> None:
    """The client timeout is captured once and spans the read plus the generation."""
    clock = [100.0]
    executor = _Executor(_SNAPSHOT, _GENERATED, _NOTE_CREATED, _NOTE_UPDATED)
    executor.on_call = lambda method: clock.__setitem__(
        0, 104.0 if method is RPCMethod.GET_NOTEBOOK else clock[0]
    )
    factory = RuntimeDeadlineFactory.fixed(10.0, monotonic=lambda: clock[0])

    await _mind_maps(executor, factory).generate(NOTEBOOK_ID, None, kind=MindMapKind.NOTE_BACKED)

    read_deadline = executor.calls[0].kwargs["_retry_deadline"]
    assert read_deadline is not None
    assert executor.calls[1].kwargs["_retry_deadline"] is read_deadline
    assert executor.calls[0].kwargs["read_timeout"] == pytest.approx(10.0)
    assert executor.calls[1].kwargs["read_timeout"] == pytest.approx(6.0)
    # The note persistence leaves sit outside the generation budget today.
    assert executor.calls[2].kwargs["_retry_deadline"] is None
    assert executor.calls[3].kwargs["_retry_deadline"] is None


# --- note-backed family: client.artifacts.generate_mind_map ------------------------


@pytest.mark.asyncio
async def test_artifact_default_scope_reads_the_notebook_before_generating() -> None:
    """The Studio workflow resolves the same scope over the same wire."""
    executor = _Executor(_SNAPSHOT, _GENERATED, _NOTE_CREATED, _NOTE_UPDATED)

    result = await _artifact_family(executor).generate(
        MindMapGenerateInput(NOTEBOOK_ID, None, "fr", "Focus")
    )

    assert result.note_id == "note-1"
    assert result.mind_map == {"name": "Tree"}
    assert executor.methods == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.GENERATE_MIND_MAP,
        RPCMethod.CREATE_NOTE,
        RPCMethod.UPDATE_NOTE,
    ]
    assert executor.calls[0].params == build_get_notebook_params(NOTEBOOK_ID)
    assert executor.calls[0].kwargs == _kwargs()
    assert executor.calls[1].params == build_mind_map_params(
        ["src_a", "src_b"], language="fr", instructions="Focus"
    )
    assert executor.calls[1].kwargs == _kwargs(allow_null=True)
    assert _digest(executor.calls[1].params) == _GENERATE_MIND_MAP_DIGEST


@pytest.mark.asyncio
async def test_artifact_workflow_shares_one_deadline_across_read_and_generation() -> None:
    """One workflow budget already covers the read, the generation and both notes."""
    clock = [100.0]
    executor = _Executor(_SNAPSHOT, _GENERATED, _NOTE_CREATED, _NOTE_UPDATED)
    executor.on_call = lambda method: clock.__setitem__(
        0, 104.0 if method is RPCMethod.GET_NOTEBOOK else clock[0]
    )
    factory = RuntimeDeadlineFactory.fixed(10.0, monotonic=lambda: clock[0])

    await _artifact_family(executor, factory).generate(
        MindMapGenerateInput(NOTEBOOK_ID, None, "en", None)
    )

    deadline = executor.calls[0].kwargs["_retry_deadline"]
    assert deadline is not None
    assert [call.kwargs["_retry_deadline"] for call in executor.calls] == [deadline] * 4
    assert executor.calls[1].kwargs["read_timeout"] == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_artifact_malformed_snapshot_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The Studio workflow inherits the same silence about an unreadable snapshot."""
    executor = _Executor(
        [["Notebook", "not-a-list", NOTEBOOK_ID]], _GENERATED, _NOTE_CREATED, _NOTE_UPDATED
    )

    with caplog.at_level(logging.WARNING):
        await _artifact_family(executor).generate(
            MindMapGenerateInput(NOTEBOOK_ID, None, "en", None)
        )

    assert [record for record in caplog.records if record.name == "notebooklm._notebooks"] == []
    assert executor.calls[1].params == build_mind_map_params([], language="en", instructions=None)


@pytest.mark.asyncio
async def test_artifact_resolution_failure_keeps_the_workflow_public_identity() -> None:
    """A failing read is re-attributed to ``artifact.generate_mind_map``, as today."""
    executor = _Executor(ServerError("boom", method_id=RPCMethod.GET_NOTEBOOK.value))

    with pytest.raises(ServerError) as caught:
        await project_backend_call(
            _artifact_family(executor).generate(MindMapGenerateInput(NOTEBOOK_ID, None, "en", None))
        )

    assert type(caught.value) is ServerError
    assert executor.methods == [RPCMethod.GET_NOTEBOOK]


# --- interactive family: client.mind_maps.generate(kind=INTERACTIVE) ---------------


@pytest.mark.asyncio
async def test_interactive_default_scope_reads_the_notebook_before_creating() -> None:
    """``source_ids=None`` resolves through ``GET_NOTEBOOK`` first, then creates."""
    executor = _Executor(_SNAPSHOT, _ARTIFACT_CREATED, [], [])

    mind_map = await _mind_maps(executor).generate(
        NOTEBOOK_ID, None, kind=MindMapKind.INTERACTIVE, instructions="Focus", wait=False
    )

    assert mind_map.id == "mm-1"
    assert executor.methods[:2] == [RPCMethod.GET_NOTEBOOK, RPCMethod.CREATE_ARTIFACT]
    assert executor.calls[0].params == build_get_notebook_params(NOTEBOOK_ID)
    assert executor.calls[0].kwargs == _kwargs()
    assert executor.calls[1].params == build_interactive_mind_map_artifact_params(
        NOTEBOOK_ID, ["src_a", "src_b"], instructions="Focus"
    )
    assert executor.calls[1].kwargs == _kwargs(allow_null=True)
    assert _digest(executor.calls[1].params) == _CREATE_ARTIFACT_DIGEST


@pytest.mark.asyncio
@pytest.mark.parametrize("source_ids", [["only"], []])
async def test_interactive_supplied_scope_never_reads_the_notebook(
    source_ids: list[str],
) -> None:
    """An explicit list — including the empty one — skips resolution entirely."""
    executor = _Executor(_ARTIFACT_CREATED, [], [])

    await _mind_maps(executor).generate(
        NOTEBOOK_ID, source_ids, kind=MindMapKind.INTERACTIVE, wait=False
    )

    assert executor.methods[0] is RPCMethod.CREATE_ARTIFACT
    assert executor.calls[0].params == build_interactive_mind_map_artifact_params(
        NOTEBOOK_ID, source_ids, instructions=None
    )


@pytest.mark.asyncio
async def test_interactive_malformed_snapshot_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The interactive family reports nothing about a snapshot it cannot read."""
    executor = _Executor([["Notebook"]], _ARTIFACT_CREATED, [], [])

    with caplog.at_level(logging.WARNING):
        await _mind_maps(executor).generate(
            NOTEBOOK_ID, None, kind=MindMapKind.INTERACTIVE, wait=False
        )

    assert [record for record in caplog.records if record.name == "notebooklm._notebooks"] == []
    assert executor.calls[1].params == build_interactive_mind_map_artifact_params(
        NOTEBOOK_ID, [], instructions=None
    )


@pytest.mark.asyncio
async def test_interactive_read_and_creation_share_one_deadline_identity() -> None:
    """The client timeout is captured once and spans the read plus the creation."""
    clock = [100.0]
    executor = _Executor(_SNAPSHOT, _ARTIFACT_CREATED, [], [])
    executor.on_call = lambda method: clock.__setitem__(
        0, 104.0 if method is RPCMethod.GET_NOTEBOOK else clock[0]
    )
    factory = RuntimeDeadlineFactory.fixed(10.0, monotonic=lambda: clock[0])

    await _mind_maps(executor, factory).generate(
        NOTEBOOK_ID, None, kind=MindMapKind.INTERACTIVE, wait=False
    )

    deadline = executor.calls[0].kwargs["_retry_deadline"]
    assert deadline is not None
    assert executor.calls[1].kwargs["_retry_deadline"] is deadline
    assert executor.calls[0].kwargs["read_timeout"] == pytest.approx(10.0)
    assert executor.calls[1].kwargs["read_timeout"] == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_interactive_creation_without_an_id_raises_feature_unavailable() -> None:
    """``CREATE_ARTIFACT`` allocating no identity is a closed public failure."""
    executor = _Executor(None)

    with pytest.raises(ArtifactFeatureUnavailableError) as caught:
        await _mind_maps(executor).generate(
            NOTEBOOK_ID, ["s1"], kind=MindMapKind.INTERACTIVE, wait=False
        )

    assert caught.value.method_id == RPCMethod.CREATE_ARTIFACT.value
    assert executor.methods == [RPCMethod.CREATE_ARTIFACT]


@pytest.mark.asyncio
async def test_interactive_resolution_failure_raises_the_read_phase_exception() -> None:
    """A failing ``GET_NOTEBOOK`` surfaces as its own error, nothing created."""
    executor = _Executor(ServerError("boom", method_id=RPCMethod.GET_NOTEBOOK.value))

    with pytest.raises(ServerError) as caught:
        await _mind_maps(executor).generate(
            NOTEBOOK_ID, None, kind=MindMapKind.INTERACTIVE, wait=False
        )

    assert type(caught.value) is ServerError
    assert caught.value.method_id == RPCMethod.GET_NOTEBOOK.value
    assert executor.methods == [RPCMethod.GET_NOTEBOOK]
