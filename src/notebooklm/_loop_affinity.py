"""Free-function event-loop affinity guard.

A single small helper that compares a previously-captured event loop reference
against ``asyncio.get_running_loop()`` and raises an actionable
:class:`RuntimeError` on mismatch. Lives in its own module so the helpers
that need to call it (``_core_drain.py`` / ``_core_reqid.py`` /
``_core_auth.py`` / ``_artifact_polling.py`` / ``_chat.py``) can import it
without dragging in :class:`notebooklm._session.Session` — none of those
modules currently have a direct ``Session`` reference and adding one
just to reach a bound-loop attribute would re-couple them.

Design constraints:

* Module-private helper, intentionally tiny. The body is a single
  ``is``-comparison so the per-call cost on the hot path (e.g.
  :meth:`ReqidCounter.next_reqid` is called once per chat ask) stays
  negligible.

* ``bound_loop is None`` is a silent no-op so callers that haven't yet
  observed an ``open()`` (most notably standalone fixtures that construct
  the helpers directly without a :class:`Session`) keep working without
  a special-case branch on every call site.

* The error message is intentionally identical in spirit to the inline
  guard at ``_core_transport.py:258-262`` so downstream call sites can
  surface a uniform diagnostic regardless of which seam the cross-loop
  call hit first.

Test coverage lives in ``tests/unit/concurrency/test_loop_affinity_guard.py``.
"""

from __future__ import annotations

import asyncio


def assert_bound_loop(bound_loop: asyncio.AbstractEventLoop | None) -> None:
    """Raise ``RuntimeError`` if the current running loop is not ``bound_loop``.

    Args:
        bound_loop: The loop the owning client / helper captured at
            ``open()`` time, or ``None`` if no binding has happened yet.
            ``None`` is a silent no-op — callers reach the helper before a
            binding exists (standalone fixtures, lazy-init paths) and the
            guard's job is to catch *cross-loop* misuse, not to enforce
            that a binding is present.

    Raises:
        RuntimeError: When ``bound_loop`` is non-``None`` and differs from
            ``asyncio.get_running_loop()``. The message mirrors the inline
            guard in ``_core_transport.AuthedTransport.perform_authed_post``
            so callers see a consistent diagnostic regardless of which
            entry point caught the mismatch.
    """
    if bound_loop is None:
        return
    current = asyncio.get_running_loop()
    if bound_loop is not current:
        raise RuntimeError(
            "NotebookLMClient is bound to a different event loop. "
            "Each client is per-loop; create a new client in the target loop."
        )


__all__ = ["assert_bound_loop"]
