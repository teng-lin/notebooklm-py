"""Top-level browser-account discovery + cookie-reading dispatchers.

Both ``_enumerate_browser_accounts`` and ``_read_browser_cookies`` live
here because both are dispatchers that pick the chromium-family vs
firefox-family vs legacy path.

Imports from :mod:`.chromium_accounts`, :mod:`.firefox_accounts`,
:mod:`.cookie_jar` (``_enumerate_one_jar`` + the
``_ROOKIEPY_BROWSER_ALIASES`` map), :mod:`.rookiepy_errors`, and
:mod:`.cookie_domains` (the "auto" + named-alias branch of
``_read_browser_cookies`` builds its own domain list).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...error_handler import exit_with_code
from ...rendering import console
from .chromium_accounts import (
    _chromium_profiles_module,
    _enumerate_chromium_profiles_fanout,
    _read_chromium_profile_cookies_from_selector,
    _split_chromium_profile_browser_spec,
)
from .cookie_domains import _build_google_cookie_domains
from .cookie_jar import _ROOKIEPY_BROWSER_ALIASES, _enumerate_one_jar
from .firefox_accounts import (
    _maybe_warn_firefox_containers_in_use,
    _read_firefox_container_cookies,
)
from .rookiepy_errors import _handle_rookiepy_error

if TYPE_CHECKING:
    from ....auth import Account


def _enumerate_browser_accounts(
    browser_name: str,
    *,
    verbose: bool = True,
    include_domains: set[str] | None = None,
) -> tuple[dict[str | None, list[dict[str, Any]]], list[Account]]:
    """Read cookies from ``browser_name`` and discover signed-in accounts.

    For chromium-family browsers with multiple populated user-data profiles
    (``Default`` plus ``Profile 1``, ``Profile 2``, …), fans out across every
    profile and aggregates the discovered accounts, deduping by email.
    ``chrome::<profile-name-or-directory>`` scopes discovery to one profile.

    For non-chromium browsers, single-profile chromium installs, and the
    legacy path, falls back to a single rookiepy call — preserving every
    existing test mock and runtime behavior.

    Args:
        browser_name: rookiepy browser alias.
        verbose: Forwarded to :func:`_read_browser_cookies` to suppress the
            human-readable progress line in JSON-output paths.
        include_domains: Forwarded to :func:`_read_browser_cookies` to
            broaden the extraction set with sibling-product cookies. See
            :func:`_parse_include_domains`.

    Returns:
        ``(per_profile_cookies, accounts)``:

        * ``per_profile_cookies`` — dict keyed by :attr:`Account.browser_profile`
          (e.g. ``"Default"``, ``"Profile 1"``) mapping to the raw rookiepy
          cookies that yielded that profile's accounts. The legacy / single-jar
          path uses ``None`` as the key.
        * ``accounts`` — :class:`notebooklm.auth.Account` records, each tagged
          with the originating ``browser_profile``, deduped by email (first
          occurrence wins; later duplicates are dropped with a warning).

    Raises:
        SystemExit: On rookiepy failure, missing required cookies, or
            ``authuser=0`` not returning a signed-in account from every probed
            profile.
    """
    chromium_profiles = _chromium_profiles_module()

    scoped_chromium = _split_chromium_profile_browser_spec(browser_name)
    if scoped_chromium is not None:
        scoped_browser, profile_selector = scoped_chromium
        profile, raw_cookies = _read_chromium_profile_cookies_from_selector(
            scoped_browser,
            profile_selector,
            verbose=verbose,
            include_domains=include_domains,
        )
        accounts = _enumerate_one_jar(
            raw_cookies,
            profile.browser,
            browser_profile=profile.directory_name,
        )
        return {profile.directory_name: raw_cookies}, accounts

    # Chromium multi-profile fan-out — only kicks in when discovery surfaces
    # >1 populated profile. Single-profile installs and non-chromium browsers
    # take the legacy path below so all existing rookiepy mocks keep working.
    if chromium_profiles.is_chromium_browser(browser_name):
        profiles = chromium_profiles.discover_chromium_profiles(browser_name)
        if len(profiles) > 1:
            return _enumerate_chromium_profiles_fanout(
                browser_name,
                profiles,
                verbose=verbose,
                include_domains=include_domains,
            )

    raw_cookies = _read_browser_cookies(
        browser_name, verbose=verbose, include_domains=include_domains
    )
    accounts = _enumerate_one_jar(raw_cookies, browser_name, browser_profile=None)
    return {None: raw_cookies}, accounts


def _read_browser_cookies(
    browser_name: str,
    *,
    verbose: bool = True,
    include_domains: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Load Google cookies from an installed browser via rookiepy.

    Wraps rookiepy import + dispatch + error handling so multiple commands
    (``login --browser-cookies``, ``auth inspect``) share one code path.

    Args:
        browser_name: ``"auto"`` to use ``rookiepy.load()``, a specific
            browser alias from :data:`_ROOKIEPY_BROWSER_ALIASES`, or
            ``"chrome::<profile-name-or-directory>"`` for a single Chromium
            user-data profile, or
            ``"firefox::<container-name>"`` (or ``"firefox::none"``) to
            extract from a single Firefox Multi-Account Container, bypassing
            rookiepy entirely.
        verbose: When False, suppress the "Reading cookies from …" progress
            line. Used by ``auth inspect --json`` to keep stdout pure JSON.
        include_domains: Optional set of ``--include-domains`` labels
            (output of :func:`_parse_include_domains`) that broaden the
            extraction set with sibling-product cookies. ``None`` (the
            default) keeps the extraction tight to
            :data:`REQUIRED_COOKIE_DOMAINS`.

    Returns:
        Raw cookie dicts as returned by rookiepy (or by the Firefox
        container extractor, which mirrors rookiepy's shape).

    Raises:
        SystemExit: With a user-friendly message printed to console on any
            rookiepy import / dispatch / read failure.
    """
    # Firefox container syntax: ``firefox::<name>`` or ``firefox::none``.
    # Routed to a direct sqlite3 reader because rookiepy does not honor
    # ``originAttributes`` — see issue #367.
    if browser_name.lower().startswith("firefox::"):
        container_spec = browser_name.split("::", 1)[1].strip()
        if not container_spec:
            # Empty spec would silently fall through to an unfiltered SELECT —
            # i.e. the merged-jar bug this feature exists to prevent. Reject.
            console.print(
                "[red]Empty Firefox container specifier in --browser-cookies.[/red]\n"
                "Use [cyan]firefox::<container-name>[/cyan] (e.g. 'firefox::Work') or "
                "[cyan]firefox::none[/cyan] for the no-container default."
            )
            exit_with_code(1)
        return _read_firefox_container_cookies(
            container_spec, verbose=verbose, include_domains=include_domains
        )

    scoped_chromium = _split_chromium_profile_browser_spec(browser_name)
    if scoped_chromium is not None:
        scoped_browser, profile_selector = scoped_chromium
        _, cookies = _read_chromium_profile_cookies_from_selector(
            scoped_browser,
            profile_selector,
            verbose=verbose,
            include_domains=include_domains,
        )
        return cookies

    try:
        import rookiepy
    except ImportError:
        console.print(
            "[red]rookiepy is not installed.[/red]\n"
            "Install it with:\n"
            "  pip install 'notebooklm-py[cookies]'\n"
            "or directly:\n"
            "  pip install rookiepy"
        )
        exit_with_code(1)

    domains = _build_google_cookie_domains(include_domains=include_domains)

    if browser_name == "auto":
        if verbose:
            console.print(
                "[yellow]Reading cookies from installed browser (auto-detect)...[/yellow]"
            )
        try:
            return rookiepy.load(domains=domains)
        except (OSError, RuntimeError) as e:
            _handle_rookiepy_error(e, "auto-detect")
            exit_with_code(1)

    canonical = _ROOKIEPY_BROWSER_ALIASES.get(browser_name.lower())
    if canonical is None:
        console.print(
            f"[red]Unknown browser: '{browser_name}'[/red]\n"
            f"Supported: {', '.join(sorted(_ROOKIEPY_BROWSER_ALIASES))}"
        )
        exit_with_code(1)
    if verbose:
        console.print(f"[yellow]Reading cookies from {browser_name}...[/yellow]")
    browser_fn = getattr(rookiepy, canonical, None)
    if browser_fn is None or not callable(browser_fn):
        console.print(
            f"[red]rookiepy does not support '{canonical}' on this platform.[/red]\n"
            "Check that rookiepy is properly installed: pip install rookiepy"
        )
        exit_with_code(1)
    try:
        cookies = browser_fn(domains=domains)
    except (OSError, RuntimeError) as e:
        _handle_rookiepy_error(e, browser_name)
        exit_with_code(1)

    # Back-compat warning: unscoped 'firefox' silently merges cookies from
    # every Multi-Account Container. Skip when ``verbose=False`` so callers
    # like ``auth inspect --json`` don't pollute stdout before their JSON.
    if canonical == "firefox" and verbose:
        _maybe_warn_firefox_containers_in_use()

    return cookies
