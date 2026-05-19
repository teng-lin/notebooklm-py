"""Public-ish request-shape aliases for the Tier-12 middleware chain.

This module promotes a small set of types that were previously private to
``_core_transport.py`` so the Tier-12 middleware chain (introduced in PRs
12.1–12.9) can refer to them without reaching across underscore-prefixed
seams. The originals (``_AuthSnapshot`` and ``_BuildRequest`` in
``_core_transport.py``) remain in place; this module simply exposes them
under names without a leading underscore. PR 12.9 collapses the aliases by
relocating the definitions into this module and deleting the underscore
originals.

Three names live here:

- :data:`AuthSnapshot` — point-in-time view of auth headers used to build
  one HTTP attempt; alias for :class:`notebooklm._core_transport._AuthSnapshot`.
  ADR-009 pins this as the public input type of the
  ``AuthRefreshMiddleware`` callbacks.
- :data:`BuildRequest` — sync callable that maps an ``AuthSnapshot`` to a
  ``(url, body, headers)`` tuple ready for the transport. Alias for the
  existing ``_BuildRequest`` callable type. The chain leaf in PR 12.2 reads
  the callable from ``RpcRequest.context["build_request"]`` and invokes it
  inside ``AuthedTransport.perform_authed_post``.
- :class:`BuildRequestResult` — the *named* dataclass form of the same
  ``(url, body, headers)`` triple, introduced for PR 12.8's
  ``AuthRefreshMiddleware.build_request_factory`` callback. The dataclass
  shape is preferred for new code (named fields, immutable, type-checked
  at construction) over the legacy tuple return. Existing callers continue
  to use the tuple shape until they migrate.

The underscore-prefixed originals (``_AuthSnapshot``, ``_BuildRequest``)
are imported here too so callers that want the private name during the
one-cycle transition can write ``from notebooklm._request_types import
_AuthSnapshot``. They are deliberately excluded from ``__all__`` so
star-imports do not propagate the private names.

See ``docs/adr/0009-middleware-chain.md`` for the full chain contract and
``.sisyphus/plans/tier-12-13-greenfield-migration.md`` section 2 for the
PR sequence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# Re-exported under public names below. The underscore originals are also
# kept importable from this module (see ``__all__`` note above).
from ._core_transport import _AuthSnapshot, _BuildRequest

#: Point-in-time view of auth headers used to build one HTTP attempt.
#:
#: Alias for :class:`notebooklm._core_transport._AuthSnapshot`. The underscore
#: original remains the canonical definition site for one cycle; PR 12.9
#: collapses the alias by relocating the dataclass definition here.
AuthSnapshot = _AuthSnapshot

#: Build-request factory callable type used by the transport and chain leaf.
#:
#: Receives a fresh ``AuthSnapshot`` and returns a ``(url, body, headers)``
#: tuple for one HTTP attempt. Alias for the existing
#: :data:`notebooklm._core_transport._BuildRequest` type. Carried in
#: :attr:`notebooklm._middleware.RpcRequest.context` under the
#: ``"build_request"`` key (PR 12.2 wires the chain leaf).
BuildRequest = _BuildRequest


@dataclass(frozen=True)
class BuildRequestResult:
    """Named dataclass form of the ``(url, body, headers)`` request triple.

    Introduced for the Tier-12 ``AuthRefreshMiddleware`` (ADR-009, PR 12.8):
    the middleware's ``build_request_factory`` callback returns this dataclass
    instead of the legacy ``(url, body, headers)`` tuple so the constructor
    signature reads as a single named value rather than positional unpacking.

    The fields mirror the tuple's positional order:

    - ``url`` — fully-built ``batchexecute`` URL (including ``authuser`` and
      ``_reqid`` query params).
    - ``body`` — encoded ``batchexecute`` body. Pinned to :class:`bytes` in
      ADR-009; the legacy ``_BuildRequest`` tuple accepts ``str | bytes`` for
      backward compatibility with existing call sites that build the body as
      a UTF-8 string.
    - ``headers`` — extra headers to merge for this request, or ``None`` when
      the snapshot's headers are sufficient.

    Frozen so a middleware cannot accidentally mutate a callback's return
    value before passing it back to the chain. Equality is value-based so
    tests can assert against expected results without identity tracking.
    """

    url: str
    body: bytes
    headers: Mapping[str, str] | None


# Only the public-named symbols are part of the module's documented API.
# ``_AuthSnapshot`` and ``_BuildRequest`` remain importable from this module
# (because they are bound at module scope by the ``from ._core_transport ...``
# line above) but are excluded from ``__all__`` so ``from notebooklm._request_types
# import *`` does not propagate the private names.
__all__ = [
    "AuthSnapshot",
    "BuildRequest",
    "BuildRequestResult",
]
