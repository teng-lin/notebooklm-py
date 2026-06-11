"""Guard the click-group / package-attribute shadowing fix from P3.T0.

Before P3.T0, ``cli/__init__.py`` did ``from .download import download`` and
similar for 15 other command groups. Because the click group was bound at the
package level under the SAME name as the source module, ``import
notebooklm.cli.download`` returned the click ``Group`` object instead of the
module — tests that wanted to patch a symbol inside the module had to use a
``get_cli_module()`` helper that called ``importlib`` to bypass the shadow.

P3.T0 renamed the source modules to ``*_cmd.py`` (e.g. ``download_cmd``) so
the click groups can keep their historical public names without overwriting
the module's package attribute. This test asserts that, for each of the 16
renamed modules:

1. ``importlib.import_module("notebooklm.cli.<name>_cmd")`` returns a real
   module (``inspect.ismodule`` is ``True``).
2. The click group is still reachable at its historical public path
   ``notebooklm.cli.<name>`` (for the 12 groups exported under their original
   name from ``cli/__init__.py``; ``chat``, ``doctor``, ``notebook`` and
   ``session`` use ``register_*`` helpers instead of a top-level click group
   so they are exempt from check 2).

If this test fails for check 1, someone re-introduced shadowing in
``cli/__init__.py`` (e.g. added ``from .download import download`` instead of
``from .download_cmd import download``). If it fails for check 2, the public
re-export under the historical click-group name was dropped — that breaks the
``notebooklm`` console script's import block in ``notebooklm_cli.py`` and any
downstream tooling that imports the click groups by their public names.
"""

from __future__ import annotations

import importlib
import inspect

import click
import pytest

# The 16 modules renamed in P3.T0.
RENAMED_MODULES = (
    "agent",
    "artifact",
    "chat",
    "doctor",
    "download",
    "generate",
    "label",
    "language",
    "note",
    "notebook",
    "profile",
    "research",
    "session",
    "share",
    "skill",
    "source",
)

# Of those 16, the click groups still re-exported under their historical names
# from ``cli/__init__.py`` (`__all__`). ``chat``/``doctor``/``notebook`` and
# ``session`` instead expose ``register_*`` helpers, so the click group is
# attached at registration time rather than re-exported as a top-level name.
CLICK_GROUPS_PUBLIC = (
    "agent",
    "artifact",
    "download",
    "generate",
    "label",
    "language",
    "note",
    "profile",
    "research",
    "share",
    "skill",
    "source",
)


@pytest.mark.parametrize("name", RENAMED_MODULES)
def test_cmd_module_imports_as_real_module(name: str) -> None:
    """``notebooklm.cli.<name>_cmd`` must resolve to a real module, not a click group."""
    mod = importlib.import_module(f"notebooklm.cli.{name}_cmd")
    assert inspect.ismodule(mod), (
        f"notebooklm.cli.{name}_cmd is {type(mod).__name__}, not a module — "
        f"someone may have re-introduced package-attribute shadowing."
    )
    # Belt-and-braces: not a click Group either.
    assert not isinstance(mod, click.Command), (
        f"notebooklm.cli.{name}_cmd is a click Command — shadowing is back."
    )


@pytest.mark.parametrize("public_name", CLICK_GROUPS_PUBLIC)
def test_click_group_public_alias_preserved(public_name: str) -> None:
    """The click group is still reachable under its historical public name.

    ``from notebooklm.cli import download`` (etc.) must continue to return the
    click ``Group`` so ``notebooklm_cli.py`` and external importers don't
    break. ``cli/__init__.py`` aliases each group to its pre-rename name via
    ``from .<X>_cmd import <X>``.
    """
    cli_pkg = importlib.import_module("notebooklm.cli")
    obj = getattr(cli_pkg, public_name, None)
    assert obj is not None, (
        f"notebooklm.cli.{public_name} is missing — the historical "
        f"click-group re-export in cli/__init__.py was dropped."
    )
    assert isinstance(obj, click.Command), (
        f"notebooklm.cli.{public_name} resolved to {type(obj).__name__}, "
        f"expected a click.Command (Group)."
    )
