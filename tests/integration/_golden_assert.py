"""Shared golden decoded-row assertion helper.

Used by ``test_golden_decoded_vcr.py`` and ``test_golden_decoded_vcr_expansion.py``
(extracted so the two modules don't cross-import — see the
``tests/_guardrails/test_no_cross_test_imports.py`` gate: a shared helper belongs
in a ``_``-prefixed non-test module both test modules import).
"""

from __future__ import annotations

import reprlib
from typing import Any


def assert_decoded_equals(actual: Any, expected: Any, *, field: str) -> None:
    """Pin one decoded leaf value, with a decode-drift-flavoured failure message.

    A thin wrapper over ``assert actual == expected`` whose only value is the
    message: when a golden value diverges it is almost always a *decoder*
    regression (a positional column mis-map or a leaf-shape change in the
    recorded response), not a test bug — the message says so, and names the
    field, so the failure points at the right place. ``field`` is a
    human-readable label like ``"artifacts_list[0].kind"``.
    """
    assert actual == expected, (
        f"Decoded golden value drift for {field}: "
        f"expected {reprlib.repr(expected)}, got {reprlib.repr(actual)}. "
        "The cassette replayed but the decoder produced a different leaf value than the "
        "golden recording — likely a positional mis-map (row-adapter column moved) or a "
        "leaf-shape change in the recorded response. If the cassette was deliberately "
        "re-recorded against a different notebook, refresh the golden value here."
    )
