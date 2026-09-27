"""Recovery must never turn uncertainty into an untracked create or deletion."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from notebooklm._app import source_fallback as fallback
from notebooklm._app.source_add import SourceAddExecutionPlan, SourceAddPlan, execute_source_add
from notebooklm._app.source_fetch import FetchedSource
from notebooklm.exceptions import (
    AuthError,
    ClientError,
    DecodingError,
    RPCError,
    ServerError,
    SourceAddError,
    ValidationError,
)
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
        list=AsyncMock(side_effect=[[], [ghost()], [ghost()]]),
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
@pytest.mark.parametrize(
    ("phase", "reason"),
    [
        ("ineligible", "import_not_eligible"),
        ("readback", "readback_failed"),
        ("missing", "candidate_missing"),
        ("ambiguous", "candidate_ambiguous"),
        ("diagnostic", "nonconnection_diagnostic"),
        ("healthy", "nonconnection_diagnostic"),
        ("fetch", "fetch_failed error_type=ValueError"),
    ],
)
async def test_skip_logs_safe_reason_and_preserves_original_error(setup, caplog, phase, reason):
    from notebooklm._idempotency import mark_commit_state

    client, original, fetch = setup
    secret = "private-response-body https://user:secret@example.com/"
    if phase == "ineligible":
        original.cause = RPCError(secret, rpc_code=13)
    elif phase == "readback":
        client.sources.list.side_effect = [[], RPCError(secret)]
    elif phase == "missing":
        client.sources.list.side_effect = [[], []]
    elif phase == "ambiguous":
        client.sources.list.side_effect = [[], [ghost(), ghost("other")]]
    elif phase == "diagnostic":
        client.sources.list.side_effect = [[], [ghost(code=3)]]
    elif phase == "healthy":
        client.sources.list.side_effect = [[], [ghost(status=SourceStatus.READY)]]
    else:
        fetch.side_effect = ValueError(secret)
    mark_commit_state(original, CommitState.UNKNOWN, operation="sources.add_url", stage="commit")
    cause = original.cause
    metadata = original.operation_metadata
    attributes = original.__dict__.copy()
    args = original.args

    with (
        caplog.at_level("WARNING", logger=fallback.__name__),
        pytest.raises(SourceAddError) as caught,
    ):
        await execute_source_add(client, plan(fallback_fetch=True))

    assert caught.value is original
    assert original.cause is cause
    assert original.operation_metadata is metadata
    assert original.__dict__ == attributes
    assert original.args == args
    assert caplog.messages == [f"URL fallback skipped: reason={reason}"]
    assert secret not in caplog.text
    assert URL not in caplog.text
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
@pytest.mark.parametrize(
    "phase", ["baseline", "readback", "fetch", "revalidation", "text", "wait", "delete"]
)
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
    elif phase == "revalidation":
        client.sources.list.side_effect = [[], [ghost()], error]
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
    client, _, fetch = setup
    original = ClientError("failed precondition", rpc_code=9)
    original.source_id = "ghost"
    original.stage = "source commit"
    client.sources.add_url.side_effect = original
    row = Source("ghost", status=SourceStatus.ERROR, experimental_failure_code=1)
    client.sources.list.side_effect = [[], [row], [row]]
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after_fetch", "reason"),
    [
        ([ghost(status=SourceStatus.READY)], "nonconnection_diagnostic"),
        ([], "candidate_missing"),
        ([ghost(code=3)], "nonconnection_diagnostic"),
        ([ghost(), ghost("other")], "candidate_ambiguous"),
        ([ghost("other")], "candidate_changed"),
        (RPCError("private readback details"), "readback_failed"),
    ],
)
async def test_changed_roster_during_fetch_preserves_original_failure(
    setup, caplog, after_fetch, reason
):
    from notebooklm._idempotency import mark_commit_state

    client, original, fetch = setup
    mark_commit_state(original, CommitState.UNKNOWN, operation="sources.add_url", stage="commit")
    attributes = original.__dict__.copy()
    metadata = original.operation_metadata
    client.sources.list.side_effect = [[], [ghost()], after_fetch]
    with (
        caplog.at_level("WARNING", logger=fallback.__name__),
        pytest.raises(SourceAddError) as caught,
    ):
        await execute_source_add(client, plan(fallback_fetch=True, cleanup_on_failure=True))
    assert caught.value is original
    assert original.__dict__ == attributes
    assert original.operation_metadata is metadata
    assert caplog.messages == [f"URL fallback skipped: reason={reason}"]
    fetch.assert_awaited_once_with(URL)
    assert client.sources.list.await_count == 3
    assert all(call.kwargs == {"strict": True} for call in client.sources.list.await_args_list)
    client.sources.add_text.assert_not_called()
    client.sources.delete_many_with_outcomes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [None, True, 3])
async def test_cleanup_retains_ghost_if_connection_diagnostic_changed(setup, code):
    client, original, _ = setup
    original.source_id = "ghost"
    client.sources.get_or_none.return_value = ghost(code=code)
    result = await execute_source_add(client, plan(fallback_fetch=True, cleanup_on_failure=True))
    assert result.source.id == "replacement"
    assert result.fallback.cleanup == "skipped_changed"
    client.sources.delete_many_with_outcomes.assert_not_called()


@pytest.mark.asyncio
async def test_android_candidate_acquiring_conflicting_url_during_fetch_is_not_recovered(setup):
    client, original, fetch = setup
    original.source_id = "ghost"
    initial = Source("ghost", status=SourceStatus.ERROR, experimental_failure_code=1)
    changed = Source(
        "ghost",
        url="https://different.example/",
        status=SourceStatus.ERROR,
        experimental_failure_code=1,
    )
    client.sources.list.side_effect = [[], [initial], [changed]]
    with pytest.raises(SourceAddError) as caught:
        await execute_source_add(client, plan(fallback_fetch=True))
    assert caught.value is original
    fetch.assert_awaited_once_with(URL)
    client.sources.add_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("add_succeeds", [True, False])
@pytest.mark.parametrize("baseline_error", [RPCError("transient read"), DecodingError("bad row")])
async def test_unavailable_baseline_never_blocks_normal_add(
    setup, caplog, add_succeeds, baseline_error
):
    """A failed optional snapshot disables recovery, not the original import."""
    client, original, fetch = setup
    client.sources.list.side_effect = baseline_error
    metadata = original.operation_metadata
    cause = original.cause
    if add_succeeds:
        client.sources.add_url.side_effect = None
        client.sources.add_url.return_value = Source("web")
        result = await execute_source_add(
            client, plan(fallback_fetch=True, cleanup_on_failure=True)
        )
        assert result.source.id == "web"
        assert not hasattr(result, "fallback")
    else:
        with pytest.raises(SourceAddError) as caught:
            await execute_source_add(client, plan(fallback_fetch=True, cleanup_on_failure=True))
        assert caught.value is original
        assert original.operation_metadata is metadata
        assert original.cause is cause
    client.sources.add_url.assert_awaited_once_with("nb", URL)
    client.sources.list.assert_awaited_once_with("nb", strict=True)
    fetch.assert_not_called()
    client.sources.add_text.assert_not_called()
    client.sources.delete_many_with_outcomes.assert_not_called()
    assert caplog.messages == ["URL fallback unavailable: reason=baseline_failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [True, False])
@pytest.mark.parametrize(
    ("requested", "reported"),
    [
        ("https://EXAMPLE.com:443", "https://example.com/"),
        ("http://example.com:80/article", "http://EXAMPLE.com/article"),
        ("https://[2001:4860:4860:0:0:0:0:8888]/", "https://[2001:4860:4860::8888]/"),
        ("https://example.com/a%2fb?q=%3a#section", "https://example.com/a%2Fb?q=%3A"),
        ("https://faß.de/", "https://xn--fa-hia.de/"),
    ],
)
async def test_canonical_url_spellings_recover_and_cleanup(setup, owned, requested, reported):
    """Matching and both rechecks tolerate equivalent backend URL spellings."""
    client, original, fetch = setup
    if owned:
        original.source_id = "ghost"
    row = Source("ghost", url=reported, status=SourceStatus.ERROR, experimental_failure_code=1)
    client.sources.list.side_effect = [[], [row], [row]]
    client.sources.get_or_none.return_value = Source(
        "ghost", url=requested, status=SourceStatus.ERROR, experimental_failure_code=1
    )
    execution = SourceAddExecutionPlan(
        "nb",
        SourceAddPlan(content=requested, detected_type="url", title=None, upload_path=None),
        fallback_fetch=True,
        cleanup_on_failure=True,
    )
    result = await execute_source_add(client, execution)
    assert result.source.id == "replacement"
    assert result.fallback.original_url == requested
    assert result.fallback.cleanup == ("deleted" if owned else "skipped_unattributed")
    fetch.assert_awaited_once_with(requested)
    if owned:
        client.sources.delete_many_with_outcomes.assert_awaited_once_with("nb", ["ghost"])
    else:
        client.sources.delete_many_with_outcomes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [True, False])
@pytest.mark.parametrize(
    "reported",
    [
        "http://example.com/a%2Fb?x=1&y=2",  # Scheme is significant.
        "https://example.com:444/a%2Fb?x=1&y=2",  # Non-default ports differ.
        "https://other.example/a%2Fb?x=1&y=2",
        "https://example.com/a/b?x=1&y=2",  # Never decode path delimiters.
        "https://example.com/a%2Fb?y=2&x=1",  # Query order may be significant.
        "https://example.com/A%2Fb?x=1&y=2",  # Path case is significant.
        "https://example.com:bad/a%2Fb?x=1&y=2",
        "https://user@example.com/a%2Fb?x=1&y=2",
    ],
)
async def test_different_or_malformed_urls_do_not_authorize_recovery(setup, owned, reported):
    """URL normalization never widens matching to different resources."""
    client, original, fetch = setup
    if owned:
        original.source_id = "ghost"
    requested = "https://example.com/a%2Fb?x=1&y=2"
    client.sources.list.side_effect = [
        [],
        [Source("ghost", url=reported, status=SourceStatus.ERROR, experimental_failure_code=1)],
    ]
    execution = SourceAddExecutionPlan(
        "nb",
        SourceAddPlan(content=requested, detected_type="url", title=None, upload_path=None),
        fallback_fetch=True,
    )
    with pytest.raises(SourceAddError) as caught:
        await execute_source_add(client, execution)
    assert caught.value is original
    fetch.assert_not_called()
    client.sources.add_text.assert_not_called()


@pytest.mark.asyncio
async def test_equivalent_url_candidates_remain_ambiguous(setup):
    """Canonicalization must count all equivalent rows, never pick one arbitrarily."""
    client, original, fetch = setup
    other = Source(
        "other",
        url="https://EXAMPLE.com:443/article",
        status=SourceStatus.ERROR,
        experimental_failure_code=1,
    )
    client.sources.list.side_effect = [[], [ghost(), other]]
    with pytest.raises(SourceAddError) as caught:
        await execute_source_add(client, plan(fallback_fetch=True))
    assert caught.value is original
    fetch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RPCError, ClientError])
@pytest.mark.parametrize("phase", ["disabled", "baseline", "readback", "fetch"])
async def test_native_failure_preserves_identity_and_metadata_when_not_recovered(
    setup, error_type, phase
):
    client, _, fetch = setup
    original = error_type("failed precondition", method_id="AddSources", rpc_code=9)
    original.source_id = "ghost"
    original.stage = "source commit"
    metadata = original.operation_metadata
    client.sources.add_url.side_effect = original
    if phase == "baseline":
        client.sources.list.side_effect = RPCError("baseline unavailable")
    elif phase == "readback":
        client.sources.list.side_effect = [[], RPCError("readback unavailable")]
    elif phase == "fetch":
        fetch.side_effect = ValueError("fetch failed")
    with pytest.raises(error_type) as caught:
        await execute_source_add(client, plan(fallback_fetch=phase != "disabled"))
    assert caught.value is original
    assert original.operation_metadata is metadata
    assert original.method_id == "AddSources"
    assert original.rpc_code == 9
    assert str(original) == "failed precondition"
    client.sources.add_text.assert_not_called()
    client.sources.delete_many_with_outcomes.assert_not_called()
    if phase == "disabled":
        client.sources.list.assert_not_called()
    if phase != "fetch":
        fetch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "code", "source_id", "stage"),
    [
        (ClientError, 9, None, "source commit"),
        (ClientError, 9, "ghost", None),
        (ClientError, 9, "ghost", "register"),
        (ClientError, 3, "ghost", "source commit"),
        (RPCError, None, "ghost", "source commit"),
        (AuthError, 9, "ghost", "source commit"),
        (ServerError, 9, "ghost", "source commit"),
    ],
)
async def test_unrelated_native_errors_never_reconcile_or_fetch(
    setup, error_type, code, source_id, stage
):
    client, _, fetch = setup
    original = error_type("failure", rpc_code=code)
    original.source_id = source_id
    original.stage = stage
    client.sources.add_url.side_effect = original
    with pytest.raises(error_type) as caught:
        await execute_source_add(client, plan(fallback_fetch=True))
    assert caught.value is original
    assert client.sources.list.await_count == 1
    fetch.assert_not_called()
    client.sources.add_text.assert_not_called()
