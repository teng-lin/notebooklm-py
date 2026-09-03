"""Coverage-focused unit tests for ``cli/services/playwright_login.py``.
These tests target branches not exercised by the existing
``test_login.py`` / ``test_playwright_login_stderr.py`` suites:
* :func:`_select_playwright_account` ambiguity-reason branches.
* :func:`repair_playwright_account_metadata` clear-metadata-failure path.
* :func:`windows_playwright_event_loop` win32 policy swap.
* :func:`ensure_chromium_installed` detection contract (present / missing /
  unreadable probe answer) plus its timeout + generic-exception pre-flight
  failures.
* :func:`recover_page` TargetClosed + non-TargetClosed PlaywrightError paths.
* :func:`validate_login_flag_conflicts` remaining mutual-exclusion gates.
* :func:`prepare_login_paths` explicit-storage and profile branches.
* :func:`run_playwright_login` ``_capture_page_html`` PlaywrightError path
  and cookie-forcing inner-recovery re-raise.
Each test drives the helper directly (or via the small public surface)
with stub/mocked collaborators so no real browser / network is required.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import notebooklm._app.login_browser as login_browser
import notebooklm._browser.browser_capture as browser_capture
from notebooklm._app.login_browser import (
    BrowserLoginPlan,
    LoginFlagConflict,
    LoginPathError,
    PreparedLoginPaths,
    prepare_login_paths,
    validate_login_flag_conflicts,
)
from notebooklm._auth.account import _select_playwright_account
from notebooklm._browser.browser_capture import recover_page, windows_playwright_event_loop
from notebooklm._env import get_base_host
from notebooklm.cli.playwright_login_io import make_login_io
from notebooklm.cli.services import playwright_login
from notebooklm.cli.services.playwright_login import (
    CHROMIUM_MISSING_MARKER,
    CHROMIUM_PRESENT_MARKER,
    CHROMIUM_PROBE_SOURCE,
    ensure_chromium_installed,
    login_flag_conflict_message,
    login_path_error_message,
    repair_playwright_account_metadata,
)


class _FakeLoginIO:
    """Shared fake ``LoginIO`` for direct-call tests.
    ``fail`` raises ``SystemExit`` so ``pytest.raises(SystemExit)`` fires on the
    service's terminal paths (a bare ``MagicMock`` ``fail`` would return a Mock
    and break the assertion). ``emit`` records its calls for the few tests that
    inspect the rendered help text; ``run_async`` drives the awaitable.
    """

    def __init__(self) -> None:
        self.emitted: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def emit(self, *args: Any, **kwargs: Any) -> None:
        self.emitted.append((args, kwargs))

    def fail(self, code: int) -> Any:
        raise SystemExit(code)

    def run_async(self, coro: Any) -> Any:
        import asyncio

        return asyncio.run(coro)


def _required_capture_state() -> dict[str, Any]:
    """Return the minimal authenticated state accepted by real capture."""
    return {
        "cookies": [
            {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "psidts",
                "domain": ".google.com",
                "path": "/",
            },
        ],
        "origins": [{"origin": "https://notebooklm.google.com", "localStorage": []}],
    }


# ---------------------------------------------------------------------------
# _select_playwright_account
# ---------------------------------------------------------------------------
def _account(email: str, authuser: int = 0) -> Any:
    return SimpleNamespace(email=email, authuser=authuser)


def test_select_account_active_email_multiple_matches_is_ambiguous() -> None:
    """Two discovered accounts with the same email cannot be disambiguated."""
    accounts = [_account("dup@example.com", 0), _account("dup@example.com", 1)]
    selected, reason = _select_playwright_account(accounts, active_email="dup@example.com")
    assert selected is None
    assert reason is not None
    assert "multiple discovered accounts matched dup@example.com" in reason


def test_select_account_active_email_no_match() -> None:
    """The active page email was not among the discovered accounts."""
    accounts = [_account("other@example.com", 0)]
    selected, reason = _select_playwright_account(accounts, active_email="missing@example.com")
    assert selected is None
    assert reason is not None
    assert "missing@example.com was not discovered" in reason


def test_select_account_single_match() -> None:
    """Exactly one matching account selects cleanly."""
    target = _account("alice@example.com", 0)
    selected, reason = _select_playwright_account([target], active_email="alice@example.com")
    assert selected is target
    assert reason is None


def test_select_account_no_active_email_multiple_accounts_is_ambiguous() -> None:
    """Multiple accounts with no page email cannot be picked silently."""
    accounts = [_account("a@example.com", 0), _account("b@example.com", 1)]
    selected, reason = _select_playwright_account(accounts, active_email=None)
    assert selected is None
    assert reason is not None
    assert "multiple Google accounts were discovered" in reason


def test_select_account_no_active_email_no_accounts() -> None:
    """Empty discovery list returns the no-accounts reason ."""
    selected, reason = _select_playwright_account([], active_email=None)
    assert selected is None
    assert reason == "no Google accounts were discovered"


# ---------------------------------------------------------------------------
# repair_playwright_account_metadata — clear-metadata-failure path (459-460)
# ---------------------------------------------------------------------------
def test_repair_metadata_clear_failure_is_logged(tmp_path, caplog) -> None:
    """When enumeration raises AND clear_account_metadata raises, the clear
    failure is logged  and the function returns False."""
    import logging

    storage_path = tmp_path / "storage.json"
    storage_path.write_bytes(b"\xff")

    with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
        result = repair_playwright_account_metadata(
            storage_path, _FakeLoginIO(), page_html=None, quiet=True
        )
    assert result is False
    assert any(
        "Failed to clear stale account metadata" in rec.getMessage() for rec in caplog.records
    )


def test_repair_metadata_degrades_on_run_async_runtime_error(tmp_path) -> None:
    """A ``RuntimeError`` from ``io.run_async`` itself (e.g. the nested-event-loop
    guard) must degrade to the same best-effort warning as an error from inside
    the coroutine, not abort the caller (review finding on PR #2139: the
    consolidation moved the try/except inside the coroutine, which does not
    cover a ``run_async`` scheduling failure happening outside it)."""

    class _RaisingRunAsyncIO(_FakeLoginIO):
        def run_async(self, coro: Any) -> Any:
            coro.close()
            raise RuntimeError("cannot run_async from a running event loop")

    storage_path = tmp_path / "storage.json"
    io = _RaisingRunAsyncIO()

    result = repair_playwright_account_metadata(storage_path, io, page_html=None, quiet=False)

    assert result is False
    assert any(
        "account metadata was not written" in str(args) and "Details:" in str(args)
        for args, _ in io.emitted
    )


# ---------------------------------------------------------------------------
# windows_playwright_event_loop — win32 policy swap (500-505)
# ---------------------------------------------------------------------------
def test_windows_event_loop_swaps_and_restores_policy(monkeypatch) -> None:
    """On win32 the context manager swaps in the default policy and restores."""
    import asyncio

    sentinel_original = object()
    swapped_policies: list[Any] = []

    class _DefaultPolicy:
        pass

    # Patch the asyncio seams *before* faking ``sys.platform`` to win32. On
    # Python 3.14 ``asyncio.DefaultEventLoopPolicy`` is resolved lazily via the
    # module ``__getattr__``, and under a faked win32 platform that lookup
    # reaches ``windows_events`` — which is never imported on a Linux/macOS
    # host, raising ``NameError`` during monkeypatch's old-value capture. Doing
    # the captures while the real platform is still in effect avoids that; once
    # the names are replaced, ``__getattr__`` is no longer consulted.
    monkeypatch.setattr(asyncio, "get_event_loop_policy", lambda: sentinel_original)
    monkeypatch.setattr(
        asyncio, "set_event_loop_policy", lambda policy: swapped_policies.append(policy)
    )
    monkeypatch.setattr(asyncio, "DefaultEventLoopPolicy", _DefaultPolicy)
    monkeypatch.setattr(browser_capture.sys, "platform", "win32")
    with windows_playwright_event_loop():
        # First swap installs a fresh DefaultEventLoopPolicy.
        assert isinstance(swapped_policies[-1], _DefaultPolicy)
    # On exit the original policy is restored.
    assert swapped_policies[-1] is sentinel_original


def test_windows_event_loop_noop_off_win32(monkeypatch) -> None:
    """Off win32 the context manager is a pure no-op."""
    monkeypatch.setattr(browser_capture.sys, "platform", "linux")
    with windows_playwright_event_loop():
        pass  # no exception, nothing swapped


# ---------------------------------------------------------------------------
# ensure_chromium_installed — detection contract (#2031)
# ---------------------------------------------------------------------------
def _record_subprocess(
    monkeypatch, probe_stdout: str, probe_returncode: int = 0
) -> list[list[str]]:
    """Stub ``subprocess.run``: probe returns ``probe_stdout``, install succeeds."""
    calls: list[list[str]] = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        if "install" in cmd:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        return SimpleNamespace(stdout=probe_stdout, stderr="", returncode=probe_returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_probe_is_programmatic_not_dry_run_output_scraping(monkeypatch) -> None:
    """The probe runs :data:`CHROMIUM_PROBE_SOURCE`, never ``install --dry-run``.

    ``playwright install --dry-run`` prints the same block whether or not the
    browser is on disk, so its output can never answer this question (#2031).
    """
    calls = _record_subprocess(monkeypatch, CHROMIUM_PRESENT_MARKER)
    ensure_chromium_installed(make_login_io())

    assert calls == [[sys.executable, "-c", CHROMIUM_PROBE_SOURCE]]
    assert "--dry-run" not in calls[0]


def test_both_subprocess_calls_stay_timeout_bounded(monkeypatch) -> None:
    """30 s probe / 300 s install: an unbounded call could hang ``notebooklm login``."""
    timeouts: list[Any] = []

    def fake_run(cmd, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        if "install" in cmd:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        return SimpleNamespace(stdout=CHROMIUM_MISSING_MARKER, stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ensure_chromium_installed(make_login_io())

    assert timeouts == [30, 300]


def test_present_marker_skips_install(monkeypatch, capsys) -> None:
    """A present answer installs nothing and prints nothing."""
    calls = _record_subprocess(monkeypatch, CHROMIUM_PRESENT_MARKER)
    ensure_chromium_installed(make_login_io())

    assert len(calls) == 1  # probe only
    assert capsys.readouterr().out == ""


def test_missing_marker_runs_the_install(monkeypatch, capsys) -> None:
    """A missing answer triggers ``playwright install chromium`` (the #2031 bug)."""
    calls = _record_subprocess(monkeypatch, f"{CHROMIUM_MISSING_MARKER}\n")
    ensure_chromium_installed(make_login_io())

    assert calls[1] == [sys.executable, "-m", "playwright", "install", "chromium"]
    out = capsys.readouterr().out
    assert "Chromium browser not installed" in out
    assert "installed successfully" in out


@pytest.mark.parametrize(
    "probe_stdout",
    [
        pytest.param("", id="empty"),
        pytest.param("Traceback (most recent call last): ...", id="crashed"),
        pytest.param(f"{CHROMIUM_PRESENT_MARKER}{CHROMIUM_MISSING_MARKER}", id="both-markers"),
    ],
)
def test_unreadable_probe_answer_does_not_install(monkeypatch, capsys, probe_stdout) -> None:
    """An ambiguous answer must not install — guessing re-downloads every login."""
    calls = _record_subprocess(monkeypatch, probe_stdout)
    ensure_chromium_installed(make_login_io())

    assert len(calls) == 1  # probe only, no install
    assert capsys.readouterr().out == ""


def test_missing_marker_from_failed_probe_does_not_install(monkeypatch, capsys) -> None:
    """A non-zero probe exit is unreadable even when the marker reached stdout.

    A probe that dies after writing the marker (or a wrapper that echoes it)
    must not be trusted to start a download.
    """
    calls = _record_subprocess(monkeypatch, CHROMIUM_MISSING_MARKER, probe_returncode=1)
    ensure_chromium_installed(make_login_io())

    assert len(calls) == 1  # probe only, no install
    assert capsys.readouterr().out == ""


@pytest.mark.reality
@pytest.mark.requires_playwright
def test_probe_source_detects_both_states_against_real_playwright(tmp_path, monkeypatch) -> None:
    """The probe source answers correctly against REAL Playwright (#2031).

    Regression guard for the class of bug this replaced: the previous
    pre-flight scraped ``playwright install --dry-run chromium`` for a
    ``"will download"`` marker no Playwright release emits, and every unit test
    fed ``subprocess.run`` fabricated output — so the probe was never once
    checked against the real tool. This test runs the actual probe source.
    """
    browsers_dir = tmp_path / "browsers"
    browsers_dir.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers_dir))

    def run_source(source: str) -> str:
        result = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    # Empty browsers dir → the bundled build is genuinely absent.
    assert run_source(CHROMIUM_PROBE_SOURCE) == CHROMIUM_MISSING_MARKER

    # Materialise the exact executable Playwright resolves → detected present.
    resolved = Path(
        run_source(
            "import sys\n"
            "from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as playwright:\n"
            "    sys.stdout.write(playwright.chromium.executable_path)\n"
        )
    )
    assert browsers_dir in resolved.parents
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.touch()

    assert run_source(CHROMIUM_PROBE_SOURCE) == CHROMIUM_PRESENT_MARKER


@pytest.mark.reality
@pytest.mark.requires_chromium
def test_chromium_launches_headless_against_real_playwright() -> None:
    """A package import and executable path are not proof that Chromium launches."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content("<title>reality probe</title>")
            assert page.title() == "reality probe"
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# ensure_chromium_installed — timeout + generic exception pre-flight (575-588)
# ---------------------------------------------------------------------------
def test_ensure_chromium_timeout_warns_and_continues(monkeypatch, capsys) -> None:
    """A TimeoutExpired during the dry-run probe surfaces a warning and returns."""

    def fake_run(cmd, **_):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ensure_chromium_installed(make_login_io())  # must not raise
    out = capsys.readouterr().out
    assert "pre-flight check timed out" in out
    # Console may wrap "Proceeding anyway" across a line boundary; normalise.
    assert "Proceeding" in out and "anyway" in out


def test_ensure_chromium_generic_exception_warns_and_continues(monkeypatch, capsys) -> None:
    """A generic exception (e.g. FileNotFoundError) is swallowed with a warning."""

    def fake_run(cmd, **_):
        raise FileNotFoundError("playwright CLI missing")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ensure_chromium_installed(make_login_io())  # must not raise
    out = capsys.readouterr().out
    assert "pre-flight check failed" in out
    # Console may wrap "Proceeding anyway" across a line boundary; normalise.
    assert "Proceeding" in out and "anyway" in out


# ---------------------------------------------------------------------------
# recover_page — TargetClosed exit + non-TargetClosed re-raise (607-614)
# ---------------------------------------------------------------------------
@pytest.mark.requires_playwright
def test_recover_page_target_closed_exits(monkeypatch) -> None:
    """A TargetClosed error while recovering exits 1 with the browser-closed help."""
    from playwright.sync_api import Error as PlaywrightError

    context = MagicMock()
    context.new_page.side_effect = PlaywrightError(
        "Target page, context or browser has been closed"
    )
    io = _FakeLoginIO()
    with pytest.raises(SystemExit) as exc_info:
        recover_page(context, io)
    assert exc_info.value.code == 1
    assert len(io.emitted) == 1
    assert "browser window was closed" in io.emitted[0][0][0].lower()


@pytest.mark.requires_playwright
def test_recover_page_non_target_closed_reraises() -> None:
    """A non-TargetClosed PlaywrightError is re-raised after logging."""
    from playwright.sync_api import Error as PlaywrightError

    context = MagicMock()
    context.new_page.side_effect = PlaywrightError("some other failure")
    with pytest.raises(PlaywrightError):
        recover_page(context, _FakeLoginIO())


@pytest.mark.requires_playwright
def test_recover_page_success_returns_new_page() -> None:
    """The happy path returns ``context.new_page()`` directly."""
    fresh = object()
    context = MagicMock()
    context.new_page.return_value = fresh
    assert recover_page(context, _FakeLoginIO()) is fresh


# ---------------------------------------------------------------------------
# validate_login_flag_conflicts — remaining mutual-exclusion gates (676-694)
# ---------------------------------------------------------------------------
def _base_flags(**overrides: Any) -> dict[str, Any]:
    flags: dict[str, Any] = {
        "browser_cookies": "chrome",
        "account_email": None,
        "all_accounts": False,
        "update": False,
        "profile_name": None,
        "storage": None,
    }
    flags.update(overrides)
    return flags


def test_validate_flags_account_requires_browser_cookies() -> None:
    """--account without --browser-cookies returns a Conflict."""
    result = validate_login_flag_conflicts(
        **_base_flags(browser_cookies=None, account_email="bob@example.com")
    )
    assert isinstance(result, LoginFlagConflict)
    assert "require --browser-cookies" in login_flag_conflict_message(result)


def test_validate_flags_all_accounts_with_account_conflicts() -> None:
    """--all-accounts + --account returns a Conflict."""
    result = validate_login_flag_conflicts(
        **_base_flags(all_accounts=True, account_email="bob@example.com")
    )
    assert isinstance(result, LoginFlagConflict)
    assert "cannot be combined with --account" in login_flag_conflict_message(result)


def test_validate_flags_all_accounts_with_storage_conflicts() -> None:
    """--all-accounts + --storage returns a Conflict ."""
    result = validate_login_flag_conflicts(**_base_flags(all_accounts=True, storage="/tmp/s.json"))
    assert isinstance(result, LoginFlagConflict)
    assert "cannot be combined with --storage" in login_flag_conflict_message(result)


def test_validate_flags_update_requires_all_accounts() -> None:
    """--update without --all-accounts returns a Conflict."""
    result = validate_login_flag_conflicts(**_base_flags(update=True, all_accounts=False))
    assert isinstance(result, LoginFlagConflict)
    assert "--update only applies to --all-accounts" in login_flag_conflict_message(result)


def test_validate_flags_clean_combo_passes() -> None:
    """A valid flag combination returns None (no conflict)."""
    assert validate_login_flag_conflicts(**_base_flags()) is None


# ---------------------------------------------------------------------------
# prepare_login_paths — explicit storage + profile branches (713, 715)
# ---------------------------------------------------------------------------
def test_prepare_login_paths_explicit_storage(tmp_path, monkeypatch) -> None:
    """Explicit ``--storage`` wins and is returned verbatim ."""
    monkeypatch.setattr(login_browser.sys, "platform", "linux")
    browser_profile = tmp_path / "profile"
    # Patch the real consumer bindings the code resolves through, not the
    # transitional ``_resolve_paths_helper`` precedence shim. ``prepare_login_paths``
    # looks both names up on this module, so patching them here bites the call.
    fake_browser_profile_dir = MagicMock(return_value=browser_profile)
    fake_storage_path = MagicMock(return_value=tmp_path / "ignored")
    monkeypatch.setattr(login_browser, "get_browser_profile_dir", fake_browser_profile_dir)
    monkeypatch.setattr(login_browser, "get_storage_path", fake_storage_path)
    outcome = prepare_login_paths(
        profile=None, storage=str(tmp_path / "explicit.json"), fresh=False
    )
    assert isinstance(outcome, PreparedLoginPaths)
    assert outcome.storage_path == Path(str(tmp_path / "explicit.json"))
    assert outcome.browser_profile == browser_profile
    assert outcome.fresh_cleared is False
    # Explicit ``--storage`` short-circuits the path resolver entirely.
    fake_storage_path.assert_not_called()
    fake_browser_profile_dir.assert_called_once_with(storage_path=tmp_path / "explicit.json")


def test_prepare_login_paths_isolates_custom_storage_profiles(tmp_path) -> None:
    """Different custom storage files use different persistent browser profiles."""
    storage_a = tmp_path / "A.json"
    storage_b = tmp_path / "B.json"

    outcome_a = prepare_login_paths(profile="work", storage=str(storage_a), fresh=False)
    outcome_b = prepare_login_paths(profile="work", storage=str(storage_b), fresh=False)

    assert isinstance(outcome_a, PreparedLoginPaths)
    assert isinstance(outcome_b, PreparedLoginPaths)
    assert outcome_a.browser_profile == tmp_path / "A.json.browser_profile"
    assert outcome_b.browser_profile == tmp_path / "B.json.browser_profile"


def test_prepare_login_paths_marks_new_custom_profile_as_owned(tmp_path) -> None:
    """A browser directory created for explicit storage carries an ownership marker."""
    outcome = prepare_login_paths(
        profile=None,
        storage=str(tmp_path / "A.json"),
        fresh=False,
    )

    assert isinstance(outcome, PreparedLoginPaths)
    assert (outcome.browser_profile / ".notebooklm-owned").is_file()


def test_prepare_login_paths_fresh_refuses_unmarked_custom_profile(tmp_path) -> None:
    """--fresh never recursively deletes a pre-existing unowned sidecar."""
    browser_profile = tmp_path / "A.json.browser_profile"
    browser_profile.mkdir()
    payload = browser_profile / "keep-me"
    payload.write_text("external")

    outcome = prepare_login_paths(
        profile=None,
        storage=str(tmp_path / "A.json"),
        fresh=True,
    )

    assert isinstance(outcome, LoginPathError)
    assert "Refusing to delete" in login_path_error_message(outcome)
    assert payload.read_text() == "external"


def test_prepare_login_paths_fresh_refuses_arbitrary_conventional_profile(
    tmp_path, monkeypatch
) -> None:
    """A conventional filename outside NOTEBOOKLM_HOME is still unowned."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path / "home"))
    external = tmp_path / "external"
    external.mkdir()
    browser_profile = external / "browser_profile"
    browser_profile.mkdir()
    payload = browser_profile / "keep-me"
    payload.write_text("external")

    outcome = prepare_login_paths(
        profile=None,
        storage=str(external / "storage_state.json"),
        fresh=True,
    )

    assert isinstance(outcome, LoginPathError)
    assert payload.read_text() == "external"


def test_prepare_login_paths_fresh_accepts_explicit_named_profile_without_marker(
    tmp_path, monkeypatch
) -> None:
    """An explicit path to a managed profile has the same ownership as ``-p``."""
    home = tmp_path / "home"
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(home))
    profile_dir = home / "profiles" / "work"
    profile_dir.mkdir(parents=True)
    storage = profile_dir / "storage_state.json"
    storage.write_text("{}")
    browser_profile = profile_dir / "browser_profile"
    browser_profile.mkdir()
    payload = browser_profile / "stale"
    payload.write_text("session")

    outcome = prepare_login_paths(
        profile=None,
        storage=str(storage),
        fresh=True,
    )

    assert isinstance(outcome, PreparedLoginPaths)
    assert outcome.fresh_cleared is True
    assert not payload.exists()


def test_prepare_login_paths_fresh_clears_only_matching_custom_profile(tmp_path) -> None:
    """--fresh resets only the browser profile bound to the selected storage file."""
    browser_a = tmp_path / "A.json.browser_profile"
    browser_b = tmp_path / "B.json.browser_profile"
    browser_a.mkdir()
    browser_b.mkdir()
    ownership_a = browser_a / ".notebooklm-owned"
    ownership_a.touch()
    payload_a = browser_a / "payload"
    payload_a.write_text("A")
    marker_b = browser_b / "marker"
    marker_b.write_text("B")

    outcome = prepare_login_paths(
        profile=None,
        storage=str(tmp_path / "A.json"),
        fresh=True,
    )

    assert isinstance(outcome, PreparedLoginPaths)
    assert outcome.fresh_cleared is True
    assert browser_a.is_dir()
    assert ownership_a.is_file()
    assert not payload_a.exists()
    assert marker_b.read_text() == "B"


def test_prepare_login_paths_with_profile(tmp_path, monkeypatch) -> None:
    """The profile branch resolves via ``get_storage_path(profile=...)`` ."""
    monkeypatch.setattr(login_browser.sys, "platform", "linux")
    browser_profile = tmp_path / "profile"
    profile_storage = tmp_path / "work" / "storage.json"
    # Patch the real consumer bindings the code resolves through directly.
    fake_browser_profile_dir = MagicMock(return_value=browser_profile)
    fake_storage_path = MagicMock(return_value=profile_storage)
    monkeypatch.setattr(login_browser, "get_browser_profile_dir", fake_browser_profile_dir)
    monkeypatch.setattr(login_browser, "get_storage_path", fake_storage_path)
    outcome = prepare_login_paths(profile="work", storage=None, fresh=False)
    assert isinstance(outcome, PreparedLoginPaths)
    assert outcome.storage_path == profile_storage
    assert outcome.browser_profile == browser_profile
    # The profile branch forwards the profile name to the storage resolver.
    fake_storage_path.assert_called_once_with(profile="work")
    fake_browser_profile_dir.assert_called_once_with(profile="work")


# ---------------------------------------------------------------------------
# run_playwright_login via run_browser_capture — _capture_page_html PlaywrightError
# and cookie-forcing inner-recovery non-target-closed re-raise
# ---------------------------------------------------------------------------
@pytest.mark.requires_playwright
def test_run_playwright_login_capture_html_error_is_swallowed(tmp_path) -> None:
    """When ``page.content()`` raises PlaywrightError, metadata HTML is None."""
    from playwright.sync_api import Error as PlaywrightError

    storage_file = tmp_path / "storage.json"
    browser_dir = tmp_path / "profile"
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_page.url = f"https://{get_base_host()}/"
    mock_page.content.side_effect = PlaywrightError("cannot read content")
    mock_context.pages = [mock_page]
    mock_context.storage_state.return_value = _required_capture_state()
    mock_playwright = MagicMock()
    mock_playwright.chromium.launch_persistent_context.return_value = mock_context

    class _FakeSyncPlaywright:
        def __enter__(self):
            return mock_playwright

        def __exit__(self, *exc):
            return False

    repair_calls: list[Any] = []
    with (
        patch.object(playwright_login, "ensure_chromium_installed"),
        patch(
            "playwright.sync_api.sync_playwright",
            side_effect=lambda: _FakeSyncPlaywright(),
        ),
        patch.object(
            login_browser,
            "repair_playwright_account_metadata",
            side_effect=lambda storage_path, **kwargs: repair_calls.append(kwargs["page_html"]),
        ),
    ):
        playwright_login.run_playwright_login(
            BrowserLoginPlan(
                browser="chromium",
                browser_profile=browser_dir,
                storage_path=storage_file,
            ),
            _FakeLoginIO(),
        )
    # content() raised, so the page-html passed to repair is None.
    assert repair_calls == [None]


@pytest.mark.requires_playwright
def test_run_playwright_login_cookie_forcing_inner_recovery_reraises(tmp_path) -> None:
    """If the recovered page's cookie-forcing goto raises a non-navigation,
    non-target-closed PlaywrightError, it propagates ."""
    from playwright.sync_api import Error as PlaywrightError

    storage_file = tmp_path / "storage.json"
    browser_dir = tmp_path / "profile"
    mock_context = MagicMock()
    mock_page_stale = MagicMock()
    mock_page_stale.url = f"https://{get_base_host()}/"
    goto_count = 0

    def stale_goto(url, **kwargs):
        nonlocal goto_count
        goto_count += 1
        # First goto (initial navigation before login) succeeds.
        if goto_count == 1:
            return None
        # Cookie-forcing goto: stale page is dead -> trigger recovery.
        raise PlaywrightError("Target page, context or browser has been closed")

    mock_page_stale.goto.side_effect = stale_goto
    mock_page_recovered = MagicMock()
    mock_page_recovered.url = f"https://{get_base_host()}/"
    # The recovered page's goto raises a NON-target-closed, NON-navigation
    # PlaywrightError, which must propagate.
    mock_page_recovered.goto.side_effect = PlaywrightError("net::ERR_SOMETHING_ELSE while loading")
    mock_context.pages = [mock_page_stale]
    mock_context.new_page.return_value = mock_page_recovered
    mock_context.storage_state.return_value = _required_capture_state()
    mock_playwright = MagicMock()
    mock_playwright.chromium.launch_persistent_context.return_value = mock_context

    class _FakeSyncPlaywright:
        def __enter__(self):
            return mock_playwright

        def __exit__(self, *exc):
            return False

    with (
        patch.object(playwright_login, "ensure_chromium_installed"),
        patch(
            "playwright.sync_api.sync_playwright",
            side_effect=lambda: _FakeSyncPlaywright(),
        ),
        pytest.raises(PlaywrightError, match="ERR_SOMETHING_ELSE"),
    ):
        playwright_login.run_playwright_login(
            BrowserLoginPlan(
                browser="chromium",
                browser_profile=browser_dir,
                storage_path=storage_file,
            ),
            _FakeLoginIO(),
        )


# ---------------------------------------------------------------------------
# redact_subprocess_output — non-string env value skip
# ---------------------------------------------------------------------------
def test_redact_subprocess_output_skips_non_string_env_value() -> None:
    """A non-string env value is skipped via ``continue`` ."""
    # The mapping intentionally carries a non-str value to exercise the
    # ``isinstance(raw_value, str)`` guard's false branch.
    env: dict[str, Any] = {"GOOD": "supersecretvalue", "BAD": 12345}
    out = playwright_login.redact_subprocess_output("leak supersecretvalue here", env=env)
    assert "<redacted>" in out
    assert "supersecretvalue" not in out


# ---------------------------------------------------------------------------
# prepare_login_paths — win32 directory-creation branch
# ---------------------------------------------------------------------------
def test_prepare_login_paths_win32_skips_mode(tmp_path, monkeypatch) -> None:
    """On win32 the parent dirs are created without ``mode=`` ."""
    monkeypatch.setattr(login_browser.sys, "platform", "win32")
    browser_profile = tmp_path / "profile"
    storage_target = tmp_path / "win" / "storage.json"
    # Patch the real consumer bindings the code resolves through directly.
    fake_browser_profile_dir = MagicMock(return_value=browser_profile)
    fake_storage_path = MagicMock(return_value=storage_target)
    monkeypatch.setattr(login_browser, "get_browser_profile_dir", fake_browser_profile_dir)
    monkeypatch.setattr(login_browser, "get_storage_path", fake_storage_path)
    outcome = prepare_login_paths(profile=None, storage=None, fresh=False)
    assert isinstance(outcome, PreparedLoginPaths)
    assert outcome.storage_path == storage_target
    assert outcome.browser_profile == browser_profile
    assert storage_target.parent.is_dir()
    assert browser_profile.is_dir()
    # No profile, no explicit storage -> the resolver is called with no args.
    fake_storage_path.assert_called_once_with()
    fake_browser_profile_dir.assert_called_once_with()


def test_prepare_login_paths_fresh_wipe_success_flags_cleared(tmp_path, monkeypatch) -> None:
    """A ``--fresh`` wipe of an existing profile sets ``fresh_cleared``."""
    monkeypatch.setattr(login_browser.sys, "platform", "linux")
    browser_profile = tmp_path / "profile"
    browser_profile.mkdir()
    storage_target = tmp_path / "store" / "storage.json"
    monkeypatch.setattr(
        login_browser, "get_browser_profile_dir", MagicMock(return_value=browser_profile)
    )
    monkeypatch.setattr(login_browser, "get_storage_path", MagicMock(return_value=storage_target))
    outcome = prepare_login_paths(profile=None, storage=None, fresh=True)
    assert isinstance(outcome, PreparedLoginPaths)
    assert outcome.fresh_cleared is True
    # The pre-existing profile dir was removed then recreated as an empty dir.
    assert browser_profile.is_dir()
    assert not any(browser_profile.iterdir())


def test_prepare_login_paths_fresh_wipe_oserror_returns_path_error(tmp_path, monkeypatch) -> None:
    """An OSError during the ``--fresh`` wipe returns a :class:`PathError`."""
    monkeypatch.setattr(login_browser.sys, "platform", "linux")
    browser_profile = tmp_path / "profile"
    browser_profile.mkdir()
    storage_target = tmp_path / "store" / "storage.json"
    monkeypatch.setattr(
        login_browser, "get_browser_profile_dir", MagicMock(return_value=browser_profile)
    )
    monkeypatch.setattr(login_browser, "get_storage_path", MagicMock(return_value=storage_target))
    monkeypatch.setattr(login_browser.shutil, "rmtree", MagicMock(side_effect=OSError("locked")))
    outcome = prepare_login_paths(profile=None, storage=None, fresh=True)
    assert isinstance(outcome, LoginPathError)
    assert "Cannot clear browser profile: locked" in login_path_error_message(outcome)


# ---------------------------------------------------------------------------
# run_playwright_login — wait_for_url non-target-closed PlaywrightError (942)
# ---------------------------------------------------------------------------
@pytest.mark.requires_playwright
def test_run_playwright_login_wait_for_url_other_error_reraises(tmp_path) -> None:
    """A non-target-closed PlaywrightError from ``wait_for_url`` propagates
    IMMEDIATELY.

    The sentinel must not be a ``net::`` code. #2257 made every ``net::ERR_*``
    a recognised navigation failure, so the old ``net::ERR_WEIRD`` sentinel
    still reached this ``raises`` — but only after 20 tolerated retries, which
    is the cap path, not the immediate propagation this test is named for.
    ``Protocol error`` is unclassifiable by construction and restores the
    original meaning.
    """
    from playwright.sync_api import Error as PlaywrightError

    storage_file = tmp_path / "storage.json"
    browser_dir = tmp_path / "profile"
    mock_context = MagicMock()
    mock_page = MagicMock()
    # URL is NOT on the base host, so the wait_for_url branch is taken.
    mock_page.url = "https://accounts.google.com/signin"
    mock_page.goto.return_value = None
    mock_page.wait_for_url.side_effect = PlaywrightError("Protocol error: something structural")
    mock_context.pages = [mock_page]
    mock_context.storage_state.return_value = _required_capture_state()
    mock_playwright = MagicMock()
    mock_playwright.chromium.launch_persistent_context.return_value = mock_context

    class _FakeSyncPlaywright:
        def __enter__(self):
            return mock_playwright

        def __exit__(self, *exc):
            return False

    with (
        patch.object(playwright_login, "ensure_chromium_installed"),
        patch(
            "playwright.sync_api.sync_playwright",
            side_effect=lambda: _FakeSyncPlaywright(),
        ),
        pytest.raises(PlaywrightError, match="Protocol error"),
    ):
        playwright_login.run_playwright_login(
            BrowserLoginPlan(
                browser="chromium",
                browser_profile=browser_dir,
                storage_path=storage_file,
            ),
            _FakeLoginIO(),
        )


# ---------------------------------------------------------------------------
# run_playwright_login — injected ``io.fail`` inside the sync_playwright block
# still tears the context down via the ``finally`` (#1391 regression).
# ---------------------------------------------------------------------------
@pytest.mark.requires_playwright
def test_run_playwright_login_io_fail_inside_block_still_closes_context(tmp_path) -> None:
    """An ``io.fail`` (``SystemExit``) raised inside the ``with sync_playwright()``
    block must still run ``context.close()`` via the ``try/finally``.
    The drain (#1391) injects ``fail`` rather than calling ``exit_with_code``
    directly; because ``fail`` forwards to ``exit_with_code`` it raises
    ``SystemExit`` (a ``BaseException``), which slips past the ``except
    Exception`` handler and unwinds through the ``finally`` — so the browser
    context is torn down before the process exits. This pins that the injected
    sink does not regress the cleanup contract.
    """
    from playwright.sync_api import Error as PlaywrightError  # noqa: F401

    storage_file = tmp_path / "storage.json"
    browser_dir = tmp_path / "profile"
    mock_context = MagicMock()
    mock_page = MagicMock()
    # NOT on the base host even after cookie-forcing → the unexpected-URL
    # ``io.fail(1)`` branch fires *inside* the sync_playwright block.
    mock_page.url = "https://accounts.google.com/AccountChooser"
    mock_page.goto.return_value = None
    mock_context.pages = [mock_page]
    mock_context.storage_state.return_value = _required_capture_state()
    mock_playwright = MagicMock()
    mock_playwright.chromium.launch_persistent_context.return_value = mock_context

    class _FakeSyncPlaywright:
        def __enter__(self):
            return mock_playwright

        def __exit__(self, *exc):
            return False

    with (
        patch.object(playwright_login, "ensure_chromium_installed"),
        patch(
            "playwright.sync_api.sync_playwright",
            side_effect=lambda: _FakeSyncPlaywright(),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        playwright_login.run_playwright_login(
            BrowserLoginPlan(
                browser="chromium",
                browser_profile=browser_dir,
                storage_path=storage_file,
            ),
            _FakeLoginIO(),
        )
    assert exc_info.value.code == 1
    # The ``finally`` ran despite the SystemExit unwinding the block.
    mock_context.close.assert_called_once_with()
