"""Tests for the ``GenerationState`` str-Enum and its construction waist.

These pin three things the non-breaking typing refinement (#1345) depends on:

* ``GenerationState`` behaves exactly like the bare status strings it replaced
  (``==``, ``in``, ``str``/``f""``, ``json.dumps``, ``isinstance(_, str)``), and
  ``__str__``/``__repr__`` keep display + ``console.print`` output unchanged.
* The private ``_status_from_code`` waist covers every status code the API can
  emit (the *range pin*), honouring ``None -> PENDING``.
* The five producers partition the states correctly: ``REMOVED`` is emitted only
  by ``wait_for_completion``; ``poll_status`` and ``_parse_generation_result``
  never fabricate it (and the parsers never emit ``NOT_FOUND`` either).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest

import notebooklm
import notebooklm.types
from notebooklm._artifact.polling import ArtifactPollingService
from notebooklm._types.artifacts import _status_from_code
from notebooklm.cli.error_handler import _generation_status_extra
from notebooklm.exceptions import ArtifactTimeoutError
from notebooklm.rpc.types import _ARTIFACT_STATUS_MAP, ArtifactStatus, ArtifactTypeCode
from notebooklm.types import Artifact, GenerationState, GenerationStatus

# ---------------------------------------------------------------------------
# Export surface + module identity
# ---------------------------------------------------------------------------


def test_generation_state_exported_from_both_facades():
    assert notebooklm.GenerationState is GenerationState
    assert notebooklm.types.GenerationState is GenerationState


def test_generation_state_module_is_public_types():
    # The types.py __module__-rewrite makes it look like it lives in the
    # public facade (so pickle + repr advertise the stable path).
    assert GenerationState.__module__ == "notebooklm.types"


# ---------------------------------------------------------------------------
# str-Enum behaviour (the non-breaking contract)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("member", list(GenerationState))
def test_str_returns_bare_value(member: GenerationState):
    assert str(member) == member.value
    assert f"{member}" == member.value


@pytest.mark.parametrize("member", list(GenerationState))
def test_repr_matches_plain_string_repr(member: GenerationState):
    # console.print(status) at cli/artifact_cmd.py renders the GenerationStatus
    # dataclass via repr(); without this the status field would render as
    # "<GenerationState.COMPLETED: 'completed'>" instead of "'completed'".
    assert repr(member) == repr(member.value)


@pytest.mark.parametrize("member", list(GenerationState))
def test_member_is_a_str_instance(member: GenerationState):
    assert isinstance(member, str)


def test_generation_state_values_are_unique():
    # Iterate __members__ (not the class) so duplicate-value aliases — which
    # Enum silently collapses when iterating the class — are still detected.
    # A stray alias would shrink list(GenerationState) and silently weaken the
    # parametrized member tests above.
    values = [member.value for member in GenerationState.__members__.values()]
    assert len(values) == len(set(values))


def test_equality_and_membership_against_bare_strings():
    assert GenerationState.COMPLETED == "completed"
    assert GenerationState.FAILED in {"failed", "removed"}
    # Reverse operand order: a bare str on the left still compares equal.
    plain = "completed"
    assert plain == GenerationState.COMPLETED


def test_json_dumps_member_is_bare_string():
    assert json.dumps(GenerationState.COMPLETED) == '"completed"'


def test_generation_status_repr_unchanged_with_enum_status():
    enum_built = GenerationStatus(task_id="t", status=GenerationState.COMPLETED)
    str_built = GenerationStatus(task_id="t", status="completed")
    assert repr(enum_built) == repr(str_built)
    assert "'completed'" in repr(enum_built)
    assert "GenerationState" not in repr(enum_built)


def test_console_print_renders_enum_status_identically():
    from io import StringIO

    from rich.console import Console

    def render(status: GenerationStatus) -> str:
        buffer = StringIO()
        Console(file=buffer, width=200, no_color=True).print(status)
        return buffer.getvalue()

    enum_built = GenerationStatus(task_id="t", status=GenerationState.COMPLETED)
    str_built = GenerationStatus(task_id="t", status="completed")
    assert render(enum_built) == render(str_built)


def test_asdict_to_json_round_trips_to_bare_string():
    status = GenerationStatus(task_id="t", status=GenerationState.COMPLETED)
    dumped = json.dumps(asdict(status))
    assert json.loads(dumped)["status"] == "completed"


# ---------------------------------------------------------------------------
# Raw-string-constructed instances keep their predicates working
# ---------------------------------------------------------------------------


def test_raw_string_constructed_predicates():
    assert GenerationStatus(task_id="t", status="completed").is_complete is True
    assert GenerationStatus(task_id="t", status="failed").is_failed is True
    assert GenerationStatus(task_id="t", status="pending").is_pending is True
    assert GenerationStatus(task_id="t", status="in_progress").is_in_progress is True
    assert GenerationStatus(task_id="t", status="not_found").is_not_found is True
    assert GenerationStatus(task_id="t", status="removed").is_removed is True


def test_raw_string_rate_limited_chain():
    rate_limited = GenerationStatus(
        task_id="t",
        status="removed",
        error="Daily quota exceeded",
    )
    assert rate_limited.is_rate_limited is True

    code_based = GenerationStatus(
        task_id="t",
        status="failed",
        error_code="USER_DISPLAYABLE_ERROR",
    )
    assert code_based.is_rate_limited is True


def test_enum_constructed_predicates():
    assert GenerationStatus(task_id="t", status=GenerationState.COMPLETED).is_complete is True
    assert GenerationStatus(task_id="t", status=GenerationState.NOT_FOUND).is_not_found is True


# ---------------------------------------------------------------------------
# CLI --json and exception/error-handler serialization stay bare strings
# ---------------------------------------------------------------------------


def test_cli_json_dict_emits_bare_status_string():
    # Mirrors the dict built at cli/artifact_cmd.py before json output.
    status = GenerationStatus(task_id="t", status=GenerationState.COMPLETED, url="u")
    payload = {"task_id": status.task_id, "status": status.status, "url": status.url}
    assert json.loads(json.dumps(payload))["status"] == "completed"


def test_exception_status_history_joins_bare_strings():
    transitions = [
        GenerationStatus(task_id="t", status=GenerationState.PENDING),
        GenerationStatus(task_id="t", status=GenerationState.IN_PROGRESS),
        GenerationStatus(task_id="t", status=GenerationState.REMOVED),
    ]
    err = ArtifactTimeoutError("nb1", "task1", 1.0, status_transitions=transitions)
    assert err.status_history == ("pending", "in_progress", "removed")
    assert " -> ".join(err.status_history) == "pending -> in_progress -> removed"
    assert "GenerationState" not in str(err)


def test_error_handler_extra_serializes_bare_status():
    status = GenerationStatus(task_id="t", status=GenerationState.FAILED)
    extra = _generation_status_extra(status)
    assert json.loads(json.dumps(extra))["status"] == "failed"


# ---------------------------------------------------------------------------
# W3: range pin — _status_from_code can never raise on a real API code
# ---------------------------------------------------------------------------


def test_status_map_values_are_subset_of_generation_state():
    api_status_strings = set(_ARTIFACT_STATUS_MAP.values()) | {
        "unknown",
        "not_found",
        "removed",
    }
    state_values = {member.value for member in GenerationState}
    assert api_status_strings <= state_values


def test_status_from_code_covers_every_mapped_code():
    for code, expected in _ARTIFACT_STATUS_MAP.items():
        result = _status_from_code(code)
        assert isinstance(result, GenerationState)
        assert result.value == expected


def test_status_from_code_pins_every_backend_artifact_status_code():
    """#2127: each backend ``ArtifactStatus`` code maps to its own state.

    Codes 1/2 were transposed and 0/5/6 were unmodelled (all three collapsed
    into ``UNKNOWN``). Pin the whole table so a future transposition or a
    silently-dropped member fails here.
    """
    assert {
        code: _status_from_code(code) for code in (member.value for member in ArtifactStatus)
    } == {
        0: GenerationState.UNKNOWN,
        1: GenerationState.PENDING,
        2: GenerationState.IN_PROGRESS,
        3: GenerationState.COMPLETED,
        4: GenerationState.FAILED,
        5: GenerationState.SUGGESTED,
        6: GenerationState.PENDING_REVIEW,
    }


def test_is_terminal_partitions_the_enum_exactly():
    """``is_terminal`` is the single authority for "generation ended".

    Pinned as an exact partition so a member added later has to be classified
    deliberately here rather than silently inheriting a default. Everything
    non-terminal means "keep waiting" — including ``NOT_FOUND``, which is a
    transport-level absence rather than an outcome.
    """
    terminal = {state for state in GenerationState if state.is_terminal}
    assert terminal == {
        GenerationState.COMPLETED,
        GenerationState.FAILED,
        GenerationState.REMOVED,
    }
    assert {state for state in GenerationState if not state.is_terminal} == {
        GenerationState.PENDING,
        GenerationState.IN_PROGRESS,
        GenerationState.NOT_FOUND,
        GenerationState.UNKNOWN,
        GenerationState.SUGGESTED,
        GenerationState.PENDING_REVIEW,
    }


def test_is_terminal_equals_the_is_complete_or_is_failed_predicate_pair():
    """The enum partition matches the predicate pair ``_run_poll_loop`` stops on.

    Scope note: this compares two derivations, it does not drive the loop —
    ``_run_poll_loop`` itself returns on ``status.is_complete or
    status.is_failed`` (``REMOVED`` it synthesizes locally, so ``poll_status``
    can never hand it back). Pinning the pair is what keeps the enum's partition
    from drifting away from the condition the loop actually evaluates.
    """
    for state in GenerationState:
        if state is GenerationState.REMOVED:
            continue  # never emitted by poll_status; wait_for_completion makes it
        status = GenerationStatus(task_id="t", status=state)
        assert state.is_terminal == (status.is_complete or status.is_failed), state


def test_generation_status_is_terminal_matches_the_enum_for_every_state():
    """The dataclass predicate agrees with the enum for all nine states."""
    for state in GenerationState:
        assert GenerationStatus(task_id="t", status=state).is_terminal == state.is_terminal, state


def test_generation_status_is_terminal_works_on_a_plain_string_status():
    """``GenerationStatus.status`` is documented raw-string-permissive.

    ``is_terminal`` is the only predicate on this dataclass that resolves by
    *hashing* (``in`` against a frozenset) rather than by ``==`` alone, so the
    plain-string path needs its own pin — every sibling predicate would keep
    working even if this one silently stopped.
    """
    assert GenerationStatus(task_id="t", status="completed").is_terminal is True
    assert GenerationStatus(task_id="t", status="failed").is_terminal is True
    assert GenerationStatus(task_id="t", status="removed").is_terminal is True
    assert GenerationStatus(task_id="t", status="pending").is_terminal is False
    assert GenerationStatus(task_id="t", status="in_progress").is_terminal is False
    # An unmodelled string is not terminal either — it must not crash the branch.
    assert GenerationStatus(task_id="t", status="some_future_status").is_terminal is False


def test_generation_state_hashes_as_its_value_not_its_member_name():
    """The mechanism the plain-string path above rests on, pinned explicitly.

    ``str`` precedes ``Enum`` in ``GenerationState``'s MRO, so ``str.__hash__``
    wins over ``Enum.__hash__`` (which would hash the *member name*, e.g.
    ``"COMPLETED"``, and would silently break frozenset lookups by value).
    Reordering the bases would keep every ``==``-based predicate working and
    break only the ``in``-based one, so pin the cause, not just the symptom.
    """
    assert GenerationState.__mro__[1] is str
    assert hash(GenerationState.COMPLETED) == hash("completed")
    assert hash(GenerationState.COMPLETED) != hash("COMPLETED")
    assert "completed" in frozenset({GenerationState.COMPLETED})


def test_generation_outcome_terminality_agrees_with_the_enum():
    """``_app/generate_retry`` classifies terminality independently — keep it in step.

    It duck-types over ``is_complete`` / ``is_failed`` / ``is_removed`` so it can
    accept non-``GenerationStatus`` payloads, which means it cannot call
    ``is_terminal``. This pins that its three-way split still partitions the
    enum the same way, so the two cannot drift apart silently.
    """
    from notebooklm._app.generate_retry import generation_outcome_from_status

    for state in GenerationState:
        outcome = generation_outcome_from_status(
            GenerationStatus(task_id="t", status=state), "audio"
        )
        assert (outcome.status != "pending") == state.is_terminal, state


def test_rare_backend_states_are_distinguishable_from_unknown():
    """SUGGESTED / PENDING_REVIEW must not collapse into ``UNKNOWN`` (#2127)."""
    for code in (ArtifactStatus.SUGGESTED, ArtifactStatus.PENDING_REVIEW):
        assert _status_from_code(code) is not GenerationState.UNKNOWN
    # ...while a code the backend enum does not define still fails closed.
    assert _status_from_code(7) is GenerationState.UNKNOWN


def test_status_from_code_none_defaults_to_pending():
    assert _status_from_code(None) is GenerationState.PENDING


def test_status_from_code_none_status_override():
    assert _status_from_code(None, none_status=GenerationState.UNKNOWN) is GenerationState.UNKNOWN


def test_status_from_code_unknown_code_is_unknown():
    assert _status_from_code(9999) is GenerationState.UNKNOWN


def test_generation_state_rejects_unmapped_string():
    # The ValueError that _status_from_code's defensive branch guards against:
    # a status string with no matching member raises on direct construction.
    # The range pin above proves artifact_status_to_str never produces such a
    # string today, so the guard is purely future-drift insurance.
    with pytest.raises(ValueError):
        GenerationState("some_future_status")


def test_status_from_code_never_returns_wait_only_states():
    codes = [None, *_ARTIFACT_STATUS_MAP.keys(), 9999]
    for code in codes:
        assert _status_from_code(code) not in {
            GenerationState.NOT_FOUND,
            GenerationState.REMOVED,
        }


# ---------------------------------------------------------------------------
# W3: producer partition — poll_status / _parse_generation_result never emit
# REMOVED; only wait_for_completion does.
# ---------------------------------------------------------------------------


class _StubSupervisor:
    """Minimal supervisor stub for direct ``poll_status`` tests.

    ``poll_status`` never touches either collaborator, but the constructor
    requires them; a plain class avoids ``MagicMock`` blocking the
    ``assert_bound_loop`` attribute name.
    """

    bound_loop = None

    def assert_bound_loop(self) -> None:
        return None

    def register_drain_hook(self, _name: str, _hook: object) -> None:
        return None


def _make_polling_service() -> ArtifactPollingService:
    supervisor = _StubSupervisor()
    return ArtifactPollingService(
        supervisor=supervisor,  # type: ignore[arg-type]
        sleep=AsyncMock(),
        monotonic=lambda: 0.0,
    )


async def _poll_with_status_code(code: int | None) -> GenerationStatus:
    service = _make_polling_service()
    artifact = Artifact(
        id="task1",
        title="Title",
        _artifact_type=ArtifactTypeCode.INFOGRAPHIC.value,
        status=code,
        url="https://example.test/artifact" if code == ArtifactStatus.COMPLETED else None,
    )

    async def list_studio(_notebook_id: str, _task_id: str) -> list[Artifact]:
        return [artifact]

    return await service.poll_status(
        "nb1",
        "task1",
        list_studio=list_studio,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", list(_ARTIFACT_STATUS_MAP.keys()) + [9999])
async def test_poll_status_never_returns_removed(code: int):
    status = await _poll_with_status_code(code)
    assert status.status is not GenerationState.REMOVED


@pytest.mark.asyncio
async def test_poll_status_decoded_failed_artifact_has_no_fabricated_error():
    """Polling consumes decoded artifacts and never fabricates an error reason."""
    service = _make_polling_service()
    artifact = Artifact(
        id="task1",
        title="Title",
        _artifact_type=ArtifactTypeCode.REPORT.value,
        status=ArtifactStatus.FAILED,
    )

    async def list_studio(_notebook_id: str, _task_id: str) -> list[Artifact]:
        return [artifact]

    status = await service.poll_status(
        "nb1",
        "task1",
        list_studio=list_studio,
    )

    assert status.status is GenerationState.FAILED
    assert status.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ArtifactStatus.PENDING, GenerationState.PENDING),
        (ArtifactStatus.PROCESSING, GenerationState.IN_PROGRESS),
        (ArtifactStatus.SUGGESTED, GenerationState.SUGGESTED),
        (ArtifactStatus.PENDING_REVIEW, GenerationState.PENDING_REVIEW),
    ],
)
async def test_poll_status_non_terminal_codes_keep_the_loop_waiting(
    code: int, expected: GenerationState
):
    """#2127: the transitional + rare codes surface distinctly and stay non-terminal.

    ``_run_poll_loop`` only stops on ``is_complete``/``is_failed``, so a state
    that is neither keeps polling — the same behaviour codes 5/6 already had
    when they decoded as ``"unknown"``.
    """
    status = await _poll_with_status_code(code)
    assert status.status is expected
    assert not status.is_complete
    assert not status.is_failed


@pytest.mark.asyncio
async def test_poll_status_completed_code_maps_to_completed():
    status = await _poll_with_status_code(ArtifactStatus.COMPLETED)
    assert status.status is GenerationState.COMPLETED


@pytest.mark.asyncio
async def test_poll_status_missing_artifact_is_not_found_not_removed():
    service = _make_polling_service()

    async def list_studio(_notebook_id: str, _task_id: str) -> list[Artifact]:
        return []

    status = await service.poll_status(
        "nb1",
        "task1",
        list_studio=list_studio,
    )
    assert status.status is GenerationState.NOT_FOUND
    assert status.status is not GenerationState.REMOVED


@asynccontextmanager
async def _noop_operation_scope():
    yield None


def _make_parse_api():
    from notebooklm._web.artifacts import WebArtifactsAPI
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(
        rpc_call=AsyncMock(),
        get_source_ids=AsyncMock(return_value=[]),
        operation_scope=MagicMock(side_effect=lambda _label: _noop_operation_scope()),
    )
    notebooks = MagicMock()
    notebooks.get_source_ids = AsyncMock(return_value=[])
    return WebArtifactsAPI(
        rpc=core,
        supervisor=core,
        notebooks=notebooks,
        mind_maps=MagicMock(),
        note_service=MagicMock(),
    )


@pytest.mark.parametrize("code", list(_ARTIFACT_STATUS_MAP.keys()) + [9999, None])
def test_parse_generation_result_never_emits_wait_or_notfound_states(code):
    api = _make_parse_api()
    result = api._parse_generation_result(
        [["artifact_x", "Title", 7, None, code]], method_id="R7cb6c"
    )
    assert isinstance(result.status, GenerationState)
    assert result.status not in {GenerationState.REMOVED, GenerationState.NOT_FOUND}


def test_parse_generation_result_none_code_is_pending():
    api = _make_parse_api()
    result = api._parse_generation_result(
        [["artifact_x", "Title", 7, None, None]], method_id="R7cb6c"
    )
    assert result.status is GenerationState.PENDING
