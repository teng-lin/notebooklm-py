"""Recurrence gate: no ``*_cmd`` module exposes a patchable ``NotebookLMClient``.

#1481 replaced the per-command-module ``NotebookLMClient`` monkeypatch seam with
client-factory injection through Click's ``ctx.obj``. Command bodies now build the
client via ``cli.auth_runtime.resolve_client_factory(ctx)``; tests inject a fake
through ``runner.invoke(..., obj=inject_client(mock_client))`` (see
``tests/unit/cli/conftest.py``). The old ``patch("notebooklm.cli.X_cmd.NotebookLMClient")``
seam — across ~564 sites — is retired.

This gate locks that shut: every ``notebooklm.cli.*_cmd`` module must NOT expose
``NotebookLMClient`` as a module attribute. A direct ``from ..client import
NotebookLMClient`` (or any re-introduction of the inline ``async with
NotebookLMClient(...)`` construction) would re-create the patch surface this
refactor removed, and copy-paste from an old example is the likely regression
vector — so the gate is enforced, not documented.

Why a runtime ``hasattr`` check (not a test-file string scan): the failure mode is
"a command module re-exposes the client name", observable directly on the live
module surface. ``hasattr`` after import catches every rebind form — plain import,
``patch.object``, ``monkeypatch.setattr``, ``__getattr__`` forward — where a
string scan of tests would miss ``patch.object``/``setattr``/f-string forms and
would false-positive on the *legitimate* ``patch("notebooklm.client.NotebookLMClient")``
source-module seam (which is NOT a ``*_cmd`` attribute and stays valid).

Modules that annotate with the type keep it under ``if TYPE_CHECKING:`` — that
import never executes at runtime, so it creates no module attribute and passes
this gate (the four ``artifact``/``note``/``download``/``session`` modules do this).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import notebooklm.cli as _cli_pkg

_CLI_DIR = Path(_cli_pkg.__file__).resolve().parent


def _command_modules() -> list[str]:
    """Every ``notebooklm.cli.*_cmd`` module, discovered from disk.

    Dynamic discovery (not a hardcoded list) so a *future* command module that
    re-introduces the old seam is caught automatically.
    """
    return sorted(p.stem for p in _CLI_DIR.glob("*_cmd.py"))


def test_discovery_found_the_command_modules() -> None:
    """Guard against the glob silently matching nothing (which would make the
    parametrized gate vacuously pass)."""
    mods = _command_modules()
    assert len(mods) >= 10, f"expected the full *_cmd module set, found {mods}"


@pytest.mark.parametrize("mod_name", _command_modules())
def test_cmd_module_has_no_client_attribute(mod_name: str) -> None:
    """No ``*_cmd`` module may expose a runtime ``NotebookLMClient`` attribute."""
    module = importlib.import_module(f"notebooklm.cli.{mod_name}")
    assert not hasattr(module, "NotebookLMClient"), (
        f"notebooklm.cli.{mod_name}.NotebookLMClient reappeared as a module "
        "attribute. The per-command client patch seam was retired in #1481 — "
        "command bodies resolve the client via "
        "cli.auth_runtime.resolve_client_factory(ctx) and tests inject a fake via "
        "runner.invoke(..., obj=inject_client(mock_client)). Do NOT add "
        "`from ..client import NotebookLMClient` at module scope or re-introduce "
        "`async with NotebookLMClient(...)`; if the type is only needed for an "
        "annotation, import it under `if TYPE_CHECKING:`."
    )


def test_injection_seam_exists() -> None:
    """Sanity: the replacement seam (``resolve_client_factory``) is present, so
    the gate above is asserting a real migration, not an empty surface."""
    from notebooklm.cli.auth_runtime import resolve_client_factory

    assert callable(resolve_client_factory)


def test_source_module_client_seam_is_untouched() -> None:
    """The legitimate source-module seam stays valid: ``notebooklm.client``
    still exposes ``NotebookLMClient`` (used by ``patch("notebooklm.client.NotebookLMClient")``
    in contract tests). This gate must never push tests away from it."""
    client_mod = importlib.import_module("notebooklm.client")
    assert hasattr(client_mod, "NotebookLMClient")
