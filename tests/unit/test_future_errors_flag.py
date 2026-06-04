"""Unit tests for the ``NOTEBOOKLM_FUTURE_ERRORS`` opt-in preview flag.

The flag lets a process (or a CI job) run the **v0.8.0 error contract** early so
forward-compatibility can be tested before the breaking flips ship (ADR-0019,
umbrella #1346). When on, the v0.7.0 deprecation *runways* that still warn today
adopt their v0.8.0 *target* behavior:

1. ``<resource>.get()`` raises the matching ``*NotFoundError`` on a miss instead
   of warning-and-returning ``None`` (#1247), routed through
   :func:`notebooklm._lookup.resolve_get`;
2. :class:`~notebooklm._deprecation.MappingCompatMixin` dict-subscript raises
   :class:`TypeError` instead of warning-and-returning the legacy dict value
   (#1251);
3. :func:`~notebooklm._deprecation.deprecated_kwarg` raises :class:`TypeError`
   on the deprecated keyword instead of warning-and-aliasing it (#1254).

The flag also previews the three **purely-behavioral** v0.8.0 changes (#1405),
each gated the same way (``if future_errors_enabled(): <v0.8.0> else: <v0.7.0>``):

4. uninformative ``bool`` returns become ``None`` — ``sources.refresh`` and
   ``chat.delete_conversation`` (#1290; ``chat.clear_cache`` is *not* gated, its
   bool is meaningful);
5. a synchronous generation refusal **raises** the decoder's
   ``RateLimitError`` / ``RPCError`` / ``DecodingError`` /
   ``ArtifactFeatureUnavailableError`` instead of being swallowed into
   ``GenerationStatus(status="failed")`` / returned ``None`` — ``_call_generate``,
   ``revise_slide``, ``_parse_generation_result``'s missing-id branch, and
   ``research.start``'s empty-payload branch (#1342);
6. mutate-existing ops fail loud on a missing target — ``notes.update`` and
   ``sources``/``artifacts`` ``rename(return_object=False)`` raise
   ``*NotFoundError`` (#1362).

Default-off must be byte-identical to current v0.7.0 behavior, and the flag
takes precedence over ``NOTEBOOKLM_QUIET_DEPRECATIONS`` (a runway raises
regardless of quiet; quiet only silences the warn path future mode replaces).
The behavioral conformance for the ``get()`` flip across all five namespaces
lives in ``test_public_api_behavior.py`` (run under both modes); this module
covers the resolver, the two non-``get`` flips, the precedence rule, and the
six behavioral previews above (one new test class per gated behavior).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm import _deprecation
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._chat import ChatAPI
from notebooklm._deprecation import (
    MappingCompatMixin,
    deprecated_kwarg,
    future_errors_enabled,
)
from notebooklm._lookup import resolve_get
from notebooklm._mind_map import NoteBackedMindMapService
from notebooklm._note_service import NoteService
from notebooklm._notes import NotesAPI
from notebooklm._research import ResearchAPI
from notebooklm._runtime.contracts import LoopGuard, RpcCaller
from notebooklm._sources import SourcesAPI
from notebooklm.exceptions import (
    ArtifactFeatureUnavailableError,
    ArtifactNotFoundError,
    DecodingError,
    NoteNotFoundError,
    RateLimitError,
    RPCError,
    SourceNotFoundError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.types import Source

_FLAG = "NOTEBOOKLM_FUTURE_ERRORS"
_QUIET = "NOTEBOOKLM_QUIET_DEPRECATIONS"
_UNSET = object()


# ---------------------------------------------------------------------------
# future_errors_enabled() — the resolver (mirrors the quiet resolver)
# ---------------------------------------------------------------------------


class TestFutureErrorsResolver:
    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        assert future_errors_enabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "Yes", "on", "ON"])
    def test_truthy_values_enable(self, monkeypatch, truthy):
        monkeypatch.setenv(_FLAG, truthy)
        assert future_errors_enabled() is True

    @pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off", "  "])
    def test_falsy_values_stay_off(self, monkeypatch, falsy):
        monkeypatch.setenv(_FLAG, falsy)
        assert future_errors_enabled() is False

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "  on  ")
        assert future_errors_enabled() is True

    def test_read_live_not_cached(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        assert future_errors_enabled() is False
        monkeypatch.setenv(_FLAG, "1")
        assert future_errors_enabled() is True
        monkeypatch.delenv(_FLAG, raising=False)
        assert future_errors_enabled() is False


# ---------------------------------------------------------------------------
# resolve_get() — the shared get()-miss bridge (#1247)
# ---------------------------------------------------------------------------


class _Sentinel(Exception):
    """A distinct exception type so ``pytest.raises`` cannot match by accident."""


class TestResolveGet:
    def test_hit_returns_value_no_warn_no_raise_off(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = resolve_get("found", not_found=_Sentinel(), resource="source")
        assert result == "found"

    def test_hit_returns_value_no_raise_on(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        # A hit never raises, even under future-errors: the flip is miss-only.
        result = resolve_get("found", not_found=_Sentinel(), resource="source")
        assert result == "found"

    def test_miss_off_warns_and_returns_none(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        monkeypatch.delenv(_QUIET, raising=False)
        with pytest.warns(DeprecationWarning, match="sources.get()") as record:
            result = resolve_get(None, not_found=_Sentinel(), resource="source")
        assert result is None
        assert len(record) == 1

    def test_miss_on_raises_the_not_found(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with pytest.raises(_Sentinel):
                resolve_get(None, not_found=_Sentinel(), resource="source")

    def test_warning_points_at_caller_through_bridge(self, monkeypatch):
        # stacklevel bookkeeping: resolve_get bumps warn_get_returns_none to
        # stacklevel=4 to account for the extra bridge frame
        # (warn (1) -> resolve_get (2) -> public get() (3) -> user (4)). The
        # ``_fake_public_get`` wrapper stands in for the public ``get()`` frame
        # so the warning is attributed to the *caller of get()* — this line —
        # not to _lookup.py / _deprecation.py.
        monkeypatch.delenv(_FLAG, raising=False)
        monkeypatch.delenv(_QUIET, raising=False)

        def _fake_public_get() -> object:
            return resolve_get(None, not_found=_Sentinel(), resource="source")

        with pytest.warns(DeprecationWarning) as record:
            _fake_public_get()
        assert record[0].filename == __file__


# ---------------------------------------------------------------------------
# MappingCompatMixin — full mapping-surface flip (#1251)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CompatProbe(MappingCompatMixin):
    """Minimal mixin subclass mirroring the real typed-dataclass returns."""

    status: str = "completed"

    def to_public_dict(self) -> dict[str, Any]:
        return {"status": self.status}


class TestMappingCompatFlip:
    def test_off_warns_and_returns_legacy_value(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        monkeypatch.delenv(_QUIET, raising=False)
        probe = _CompatProbe()
        with pytest.warns(DeprecationWarning, match="dict-style access"):
            value = probe["status"]
        assert value == "completed"

    def test_on_raises_typeerror_not_subscriptable(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        probe = _CompatProbe()
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with pytest.raises(TypeError, match="not subscriptable"):
                probe["status"]

    def test_on_message_matches_plain_dataclass(self, monkeypatch):
        # The previewed error must read like the real v0.8.0 one: a plain
        # dataclass with no __getitem__ raises "'X' object is not subscriptable".
        @dataclass(frozen=True)
        class _Plain:
            status: str = "completed"

        plain_msg = ""
        try:
            _Plain()["status"]  # type: ignore[index]
        except TypeError as exc:
            plain_msg = str(exc)

        monkeypatch.setenv(_FLAG, "1")
        with pytest.raises(TypeError) as caught:
            _CompatProbe()["status"]
        # Same shape: "'<Type>' object is not subscriptable" (only the type name
        # differs between the plain dataclass and the mixin subclass).
        assert "object is not subscriptable" in plain_msg
        assert "object is not subscriptable" in str(caught.value)

    def test_on_full_mapping_surface_raises(self, monkeypatch):
        # In v0.8.0 the mixin is gone, so EVERY mapping op breaks — not just
        # subscript. The flag previews each with the exact error a bare
        # attribute-only dataclass raises: AttributeError for the method-style
        # shims, TypeError for the operator-style ones (#1251).
        monkeypatch.setenv(_FLAG, "1")
        probe = _CompatProbe()
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with pytest.raises(AttributeError, match="has no attribute 'get'"):
                probe.get("status")
            with pytest.raises(AttributeError, match="has no attribute 'keys'"):
                probe.keys()
            with pytest.raises(AttributeError, match="has no attribute 'items'"):
                probe.items()
            with pytest.raises(AttributeError, match="has no attribute 'values'"):
                probe.values()
            with pytest.raises(TypeError, match="has no len"):
                len(probe)
            with pytest.raises(TypeError, match="not iterable"):
                assert "status" in probe
            with pytest.raises(TypeError, match="not iterable"):
                iter(probe)

    def test_off_mapping_surface_stays_silent(self, monkeypatch):
        # Off the flag the shape-probe surface is unchanged (no warning storm):
        # get/keys/in/iter/len keep returning the legacy dict shape quietly.
        monkeypatch.delenv(_FLAG, raising=False)
        monkeypatch.delenv(_QUIET, raising=False)
        probe = _CompatProbe()
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert probe.get("status") == "completed"
            assert "status" in probe
            assert list(probe.keys()) == ["status"]
            assert len(probe) == 1
            assert list(iter(probe)) == ["status"]


# ---------------------------------------------------------------------------
# deprecated_kwarg — renamed-keyword flip (#1254)
# ---------------------------------------------------------------------------


class TestDeprecatedKwargFlip:
    def test_off_old_only_warns_and_aliases(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        monkeypatch.delenv(_QUIET, raising=False)
        with pytest.warns(DeprecationWarning, match="deprecated"):
            result = deprecated_kwarg(
                2.0,
                _UNSET,
                old="interval",
                new="initial_interval",
                owner="X.m",
                sentinel=_UNSET,
            )
        assert result == 2.0

    def test_on_old_passed_raises_typeerror(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            with pytest.raises(TypeError, match="unexpected keyword argument 'interval'"):
                deprecated_kwarg(
                    2.0,
                    _UNSET,
                    old="interval",
                    new="initial_interval",
                    owner="X.m",
                    sentinel=_UNSET,
                )

    def test_on_new_only_still_works(self, monkeypatch):
        # The canonical keyword is unaffected by the flag.
        monkeypatch.setenv(_FLAG, "1")
        result = deprecated_kwarg(
            _UNSET,
            3.0,
            old="interval",
            new="initial_interval",
            owner="X.m",
            sentinel=_UNSET,
        )
        assert result == 3.0

    def test_on_neither_passed_returns_sentinel(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        result = deprecated_kwarg(
            _UNSET,
            _UNSET,
            old="interval",
            new="initial_interval",
            owner="X.m",
            sentinel=_UNSET,
        )
        assert result is _UNSET

    def test_both_passed_still_raises_under_both_modes(self, monkeypatch):
        # The pre-existing both-passed ambiguity TypeError is independent of the
        # flag — it must keep raising whether the preview is on or off.
        for flag in ("1", None):
            if flag is None:
                monkeypatch.delenv(_FLAG, raising=False)
            else:
                monkeypatch.setenv(_FLAG, flag)
            with pytest.raises(TypeError, match="both"):
                deprecated_kwarg(
                    2.0,
                    3.0,
                    old="interval",
                    new="initial_interval",
                    owner="X.m",
                    sentinel=_UNSET,
                )


# ---------------------------------------------------------------------------
# Precedence: FUTURE_ERRORS overrides QUIET_DEPRECATIONS for all three flips
# ---------------------------------------------------------------------------


class TestFutureErrorsTakesPrecedenceOverQuiet:
    def test_resolve_get_raises_even_when_quiet(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_QUIET, "1")
        with pytest.raises(_Sentinel):
            resolve_get(None, not_found=_Sentinel(), resource="source")

    def test_subscript_raises_even_when_quiet(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_QUIET, "1")
        with pytest.raises(TypeError, match="not subscriptable"):
            _CompatProbe()["status"]

    def test_deprecated_kwarg_raises_even_when_quiet(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_QUIET, "1")
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            deprecated_kwarg(
                2.0,
                _UNSET,
                old="interval",
                new="initial_interval",
                owner="X.m",
                sentinel=_UNSET,
            )

    def test_quiet_alone_silences_warn_path_off(self, monkeypatch):
        # Sanity: with the flag OFF, quiet still just silences (no raise),
        # proving the precedence is specifically the flag's doing.
        monkeypatch.delenv(_FLAG, raising=False)
        monkeypatch.setenv(_QUIET, "1")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert resolve_get(None, not_found=_Sentinel(), resource="source") is None
            assert _CompatProbe()["status"] == "completed"


# ---------------------------------------------------------------------------
# Default-off is byte-identical: the public alias matches the private resolver
# ---------------------------------------------------------------------------


def test_public_alias_matches_private_resolver(monkeypatch):
    for value in ("1", "0", "", "yes", "off"):
        monkeypatch.setenv(_FLAG, value)
        assert future_errors_enabled() == _deprecation._future_errors_enabled()
    monkeypatch.delenv(_FLAG, raising=False)
    assert future_errors_enabled() == _deprecation._future_errors_enabled()


# ===========================================================================
# Behavioral previews (#1405): one new test class per gated behavior. Each
# asserts flag-OFF = current v0.7.0 behavior AND flag-ON = the v0.8.0 target,
# using the same setenv/delenv(_FLAG) idiom as the classes above.
# ===========================================================================


def _make_artifacts_api(rpc_call: AsyncMock) -> ArtifactsAPI:
    """Build a minimal ``ArtifactsAPI`` over a single ``rpc_call`` seam.

    ADR-0007: the ``rpc_call`` seam is injected via ``make_fake_core`` rather
    than dotted attribute assignment so the forbidden-monkeypatch lint stays
    clean.
    """
    from _fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=rpc_call, get_source_ids=AsyncMock(return_value=[]))
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


# ---------------------------------------------------------------------------
# #1290 — uninformative bool returns become None
# ---------------------------------------------------------------------------


class TestBoolReturnsBecomeNone:
    """``sources.refresh`` / ``chat.delete_conversation`` return ``None`` flag-on.

    The ``True`` they return today carries no information (any failure raises
    first), so under the flag they preview the v0.8.0 ``-> None`` return. The
    ``-> bool`` annotation is intentionally preserved at runtime; only the
    returned *value* flips. ``chat.clear_cache`` is deliberately NOT gated — its
    bool is meaningful (the cache reports whether the id was present).
    """

    @pytest.mark.asyncio
    async def test_sources_refresh_off_returns_true(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        api = SourcesAPI(MagicMock(rpc_call=AsyncMock(return_value=None)), uploader=MagicMock())
        assert await api.refresh("nb_1", "src_1") is True

    @pytest.mark.asyncio
    async def test_sources_refresh_on_returns_none(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        api = SourcesAPI(MagicMock(rpc_call=AsyncMock(return_value=None)), uploader=MagicMock())
        assert await api.refresh("nb_1", "src_1") is None

    def _chat_api(self) -> ChatAPI:
        return ChatAPI(
            rpc=MagicMock(spec=RpcCaller, rpc_call=AsyncMock(return_value=None)),
            transport=MagicMock(),
            reqid=MagicMock(),
            loop_guard=MagicMock(spec=LoopGuard),
        )

    @pytest.mark.asyncio
    async def test_delete_conversation_off_returns_true(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        api = self._chat_api()
        assert await api.delete_conversation("nb_1", "conv_1") is True

    @pytest.mark.asyncio
    async def test_delete_conversation_on_returns_none(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        api = self._chat_api()
        assert await api.delete_conversation("nb_1", "conv_1") is None

    def test_clear_cache_is_never_gated(self, monkeypatch):
        # clear_cache's bool is meaningful (id present/absent), so it must NOT
        # be touched by the flag in either mode.
        monkeypatch.setenv(_FLAG, "1")
        api = self._chat_api()
        api._cache.cache_conversation_turn("conv_1", "Q?", "A.", turn_number=1)
        assert api.clear_cache("conv_1") is True  # present -> True even under the flag
        assert api.clear_cache("conv_missing") is False  # absent -> meaningful False


# ---------------------------------------------------------------------------
# #1342 — synchronous generation refusal raises (drops status="failed")
# ---------------------------------------------------------------------------


class TestRefusalRaises:
    """A synchronous refusal raises under the flag instead of soft-failing."""

    @pytest.mark.asyncio
    async def test_call_generate_off_swallows_to_failed_status(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        rpc = AsyncMock(
            side_effect=RateLimitError("Rate limit exceeded", rpc_code="USER_DISPLAYABLE_ERROR")
        )
        api = _make_artifacts_api(rpc)
        result = await api.generate_video("nb_1")
        assert result.status == "failed"
        assert result.error_code == "USER_DISPLAYABLE_ERROR"
        assert result.is_rate_limited is True

    @pytest.mark.asyncio
    async def test_call_generate_on_raises_rate_limit(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        rpc = AsyncMock(
            side_effect=RateLimitError("Rate limit exceeded", rpc_code="USER_DISPLAYABLE_ERROR")
        )
        api = _make_artifacts_api(rpc)
        with pytest.raises(RateLimitError, match="Rate limit"):
            await api.generate_video("nb_1")

    @pytest.mark.asyncio
    async def test_call_generate_non_refusal_propagates_both_modes(self, monkeypatch):
        # A non-USER_DISPLAYABLE_ERROR RPCError always propagates, flag or not.
        for value in ("1", None):
            if value is None:
                monkeypatch.delenv(_FLAG, raising=False)
            else:
                monkeypatch.setenv(_FLAG, value)
            rpc = AsyncMock(side_effect=RPCError("Server error", rpc_code="INTERNAL_ERROR"))
            api = _make_artifacts_api(rpc)
            with pytest.raises(RPCError, match="Server error"):
                await api.generate_video("nb_1")

    @pytest.mark.asyncio
    async def test_revise_slide_off_swallows_to_failed_status(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        rpc = AsyncMock(side_effect=RPCError("Refused", rpc_code="USER_DISPLAYABLE_ERROR"))
        api = _make_artifacts_api(rpc)
        result = await api.revise_slide("nb_1", "art_1", 0, "make it pop")
        assert result.status == "failed"
        assert result.error_code == "USER_DISPLAYABLE_ERROR"

    @pytest.mark.asyncio
    async def test_revise_slide_on_raises(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        rpc = AsyncMock(side_effect=RPCError("Refused", rpc_code="USER_DISPLAYABLE_ERROR"))
        api = _make_artifacts_api(rpc)
        with pytest.raises(RPCError, match="Refused"):
            await api.revise_slide("nb_1", "art_1", 0, "make it pop")

    def test_parse_generation_result_null_id_off_returns_failed(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        api = _make_artifacts_api(AsyncMock())
        # A well-structured row whose artifact id (result[0][0]) is null.
        status = api._parse_generation_result(
            [[None, "Title", 1, None, 1]], method_id=RPCMethod.CREATE_ARTIFACT.value
        )
        assert status.status == "failed"
        assert status.task_id == ""

    def test_parse_generation_result_null_id_on_raises_feature_unavailable(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        api = _make_artifacts_api(AsyncMock())
        with pytest.raises(ArtifactFeatureUnavailableError):
            api._parse_generation_result(
                [[None, "Title", 1, None, 1]], method_id=RPCMethod.CREATE_ARTIFACT.value
            )

    def test_parse_generation_result_empty_id_on_raises_decoding(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        api = _make_artifacts_api(AsyncMock())
        # A falsey-but-non-null id (``""``) is degenerate shape drift -> DecodingError.
        with pytest.raises(DecodingError):
            api._parse_generation_result(
                [["", "Title", 1, None, 1]], method_id=RPCMethod.CREATE_ARTIFACT.value
            )

    @pytest.mark.asyncio
    async def test_research_start_empty_payload_off_returns_none(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        api = ResearchAPI(MagicMock(rpc_call=AsyncMock(return_value=[])))
        assert await api.start("nb_1", "query") is None

    @pytest.mark.asyncio
    async def test_research_start_empty_payload_on_raises_decoding(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        api = ResearchAPI(MagicMock(rpc_call=AsyncMock(return_value=[])))
        with pytest.raises(DecodingError):
            await api.start("nb_1", "query")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("falsey_id", [None, "", 0])
    async def test_research_start_falsey_task_id_off_builds_handle(self, monkeypatch, falsey_id):
        # A non-empty payload whose task_id is falsey still builds a (degenerate)
        # ResearchStart on the v0.7.0 default path — byte-for-byte unchanged.
        monkeypatch.delenv(_FLAG, raising=False)
        api = ResearchAPI(MagicMock(rpc_call=AsyncMock(return_value=[falsey_id, "report_1"])))
        result = await api.start("nb_1", "query")
        assert result is not None
        assert result.task_id == falsey_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize("falsey_id", [None, "", 0])
    async def test_research_start_falsey_task_id_on_raises_decoding(self, monkeypatch, falsey_id):
        # Under the flag a falsey task_id means no task was created — raise
        # (mirrors _parse_generation_result's missing-id branch).
        monkeypatch.setenv(_FLAG, "1")
        api = ResearchAPI(MagicMock(rpc_call=AsyncMock(return_value=[falsey_id, "report_1"])))
        with pytest.raises(DecodingError):
            await api.start("nb_1", "query")

    @pytest.mark.asyncio
    async def test_research_start_real_task_id_on_returns_handle(self, monkeypatch):
        # A truthy task_id is unaffected by the flag.
        monkeypatch.setenv(_FLAG, "1")
        api = ResearchAPI(MagicMock(rpc_call=AsyncMock(return_value=["task_1", "report_1"])))
        result = await api.start("nb_1", "query")
        assert result is not None
        assert result.task_id == "task_1"


# ---------------------------------------------------------------------------
# #1362 — mutate-existing fail-loud on a missing target
# ---------------------------------------------------------------------------


class TestMutateExistingFailLoud:
    """``notes.update`` and ``rename(return_object=False)`` raise on a miss."""

    def _notes_api(self) -> NotesAPI:
        from _fixtures.fake_core import make_fake_core

        core = make_fake_core(rpc_call=AsyncMock())
        note_service = NoteService(core)
        mind_maps = NoteBackedMindMapService(note_service)
        return NotesAPI(notes=note_service, mind_maps=mind_maps)

    @pytest.mark.asyncio
    async def test_notes_update_off_silently_noops_on_miss(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        api = self._notes_api()
        # No preflight on the off path: a missing note must NOT trigger a lookup.
        api.get_or_none = AsyncMock(return_value=None)
        api._notes.update_note = AsyncMock(return_value=None)
        await api.update("nb_1", "missing", "content", "Title")
        api.get_or_none.assert_not_awaited()
        api._notes.update_note.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_notes_update_on_raises_on_miss(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        api = self._notes_api()
        api.get_or_none = AsyncMock(return_value=None)
        api._notes.update_note = AsyncMock(return_value=None)
        with pytest.raises(NoteNotFoundError):
            await api.update("nb_1", "missing", "content", "Title")
        # Fail-loud: the underlying update RPC must not fire on a miss.
        api._notes.update_note.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notes_update_on_succeeds_when_present(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        api = self._notes_api()
        api.get_or_none = AsyncMock(return_value=MagicMock())  # hit
        api._notes.update_note = AsyncMock(return_value=None)
        await api.update("nb_1", "note_1", "content", "Title")
        api._notes.update_note.assert_awaited_once()

    def _sources_api(self, *, echo: object) -> SourcesAPI:
        # ``echo`` is the UPDATE_SOURCE return; ``None`` forces the existence
        # preflight on the future-errors path.
        return SourcesAPI(MagicMock(rpc_call=AsyncMock(return_value=echo)), uploader=MagicMock())

    @pytest.mark.asyncio
    async def test_sources_rename_no_object_off_skips_detection(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        api = self._sources_api(echo=None)
        api._get_or_none = AsyncMock(return_value=None)  # would-be miss
        # Off path short-circuits to None WITHOUT probing existence.
        assert await api.rename("nb_1", "missing", "T", return_object=False) is None
        api._get_or_none.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sources_rename_no_object_on_raises_on_miss(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        api = self._sources_api(echo=None)
        api._get_or_none = AsyncMock(return_value=None)  # miss
        with pytest.raises(SourceNotFoundError):
            await api.rename("nb_1", "missing", "T", return_object=False)

    @pytest.mark.asyncio
    async def test_sources_rename_no_object_on_returns_none_when_present(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        api = self._sources_api(echo=None)
        api._get_or_none = AsyncMock(return_value=Source(id="src_1", title="T"))  # hit
        # The flag controls miss-detection, not the return: still None on a hit.
        assert await api.rename("nb_1", "src_1", "T", return_object=False) is None

    def _artifacts_api_for_rename(self) -> ArtifactsAPI:
        # UPDATE/RENAME echo is None so the False path reaches the studio-only
        # existence preflight under the flag.
        return _make_artifacts_api(AsyncMock(return_value=None))

    @pytest.mark.asyncio
    async def test_artifacts_rename_no_object_off_skips_detection(self, monkeypatch):
        monkeypatch.delenv(_FLAG, raising=False)
        api = self._artifacts_api_for_rename()
        api._listing.get_studio_only = AsyncMock(return_value=None)  # would-be miss
        assert await api.rename("nb_1", "missing", "T", return_object=False) is None
        api._listing.get_studio_only.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_artifacts_rename_no_object_on_raises_on_miss(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        api = self._artifacts_api_for_rename()
        api._listing.get_studio_only = AsyncMock(return_value=None)  # miss
        with pytest.raises(ArtifactNotFoundError):
            await api.rename("nb_1", "missing", "T", return_object=False)

    @pytest.mark.asyncio
    async def test_artifacts_rename_no_object_on_returns_none_when_present(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        api = self._artifacts_api_for_rename()
        api._listing.get_studio_only = AsyncMock(return_value=MagicMock())  # hit
        assert await api.rename("nb_1", "art_1", "T", return_object=False) is None
