"""Transport-neutral type-only contracts shared across feature APIs.

This module defines the narrow structural Protocols feature APIs depend
on. Per ADR-0013, a Protocol lives here only when **shared by ≥2
features**; single-consumer capabilities stay local to their owning
feature module (e.g. ``AuthMetadata`` lives in ``_web/sources/upload.py``).

Only :class:`LoopGuard` remains here. The web-only :class:`Kernel` and
:class:`RpcCaller` contracts live in :mod:`notebooklm._web.contracts`.

Feature APIs that need more than one capability take their direct
collaborators by keyword-only constructor argument (``ChatAPI`` in
``_chat.py``, ``ArtifactsAPI`` in ``_artifacts.py``, and
``SourceUploadPipeline`` in ``_web/sources/upload.py``). The feature-local
composite Protocols ``ArtifactsRuntime`` and ``UploadRuntime`` (and
their corresponding adapter dataclasses) that previously bundled three
capability Protocols apiece were retired once it was clear they only
hid three stable collaborators with exactly one production satisfier.
The single-consumer ``AuthMetadata`` was inlined and the unused
``AsyncWorkRuntime`` composite was deleted in issue #1327 for the same
reason — a Protocol with fewer than two production consumers is
indirection that no production code varies. B0 then made
``CallSupervisor`` the concrete admission authority for artifact polling.
"""

from __future__ import annotations

from typing import Protocol


class LoopGuard(Protocol):
    """Loop-affinity assertion surface for features that own async work."""

    def assert_bound_loop(self) -> None: ...


__all__ = [
    "LoopGuard",
]
