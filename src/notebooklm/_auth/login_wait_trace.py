"""DEBUG tracing for the interactive login wait — pure leaf of ``browser_capture``.

``notebooklm -vv login`` used to print nothing at all for the whole five-minute
``page.wait_for_url`` block, so a login that never landed (e.g. Google's
``notebook.google.com`` rebrand) was indistinguishable from a user who simply
walked away from the browser. Issues #2017 / #2022 / #2023 / #2025 / #2028 /
#2030 / #2032 each needed manual triage that a single "navigated to X" line
would have answered. See #2046.

This module owns the Playwright-event side of that tracing so the capture core
stays under the ADR-0008 module-size budget. It is a leaf: it imports only the
credential-stripping URL formatter (:func:`trace_url`) and stdlib, holds no
state, and has no CLI / Click / Rich coupling (ADR-0021).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = ["log_observed_navigations", "safe_page_url", "trace_url"]

# Stand-in when the page's URL cannot be read at all. Distinct from
# ``trace_url("")`` (which returns ``""``) so an operator reading the log can
# tell "the page was gone" apart from "the URL was empty".
_UNREADABLE_URL = "<unavailable>"

# Rendering for a URL that carries no host at all (``about:blank``, ``data:``,
# ``chrome-error://``). The scheme is the useful signal; whatever follows it can
# be arbitrary opaque data, so it is never reproduced.
_HOSTLESS_URL = "{scheme}:<no host>"


def trace_url(url: str) -> str:
    """Render ``url`` as ``scheme://host[:port]/`` — host only, nothing else.

    Deliberately stricter than ``_auth.extraction._safe_url``, which is built
    for *error messages about Google endpoints*: it keeps the path for any host
    outside a small Google-OAuth allowlist, on the reasoning that the path tells
    an operator which endpoint failed.

    That trade is wrong here. This formatter renders **arbitrary main-frame
    navigations observed during a live SSO flow**, and a Workspace tenant can
    federate to any identity provider — ``https://idp.example/sso/<assertion>``
    puts a one-time credential straight in the path of a host no allowlist can
    anticipate. `-vv` output is exactly what our issue template asks users to
    paste into public bug reports, so the safe default is to keep nothing but
    the host.

    Nothing is lost: the entire diagnostic this tracing exists to provide is
    *which host the browser is on* — "waiting for notebooklm.google.com, landed
    on notebook.google.com". The path never contributed to that answer.

    Userinfo (``https://TOKEN@host/``) is dropped by rebuilding from
    ``hostname``; query and fragment are dropped by never reading them.
    """
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return _HOSTLESS_URL.format(scheme=parsed.scheme or "<unknown>")
    # ``hostname`` strips the brackets off an IPv6 literal, so they have to go
    # back on before a port can be appended — otherwise
    # ``https://[2001:db8::1]:8443/`` renders as ``https://2001:db8::1:8443/``,
    # where the port is indistinguishable from the address's last group.
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = f"{rendered_host}:{parsed.port}" if parsed.port is not None else rendered_host
    return f"{parsed.scheme}://{netloc}/"


def _log_suppressed(what: str, exc: BaseException) -> None:
    """Record that a tracing step failed, naming the exception TYPE only.

    Deliberately **not** ``exc_info=True``. Playwright exception messages
    routinely embed the offending URL (``net::ERR_ABORTED at https://…?f.sid=…``),
    and a rendered traceback bypasses :func:`trace_url` — the precise,
    structural redaction this module promises — leaving only the package's
    heuristic ``scrub_secrets`` backstop, which has no marker to match on an
    opaque OAuth grant carried in a URL *path*. The exception class is the
    entire diagnostic signal here (``TargetClosedError`` vs ``TypeError``);
    the message adds leak surface and nothing else.
    """
    logger.debug("Login wait: %s (%s)", what, type(exc).__name__)


def safe_page_url(page: Any) -> str:
    """Return ``page.url`` credential-stripped, or a placeholder if unreadable.

    Reading ``url`` off a Playwright page can raise once the page or browser is
    gone. A DEBUG diagnostic must never be the thing that turns a
    browser-closed login into an unhandled traceback, so every failure degrades
    to :data:`_UNREADABLE_URL` instead of propagating.
    """
    try:
        return trace_url(page.url)
    except Exception as exc:
        _log_suppressed("could not read the page URL", exc)
        return _UNREADABLE_URL


def _is_main_frame(frame: Any, main_frame: Any) -> bool:
    """True when ``frame`` is the page's top-level frame.

    Identity against ``page.main_frame`` is the fast path, but it is not the
    only test: if Playwright ever hands the listener a different wrapper object
    for the same underlying frame, an identity-only filter would silently drop
    *every* navigation — turning this diagnostic back into the silence it
    exists to fix. So fall back to the structural definition: only the top
    frame has no parent.
    """
    if main_frame is not None and frame is main_frame:
        return True
    return getattr(frame, "parent_frame", None) is None


@contextmanager
def log_observed_navigations(page: Any) -> Iterator[None]:
    """Log every main-frame navigation observed inside the block, at DEBUG.

    Guarantees that let this sit inside the five-minute login wait:

    * **Inert when DEBUG is off** — the listener is never attached, so no
      Playwright event plumbing runs and the wait is byte-for-byte unchanged.
    * **Never breaks the wait** — the callback swallows every exception, and a
      Playwright build without ``page.on`` degrades to a no-op block.
    * **Never leaks credentials** — URLs go through :func:`trace_url`, which
      keeps the host and drops everything else (path, query, fragment,
      userinfo), any of which can carry auth material mid-SSO — including on a
      third-party identity provider no allowlist could anticipate.

    Args:
        page: The Playwright ``Page`` being waited on. Typed ``Any`` because
            ``playwright`` is an optional (``browser`` extra) dependency this
            leaf must not import.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        yield
        return

    # ``getattr`` only absorbs a MISSING attribute — a ``main_frame`` property
    # that *raises* (dead page) would propagate straight past the ``yield`` and
    # pre-empt the wait entirely, so the read itself is guarded.
    try:
        main_frame = getattr(page, "main_frame", None)
    except Exception as exc:
        _log_suppressed("could not read the page's main frame", exc)
        main_frame = None

    def _on_navigated(frame: Any) -> None:
        try:
            # Sub-frame navigations (SSO iframes, ad frames) are noise; the
            # login predicate only ever looks at the main frame's URL.
            if not _is_main_frame(frame, main_frame):
                return
            logger.debug("Login wait: navigated to %s", trace_url(getattr(frame, "url", "") or ""))
        except Exception as exc:
            _log_suppressed("could not read a navigation URL", exc)

    try:
        page.on("framenavigated", _on_navigated)
    except Exception as exc:
        _log_suppressed("navigation logging unavailable", exc)

    try:
        yield
    finally:
        # Detach unconditionally rather than gating on "did ``on`` return
        # cleanly". A registration that raised *after* recording the handler
        # would otherwise leak a listener onto a page the caller keeps using,
        # and an unnecessary detach is free: removing a handler that was never
        # registered fails locally and is swallowed right here.
        try:
            page.remove_listener("framenavigated", _on_navigated)
        except Exception as exc:
            _log_suppressed("could not detach the navigation listener", exc)
