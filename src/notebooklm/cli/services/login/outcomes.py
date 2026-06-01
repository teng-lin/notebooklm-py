"""Typed outcomes for the browser-cookie helper chain.

This module defines a small sum-type ADT that the
:mod:`notebooklm.cli.services.login` helper chain (``browser_accounts``,
``cookie_jar``, ``cookie_writes``, ``rookiepy_errors``) returns instead
of calling ``console.print`` + ``exit_with_code(1)`` directly. Command
bodies dispatch on the returned outcome — they emit a JSON envelope
under ``--json`` (per :doc:`/adr/0015-json-envelope-contract-for-post-parse-click-exceptions`)
or Rich text otherwise.

The ADT is intentionally narrow: it carries just enough structure to
produce the canonical envelope shape
``{"error": true, "code": "<CODE>", "message": "<text>", ...extras}``
and to render the same human-readable text the previous inline
``console.print`` produced. ``message`` may contain Rich markup; command
layers strip it for JSON envelopes and preserve it for text output.
``code`` is the stable code for the JSON envelope.

Outcome variants:

* :class:`UnknownBrowser` — caller passed a browser name we do not
  support (``--browser foo`` where ``foo`` is not in the rookiepy
  alias map). Carries the supported list for the message body.
* :class:`UnsupportedBrowser` — rookiepy itself doesn't support the
  named browser on this platform (e.g. ``safari`` on Linux). Distinct
  from :class:`UnknownBrowser` so callers can render a different hint.
* :class:`CookieValidationFailure` — cookies were read but failed the
  ``validate_with_recovery`` policy check (missing required cookies,
  malformed values).
* :class:`StaleCookies` — Google rejected the cookie set as too stale
  (passive sign-in redirected to the account chooser, RotateCookies
  returned 401).
* :class:`NetworkFailure` — transport-layer failure reaching Google
  (DNS, timeout, TLS). Distinct from cookie-policy failures so the
  caller can render a retry hint.

A success path keeps its existing return shape — outcome objects are
returned only on failure. Callers therefore branch on
``isinstance(result, BrowserCookieOutcome)`` to detect the failure
path; the boolean check is sufficient because there is no positive
``Success`` variant (success returns the original tuple / list / value
unchanged).
"""

from __future__ import annotations

from dataclasses import dataclass


class BrowserCookieOutcome:
    """Sealed base class for browser-cookie helper failure outcomes.

    Subclasses are frozen dataclasses with two mandatory attributes:

    * ``code`` — stable JSON envelope error code (e.g. ``"UNKNOWN_BROWSER"``).
    * ``message`` — human-readable text (Rich markup is preserved so the
      existing text-mode rendering is byte-for-byte unchanged).

    The base-class annotations are an interface hint for dispatchers, not
    constructor fields. Instantiate one of the frozen dataclass variants
    below; ``BrowserCookieOutcome`` itself is never constructed directly.

    Variants are kept narrow on purpose; new failure modes should either
    reuse an existing variant or introduce a new dataclass here with an
    explicit code constant.
    """

    code: str
    message: str


@dataclass(frozen=True)
class UnknownBrowser(BrowserCookieOutcome):
    """User passed a browser name the rookiepy alias map does not know."""

    code: str
    message: str
    name: str
    supported: tuple[str, ...]


@dataclass(frozen=True)
class UnsupportedBrowser(BrowserCookieOutcome):
    """rookiepy is installed but does not support this browser on this platform."""

    code: str
    message: str
    name: str


@dataclass(frozen=True)
class CookieValidationFailure(BrowserCookieOutcome):
    """Cookies were read but failed the required-cookies / recovery policy."""

    code: str
    message: str


@dataclass(frozen=True)
class StaleCookies(BrowserCookieOutcome):
    """Google rejected the cookie set as too stale to re-authenticate."""

    code: str
    message: str


@dataclass(frozen=True)
class NetworkFailure(BrowserCookieOutcome):
    """Transport-layer failure (DNS / timeout / TLS) reaching Google."""

    code: str
    message: str


__all__ = [
    "BrowserCookieOutcome",
    "CookieValidationFailure",
    "NetworkFailure",
    "StaleCookies",
    "UnknownBrowser",
    "UnsupportedBrowser",
]
