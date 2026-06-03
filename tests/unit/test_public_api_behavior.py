"""Behavioral conformance for the public ``get`` / ``get_or_none`` miss contract (ADR-019).

The static sibling, ``test_public_api_contract.py``, walks
``inspect.signature(...)`` return annotations across the whole public surface. It
is the Tier-1 *shape* floor: it proves ``get_or_none`` is annotated ``Optional``,
``delete`` returns ``None``, and every still-``Optional`` ``get`` carries a
reason-tagged exemption. But a static walk **never executes a method**, so it
cannot catch the exact historical ``mind_maps`` bug — a ``get()`` correctly
annotated ``MindMap | None`` that *forgot to warn* on a miss (#1358). That
miss-behaviour is hand-duplicated across ``_sources`` / ``_artifacts`` /
``_notes`` / ``_mind_maps_api`` as ``result = await self.get_or_none(...); if
result is None: warn_get_returns_none("x"); return result`` — exactly the kind of
copy that silently rots when one copy is dropped.

This module adds the **behavioural** half of the Tier-1 floor. For each lookup
namespace it instantiates the backing API with a fake backend (reusing the
constructor-injection substrate under ``tests/_fixtures/`` — no network, auth, or
event loop beyond what those provide) arranged to yield a genuine MISS, then
asserts today's *warn-contract*:

* ``await get(<missing id>)`` emits a ``DeprecationWarning`` **and** returns
  ``None`` (the warn-runway state #1247 will flip);
* ``await get_or_none(<missing id>)`` emits **no** ``DeprecationWarning`` **and**
  returns ``None`` (the sanctioned silent optional-lookup).

**Flip-durability (the load-bearing design choice).** The per-namespace table
:data:`LOOKUP_CASES` carries, for each namespace, its API factory, a
miss-arranger, the ``get`` arguments, the resource name, and the
``*NotFoundError`` type. The ``get_warns`` flag marks the warn-runway state. When
the v0.8.0 ``get()``→raise flip lands (#1247) a namespace migrates with a
*single table-driven edit* — flip its ``get_warns`` to ``False`` — and the
assertion automatically swaps from ``pytest.warns(DeprecationWarning)`` to
``pytest.raises(<*NotFoundError>)``. ``notebooks`` is the already-flipped
exemplar (``get_warns=False`` today: it raises ``NotebookNotFoundError`` now), so
both sides of the flip are exercised continuously and the post-flip path can
never bit-rot before #1247 arrives. This mirrors how the static
``GET_OPTIONAL_EXEMPTIONS`` allowlist *shrinks*: the deferred behaviours live in
one visible, reason-tagged table, never scattered.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._mind_map import NoteBackedMindMapService
from notebooklm._mind_maps_api import MindMapsAPI
from notebooklm._note_service import NoteService
from notebooklm._notebooks import NotebooksAPI
from notebooklm._notes import NotesAPI
from notebooklm._sources import SourcesAPI
from notebooklm.exceptions import (
    ArtifactNotFoundError,
    MindMapNotFoundError,
    NotebookNotFoundError,
    NoteNotFoundError,
    SourceNotFoundError,
)

# This behavioural table is the executable companion of the static
# ``LOOKUP_NAMESPACES`` set in ``test_public_api_contract.py``: the same five
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
# ``test_get_or_none.py`` / ``test_get_returns_none_deprecation.py`` but
# consolidated behind one flip-durable table.
# ---------------------------------------------------------------------------


def _make_sources_api() -> SourcesAPI:
    # No ``make_fake_core`` here: ``_arrange_list_miss`` overrides ``api.list``
    # before any RPC path is reached, so the first positional collaborator is
    # never called (matches how ``test_get_or_none.py`` builds its sources API).
    return SourcesAPI(MagicMock(), uploader=MagicMock())


def _make_artifacts_api() -> ArtifactsAPI:
    from _fixtures.fake_core import make_fake_core

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
    from _fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock())
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


def _make_notebooks_api() -> NotebooksAPI:
    from _fixtures.fake_core import make_fake_core

    # An empty/degenerate GET_NOTEBOOK payload is the unknown-id shape that
    # ``notebooks.get`` post-validates into ``NotebookNotFoundError`` — so this
    # factory is already arranged for a miss (see ``_arrange_notebooks_miss``).
    core = make_fake_core(rpc_call=AsyncMock(return_value=[[]]))
    return NotebooksAPI(core.rpc_executor, sources_api=MagicMock())


def _arrange_list_miss(api: object) -> None:
    """Force a miss for the four ``list``-scanning namespaces.

    ``sources`` / ``artifacts`` / ``notes`` / ``mind_maps`` all resolve a single
    ``get`` by scanning ``self.list(...)``, so an empty ``list`` is a uniform,
    backend-agnostic miss (the same lever the existing per-namespace tests pull).
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
        get_warns=True,  # flip to False with #1247
    ),
    LookupCase(
        namespace="artifacts",
        factory=_make_artifacts_api,
        arrange_miss=_arrange_list_miss,
        get_args=("nb_1", "missing"),
        resource="artifact",
        not_found_error=ArtifactNotFoundError,
        get_warns=True,  # flip to False with #1247
    ),
    LookupCase(
        namespace="notes",
        factory=_make_notes_api,
        arrange_miss=_arrange_list_miss,
        get_args=("nb_1", "missing"),
        resource="note",
        not_found_error=NoteNotFoundError,
        get_warns=True,  # flip to False with #1247
    ),
    LookupCase(
        namespace="mind_maps",
        factory=_make_mind_maps_api,
        arrange_miss=_arrange_list_miss,
        get_args=("nb_1", "missing"),
        resource="mind_map",
        not_found_error=MindMapNotFoundError,
        get_warns=True,  # flip to False with #1247
    ),
)

_CASES_BY_ID = [pytest.param(case, id=case.namespace) for case in LOOKUP_CASES]


def _build_missing(case: LookupCase) -> object:
    """Build the backing API and arrange it to yield a miss."""
    api = case.factory()
    case.arrange_miss(api)
    return api


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
    from test_public_api_contract import LOOKUP_NAMESPACES

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
    @pytest.mark.parametrize("case", _CASES_BY_ID)
    async def test_get_on_miss_warns_or_raises(self, case: LookupCase) -> None:
        """``get(<missing>)`` warns + returns ``None`` today; raises post-#1247-flip.

        The single ``get_warns`` field selects the branch, so flipping a
        namespace with #1247 is one table edit, not a rewrite of this test.
        """
        api = _build_missing(case)
        if case.get_warns:
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
    @pytest.mark.parametrize("case", _CASES_BY_ID)
    async def test_get_or_none_on_miss_is_silent_and_none(self, case: LookupCase) -> None:
        """Public ``get_or_none(<missing>)`` returns ``None`` with NO DeprecationWarning.

        This contract is invariant across the #1247 flip — ``get_or_none`` is the
        sanctioned ``None``-on-miss path for every namespace, before and after
        ``get`` starts raising — so it is asserted unconditionally for all rows.
        """
        api = _build_missing(case)
        with warnings.catch_warnings():
            # Escalate so any self-warn from the public get_or_none path is a
            # hard failure (the library must never trip its own get() deprecation).
            warnings.simplefilter("error", DeprecationWarning)
            result = await api.get_or_none(*case.get_args)  # type: ignore[attr-defined]
        assert result is None, f"{case.namespace}.get_or_none must return None on a miss"
