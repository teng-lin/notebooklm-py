"""Public I/O helpers (re-exports from internal _atomic_io).

Exists so :mod:`notebooklm.cli` can import I/O helpers without violating
the ``cli/`` boundary rule (no ``notebooklm._*`` imports). See
``tests/unit/test_cli_boundary.py``.
"""

from ._atomic_io import atomic_update_json, atomic_write_json, replace_file_atomically

__all__ = ["atomic_update_json", "atomic_write_json", "replace_file_atomically"]
