"""CLI adapter for skill-install result handling — re-export over ``_app``.

The ``report_mixed_no_clobber_up_to_date`` reporting decision is
transport-neutral and now lives in :mod:`notebooklm._app.skill`. This module
re-exports it so existing
``from notebooklm.cli.services.skill_install import report_mixed_no_clobber_up_to_date``
imports (the command layer + its unit tests) keep resolving.
"""

from ..._app.skill import report_mixed_no_clobber_up_to_date

__all__ = ["report_mixed_no_clobber_up_to_date"]
