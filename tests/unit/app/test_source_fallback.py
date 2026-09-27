"""Recovery must never turn uncertainty into an untracked create or deletion."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from notebooklm._app import source_fallback as fallback
from notebooklm._app.source_add import SourceAddExecutionPlan, SourceAddPlan, execute_source_add
from notebooklm._app.source_fetch import FetchedSource
from notebooklm.exceptions import ClientError, RPCError, SourceAddError, ValidationError
from notebooklm.outcomes import CommitState
from notebooklm.types import Source, SourceStatus

URL = "https://example.com/article"


@asynccontextmanager
async def operation(**kwargs):
    yield


def plan(**kwargs):
    return SourceAddExecutionPlan(
        "nb",
        SourceAddPlan(content=URL, detected_type="url", title=None, upload_path=None),
        **kwargs,
    )


def ghost(source_id="ghost", *, code=1, status=SourceStatus.ERROR):
    return Source(source_id, url=URL, status=status, experimental_failure_code=code)


@pytest.fixture
def setup(monkeypatch):
    original = SourceAddError(URL, cause=RPCError("rejected", rpc_code=9))
    sources = SimpleNamespace(
        list=AsyncMock(side_effect=[[], [ghost()]]),
        add_url=AsyncMock(side_effect=original),
        add_text=AsyncMock(return_value=Source("replacement", _type_code=4)),
        wait_until_ready=AsyncMock(return_value=Source("replacement")),
        get_or_none=AsyncMock(return_value=ghost()),
        delete_many_with_outcomes=AsyncMock(
            return_value=[
                SimpleNamespace(outcome=SimpleNamespace(commit_state=CommitState.CONFIRMED))
            ]
        ),
    )
    client = SimpleNamespace(sources=sources, operation=operation)
    fetch = AsyncMock(return_value=FetchedSource(URL, "Article", "real text " * 40))
    monkeypatch.setattr(fallback, "fetch_source", fetch)
    monkeypatch.setattr(fallback, "require_fetch_dependencies", lambda: None)
    return client, original, fetch


@pytest.mark.asyncio
async def test_disabled_preserves_existing_error_and_makes_no_extra_calls(setup):
    client, original, fetch = setup
    with pytest.raises(SourceAddError) as caught:
        await execute_source_add(client, plan())
    assert caught.value is original
    client.sources.list.assert_not_called()
    client.sources.add_text.assert_not_called()
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_normal_success_does_not_fetch_or_cleanup(setup):
    client, _, fetch = setup
    client.sources.add_url.side_effect = None
    client.sources.add_url.return_value = Source("web")
    result = await execute_source_add(client, plan(fallback_fetch=True, cleanup_on_failure=True))
    assert result.source.id == "web"
    assert not hasattr(result, "fallback")
    fetch.assert_not_called()
    client.sources.delete_many_with_outcomes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [None, 0, True, 3, 5, 6, 9, 100])
async def test_unknown_or_ineligible_cause_never_fetches(setup, code):
    client, original, fetch = setup
    client.sources.list.side_effect = [[], [ghost(code=code)]]
    with pytest.raises(SourceAddError) as caught:
        await execute_source_add(client, plan(fallback_fetch=True))
    assert caught.value is original
    fetch.assert_not_called()
    client.sources.add_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after", [[], [ghost(), ghost("other")], [ghost(status=SourceStatus.READY)]]
)
async def test_ambiguous_missing_or_successful_row_is_not_recovered(setup, after):
    client, original, fetch = setup
    client.sources.list.side_effect = [[], after]
    with pytest.raises(SourceAddError) as caught:
        await execute_source_add(client, plan(fallback_fetch=True))
    assert caught.value is original
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_old_processing_row_becoming_error_is_not_our_ghost(setup):
    client, _, fetch = setup
    client.sources.list.side_effect = [[ghost(status=SourceStatus.PROCESSING)], [ghost()]]
    with pytest.raises(SourceAddError):
        await execute_source_add(client, plan(fallback_fetch=True))
    fetch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", [RPCError("unknown"), RPCError("server", rpc_code=13)])
async def test_non_precondition_failure_does_not_reconcile_or_fetch(setup, cause):
    client, original, fetch = setup
    original.cause = cause
    with pytest.raises(SourceAddError):
        await execute_source_add(client, plan(fallback_fetch=True))
    assert client.sources.list.await_count == 1
    fetch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["readback", "fetch"])
async def test_recovery_failure_keeps_original_exception(setup, phase):
    client, original, fetch = setup
    if phase == "readback":
        client.sources.list.side_effect = [[], RPCError("read failed")]
    else:
        fetch.side_effect = ValueError("unsafe redirect or bad content")
    with pytest.raises(SourceAddError) as caught:
        await execute_source_add(client, plan(fallback_fetch=True))
    assert caught.value is original
    client.sources.add_text.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_reports_static_provenance_but_does_not_claim_ghost(setup):
    client, _, fetch = setup
    result = await execute_source_add(client, plan(fallback_fetch=True, cleanup_on_failure=True))
    assert result.source.id == "replacement"
    assert result.fallback.original_url == URL
    assert result.fallback.refreshable is False
    assert result.fallback.ghost_candidates == ("ghost",)
    assert result.fallback.cleanup == "skipped_unattributed"
    fetch.assert_awaited_once_with(URL)
    client.sources.add_text.assert_awaited_once()
    client.sources.delete_many_with_outcomes.assert_not_called()


@pytest.mark.asyncio
async def test_attributable_ghost_deleted_only_after_replacement_readiness(setup):
    client, original, _ = setup
    original.source_id = "ghost"
    result = await execute_source_add(client, plan(fallback_fetch=True, cleanup_on_failure=True))
    assert result.fallback.cleanup == "deleted"
    client.sources.wait_until_ready.assert_awaited_once_with("nb", "replacement")
    client.sources.delete_many_with_outcomes.assert_awaited_once_with("nb", ["ghost"])


@pytest.mark.asyncio
async def test_replacement_write_failure_is_not_masked_or_retried(setup):
    client, original, _ = setup
    original.source_id = "ghost"
    failure = SourceAddError("text", message="unconfirmed text create")
    client.sources.add_text.side_effect = failure
    with pytest.raises(SourceAddError) as caught:
        await execute_source_add(client, plan(fallback_fetch=True, cleanup_on_failure=True))
    assert caught.value is failure
    assert client.sources.add_text.await_count == 1
    client.sources.delete_many_with_outcomes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["wait", "delete", "changed", "unconfirmed"])
async def test_cleanup_failure_retains_successful_replacement(setup, phase):
    client, original, _ = setup
    original.source_id = "ghost"
    if phase == "wait":
        client.sources.wait_until_ready.side_effect = TimeoutError()
    elif phase == "delete":
        client.sources.delete_many_with_outcomes.side_effect = RPCError("unknown delete")
    elif phase == "changed":
        client.sources.get_or_none.return_value = ghost(status=SourceStatus.READY)
    else:
        client.sources.delete_many_with_outcomes.return_value[
            0
        ].outcome.commit_state = CommitState.UNKNOWN
    result = await execute_source_add(client, plan(fallback_fetch=True, cleanup_on_failure=True))
    assert result.source.id == "replacement"
    assert result.fallback.cleanup != "deleted"
    if phase in {"wait", "changed"}:
        client.sources.delete_many_with_outcomes.assert_not_called()


@pytest.mark.asyncio
async def test_options_validated_before_source_mutations(setup):
    client, _, _ = setup
    with pytest.raises(ValidationError):
        await execute_source_add(client, plan(cleanup_on_failure=True))
    client.sources.add_url.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["baseline", "readback", "fetch", "text", "wait", "delete"])
async def test_cancellation_propagates_without_replaying_writes(setup, phase):
    client, original, fetch = setup
    original.source_id = "ghost"
    error = asyncio.CancelledError()
    if phase == "baseline":
        client.sources.list.side_effect = error
    elif phase == "readback":
        client.sources.list.side_effect = [[], error]
    elif phase == "fetch":
        fetch.side_effect = error
    elif phase == "text":
        client.sources.add_text.side_effect = error
    elif phase == "wait":
        client.sources.wait_until_ready.side_effect = error
    else:
        client.sources.delete_many_with_outcomes.side_effect = error
    with pytest.raises(asyncio.CancelledError):
        await execute_source_add(client, plan(fallback_fetch=True, cleanup_on_failure=True))
    assert client.sources.add_text.await_count <= 1
    if phase != "delete":
        client.sources.delete_many_with_outcomes.assert_not_called()


@pytest.mark.asyncio
async def test_correlated_id_mismatch_never_recovers_another_callers_row(setup):
    client, original, fetch = setup
    original.source_id = "different-id"
    with pytest.raises(SourceAddError):
        await execute_source_add(client, plan(fallback_fetch=True))
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_missing_optional_dependency_fails_before_any_source_write(setup, monkeypatch):
    client, _, _ = setup

    def missing():
        raise ValidationError("missing dependency")

    monkeypatch.setattr(fallback, "require_fetch_dependencies", missing)
    with pytest.raises(ValidationError, match="dependency"):
        await execute_source_add(client, plan(fallback_fetch=True))
    client.sources.add_url.assert_not_called()
    client.sources.list.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [CommitState.CONFIRMED, CommitState.NOT_SENT])
async def test_positive_original_commit_evidence_prohibits_fallback(setup, state):
    from notebooklm._idempotency import mark_commit_state

    client, original, fetch = setup
    mark_commit_state(original, state)
    with pytest.raises(SourceAddError):
        await execute_source_add(client, plan(fallback_fetch=True))
    fetch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "content"),
    [
        ("text", "hello"),
        ("url", "https://youtube.com/watch?v=abc"),
        ("url", "https://user:secret@example.com/"),
    ],
)
async def test_incompatible_inputs_fail_before_baseline_read(setup, kind, content):
    client, _, _ = setup
    execution = SourceAddExecutionPlan(
        "nb",
        SourceAddPlan(content=content, detected_type=kind, title=None, upload_path=None),
        fallback_fetch=True,
    )
    with pytest.raises(ValidationError):
        await execute_source_add(client, execution)
    client.sources.list.assert_not_called()
    client.sources.add_url.assert_not_called()


@pytest.mark.asyncio
async def test_disappeared_ghost_does_not_delete_anything(setup):
    client, original, _ = setup
    original.source_id = "ghost"
    client.sources.get_or_none.return_value = None
    result = await execute_source_add(client, plan(fallback_fetch=True, cleanup_on_failure=True))
    assert result.fallback.cleanup == "already_absent"
    assert "not confirmed" not in result.fallback.warning
    client.sources.delete_many_with_outcomes.assert_not_called()


@pytest.mark.asyncio
async def test_android_receipt_recovers_url_less_ghost_and_cleans_after_ready(setup):
    client, original, fetch = setup
    original.cause = ClientError("failed precondition", rpc_code=9)
    original.source_id = "ghost"
    row = Source("ghost", status=SourceStatus.ERROR, experimental_failure_code=1)
    client.sources.list.side_effect = [[], [row]]
    client.sources.get_or_none.return_value = row
    result = await execute_source_add(client, plan(fallback_fetch=True, cleanup_on_failure=True))
    assert result.source.id == "replacement"
    assert result.fallback.cleanup == "deleted"
    fetch.assert_awaited_once_with(URL)
    client.sources.wait_until_ready.assert_awaited_once_with("nb", "replacement")
    client.sources.delete_many_with_outcomes.assert_awaited_once_with("nb", ["ghost"])


@pytest.mark.asyncio
@pytest.mark.parametrize("owned_id", [None, "ghost"])
async def test_missing_attribution_or_conflicting_url_is_not_recovered(setup, owned_id):
    client, original, fetch = setup
    original.source_id = owned_id
    row = Source(
        "ghost",
        url="https://different.example/" if owned_id else None,
        status=SourceStatus.ERROR,
        experimental_failure_code=1,
    )
    client.sources.list.side_effect = [[], [row]]
    with pytest.raises(SourceAddError):
        await execute_source_add(client, plan(fallback_fetch=True))
    fetch.assert_not_called()
