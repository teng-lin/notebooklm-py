"""Behavioral conformance for the public ``get`` / ``get_or_none`` miss contract (ADR-0019).

The static sibling, ``test_public_api_contract.py``, walks
``inspect.signature(...)`` return annotations across the whole public surface. It
is the Tier-1 *shape* floor: it proves ``get_or_none`` is annotated ``Optional``,
``delete`` returns ``None``, and every still-``Optional`` ``get`` carries a
reason-tagged exemption. But a static walk **never executes a method**, so it
cannot catch the exact historical ``mind_maps`` bug — a ``get()`` correctly
annotated ``MindMap | None`` that *forgot to warn* on a miss (#1358). That
miss-behaviour is hand-duplicated across ``_sources`` / ``_artifacts`` /
``_notes`` / ``_mind_maps_api`` as each namespace moved off the old
None-on-miss warning runway — exactly the kind of copy that silently rots when
one copy is dropped.

This module adds the **behavioural** half of the Tier-1 floor. For each lookup
namespace it instantiates the backing API with a fake backend (reusing the
constructor-injection substrate under ``tests/_fixtures/`` — no network, auth, or
event loop beyond what those provide) arranged to yield a genuine MISS, then
asserts the not-found contract:

* ``await get(<missing id>)`` raises its ``*NotFoundError`` (the v0.8.0 contract,
  #1247 — now live for all five namespaces);
* ``await get_or_none(<missing id>)`` emits **no** ``DeprecationWarning`` **and**
  returns ``None`` (the sanctioned silent optional-lookup).

**Flip-durability (the load-bearing design choice).** The per-namespace table
:data:`LOOKUP_CASES` carries, for each namespace, its API factory, a
miss-arranger, the ``get`` arguments, the resource name, and the
``*NotFoundError`` type. The ``get_warns`` flag selects the assertion:
``get_warns=False`` → ``pytest.raises(<*NotFoundError>)``;
``get_warns=True`` → ``pytest.warns(DeprecationWarning)`` + ``None``. The v0.8.0
``get()``→raise flip (#1247) landed by flipping every namespace's ``get_warns``
to ``False`` with a *single table-driven edit*; ``test_no_namespace_remains_on_the_warn_runway``
now pins the warn branch unused (no row may regain ``get_warns=True``). The harness
keeps both branches so the flip is exercised by construction and the table mirrors
how the static ``GET_OPTIONAL_EXEMPTIONS`` allowlist drained to empty: the contract
lives in one visible, reason-tagged table, never scattered.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._labels import LabelsAPI
from notebooklm._mind_map import NoteBackedMindMapService
from notebooklm._mind_maps_api import MindMapsAPI
from notebooklm._note_service import NoteService
from notebooklm._notebooks import NotebooksAPI
from notebooklm._notes import NotesAPI
from notebooklm._sources import SourcesAPI
from notebooklm.exceptions import (
    ArtifactNotFoundError,
    LabelNotFoundError,
    MindMapNotFoundError,
    NotebookNotFoundError,
    NoteNotFoundError,
    SourceNotFoundError,
)

# This behavioural table is the executable companion of the static
# ``LOOKUP_NAMESPACES`` set in ``test_public_api_contract.py``: the same six
# namespaces that expose the ``get`` / ``get_or_none`` pair.
# ``test_table_covers_all_lookup_namespaces`` below pins the two in lock-step so
# a namespace can never gain the lookup pair without also gaining behavioural
# coverage of its miss contract.


# ---------------------------------------------------------------------------
# Per-namespace factories + miss-arrangers
#
# Each factory builds the backing API through constructor injection only
# (``make_fake_core`` / ``MagicMock`` collaborators) so the behavioural walk
# needs no auth, event loop, or network — mirroring the fixtures in
# ``test_get_or_none.py`` but consolidated behind one post-#1247 table.
# ---------------------------------------------------------------------------


def _make_sources_api() -> SourcesAPI:
    # No ``make_fake_core`` here: ``_arrange_list_miss`` overrides ``api.list``
    # before any RPC path is reached, so the first positional collaborator is
    # never called (matches how ``test_get_or_none.py`` builds its sources API).
    return SourcesAPI(MagicMock(), uploader=MagicMock())


def _make_artifacts_api() -> ArtifactsAPI:
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock(), get_source_ids=AsyncMock(return_value=[]))
    mind_maps = MagicMock(spec=NoteBackedMindMapService)
    mind_maps.list_mind_maps = AsyncMock(return_value=[])
    notebooks = MagicMock()
    notebooks.get_source_ids = AsyncMock(return_value=[])
    return ArtifactsAPI(
        rpc=core,
        drain=core,
        lifecycle=core,
        notebooks=notebooks,
        mind_maps=mind_maps,
        note_service=MagicMock(spec=NoteService),
    )


def _make_notes_api() -> NotesAPI:
    from tests._fixtures.fake_core import make_fake_core

    # ``None`` is the empty-notebook payload (a notebook with no notes); it is
    # the realistic miss shape that ``fetch_note_rows`` resolves to ``[]``. A
    # truthy non-list payload would now raise ``DecodingError`` as drift (#1344),
    # so it can no longer stand in for "empty".
    core = make_fake_core(rpc_call=AsyncMock(return_value=None))
    note_service = NoteService(core)
    mind_maps = NoteBackedMindMapService(note_service)
    return NotesAPI(notes=note_service, mind_maps=mind_maps)


def _make_mind_maps_api() -> MindMapsAPI:
    mind_maps = MagicMock(spec=NoteBackedMindMapService)
    mind_maps.list_mind_maps = AsyncMock(return_value=[])
    artifacts = MagicMock()
    artifacts.list = AsyncMock(return_value=[])
    notebooks = MagicMock()
    return MindMapsAPI(
        rpc=MagicMock(),
        mind_maps=mind_maps,
        artifacts=artifacts,
        notebooks=notebooks,
    )


def _make_labels_api() -> LabelsAPI:
    # ``_arrange_list_miss`` overrides ``api.list`` before any RPC path is reached
    # (``labels.get`` scans ``self.list``), so the rpc collaborator and
    # ``list_sources`` are never called on the miss path.
    return LabelsAPI(MagicMock(), list_sources=AsyncMock(return_value=[]))


def _make_notebooks_api() -> NotebooksAPI:
    from tests._fixtures.fake_core import make_fake_core

    # An empty/degenerate GET_NOTEBOOK payload is the unknown-id shape that
    # ``notebooks.get`` post-validates into ``NotebookNotFoundError`` — so this
    # factory is already arranged for a miss (see ``_arrange_notebooks_miss``).
    core = make_fake_core(rpc_call=AsyncMock(return_value=[[]]))
    return NotebooksAPI(core.rpc_executor, sources_api=MagicMock())


def _arrange_list_miss(api: object) -> None:
    """Force a miss for the four non-``notebooks`` namespaces.

    ``sources`` / ``artifacts`` / ``mind_maps`` resolve a single ``get`` by
    scanning ``self.list(...)``, so stubbing ``list`` to ``[]`` is a uniform,
    backend-agnostic miss (the same lever the existing per-namespace tests pull).

    ``notes`` is the exception: ``NotesAPI.get_or_none`` resolves through
    ``_get_all_notes_and_mind_maps`` → ``fetch_note_rows``, **not** ``self.list``,
    so the assigned ``api.list`` stub is a harmless no-op for it. The notes miss
    comes from its factory (``_make_notes_api``) wiring a fake core whose
    ``rpc_call`` returns ``None`` — the empty-notebook payload that
    ``fetch_note_rows`` resolves to ``[]`` — so the ``get`` still misses. Keeping
    one shared arranger across all four rows is deliberate: it stays a single
    table lever even though one namespace reaches the empty result by a different
    internal path.
    """
    api.list = AsyncMock(return_value=[])  # type: ignore[attr-defined]


def _arrange_notebooks_miss(api: object) -> None:
    """Notebooks already returns the degenerate payload from its factory.

    ``notebooks.get`` validates the RPC payload directly (it does not scan
    ``list``); ``_make_notebooks_api`` wires the empty payload, so no further
    arrangement is needed. Kept explicit so every row carries an arranger and
    the miss setup is never an implicit factory side effect that a reader misses.
    """
    return None


@dataclass(frozen=True)
class LookupCase:
    """One namespace's miss-contract row — the unit the flip edits.

    Attributes:
        namespace: Public client attribute name (keys this row to the static
            ``LOOKUP_NAMESPACES`` set in ``test_public_api_contract.py``).
        factory: Builds the backing API via constructor injection only.
        arrange_miss: Configures the built instance to yield a genuine miss.
        get_args: Positional args for ``get`` / ``get_or_none`` (per-arity).
        resource: Singular resource name. Load-bearing in the warn-runway
            assertion: the miss warning must name ``<resource>s.get()``, which is
            what distinguishes a correct warning from a *wrong-resource* one
            (the exact #1358-class bug). Also makes failures self-describing.
        not_found_error: The ``*NotFoundError`` ``get`` raises **after** the
            #1247 flip; asserted today only for already-flipped rows.
        get_warns: ``True`` while ``get`` is in the warn-runway (warns + returns
            ``None`` on a miss); ``False`` once it raises ``not_found_error``.
            **The single field the #1247 flip toggles per namespace.**
    """

    namespace: str
    factory: Callable[[], object]
    arrange_miss: Callable[[object], None]
    get_args: tuple[str, ...]
    resource: str
    not_found_error: type[Exception]
    get_warns: bool


# The flip-durable table. ``get_warns=True`` is the warn-runway state #1247 will
# flip to ``False`` (a single per-row edit, mirroring ``GET_OPTIONAL_EXEMPTIONS``
# shrinking in the static gate). ``notebooks`` ships ``get_warns=False`` today —
# it already raises — so the post-flip ``pytest.raises`` branch is exercised on
# every run and cannot bit-rot before #1247 lands.
LOOKUP_CASES: tuple[LookupCase, ...] = (
    LookupCase(
        namespace="notebooks",
        factory=_make_notebooks_api,
        arrange_miss=_arrange_notebooks_miss,
        get_args=("nb_missing",),
        resource="notebook",
        not_found_error=NotebookNotFoundError,
        get_warns=False,  # already flipped: notebooks.get raises today
    ),
    LookupCase(
        namespace="sources",
        factory=_make_sources_api,
        arrange_miss=_arrange_list_miss,
        get_args=("nb_1", "missing"),
        resource="source",
        not_found_error=SourceNotFoundError,
        get_warns=False,  # flipped: raises *NotFoundError on a miss (#1247)
    ),
    LookupCase(
        namespace="artifacts",
        factory=_make_artifacts_api,
        arrange_miss=_arrange_list_miss,
        get_args=("nb_1", "missing"),
        resource="artifact",
        not_found_error=ArtifactNotFoundError,
        get_warns=False,  # flipped: raises *NotFoundError on a miss (#1247)
    ),
    LookupCase(
        namespace="notes",
        factory=_make_notes_api,
        arrange_miss=_arrange_list_miss,
        get_args=("nb_1", "missing"),
        resource="note",
        not_found_error=NoteNotFoundError,
        get_warns=False,  # flipped: raises *NotFoundError on a miss (#1247)
    ),
    LookupCase(
        namespace="mind_maps",
        factory=_make_mind_maps_api,
        arrange_miss=_arrange_list_miss,
        get_args=("nb_1", "missing"),
        resource="mind_map",
        not_found_error=MindMapNotFoundError,
        get_warns=False,  # flipped: raises *NotFoundError on a miss (#1247)
    ),
    LookupCase(
        namespace="labels",
        factory=_make_labels_api,
        arrange_miss=_arrange_list_miss,
        get_args=("nb_1", "missing"),
        resource="label",
        not_found_error=LabelNotFoundError,
        get_warns=False,  # v0.8.0: labels.get raises LabelNotFoundError on a miss
    ),
)

_CASES_BY_ID = [pytest.param(case, id=case.namespace) for case in LOOKUP_CASES]


def test_no_namespace_remains_on_the_warn_runway() -> None:
    """#1247 has landed: every namespace get() raises on a miss, so no
    LookupCase may carry get_warns=True (flipping one back fails here)."""
    warned = [c.namespace for c in LOOKUP_CASES if c.get_warns]
    assert warned == [], (
        f"These namespaces still claim the get()-warn runway (get_warns=True): "
        f"{warned}. After #1247 every get() raises *NotFoundError on a miss."
    )


def _build_missing(case: LookupCase) -> object:
    """Build the backing API and arrange it to yield a miss."""
    api = case.factory()
    case.arrange_miss(api)
    return api


# ---------------------------------------------------------------------------
# The two error-contract modes the miss path is exercised under
#
# ``NOTEBOOKLM_FUTURE_ERRORS`` is retired in v0.8.0 and ignored. Both ``get``
# test methods still run under set/unset modes so the matrix verifies behavior
# is identical with the compatibility env var present or absent.
# ---------------------------------------------------------------------------

_FUTURE_MODES = [
    pytest.param(False, id="future-off"),
    pytest.param(True, id="future-on"),
]


@pytest.fixture
def _apply_future_errors(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    """Set/clear the retired ``NOTEBOOKLM_FUTURE_ERRORS`` env var."""
    # Hermetic both ways: the future-off branch asserts a DeprecationWarning
    # fires, so a parent process exporting NOTEBOOKLM_QUIET_DEPRECATIONS=1 would
    # otherwise silence the warn path and fail the warn-runway rows. Clear it so
    # the matrix is independent of the ambient environment.
    monkeypatch.delenv("NOTEBOOKLM_QUIET_DEPRECATIONS", raising=False)
    future_on: bool = request.param
    if future_on:
        monkeypatch.setenv("NOTEBOOKLM_FUTURE_ERRORS", "1")
    else:
        monkeypatch.delenv("NOTEBOOKLM_FUTURE_ERRORS", raising=False)
    return future_on


# ---------------------------------------------------------------------------
# The table is pinned to the static gate's lookup set
# ---------------------------------------------------------------------------


def test_table_covers_all_lookup_namespaces() -> None:
    """Every namespace with the ``get`` / ``get_or_none`` pair has a behavioural row.

    Pins this behavioural table to the static gate's ``LOOKUP_NAMESPACES`` so a
    namespace can never gain (or rename away) the lookup pair without its miss
    contract being covered here too — the static and behavioural halves of the
    Tier-1 floor stay in lock-step.
    """
    from tests.unit.test_public_api_contract import LOOKUP_NAMESPACES

    covered = {case.namespace for case in LOOKUP_CASES}
    assert covered == set(LOOKUP_NAMESPACES), (
        f"behavioural LOOKUP_CASES cover {sorted(covered)}, but the static gate "
        f"pins LOOKUP_NAMESPACES = {sorted(LOOKUP_NAMESPACES)}; add/remove a row."
    )


# ---------------------------------------------------------------------------
# get() — the warn-runway / raise contract (the field #1247 flips)
# ---------------------------------------------------------------------------


class TestGetMissContract:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("_apply_future_errors", _FUTURE_MODES, indirect=True)
    @pytest.mark.parametrize("case", _CASES_BY_ID)
    async def test_get_on_miss_warns_or_raises(
        self, case: LookupCase, _apply_future_errors: bool
    ) -> None:
        """``get(<missing>)`` raises after #1247, with the retired env var ignored."""
        future_on = _apply_future_errors
        api = _build_missing(case)
        if case.get_warns and not future_on:
            # Warn-runway: a DeprecationWarning fires AND None comes back.
            with pytest.warns(DeprecationWarning) as record:
                result = await api.get(*case.get_args)  # type: ignore[attr-defined]
            assert result is None, f"{case.namespace}.get must return None on a miss (warn-runway)"
            # Tie the warning to *this* namespace's resource, not just any
            # DeprecationWarning: the message must name both ``<resource>s.get()``
            # and the matching ``*NotFoundError``. This is what catches the exact
            # #1358-class bug — a get() that warns, but with the wrong resource
            # (e.g. mind_maps emitting the source warning) — which a bare
            # ``pytest.warns(DeprecationWarning)`` would wave through.
            assert len(record) == 1, (
                f"{case.namespace}.get must emit exactly one DeprecationWarning on a miss"
            )
            message = str(record[0].message)
            assert f"{case.resource}s.get()" in message, (
                f"{case.namespace}.get warning must name '{case.resource}s.get()'; got: {message!r}"
            )
            assert case.not_found_error.__name__ in message, (
                f"{case.namespace}.get warning must name {case.not_found_error.__name__}; "
                f"got: {message!r}"
            )
        else:
            # Post-flip: a miss raises the namespace's *NotFoundError, and no
            # DeprecationWarning may fire on the raising path.
            with warnings.catch_warnings():
                warnings.simplefilter("error", DeprecationWarning)
                with pytest.raises(case.not_found_error):
                    await api.get(*case.get_args)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# get_or_none() — the sanctioned silent optional-lookup (invariant across the flip)
# ---------------------------------------------------------------------------


class TestGetOrNoneMissContract:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("_apply_future_errors", _FUTURE_MODES, indirect=True)
    @pytest.mark.parametrize("case", _CASES_BY_ID)
    async def test_get_or_none_on_miss_is_silent_and_none(
        self, case: LookupCase, _apply_future_errors: bool
    ) -> None:
        """Public ``get_or_none(<missing>)`` returns ``None`` with NO DeprecationWarning.

        This contract is invariant across the #1247 flip — ``get_or_none`` is the
        sanctioned ``None``-on-miss path for every namespace, before and after
        ``get`` starts raising — so it is asserted unconditionally for all rows.
        It is also invariant under the retired ``NOTEBOOKLM_FUTURE_ERRORS`` env
        var, which is ignored in v0.8.0.
        """
        api = _build_missing(case)
        with warnings.catch_warnings():
            # Escalate so any self-warn from the public get_or_none path is a
            # hard failure (the library must never trip its own get() deprecation).
            warnings.simplefilter("error", DeprecationWarning)
            result = await api.get_or_none(*case.get_args)  # type: ignore[attr-defined]
        assert result is None, f"{case.namespace}.get_or_none must return None on a miss"
