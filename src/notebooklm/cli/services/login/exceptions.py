"""Exceptions for the ``cli/services/login/`` sub-package.

These exceptions let login service modules signal configuration errors
without taking a runtime dependency on Click. The calling Click commands
in :mod:`notebooklm.cli.session_cmd` and :mod:`notebooklm.cli.profile_cmd`
catch these and translate them to ``click.ClickException`` (or the JSON
error envelope) at the boundary — see ADR-0015.
"""

from __future__ import annotations


class LoginConfigurationError(Exception):
    """Raised when login-service input fails validation.

    Used by :func:`notebooklm.cli.services.login.profile_targets._validate_profile_name`
    (and any other login-service validator) to report user-facing
    configuration problems without reaching into Click. The Click command
    layer wraps this into the appropriate boundary error (typically
    ``click.ClickException`` for human output or the JSON error envelope
    under ``--json``).

    Attributes:
        message: User-facing error message describing what's wrong.
        hint: Optional follow-up sentence with remediation advice (e.g.
            allowed character set, format example).
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
