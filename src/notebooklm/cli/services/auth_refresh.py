"""Click-free orchestration helpers for ``notebooklm auth refresh``.

``bootstrap_missing_storage_from_master_token`` is a pure re-export (auth
cross-boundary ledger shrink, follow-up to #2103 PR-2/PR-3): the
bootstrap-flock machinery AND the four-state-outcome-to-bool collapse both
live in :mod:`notebooklm._auth.master_token` now — this module previously
did the collapse itself, importing both ``BootstrapOutcome`` and the
enum-returning coarse op across the ``cli/`` boundary to do it. Re-exported
here (rather than imported directly by ``cli/playwright_login_io.py``) to
preserve the existing call path
(``auth_refresh_service.bootstrap_missing_storage_from_master_token``) tests
already exercise.
"""

from __future__ import annotations

# The app operation is still a one-await adapter over the public facade.  This
# service remains a pure re-export so its historical import path adds no frame.
from ..._app.master_token import bootstrap_missing_storage_from_master_token

__all__ = ["bootstrap_missing_storage_from_master_token"]
