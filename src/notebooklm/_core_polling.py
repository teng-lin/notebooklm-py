"""Compatibility shim for polling registry imports.

The registry moved out of ``_core`` ownership in Tier 13 PR 13.3. Keep this
module as the legacy import path for first-party tests and downstream private
imports while new code imports from :mod:`notebooklm._polling_registry`.
"""

from ._polling_registry import PendingPoll, PendingPolls, PollKey, PollRegistry

__all__ = [
    "PendingPoll",
    "PendingPolls",
    "PollKey",
    "PollRegistry",
]
