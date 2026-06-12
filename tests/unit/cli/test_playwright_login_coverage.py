"""Coverage-focused unit tests for ``cli/services/playwright_login.py``.
These tests target branches not exercised by the existing
``test_login.py`` / ``test_playwright_login_stderr.py`` suites:
* :func:`_select_playwright_account` ambiguity-reason branches.
* :func:`repair_playwright_account_metadata` clear-metadata-failure path.
* :func:`windows_playwright_event_loop` win32 policy swap.
* :func:`ensure_chromium_installed` timeout + generic-exception pre-flight
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
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import notebooklm.auth as auth_module
from notebooklm.cli.playwright_login_io import make_login_io
from notebooklm.cli.services import playwright_login
from notebooklm.cli.services.playwright_login import (
    Conflict,
    PathError,
    PreparedPaths,
    _select_playwright_account,
    ensure_chromium_installed,
    prepare_login_paths,
    recover_page,
    repair_playwright_account_metadata,
    validate_login_flag_conflicts,
    windows_playwright_event_loop,
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

    def _boom_build(_path):
        raise ValueError("bad storage state")

    def _boom_clear(_path):
        raise OSError("cannot clear")

    with (
        patch.object(auth_module, "build_httpx_cookies_from_storage", side_effect=_boom_build),
        patch.object(auth_module, "clear_account_metadata", side_effect=_boom_clear),
        patch.object(auth_module, "extract_email_from_html", return_value=None),
        caplog.at_level(logging.WARNING, logger="notebooklm.cli.services.playwright_login"),
    ):
        result = repair_playwright_account_metadata(
            storage_path, _FakeLoginIO(), page_html=None, quiet=True
        )
    assert result is False
    assert any(
        "Failed to clear stale account metadata" in rec.getMessage() for rec in caplog.records
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
    monkeypatch.setattr(playwright_login.sys, "platform", "win32")
    with windows_playwright_event_loop():
        # First swap installs a fresh DefaultEventLoopPolicy.
        assert isinstance(swapped_policies[-1], _DefaultPolicy)
    # On exit the original policy is restored.
    assert swapped_policies[-1] is sentinel_original


def test_windows_event_loop_noop_off_win32(monkeypatch) -> None:
    """Off win32 the context manager is a pure no-op."""
    monkeypatch.setattr(playwright_login.sys, "platform", "linux")
    with windows_playwright_event_loop():
        pass  # no exception, nothing swapped


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
    assert isinstance(result, Conflict)
    assert "require --browser-cookies" in result.message


def test_validate_flags_all_accounts_with_account_conflicts() -> None:
    """--all-accounts + --account returns a Conflict."""
    result = validate_login_flag_conflicts(
        **_base_flags(all_accounts=True, account_email="bob@example.com")
    )
    assert isinstance(result, Conflict)
    assert "cannot be combined with --account" in result.message


def test_validate_flags_all_accounts_with_storage_conflicts() -> None:
    """--all-accounts + --storage returns a Conflict ."""
    result = validate_login_flag_conflicts(**_base_flags(all_accounts=True, storage="/tmp/s.json"))
    assert isinstance(result, Conflict)
    assert "cannot be combined with --storage" in result.message


def test_validate_flags_update_requires_all_accounts() -> None:
    """--update without --all-accounts returns a Conflict."""
    result = validate_login_flag_conflicts(**_base_flags(update=True, all_accounts=False))
    assert isinstance(result, Conflict)
    assert "--update only applies to --all-accounts" in result.message


def test_validate_flags_clean_combo_passes() -> None:
    """A valid flag combination returns None (no conflict)."""
    assert validate_login_flag_conflicts(**_base_flags()) is None


# ---------------------------------------------------------------------------
# prepare_login_paths — explicit storage + profile branches (713, 715)
# ---------------------------------------------------------------------------
def test_prepare_login_paths_explicit_storage(tmp_path, monkeypatch) -> None:
    """Explicit ``--storage`` wins and is returned verbatim ."""
    monkeypatch.setattr(playwright_login.sys, "platform", "linux")
    browser_profile = tmp_path / "profile"
    # Patch the real consumer bindings the code resolves through, not the
    # transitional ``_resolve_paths_helper`` precedence shim. ``prepare_login_paths``
    # looks both names up on this module, so patching them here bites the call.
    fake_browser_profile_dir = MagicMock(return_value=browser_profile)
    fake_storage_path = MagicMock(return_value=tmp_path / "ignored")
    monkeypatch.setattr(playwright_login, "get_browser_profile_dir", fake_browser_profile_dir)
    monkeypatch.setattr(playwright_login, "get_storage_path", fake_storage_path)
    outcome = prepare_login_paths(
        profile=None, storage=str(tmp_path / "explicit.json"), fresh=False
    )
    assert isinstance(outcome, PreparedPaths)
    assert outcome.storage_path == Path(str(tmp_path / "explicit.json"))
    assert outcome.browser_profile == browser_profile
    assert outcome.fresh_cleared is False
    # Explicit ``--storage`` short-circuits the path resolver entirely.
    fake_storage_path.assert_not_called()
    fake_browser_profile_dir.assert_called_once_with()


def test_prepare_login_paths_with_profile(tmp_path, monkeypatch) -> None:
    """The profile branch resolves via ``get_storage_path(profile=...)`` ."""
    monkeypatch.setattr(playwright_login.sys, "platform", "linux")
    browser_profile = tmp_path / "profile"
    profile_storage = tmp_path / "work" / "storage.json"
    # Patch the real consumer bindings the code resolves through directly.
    fake_browser_profile_dir = MagicMock(return_value=browser_profile)
    fake_storage_path = MagicMock(return_value=profile_storage)
    monkeypatch.setattr(playwright_login, "get_browser_profile_dir", fake_browser_profile_dir)
    monkeypatch.setattr(playwright_login, "get_storage_path", fake_storage_path)
    outcome = prepare_login_paths(profile="work", storage=None, fresh=False)
    assert isinstance(outcome, PreparedPaths)
    assert outcome.storage_path == profile_storage
    assert outcome.browser_profile == browser_profile
    # The profile branch forwards the profile name to the storage resolver.
    fake_storage_path.assert_called_once_with(profile="work")
    fake_browser_profile_dir.assert_called_once_with()


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
    mock_page.url = "https://notebooklm.google.com/"
    mock_page.content.side_effect = PlaywrightError("cannot read content")
    mock_context.pages = [mock_page]
    mock_context.storage_state.return_value = {"cookies": [], "origins": []}
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
            playwright_login,
            "repair_playwright_account_metadata",
            side_effect=lambda storage_path, io, *, page_html=None, quiet=False: (
                repair_calls.append(page_html)
            ),
        ),
    ):
        playwright_login.run_playwright_login(
            playwright_login.PlaywrightLoginPlan(
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
    mock_page_stale.url = "https://notebooklm.google.com/"
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
    mock_page_recovered.url = "https://notebooklm.google.com/"
    # The recovered page's goto raises a NON-target-closed, NON-navigation
    # PlaywrightError, which must propagate.
    mock_page_recovered.goto.side_effect = PlaywrightError("net::ERR_SOMETHING_ELSE while loading")
    mock_context.pages = [mock_page_stale]
    mock_context.new_page.return_value = mock_page_recovered
    mock_context.storage_state.return_value = {"cookies": [], "origins": []}
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
            playwright_login.PlaywrightLoginPlan(
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
# ensure_chromium_installed — install success path
# ---------------------------------------------------------------------------
def test_ensure_chromium_install_success(monkeypatch, capsys) -> None:
    """When the dry-run reports a missing browser and install succeeds, the
    success banner is printed ."""
    calls: list[list[str]] = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        if "--dry-run" in cmd:
            return SimpleNamespace(stdout="chromium will download to ...", stderr="", returncode=0)
        # The real install call.
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ensure_chromium_installed(make_login_io())
    out = capsys.readouterr().out
    assert "installed successfully" in out
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# prepare_login_paths — win32 directory-creation branch
# ---------------------------------------------------------------------------
def test_prepare_login_paths_win32_skips_mode(tmp_path, monkeypatch) -> None:
    """On win32 the parent dirs are created without ``mode=`` ."""
    monkeypatch.setattr(playwright_login.sys, "platform", "win32")
    browser_profile = tmp_path / "profile"
    storage_target = tmp_path / "win" / "storage.json"
    # Patch the real consumer bindings the code resolves through directly.
    fake_browser_profile_dir = MagicMock(return_value=browser_profile)
    fake_storage_path = MagicMock(return_value=storage_target)
    monkeypatch.setattr(playwright_login, "get_browser_profile_dir", fake_browser_profile_dir)
    monkeypatch.setattr(playwright_login, "get_storage_path", fake_storage_path)
    outcome = prepare_login_paths(profile=None, storage=None, fresh=False)
    assert isinstance(outcome, PreparedPaths)
    assert outcome.storage_path == storage_target
    assert outcome.browser_profile == browser_profile
    assert storage_target.parent.is_dir()
    assert browser_profile.is_dir()
    # No profile, no explicit storage -> the resolver is called with no args.
    fake_storage_path.assert_called_once_with()
    fake_browser_profile_dir.assert_called_once_with()


def test_prepare_login_paths_fresh_wipe_success_flags_cleared(tmp_path, monkeypatch) -> None:
    """A ``--fresh`` wipe of an existing profile sets ``fresh_cleared``."""
    monkeypatch.setattr(playwright_login.sys, "platform", "linux")
    browser_profile = tmp_path / "profile"
    browser_profile.mkdir()
    storage_target = tmp_path / "store" / "storage.json"
    monkeypatch.setattr(
        playwright_login, "get_browser_profile_dir", MagicMock(return_value=browser_profile)
    )
    monkeypatch.setattr(
        playwright_login, "get_storage_path", MagicMock(return_value=storage_target)
    )
    outcome = prepare_login_paths(profile=None, storage=None, fresh=True)
    assert isinstance(outcome, PreparedPaths)
    assert outcome.fresh_cleared is True
    # The pre-existing profile dir was removed then recreated as an empty dir.
    assert browser_profile.is_dir()
    assert not any(browser_profile.iterdir())


def test_prepare_login_paths_fresh_wipe_oserror_returns_path_error(tmp_path, monkeypatch) -> None:
    """An OSError during the ``--fresh`` wipe returns a :class:`PathError`."""
    monkeypatch.setattr(playwright_login.sys, "platform", "linux")
    browser_profile = tmp_path / "profile"
    browser_profile.mkdir()
    storage_target = tmp_path / "store" / "storage.json"
    monkeypatch.setattr(
        playwright_login, "get_browser_profile_dir", MagicMock(return_value=browser_profile)
    )
    monkeypatch.setattr(
        playwright_login, "get_storage_path", MagicMock(return_value=storage_target)
    )
    monkeypatch.setattr(playwright_login.shutil, "rmtree", MagicMock(side_effect=OSError("locked")))
    outcome = prepare_login_paths(profile=None, storage=None, fresh=True)
    assert isinstance(outcome, PathError)
    assert "Cannot clear browser profile: locked" in outcome.message


# ---------------------------------------------------------------------------
# run_playwright_login — wait_for_url non-target-closed PlaywrightError (942)
# ---------------------------------------------------------------------------
@pytest.mark.requires_playwright
def test_run_playwright_login_wait_for_url_other_error_reraises(tmp_path) -> None:
    """A non-target-closed PlaywrightError from ``wait_for_url`` propagates
    ."""
    from playwright.sync_api import Error as PlaywrightError

    storage_file = tmp_path / "storage.json"
    browser_dir = tmp_path / "profile"
    mock_context = MagicMock()
    mock_page = MagicMock()
    # URL is NOT on the base host, so the wait_for_url branch is taken.
    mock_page.url = "https://accounts.google.com/signin"
    mock_page.goto.return_value = None
    mock_page.wait_for_url.side_effect = PlaywrightError("net::ERR_WEIRD other failure")
    mock_context.pages = [mock_page]
    mock_context.storage_state.return_value = {"cookies": [], "origins": []}
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
        pytest.raises(PlaywrightError, match="ERR_WEIRD"),
    ):
        playwright_login.run_playwright_login(
            playwright_login.PlaywrightLoginPlan(
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
    mock_context.storage_state.return_value = {"cookies": [], "origins": []}
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
            playwright_login.PlaywrightLoginPlan(
                browser="chromium",
                browser_profile=browser_dir,
                storage_path=storage_file,
            ),
            _FakeLoginIO(),
        )
    assert exc_info.value.code == 1
    # The ``finally`` ran despite the SystemExit unwinding the block.
    mock_context.close.assert_called_once_with()
