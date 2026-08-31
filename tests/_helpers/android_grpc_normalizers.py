"""Per-RPC request normalizers for Android gRPC cassettes.

Cassette replay matches requests byte-for-byte after sanitization. A few
requests carry values the client mints at random on every call (nonces), which
would never match a recording. This module clears exactly those fields, on
both the record and the replay side, so the remaining request bytes stay a
strict contract. It is a small audited table keyed by full method path — not a
generic field-policy engine — and it never touches responses.
"""

from __future__ import annotations

from google.protobuf.message import Message

from notebooklm._android.chat import GENERATE_FREE_FORM_STREAMED_METHOD

from .android_grpc_cassette import MessageDirection

# method path -> request fields the client fills with a fresh random value per call
REQUEST_NONCE_FIELDS: dict[str, tuple[str, ...]] = {
    # ``AndroidChatAPI._stream_answer`` mints ``user_message_id`` via ``uuid4``.
    GENERATE_FREE_FORM_STREAMED_METHOD: ("user_message_id",),
}


def normalize_request(method: str, direction: MessageDirection, message: Message) -> Message:
    """Clear client nonce fields from ``message`` when it is a known request."""

    if direction != "request":
        return message
    for field_name in REQUEST_NONCE_FIELDS.get(method, ()):
        if message.DESCRIPTOR.fields_by_name.get(field_name) is None:
            raise TypeError(
                f"{method} request {message.DESCRIPTOR.full_name} has no field {field_name!r}"
            )
        message.ClearField(field_name)
    return message


__all__ = ["REQUEST_NONCE_FIELDS", "normalize_request"]
