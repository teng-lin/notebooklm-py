"""Public logging helpers (re-exports from internal _logging).

Named ``log`` (not ``logging``) to avoid shadowing the stdlib ``logging`` module
for users doing ``from notebooklm import logging``.
"""

from ._logging import install_redaction

__all__ = ["install_redaction"]
