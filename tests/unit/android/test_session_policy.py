from __future__ import annotations

import pytest

from notebooklm._android.notebooks import GET_PROJECT_METHOD
from notebooklm._android.retry_policy import ANDROID_RETRY_MANIFEST
from notebooklm._android.session import _resolve_replay_safe, classify_raw_replay

_CREATE_NOTE_METHOD = next(
    method for method in ANDROID_RETRY_MANIFEST if method.endswith("/CreateNote")
)
_UNKNOWN_METHOD = "/new.Service/NewMethod"


def test_typed_session_replay_is_bounded_by_the_pr5_manifest() -> None:
    assert _resolve_replay_safe(GET_PROJECT_METHOD, True, None, None) is True
    assert _resolve_replay_safe(GET_PROJECT_METHOD, False, None, None) is False
    assert _resolve_replay_safe(_CREATE_NOTE_METHOD, True, None, None) is False
    assert _resolve_replay_safe(_UNKNOWN_METHOD, True, None, None) is False


def test_raw_replay_capability_preserves_an_explicit_unknown_safe_read() -> None:
    capability = classify_raw_replay(True)

    assert _resolve_replay_safe(_UNKNOWN_METHOD, True, None, capability) is True
    with pytest.raises(ValueError, match="disagrees with raw classification"):
        _resolve_replay_safe(_UNKNOWN_METHOD, False, None, capability)
