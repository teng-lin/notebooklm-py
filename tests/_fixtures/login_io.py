"""Test sinks satisfying the browser-cookie login ``LoginIO`` Protocol (#1393).

The browser-cookie login DAG (``cli/services/login/*``) inverts its
presentation / exit / async side effects behind a caller-injected ``LoginIO``
sink (Protocol + resolver in
:mod:`notebooklm.cli.services.login.io_seam`). These helpers let direct unit
tests inject a controllable sink instead of relying on the command-layer
default factory:

* :class:`RecordingLoginIO` — captures every ``emit`` line, raises ``SystemExit``
  on ``fail`` (mirroring ``exit_with_code``), and runs the supplied coroutine /
  awaitable through a real event loop on ``run_async`` (overridable).
* :func:`make_recording_io` — convenience constructor.

Behavior parity note: the production default sink
(:class:`notebooklm.cli.playwright_login_io.PlaywrightLoginIO`) forwards ``emit``
→ ``console.print``, ``fail`` → ``exit_with_code`` (``SystemExit``), and
``run_async`` → ``cli.runtime.run_async``. :class:`RecordingLoginIO` matches
``fail``'s ``SystemExit`` contract exactly so exit-code assertions are
unchanged; ``emit`` lines are captured rather than rendered, and ``run_async``
is a stub by default (tests that need real async drive supply ``run_async``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, NoReturn


def _default_run_async(coro: Awaitable[Any]) -> Any:
    """Drive an awaitable to completion on a throwaway event loop."""
    return asyncio.run(coro)  # type: ignore[arg-type]


@dataclass
class RecordingLoginIO:
    """Controllable ``LoginIO`` sink that records emitted lines.

    Attributes:
        emitted: Every positional first-arg passed to :meth:`emit`, in order.
        run_async: Callable used by :meth:`run_async`. Defaults to
            :func:`_default_run_async` (real event loop); tests that stub the
            async bridge pass their own (e.g. ``MagicMock(return_value=...)``).
    """

    emitted: list[str] = field(default_factory=list)
    run_async: Callable[[Awaitable[Any]], Any] = _default_run_async

    def emit(self, *args: Any, **kwargs: Any) -> None:
        # Mirror ``console.print`` 's first-arg-is-the-message convention so
        # tests can assert on ``io.emitted`` the same way they asserted on
        # ``capsys`` / ``console.print`` call args.
        self.emitted.append(args[0] if args else "")

    def fail(self, code: int) -> NoReturn:
        raise SystemExit(code)


def make_recording_io(
    run_async: Callable[[Awaitable[Any]], Any] | None = None,
) -> RecordingLoginIO:
    """Build a :class:`RecordingLoginIO`, optionally overriding ``run_async``."""
    if run_async is None:
        return RecordingLoginIO()
    return RecordingLoginIO(run_async=run_async)
