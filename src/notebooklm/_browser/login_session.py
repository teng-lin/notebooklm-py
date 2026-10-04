"""Whether a browser that landed on the app host is actually signed in.

Split out of ``browser_capture.py`` (ADR-0008 module-size budget) when #2467
showed that landing on the app host is not proof of a session: signed out,
``notebook.google.com`` serves its ``/trynow`` landing page instead of
redirecting to ``accounts.google.com``, so a host check alone reported
"Already logged in" and saved a state with no ``SID``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .._auth.cookie_policy import cookie_names_from_storage
from ..config import get_base_url
from .navigation_errors import is_navigation_race

GOOGLE_SIGN_IN_URL = "https://accounts.google.com/ServiceLogin"


def has_google_session(context: Any) -> bool:
    """Return whether the browser context holds a Google ``SID`` cookie.

    Only ``SID`` is checked: a missing ``__Secure-1PSIDTS`` is healed after
    capture, so requiring it here would reject a recoverable sign-in.
    """
    return "SID" in cookie_names_from_storage(context.storage_state())


def google_sign_in_url() -> str:
    """Return the Google sign-in URL that continues back to the app host."""
    return f"{GOOGLE_SIGN_IN_URL}?continue={quote(f'{get_base_url()}/', safe='')}"


def open_google_sign_in(page: Any) -> None:
    """Send a signed-out landing to the sign-in form so a human can log in.

    The login wait then returns only once Google sends the browser back to the
    app host, i.e. after a real sign-in. A superseded navigation is benign.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        page.goto(google_sign_in_url(), wait_until="commit", timeout=30000)
    except PlaywrightError as exc:
        if not is_navigation_race(exc):
            raise
