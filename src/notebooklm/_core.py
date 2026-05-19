"""Compatibility shim for legacy ``notebooklm._core`` imports.

The concrete session implementation lives in :mod:`notebooklm._session`.
This module intentionally re-exports the old private import surface and keeps
``ClientCore`` as an alias for callers that have not migrated yet.
"""

from __future__ import annotations

from . import _session as _session
from .rpc import decode_response as decode_response

for _name in dir(_session):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_session, _name)
del _name

ClientCore = _session.Session
Session = _session.Session
asyncio = _session.asyncio
is_auth_error = _session.is_auth_error
save_cookies_to_storage = _session.save_cookies_to_storage
_rotate_cookies = _session._rotate_cookies

# Named private imports are preserved as attributes on this compatibility
# module, but star-imports should not advertise underscore-prefixed internals.
__all__ = sorted(name for name in globals() if not name.startswith("_"))
