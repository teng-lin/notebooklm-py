"""Top-level browser-account discovery + cookie-reading dispatchers.

Both ``_enumerate_browser_accounts`` and ``_read_browser_cookies`` live
here because both are dispatchers that pick the chromium-family vs
firefox-family vs legacy path.

Failure shape: both helpers return either their normal success value
OR a :class:`.outcomes.BrowserCookieOutcome` subclass on failure.
Callers (the auth-inspect command, the ``login --browser-cookies``
refresh driver) dispatch on the outcome. The boundary test keeps this
module in :data:`GUARDED_PATHS` — no presentation reach-in, no exit
policy. The transitional helpers in :mod:`.chromium_accounts` and
:mod:`.firefox_accounts` (still owning presentation + exit policy per
their own ``TRANSITIONAL_GUARDED_PATHS`` entries) are wrapped here so
the caller sees a uniform outcome shape on the auth-inspect path.

Imports from :mod:`.chromium_accounts`, :mod:`.firefox_accounts`,
:mod:`.cookie_jar` (``_enumerate_one_jar`` + the
``_ROOKIE_COOKIES_BROWSER_ALIASES`` map), :mod:`.rookie_cookies_errors`, and
:mod:`.cookie_domains` (the "auto" + named-alias branch of
``_read_browser_cookies`` builds its own domain list).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .chromium_accounts import (
    _chromium_profiles_module,
    _enumerate_chromium_profiles_fanout,
    _read_chromium_profile_cookies_from_selector,
    _split_chromium_profile_browser_spec,
)
from .cookie_domains import _build_google_cookie_domains
from .cookie_jar import _ROOKIE_COOKIES_BROWSER_ALIASES, _enumerate_one_jar
from .firefox_accounts import (
    _maybe_warn_firefox_containers_in_use,
    _read_firefox_container_cookies,
)
from .io_seam import resolve_login_io
from .outcomes import (
    BrowserCookieOutcome,
    CookieValidationFailure,
    UnknownBrowser,
    UnsupportedBrowser,
)
from .rookie_cookies_errors import _handle_rookie_cookies_error

if TYPE_CHECKING:
    from ...._app.login_cookie import Account
    from .io_seam import LoginIO


def _emit_progress(io: LoginIO, message: str) -> None:
    """Emit a verbose-mode progress line through the caller-injected sink.

    Routes the "Reading cookies from ..." status line (byte-for-byte
    unchanged) through ``io.emit`` so this dispatcher never imports the
    command layer's ``...rendering`` module (ADR-0008 level-3 boundary,
    #1393). The command layer supplies the concrete ``console.print`` sink.
    """
    io.emit(message)


def _enumerate_browser_accounts(
    browser_name: str,
    *,
    verbose: bool = True,
    include_domains: set[str] | None = None,
    validate_before_probe: bool = True,
    io: LoginIO | None = None,
) -> tuple[dict[str | None, list[dict[str, Any]]], list[Account]] | BrowserCookieOutcome:
    """Read cookies from ``browser_name`` and discover signed-in accounts.

    For chromium-family browsers with multiple populated user-data profiles
    (``Default`` plus ``Profile 1``, ``Profile 2``, …), fans out across every
    profile and aggregates the discovered accounts, deduping by email.
    ``chrome::<profile-name-or-directory>`` scopes discovery to one profile.

    For non-chromium browsers, single-profile chromium installs, and the
    legacy path, falls back to a single rookie-cookies call — preserving every
    existing test mock and runtime behavior.

    Args:
        browser_name: rookie-cookies browser alias.
        verbose: Forwarded to :func:`_read_browser_cookies` to suppress the
            human-readable progress line in JSON-output paths.
        include_domains: Forwarded to :func:`_read_browser_cookies` to
            broaden the extraction set with sibling-product cookies. See
            :func:`_parse_include_domains`.
        validate_before_probe: Run route-aware cookie validation before the
            account probe when true. ``auth inspect`` disables this so a
            transport failure keeps its historical precedence.
        io: Optional caller-injected :class:`.io_seam.LoginIO` sink (resolved
            to the command-layer default when ``None``) threaded to the
            chromium / firefox readers for their verbose progress lines.

    Returns:
        On success — ``(per_profile_cookies, accounts)``:

        * ``per_profile_cookies`` — dict keyed by :attr:`Account.browser_profile`
          (e.g. ``"Default"``, ``"Profile 1"``) mapping to the raw rookie-cookies
          cookies that yielded that profile's accounts. The legacy / single-jar
          path uses ``None`` as the key.
        * ``accounts`` — :class:`notebooklm.auth.Account` records, each tagged
          with the originating ``browser_profile``, deduped by email (first
          occurrence wins; later duplicates are dropped with a warning).

        On failure — a :class:`.outcomes.BrowserCookieOutcome` subclass.
        Every path now returns an outcome on failure (the Chromium-profile
        fan-out, the scoped-Chromium reader, the legacy single-jar path, and
        the unknown-browser dispatch); the command layer — or
        ``refresh._exit_on_outcome`` — renders ``outcome.message`` and exits.
    """
    resolved_io = chromium_profiles = scoped_chromium = scoped_browser = None
    profile_selector = scoped_result = profile = raw_cookies = result = profiles = None
    cookies_result = enum_result = read_scoped = enumerate_jar = fanout = read_browser = None
    try:
        resolved_io = resolve_login_io(io)
        chromium_profiles = _chromium_profiles_module()
        read_scoped = _read_chromium_profile_cookies_from_selector
        enumerate_jar = _enumerate_one_jar
        fanout = _enumerate_chromium_profiles_fanout
        read_browser = _read_browser_cookies

        scoped_chromium = _split_chromium_profile_browser_spec(browser_name)
        if scoped_chromium is not None:
            scoped_browser, profile_selector = scoped_chromium
            scoped_result = read_scoped(
                resolved_io,
                scoped_browser,
                profile_selector,
                verbose=verbose,
                include_domains=include_domains,
            )
            if isinstance(scoped_result, BrowserCookieOutcome):
                return scoped_result
            profile, raw_cookies = scoped_result
            result = enumerate_jar(
                raw_cookies,
                profile.browser,
                browser_profile=profile.directory_name,
                validate_before_probe=validate_before_probe,
                io=resolved_io,
            )
            if isinstance(result, BrowserCookieOutcome):
                return result
            return {profile.directory_name: raw_cookies}, result

        if chromium_profiles.is_chromium_browser(browser_name):
            profiles = chromium_profiles.discover_chromium_profiles(browser_name)
            if len(profiles) > 1:
                return fanout(
                    resolved_io,
                    browser_name,
                    profiles,
                    verbose=verbose,
                    include_domains=include_domains,
                    validate_before_probe=validate_before_probe,
                )

        cookies_result = read_browser(
            browser_name,
            verbose=verbose,
            include_domains=include_domains,
            io=resolved_io,
        )
        if isinstance(cookies_result, BrowserCookieOutcome):
            return cookies_result
        enum_result = enumerate_jar(
            cookies_result,
            browser_name,
            browser_profile=None,
            validate_before_probe=validate_before_probe,
            io=resolved_io,
        )
        if isinstance(enum_result, BrowserCookieOutcome):
            return enum_result
        return {None: cookies_result}, enum_result
    finally:
        del browser_name, verbose, include_domains, validate_before_probe, io
        del resolved_io, chromium_profiles, scoped_chromium, scoped_browser, profile_selector
        del scoped_result, profile, raw_cookies, result, profiles, cookies_result, enum_result
        del read_scoped, enumerate_jar, fanout, read_browser


def _inspect_browser_accounts(
    browser_name: str,
    *,
    verbose: bool = True,
    include_domains: set[str] | None = None,
    io: LoginIO | None = None,
) -> tuple[dict[str | None, list[dict[str, Any]]], list[Account]] | BrowserCookieOutcome:
    """``auth inspect`` variant of :func:`_enumerate_browser_accounts`.

    Identical except that it probes before validating the local cookie set.
    ``auth inspect`` is a diagnostic: when the network is down, "could not
    reach Google" is the honest answer, and reporting a cookie-validation
    failure instead would blame the profile for an unrelated outage. The
    login/refresh paths keep validate-then-probe, so a heal can still repair
    the set before the probe that depends on it.

    Lives here rather than at the command so ``session_cmd`` states which
    flow it wants, not how the flag is spelled.
    """
    return _enumerate_browser_accounts(
        browser_name,
        verbose=verbose,
        include_domains=include_domains,
        validate_before_probe=False,
        io=io,
    )


def _read_browser_cookies(
    browser_name: str,
    *,
    verbose: bool = True,
    include_domains: set[str] | None = None,
    io: LoginIO | None = None,
) -> list[dict[str, Any]] | BrowserCookieOutcome:
    """Load Google cookies from an installed browser via rookie-cookies.

    Wraps rookie-cookies import + dispatch + error handling so multiple commands
    (``login --browser-cookies``, ``auth inspect``) share one code path.

    Args:
        browser_name: ``"auto"`` to use the automatic browser loader, a specific
            browser alias from :data:`_ROOKIE_COOKIES_BROWSER_ALIASES`, or
            ``"chrome::<profile-name-or-directory>"`` for a single Chromium
            user-data profile, or
            ``"firefox::<container-name>"`` (or ``"firefox::none"``) to
            extract from a single Firefox Multi-Account Container, bypassing
            rookie-cookies entirely.
        verbose: When False, suppress the "Reading cookies from …" progress
            line. Used by ``auth inspect --json`` to keep stdout pure JSON.
        include_domains: Optional set of ``--include-domains`` labels
            (output of :func:`_parse_include_domains`) that broaden the
            extraction set with sibling-product cookies. ``None`` (the
            default) keeps the extraction tight to
            :data:`REQUIRED_COOKIE_DOMAINS`.
        io: Optional caller-injected :class:`.io_seam.LoginIO` sink (resolved
            to the command-layer default when ``None``) threaded to the
            firefox / chromium readers and used for the verbose progress line.

    Returns:
        On success — raw cookie dicts as returned by rookie-cookies (or by the
        Firefox container extractor, which mirrors its shape).

        On failure — a :class:`.outcomes.BrowserCookieOutcome` subclass:
        :class:`.outcomes.UnknownBrowser` (alias not in the rookie-cookies map),
        :class:`.outcomes.UnsupportedBrowser` (rookie-cookies lacks the
        platform-specific function), :class:`.outcomes.CookieValidationFailure`
        (rookie-cookies not installed, empty Firefox container spec, or read
        failure surfaced by :func:`_handle_rookie_cookies_error`).
    """
    io = resolve_login_io(io)
    # Firefox container syntax: ``firefox::<name>`` or ``firefox::none``.
    # Routed to a direct sqlite3 reader because rookie-cookies does not honor
    # ``originAttributes`` — see issue #367.
    if browser_name.lower().startswith("firefox::"):
        container_spec = browser_name.split("::", 1)[1].strip()
        if not container_spec:
            # Empty spec would silently fall through to an unfiltered SELECT —
            # i.e. the merged-jar bug this feature exists to prevent. Reject.
            return CookieValidationFailure(
                code="EMPTY_FIREFOX_CONTAINER",
                message=(
                    "[red]Empty Firefox container specifier in --browser-cookies.[/red]\n"
                    "Use [cyan]firefox::<container-name>[/cyan] (e.g. 'firefox::Work') or "
                    "[cyan]firefox::none[/cyan] for the no-container default."
                ),
            )
        return _read_firefox_container_cookies(
            io, container_spec, verbose=verbose, include_domains=include_domains
        )

    scoped_chromium = _split_chromium_profile_browser_spec(browser_name)
    if scoped_chromium is not None:
        scoped_browser, profile_selector = scoped_chromium
        scoped_result = _read_chromium_profile_cookies_from_selector(
            io,
            scoped_browser,
            profile_selector,
            verbose=verbose,
            include_domains=include_domains,
        )
        if isinstance(scoped_result, BrowserCookieOutcome):
            return scoped_result
        _, cookies = scoped_result
        return cookies

    canonical: str | None = None
    if browser_name != "auto":
        canonical = _ROOKIE_COOKIES_BROWSER_ALIASES.get(browser_name.lower())
        if canonical is None:
            supported = tuple(sorted(_ROOKIE_COOKIES_BROWSER_ALIASES))
            return UnknownBrowser(
                code="UNKNOWN_BROWSER",
                message=(
                    f"[red]Unknown browser: '{browser_name}'[/red]\n"
                    f"Supported: {', '.join(supported)}"
                ),
                name=browser_name,
                supported=supported,
            )

    try:
        import rookie_cookies
    except ImportError:
        return CookieValidationFailure(
            code="ROOKIEPY_NOT_INSTALLED",
            message=(
                "[red]rookie-cookies is not installed.[/red]\n"
                "Install it with:\n"
                "  pip install 'notebooklm-py[cookies]'\n"
                "or directly:\n"
                "  pip install rookie-cookies"
            ),
        )

    domains = _build_google_cookie_domains(include_domains=include_domains)

    if browser_name == "auto":
        if verbose:
            _emit_progress(
                io,
                "[yellow]Reading cookies from installed browser (auto-detect)...[/yellow]",
            )
        try:
            return rookie_cookies.load(domains=domains)
        except (OSError, RuntimeError) as e:
            return CookieValidationFailure(
                code="COOKIE_READ_FAILED",
                message=_handle_rookie_cookies_error(e, "auto-detect"),
            )

    assert canonical is not None
    if verbose:
        _emit_progress(io, f"[yellow]Reading cookies from {browser_name}...[/yellow]")
    browser_fn = getattr(rookie_cookies, canonical, None)
    if browser_fn is None or not callable(browser_fn):
        return UnsupportedBrowser(
            code="UNSUPPORTED_BROWSER",
            message=(
                f"[red]rookie-cookies does not support '{canonical}' on this platform.[/red]\n"
                "Check that rookie-cookies is properly installed: pip install rookie-cookies"
            ),
            name=canonical,
        )
    try:
        cookies = browser_fn(domains=domains)
    except (OSError, RuntimeError) as e:
        return CookieValidationFailure(
            code="COOKIE_READ_FAILED",
            message=_handle_rookie_cookies_error(e, browser_name),
        )

    # Back-compat warning: unscoped 'firefox' silently merges cookies from
    # every Multi-Account Container. Skip when ``verbose=False`` so callers
    # like ``auth inspect --json`` don't pollute stdout before their JSON.
    if canonical == "firefox" and verbose:
        _maybe_warn_firefox_containers_in_use(io)

    return cookies
