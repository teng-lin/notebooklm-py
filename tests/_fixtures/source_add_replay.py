"""Shared oracle for the P10 source-add failure replay contract.

P10 R3.1 replaced the ``ErrorMode.RAW_PASSTHROUGH`` object-identity contract
(``caught.value is error``) with a *value* contract: the source-add family
captures its public leaf as a :class:`SourceAddFailureRecord` and
``_backend_compat`` replays an equal — never identical — exception at the
facade. This module is the pin for "equal": every field below is captured by
``_web/failure_projection.py`` and restored by
``_backend_compat._project_source_add_record``.

It lives here rather than in one test module because both the surviving custom
rows and the workflows P10 hoists above the port (R3.2's ``source.add_text``
first) have to be held to the same oracle.
"""

from __future__ import annotations

_MISSING = object()

#: Every public attribute ``SourceAddFailureRecord`` carries for a source-add
#: leaf. ``getattr`` with a sentinel so a field absent on both sides matches and
#: a field present on only one side fails.
_REPLAYED_ATTRIBUTES: tuple[str, ...] = (
    # RPCError diagnostics
    "method_id",
    "rpc_id",
    "raw_response",
    "rpc_code",
    "found_ids",
    # upload/registration tagging (raise_partial_upload_failure)
    "source_id",
    "stage",
    "unconfirmed",
    # SourceAddError
    "url",
    "cause",
    # per-family fields
    "recoverable",
    "retry_after",
    "status_code",
    "timeout_seconds",
    "limit_bytes",
    "bytes_read",
    "status",
    "timeout",
    "last_status",
    "path",
    "source",
    "data_at_failure",
    "original_error",
)


def _assert_replays(replayed: BaseException | None, original: BaseException | None) -> None:
    """Assert one public failure graph was reconstructed field-for-field."""
    if original is None or replayed is None:
        assert replayed is None and original is None
        return
    assert type(replayed) is type(original)
    assert replayed.args == original.args
    assert str(replayed) == str(original)
    for name in _REPLAYED_ATTRIBUTES:
        expected = getattr(original, name, _MISSING)
        actual = getattr(replayed, name, _MISSING)
        if isinstance(expected, BaseException) or isinstance(actual, BaseException):
            _assert_replays(
                actual if isinstance(actual, BaseException) else None,
                expected if isinstance(expected, BaseException) else None,
            )
            continue
        assert actual == expected, name
    assert replayed.__suppress_context__ == original.__suppress_context__
    _assert_replays(replayed.__cause__, original.__cause__)
    _assert_replays(replayed.__context__, original.__context__)


__all__ = ["assert_replays"]

#: Public alias; the leading-underscore name is kept for the recursive calls
#: above so the extracted block stays byte-identical to its origin.
assert_replays = _assert_replays
