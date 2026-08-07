"""In-memory browser-cookie validation and PSIDTS recovery bridge.

ADR-0031 Stage 2 splits the two verbs this module fuses. ``validate_with_recovery``
did three things behind one name — check, then try to fix, then check again —
with the outcome threaded through an ``initial`` variable across nested
try/except/else. The name says the quiet part out loud: a function called
"validate WITH RECOVERY" is two operations, and callers who want only the
question ("is this set usable?") had no way to ask it without also triggering a
network POST and an in-place mutation of their rows.

The verbs are now separable:

* :func:`validate` — **pure**. Converts, runs both checks, returns a
  :class:`ValidationResult`. No network, no mutation.
* :func:`heal` — the fix. Fires one in-memory ``RotateCookies`` rotation and
  mutates ``rookiepy_cookies`` in place on success.
* :func:`validate_with_recovery` — unchanged compat wrapper composing
  ``validate → heal → re-check``, byte-identical in behavior to before.

The wrapper keeps its exact signature, its in-place mutation contract (which
``cli/services/login/refresh.py`` depends on and documents), and its
``(storage_state, error_or_None)`` return shape. It has four first-party
callers plus a ``RefreshDeps`` injection seam and an entry in the auth
cross-boundary ledger, so it is not going anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import cookie_policy as _cookie_policy
from . import cookies as _auth_cookies
from . import psidts_recovery as _recovery


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a pure cookie-set validation.

    ``error`` carries the policy's own
    :class:`~notebooklm._auth.cookie_policy.RequiredCookieValidationError`
    (a ``ValueError``) with the closed-enum ``reason`` it grew in #2061 — the
    result object wraps that error rather than replacing it, so callers keep
    the machine-readable reason and the exact message they had before.
    """

    ok: bool
    error: _cookie_policy.RequiredCookieValidationError | None = None

    @property
    def reason(self) -> _cookie_policy.RequiredCookieReason | None:
        """The closed-enum failure reason, or ``None`` when valid."""
        return self.error.reason if self.error is not None else None


def _check_required_present(storage_state: dict[str, Any]) -> ValidationResult:
    """Tier-1 presence check against a converted storage state."""
    try:
        _auth_cookies.extract_cookies_from_storage(storage_state)
    except _cookie_policy.RequiredCookieValidationError as exc:
        return ValidationResult(ok=False, error=exc)
    return ValidationResult(ok=True)


def _check_routable(rookiepy_cookies: list[dict[str, Any]]) -> ValidationResult:
    """RFC 6265 routing preflight against the RAW rookiepy rows.

    Deliberately reads the raw rows, not the converted state: the two shapes
    spell the http-only flag differently (``http_only`` vs ``httpOnly``), and
    this check needs the rookiepy converter
    (:func:`notebooklm._auth.psidts_recovery._rookiepy_entry_to_cookie`) so the
    cookies it reasons about are the ones a request would actually send.
    """
    entries = _auth_cookies._sanitized_auth_entries({"cookies": rookiepy_cookies})
    try:
        _auth_cookies._validate_routable_entries(
            entries,
            to_cookie=_recovery._rookiepy_entry_to_cookie,
            require_routable=True,
        )
    except _cookie_policy.RequiredCookieValidationError as exc:
        return ValidationResult(ok=False, error=exc)
    return ValidationResult(ok=True)


def validate(
    rookiepy_cookies: list[dict[str, Any]],
) -> tuple[dict[str, Any], ValidationResult]:
    """Convert and validate browser rows. **Pure** — no network, no mutation.

    Runs the presence check first and the routing preflight second, returning
    the first failure. Returns the converted storage state alongside the result
    so callers that need the converted form do not convert twice.
    """
    storage_state = _auth_cookies.convert_rookiepy_cookies_to_storage_state(rookiepy_cookies)
    present = _check_required_present(storage_state)
    if not present.ok:
        return storage_state, present
    return storage_state, _check_routable(rookiepy_cookies)


def heal(rookiepy_cookies: list[dict[str, Any]]) -> bool:
    """Attempt one in-memory ``RotateCookies`` rotation to mint PSIDTS.

    **Mutates ``rookiepy_cookies`` in place** on success, replacing rotated
    rows by RFC 6265 identity — the contract
    ``cli/services/login/refresh.py`` relies on. Returns whether the rotation
    landed a usable ``__Secure-1PSIDTS``.

    The only heal strategy that exists today. Naming it makes the recovery
    ladder's shape (try cheap, escalate) extendable here rather than stopping
    at one bespoke arm baked into a validator's name.
    """
    return _recovery.recover_psidts_in_memory(rookiepy_cookies)


def validate_with_recovery(
    rookiepy_cookies: list[dict[str, Any]],
) -> tuple[dict[str, Any], ValueError | None]:
    """Convert and validate rookiepy cookies, attempting one in-memory heal.

    Compat wrapper over :func:`validate` + :func:`heal`, preserving the exact
    prior behavior for its four first-party callers and the ``RefreshDeps``
    injection seam.

    One asymmetry is worth stating because the pre-split control flow hid it:
    the post-heal re-check runs the **presence** check only, not the routing
    preflight. That is intentional and unchanged — the heal exists precisely to
    mint a PSIDTS that routes, so its success is what the rotation already
    proved; re-running the preflight here would re-litigate the thing the heal
    just established.
    """
    storage_state, result = validate(rookiepy_cookies)
    if result.ok:
        return storage_state, None

    if not heal(rookiepy_cookies):
        return storage_state, result.error

    healed_state = _auth_cookies.convert_rookiepy_cookies_to_storage_state(rookiepy_cookies)
    return healed_state, _check_required_present(healed_state).error
