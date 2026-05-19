"""CLI session-side test fixtures (D1 PR-3).

Background
----------

Before D1 PR-3, ``notebooklm.cli.session`` wrapped every helper that
``cli.services.login`` exposed in a per-call
``_patched_login_service_dependencies()`` context manager. The wrapper copied
session-side monkeypatches forward into ``cli.services.login`` at call time,
which is why historical tests could ``patch("notebooklm.cli.session.X")`` and
have the patch be visible to ``cli.services.login`` internals that referenced
``X`` by local name.

D1 PR-3 retired that 350-LOC forwarding block in favor of direct re-imports.
The trade-off: a patch on ``cli.session.X`` now rebinds *only*
``cli.session``'s module namespace; ``cli.services.login``'s own local
binding (the canonical source of truth) is untouched. Tests that want a
helper intercepted regardless of which entry point reaches it must patch
both surfaces.

What this module provides
-------------------------

``patch_session_login_dual(name, **patch_kwargs)`` —
    Convenience context manager that patches both
    ``notebooklm.cli.session.<name>`` and
    ``notebooklm.cli.services.login.<name>`` with the same mock. Returns the
    primary mock (the services-side one — that's where the canonical
    implementation lives, so the patch surface that matters most).

This helper is the recommended migration target for the small number of
test sites that still rely on the legacy session-only patch pattern. New
tests should patch ``notebooklm.cli.services.login.X`` directly per ADR-008.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import patch


@contextmanager
def patch_session_login_dual(name: str, **patch_kwargs: Any) -> Iterator[Any]:
    """Patch both session-side and services-side bindings of ``name``.

    ``name`` is a helper symbol that lives in
    :mod:`notebooklm.cli.services.login` and is re-imported by
    :mod:`notebooklm.cli.session`. Tests that need a helper intercepted
    regardless of which module's binding resolves the call use this to
    avoid hand-wiring two ``patch(...)`` calls.

    The two patches share the same mock object, so call assertions made
    against the returned mock count *every* invocation across both
    surfaces — matching the historical pre-D1 PR-3 behavior of the
    forwarding wrappers in ``cli.session``.

    Args:
        name: Bare symbol name (e.g. ``"_login_with_browser_cookies"``).
        **patch_kwargs: Forwarded to :func:`unittest.mock.patch` for both
            surfaces. Typical: ``new=...``, ``side_effect=...``,
            ``return_value=...``, ``new_callable=AsyncMock``.

    Yields:
        The shared mock used for both surfaces.
    """
    services_target = f"notebooklm.cli.services.login.{name}"
    session_target = f"notebooklm.cli.session.{name}"

    with ExitStack() as stack:
        primary = stack.enter_context(patch(services_target, **patch_kwargs))
        # Patch session-side with the SAME mock so call counts aggregate.
        stack.enter_context(patch(session_target, new=primary))
        yield primary
