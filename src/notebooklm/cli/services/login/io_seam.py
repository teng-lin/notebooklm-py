"""Caller-injected IO seam for the browser-cookie login service (#1393).

The browser-cookie login DAG (``refresh`` → ``browser_accounts`` →
``chromium_accounts`` / ``firefox_accounts`` / ``cookie_writes``) used to
reach the command layer's presentation (``...rendering``), exit-policy
(``...error_handler``), and async-runner (``...runtime``) modules directly
through *level-3* relative imports. Those imports slipped past the ADR-0008
services-boundary scanner, which only flagged *level-2* (``..rendering``)
reach-ins (#1391 closed the level-2 gap for ``playwright_login.py``; the
scanner was tightened to level-3 in #1393).

To stay on the service side of the boundary, the login DAG inverts those side
effects behind the :class:`LoginIO` Protocol — the *same* structural shape
#1391 introduced for the Playwright flow (``emit`` / ``fail`` /
``run_async``). The command layer (:mod:`notebooklm.cli.playwright_login_io`)
supplies the concrete sink and registers it here as the default factory at
import time, so:

* Production code (and tests that import the command layer, directly or via the
  ``cli`` package) gets the real ``console.print`` / ``exit_with_code`` /
  ``run_async`` sink with byte-for-byte-identical behavior.
* Service entry points call :func:`resolve_login_io` to accept an explicit
  injected sink (preferred) or fall back to the default factory. When no sink is
  injected and no factory has been registered yet (a sink-resolving entry point
  reached on a path that never imported the command layer — e.g. a bare
  ``_sync_server_language_to_config()`` call, or a library consumer that
  imported only ``cli.services.login``), the resolver registers the
  command-layer default lazily so it never raises and behavior stays the
  historical default (#1393).

No forbidden ``...rendering`` / ``...error_handler`` / ``...runtime`` import
lives in this module or any of its peers — the concrete sink that *does* import
them lives in the (unscanned) command layer, which this module pulls in only
lazily, on the fallback path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class LoginIO(Protocol):
    """Caller-injected sink for the browser-cookie login flow's side effects.

    Structurally identical to the Playwright flow's ``LoginIO`` (#1391) so the
    one concrete command-layer sink
    (:class:`notebooklm.cli.playwright_login_io.PlaywrightLoginIO`) satisfies
    both. ``emit`` forwards to ``console.print`` (``*args, **kwargs`` pass
    through verbatim, incl. ``markup=False``); ``fail`` forwards to
    ``exit_with_code`` (raises ``SystemExit``); ``run_async`` forwards to
    ``run_async``.
    """

    def emit(self, *args: Any, **kwargs: Any) -> None: ...

    def fail(self, code: int) -> NoReturn: ...

    def run_async(self, coro: Awaitable[Any]) -> Any: ...


# Default sink factory, registered by the command layer
# (:mod:`notebooklm.cli.playwright_login_io`) at import time so service entry
# points that are *not* handed an explicit sink still resolve the real
# ``console`` / ``exit_with_code`` / ``run_async`` behavior. Kept here (not as a
# direct import of the command-layer sink) so this services module never
# reaches across the ADR-0008 boundary.
_default_io_factory: Callable[[], LoginIO] | None = None


def set_default_login_io_factory(factory: Callable[[], LoginIO]) -> None:
    """Register the command-layer default :class:`LoginIO` sink factory.

    Called once by :mod:`notebooklm.cli.playwright_login_io` at import time.
    Idempotent: re-registering simply replaces the factory (the command layer
    only ever registers its single concrete sink).
    """
    global _default_io_factory
    _default_io_factory = factory


def _ensure_default_factory_registered() -> None:
    """Lazily trigger the command-layer default-sink registration.

    Importing :mod:`notebooklm.cli.playwright_login_io` registers the concrete
    default factory as an import side effect (it calls
    :func:`set_default_login_io_factory`). Some login flows resolve the sink
    without going through a driver that already forced that import (e.g. a
    direct ``_sync_server_language_to_config()`` call, or a library consumer
    that imported only ``cli.services.login``). Triggering the import here makes
    :func:`resolve_login_io` robust on every path while keeping the *static*
    dependency direction clean: this module never imports the command layer at
    module scope, and the only thing it pulls in is the ADR-0008 sink module
    (not a presentation / exit / runtime module). Idempotent — once registered,
    the re-import is a no-op.
    """
    # ``...playwright_login_io`` is the command-layer sink module (the ADR-0008
    # seam), not ``...rendering`` / ``...error_handler`` / ``...runtime``; the
    # services-boundary scanner permits it. Kept lazy + local so it only fires
    # on the fallback path. Register ``make_login_io`` explicitly rather than
    # relying solely on the import side effect, so this self-heals even when the
    # module was already imported (its import-time registration won't re-run).
    from ...playwright_login_io import make_login_io

    set_default_login_io_factory(make_login_io)


def resolve_login_io(io: LoginIO | None) -> LoginIO:
    """Return ``io`` if given, else build one from the default factory.

    Service entry points call this so callers may inject a sink explicitly
    (the preferred shape) while direct callers — and tests / library consumers
    that exercise the service without injecting a sink — still get the real
    console/exit/async behavior. When no factory has been registered yet (the
    command layer was never imported on this path), the command-layer default
    is registered lazily via :func:`_ensure_default_factory_registered` so the
    resolver never raises and behavior is byte-for-byte the historical default.
    """
    if io is not None:
        return io
    if _default_io_factory is None:
        _ensure_default_factory_registered()
    if _default_io_factory is None:  # pragma: no cover - defensive wiring guard
        raise RuntimeError(
            "No LoginIO sink injected and the command-layer default sink could "
            "not be registered (notebooklm.cli.playwright_login_io import had no "
            "effect). Pass an explicit io= sink."
        )
    return _default_io_factory()


__all__ = [
    "LoginIO",
    "resolve_login_io",
    "set_default_login_io_factory",
]
