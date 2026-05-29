"""Public runtime configuration surface.

This module is the stable import surface for configuration helpers. The
resolution logic lives in :mod:`notebooklm._env`; only the names re-exported
here are public API.
"""

from ._env import (
    DEFAULT_BASE_URL,
    ENTERPRISE_BASE_HOST,
    PERSONAL_BASE_HOST,
    get_base_host,
    get_base_url,
    get_default_language,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "ENTERPRISE_BASE_HOST",
    "get_base_host",
    "get_base_url",
    "get_default_language",
    "PERSONAL_BASE_HOST",
]
