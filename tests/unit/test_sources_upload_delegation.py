"""Regression guard for the source-upload single-source-of-truth invariant.

Issue #1326 consolidated all resumable-upload / streaming / file-registration
logic into :class:`notebooklm._source.upload.SourceUploadPipeline`. The public
``SourcesAPI`` surface keeps only *thin delegators* that forward verbatim to the
pipeline; it must never re-grow a parallel implementation of the Scotty upload
protocol.

These tests pin three things:

1. ``SourcesAPI._uploader`` is built from ``_source/upload.py`` — the upload
   implementation collaborator.
2. Every ``SourcesAPI`` upload entry point (``add_file`` and the private
   ``_register_file_source`` / ``_start_resumable_upload`` /
   ``_upload_file_streaming`` / ``_cancel_upload_session`` helpers) forwards its
   arguments unchanged to the matching ``SourceUploadPipeline`` method — verified
   down to the exact positional/keyword shape against the *real* helper
   signatures.
3. ``_sources.py`` carries no resumable-upload HTTP/Scotty implementation of its
   own — neither implementation tokens (token guard) nor anything beyond a
   single delegating ``await self._uploader.<method>(...)`` statement per helper
   (structural guard).
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import notebooklm._sources as sources_module
from notebooklm._source.upload import SourceUploadPipeline
from notebooklm._sources import SourcesAPI
from tests._fixtures.fake_core import make_fake_core


def _parse_sources_module() -> ast.Module:
    """Parse ``_sources.py`` from its on-disk source.

    Reads the file directly via ``Path.read_text(encoding="utf-8")`` rather than
    ``inspect.getsource`` so the structural guards stay robust under packaging /
    frozen environments and on platforms with a non-UTF-8 default encoding.
    """
    module_file = sources_module.__file__
    assert module_file is not None, "notebooklm._sources has no __file__ to parse"
    source_path = Path(module_file)
    if source_path.suffix.casefold() == ".pyc":
        # Some packaging/import setups expose a compiled ``.pyc`` path; map it
        # back to the readable ``.py`` source so the AST guards never try to
        # parse bytecode. ``casefold()`` makes the suffix check robust on
        # case-insensitive filesystems.
        source_path = source_path.with_suffix(".py")
    return ast.parse(source_path.read_text(encoding="utf-8"))


def _sources_api_class(tree: ast.Module) -> ast.ClassDef:
    """Return the ``SourcesAPI`` class node from a parsed ``_sources`` module."""
    return next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SourcesAPI"
    )


class _RecordingPipeline:
    """Records each upload call's exact args/kwargs without performing I/O.

    Stands in for the real :class:`SourceUploadPipeline` so the delegation tests
    can assert the precise call shape each ``SourcesAPI`` helper forwards.
    """

    def __init__(self) -> None:
        self.calls: dict[str, tuple[tuple[object, ...], dict[str, object]]] = {}

    def _record(self, name: str):
        async def _call(*args: object, **kwargs: object) -> str:
            self.calls[name] = (args, kwargs)
            return f"<{name}-result>"

        return _call

    def __getattr__(self, name: str):
        # Only fabricate recorders for the real upload entry points. Dunder
        # lookups (``__wrapped__``, ``__members__``, copy/pickle probes, etc.)
        # must raise ``AttributeError`` so test-inspection tooling sees a normal
        # object rather than a stray coroutine factory.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return self._record(name)


def _make_sources_api() -> SourcesAPI:
    """Build a real ``SourcesAPI`` with a real ``SourceUploadPipeline``.

    Mirrors the ``sources_api`` fixture in ``test_sources_upload.py``: the
    pipeline is constructed from the same mocked core so it structurally
    satisfies the pipeline's narrow collaborator slots without touching the
    network.
    """
    core = make_fake_core(rpc_call=AsyncMock())
    # ``make_fake_core`` already supplies an ``auth`` namespace with the same
    # ``authuser``/``account_email`` values; a ``MagicMock`` is used here so any
    # future auth attribute the pipeline reaches for resolves to a stub rather
    # than raising ``AttributeError`` during construction.
    core.auth = MagicMock()
    core.auth.authuser = 0
    core.auth.account_email = None
    # Upload I/O is intercepted by ``_RecordingPipeline`` in the delegation
    # tests, so ``SourceUploadPipeline._live_cookies()`` is never reached; the
    # self-referential kernel only needs to construct cleanly.
    core.kernel = core
    core.record_upload_queue_wait = MagicMock()
    uploader = SourceUploadPipeline(
        rpc=core,
        drain=core,
        lifecycle=core,
        kernel=core.kernel,
        auth=core.auth,
        record_upload_queue_wait=core.record_upload_queue_wait,
    )
    return SourcesAPI(core, uploader=uploader)


def _make_api_with_recording_pipeline() -> tuple[SourcesAPI, _RecordingPipeline]:
    """Return a ``SourcesAPI`` whose uploader is swapped for a recording double."""
    api = _make_sources_api()
    pipeline = _RecordingPipeline()
    api._uploader = pipeline  # type: ignore[assignment]
    return api, pipeline


@pytest.mark.asyncio
async def test_uploader_is_the_pipeline() -> None:
    """SourcesAPI builds its upload collaborator from _source/upload.py."""
    api = _make_sources_api()
    assert isinstance(api._uploader, SourceUploadPipeline)


@pytest.mark.asyncio
async def test_add_file_delegates_to_pipeline() -> None:
    """SourcesAPI.add_file forwards its args verbatim to the pipeline."""
    api, pipeline = _make_api_with_recording_pipeline()

    def progress(_done: int, _total: int) -> None:
        return None

    result = await api.add_file(
        "nb-1",
        "/tmp/report.pdf",
        "application/pdf",
        wait=True,
        wait_timeout=42.0,
        title="Report",
        on_progress=progress,
    )

    assert result == "<add_file-result>"
    # SourcesAPI.add_file forwards notebook_id / file_path positionally and
    # mime_type / wait / wait_timeout / title / on_progress as keywords.
    args, kwargs = pipeline.calls["add_file"]
    assert args == ("nb-1", "/tmp/report.pdf")
    assert kwargs == {
        "mime_type": "application/pdf",
        "wait": True,
        "wait_timeout": 42.0,
        "title": "Report",
        "on_progress": progress,
    }
    # The public surface intentionally does NOT expose the pipeline's
    # ``upload_index`` knob; guard against it being forwarded by accident.
    assert "upload_index" not in kwargs


@pytest.mark.asyncio
async def test_register_file_source_delegates_to_pipeline() -> None:
    """_register_file_source forwards (notebook_id, filename) to the pipeline."""
    api, pipeline = _make_api_with_recording_pipeline()

    # The real helper is a 2-arg delegator: (notebook_id, filename).
    result = await api._register_file_source("nb-2", "doc.txt")

    assert result == "<register_file_source-result>"
    args, kwargs = pipeline.calls["register_file_source"]
    assert args == ("nb-2", "doc.txt")
    assert kwargs == {}


@pytest.mark.asyncio
async def test_start_resumable_upload_delegates_to_pipeline() -> None:
    """_start_resumable_upload forwards its positional args to the pipeline."""
    api, pipeline = _make_api_with_recording_pipeline()

    result = await api._start_resumable_upload(
        "nb-3",
        "movie.mp4",
        123456,
        "src-abc",
        "video/mp4",
    )

    assert result == "<start_resumable_upload-result>"
    args, kwargs = pipeline.calls["start_resumable_upload"]
    assert args == ("nb-3", "movie.mp4", 123456, "src-abc", "video/mp4")
    assert kwargs == {}


@pytest.mark.asyncio
async def test_upload_file_streaming_delegates_to_pipeline() -> None:
    """_upload_file_streaming forwards its args (plus a logger) to the pipeline."""
    api, pipeline = _make_api_with_recording_pipeline()

    def progress(_done: int, _total: int) -> None:
        return None

    file_obj = Path("/tmp/movie.mp4")
    result = await api._upload_file_streaming(
        "https://upload.example/resumable",
        file_obj,
        filename="movie.mp4",
        on_progress=progress,
        total_bytes=123456,
    )

    assert result == "<upload_file_streaming-result>"
    # upload_url / file_obj positional; filename / on_progress / total_bytes as
    # keywords, plus the module logger the helper injects.
    args, kwargs = pipeline.calls["upload_file_streaming"]
    assert args == ("https://upload.example/resumable", file_obj)
    assert kwargs["filename"] == "movie.mp4"
    assert kwargs["on_progress"] is progress
    assert kwargs["total_bytes"] == 123456
    assert "logger" in kwargs


@pytest.mark.asyncio
async def test_cancel_upload_session_delegates_to_pipeline() -> None:
    """_cancel_upload_session forwards its args (plus a logger) and returns None."""
    api, pipeline = _make_api_with_recording_pipeline()

    # ``_cancel_upload_session`` awaits the pipeline without returning its
    # value (its public contract is ``-> None``), so assert on the recorded
    # call rather than the return value.
    result = await api._cancel_upload_session(
        "https://upload.example/resumable",
        "https://notebooklm.example",
        "0",
    )

    assert result is None
    args, kwargs = pipeline.calls["cancel_upload_session"]
    assert args == (
        "https://upload.example/resumable",
        "https://notebooklm.example",
        "0",
    )
    assert "logger" in kwargs


def _strip_docstrings(node: ast.AST) -> None:
    """Blank out every docstring in the tree in place.

    The upload helpers legitimately *describe* the Scotty cancellation contract
    (``asyncio.shield`` / ``asyncio.create_task``) in their docstrings; only
    actual implementation tokens in executable code should fail the token guard.
    """
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(child, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body[0].value.value = ""


def test_sources_module_holds_no_scotty_implementation() -> None:
    """_sources.py must not re-grow a parallel resumable-upload implementation.

    The Scotty upload protocol (resumable start request, x-goog-upload-* headers,
    streaming finalize, shielded background finalize) lives only in
    ``_source/upload.py``. ``_sources.py`` should reference none of those
    implementation tokens in executable code — it only delegates. Docstrings are
    excluded because the delegators legitimately *document* the contract they
    forward to.
    """
    tree = _parse_sources_module()
    _strip_docstrings(tree)
    code = ast.unparse(tree)
    forbidden = (
        "x-goog-upload-command",
        "x-goog-upload-offset",
        "x-goog-upload-url",
        "build_resumable_upload_start_request",
        "_validate_resumable_upload_url",
        "asyncio.shield",
        "asyncio.create_task",
    )
    leaked = [token for token in forbidden if token in code]
    assert not leaked, (
        "_sources.py leaked resumable-upload implementation tokens in executable "
        f"code (should delegate to notebooklm._source.upload): {leaked}"
    )


def test_sources_upload_helpers_are_pure_delegators() -> None:
    """Each SourcesAPI upload helper body must be delegation-only.

    Identifier-independent structural guard (complements the token check above):
    the body of every upload helper must be exactly one awaited
    ``self._uploader.<method>(...)`` statement — so re-introducing a parallel
    implementation in ``_sources.py`` (even with renamed identifiers or a
    differently-cased header dict) is caught here.
    """
    # Strip docstrings first so a docstring that merely *mentions*
    # ``self._uploader`` (e.g. to explain a method does NOT use it) cannot
    # perturb either the completeness scan below or the per-helper body checks.
    tree = _parse_sources_module()
    _strip_docstrings(tree)
    class_def = _sources_api_class(tree)
    # Collect both sync and async methods: today every upload helper is async,
    # but a future sync helper that delegated to ``self._uploader`` must not slip
    # past either guard below.
    func_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    methods = {node.name: node for node in class_def.body if isinstance(node, func_types)}

    expected = {
        "_register_file_source": "register_file_source",
        "_start_resumable_upload": "start_resumable_upload",
        "_upload_file_streaming": "upload_file_streaming",
        "_cancel_upload_session": "cancel_upload_session",
        "add_file": "add_file",
    }

    # Completeness guard: ``expected`` must be an exhaustive allowlist of the
    # methods that *delegate to* ``self._uploader`` — otherwise a newly added
    # uploader-delegating helper would be silently skipped by the loop below.
    # ``__init__`` is excluded: it only *wires* the uploader (configuring the
    # shared lister/poller and source-limit lookup), it does not delegate an
    # upload operation to it.
    uploader_methods = {
        node.name
        for node in class_def.body
        if isinstance(node, func_types)
        and node.name != "__init__"
        and "self._uploader" in ast.unparse(node)
    }
    assert uploader_methods == set(expected), (
        "SourcesAPI uploader-delegating methods drifted from the expected "
        f"allowlist: code has {sorted(uploader_methods)}, "
        f"expected covers {sorted(expected)}"
    )

    for wrapper, pipeline_method in expected.items():
        # The structural double accepts any attribute name, so verify the
        # delegatee actually exists on the *real* pipeline. This catches a
        # pipeline-side rename (e.g. ``register_file_source`` -> something else)
        # that would otherwise pass every delegation test yet break at runtime.
        assert hasattr(SourceUploadPipeline, pipeline_method), (
            f"SourceUploadPipeline has no method {pipeline_method!r}; "
            f"{wrapper} would delegate to a non-existent target"
        )
        node = methods[wrapper]
        # Skip the docstring statement, if any.
        stmts = [
            stmt
            for stmt in node.body
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        ]
        assert len(stmts) == 1, (
            f"{wrapper} must be a single-statement delegator, found {len(stmts)} statements"
        )
        ret = stmts[0]
        # ``_cancel_upload_session`` returns None (bare ``await`` Expr); the
        # others ``return await``. Accept either await-only delegation shape.
        assert isinstance(ret, (ast.Return, ast.Expr)), f"{wrapper} must await its delegation"
        call_holder = ret.value
        assert isinstance(call_holder, ast.Await), f"{wrapper} must await the pipeline"
        inner = call_holder.value
        assert isinstance(inner, ast.Call), f"{wrapper} must call the pipeline"
        func = inner.func
        assert isinstance(func, ast.Attribute), f"{wrapper} must call an attribute"
        assert func.attr == pipeline_method, (
            f"{wrapper} must delegate to pipeline.{pipeline_method}, got {func.attr}"
        )
        # Receiver must be ``self._uploader``.
        receiver = func.value
        assert (
            isinstance(receiver, ast.Attribute)
            and receiver.attr == "_uploader"
            and isinstance(receiver.value, ast.Name)
            and receiver.value.id == "self"
        ), f"{wrapper} must delegate to self._uploader, not a local implementation"
