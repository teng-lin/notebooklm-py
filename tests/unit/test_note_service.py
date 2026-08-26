"""Unit tests for the semantic note service.

Until P10 R4.2 these tests drove ``LegacyNoteBackedService``, the deferred raw
note-row implementation in ``notebooklm._mind_map``. That class and its module
are gone: every note and note-backed mind-map path now runs on
:class:`notebooklm._note_service.NoteService` over the semantic port, so each
case below is retargeted at the surviving authority rather than retired.

* the envelope normalizer that ``fetch_note_rows`` owned is the ``mind_map.list``
  raw branch behind :meth:`NoteService.list_note_rows`;
* the ``NoteRowKind`` classifier is the codec's row partition, observable as
  which rows reach ``list_mind_map_rows`` versus ``list_notes``;
* the CRUD wire payloads are the ``note.*`` codec rows, asserted here at the
  ``rpc_call`` boundary exactly as before;
* the audit §28 cancel-shielded create is
  :meth:`NoteService.create_note_record`, which is now the package's single
  copy of that choreography — ``NoteBackedMindMapFamilyService`` sequences it
  rather than repeating it.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, call

import pytest

from notebooklm._note_service import NoteService
from notebooklm._semantic.records import NoteRecord
from notebooklm._web.backend import WebRpcBackend
from notebooklm.exceptions import DecodingError, RPCError
from notebooklm.rpc import RPCMethod
from tests._fixtures.fake_core import FakeSession, make_fake_core


@pytest.fixture
def mock_session() -> FakeSession:
    # ``make_fake_core`` is the ADR-0007 sanctioned substrate. We inject a
    # fresh ``AsyncMock`` for ``rpc_call`` at construction time so per-test
    # ``.return_value`` / ``.side_effect`` assignment still works.
    return make_fake_core(rpc_call=AsyncMock(return_value=None))


@pytest.fixture
def service(mock_session: FakeSession) -> NoteService:
    return NoteService(WebRpcBackend(mock_session.rpc_executor))


def _read_kwargs() -> dict[str, Any]:
    """The keyword set the semantic port sends for a note-collection read."""

    return {
        "source_path": "/notebook/nb_123",
        "allow_null": True,
        "_is_retry": False,
        "disable_internal_retries": False,
        "operation_variant": None,
        "read_timeout": None,
        "raise_on_null_status": False,
        "_retry_deadline": None,
    }


class TestListNoteRows:
    """``list_note_rows`` returns raw rows or ``[]`` for malformed payloads."""

    @pytest.mark.asyncio
    async def test_list_note_rows_filters_invalid_rows(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        mock_session.rpc_executor.rpc_call.return_value = [
            [
                ["note_1", "Content"],
                [None, ["note_3", "Nested body", None, None, "Nested Title"]],
                [],
                "not-a-row",
                [123, "Non-string ID"],
                [None, "Non-nested note payload"],
                ["note_2", "Content"],
            ]
        ]

        rows = await service.list_note_rows("nb_123")

        assert rows == [
            ["note_1", "Content"],
            ["note_3", ["note_3", "Nested body", None, None, "Nested Title"]],
            ["note_2", "Content"],
        ]
        mock_session.rpc_executor.rpc_call.assert_awaited_once_with(
            RPCMethod.GET_NOTES_AND_MIND_MAPS,
            ["nb_123"],
            **_read_kwargs(),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [None, [], ["not-a-list"], [[]]])
    async def test_list_note_rows_returns_empty_for_malformed_payload(
        self, service: NoteService, mock_session: FakeSession, payload: object
    ) -> None:
        mock_session.rpc_executor.rpc_call.return_value = payload
        assert await service.list_note_rows("nb_123") == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", ["drift-string", {"oops": 1}, 42])
    async def test_list_note_rows_raises_on_truthy_non_list_drift(
        self, service: NoteService, mock_session: FakeSession, payload: object
    ) -> None:
        # A truthy non-list payload is schema drift, not an empty notebook (#1344):
        # raise so notes/mind_maps get()/get_or_none can tell a miss from drift
        # instead of silently collapsing to ``[]``. The neutral port translates
        # the decoding failure, so the facade projects it back to DecodingError.
        mock_session.rpc_executor.rpc_call.return_value = payload
        with pytest.raises(Exception) as caught:
            await service.list_note_rows("nb_123")
        assert caught.value.reason.value == "decoding"

    @pytest.mark.asyncio
    async def test_list_note_rows_accepts_flat_row_container(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        mock_session.rpc_executor.rpc_call.return_value = [
            ["note_1", "Content"],
            ["deleted_note", None, 2],
        ]

        assert await service.list_note_rows("nb_123") == [
            ["note_1", "Content"],
            ["deleted_note", None, 2],
        ]


class TestRowPartition:
    """The codec partitions rows exactly as ``NoteRowKind`` classified them."""

    @staticmethod
    async def _ids(
        service: NoteService, mock_session: FakeSession, row: list[Any]
    ) -> tuple[list[str], list[str]]:
        """Return ``(mind_map_ids, note_ids)`` for one raw row."""

        mock_session.rpc_executor.rpc_call.return_value = [[row]]
        mind_maps = await service.list_mind_map_rows("nb_123")
        notes = await service.list_notes("nb_123")
        return [item[0] for item in mind_maps], [item.id for item in notes]

    @pytest.mark.asyncio
    async def test_deleted_row_reaches_neither_listing(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        assert await self._ids(service, mock_session, ["row_1", None, 2]) == ([], [])

    @pytest.mark.asyncio
    async def test_mind_map_row_via_children_key(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        row = ["mm_1", json.dumps({"children": []})]
        assert await self._ids(service, mock_session, row) == (["mm_1"], [])

    @pytest.mark.asyncio
    async def test_mind_map_row_via_nodes_key(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        row = ["mm_2", ["mm_2", json.dumps({"nodes": []}), None, None, "Title"]]
        assert await self._ids(service, mock_session, row) == (["mm_2"], [])

    @pytest.mark.asyncio
    async def test_plain_note_row(self, service: NoteService, mock_session: FakeSession) -> None:
        row = ["note_1", "This is a regular note body."]
        assert await self._ids(service, mock_session, row) == ([], ["note_1"])

    @pytest.mark.asyncio
    async def test_nested_note_shape_is_a_note(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        row = ["note_2", ["note_2", "Nested body", None, None, "Nested Title"]]
        assert await self._ids(service, mock_session, row) == ([], ["note_2"])

    @pytest.mark.asyncio
    async def test_row_without_extractable_content_is_not_a_mind_map(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        # A row with an id but no readable content slot is never a mind map;
        # the plain-note listing keeps surfacing it rather than dropping it.
        assert await self._ids(service, mock_session, ["row_3", 123]) == ([], ["row_3"])

    @pytest.mark.asyncio
    async def test_saved_chat_without_metadata_stays_a_note(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        """Per docs/refactor-history.md §Risks: when saved-chat metadata is not
        positively detectable, the row must still surface through
        ``NotesAPI.list()`` rather than dropping out.
        """
        row = ["chat_note_1", "Saved chat answer body without explicit chat flag."]
        assert await self._ids(service, mock_session, row) == ([], ["chat_note_1"])


class TestContentExtraction:
    """Legacy and current wire shapes both yield their content payload."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("row", "expected"),
        [
            (["row_1", "legacy"], "legacy"),
            (["row_1", ["row_1", "nested", None, None, "Title"]], "nested"),
            (["row_1", 123], ""),
            (["row_1", ["row_1"]], ""),
        ],
    )
    async def test_note_content_for_each_wire_shape(
        self,
        service: NoteService,
        mock_session: FakeSession,
        row: list[Any],
        expected: str,
    ) -> None:
        mock_session.rpc_executor.rpc_call.return_value = [[row]]
        notes = await service.list_notes("nb_123")
        assert [item.content for item in notes] == [expected]

    @pytest.mark.asyncio
    async def test_mind_map_content_is_the_persisted_json(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        tree = json.dumps({"children": []})
        mock_session.rpc_executor.rpc_call.return_value = [[["mm_1", tree]]]
        records = await service.list_mind_maps("nb_123")
        assert [record.tree_json for record in records] == [tree]


class TestCrud:
    """CRUD methods send the expected wire payloads."""

    @pytest.mark.asyncio
    async def test_create_note_does_create_then_update(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        mock_session.rpc_executor.rpc_call.side_effect = [[["note_123"]], None]

        note = await service.create_note_record(
            "nb_123",
            title="Mind Map",
            content='{"children":[]}',
        )

        # R6.6: the service returns the neutral allocation record. The public
        # ``Note`` this projects to is asserted at the facade, in
        # ``test_notes_unit.py::test_create_projects_the_allocation_record``.
        assert note == NoteRecord(
            id="note_123",
            notebook_id="nb_123",
            title="Mind Map",
            content='{"children":[]}',
        )
        create_kwargs = {**_read_kwargs(), "allow_null": False, "operation_variant": "plain"}
        assert mock_session.rpc_executor.rpc_call.await_args_list == [
            call(
                RPCMethod.CREATE_NOTE,
                ["nb_123", "", [1], None, "Mind Map"],
                **create_kwargs,
            ),
            call(
                RPCMethod.UPDATE_NOTE,
                ["nb_123", "note_123", [[['{"children":[]}', "Mind Map", [], 0]]]],
                **_read_kwargs(),
            ),
        ]

    @pytest.mark.asyncio
    async def test_create_note_raises_when_server_omits_id(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        mock_session.rpc_executor.rpc_call.return_value = None

        # An unparseable CREATE_NOTE payload must surface as an error
        # rather than a success-shaped ``Note(id="")`` (issue #1162).
        with pytest.raises(Exception, match="no usable note id"):
            await service.create_note_record("nb_123", title="T", content="body")

        # Only CREATE_NOTE should fire; bailing before UPDATE_NOTE avoids
        # poisoning a non-existent row.
        assert mock_session.rpc_executor.rpc_call.await_count == 1

    @pytest.mark.asyncio
    async def test_create_note_rejects_a_non_plain_variant(self, service: NoteService) -> None:
        # The saved-from-chat variant belongs to ``chat.save_note``; the plain
        # note service never allocated it and must not start now.
        with pytest.raises(ValueError):
            await service.create_note_record("nb_123", operation_variant="saved_from_chat")

    @pytest.mark.asyncio
    async def test_update_note_sends_existing_payload(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        await service.update_note("nb_123", "note_123", "Body", "Title")

        mock_session.rpc_executor.rpc_call.assert_awaited_once_with(
            RPCMethod.UPDATE_NOTE,
            ["nb_123", "note_123", [[["Body", "Title", [], 0]]]],
            **_read_kwargs(),
        )

    @pytest.mark.asyncio
    async def test_delete_note_returns_none_and_sends_soft_delete(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        # v0.7.0: delete_note returns None (issue #1211).
        assert await service.delete_note("nb_123", "note_123") is None

        mock_session.rpc_executor.rpc_call.assert_awaited_once_with(
            RPCMethod.DELETE_NOTE,
            ["nb_123", None, ["note_123"]],
            **_read_kwargs(),
        )


class TestCreateNoteCancellation:
    """Audit item §28: cancel mid-UPDATE_NOTE must not leave an orphan row.

    P10 R4.2 deleted the second copy of this choreography along with
    ``LegacyNoteBackedService``. These two cases move to the surviving one and
    are now the ordered-cleanup gate for every caller that persists generated
    content, ``artifact.generate_mind_map`` included.
    """

    @staticmethod
    def _gated_rpc(
        *,
        update_started: asyncio.Event,
        update_can_finish: asyncio.Event,
        update_finished: asyncio.Event,
        delete_started: asyncio.Event,
        update_raises: bool = False,
    ) -> Any:
        async def _rpc_call(method: RPCMethod, params: list[Any], **_: Any) -> Any:
            if method is RPCMethod.CREATE_NOTE:
                return [["note_123"]]
            if method is RPCMethod.UPDATE_NOTE:
                update_started.set()
                try:
                    await update_can_finish.wait()
                    if update_raises:
                        raise RuntimeError("simulated UPDATE_NOTE failure after shield")
                finally:
                    update_finished.set()
                return None
            if method is RPCMethod.DELETE_NOTE:
                assert params == ["nb_123", None, ["note_123"]]
                delete_started.set()
                return None
            return None

        return AsyncMock(side_effect=_rpc_call)

    @pytest.mark.asyncio
    async def test_cancellation_schedules_ordered_best_effort_cleanup(self) -> None:
        update_started = asyncio.Event()
        update_can_finish = asyncio.Event()
        update_finished = asyncio.Event()
        delete_started = asyncio.Event()
        session = make_fake_core(
            rpc_call=self._gated_rpc(
                update_started=update_started,
                update_can_finish=update_can_finish,
                update_finished=update_finished,
                delete_started=delete_started,
            )
        )
        service = NoteService(WebRpcBackend(session.rpc_executor))

        task = asyncio.create_task(
            service.create_note_record("nb_123", title="Title", content="body")
        )
        await asyncio.wait_for(update_started.wait(), timeout=1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        # Ordered cleanup (coderabbit feedback on PR #875): the cleanup wrapper
        # task is scheduled at cancel time but DELETE_NOTE only fires AFTER the
        # shielded UPDATE_NOTE finishes, so delete can never write to a row the
        # still-running update is about to touch.
        assert not update_finished.is_set()
        assert not delete_started.is_set()

        update_can_finish.set()
        await asyncio.wait_for(update_finished.wait(), timeout=1)
        await asyncio.wait_for(delete_started.wait(), timeout=1)

    @pytest.mark.asyncio
    async def test_cancellation_cleanup_runs_even_when_update_raises(self) -> None:
        """A shielded UPDATE_NOTE that raises must not skip the cleanup.

        Coderabbit feedback on PR #875 (the ordered-cleanup change) added this
        guard: the cleanup wrapper logs and swallows any UPDATE_NOTE exception
        so the DELETE_NOTE half always runs. Without it, an update-side error
        would leave exactly the orphan row the shield exists to prevent.
        """
        update_started = asyncio.Event()
        update_can_finish = asyncio.Event()
        update_finished = asyncio.Event()
        delete_started = asyncio.Event()
        session = make_fake_core(
            rpc_call=self._gated_rpc(
                update_started=update_started,
                update_can_finish=update_can_finish,
                update_finished=update_finished,
                delete_started=delete_started,
                update_raises=True,
            )
        )
        service = NoteService(WebRpcBackend(session.rpc_executor))

        task = asyncio.create_task(service.create_note_record("nb_123", title="T", content="b"))
        await asyncio.wait_for(update_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert not delete_started.is_set()

        update_can_finish.set()
        await asyncio.wait_for(delete_started.wait(), timeout=1)


class TestPrivacy:
    """The row classification never became part of the public surface."""

    def test_note_row_kind_not_in_public_exports(self) -> None:
        import notebooklm
        import notebooklm.types

        assert "NoteRowKind" not in dir(notebooklm)
        assert "NoteRowKind" not in dir(notebooklm.types)


#: ``src/notebooklm/_note_service.py``, located from this test file.
_NOTE_SERVICE_PATH = Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_note_service.py"

#: The top-level ``notebooklm`` roots a neutral semantic service may not import,
#: mirroring ``tests/_guardrails/test_service_boundary.py``'s I1 rule. Repeated
#: here (not imported) because that guardrail is ``repo_lint``-marked and so is
#: excluded from the required pull-request suite, while R4.1's acceptance
#: criterion has to hold on every commit.
_WIRE_ROOTS = frozenset({"rpc", "_row_adapters"})
_PROJECTION_ROOTS = frozenset({"_backend_compat", "_projectors", "_types", "_web", "types"})


def _parse_outside_type_checking(path: Path) -> ast.Module:
    """Parse ``path`` with every ``if TYPE_CHECKING:`` body removed.

    A type-only import couples nothing at runtime, which is the property both
    audits below measure.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(node, field, None)
            if not isinstance(statements, list):
                continue
            setattr(
                node,
                field,
                [
                    statement
                    for statement in statements
                    if not (
                        isinstance(statement, ast.If)
                        and any(
                            isinstance(name, ast.Name) and name.id == "TYPE_CHECKING"
                            for name in ast.walk(statement.test)
                        )
                    )
                ],
            )
    return tree


def _runtime_import_roots(path: Path) -> set[str]:
    """Return the first-party roots ``path`` imports outside ``TYPE_CHECKING``."""

    roots: set[str] = set()
    for node in ast.walk(_parse_outside_type_checking(path)):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("notebooklm."):
                    roots.add(alias.name.split(".")[1])
    return roots


#: ``src/notebooklm``, the package root every first-party target resolves under.
_PACKAGE_ROOT = _NOTE_SERVICE_PATH.parent


def _runtime_import_targets(path: Path) -> set[str]:
    """Dotted first-party targets ``path`` imports outside ``TYPE_CHECKING``.

    Like :func:`_runtime_import_roots` but keeps the whole dotted name
    (``rpc.decoder``) and resolves a relative import against the importing
    file's own package, so the walk below works from any depth.
    """

    parts = list(path.relative_to(_PACKAGE_ROOT).parts)
    package = parts[:-1] if path.name != "__init__.py" else parts[:-1]
    targets: set[str] = set()
    for node in ast.walk(_parse_outside_type_checking(path)):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                base = [*base, node.module] if node.module else base
            elif node.module and node.module.startswith("notebooklm."):
                base = node.module.split(".")[1:]
            else:
                continue
            if base:
                targets.add(".".join(base))
            targets.update(".".join([*base, alias.name]) for alias in node.names)
        elif isinstance(node, ast.Import):
            targets.update(
                alias.name[len("notebooklm.") :]
                for alias in node.names
                if alias.name.startswith("notebooklm.")
            )
    return targets


def _reachable_wire_modules(path: Path) -> set[str]:
    """Wire modules reachable from ``path`` through runtime first-party imports.

    The direct-import pins cannot see this: ``_projectors`` and
    ``notebooklm.types`` each pull the whole wire layer in one hop, which is
    why R4.1's measurement of removing either one alone came out at zero.
    """

    def resolve(dotted: str) -> Path | None:
        relative = dotted.replace(".", "/")
        module = _PACKAGE_ROOT / f"{relative}.py"
        if module.is_file():
            return module
        package = _PACKAGE_ROOT / relative / "__init__.py"
        return package if package.is_file() else None

    seen: set[str] = set()
    pending = [path]
    wire: set[str] = set()
    while pending:
        for dotted in _runtime_import_targets(pending.pop()):
            if dotted in seen:
                continue
            seen.add(dotted)
            if dotted.split(".")[0] in _WIRE_ROOTS:
                wire.add(dotted)
            resolved = resolve(dotted)
            if resolved is not None:
                pending.append(resolved)
    return wire


class TestSemanticNoteServiceCarriesNoWireImports:
    """R4.1/R6.6 acceptance: the semantic note module names no wire module."""

    def test_note_service_module_imports_no_rpc_or_row_adapter_module(self) -> None:
        """``NoteRow``/``safe_index``/``RPCMethod`` left with the legacy class."""

        assert not (_runtime_import_roots(_NOTE_SERVICE_PATH) & _WIRE_ROOTS)

    def test_no_projection_root_survives(self) -> None:
        """R6.6 drained the residue R4.1 pinned; the set shrinks, never grows.

        R4.1 left ``{_projectors, types}`` here and measured that dropping
        either alone changed nothing: each one reaches the whole wire layer
        transitively on its own. R6.6 dropped both together by moving
        projection to ``NotesAPI``/``MindMapsAPI``, so the residue is empty and
        ``_note_service.py`` left the I1 seed allowlist.
        """

        assert not (_runtime_import_roots(_NOTE_SERVICE_PATH) & _PROJECTION_ROOTS)

    def test_no_wire_module_is_reachable_transitively(self) -> None:
        """The point of the two removals: nothing wire-side is imported at all.

        The direct-import pins above cannot see the transitive reach that made
        R4.1's gain unmeasurable, so walk the runtime import graph. Both
        ``_projectors`` and ``notebooklm.types`` still reach every one of these
        modules, which is why R6.6 had to remove them together.
        """

        assert _reachable_wire_modules(_NOTE_SERVICE_PATH) == set()


def test_decoding_error_is_still_the_drift_signal() -> None:
    """The codec raises the same public class the raw fetch did."""

    from notebooklm._web.codec.notes import _decode_note_rows

    with pytest.raises(DecodingError):
        _decode_note_rows("drift-string")
    with pytest.raises(RPCError):
        _decode_note_rows(42)
