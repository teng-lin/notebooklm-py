"""The transport-neutral semantic services.

Each module owns one domain's workflows over the semantic port: it invokes
typed operation definitions through ``BackendAdapter``, composes the multi-leaf
workflows under a single deadline, and returns neutral records. None of them
may name the wire, the public models, or the projection layer — invariant I1 in
``docs/plan/2026-08-25-p10-semantic-remediation.md``, enforced by
``tests/_guardrails/test_service_boundary.py``, which discovers the governed
set from this directory. Projection to public models happens above, in the
facades that call these services.

P10 R7.2 moved them here from the flat package root, where ten
``_*_service.py`` modules sat indistinguishable from the facades beside them.
"""

from __future__ import annotations
