"""Unit tests for the transport-neutral ``notebooklm._app.source_mutations`` core.

These pin the relocated source-mutation business logic at the ``_app`` boundary
(independent of the Click adapter):

* source-id resolvers — :func:`resolve_source_for_delete` (UUID fast-path,
  partial-prefix match, ambiguity, title-instead-of-id hint, not-found) and
  :func:`resolve_source_by_exact_title`.
* the typed :class:`SourceMutationError` (carried ``.code`` / ``.extra`` /
  ``.status_message``) and the :func:`require_yes_in_json` JSON-mode gate.
* the small pure helpers :func:`looks_like_full_source_id` /
  :func:`build_id_ambiguity_error`.
* the executors — delete / delete-by-title (confirm + cancel + JSON gate),
  rename / refresh (injected ``resolve_source_id``), add-drive (mime mapping).

Pure-service tests (no Click / CliRunner): the command-layer rendering +
exit-code policy is exercised in ``tests/unit/cli/test_source.py`` and
``tests/unit/cli/test_source_cmd_coverage.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.resolve import resolve_ref
from notebooklm._app.source_mutations import (
    SourceAddDrivePlan,
    SourceDeleteByTitlePlan,
    SourceDeletePlan,
    SourceMutationError,
    SourceRefreshPlan,
    SourceRenamePlan,
    build_id_ambiguity_error,
    execute_source_add_drive,
    execute_source_delete,
    execute_source_delete_by_title,
    execute_source_refresh,
    execute_source_rename,
    looks_like_full_source_id,
    require_yes_in_json,
    resolve_source_by_exact_title,
    resolve_source_for_delete,
)
from notebooklm.exceptions import NotebookLMError, ValidationError
from notebooklm.types import DriveMimeType, Source

_FULL_UUID = "11111111-2222-3333-4444-555555555555"


def _client(*, sources: list[Source] | None = None) -> MagicMock:
    client = MagicMock()
    client.sources = MagicMock()
    client.sources.list = AsyncMock(return_value=sources or [])
    client.sources.delete = AsyncMock(return_value=None)
    client.sources.rename = AsyncMock()
    client.sources.refresh = AsyncMock(return_value=None)
    client.sources.add_drive = AsyncMock()
    return client


# ===========================================================================
# Pure helpers
# ===========================================================================


class TestPureHelpers:
    def test_looks_like_full_source_id_accepts_uuid(self) -> None:
        assert looks_like_full_source_id(_FULL_UUID) is True

    @pytest.mark.parametrize("partial", ["src", "1234", "11111111-2222"])
    def test_looks_like_full_source_id_rejects_partial(self, partial: str) -> None:
        assert looks_like_full_source_id(partial) is False

    def test_build_id_ambiguity_error_lists_matches(self) -> None:
        matches = [Source(id="src_aaa111", title="One"), Source(id="src_aaa222", title=None)]
        msg = build_id_ambiguity_error("src_aaa", matches)
        assert "Ambiguous ID 'src_aaa'" in msg
        assert "matches 2 sources" in msg
        assert "src_aaa111" in msg
        assert "(untitled)" in msg  # None title rendered as placeholder

    def test_build_id_ambiguity_error_truncates_overflow(self) -> None:
        matches = [Source(id=f"src_{i:06d}", title=f"T{i}") for i in range(7)]
        msg = build_id_ambiguity_error("src", matches)
        assert "... and 2 more" in msg


# ===========================================================================
# SourceMutationError + require_yes_in_json
# ===========================================================================


class TestSourceMutationError:
    def test_is_notebooklm_error_subclass(self) -> None:
        assert issubclass(SourceMutationError, NotebookLMError)

    def test_carries_code_and_extra(self) -> None:
        err = SourceMutationError("boom", "NOT_FOUND", {"source_id": "s"}, "hint")
        assert err.code == "NOT_FOUND"
        assert err.extra == {"source_id": "s"}
        assert err.status_message == "hint"
        # The metadata is embedded in the str message.
        assert "code=NOT_FOUND" in str(err)
        assert "extra=" in str(err)

    def test_no_extra_omits_extra_from_message(self) -> None:
        err = SourceMutationError("boom", "AMBIGUOUS_ID")
        assert "code=AMBIGUOUS_ID" in str(err)
        assert "extra=" not in str(err)

    def test_require_yes_in_json_raises_confirm_required(self) -> None:
        with pytest.raises(SourceMutationError) as exc:
            require_yes_in_json(action="delete", extra={"source_id": "s"}, status_message="hint")
        err = exc.value
        assert err.code == "CONFIRM_REQUIRED"
        assert err.extra == {"action": "delete", "source_id": "s"}
        assert err.status_message == "hint"


# ===========================================================================
# resolve_source_for_delete
# ===========================================================================


class TestResolveSourceForDelete:
    @pytest.mark.asyncio
    async def test_full_uuid_skips_list(self) -> None:
        client = _client()
        resolution = await resolve_source_for_delete(client, "nb_1", _FULL_UUID)
        assert resolution.source_id == _FULL_UUID
        assert resolution.status_message is None
        client.sources.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_unique_partial_match_resolves_with_status(self) -> None:
        client = _client(sources=[Source(id="src_aaa111", title="One")])
        resolution = await resolve_source_for_delete(client, "nb_1", "src_aaa")
        assert resolution.source_id == "src_aaa111"
        # A partial→full expansion surfaces the "Matched:" status prose.
        assert resolution.status_message is not None
        assert "Matched:" in resolution.status_message

    @pytest.mark.asyncio
    async def test_exact_partial_match_no_status(self) -> None:
        # When the input already equals the matched id, no status prose is emitted.
        client = _client(sources=[Source(id="src_aaa111", title="One")])
        resolution = await resolve_source_for_delete(client, "nb_1", "src_aaa111")
        assert resolution.source_id == "src_aaa111"
        assert resolution.status_message is None

    @pytest.mark.asyncio
    async def test_exact_match_wins_over_prefix_ambiguity(self) -> None:
        # "abc" is an exact (case-insensitive) match for the first source AND a
        # strict prefix of "abcdef" — exact must win and not report ambiguity,
        # mirroring resolve_ref / resolve_partial_id_in_items Rule 3 so delete
        # stays in lockstep with get/rename/refresh (issue #1522).
        client = _client(
            sources=[Source(id="abc", title="Exact"), Source(id="abcdef", title="Prefixed")]
        )
        resolution = await resolve_source_for_delete(client, "nb_1", "abc")
        assert resolution.source_id == "abc"
        # An exact match is not a partial expansion, so no "Matched:" prose.
        assert resolution.status_message is None

    @pytest.mark.asyncio
    async def test_exact_match_wins_case_insensitive(self) -> None:
        # Exact match is case-insensitive and returns the source's canonical id.
        client = _client(
            sources=[Source(id="ABC", title="Exact"), Source(id="abcdef", title="Prefixed")]
        )
        resolution = await resolve_source_for_delete(client, "nb_1", "abc")
        assert resolution.source_id == "ABC"
        assert resolution.status_message is None

    @pytest.mark.asyncio
    async def test_ambiguous_partial_raises(self) -> None:
        client = _client(
            sources=[Source(id="src_aaa111", title="One"), Source(id="src_aaa222", title="Two")]
        )
        with pytest.raises(SourceMutationError) as exc:
            await resolve_source_for_delete(client, "nb_1", "src_aaa")
        assert exc.value.code == "AMBIGUOUS_ID"

    @pytest.mark.asyncio
    async def test_genuine_ambiguity_without_exact_still_raises(self) -> None:
        # Two strict prefixes and NO exact match → genuine ambiguity is preserved.
        client = _client(
            sources=[Source(id="abc111", title="One"), Source(id="abc222", title="Two")]
        )
        with pytest.raises(SourceMutationError) as exc:
            await resolve_source_for_delete(client, "nb_1", "abc")
        assert exc.value.code == "AMBIGUOUS_ID"

    @pytest.mark.asyncio
    async def test_title_match_suggests_delete_by_title(self) -> None:
        client = _client(sources=[Source(id="src_xyz999", title="My Title")])
        with pytest.raises(SourceMutationError) as exc:
            await resolve_source_for_delete(client, "nb_1", "My Title")
        assert exc.value.code == "VALIDATION_ERROR"
        assert "delete-by-title" in str(exc.value)

    @pytest.mark.asyncio
    async def test_no_match_raises_not_found(self) -> None:
        client = _client(sources=[Source(id="src_aaa111", title="One")])
        with pytest.raises(SourceMutationError) as exc:
            await resolve_source_for_delete(client, "nb_1", "zzz")
        assert exc.value.code == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_exact_match_parity_with_shared_resolver(self) -> None:
        # Parity guard (issue #1522): delete's bespoke resolver must agree with
        # the canonical resolve_ref on the exact-match-wins outcome, the rule
        # get/rename/refresh already get for free via resolve_source_id.
        sources = [Source(id="abc", title="Exact"), Source(id="abcdef", title="Prefixed")]
        client = _client(sources=sources)
        resolution = await resolve_source_for_delete(client, "nb_1", "abc")
        shared = resolve_ref("abc", sources, id_of=lambda s: s.id, title_of=lambda s: s.title)
        assert resolution.source_id == shared.id == "abc"

    @pytest.mark.asyncio
    async def test_injected_validate_id_runs_first(self) -> None:
        client = _client()
        called: list[tuple[str, str]] = []

        def validate_id(value: str, kind: str) -> str:
            called.append((value, kind))
            return value.strip()

        await resolve_source_for_delete(
            client, "nb_1", f"  {_FULL_UUID}  ", validate_id=validate_id
        )
        assert called == [(f"  {_FULL_UUID}  ", "source")]


# ===========================================================================
# resolve_source_by_exact_title
# ===========================================================================


class TestResolveSourceByExactTitle:
    @pytest.mark.asyncio
    async def test_single_title_match(self) -> None:
        client = _client(sources=[Source(id="src_1", title="My Title")])
        src = await resolve_source_by_exact_title(client, "nb_1", "My Title")
        assert src.id == "src_1"

    @pytest.mark.asyncio
    async def test_duplicate_titles_raise_ambiguous(self) -> None:
        client = _client(sources=[Source(id="src_1", title="Dup"), Source(id="src_2", title="Dup")])
        with pytest.raises(SourceMutationError) as exc:
            await resolve_source_by_exact_title(client, "nb_1", "Dup")
        assert exc.value.code == "AMBIGUOUS_TITLE"

    @pytest.mark.asyncio
    async def test_no_title_match_raises_not_found(self) -> None:
        client = _client(sources=[Source(id="src_1", title="Other")])
        with pytest.raises(SourceMutationError) as exc:
            await resolve_source_by_exact_title(client, "nb_1", "Missing")
        assert exc.value.code == "NOT_FOUND"


# ===========================================================================
# execute_source_delete
# ===========================================================================


class TestExecuteSourceDelete:
    @pytest.mark.asyncio
    async def test_delete_with_yes_completes(self) -> None:
        client = _client()
        confirm = MagicMock()  # never consulted under yes=True
        plan = SourceDeletePlan(
            notebook_id="nb_1", source_id=_FULL_UUID, yes=True, json_output=False
        )
        result = await execute_source_delete(client, plan, confirmer=confirm)
        assert result.success is True
        assert result.status == "completed"
        client.sources.delete.assert_awaited_once_with("nb_1", _FULL_UUID)
        confirm.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_declined_confirmation_cancels(self) -> None:
        client = _client()
        plan = SourceDeletePlan(
            notebook_id="nb_1", source_id=_FULL_UUID, yes=False, json_output=False
        )
        result = await execute_source_delete(client, plan, confirmer=lambda _msg: False)
        assert result.success is False
        assert result.status == "cancelled"
        client.sources.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_confirmed_interactively_completes(self) -> None:
        client = _client()
        plan = SourceDeletePlan(
            notebook_id="nb_1", source_id=_FULL_UUID, yes=False, json_output=False
        )
        result = await execute_source_delete(client, plan, confirmer=lambda _msg: True)
        assert result.success is True
        client.sources.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_json_with_yes_bypasses_gate(self) -> None:
        client = _client()
        plan = SourceDeletePlan(
            notebook_id="nb_1", source_id=_FULL_UUID, yes=True, json_output=True
        )
        # yes=True bypasses the gate even in json mode → completes.
        result = await execute_source_delete(client, plan, confirmer=MagicMock())
        assert result.success is True
        client.sources.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_json_no_yes_raises_confirm_required(self) -> None:
        client = _client()
        plan = SourceDeletePlan(
            notebook_id="nb_1", source_id=_FULL_UUID, yes=False, json_output=True
        )
        with pytest.raises(SourceMutationError) as exc:
            await execute_source_delete(client, plan, confirmer=MagicMock())
        assert exc.value.code == "CONFIRM_REQUIRED"
        assert exc.value.extra is not None
        assert exc.value.extra["action"] == "delete"
        client.sources.delete.assert_not_called()


# ===========================================================================
# execute_source_delete_by_title
# ===========================================================================


class TestExecuteSourceDeleteByTitle:
    @pytest.mark.asyncio
    async def test_delete_by_title_with_yes_completes(self) -> None:
        client = _client(sources=[Source(id="src_1", title="Doc")])
        plan = SourceDeleteByTitlePlan(notebook_id="nb_1", title="Doc", yes=True, json_output=False)
        result = await execute_source_delete_by_title(client, plan, confirmer=MagicMock())
        assert result.success is True
        assert result.source_id == "src_1"
        assert result.title == "Doc"
        client.sources.delete.assert_awaited_once_with("nb_1", "src_1")

    @pytest.mark.asyncio
    async def test_delete_by_title_declined_cancels(self) -> None:
        client = _client(sources=[Source(id="src_1", title="Doc")])
        plan = SourceDeleteByTitlePlan(
            notebook_id="nb_1", title="Doc", yes=False, json_output=False
        )
        result = await execute_source_delete_by_title(client, plan, confirmer=lambda _m: False)
        assert result.success is False
        assert result.status == "cancelled"
        client.sources.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_by_title_json_no_yes_raises(self) -> None:
        client = _client(sources=[Source(id="src_1", title="Doc")])
        plan = SourceDeleteByTitlePlan(notebook_id="nb_1", title="Doc", yes=False, json_output=True)
        with pytest.raises(SourceMutationError) as exc:
            await execute_source_delete_by_title(client, plan, confirmer=MagicMock())
        assert exc.value.code == "CONFIRM_REQUIRED"
        assert exc.value.extra is not None
        assert exc.value.extra["action"] == "delete-by-title"


# ===========================================================================
# execute_source_rename
# ===========================================================================


@pytest.mark.asyncio
async def test_rename_resolves_then_renames() -> None:
    client = _client()
    renamed = Source(id="src_full", title="New Name")
    client.sources.rename = AsyncMock(return_value=renamed)
    resolve = AsyncMock(return_value="src_full")
    plan = SourceRenamePlan(
        notebook_id="nb_1", source_id="src", new_title="New Name", json_output=False
    )
    result = await execute_source_rename(client, plan, resolve_source_id=resolve)
    assert result.source is renamed
    assert result.notebook_id == "nb_1"
    resolve.assert_awaited_once_with(client, "nb_1", "src", json_output=False)
    client.sources.rename.assert_awaited_once_with("nb_1", "src_full", "New Name")


# ===========================================================================
# execute_source_refresh
# ===========================================================================


@pytest.mark.asyncio
async def test_refresh_resolves_then_refreshes() -> None:
    client = _client()
    resolve = AsyncMock(return_value="src_full")
    plan = SourceRefreshPlan(notebook_id="nb_1", source_id="src", json_output=False)
    result = await execute_source_refresh(client, plan, resolve_source_id=resolve)
    assert result.source_id == "src_full"
    assert result.notebook_id == "nb_1"
    assert result.result is None
    resolve.assert_awaited_once_with(client, "nb_1", "src", json_output=False)
    client.sources.refresh.assert_awaited_once_with("nb_1", "src_full")


# ===========================================================================
# execute_source_add_drive — mime mapping
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "expected_mime"),
    [
        ("google-doc", DriveMimeType.GOOGLE_DOC.value),
        ("google-slides", DriveMimeType.GOOGLE_SLIDES.value),
        ("google-sheets", DriveMimeType.GOOGLE_SHEETS.value),
        ("pdf", DriveMimeType.PDF.value),
    ],
)
async def test_add_drive_maps_mime(choice: str, expected_mime: str) -> None:
    client = _client()
    added = Source(id="src_drive", title="Drive Doc")
    client.sources.add_drive = AsyncMock(return_value=added)
    plan = SourceAddDrivePlan(
        notebook_id="nb_1",
        file_id="fid",
        title="Drive Doc",
        mime_type=choice,  # type: ignore[arg-type]
    )
    result = await execute_source_add_drive(client, plan)
    assert result.source is added
    assert result.file_id == "fid"
    assert result.mime_type == choice
    client.sources.add_drive.assert_awaited_once_with("nb_1", "fid", "Drive Doc", expected_mime)


@pytest.mark.asyncio
async def test_add_drive_bad_mime_raises_validation_error() -> None:
    """An unknown ``mime_type`` raises the public ``ValidationError`` (ADR-0021).

    The CLI never reaches this guard (Click validates the ``Choice`` first), but
    a transport adapter that forwards a raw string (MCP/HTTP) must get a clean
    ``VALIDATION`` rather than a raw ``KeyError`` leaking as ``UNEXPECTED``.
    """
    client = _client()
    plan = SourceAddDrivePlan(
        notebook_id="nb_1",
        file_id="fid",
        title="Drive Doc",
        mime_type="bogus",  # type: ignore[arg-type]
    )
    with pytest.raises(ValidationError) as excinfo:
        await execute_source_add_drive(client, plan)
    # The message lists the valid keys so the caller can self-correct.
    assert "google-doc" in str(excinfo.value)
    client.sources.add_drive.assert_not_called()
