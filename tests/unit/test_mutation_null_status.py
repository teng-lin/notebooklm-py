"""#2290: a status-tagged null on a write RPC is a rejection, not a success.

``allow_null=True`` lets a genuinely empty success payload decode to ``None``.
Until #2290 the same flag also swallowed a null the server had *tagged* with a
non-OK ``google.rpc.Status`` — live, ``REFRESH_SOURCE`` (``FLmJqe``) answered
``[3]`` INVALID_ARGUMENT and ``sources.refresh()`` handed its caller ``None``,
the documented success value. Every write RPC pinned here now passes
``raise_on_null_status=True`` so the server's "no" surfaces as ``RPCError``
while an untagged empty payload still decodes to ``None``.

Three layers:

* the refresh path end-to-end through the production client shell (real
  executor + decoder, ``httpx_mock`` at the wire);
* one row per changed call site pinning that the strictness kwarg reaches the
  ``RpcCaller`` and that the rejection propagates unwrapped;
* an AST guard over ``src/notebooklm/_web`` so a future ``allow_null=True`` on
  one of these write RPCs cannot land without the strictness kwarg.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._web.artifacts import WebArtifactsAPI
from notebooklm._web.chat import WebChatAPI
from notebooklm._web.collections import WebCollectionsAPI
from notebooklm._web.labels import WebLabelsAPI
from notebooklm._web.mind_maps import NoteBackedMindMapService
from notebooklm._web.notebooks import WebNotebooksAPI
from notebooklm._web.notes import NoteService
from notebooklm._web.sharing import WebSharingAPI
from notebooklm._web.sources import WebSourcesAPI
from notebooklm._web.sources.upload import SourceUploadPipeline
from notebooklm.auth import AuthTokens
from notebooklm.exceptions import RPCError
from notebooklm.rpc import RPCMethod
from notebooklm.types import ShareViewLevel
from tests._helpers.client_factory import build_client_shell_for_tests

NB, SRC, TITLE = "nb-1", "src-1", "Renamed"

#: Write RPCs whose success payload the client reads as "done" without a
#: verifying re-read. Every ``allow_null=True`` call on one of these must carry
#: ``raise_on_null_status=True`` (see ``test_write_rpcs_do_not_swallow_a_status``).
STRICT_WRITE_RPCS = frozenset(
    {
        RPCMethod.REFRESH_SOURCE,
        RPCMethod.UPDATE_SOURCE,
        RPCMethod.RENAME_NOTEBOOK,
        RPCMethod.UPDATE_NOTE,
        RPCMethod.UPDATE_LABEL,
        RPCMethod.CREATE_LABEL,
        RPCMethod.RENAME_ARTIFACT,
        RPCMethod.EXPORT_ARTIFACT,
        RPCMethod.GENERATE_MIND_MAP,
    }
)


def _wire(rpc_id: str, status: list[int] | None) -> str:
    """One batchexecute body: a null payload for ``rpc_id``, index 5 = ``status``."""
    chunk = json.dumps([["wrb.fr", rpc_id, None, None, None, status, "generic"]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


# ---------------------------------------------------------------------------
# refresh() end-to-end: real executor + decoder, fabricated wire
# ---------------------------------------------------------------------------


class TestRefreshEndToEnd:
    """The exact #2290 repro, replayed through the production client shell."""

    _AUTH = AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="sid")
    _BATCHEXECUTE = re.compile(r"https://[^/]+/_/LabsTailwindUi/data/batchexecute.*")

    @pytest.mark.asyncio
    async def test_invalid_argument_raises_instead_of_returning_none(self, httpx_mock) -> None:
        httpx_mock.add_response(
            method="POST",
            url=self._BATCHEXECUTE,
            text=_wire(RPCMethod.REFRESH_SOURCE.value, [3]),
        )
        async with build_client_shell_for_tests(self._AUTH) as client:
            with pytest.raises(RPCError) as exc_info:
                await client.sources.refresh(NB, SRC)

        assert exc_info.value.rpc_code == 3
        assert exc_info.value.method_id == RPCMethod.REFRESH_SOURCE.value
        assert "invalid argument" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_untagged_null_is_still_success(self, httpx_mock) -> None:
        """The recorded success frame (``tests/cassettes/web/sources_refresh_direct.yaml``)
        is a null payload with nothing at index 5 — that must keep decoding to ``None``."""
        httpx_mock.add_response(
            method="POST",
            url=self._BATCHEXECUTE,
            text=_wire(RPCMethod.REFRESH_SOURCE.value, None),
        )
        async with build_client_shell_for_tests(self._AUTH) as client:
            assert await client.sources.refresh(NB, SRC) is None

    @pytest.mark.asyncio
    async def test_ok_status_on_a_null_is_still_success(self, httpx_mock) -> None:
        httpx_mock.add_response(
            method="POST",
            url=self._BATCHEXECUTE,
            text=_wire(RPCMethod.REFRESH_SOURCE.value, [0]),
        )
        async with build_client_shell_for_tests(self._AUTH) as client:
            assert await client.sources.refresh(NB, SRC) is None


# ---------------------------------------------------------------------------
# Per-site contract: the strictness kwarg reaches the RpcCaller
# ---------------------------------------------------------------------------


class _RoutedRpc:
    """``RpcCaller`` stub: canned answers per method, a rejection on ``target``.

    Records every call's keyword arguments so a case can prove BOTH that the
    rejection propagated AND that the call site asked for strictness — the
    decoder only raises on a status-tagged null when told to, so the kwarg is
    the whole fix.
    """

    def __init__(self, target: RPCMethod, canned: dict[RPCMethod, Any] | None = None) -> None:
        self.target = target
        self.canned = canned or {}
        self.calls: list[tuple[RPCMethod, dict[str, Any]]] = []

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.calls.append((method, {"allow_null": allow_null, **kwargs}))
        if method is self.target:
            raise RPCError(
                "The server rejected this request (invalid argument).",
                method_id=method.value,
                rpc_code=3,
            )
        return self.canned.get(method)

    def kwargs_for(self, method: RPCMethod) -> dict[str, Any]:
        matching = [kw for m, kw in self.calls if m is method]
        assert matching, f"{method.name} was never called; calls={self.calls!r}"
        return matching[0]


def _sources(rpc: _RoutedRpc) -> WebSourcesAPI:
    return WebSourcesAPI(rpc, supervisor=MagicMock(), uploader=MagicMock())


def _uploader(rpc: _RoutedRpc) -> SourceUploadPipeline:
    auth = MagicMock()
    auth.authuser = 0
    auth.account_email = None
    return SourceUploadPipeline(rpc=rpc, supervisor=MagicMock(), kernel=MagicMock(), auth=auth)


def _notebooks(rpc: _RoutedRpc) -> WebNotebooksAPI:
    return WebNotebooksAPI(rpc, sources_api=MagicMock())


def _chat(rpc: _RoutedRpc) -> WebChatAPI:
    return WebChatAPI(
        rpc=rpc,
        transport=MagicMock(),
        reqid=MagicMock(),
        loop_guard=SimpleNamespace(assert_bound_loop=lambda: None),
        notebooks=MagicMock(),
    )


def _notes(rpc: _RoutedRpc) -> NoteService:
    return NoteService(rpc, supervisor=MagicMock())


def _labels(rpc: _RoutedRpc) -> WebLabelsAPI:
    return WebLabelsAPI(rpc, list_sources=AsyncMock(return_value=[]))


def _collections(rpc: _RoutedRpc) -> WebCollectionsAPI:
    return WebCollectionsAPI(rpc, list_notebooks=AsyncMock(return_value=[]))


def _artifacts(rpc: _RoutedRpc) -> WebArtifactsAPI:
    notebooks = MagicMock()
    notebooks.get_source_ids = AsyncMock(return_value=[SRC])
    return WebArtifactsAPI(
        rpc=rpc,
        supervisor=MagicMock(),
        notebooks=notebooks,
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )


# LIST_LABELS echoes ``[[label, ...]]`` for source labels and ``[None, [collection,
# ...]]`` for collections; both tuples are ``[name, members, id, emoji]``.
_LABEL_LIST = [[["Topic", None, "lbl-1", ""]]]
_COLLECTION_LIST = [None, [["Work", None, "col-1", ""]]]

Case = tuple[str, RPCMethod, dict[RPCMethod, Any], Callable[[_RoutedRpc], Awaitable[Any]]]

CASES: list[Case] = [
    ("sources.refresh", RPCMethod.REFRESH_SOURCE, {}, lambda r: _sources(r).refresh(NB, SRC)),
    ("sources.rename", RPCMethod.UPDATE_SOURCE, {}, lambda r: _sources(r).rename(NB, SRC, TITLE)),
    (
        "upload.rename (post-upload retitle)",
        RPCMethod.UPDATE_SOURCE,
        {},
        lambda r: _uploader(r).rename(NB, SRC, TITLE),
    ),
    (
        "notebooks.update",
        RPCMethod.RENAME_NOTEBOOK,
        {},
        lambda r: _notebooks(r).update(NB, title=TITLE),
    ),
    ("chat.configure", RPCMethod.RENAME_NOTEBOOK, {}, lambda r: _chat(r).configure(NB)),
    (
        "sharing.set_view_level",
        RPCMethod.RENAME_NOTEBOOK,
        {},
        lambda r: WebSharingAPI(r).set_view_level(NB, ShareViewLevel.CHAT_ONLY),
    ),
    (
        "notes.update_note",
        RPCMethod.UPDATE_NOTE,
        {},
        lambda r: _notes(r).update_note(NB, "note-1", "body", TITLE),
    ),
    ("labels.generate", RPCMethod.CREATE_LABEL, {}, lambda r: _labels(r).generate(NB)),
    (
        "labels.create",
        RPCMethod.CREATE_LABEL,
        {RPCMethod.LIST_LABELS: _LABEL_LIST},
        lambda r: _labels(r).create(NB, TITLE),
    ),
    (
        "collections.create",
        RPCMethod.CREATE_LABEL,
        {RPCMethod.LIST_LABELS: _COLLECTION_LIST},
        lambda r: _collections(r).create(TITLE),
    ),
    (
        "labels.update",
        RPCMethod.UPDATE_LABEL,
        {RPCMethod.LIST_LABELS: _LABEL_LIST},
        lambda r: _labels(r).update(NB, "lbl-1", name=TITLE),
    ),
    (
        "labels.add_sources",
        RPCMethod.UPDATE_LABEL,
        {RPCMethod.LIST_LABELS: _LABEL_LIST},
        lambda r: _labels(r).add_sources(NB, "lbl-1", [SRC]),
    ),
    (
        "labels.remove_sources",
        RPCMethod.UPDATE_LABEL,
        {RPCMethod.LIST_LABELS: _LABEL_LIST},
        lambda r: _labels(r).remove_sources(NB, "lbl-1", [SRC]),
    ),
    (
        "collections.rename",
        RPCMethod.UPDATE_LABEL,
        {RPCMethod.LIST_LABELS: _COLLECTION_LIST},
        lambda r: _collections(r).rename("col-1", TITLE),
    ),
    (
        "collections.add_notebooks",
        RPCMethod.UPDATE_LABEL,
        {RPCMethod.LIST_LABELS: _COLLECTION_LIST},
        lambda r: _collections(r).add_notebooks("col-1", [NB]),
    ),
    (
        "collections.remove_notebooks",
        RPCMethod.UPDATE_LABEL,
        {RPCMethod.LIST_LABELS: _COLLECTION_LIST},
        lambda r: _collections(r).remove_notebooks("col-1", [NB]),
    ),
    (
        "artifacts.rename",
        RPCMethod.RENAME_ARTIFACT,
        {},
        lambda r: _artifacts(r).rename(NB, "art-1", TITLE),
    ),
    (
        "artifacts.export_report",
        RPCMethod.EXPORT_ARTIFACT,
        {},
        lambda r: _artifacts(r).export_report(NB, "art-1"),
    ),
    (
        "artifacts.export_data_table",
        RPCMethod.EXPORT_ARTIFACT,
        {},
        lambda r: _artifacts(r).export_data_table(NB, "art-1"),
    ),
    (
        "artifacts.export",
        RPCMethod.EXPORT_ARTIFACT,
        {},
        lambda r: _artifacts(r).export(NB, "art-1"),
    ),
    (
        "artifacts.generate_mind_map",
        RPCMethod.GENERATE_MIND_MAP,
        {},
        lambda r: _artifacts(r).generate_mind_map(NB, source_ids=[SRC]),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("label", "target", "canned", "run"), CASES, ids=[c[0] for c in CASES])
async def test_write_site_asks_for_strictness_and_propagates_the_rejection(
    label: str,
    target: RPCMethod,
    canned: dict[RPCMethod, Any],
    run: Callable[[_RoutedRpc], Awaitable[Any]],
) -> None:
    rpc = _RoutedRpc(target, canned)

    with pytest.raises(RPCError) as exc_info:
        await run(rpc)

    assert exc_info.value.rpc_code == 3, label
    kwargs = rpc.kwargs_for(target)
    assert kwargs["allow_null"] is True, (
        f"{label}: allow_null dropped — empty success must still decode"
    )
    assert kwargs.get("raise_on_null_status") is True, (
        f"{label}: the decoder swallows a status-tagged null unless the call site "
        f"passes raise_on_null_status=True (#2290)"
    )


@pytest.mark.asyncio
async def test_refresh_returns_none_when_the_server_accepts() -> None:
    """The documented success value is unchanged (#1290)."""
    rpc = MagicMock(rpc_call=AsyncMock(return_value=None))
    api = WebSourcesAPI(rpc, supervisor=MagicMock(), uploader=MagicMock())

    assert await api.refresh(NB, SRC) is None
    assert rpc.rpc_call.await_args.args[0] is RPCMethod.REFRESH_SOURCE
    assert rpc.rpc_call.await_args.kwargs["raise_on_null_status"] is True


# ---------------------------------------------------------------------------
# Source guard: the write RPCs above never regain a status-swallowing call
# ---------------------------------------------------------------------------

_WEB_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_web"


def _rpc_call_sites(path: Path) -> list[tuple[int, str, dict[str, ast.expr]]]:
    """``(lineno, RPCMethod member name, keyword -> value)`` for every ``rpc_call(...)``."""
    sites: list[tuple[int, str, dict[str, ast.expr]]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "rpc_call" or not node.args:
            continue
        method = node.args[0]
        if not (
            isinstance(method, ast.Attribute)
            and isinstance(method.value, ast.Name)
            and method.value.id == "RPCMethod"
        ):
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        sites.append((node.lineno, method.attr, keywords))
    return sites


def _is_true(expr: ast.expr | None) -> bool:
    return isinstance(expr, ast.Constant) and expr.value is True


@pytest.mark.repo_lint
def test_write_rpcs_do_not_swallow_a_status() -> None:
    """Every ``allow_null=True`` call on a :data:`STRICT_WRITE_RPCS` member is strict.

    Deletes (idempotent by contract — an absent target may legitimately come
    back as a tagged null), ``SHARE_NOTEBOOK`` / ``SHARE_ARTIFACT`` /
    ``REMOVE_RECENTLY_VIEWED`` (recorded returning a tagged null on flows the
    client reports as successful, see ``_RPCS_OBSERVED_SWALLOWING_A_STATUS``)
    and the derived reads are deliberately NOT in the set.
    """
    strict_names = {method.name for method in STRICT_WRITE_RPCS}
    offenders: list[str] = []
    scanned = 0
    for path in sorted(_WEB_ROOT.rglob("*.py")):
        for lineno, name, keywords in _rpc_call_sites(path):
            scanned += 1
            if name not in strict_names or not _is_true(keywords.get("allow_null")):
                continue
            if not _is_true(keywords.get("raise_on_null_status")):
                offenders.append(f"{path.relative_to(_WEB_ROOT.parents[2])}:{lineno} {name}")

    # Sanity-check the scan itself, not the number of ``allow_null=True`` sites:
    # a refactor that drops ``allow_null`` from every write RPC satisfies the
    # invariant vacuously and must not fail here. What would make the guard
    # blind is the ``rpc_call(RPCMethod.X, ...)`` call shape changing so that
    # ``_rpc_call_sites`` stops recognising any call at all.
    assert scanned, (
        "guard recognised no rpc_call(RPCMethod.X, ...) sites — did the call shape change?"
    )
    assert not offenders, (
        "allow_null=True on a write RPC without raise_on_null_status=True lets a "
        "server rejection decode to the success value (#2290):\n  " + "\n  ".join(offenders)
    )
