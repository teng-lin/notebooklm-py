"""REST configuration for shared Android profile support."""

from .._app.profiles import configured_profiles as configured_profiles
from .._app.profiles import profile_startup_timeout as _profile_startup_timeout

PROFILES_ENV = "NOTEBOOKLM_SERVER_PROFILES"
PROFILE_HEADER = "X-NotebookLM-Profile"
PROFILE_STARTUP_TIMEOUT_ENV = "NOTEBOOKLM_SERVER_PROFILE_STARTUP_TIMEOUT"


def profile_startup_timeout() -> float:
    """Resolve the REST-specific startup and recovery deadline."""
    return _profile_startup_timeout(PROFILE_STARTUP_TIMEOUT_ENV)
