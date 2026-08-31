"""Cookie-domain policy and required-cookie validation for authentication."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any, Literal

from notebooklm._env import (
    PERSONAL_APP_HOSTS,
    PERSONAL_BASE_HOST,
    PERSONAL_LEGACY_HOST,
    get_base_host,
)

logger = logging.getLogger("notebooklm.auth")

# Closed enum of reasons a pure loader may reject a cookie set. Kept small and
# stable so wrapper recovery composition can branch on ``exc.reason`` instead of
# scraping the human-readable message:
#
# * ``"missing_cookie"`` — a Tier-1 required cookie (``SID`` / ``__Secure-1PSIDTS``)
#   is absent by name. No RotateCookies POST can conjure a session that never
#   had these; recovery declines and the error stands.
# * ``"psidts_unroutable"`` — ``__Secure-1PSIDTS`` is present by name but would
#   not be sent to the ``RotateCookies`` URL (absent from the rotate host,
#   expired, or scoped to a domain that never reaches ``accounts.google.com``).
#   This is the one condition the inline recovery POST exists to heal (#2061).
RequiredCookieReason = Literal["missing_cookie", "psidts_unroutable"]


class RequiredCookieValidationError(ValueError):
    """Typed required-cookie/preflight failure used by recovery wrappers.

    ``reason`` carries a :data:`RequiredCookieReason` closed-enum tag so the
    public recovery wrappers can compose on a stable signal rather than the
    diagnostic message text. It is ``None`` only for errors raised outside the
    two known validation sites.
    """

    def __init__(self, *args: object, reason: RequiredCookieReason | None = None) -> None:
        super().__init__(*args)
        self.reason: RequiredCookieReason | None = reason


def cookie_names_from_storage(storage_state: Mapping[str, Any]) -> set[str]:
    """Return the set of cookie names present in a Playwright storage_state.

    Centralizes the ``{entry["name"] for entry in storage_state["cookies"]}``
    pattern that the CLI extraction paths use to feed
    :func:`missing_cookies_hint` after a failed extraction. Defensive against
    non-dict entries (rookiepy can return malformed rows), missing keys, and
    ``None`` / empty-string names (so the returned set never contains ``""``).
    """
    cookies = storage_state.get("cookies", [])
    names: set[str] = set()
    if not isinstance(cookies, list):
        return names
    for entry in cookies:
        # This helper is deliberately a lightweight diagnostic projection, not
        # a loader/conversion boundary.  Doctor and compatibility callers have
        # historically supplied minimal ``{"name": ..., "value": ...}``
        # rows, so requiring storage-only fields such as ``domain`` here would
        # turn a usable profile into a false "SID missing" report.  Full shape
        # and expiry sanitization remains in the cookie loaders.
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


# Tier 1: cookies whose absence Google rejects deterministically.
#
# - ``SID``: only individually-required cookie (singleton ablation).
# - ``__Secure-1PSIDTS``: directly accepted by Google's homepage check, OR
#   recoverable via the RotateCookies POST when other auth cookies are intact.
#   When neither path is viable the homepage GET 302s to login.
#
# See ``docs/auth-cookie-lifecycle.md`` §3.3 for the singleton/pair-wise Tier 1
# evidence and the corrected three-way secondary-binding table.
MINIMUM_REQUIRED_COOKIES = {"SID", "__Secure-1PSIDTS"}

# Rows whose pre-POST value PSIDTS recovery observes for the compare-and-set
# that decides whether a rotated row may replace a stored one.  PSIDTS only:
# these are the cookies the gate reasons about as present/absent/unusable.
#
# One definition, two consumers (ADR-0033 / PR 0.2): ``psidts_recovery``
# (which observes the rows before the RotateCookies POST) and ``storage``
# (whose ``_merge_recovery_target_rows`` collapses the observed deltas back
# into storage_state).  They previously carried value-equal copies with no
# import between them, so a policy edit had to be made twice to stay correct.
# It lives HERE because ``cookie_policy`` is the pure policy leaf both modules
# already import; the reverse edge (policy importing storage/recovery) would
# reintroduce the cycle this consolidation exists to remove.
#
# NOTE: ``psidts_recovery._ROTATION_MERGE_COOKIE_NAMES`` derives from this set
# and is deliberately WIDER (it adds ``LSID``) — see the rationale at the merge
# loop in ``psidts_recovery.recover_psidts_in_memory`` (#1977).
_RECOVERY_TARGET_COOKIE_NAMES: frozenset[str] = frozenset({"__Secure-1PSIDTS", "__Secure-3PSIDTS"})

# Cookie names whose value/domain/path state is authentication-bearing for a
# live-vs-disk recovery decision. Ambient Google cookies (for example ``NID``)
# may change on every rejected redirect and must not starve a valid profile
# reload; changes to these Tier-1, rotation, or secondary-binding cookies can
# instead represent a newer in-memory session and must not be overwritten by a
# lagging disk sample.
_AUTH_MATERIAL_COOKIE_NAMES: frozenset[str] = frozenset(
    MINIMUM_REQUIRED_COOKIES | _RECOVERY_TARGET_COOKIE_NAMES | {"OSID", "APISID", "SAPISID", "LSID"}
)


_EXTRACTION_HINT = (
    "This typically means --browser-cookies extraction was incomplete "
    "(Chrome 127+ App-Bound Encryption can cause silent partial reads). "
    "Run 'notebooklm login' to re-authenticate."
)

# Tier 2 fires per cookie-load; a single CLI run can hit it 2-3 times across
# the four loader entry points. One warning per process is enough signal.
#
# Dedupe contract: best-effort under threads, exactly-once on a single
# event loop. The check-then-set at the call site (``_validate_required_cookies``
# below) reads ``_SECONDARY_BINDING_WARNED`` and sets it to ``True`` in a single
# synchronous block with no intervening ``await``. The asyncio scheduler can
# only switch coroutines at ``await`` points, so concurrent coroutines on one
# loop cannot interleave between the check and the set — the warning fires
# exactly once per process. Under genuine OS threads (which this library does
# NOT support per the documented concurrency contract — each client is bound
# to one event loop), the pattern is racy: two threads can both observe
# ``False`` before either has written ``True``, causing a duplicate warning.
# We accept that as best-effort rather than introduce an ``asyncio.Lock``
# (would not help threads) or a ``threading.Lock`` (re-architects for a use
# case we don't support).
#
# Note: ``functools.lru_cache`` and ``logging.LoggerAdapter`` are sometimes
# suggested as drop-in dedupe primitives here. They are NOT: ``lru_cache``
# memoizes return values, not the side-effect of ``logger.warning``;
# ``LoggerAdapter`` only rewrites records, it does not filter duplicates.
_SECONDARY_BINDING_WARNED = False
# The log-once flag is now read-modify-written from worker threads (the pure
# loaders run under ``asyncio.to_thread``), so guard it with a sync lock — same
# pattern as keepalive's ``_POKE_STATE_LOCK`` — or the unlocked check-then-set
# races and logs the warning 2-3x under concurrent loads.
_SECONDARY_BINDING_WARN_LOCK = threading.Lock()


def _has_valid_secondary_binding(cookie_names: set[str]) -> bool:
    """Tier 2 acceptance check (see ``MINIMUM_REQUIRED_COOKIES``).

    The homepage GET requires *at least one* of two secondary-binding paths in
    addition to Tier 1:

    - ``OSID`` (recent-sign-in binding), OR
    - ``APISID`` AND ``SAPISID`` (legacy XSSI pair) **AND** bare ``LSID``.

    Without one of those, Google 302s to ``accounts.google.com/v3/signin`` even
    when ``SID`` and ``__Secure-1PSIDTS`` are present and otherwise valid.

    The ``LSID`` conjunct is the correction. The original pair-wise ablation
    only varied ``OSID`` and the XSSI pair; because ``APISID``/``SAPISID`` are
    ``.google.com``-scoped they survived every domain filter, so the XSSI branch
    was never tested *without* them and the ``LSID`` dependency stayed hidden.
    A three-way ablation, replicated on two unrelated accounts (2026-08-04,
    issue #1977), gives:

    ==========  =====================  ============  ========
    ``OSID``    ``APISID``+``SAPISID``  bare ``LSID``  result
    ==========  =====================  ============  ========
    present     --                     --            works
    present     present                --            works
    --          present                present       works
    --          present                --            **fails**
    --          --                     present       fails
    ==========  =====================  ============  ========

    Row 4 is what this function used to get wrong. Two consequences worth
    keeping straight:

    * ``OSID`` alone is sufficient — a profile with **no** ``LSID`` anywhere
      authenticates (row 1, verified with every ``accounts.google.com`` cookie
      stripped). ``LSID`` is required *only* when ``OSID`` is absent.
    * ``__Host-1PLSID`` / ``__Host-3PLSID`` do **not** substitute for bare
      ``LSID``; row 4 retains them and still fails.

    Deliberately domain-blind, and that is not the #2054 mistake. ``LSID`` is
    ``accounts.google.com``-scoped and never routes to the app host, while
    ``OSID`` is app-host-scoped — the binding spans hosts because the auth flow
    does (app-host GET, redirect through accounts, back). A check restricted to
    what routes to the *target* URL would reject working profiles. Host-aware,
    were it ever needed, would mean "``OSID`` routable to the app host, or the
    XSSI pair routable there plus ``LSID`` present for the accounts hop" — not a
    single routed header.
    """
    if "OSID" in cookie_names:
        return True
    return {"APISID", "SAPISID", "LSID"} <= cookie_names


def _has_rotatable_secondary_binding(cookie_names: set[str]) -> bool:
    """Whether a ``RotateCookies`` attempt is worth making — deliberately weaker.

    This is **not** :func:`_has_valid_secondary_binding` and must not be
    collapsed into it. That one answers "will the homepage GET succeed with
    these cookies *as they stand*". This one answers "is this set intact enough
    that rotating ``__Secure-1PSIDTS`` could plausibly produce a working
    session" — a precondition for attempting recovery, not for succeeding.

    The difference is the ``LSID`` conjunct. Rotation POSTs to
    ``accounts.google.com``; whether that hop can itself re-establish the
    accounts-side session (and so supply the missing ``LSID``) has not been
    ablated. Requiring the *post*-recovery condition before *attempting*
    recovery would gate off the rotation that might satisfy it — checking the
    destination as a precondition for the journey. That is the failure shape of
    #2061, where an over-strict heal check never converged.

    So this keeps the pre-#1977 rule until someone ablates the rotation hop. Its
    cost when wrong is one wasted POST; the strict version's cost when wrong is
    a recoverable session left unrecovered.
    """
    if "OSID" in cookie_names:
        return True
    return {"APISID", "SAPISID"} <= cookie_names


def _validate_required_cookies(
    cookie_names: set[str],
    *,
    context: str = "",
    extra_diagnostics: list[str] | None = None,
) -> None:
    """Enforce the Tier 1 cookie-set rule (raise) and warn on Tier 2 violation.

    Hybrid rollout: Tier 1 (``MINIMUM_REQUIRED_COOKIES``) is a hard validator
    failure because callers that reach this function without a recovery wrapper
    must not proceed with an unusable cookie set. The dedicated PSIDTS recovery
    paths catch the recoverable missing/expired-PSIDTS case before retrying
    validation; unrecoverable Tier-1 failures still raise here. Tier 2
    (secondary binding, see ``_has_valid_secondary_binding``) is logged as a
    warning so partial extractions surface in user logs without breaking
    edge-case auth flows we have not ablated yet (e.g. Workspace SSO). After one
    release of telemetry this can be promoted to a hard raise.

    Args:
        cookie_names: Names of cookies present in the loaded set (any domain).
        context: Optional suffix for the Tier 1 error message
            (e.g. ``" for downloads"``).
        extra_diagnostics: Optional extra lines inserted into the Tier 1 error
            (e.g. observed cookies, source domains) for friendlier diagnosis.
    """
    missing = MINIMUM_REQUIRED_COOKIES - cookie_names
    if missing:
        missing_names = ", ".join(sorted(missing))
        parts = [f"Missing required cookies{context}: {missing_names}"]
        if extra_diagnostics:
            parts.extend(extra_diagnostics)
        parts.append(_EXTRACTION_HINT)
        raise RequiredCookieValidationError("\n".join(parts), reason="missing_cookie")

    if not _has_valid_secondary_binding(cookie_names):
        global _SECONDARY_BINDING_WARNED
        with _SECONDARY_BINDING_WARN_LOCK:
            should_warn = not _SECONDARY_BINDING_WARNED
            _SECONDARY_BINDING_WARNED = True
        if should_warn:
            logger.warning(
                "Cookie set lacks a secondary binding (need OSID, or all three of "
                "APISID, SAPISID and LSID). Google may reject auth on the next "
                "call. %s",
                _EXTRACTION_HINT,
            )


def app_host_scope_note() -> str:
    """Return the both-hosts caveat to append to "open the app in your browser" advice.

    Every hint that tells a user to open the NotebookLM app in their browser is
    really telling them to *mint the per-product binding cookie* (``OSID`` /
    ``__Secure-OSID``). Google now serves the personal app from two hosts and
    redirects between them, and those binding cookies are **host-scoped**: a
    cookie set on one host is never sent to the other. So the browser visit can
    succeed and still leave the configured host without a binding — which is the
    #2019 shape all over again (a working session read as an expired one).

    Naming a different URL in the advice does not fix that: the redirect happens
    either way. What the user needs is the *outcome* named — which host the
    cookies landed on, which host this client is talking to — plus a recovery
    that works. Hence this note, appended to the binding-related hints.

    Both recoveries are real, and ordered deliberately:

    1. Re-run ``notebooklm login`` and complete the sign-in. That re-mints the
       account-wide cookies. ``APISID`` + ``SAPISID`` live on ``.google.com``
       and ``LSID`` on ``accounts.google.com``, so none of them are host-scoped
       and together they satisfy the binding on *either* host — see
       :func:`_has_valid_secondary_binding`.
    2. Select the host that actually holds the cookies via
       ``NOTEBOOKLM_BASE_URL``.

    Recovery 2 used to carry a caveat marking the rebrand host "experimental —
    not the documented default", asymmetrically so it could never land on the
    legacy host and warn users off the one fallback this project had exercised
    end to end.

    **#2067 retired that caveat rather than inverting it.** The flip made the
    rebrand host the documented default and moved the cassettes onto it, so
    every clause of the old wording became false for it. Re-pointing the same
    words at the legacy host would have been false in the other direction: that
    host is long-exercised, still served, and is now the documented rollback
    lever (ADR-0028, as amended). Neither host is experimental today, so the
    note recommends the sibling without an editorial. Overclaiming in either
    direction sends users into a different failure, which is the bug this note
    exists to avoid.

    Returns:
        The note as plain text (no trailing newline), or ``""`` when the
        configured host has no sibling — i.e. the enterprise host, which has no
        alias, so there is no cross-host scope to warn about.
    """
    base_host = get_base_host()
    siblings = sorted(PERSONAL_APP_HOSTS - {base_host})
    if base_host not in PERSONAL_APP_HOSTS or not siblings:
        return ""
    other_host = siblings[0]
    return (
        f"Heads-up: Google serves the personal app from both {base_host} and "
        f"{other_host} and redirects between them, but the OSID binding is "
        f"host-scoped — a cookie set on {other_host} is never sent to {base_host}, "
        f"which is the host this client is configured to use.\n"
        f"If the binding is still missing afterwards it landed on {other_host}: "
        f"re-run 'notebooklm login' and complete the sign-in (that re-mints the "
        f"account-wide binding APISID+SAPISID+LSID, none of which are "
        f"host-scoped, so both hosts accept it), or select the host that has "
        f"the cookies with "
        f"NOTEBOOKLM_BASE_URL=https://{other_host}."
    )


def _with_scope_note(hint: str) -> str:
    """Append :func:`app_host_scope_note` to ``hint`` when there is one to append."""
    note = app_host_scope_note()
    return f"{hint}\n{note}" if note else hint


def missing_cookies_hint(
    cookie_names: set[str],
    *,
    browser_label: str | None = None,
) -> str:
    """Return an actionable recovery hint for the missing-cookies failure mode.

    The browser-extraction CLI calls this after a ``ValueError`` from
    :func:`extract_cookies_from_storage` to replace the generic "Make sure you
    are logged into Google in your browser" tail with a scenario-specific
    message. Branches on which Tier-1 / Tier-2 cookies are actually missing.

    Scenarios (issue #990):

    - ``SID`` missing: user is not signed in to Google at all in this browser.
      Recovery is impossible without a fresh login.
    - ``__Secure-1PSIDTS`` missing + secondary binding present: typically a
      cold browser session. The in-memory ``RotateCookies`` recovery should
      have already attempted to mint it; reaching this hint means Google
      declined the POST (4xx / 5xx / withheld the Set-Cookie). Suggest
      visiting NotebookLM in-browser to refresh.
    - ``__Secure-1PSIDTS`` missing + secondary binding missing: ``RotateCookies``
      cannot help because Google rejects requests without the binding cookies.
      User must visit NotebookLM in-browser to populate ``OSID``.
    - Secondary binding missing (Tier-2 warning case): the session works for
      now but is fragile. Visiting NotebookLM populates the missing cookies.

    Every branch names the *configured* host rather than a fixed URL, and the
    two binding-related branches carry :func:`app_host_scope_note` — the browser
    visit they ask for can land the host-scoped binding on the sibling personal
    host, which no re-wording of the URL prevents.

    Args:
        cookie_names: Names of cookies that survived extraction.
        browser_label: Optional browser label for the message
            (``"chrome"``, ``"firefox"``). When omitted, defaults to
            ``"your browser"``.

    Returns:
        A multi-line human-readable hint. The caller is responsible for any
        formatting (rich tags, indentation) — this returns plain text.
    """
    browser_phrase = browser_label or "your browser"

    if "SID" not in cookie_names:
        return (
            f"You are not signed in to Google in {browser_phrase}.\n"
            f"Sign in to a Google account (Gmail, Drive, NotebookLM, ...) "
            f"in {browser_phrase} and re-run this command."
        )

    psidts_missing = "__Secure-1PSIDTS" not in cookie_names
    has_secondary = _has_valid_secondary_binding(cookie_names)

    if psidts_missing and not has_secondary:
        return _with_scope_note(
            f"Your {browser_phrase} session is signed in to Google but is missing "
            f"the cookies NotebookLM needs (OSID, or APISID+SAPISID+LSID, plus "
            f"__Secure-1PSIDTS).\n"
            f"Open https://{get_base_host()} in {browser_phrase} (sign in if "
            f"prompted), reload the page, then re-run this command."
        )

    if psidts_missing:
        # No scope note here: the missing cookie is ``__Secure-1PSIDTS``, which
        # lives on ``.google.com`` and is therefore sent to both personal hosts.
        # Only the host-scoped OSID binding has a cross-host failure mode.
        return (
            f"__Secure-1PSIDTS is missing and the automatic RotateCookies recovery "
            f"did not succeed.\n"
            f"Open https://{get_base_host()} in {browser_phrase} (this triggers "
            f"Google to refresh the cookie), then re-run this command."
        )

    if not has_secondary:
        return _with_scope_note(
            f"Your {browser_phrase} cookies are missing the NotebookLM binding "
            f"(OSID, or all of APISID, SAPISID and LSID).\n"
            f"Open https://{get_base_host()} in {browser_phrase} (sign in if "
            f"prompted), reload the page, then re-run this command."
        )

    return _EXTRACTION_HINT


# Cookie domains we extract / accept by default.
#
# Empirical justification: traced cassettes
# (``tests/cassettes/web/*.yaml``) and the live auth-refresh path. Only the
# following domains are actually exercised during login + token refresh +
# source-add + chat-ask flows:
#   - ``notebook.google.com`` (the default app host since #2067 — CLI RPCs
#     land here, and Google sets the per-product ``OSID`` /
#     ``__Secure-OSID`` binding cookies here)
#   - ``notebooklm.google.com`` (the pre-rebrand host; still served, still
#     selectable via ``NOTEBOOKLM_BASE_URL``, and sets the same binding
#     cookies — both must stay accepted)
#   - ``.google.com`` (carries ``SID``/``HSID``/``SSID``/etc.)
#   - ``accounts.google.com`` (token refresh + ``RotateCookies`` endpoint at
#     :data:`KEEPALIVE_ROTATE_URL`)
#   - ``.googleusercontent.com`` (authenticated media downloads — audio /
#     infographic / slide assets)
#   - ``drive.google.com`` (Drive-source ingest follows redirects through
#     here; kept in REQUIRED for source-add safety)
#
# YouTube / Docs / Mail / myaccount cookies do NOT appear in any traced flow.
# Their hosts are not explicitly requested unless opted in via
# ``notebooklm login --include-domains=...``. The write-time compatibility
# policy still preserves cookies an extractor returns under trusted Google
# roots; distinct roots such as ``youtube.com`` remain opt-in.
#
# ``REQUIRED_COOKIE_DOMAINS`` is included in the default extractor allowlist
# built by ``_build_google_cookie_domains`` / ``build_cookie_domain_allowlist``.
# Those builders also add regional ``.google.<ccTLD>`` variants by default.
#
# This frozenset is the required-domain chokepoint for the cookie-domain
# narrowing control: extraction explicitly requests required domains plus
# regional ccTLDs by default. The runtime and write gates stay
# compatibility-permissive for boundary-matched trusted Google roots because
# browser extractors may return host-scoped subdomain cookies required by an
# untraced flow. Optional domains on distinct roots remain opt-in.
REQUIRED_COOKIE_DOMAINS: frozenset[str] = frozenset(
    {
        ".google.com",
        "google.com",  # Host-only Domain=google.com cookies (rare but possible)
        # Playwright storage_state may preserve the leading dot for NotebookLM cookies.
        f".{PERSONAL_LEGACY_HOST}",
        PERSONAL_LEGACY_HOST,
        # Gemini Notebook rebrand (July 2026, issue #2013): Google now also serves
        # the app from ``notebook.google.com`` and sets the per-product binding
        # cookies (``OSID`` / ``__Secure-OSID``) on this host. Both dotted and
        # non-dotted variants are listed (same defensive pattern as
        # ``notebooklm.google.com`` above) so http.cookiejar normalization does
        # not drop them at extraction / load time.
        f".{PERSONAL_BASE_HOST}",
        PERSONAL_BASE_HOST,
        ".notebooklm.cloud.google.com",
        "notebooklm.cloud.google.com",
        ".googleusercontent.com",
        "accounts.google.com",  # Required for token refresh + RotateCookies
        ".accounts.google.com",  # http.cookiejar may normalize Domain=accounts.google.com
        # Drive-source ingest follows redirects through drive.google.com.
        # Both dotted and non-dotted variants are listed so that
        # http.cookiejar normalization (which can add a leading dot) doesn't
        # drop a cookie at the next extraction; same defensive pattern as
        # accounts.google.com above.
        "drive.google.com",
        ".drive.google.com",
    }
)

# Sibling Google product domains — NOT exercised by any current code path but
# historically requested "for symmetry with a logged-in browser session"
# (issue #360). They are now explicit-request labels for
# ``--include-domains=...``. Cookies already returned under a trusted Google
# root may still survive the compatibility-first write policy; YouTube uses a
# distinct root and remains excluded by default.
#
# Both dotted and non-dotted variants are listed so that http.cookiejar
# normalization (which can add a leading dot) doesn't drop a cookie at the
# next extraction.
OPTIONAL_COOKIE_DOMAINS_BY_LABEL: dict[str, frozenset[str]] = {
    "youtube": frozenset(
        {
            ".youtube.com",
            "youtube.com",
            "accounts.youtube.com",
            ".accounts.youtube.com",
        }
    ),
    "docs": frozenset({"docs.google.com", ".docs.google.com"}),
    "myaccount": frozenset({"myaccount.google.com", ".myaccount.google.com"}),
    "mail": frozenset({"mail.google.com", ".mail.google.com"}),
}

OPTIONAL_COOKIE_DOMAINS: frozenset[str] = frozenset().union(
    *OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values()
)

# Sentinel ``--include-domains`` label meaning "every optional sibling-product
# domain". Lives here (with the domain constants) so both the CLI extractor
# builder and the neutral browser-capture filter share one source of truth.
INCLUDE_DOMAINS_ALL = "all"


def resolve_optional_cookie_domains(labels: set[str]) -> frozenset[str]:
    """Resolve ``--include-domains`` labels to the union of their domain sets.

    ``labels`` is expected to be pre-validated (every entry a key of
    :data:`OPTIONAL_COOKIE_DOMAINS_BY_LABEL`, or the literal
    :data:`INCLUDE_DOMAINS_ALL`). The dict lookup is unguarded by design — a
    ``KeyError`` here would signal a validation bug upstream, not user input.
    """
    if not labels:
        return frozenset()
    if INCLUDE_DOMAINS_ALL in labels:
        return frozenset().union(*OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values())
    selected: set[str] = set()
    for label in labels:
        selected.update(OPTIONAL_COOKIE_DOMAINS_BY_LABEL[label])
    return frozenset(selected)


def build_cookie_domain_allowlist(
    *,
    include_optional: bool = False,
    include_domains: set[str] | None = None,
) -> list[str]:
    """Return the cookie-domain allowlist for the configured opt-in policy.

    Single source of truth for the domain set both the CLI rookiepy/Firefox
    extractors (``rookiepy.load(domains=...)``) and the Playwright
    browser-capture cookie filter consume. Defaults to
    :data:`REQUIRED_COOKIE_DOMAINS` plus every regional ``.google.<ccTLD>``
    variant. Optional sibling hosts are not explicitly requested unless the
    caller opts in via ``include_optional=True`` or a non-empty
    ``include_domains`` label set (``"all"`` = every label).

    Args:
        include_optional: When ``True``, include every optional sibling domain
            (equivalent to ``--include-domains=all``).
        include_domains: Optional-domain labels; each expands via
            :data:`OPTIONAL_COOKIE_DOMAINS_BY_LABEL`. ``"all"`` is a shortcut
            for every label.

    Returns:
        A sorted list of cookie-domain strings. Matching uses set semantics,
        but deterministic order keeps extractor calls and diagnostics stable.
    """
    selected_optional: frozenset[str]
    if include_domains:
        selected_optional = resolve_optional_cookie_domains(include_domains)
    elif include_optional:
        selected_optional = frozenset().union(*OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values())
    else:
        selected_optional = frozenset()

    domains = set(REQUIRED_COOKIE_DOMAINS | selected_optional)
    domains.update(f".google.{cctld}" for cctld in GOOGLE_REGIONAL_CCTLDS)
    return sorted(domains)


# Backward-compatible union — preserves the old constant name so external
# imports keep working. Internal code should prefer ``REQUIRED_*`` /
# ``OPTIONAL_*`` so the security tier is explicit at the call site.
ALLOWED_COOKIE_DOMAINS: frozenset[str] = REQUIRED_COOKIE_DOMAINS | OPTIONAL_COOKIE_DOMAINS

# Regional Google ccTLDs where Google may set auth cookies
# Users in these regions may have SID cookies on regional domains instead of .google.com
# Format: suffix after ".google." (e.g., "com.sg" for ".google.com.sg")
#
# Categories:
# - com.XX: Country-code second-level domains (Singapore, Australia, Brazil, etc.)
# - co.XX: Country domains using .co (UK, Japan, India, Korea, etc.)
# - XX: Single ccTLD countries (Germany, France, Italy, etc.)
GOOGLE_REGIONAL_CCTLDS = frozenset(
    {
        # .google.com.XX pattern (country-code second-level domains)
        "com.sg",  # Singapore
        "com.au",  # Australia
        "com.br",  # Brazil
        "com.mx",  # Mexico
        "com.ar",  # Argentina
        "com.hk",  # Hong Kong
        "com.tw",  # Taiwan
        "com.my",  # Malaysia
        "com.ph",  # Philippines
        "com.vn",  # Vietnam
        "com.pk",  # Pakistan
        "com.bd",  # Bangladesh
        "com.ng",  # Nigeria
        "com.eg",  # Egypt
        "com.tr",  # Turkey
        "com.ua",  # Ukraine
        "com.co",  # Colombia
        "com.pe",  # Peru
        "com.sa",  # Saudi Arabia
        "com.ae",  # UAE
        # .google.co.XX pattern (countries using .co second-level)
        "co.uk",  # United Kingdom
        "co.jp",  # Japan
        "co.in",  # India
        "co.kr",  # South Korea
        "co.za",  # South Africa
        "co.nz",  # New Zealand
        "co.id",  # Indonesia
        "co.th",  # Thailand
        "co.il",  # Israel
        "co.ve",  # Venezuela
        "co.cr",  # Costa Rica
        "co.ke",  # Kenya
        "co.ug",  # Uganda
        "co.tz",  # Tanzania
        "co.ma",  # Morocco
        "co.ao",  # Angola
        "co.mz",  # Mozambique
        "co.zw",  # Zimbabwe
        "co.bw",  # Botswana
        # .google.XX pattern (single ccTLD countries)
        "cn",  # China
        "de",  # Germany
        "fr",  # France
        "it",  # Italy
        "es",  # Spain
        "nl",  # Netherlands
        "pl",  # Poland
        "ru",  # Russia
        "ca",  # Canada
        "be",  # Belgium
        "at",  # Austria
        "ch",  # Switzerland
        "se",  # Sweden
        "no",  # Norway
        "dk",  # Denmark
        "fi",  # Finland
        "pt",  # Portugal
        "gr",  # Greece
        "cz",  # Czech Republic
        "ro",  # Romania
        "hu",  # Hungary
        "ie",  # Ireland
        "sk",  # Slovakia
        "bg",  # Bulgaria
        "hr",  # Croatia
        "si",  # Slovenia
        "lt",  # Lithuania
        "lv",  # Latvia
        "ee",  # Estonia
        "lu",  # Luxembourg
        "cl",  # Chile
        "cat",  # Catalonia (special case - 3 letter)
    }
)

# Compatibility-first roots for cookie domains Google may use during auth,
# Drive ingest, or authenticated downloads.  Keep this derived from the
# existing regional whitelist so a new regional root has one maintenance
# chokepoint.  The boundary-aware matcher below accepts both the root and its
# subdomains (for example ``accounts.google.com.hk``), but never lookalikes
# such as ``evilgoogle.com`` or ``google.com.evil.example``.
_TRUSTED_GOOGLE_COOKIE_ROOTS: frozenset[str] = frozenset(
    {
        "google.com",
        "googleusercontent.com",
        *(f"google.{cctld}" for cctld in GOOGLE_REGIONAL_CCTLDS),
    }
)


def _is_trusted_google_cookie_domain(domain: str) -> bool:
    """Return whether ``domain`` is a trusted Google root or subdomain.

    Cookie domains may carry one leading dot to express domain scope.  Strip
    exactly that one dot, normalize DNS case, then require a label boundary
    before a trusted root.  This deliberately keeps unknown ``*.google.com``
    and regional Google subdomains for compatibility until live-flow evidence
    lets the persisted set be narrowed without breaking authentication.
    """
    normalized = domain[1:] if domain.startswith(".") else domain
    normalized = normalized.lower()
    if not normalized or normalized.startswith(".") or normalized.endswith("."):
        return False
    return any(
        normalized == root or normalized.endswith(f".{root}")
        for root in _TRUSTED_GOOGLE_COOKIE_ROOTS
    )


def _is_google_domain(domain: str) -> bool:
    """Check if a cookie domain is a valid Google domain.

    Uses a whitelist approach to validate Google domains including:
    - Base domain: .google.com
    - Regional .google.com.XX: .google.com.sg, .google.com.au, etc.
    - Regional .google.co.XX: .google.co.uk, .google.co.jp, etc.
    - Regional .google.XX: .google.de, .google.fr, etc.

    This function is used by both auth cookie extraction and download cookie
    validation to ensure consistent domain handling across the codebase.

    Args:
        domain: Cookie domain to check (e.g., '.google.com', '.google.com.sg')

    Returns:
        True if domain is a valid Google domain.

    Note:
        Uses an explicit whitelist (GOOGLE_REGIONAL_CCTLDS) rather than regex
        to prevent false positives from invalid or malicious domains.
    """
    # Base Google domain
    if domain == ".google.com":
        return True

    # Check regional Google domains using whitelist
    if domain.startswith(".google."):
        suffix = domain[8:]  # Remove ".google." prefix
        return suffix in GOOGLE_REGIONAL_CCTLDS

    return False


def _is_allowed_auth_domain(domain: str) -> bool:
    """Check if a cookie domain is allowed for auth cookie extraction.

    Thin alias of :func:`_is_allowed_cookie_domain`. Both auth-jar building
    and download-cookie loading (and the persistence path that filters which
    cookies get saved back) share a single allowlist policy:

    1. Exact match against :data:`REQUIRED_COOKIE_DOMAINS` (covers the API
       host, ``.google.com`` / ``accounts.google.com`` /
       ``.googleusercontent.com`` / ``drive.google.com``, and the
       leading-dot variants ``http.cookiejar`` may normalize to).
    2. Regional Google ccTLDs (``.google.com.sg``, ``.google.co.uk``,
       ``.google.de``, …) where SID cookies may be set for users in those
       regions.
    3. Boundary-aware suffix matches for ``google.com``,
       ``googleusercontent.com``, and every explicitly whitelisted regional
       root. This preserves host-scoped cookies such as
       ``drive.usercontent.google.com`` and ``accounts.google.com.hk``.

    The previous strict / broad split (#334 / fea8315) created an asymmetry
    where ``save_cookies_to_storage`` would persist cookies that the next
    extraction would silently drop. Issue #360 collapsed both filters into
    this single policy. The cookie-domain narrowing control restricts the
    *extraction* surface: ``rookiepy`` requests required domains plus regional
    Google ccTLD variants by default, so YouTube cookies are never written to
    ``storage_state.json`` unless the user opts in via
    ``--include-domains=youtube``. The runtime gate stays permissive over
    the full :data:`ALLOWED_COOKIE_DOMAINS` union so that opted-in cookies
    survive the downstream filters.

    Args:
        domain: Cookie domain to check (e.g., '.google.com', '.google.com.sg')

    Returns:
        True if domain is allowed for auth/download cookies.
    """
    return _is_allowed_cookie_domain(domain)


def _auth_domain_priority(domain: str) -> int:
    """Return duplicate-cookie priority for allowed auth domains.

    Higher value wins. Tiers are **not** distinct: several domains share a tier,
    so the resolved cookie is NOT fully determined by the ranking alone. Where a
    tier is shared, the first occurrence in ``storage_state`` iteration order
    wins, and reordering the file changes the result. The collisions are:

    - **tier 3** — ``.notebooklm.google.com``, ``.notebook.google.com``
      (the Gemini Notebook rebrand host), ``.notebooklm.cloud.google.com``
    - **tier 2** — the three bare (no leading dot) variants of the above
    - **tier 0** — every allowlisted domain that is not a Google ccTLD:
      ``accounts.google.com``, ``drive.google.com``, ``.googleusercontent.com``,
      ``lh3.google.com``, bare ``google.com``, …

    Tier 0 is worth calling out: it holds ``accounts.google.com``, the host the
    ``RotateCookies`` POST targets. A host-scoped cookie there — which *does*
    route to that POST — is outranked by app-host cookies that do not. Ranking
    by tier is therefore not a substitute for RFC 6265 routing when the question
    is "would this cookie be sent to URL X"; use a cookie jar for that
    (issue #2057).
    """
    if domain == ".google.com":
        return 4
    if domain == f".{PERSONAL_LEGACY_HOST}":
        return 3
    if domain == PERSONAL_LEGACY_HOST:
        return 2
    # The other personal app host (issue #2013), which #2067 made the default.
    # The dotted variant sits at the same tier as the legacy host's dotted
    # variant and the bare at the same tier as its bare -- deliberately, so
    # neither host is ranked below the other now that Google mints host-scoped
    # cookies on both.
    #
    # It does NOT mean the default host wins when both carry a name: 3 == 3 and
    # 2 == 2, so those pairs tie and the winner falls to ``storage_state``
    # iteration order. This comment claimed the opposite until #2054; the
    # docstring above describes the real tier structure.
    if domain == f".{PERSONAL_BASE_HOST}":
        return 3
    if domain == PERSONAL_BASE_HOST:
        return 2
    if domain == ".notebooklm.cloud.google.com":
        return 3
    if domain == "notebooklm.cloud.google.com":
        return 2
    if _is_google_domain(domain):
        return 1
    # Allowlisted but unranked domains (e.g. .googleusercontent.com) fall through.
    return 0


def _is_allowed_cookie_domain(domain: str) -> bool:
    """Canonical cookie-domain allowlist for both auth and downloads.

    Single source of truth for "is this cookie domain one we accept at
    runtime?". Both the auth-extraction path and the download path go
    through here — :func:`_is_allowed_auth_domain` is a thin alias
    preserved for call-site readability. See issue #360 for why the split
    was collapsed.

    A domain is allowed if any of the following holds:

    1. Exact match against :data:`REQUIRED_COOKIE_DOMAINS` (the API host,
       ``.google.com``, ``accounts.google.com``, ``.googleusercontent.com``,
       ``drive.google.com``, and the leading-dot variants ``http.cookiejar``
       may normalize to).
    2. Valid Google domain via :func:`_is_google_domain` (regional ccTLDs:
       ``.google.com.sg``, ``.google.co.uk``, ``.google.de``, …).
    3. Root or subdomain accepted by the compatibility-first trusted-Google
       matcher. This covers ``*.google.com``, ``*.googleusercontent.com``, and
       every regional root in :data:`GOOGLE_REGIONAL_CCTLDS` (for example
       ``accounts.google.com.hk`` and ``lh3.google.co.uk``).

    The leading-dot suffix check ensures lookalikes like ``evil-google.com``
    are rejected.

    Note: the runtime gate consults the
    :data:`ALLOWED_COOKIE_DOMAINS` union (REQUIRED ∪ OPTIONAL). The
    blast-radius reduction starts with the **requested extraction set** —
    ``_build_google_cookie_domains`` defaults to
    :data:`REQUIRED_COOKIE_DOMAINS` plus regional ``.google.<ccTLD>`` variants.
    Some extractors suffix-match those requests, so the runtime and write
    gates deliberately retain trusted Google-root subdomains for compatibility.
    Distinct optional roots still require ``--include-domains=...``. The
    runtime gate must stay permissive so that opted-in cookies survive
    the downstream filters in :func:`convert_rookiepy_cookies_to_storage_state`,
    :func:`extract_cookies_with_domains`, and
    :func:`build_httpx_cookies_from_storage`.

    Args:
        domain: Cookie domain to check (e.g., '.google.com', 'lh3.google.com')

    Returns:
        True if domain is allowed for auth/download cookies.
    """
    # Exact match against the union of REQUIRED + OPTIONAL. Anything that
    # could have been validly opted in via ``--include-domains`` at
    # extraction time must pass this gate at runtime.
    if domain in ALLOWED_COOKIE_DOMAINS:
        return True

    # Check if it's a valid canonical Google domain (base or regional).
    if _is_google_domain(domain):
        return True

    # Compatibility-first suffix policy shared with the write-time filter.
    # This includes regional subdomains and ``drive.usercontent.google.com``;
    # label-boundary matching rejects ``evilgoogle.com``.
    return _is_trusted_google_cookie_domain(domain)
