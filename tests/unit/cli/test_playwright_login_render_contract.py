"""Characterization net for the ``playwright_login`` render contract (#1391 PR-1).

This is the **refactor-invariant baseline** for the planned drain of
``cli/services/playwright_login.py`` from the ADR-0008 services-boundary
allowlist (#1391). It pins the *current* command-boundary render contract of
the ``notebooklm login`` (Playwright) and ``notebooklm auth refresh`` flows so
the later refactor (PR-2) — which inverts ``console.print`` / ``exit_with_code``
/ ``run_async`` into an injected ``LoginIO`` sink — can be diffed against green.

Why the command boundary, not the helpers
==========================================

PR-2 moves *where* the raise / render happens (out of ``validate`` /
``prepare`` and behind a service-local Protocol), but the ``login`` /
``auth refresh`` Click commands re-render byte-identically afterwards. Driving
the real commands through ``CliRunner().invoke`` and snapshotting
``result.output`` by **string equality** (not substring) plus
``result.exit_code`` therefore survives PR-2 unchanged — substring asserts
would silently pass even if the refactor dropped or reordered a line, and they
miss the two ``markup=False`` sites where Rich would otherwise eat ``[...]``
brackets.

Determinism
===========

Every command is driven with ``COLUMNS=80`` so Rich wraps at a fixed width
regardless of the CI runner's terminal, and every storage / browser-profile
path is a short, synthetic, filesystem-free :func:`_fake_path` whose ``str``
renders identically on Linux / macOS / Windows and never wraps. The one
remaining host-dependent value — ``sys.executable`` in the Chromium
install-failure diagnostic — is pinned to a fixed stub so that line, too, is
byte-stable.

These tests assert **current** behaviour: they are green on ``origin/main``
with zero ``src/`` change and must stay green across PR-2.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import notebooklm.cli.services.playwright_login as _pl
from notebooklm.notebooklm_cli import cli

# Fixed, synthetic paths keep the snapshots byte-stable across hosts AND across
# the OS test matrix (ubuntu / macos / windows). They are short enough that the
# rendered ``... <path>`` lines never wrap at the pinned 80-column width, and —
# crucially — every snapshot interpolates these *same* ``Path`` objects, so the
# OS-specific separator (``/`` vs ``\``) matches in both the rendered output and
# the expected string. No real file is ever written to them: the filesystem
# boundary (Playwright ``storage_state`` write, ``--fresh`` ``rmtree`` / ``mkdir``,
# the auth-refresh repair's metadata read/write) is mocked in every test.
_STORAGE = Path("/x/storage.json")
_PROFILE = Path("/x/profile")
_PROFILE_NAME = "default"

# Pin the Rich console width so the wrapped output is deterministic regardless of
# any ``COLUMNS`` the CI runner exports. 80 is the current no-TTY default, so
# this characterizes today's behaviour exactly — it just removes the dependency
# on the ambient terminal width.
_ENV = {"COLUMNS": "80"}


def _fake_path(text: str, *, exists: bool = False) -> MagicMock:
    """A filesystem-free stand-in for a ``pathlib.Path``.

    ``prepare_login_paths`` and the storage write call ``.exists()`` / ``.mkdir()``
    / ``.chmod()`` / ``.parent.mkdir()`` on the resolved paths and render them via
    ``f"...{path}"``. Returning a configured ``MagicMock`` instead of a real
    ``Path`` makes every method a no-op while ``str(...)`` yields exactly
    ``text`` — byte-identical on Linux / macOS / Windows (a real ``Path`` would
    render OS-specific separators and length). No real directory is ever
    created, so the synthetic ``/x/...`` paths never need to exist on disk.
    """
    fake = MagicMock(name=f"FakePath({text!r})")
    fake.__str__.return_value = text
    fake.__fspath__.return_value = text
    fake.exists.return_value = exists
    parent = MagicMock(name=f"FakePath({text!r}).parent")
    parent.__str__.return_value = text.rsplit("/", 1)[0] or "/"
    fake.parent = parent
    return fake


def _required_cookie_state() -> dict[str, Any]:
    """A minimal valid Playwright ``storage_state`` for the auth-refresh flow."""
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


def _drive_login(
    runner,
    *,
    args: list[str] | None = None,
    page_url: str = "https://notebooklm.google.com/",
    goto_side: Any = None,
    wait_side: Any = None,
    wire: Any = None,
    new_page: Any = None,
    new_page_side: Any = None,
    launch_side: Any = None,
    storage_state: dict[str, Any] | None = None,
    subprocess_run: Any = None,
    python_executable: str | None = None,
    patch_ensure: bool = True,
    patch_repair: bool = True,
    page_content: Any = None,
    profile_dir: Path = _PROFILE,
    fresh_profile_exists: bool = False,
    rmtree_side: Any = None,
):
    """Drive the real ``login`` command with a mocked Playwright + fixed paths.

    Returns ``(result, page)`` where ``page`` is the mocked initial ``Page`` so
    callers can tweak per-call ``side_effect`` after the fact if needed.

    ``wire`` (when given) is called as ``wire(page)`` immediately after the
    mock page is constructed, so a test can attach ``goto`` / ``wait_for_url``
    side effects that *mutate the live page* (e.g. flip ``page.url`` on a
    successful wait) without closing over a not-yet-bound name.

    The path patches (``get_storage_path`` / ``get_browser_profile_dir`` return
    filesystem-free :func:`_fake_path` stand-ins; ``resolve_profile`` is pinned)
    keep the rendered paths byte-stable, and the storage write
    (``atomic_write_json``) plus the ``--fresh`` ``shutil.rmtree`` are stubbed —
    so the synthetic ``_STORAGE`` / ``_PROFILE`` paths are never created on disk.
    ``fresh_profile_exists`` drives the ``--fresh`` ``browser_profile.exists()``
    gate; ``rmtree_side`` makes the profile wipe raise. The metadata-repair and
    language-sync collaborators are patched to no-ops so the snapshots cover only
    the Playwright service's own render lines.
    """
    if storage_state is None:
        storage_state = {"cookies": [], "origins": []}

    from contextlib import ExitStack

    with ExitStack() as stack:
        if patch_ensure:
            stack.enter_context(patch.object(_pl, "ensure_chromium_installed"))
        if subprocess_run is not None:
            stack.enter_context(patch("subprocess.run", subprocess_run))
        if python_executable is not None:
            # Pin ``sys.executable`` so the install-failure "Run manually:" line
            # wraps deterministically regardless of the host interpreter path.
            stack.enter_context(patch.object(_pl.sys, "executable", python_executable))
        mock_pw = stack.enter_context(patch("playwright.sync_api.sync_playwright"))
        stack.enter_context(
            patch.object(_pl, "get_storage_path", return_value=_fake_path(str(_STORAGE)))
        )
        stack.enter_context(
            patch.object(
                _pl,
                "get_browser_profile_dir",
                return_value=_fake_path(str(profile_dir), exists=fresh_profile_exists),
            )
        )
        stack.enter_context(patch("notebooklm.paths.resolve_profile", return_value=_PROFILE_NAME))
        stack.enter_context(patch("notebooklm.cli.session_cmd._sync_server_language_to_config"))
        if patch_repair:
            stack.enter_context(
                patch("notebooklm.cli.services.playwright_login.repair_playwright_account_metadata")
            )
        # The synthetic ``_STORAGE`` path is never created on disk; stub the
        # atomic write so the success paths don't touch the filesystem.
        stack.enter_context(patch("notebooklm.cli.services.playwright_login.atomic_write_json"))
        # ``--fresh`` wipes the (fake) profile via ``shutil.rmtree``; control
        # success vs OSError here. Non-fresh runs skip the wipe entirely.
        stack.enter_context(
            patch(
                "notebooklm.cli.services.playwright_login.shutil.rmtree",
                side_effect=rmtree_side,
            )
        )
        # Linear retry backoff sleeps real seconds; neutralise it.
        stack.enter_context(patch("notebooklm.cli.services.playwright_login.time.sleep"))

        mock_context = MagicMock()
        page = MagicMock()
        page.url = page_url
        if page_content is not None:
            page.content.return_value = page_content
        if goto_side is not None:
            page.goto.side_effect = goto_side
        if wait_side is not None:
            page.wait_for_url.side_effect = wait_side
        if wire is not None:
            wire(page)
        mock_context.pages = [page]
        if new_page is not None:
            mock_context.new_page.return_value = new_page
        if new_page_side is not None:
            mock_context.new_page.side_effect = new_page_side
        mock_context.storage_state.return_value = storage_state
        launch = mock_pw.return_value.__enter__.return_value.chromium.launch_persistent_context
        if launch_side is not None:
            launch.side_effect = launch_side
        else:
            launch.return_value = mock_context

        result = runner.invoke(cli, args or ["login"], env=_ENV)
    return result, page


def _drive_refresh(runner, *, enumerate_accounts: Any, args: list[str]):
    """Drive the real ``auth refresh`` keepalive path against synthetic storage.

    The keepalive-only path (no ``--browser-cookies``) fetches tokens, then —
    when the on-disk account metadata is missing / malformed — runs the real
    ``repair_playwright_account_metadata`` so its render lines are captured. The
    storage path is a filesystem-free :func:`_fake_path` (``_STORAGE``): the
    token fetch, ``read_account_metadata`` (returns ``{}`` → repair runs), and
    the repair's ``build_httpx_cookies_from_storage`` / ``write_account_metadata``
    / ``clear_account_metadata`` / ``extract_email_from_html`` are all mocked, so
    nothing is read from / written to disk. Only ``enumerate_accounts`` varies
    per test to drive the repair branches.
    """
    from contextlib import ExitStack

    storage = _fake_path(str(_STORAGE), exists=True)
    with ExitStack() as stack:
        stack.enter_context(patch.object(_pl, "get_storage_path", return_value=storage))
        stack.enter_context(
            patch("notebooklm.cli.session_cmd.get_storage_path", return_value=storage)
        )
        mock_fetch = stack.enter_context(
            patch("notebooklm.cli.session_cmd.fetch_tokens_with_domains", new_callable=AsyncMock)
        )
        mock_fetch.return_value = ("csrf_ok", "session_ok")
        stack.enter_context(patch("notebooklm.auth.read_account_metadata", return_value={}))
        # Repair collaborators (file-touching) stubbed; only enumeration varies.
        stack.enter_context(patch("notebooklm.auth.enumerate_accounts", new=enumerate_accounts))
        stack.enter_context(
            patch("notebooklm.auth.build_httpx_cookies_from_storage", return_value=MagicMock())
        )
        stack.enter_context(patch("notebooklm.auth.write_account_metadata"))
        stack.enter_context(patch("notebooklm.auth.clear_account_metadata"))
        stack.enter_context(patch("notebooklm.auth.extract_email_from_html", return_value=None))
        return runner.invoke(cli, args, env=_ENV)


# ---------------------------------------------------------------------------
# Pre-flight: validate_login_flag_conflicts (4 conflicts) + login env block
# ---------------------------------------------------------------------------


class TestPreflightValidate:
    def test_account_requires_browser_cookies(self, runner):
        result = runner.invoke(cli, ["login", "--account", "bob@example.com"], env=_ENV)
        assert result.exit_code == 1
        assert result.output == (
            "Error: --account, --all-accounts, and --profile-name require --browser-cookies.\n"
        )

    def test_all_accounts_with_account_conflicts(self, runner):
        result = runner.invoke(
            cli,
            ["login", "--browser-cookies", "chrome", "--all-accounts", "--account", "bob@x.com"],
            env=_ENV,
        )
        assert result.exit_code == 1
        assert result.output == (
            "Error: --all-accounts cannot be combined with --account or --profile-name.\n"
        )

    def test_all_accounts_with_storage_conflicts(self, runner):
        result = runner.invoke(
            cli,
            ["login", "--browser-cookies", "chrome", "--all-accounts", "--storage", "/tmp/s.json"],
            env=_ENV,
        )
        assert result.exit_code == 1
        assert result.output == (
            "Error: --all-accounts writes one profile per account and cannot be "
            "combined with\n--storage.\n"
        )

    def test_update_requires_all_accounts(self, runner):
        result = runner.invoke(cli, ["login", "--browser-cookies", "chrome", "--update"], env=_ENV)
        assert result.exit_code == 1
        assert result.output == "Error: --update only applies to --all-accounts.\n"


# ---------------------------------------------------------------------------
# Pre-flight: prepare_login_paths --fresh (success + OSError exit)
# ---------------------------------------------------------------------------


class TestPreflightPrepareFresh:
    @pytest.mark.requires_playwright
    def test_fresh_clears_profile_then_logs_in(self, runner):
        result, _ = _drive_login(
            runner,
            args=["login", "--fresh"],
            fresh_profile_exists=True,
        )
        assert result.exit_code == 0
        assert result.output == (
            "Cleared cached browser session (--fresh)\n"
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "Already logged in.\n"
            "\n"
            f"Authentication saved to: {_STORAGE}\n"
        )

    @pytest.mark.requires_playwright
    def test_fresh_clear_failure_exits(self, runner):
        result, _ = _drive_login(
            runner,
            args=["login", "--fresh"],
            fresh_profile_exists=True,
            rmtree_side=OSError("locked"),
        )
        assert result.exit_code == 1
        assert result.output == (
            "Cannot clear browser profile: locked\n"
            "Close any open browser windows and try again.\n"
            f"If the problem persists, manually delete: {_PROFILE}\n"
        )


# ---------------------------------------------------------------------------
# ensure_chromium_installed (banner / success / install-fail / timeout / generic)
# Reached through the real ``login`` command's chromium pre-flight by patching
# ``subprocess.run`` rather than stubbing out the helper.
# ---------------------------------------------------------------------------


def _dry_run_says_missing(stdout="chromium will download to ...") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


class TestEnsureChromiumInstalled:
    @pytest.mark.requires_playwright
    def test_install_success_banner_then_login(self, runner):
        def fake_run(cmd, **_):
            if "--dry-run" in cmd:
                return _dry_run_says_missing()
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        result, _ = _drive_login(runner, subprocess_run=fake_run, patch_ensure=False)
        assert result.exit_code == 0
        assert result.output == (
            "Chromium browser not installed. Installing now...\n"
            "Chromium installed successfully.\n"
            "\n"
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "Already logged in.\n"
            "\n"
            f"Authentication saved to: {_STORAGE}\n"
        )

    @pytest.mark.requires_playwright
    def test_install_failure_exits_with_markup_false_diagnostic(self, runner):
        """The install-failure path pins the ``markup=False`` site (``:524``).

        The captured subprocess line ``install boom [err]`` keeps its literal
        ``[err]`` brackets, and the surrounding ``[dim]...[/dim]`` tags render
        verbatim (markup disabled) — a substring assert would miss both.
        """

        def fake_run(cmd, **_):
            if "--dry-run" in cmd:
                return _dry_run_says_missing()
            return SimpleNamespace(stdout="", stderr="install boom [err]", returncode=1)

        result, _ = _drive_login(
            runner, subprocess_run=fake_run, patch_ensure=False, python_executable="/py"
        )
        assert result.exit_code == 1
        assert result.output == (
            "Chromium browser not installed. Installing now...\n"
            "Failed to install Chromium browser.\n"
            'Run manually: "/py" -m playwright install chromium\n'
            "[dim]Subprocess output (sanitised):[/dim]\n"
            "install boom [err]\n"
        )

    @pytest.mark.requires_playwright
    def test_preflight_timeout_warns_and_proceeds(self, runner):
        def fake_run(cmd, **_):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

        result, _ = _drive_login(runner, subprocess_run=fake_run, patch_ensure=False)
        assert result.exit_code == 0
        assert result.output == (
            "Warning: Chromium pre-flight check timed out after 30s. Proceeding "
            "anyway.\n"
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "Already logged in.\n"
            "\n"
            f"Authentication saved to: {_STORAGE}\n"
        )

    @pytest.mark.requires_playwright
    def test_preflight_generic_error_warns_and_proceeds(self, runner):
        def fake_run(cmd, **_):
            raise FileNotFoundError("playwright CLI missing")

        result, _ = _drive_login(runner, subprocess_run=fake_run, patch_ensure=False)
        assert result.exit_code == 0
        assert result.output == (
            "Warning: Chromium pre-flight check failed: playwright CLI missing. "
            "Proceeding \nanyway.\n"
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "Already logged in.\n"
            "\n"
            f"Authentication saved to: {_STORAGE}\n"
        )


# ---------------------------------------------------------------------------
# run_playwright_login — Playwright-not-installed (markup=False ``:747``)
# ---------------------------------------------------------------------------


class TestPlaywrightNotInstalled:
    def test_chromium_install_hint_keeps_browser_extra_and_playwright_line(self, runner):
        """``:747`` ``markup=False`` keeps the literal ``[browser]`` extra.

        With markup enabled Rich would parse ``[browser]`` as a style tag and
        strip it, leaving ``pip install "notebooklm-py"`` (no extras). The
        chromium hint also carries the ``playwright install chromium`` line.
        """
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            result = runner.invoke(cli, ["login"], env=_ENV)
        assert result.exit_code == 1
        assert result.output == (
            "Playwright not installed. Run:\n"
            '  pip install "notebooklm-py[browser]"\n'
            "  playwright install chromium\n"
        )

    def test_channel_install_hint_omits_playwright_line(self, runner):
        with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
            result = runner.invoke(cli, ["login", "--browser", "msedge"], env=_ENV)
        assert result.exit_code == 1
        assert result.output == (
            'Playwright not installed. Run:\n  pip install "notebooklm-py[browser]"\n'
        )


# ---------------------------------------------------------------------------
# run_playwright_login — progress / success render
# ---------------------------------------------------------------------------


class TestLoginProgressSuccess:
    @pytest.mark.requires_playwright
    def test_already_logged_in_fast_path(self, runner):
        result, _ = _drive_login(runner)
        assert result.exit_code == 0
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "Already logged in.\n"
            "\n"
            f"Authentication saved to: {_STORAGE}\n"
        )

    @pytest.mark.requires_playwright
    def test_not_logged_in_instructions_then_login_detected(self, runner):
        def wire(page):
            def wait_succeeds(url, **kwargs):
                page.url = "https://notebooklm.google.com/"

            page.wait_for_url.side_effect = wait_succeeds

        result, _ = _drive_login(
            runner,
            page_url="https://accounts.google.com/signin",
            wire=wire,
        )
        assert result.exit_code == 0
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "\n"
            "Instructions:\n"
            "1. Complete the Google login in the browser window\n"
            "2. Authentication will be saved automatically once login is detected\n"
            "\n"
            "Waiting for login (up to 5 minutes)...\n"
            "Login detected.\n"
            "\n"
            f"Authentication saved to: {_STORAGE}\n"
        )

    @pytest.mark.requires_playwright
    def test_target_closed_then_recovers_and_succeeds(self, runner):
        from playwright.sync_api import Error as PlaywrightError

        recovered = MagicMock()
        recovered.url = "https://notebooklm.google.com/"
        recovered.goto.return_value = None

        calls = {"n": 0}

        def goto_side(url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PlaywrightError("Target page, context or browser has been closed")
            return None

        result, _ = _drive_login(runner, goto_side=goto_side, new_page=recovered)
        assert result.exit_code == 0
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "Browser page closed (attempt 1/3). Retrying with fresh page...\n"
            "Already logged in.\n"
            "\n"
            f"Authentication saved to: {_STORAGE}\n"
        )

    @pytest.mark.requires_playwright
    def test_navigation_interrupted_during_cookie_forcing_is_ignored(self, runner):
        from playwright.sync_api import Error as PlaywrightError

        calls = {"n": 0}

        def goto_side(url, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise PlaywrightError("Page.goto: Navigation interrupted by another one")
            return None

        result, _ = _drive_login(runner, goto_side=goto_side)
        assert result.exit_code == 0
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "Already logged in.\n"
            "\n"
            f"Authentication saved to: {_STORAGE}\n"
        )

    @pytest.mark.requires_playwright
    def test_single_account_metadata_is_written(self, runner):
        """End-to-end success including the real metadata-repair render lines.

        The repair runs for real (``patch_repair=False``) so its
        ``Identifying Google account...`` / ``Account: <email>`` lines are
        snapshotted, but its filesystem-touching ``auth`` collaborators are
        stubbed so nothing is read from / written to the synthetic ``_STORAGE``.
        """
        from notebooklm.auth import Account

        async def _enum(*args, **kwargs):
            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch("notebooklm.auth.build_httpx_cookies_from_storage", return_value=MagicMock()),
            patch("notebooklm.auth.write_account_metadata"),
            patch("notebooklm.auth.extract_email_from_html", return_value=None),
        ):
            result, _ = _drive_login(
                runner,
                patch_repair=False,
                page_content="<html></html>",
                storage_state=_required_cookie_state(),
            )

        assert result.exit_code == 0
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "Already logged in.\n"
            "Identifying Google account...\n"
            "Account: alice@example.com\n"
            "\n"
            f"Authentication saved to: {_STORAGE}\n"
        )


# ---------------------------------------------------------------------------
# run_playwright_login — error render
# ---------------------------------------------------------------------------


class TestLoginErrorRender:
    @pytest.mark.requires_playwright
    def test_retry_exhausted_connection_error_help(self, runner):
        from playwright.sync_api import Error as PlaywrightError

        def goto_side(url, **kwargs):
            raise PlaywrightError(
                "Page.goto: net::ERR_CONNECTION_CLOSED at https://notebooklm.google.com/"
            )

        result, _ = _drive_login(runner, goto_side=goto_side)
        assert result.exit_code == 1
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "Connection interrupted (attempt 1/3). Retrying in 1s...\n"
            "Connection interrupted (attempt 2/3). Retrying in 2s...\n"
            "Failed to connect to NotebookLM after multiple retries.\n"
            "This may be caused by:\n"
            "  • Network connectivity issues\n"
            "  • Firewall or VPN blocking notebooklm.google.com\n"
            "  • Corporate proxy interfering with the connection\n"
            "  • Google rate limiting (too many login attempts)\n"
            "\n"
            "Try:\n"
            "  1. Check your internet connection\n"
            "  2. Disable VPN/proxy temporarily\n"
            "  3. Wait a few minutes before retrying\n"
            "  4. Check if notebooklm.google.com is accessible in your browser\n"
        )

    @pytest.mark.requires_playwright
    def test_target_closed_nav_exhausted_browser_closed_help(self, runner):
        from playwright.sync_api import Error as PlaywrightError

        recovered = MagicMock()
        recovered.url = "https://notebooklm.google.com/"
        recovered.goto.side_effect = PlaywrightError(
            "Target page, context or browser has been closed"
        )

        def goto_side(url, **kwargs):
            raise PlaywrightError("Target page, context or browser has been closed")

        result, _ = _drive_login(runner, goto_side=goto_side, new_page=recovered)
        assert result.exit_code == 1
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "Browser page closed (attempt 1/3). Retrying with fresh page...\n"
            "Browser page closed (attempt 2/3). Retrying with fresh page...\n"
            "The browser window was closed during login.\n"
            "This can happen when switching Google accounts in a persistent browser "
            "session.\n"
            "\n"
            "Try:\n"
            "  1. Run: notebooklm login --fresh\n"
            "  2. Or run: notebooklm auth logout && notebooklm login\n"
        )

    @pytest.mark.requires_playwright
    def test_wait_for_url_timeout_5min(self, runner):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        result, _ = _drive_login(
            runner,
            page_url="https://accounts.google.com/signin",
            wait_side=PlaywrightTimeout("Timeout 300000ms exceeded"),
        )
        assert result.exit_code == 1
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "\n"
            "Instructions:\n"
            "1. Complete the Google login in the browser window\n"
            "2. Authentication will be saved automatically once login is detected\n"
            "\n"
            "Waiting for login (up to 5 minutes)...\n"
            "Login not detected within 5 minutes.\n"
            "Try again with: notebooklm login\n"
        )

    @pytest.mark.requires_playwright
    def test_wait_for_url_target_closed_browser_closed_help(self, runner):
        from playwright.sync_api import Error as PlaywrightError

        result, _ = _drive_login(
            runner,
            page_url="https://accounts.google.com/signin",
            wait_side=PlaywrightError("Target page, context or browser has been closed"),
        )
        assert result.exit_code == 1
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "\n"
            "Instructions:\n"
            "1. Complete the Google login in the browser window\n"
            "2. Authentication will be saved automatically once login is detected\n"
            "\n"
            "Waiting for login (up to 5 minutes)...\n"
            "The browser window was closed during login.\n"
            "This can happen when switching Google accounts in a persistent browser "
            "session.\n"
            "\n"
            "Try:\n"
            "  1. Run: notebooklm login --fresh\n"
            "  2. Or run: notebooklm auth logout && notebooklm login\n"
        )

    @pytest.mark.requires_playwright
    def test_unexpected_url_after_login_drift(self, runner):
        def wire(page):
            def wait_succeeds(url, **kwargs):
                page.url = "https://notebooklm.google.com/"

            def goto_drifts(url, **kwargs):
                if "notebooklm" in url:
                    page.url = "https://accounts.google.com/AccountChooser"

            page.wait_for_url.side_effect = wait_succeeds
            page.goto.side_effect = goto_drifts

        result, _ = _drive_login(
            runner,
            page_url="https://accounts.google.com/signin",
            wire=wire,
        )
        assert result.exit_code == 1
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "\n"
            "Instructions:\n"
            "1. Complete the Google login in the browser window\n"
            "2. Authentication will be saved automatically once login is detected\n"
            "\n"
            "Waiting for login (up to 5 minutes)...\n"
            "Login detected.\n"
            "Unexpected URL after login: https://accounts.google.com/AccountChooser\n"
            "Authentication may be incomplete. Try: notebooklm login --fresh\n"
        )

    @pytest.mark.requires_playwright
    def test_cookie_forcing_target_closed_recover_then_exit(self, runner):
        """Pins the cookie-forcing recover-then-exit (``:897``/``:898``).

        The stale page's cookie-forcing ``goto`` raises target-closed, a fresh
        page is recovered, and the recovered page's ``goto`` *also* raises
        target-closed — the inner branch surfaces the browser-closed help and
        exits 1.
        """
        from playwright.sync_api import Error as PlaywrightError

        recovered = MagicMock()
        recovered.url = "https://notebooklm.google.com/"
        recovered.goto.side_effect = PlaywrightError(
            "Target page, context or browser has been closed"
        )

        calls = {"n": 0}

        def goto_side(url, **kwargs):
            calls["n"] += 1
            # First goto (initial navigation) succeeds; cookie-forcing goto dies.
            if calls["n"] == 1:
                return None
            raise PlaywrightError("Target page, context or browser has been closed")

        result, _ = _drive_login(runner, goto_side=goto_side, new_page=recovered)
        assert result.exit_code == 1
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "Already logged in.\n"
            "The browser window was closed during login.\n"
            "This can happen when switching Google accounts in a persistent browser "
            "session.\n"
            "\n"
            "Try:\n"
            "  1. Run: notebooklm login --fresh\n"
            "  2. Or run: notebooklm auth logout && notebooklm login\n"
        )

    @pytest.mark.requires_playwright
    def test_recover_page_non_target_closed_reraises_to_unexpected_error(self, runner):
        """A non-target-closed failure inside ``recover_page`` propagates to
        ``handle_errors`` (exit 2 + the generic 'Unexpected error' line).

        Initial navigation hits target-closed → ``recover_page`` is invoked →
        ``context.new_page`` raises a NON-target error → it re-raises.
        """
        from playwright.sync_api import Error as PlaywrightError

        def goto_side(url, **kwargs):
            raise PlaywrightError("Target page, context or browser has been closed")

        result, _ = _drive_login(
            runner,
            goto_side=goto_side,
            new_page_side=PlaywrightError("some other recovery failure"),
        )
        assert result.exit_code == 2
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            "Opening Chromium for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            "Unexpected error: some other recovery failure\n"
            "This may be a bug. Please report at "
            "https://github.com/teng-lin/notebooklm-py/issues\n"
        )

    @pytest.mark.requires_playwright
    @pytest.mark.parametrize(
        ("browser", "label", "install_url"),
        [
            ("chrome", "Google Chrome", "https://www.google.com/chrome"),
            ("msedge", "Microsoft Edge", "https://www.microsoft.com/edge"),
        ],
    )
    def test_channel_browser_not_found(self, runner, browser, label, install_url):
        result, _ = _drive_login(
            runner,
            args=["login", "--browser", browser],
            launch_side=Exception(f"Executable doesn't exist at /{browser}\nFailed to launch"),
        )
        assert result.exit_code == 1
        assert result.output == (
            f"Profile: {_PROFILE_NAME}\n"
            f"Opening {label} for Google login...\n"
            f"Using persistent profile: {_PROFILE}\n"
            f"{label} not found.\n"
            f"Install from: {install_url}\n"
            "Or use the default Chromium browser: notebooklm login\n"
        )


# ---------------------------------------------------------------------------
# auth refresh — repair_playwright_account_metadata render (success / quiet /
# ambiguous-clear / exception-clear), driven at the command boundary.
# ---------------------------------------------------------------------------


class TestAuthRefreshRepair:
    def test_repair_success_writes_account_line(self, runner):
        from notebooklm.auth import Account

        async def _enum(*args, **kwargs):
            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        result = _drive_refresh(runner, enumerate_accounts=_enum, args=["auth", "refresh"])
        assert result.exit_code == 0
        assert result.output == (
            f"Identifying Google account...\nAccount: alice@example.com\nok refreshed: {_STORAGE}\n"
        )

    def test_repair_quiet_silences_all_output(self, runner):
        from notebooklm.auth import Account

        async def _enum(*args, **kwargs):
            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        result = _drive_refresh(
            runner, enumerate_accounts=_enum, args=["auth", "refresh", "--quiet"]
        )
        assert result.exit_code == 0
        assert result.output == ""

    def test_repair_ambiguous_clears_metadata_with_warning(self, runner):
        from notebooklm.auth import Account

        async def _enum(*args, **kwargs):
            return [
                Account(authuser=0, email="a@example.com", is_default=True),
                Account(authuser=1, email="b@example.com", is_default=False),
            ]

        result = _drive_refresh(runner, enumerate_accounts=_enum, args=["auth", "refresh"])
        assert result.exit_code == 0
        assert result.output == (
            "Identifying Google account...\n"
            "Warning: account metadata was not written; multiple Google accounts "
            "were \n"
            "discovered but the active page email was unavailable. Run notebooklm "
            "auth \n"
            "inspect --browser chrome -v or notebooklm login --browser-cookies "
            "chrome \n"
            "--account EMAIL.\n"
            f"ok refreshed: {_STORAGE}\n"
        )

    def test_repair_exception_clears_metadata_with_warning(self, runner):
        async def _enum(*args, **kwargs):
            raise RuntimeError("network down")

        result = _drive_refresh(runner, enumerate_accounts=_enum, args=["auth", "refresh"])
        assert result.exit_code == 0
        assert result.output == (
            "Identifying Google account...\n"
            "Warning: account metadata was not written. NotebookLM auth still "
            "saved, but \n"
            "multi-account routing may fall back to authuser=0. Run notebooklm auth "
            "inspect \n"
            "--browser chrome -v or notebooklm login --browser-cookies chrome "
            "--account \n"
            "EMAIL. Details: network down\n"
            f"ok refreshed: {_STORAGE}\n"
        )
