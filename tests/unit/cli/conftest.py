"""Shared fixtures for CLI unit tests."""

import math
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.types import (
    MindMapResult,
    ResearchSource,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
    SourceGuide,
)


def source_guide(spec: dict | None = None, **overrides: Any) -> SourceGuide:
    """Build a typed ``SourceGuide`` from a legacy guide dict spec.

    ``sources.get_guide`` now returns a typed ``SourceGuide`` (issue #1209);
    CLI tests historically declared canned guides as the old dict shape.
    """
    data: dict[str, Any] = dict(spec or {})
    data.update(overrides)
    return SourceGuide(
        summary=data.get("summary", ""),
        keywords=data.get("keywords", []),
    )


def research_task(spec: dict | None = None, **overrides: Any) -> ResearchTask:
    """Build a typed ``ResearchTask`` from a legacy poll/wait dict spec.

    ``research.poll`` / ``research.wait_for_completion`` now return a typed
    ``ResearchTask`` (issue #1209). CLI tests historically declared canned
    results as the old dict shape; this helper adapts them. Unknown status
    strings map to ``FAILED`` (mirroring the parser); ``sources`` entries are
    coerced into ``ResearchSource``.
    """
    data: dict[str, Any] = dict(spec or {})
    data.update(overrides)
    raw_status = data.get("status", "no_research")
    try:
        status = ResearchStatus(raw_status)
    except ValueError:
        status = ResearchStatus.FAILED
    raw_sources = data.get("sources") or []
    sources = tuple(
        ResearchSource.from_public_dict(s)
        for s in (raw_sources if isinstance(raw_sources, list) else [])
        if isinstance(s, dict)
    )
    raw_tasks = data.get("tasks") or []
    tasks = tuple(
        research_task(t)
        for t in (raw_tasks if isinstance(raw_tasks, list) else [])
        if isinstance(t, dict)
    )
    query = data.get("query", "")
    report = data.get("report", "")
    return ResearchTask(
        task_id=data.get("task_id", ""),
        status=status,
        query=query if isinstance(query, str) else "",
        sources=sources,
        summary=data.get("summary", "") if isinstance(data.get("summary"), str) else "",
        report=report if isinstance(report, str) else "",
        tasks=tasks,
    )


def research_start(spec: dict | None = None, **overrides: Any) -> ResearchStart:
    """Build a typed ``ResearchStart`` from a legacy start dict spec."""
    data: dict[str, Any] = dict(spec or {})
    data.update(overrides)
    return ResearchStart(
        task_id=data.get("task_id", ""),
        report_id=data.get("report_id"),
        notebook_id=data.get("notebook_id", ""),
        query=data.get("query", ""),
        mode=data.get("mode", "fast"),
    )


def mind_map_result(spec: dict | None = None, **overrides: Any) -> MindMapResult:
    """Build a typed ``MindMapResult`` from a legacy mind-map dict spec."""
    data: dict[str, Any] = dict(spec or {})
    data.update(overrides)
    return MindMapResult(mind_map=data.get("mind_map"), note_id=data.get("note_id"))


@pytest.fixture(autouse=True)
def _disable_chromium_profile_fanout():
    """Default: Chromium multi-user-profile discovery returns nothing in tests.

    The session multi-account paths (``auth inspect``, ``login --account``,
    ``login --all-accounts``, ``auth refresh``) auto-fan-out across every
    populated Chromium user-data profile (issue #571). On a developer machine
    with multiple real Chrome profiles, that discovery would otherwise leak
    into tests that mock ``rookiepy.chrome`` (and ignore the new
    ``rookiepy.any_browser`` fan-out path), making them flaky depending on
    whoever runs the suite.

    Tests that exercise the fan-out path itself override this fixture by
    patching ``discover_chromium_profiles`` explicitly with their own list of
    synthetic profiles — the autouse here just guarantees deterministic
    legacy-path behavior everywhere else.

    D1 PR-3 migration: previously used a ``monkeypatch`` string-target
    setattr aimed at the ``cli._chromium_profiles`` discovery helper — the
    string-target form ADR-007 forbids because it silently no-ops if the
    target relocates. Now uses ``patch(...)`` which raises
    ``AttributeError`` on missing targets.
    """
    with patch(
        "notebooklm.cli._chromium_profiles.discover_chromium_profiles",
        lambda *a, **kw: [],
    ):
        yield


@pytest.fixture
def runner():
    """Create a Click test runner."""
    return CliRunner()


@pytest.fixture
def mock_auth():
    """Mock authentication for CLI commands.

    After CLI refactoring, auth is loaded via cli.helpers module.
    We patch both the main CLI and the helpers module for full coverage.
    """
    with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock:
        mock.return_value = {
            "SID": "test",
            # ``__Secure-1PSIDTS`` is required by ``MINIMUM_REQUIRED_COOKIES``
            # in ``_auth.cookie_policy``. Tests that route through
            # ``_validate_required_cookies`` (e.g. anything calling
            # ``fetch_tokens_with_domains``) need both cookies present —
            # without this, the validator raises ``ValueError`` before the
            # CLI command body runs.
            "__Secure-1PSIDTS": "test_1psidts",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield mock


@pytest.fixture
def mock_fetch_tokens():
    """Mock fetch_tokens_with_domains for CLI commands.

    After cookie-jar refactoring, the CLI path uses fetch_tokens_with_domains
    from auth module (via helpers.get_client). We also mock build_cookie_jar
    to avoid reading storage files.
    """
    import httpx

    mock_jar = httpx.Cookies()
    mock_jar.set("SID", "test", domain=".google.com")

    with (
        patch("notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock) as mock,
        patch("notebooklm.cli.helpers.build_cookie_jar", return_value=mock_jar),
    ):
        mock.return_value = ("csrf_token", "session_id")
        yield mock


class MockNotebook:
    """Mock notebook object for partial ID resolution tests."""

    def __init__(self, id: str, title: str = "Mock Notebook"):
        self.id = id
        self.title = title


class MockSource:
    """Mock source object for partial ID resolution tests."""

    def __init__(self, id: str, title: str = "Mock Source"):
        self.id = id
        self.title = title


class MockArtifact:
    """Mock artifact object for partial ID resolution tests."""

    def __init__(self, id: str, title: str = "Mock Artifact"):
        self.id = id
        self.title = title


class MockNote:
    """Mock note object for partial ID resolution tests."""

    def __init__(self, id: str, title: str = "Mock Note"):
        self.id = id
        self.title = title


def create_mock_client():
    """Helper to create a properly configured mock client.

    Returns a MagicMock configured as an async context manager
    that can be used with `async with NotebookLMClient(...) as client:`.

    IMPORTANT: The mock has pre-created namespace objects (artifacts, sources,
    notebooks, chat, research, notes) to match NotebookLMClient's structure.
    Always use client.artifacts.method(), not client.method() directly.

    The mock includes default implementations for list methods that support
    partial ID resolution. Common test IDs (nb_*, src_*, art_*, note_*) will
    be matched by the mock notebooks/sources/artifacts/notes list.

    Example:
        mock_client = create_mock_client()
        mock_client.artifacts.list = AsyncMock(return_value=[...])
        mock_client.artifacts.download_audio = async_download_fn
    """
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    # Pre-create namespace mocks to match NotebookLMClient structure
    # This ensures consistent attribute access (mock_client.artifacts is always
    # the same object) and reminds developers to use the correct namespace
    mock_client.notebooks = MagicMock()
    mock_client.sources = MagicMock()
    mock_client.artifacts = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.research = MagicMock()
    mock_client.notes = MagicMock()
    mock_client.sharing = MagicMock()
    mock_client.mind_maps = MagicMock()
    # Default: no mind maps, so non-mind-map flows (e.g. ``artifact rename`` of a
    # regular artifact) fall through without an unmocked-await on ``mind_maps.list``.
    mock_client.mind_maps.list = AsyncMock(return_value=[])

    mock_client.research.poll = AsyncMock(return_value=research_task({"status": "no_research"}))

    async def wait_for_research_completion(
        notebook_id,
        task_id=None,
        *,
        timeout=1800,
        interval=5,
        initial_interval=None,
    ):
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        # Mirror the real API: ``initial_interval`` is the canonical keyword and
        # wins when supplied; ``interval`` is the deprecated alias kept working.
        effective_interval = initial_interval if initial_interval is not None else interval
        if effective_interval <= 0:
            raise ValueError("poll interval must be positive")
        pinned_task_id = task_id
        attempts = max(1, math.ceil(timeout / effective_interval) + 1)
        status: ResearchTask = research_task({"status": "no_research"})
        for _ in range(attempts):
            status = await mock_client.research.poll(notebook_id, task_id=pinned_task_id)
            if pinned_task_id is None and status.task_id:
                pinned_task_id = status.task_id
            status_val = status.status
            if status_val in ("completed", "failed"):
                return status
            if status_val == "no_research" and pinned_task_id is None:
                return status
        raise TimeoutError(f"Research task {pinned_task_id or 'unknown'} timed out")

    mock_client.research.wait_for_completion = AsyncMock(side_effect=wait_for_research_completion)

    # Default mocks for partial ID resolution
    # These return mock objects that match common test ID patterns (nb_*, src_*, etc.)
    # The pattern ensures that any ID starting with "nb_" will match a notebook,
    # any ID starting with "src_" will match a source, etc.
    def make_notebook_list():
        """Return notebook list that matches common test IDs."""
        return [
            MockNotebook("nb_123", "Test Notebook"),
            MockNotebook("nb_456", "Another Notebook"),
            MockNotebook("notebook_test", "Notebook Test"),
        ]

    def make_source_list(notebook_id, *, strict=False):
        """Return source list that matches common test IDs."""
        del strict
        return [
            MockSource("src_1", "Source One"),
            MockSource("src_2", "Source Two"),
            MockSource("src_001", "Source 001"),
            MockSource("src_002", "Source 002"),
            MockSource("src_new", "New Source"),
            MockSource("source_test", "Source Test"),
        ]

    def make_artifact_list(notebook_id):
        """Return artifact list that matches common test IDs."""
        return [
            MockArtifact("art_1", "Artifact One"),
            MockArtifact("art_2", "Artifact Two"),
            MockArtifact("artifact_test", "Artifact Test"),
        ]

    def make_note_list(notebook_id):
        """Return note list that matches common test IDs."""
        return [
            MockNote("note_1", "Note One"),
            MockNote("note_2", "Note Two"),
            MockNote("note_test", "Note Test"),
        ]

    mock_client.notebooks.list = AsyncMock(side_effect=make_notebook_list)
    mock_client.sources.list = AsyncMock(side_effect=make_source_list)
    mock_client.artifacts.list = AsyncMock(side_effect=make_artifact_list)
    mock_client.notes.list = AsyncMock(side_effect=make_note_list)

    return mock_client


class MultiMockProxy:
    """Proxy that forwards attribute access to all underlying mocks.

    When you set return_value on this proxy, it propagates to all mocks.
    Other attribute access is delegated to the primary mock.
    """

    def __init__(self, mocks):
        object.__setattr__(self, "_mocks", mocks)
        object.__setattr__(self, "_primary", mocks[0])

    def __getattr__(self, name):
        return getattr(self._primary, name)

    def __setattr__(self, name, value):
        if name == "return_value":
            # Propagate return_value to all mocks
            for m in self._mocks:
                m.return_value = value
        else:
            setattr(self._primary, name, value)


class MultiPatcher:
    """Context manager that patches ``NotebookLMClient`` in multiple CLI modules.

    Top-level commands are spread across ``notebook_cmd`` / ``chat_cmd`` /
    ``session_cmd`` / ``share_cmd`` so a single ``patch()`` cannot cover them.
    Since P3.T0 broke the click-group shadow on the package attributes
    (modules are now ``*_cmd``), direct string-form ``patch(...)`` works on
    each module name without needing ``importlib`` indirection.
    """

    def __init__(self):
        self.patches = [
            patch("notebooklm.cli.notebook_cmd.NotebookLMClient"),
            patch("notebooklm.cli.chat_cmd.NotebookLMClient"),
            patch("notebooklm.cli.session_cmd.NotebookLMClient"),
            patch("notebooklm.cli.share_cmd.NotebookLMClient"),
        ]
        self.mocks = []

    def __enter__(self):
        # Start all patches and collect mocks
        self.mocks = [p.__enter__() for p in self.patches]
        # Return a proxy that propagates return_value to all mocks
        return MultiMockProxy(self.mocks)

    def __exit__(self, *args):
        for p in reversed(self.patches):
            p.__exit__(*args)


def patch_main_cli_client():
    """Create a context manager that patches NotebookLMClient in CLI command modules.

    After refactoring, top-level commands are in separate modules:
    - notebook.py: list, create, delete, rename, summary
    - chat.py: ask, configure, history
    - session.py: use
    - share.py: status, public, view-level, add, update, remove

    Returns:
        A context manager that patches NotebookLMClient in all relevant modules

    Example:
        with patch_main_cli_client() as mock_cls:
            mock_client = create_mock_client()
            mock_cls.return_value = mock_client
            # ... run test
    """
    return MultiPatcher()


@pytest.fixture
def mock_context_file(tmp_path):
    """Provide a temporary context file for testing context commands.

    Patches every current context-path seam:

    * ``cli.helpers`` — passes the resolver explicitly.
    * ``cli.context`` + ``cli.resolve`` — call-time lookups after the helper split.
    * ``cli.services.session_context`` — the service-layer call site that reads
      the context file in ``read_status``. This is the real consumer binding;
      ``read_status`` resolves ``get_context_path`` here (the precedence chain's
      level-1 service-module attribute), so without this patch the service-layer
      ``read_status`` would fall through to the real ``~/.notebooklm/context.json``
      even when every other binding is patched (rev-1 CodeRabbit feedback on #962).
      Migrated off the legacy ``cli.session_cmd.get_context_path`` patch surface
      (#1367): that re-export was a pure bridge fully shadowed by this binding and
      never invoked, so patching it was a no-op.
    """
    context_file = tmp_path / "context.json"
    with (
        patch("notebooklm.cli.helpers.get_context_path", return_value=context_file),
        patch("notebooklm.cli.context.get_context_path", return_value=context_file),
        patch("notebooklm.cli.resolve.get_context_path", return_value=context_file),
        patch(
            "notebooklm.cli.services.session_context.get_context_path",
            return_value=context_file,
        ),
    ):
        yield context_file
