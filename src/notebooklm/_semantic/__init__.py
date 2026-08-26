"""The transport-neutral semantic layer: records, projectors, and the port.

Everything under this package sits *above* the wire and below the public
facades. It is the vocabulary the semantic backend port speaks — neutral
records and operation definitions (:mod:`._semantic.records`), the
record-to-public projectors the facades call, and the closed mapping from a
backend error back to the public exception a migrated facade must raise.

Moved here wholesale by programme P10 R7 (``docs/plan/2026-08-25-p10-semantic-
remediation.md``) from the flat ``src/notebooklm/_*.py`` root, which had grown
27 modules that were indistinguishable from the facades beside them. Nothing
re-exports through this ``__init__``: importers name the module they need, so
the package boundary carries no import cost and no shim.
"""

from __future__ import annotations
