"""CLI session-side test fixtures (D1 PR-3, extended for P3.T4).

Background
----------

Before D1 PR-3, ``notebooklm.cli.session_cmd`` wrapped every helper that
``cli.services.login`` exposed in a per-call
``_patched_login_service_dependencies()`` context manager. The wrapper copied
session-side monkeypatches forward into ``cli.services.login`` at call time,
which is why historical tests could ``patch("notebooklm.cli.session_cmd.X")`` and
have the patch be visible to ``cli.services.login`` internals that referenced
``X`` by local name.

D1 PR-3 retired that 350-LOC forwarding block in favor of direct re-imports.
The trade-off: a patch on ``cli.session.X`` now rebinds *only*
``cli.session``'s module namespace; ``cli.services.login``'s own local
binding (the canonical source of truth) is untouched. Tests that want a
helper intercepted regardless of which entry point reaches it must patch
both surfaces.

P3.T4 split ``cli/services/login.py`` into a package whose implementation
modules each bind their own copies of the external helpers
(``get_storage_path``, ``run_async``, ``console``, …). A patch on the
package's ``__init__.py`` attribute would no longer reach those binding
sites. To preserve the historical "patch once, intercept everywhere"
contract, the fixture below additionally fans the patch out to every
submodule of :mod:`notebooklm.cli.services.login` that binds ``name`` —
the test API stays the same; only the patch surface grew.

What this module provides
-------------------------

``patch_session_login_dual(name, **patch_kwargs)`` —
    Convenience context manager that patches:

    * ``notebooklm.cli.session_cmd.<name>`` (legacy session-side binding).
    * ``notebooklm.cli.services.login.<name>`` (package ``__init__.py``
      re-export — primary mock that ``patch_kwargs`` originally configures).
    * ``notebooklm.cli.services.login.<sub>.<name>`` for every submodule
      ``<sub>`` of the package that has ``name`` as a module attribute
      (post-T4 fan-out — the binding sites that actually live inside
      submodule globals).

    All patches share the same mock object so call assertions made
    against the returned mock aggregate *every* invocation across all
    surfaces — matching the historical pre-D1 PR-3 behavior of the
    forwarding wrappers.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import patch


def _services_login_submodule_targets(name: str) -> list[str]:
    """Return dotted-path patch targets for every submodule binding ``name``.

    Walks :mod:`notebooklm.cli.services.login`'s package and yields the
    target string for every submodule whose globals include ``name``. The
    package's own ``__init__.py`` is intentionally skipped (it has its own
    explicit patch in :func:`patch_session_login_dual`).
    """
    pkg = importlib.import_module("notebooklm.cli.services.login")
    pkg_path = getattr(pkg, "__path__", None)
    if pkg_path is None:
        # ``cli.services.login`` is a single file (pre-T4 state) — no
        # submodules to fan out to.
        return []

    targets: list[str] = []
    for module_info in pkgutil.iter_modules(pkg_path):
        sub_name = module_info.name
        full_name = f"notebooklm.cli.services.login.{sub_name}"
        try:
            sub = importlib.import_module(full_name)
        except ImportError:
            continue
        if name in vars(sub):
            targets.append(f"{full_name}.{name}")
    return targets


@contextmanager
def patch_session_login_dual(name: str, **patch_kwargs: Any) -> Iterator[Any]:
    """Patch session-side, package, and submodule bindings of ``name``.

    ``name`` is a helper symbol that lives in
    :mod:`notebooklm.cli.services.login` (or one of its submodules,
    post-T4) and is re-imported by :mod:`notebooklm.cli.session_cmd`.
    Tests that need a helper intercepted regardless of which entry point
    reaches it use this to avoid hand-wiring N ``patch(...)`` calls.

    The patches share the same mock object, so call assertions made
    against the returned mock count *every* invocation across all
    surfaces — matching the historical pre-D1 PR-3 behavior of the
    forwarding wrappers in ``cli.session``.

    Args:
        name: Bare symbol name (e.g. ``"_login_with_browser_cookies"``).
        **patch_kwargs: Forwarded to :func:`unittest.mock.patch` for the
            primary services-side surface. Typical: ``new=...``,
            ``side_effect=...``, ``return_value=...``,
            ``new_callable=AsyncMock``.

    Yields:
        The shared mock used for every surface.
    """
    services_target = f"notebooklm.cli.services.login.{name}"
    session_target = f"notebooklm.cli.session_cmd.{name}"
    submodule_targets = _services_login_submodule_targets(name)

    with ExitStack() as stack:
        primary = stack.enter_context(patch(services_target, **patch_kwargs))
        # Patch session-side with the SAME mock so call counts aggregate.
        stack.enter_context(patch(session_target, new=primary))
        # Fan the same mock out to every submodule binding (post-T4 split).
        # The submodules have their own copies of external helpers
        # (`get_storage_path`, `run_async`, etc.) plus their own copies of
        # internal helpers (`_write_extracted_cookies`, `_select_account`,
        # …) — patching only the package re-export would silently no-op for
        # calls originating inside those submodules.
        for target in submodule_targets:
            stack.enter_context(patch(target, new=primary))
        yield primary
