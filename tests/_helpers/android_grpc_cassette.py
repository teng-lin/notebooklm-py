"""Deterministic record/replay seam for Android ``grpc.aio`` integration tests.

This is deliberately not a vcrpy adapter.  vcrpy intercepts HTTP transports,
whereas ``grpc.aio`` performs I/O in gRPC C-core.  These adapters implement the
small channel surface used by :class:`notebooklm._android.session.AndroidSession`
and are injected through its existing ``grpc_loader`` seam.

The persisted format contains redacted protobuf messages and, for explicitly
allowlisted application headers, metadata *key names* only. Credential metadata
and every metadata value (including the bearer and experiment token) are never
copied into the cassette model. Recording requires an explicit
application-level sanitizer, then always applies :class:`ProtoRedactor` as the
final security boundary. It replaces every user-controlled scalar while
preserving message shape and enum values.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

from notebooklm._android.auth import BearerCredential
from notebooklm._android.phenotype import CLIENT_TYPE_HEADER, EXPERIMENT_TOKEN_HEADER
from notebooklm._android.session import ANDROID_GRPC_TARGET
from notebooklm._loop_bound import LoopBoundPrimitive

CASSETTE_FORMAT = "notebooklm.android.grpc-cassette"
CASSETTE_VERSION = 1

RpcShape = Literal["unary_unary", "unary_stream"]
MessageDirection = Literal["request", "response"]

_METHOD_RE = re.compile(r"^/[A-Za-z_][A-Za-z0-9_.]*/[A-Za-z_][A-Za-z0-9_]*$")
_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SAFE_STRING_RE = re.compile(r"^SCRUBBED_(STRING|REQUEST)_[0-9]{4}$")
_SAFE_BYTES_RE = re.compile(rb"^SCRUBBED_(BYTES|REQUEST_BYTES)_[0-9]{4}$")
# A ``SourceId{id: "<uuid>"}`` message serialized into a ``bytes`` field
# (field 1, length 36): GetLabels source members arrive in this shape.
_WRAPPED_UUID_RE = re.compile(rb"^\x0a\x24([0-9a-fA-F-]{36})$")
_SAFE_URL_PREFIX = "https://example.invalid/grpc-cassette/url-"
_RECORDED_APPLICATION_METADATA_KEYS = frozenset({CLIENT_TYPE_HEADER, EXPERIMENT_TOKEN_HEADER})
# Integer fields that carry a schema-defined status code rather than data.
# The recovered proto declares them as ``int32`` instead of an enum; their
# meaning is pinned in ``notebooklm._types.research`` (RESEARCH_STATUS_CODE_*).
# Collapsing them to ``1`` would turn every recorded research run into
# "in progress" and make polling loops unreplayable.
_PRESERVED_CODE_FIELDS = frozenset(
    {"google.internal.labs.tailwind.orchestration.v1.ResearchJobInfo.status"}
)
_NUMERIC_TYPES = frozenset(
    {
        FieldDescriptor.TYPE_DOUBLE,
        FieldDescriptor.TYPE_FLOAT,
        FieldDescriptor.TYPE_INT64,
        FieldDescriptor.TYPE_UINT64,
        FieldDescriptor.TYPE_INT32,
        FieldDescriptor.TYPE_FIXED64,
        FieldDescriptor.TYPE_FIXED32,
        FieldDescriptor.TYPE_UINT32,
        FieldDescriptor.TYPE_SFIXED32,
        FieldDescriptor.TYPE_SFIXED64,
        FieldDescriptor.TYPE_SINT32,
        FieldDescriptor.TYPE_SINT64,
    }
)


class AndroidGrpcCassetteError(ValueError):
    """A cassette is malformed or cannot safely represent a call."""


class AndroidGrpcCassetteMismatch(BaseException):
    """Replay did not match the recorded method, shape, or protobuf payload.

    Deliberately *not* an ``Exception``: ``AndroidSession`` maps every
    ``Exception`` raised by the channel into a sanitized gRPC status, which
    would collapse a cassette mismatch into ``RPCError(... UNKNOWN)`` and lose
    the diagnostic. Deriving from ``BaseException`` lets the mismatch escape
    that mapping and reach the test verbatim, like a cancellation would.
    """


class MessageSanitizer(Protocol):
    """Return a sanitized message of the same protobuf type."""

    def __call__(
        self,
        method: str,
        direction: MessageDirection,
        message: Message,
    ) -> Message: ...


def _exact_keys(value: dict[str, Any], expected: set[str], *, location: str) -> None:
    actual = set(value)
    if actual != expected:
        raise AndroidGrpcCassetteError(
            f"{location} keys must be {sorted(expected)!r}, got {sorted(actual)!r}"
        )


def _message_type(message: Message) -> str:
    descriptor = getattr(message, "DESCRIPTOR", None)
    type_name = getattr(descriptor, "full_name", None)
    if not isinstance(type_name, str) or not _TYPE_RE.fullmatch(type_name):
        raise AndroidGrpcCassetteError("Cassette messages must have a protobuf FQN")
    return type_name


def _deserializer_type(deserializer: Callable[[bytes], Message]) -> str:
    message_type = getattr(deserializer, "__self__", None)
    descriptor = getattr(message_type, "DESCRIPTOR", None)
    type_name = getattr(descriptor, "full_name", None)
    if not isinstance(type_name, str) or not _TYPE_RE.fullmatch(type_name):
        raise AndroidGrpcCassetteError(
            "AndroidSession response deserializer does not expose a protobuf FQN"
        )
    return type_name


def _application_metadata_keys(metadata: Any) -> tuple[str, ...] | None:
    """Return safe application metadata key names, never values or credentials."""

    if metadata is None:
        return None
    keys = sorted(
        str(item[0]).casefold()
        for item in metadata
        if isinstance(item, (tuple, list))
        and len(item) == 2
        and str(item[0]).casefold() in _RECORDED_APPLICATION_METADATA_KEYS
    )
    return tuple(keys) or None


_PLACEHOLDER_BUDGET = 9999


def _check_placeholder_budget(count: int) -> None:
    """The trusted-placeholder patterns admit exactly four digits."""

    if count > _PLACEHOLDER_BUDGET:
        raise AndroidGrpcCassetteError(
            f"More than {_PLACEHOLDER_BUDGET} distinct scalars in one cassette; "
            "split the family before placeholders stop matching the trusted pattern"
        )


def _clone(message: Message) -> Message:
    clone = cast(Message, type(message)())
    clone.CopyFrom(message)
    clone.DiscardUnknownFields()
    return clone


class ProtoRedactor:
    """Stateful, idempotent redaction for arbitrary protobuf messages.

    Every string, byte string, integer, and float is potentially identifying.
    Strings and bytes receive stable encounter-order placeholders so equality
    relationships (for example, request ID == response ID) survive replay.
    UUIDs and URLs use syntactically valid reserved placeholders.  Numeric
    values become one; booleans and schema-defined enum values are retained.
    A non-zero numeric placeholder preserves proto3 scalar presence on the
    serialized wire. Unknown fields are discarded before traversal so an
    undeclared wire field cannot bypass the sanitizer.
    """

    def __init__(self, *, trust_placeholders: bool = False) -> None:
        # Response-side only: requests always go through ``_RequestScope``,
        # which must pass placeholders through so replay requests (which carry
        # placeholders by construction) compare equal to the recording.
        self._trust_placeholders = trust_placeholders
        self._strings: dict[str, str] = {}
        self._bytes: dict[bytes, bytes] = {}
        self._uuid_count = 0
        self._url_count = 0
        self._string_count = 0
        self._bytes_count = 0

    def __call__(
        self,
        method: str,
        direction: MessageDirection,
        message: Message,
    ) -> Message:
        del method
        sanitized = _clone(message)
        if direction == "request":
            # Requests are matched byte-for-byte on replay, so their placeholders
            # must not depend on how much response traffic preceded them.
            # Known values (reservations, ids echoed by earlier responses) keep
            # their global placeholder; anything else -- client constants such
            # as action names or language codes -- is numbered per request.
            _RequestScope(self)._sanitize_message(sanitized)
        else:
            self._sanitize_message(sanitized)
        return sanitized

    @property
    def trusts_placeholders(self) -> bool:
        return self._trust_placeholders

    def known_string(self, value: str) -> str | None:
        return self._strings.get(value)

    def known_bytes(self, value: bytes) -> bytes | None:
        return self._bytes.get(value)

    def reserve(self, value: str) -> str:
        """Assign ``value`` its placeholder now, before any traffic is seen.

        Placeholders are numbered in encounter order, so a replay test cannot
        otherwise know which placeholder a test input (notebook id, question)
        received during recording. Reserving the inputs in a fixed order in
        both modes makes the sequence deterministic; the returned placeholder
        is what the replay side passes to the public API.
        """

        if not isinstance(value, str) or not value:
            raise AndroidGrpcCassetteError("Only non-empty strings can be reserved")
        return self._sanitize_string(value)

    def _sanitize_string(self, value: str) -> str:
        if not value:
            return value
        if self._trust_placeholders:
            trusted = self._trust_string_placeholder(value)
            if trusted:
                return value
        existing = self._strings.get(value)
        if existing is not None:
            return existing
        if _UUID_RE.fullmatch(value):
            self._uuid_count += 1
            replacement = f"00000000-0000-4000-8000-{self._uuid_count:012d}"
        else:
            parsed = urlsplit(value)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                self._url_count += 1
                _check_placeholder_budget(self._url_count)
                replacement = f"{_SAFE_URL_PREFIX}{self._url_count:04d}"
            else:
                self._string_count += 1
                _check_placeholder_budget(self._string_count)
                replacement = f"SCRUBBED_STRING_{self._string_count:04d}"
        self._strings[value] = replacement
        return replacement

    def _trust_string_placeholder(self, value: str) -> bool:
        """Accept an existing placeholder and retire its index from the counters.

        A trusted placeholder must still be recorded and its number reserved;
        otherwise the next genuinely new value would be handed the very same
        placeholder and two distinct entities would alias in the cassette.
        """

        if _SAFE_STRING_RE.fullmatch(value):
            if value.startswith("SCRUBBED_STRING_"):
                self._string_count = max(self._string_count, int(value.rsplit("_", 1)[1]))
        elif value.startswith(_SAFE_URL_PREFIX) and value[len(_SAFE_URL_PREFIX) :].isdigit():
            self._url_count = max(self._url_count, int(value[len(_SAFE_URL_PREFIX) :]))
        elif _UUID_RE.fullmatch(value) and value.startswith("00000000-0000-4000-8000-"):
            self._uuid_count = max(self._uuid_count, int(value[-12:]))
        else:
            return False
        self._strings[value] = value
        return True

    def _sanitize_bytes(self, value: bytes) -> bytes:
        if not value:
            return value
        if self._trust_placeholders and _SAFE_BYTES_RE.fullmatch(value):
            if value.startswith(b"SCRUBBED_BYTES_"):
                self._bytes_count = max(self._bytes_count, int(value.rsplit(b"_", 1)[1]))
            self._bytes[value] = value
            return value
        wrapped = _WRAPPED_UUID_RE.fullmatch(value)
        if wrapped is not None:
            inner = wrapped.group(1).decode("ascii")
            if _UUID_RE.fullmatch(inner):
                return b"\x0a\x24" + self._sanitize_string(inner).encode("ascii")
        # Some recovered fields carry a bare UUID string as ``bytes`` (for
        # example ``GetLabels`` collection ``member_ids``). Keep the UUID shape
        # so decoders that canonicalize the id still accept the placeholder,
        # and share the string table so bytes/string forms of one id agree.
        try:
            text = value.decode("ascii")
        except UnicodeDecodeError:
            text = ""
        if text and _UUID_RE.fullmatch(text):
            return self._sanitize_string(text).encode("ascii")
        existing = self._bytes.get(value)
        if existing is not None:
            return existing
        self._bytes_count += 1
        _check_placeholder_budget(self._bytes_count)
        replacement = f"SCRUBBED_BYTES_{self._bytes_count:04d}".encode()
        self._bytes[value] = replacement
        return replacement

    def _sanitize_scalar(self, field: FieldDescriptor, value: Any) -> Any:
        if field.type == FieldDescriptor.TYPE_STRING:
            return self._sanitize_string(str(value))
        if field.type == FieldDescriptor.TYPE_BYTES:
            return self._sanitize_bytes(bytes(value))
        if field.type in _NUMERIC_TYPES:
            if field.full_name in _PRESERVED_CODE_FIELDS:
                return value
            return (
                1.0
                if field.type in {FieldDescriptor.TYPE_DOUBLE, FieldDescriptor.TYPE_FLOAT}
                else 1
            )
        # Booleans and enum numbers are schema-level semantics, not opaque data.
        return value

    def _sanitize_message(self, message: Message) -> None:
        for field, value in list(message.ListFields()):
            if (
                field.is_repeated
                and field.message_type is not None
                and field.message_type.GetOptions().map_entry
            ):
                key_field = field.message_type.fields_by_name["key"]
                value_field = field.message_type.fields_by_name["value"]
                # Sort so placeholder numbering never depends on map iteration
                # order, which protobuf does not guarantee across processes.
                entries = sorted(value.items(), key=lambda item: repr(item[0]))
                value.clear()
                for key, map_value in entries:
                    clean_key = self._sanitize_scalar(key_field, key)
                    if value_field.type == FieldDescriptor.TYPE_MESSAGE:
                        clean_value = _clone(map_value)
                        self._sanitize_message(clean_value)
                        value[clean_key].CopyFrom(clean_value)
                    else:
                        value[clean_key] = self._sanitize_scalar(value_field, map_value)
                continue
            if field.type == FieldDescriptor.TYPE_MESSAGE:
                if field.is_repeated:
                    for child in value:
                        child.DiscardUnknownFields()
                        self._sanitize_message(child)
                else:
                    value.DiscardUnknownFields()
                    self._sanitize_message(value)
                continue
            if field.is_repeated:
                value[:] = [self._sanitize_scalar(field, item) for item in value]
            else:
                setattr(message, field.name, self._sanitize_scalar(field, value))


class _RequestScope(ProtoRedactor):
    """Request-local placeholder assignment on top of a parent redactor.

    Lookups consult the parent's tables (reservations and values already seen
    in responses) so cross-message identity survives; UUIDs and URLs are
    assigned through the parent for the same reason. Other unknown scalars
    receive ``SCRUBBED_REQUEST_NNNN`` / ``SCRUBBED_REQUEST_BYTES_NNNN``
    placeholders numbered from one within this request, and nothing else is
    written back to the parent. Trusted placeholders always pass through so replay requests, which
    already carry placeholders, compare equal to the recording.
    """

    def __init__(self, parent: ProtoRedactor) -> None:
        super().__init__(trust_placeholders=True)
        self._parent = parent

    def _sanitize_string(self, value: str) -> str:
        if not value:
            return value
        if _SAFE_STRING_RE.fullmatch(value):
            return value
        if value.startswith(_SAFE_URL_PREFIX) and value[len(_SAFE_URL_PREFIX) :].isdigit():
            return value
        if _UUID_RE.fullmatch(value) and value.startswith("00000000-0000-4000-8000-"):
            return value
        known = self._parent.known_string(value)
        if known is not None:
            return known
        parsed = urlsplit(value)
        if _UUID_RE.fullmatch(value) or (parsed.scheme in {"http", "https"} and parsed.netloc):
            # Identifiers keep cross-message identity: a replay request can
            # only carry a placeholder here (reservation or echoed response),
            # so assigning globally costs nothing and lets a later response
            # echo the same placeholder.
            return self._parent._sanitize_string(value)
        existing = self._strings.get(value)
        if existing is not None:
            return existing
        self._string_count += 1
        _check_placeholder_budget(self._string_count)
        replacement = f"SCRUBBED_REQUEST_{self._string_count:04d}"
        self._strings[value] = replacement
        return replacement

    def _sanitize_bytes(self, value: bytes) -> bytes:
        if not value or _SAFE_BYTES_RE.fullmatch(value):
            return value
        wrapped = _WRAPPED_UUID_RE.fullmatch(value)
        if wrapped is not None and _UUID_RE.fullmatch(wrapped.group(1).decode("ascii")):
            return b"\x0a\x24" + self._sanitize_string(wrapped.group(1).decode("ascii")).encode()
        try:
            text = value.decode("ascii")
        except UnicodeDecodeError:
            text = ""
        if text and _UUID_RE.fullmatch(text):
            return self._sanitize_string(text).encode("ascii")
        known = self._parent.known_bytes(value)
        if known is not None:
            return known
        existing = self._bytes.get(value)
        if existing is not None:
            return existing
        self._bytes_count += 1
        _check_placeholder_budget(self._bytes_count)
        replacement = f"SCRUBBED_REQUEST_BYTES_{self._bytes_count:04d}".encode()
        self._bytes[value] = replacement
        return replacement


def compose_sanitizers(*sanitizers: MessageSanitizer) -> MessageSanitizer:
    """Apply ``sanitizers`` left to right, each on the previous result."""

    if not sanitizers:
        raise AndroidGrpcCassetteError("compose_sanitizers needs at least one sanitizer")

    def composed(method: str, direction: MessageDirection, message: Message) -> Message:
        for sanitizer in sanitizers:
            message = sanitizer(method, direction, message)
            if not isinstance(message, Message):
                raise AndroidGrpcCassetteError("Cassette sanitizer must return a protobuf message")
        return message

    return composed


@dataclass(frozen=True)
class ProtoPayload:
    type_name: str
    wire_bytes: bytes

    @classmethod
    def from_message(
        cls,
        method: str,
        direction: MessageDirection,
        message: Message,
        sanitizer: MessageSanitizer,
    ) -> ProtoPayload:
        original_type = _message_type(message)
        sanitized = sanitizer(method, direction, _clone(message))
        if not isinstance(sanitized, Message):
            raise AndroidGrpcCassetteError("Cassette sanitizer must return a protobuf message")
        if _message_type(sanitized) != original_type:
            raise AndroidGrpcCassetteError("Cassette sanitizer changed the protobuf type")
        return cls(
            type_name=original_type,
            wire_bytes=sanitized.SerializeToString(deterministic=True),
        )

    def to_json(self) -> dict[str, str]:
        return {
            "protobuf_b64": base64.b64encode(self.wire_bytes).decode("ascii"),
            "protobuf_type": self.type_name,
        }

    @classmethod
    def from_json(cls, value: Any, *, location: str) -> ProtoPayload:
        if not isinstance(value, dict):
            raise AndroidGrpcCassetteError(f"{location} must be an object")
        _exact_keys(value, {"protobuf_b64", "protobuf_type"}, location=location)
        type_name = value["protobuf_type"]
        encoded = value["protobuf_b64"]
        if not isinstance(type_name, str) or not _TYPE_RE.fullmatch(type_name):
            raise AndroidGrpcCassetteError(f"{location}.protobuf_type is not a protobuf FQN")
        if not isinstance(encoded, str):
            raise AndroidGrpcCassetteError(f"{location}.protobuf_b64 must be text")
        try:
            wire_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            raise AndroidGrpcCassetteError(
                f"{location}.protobuf_b64 is not canonical base64"
            ) from None
        if base64.b64encode(wire_bytes).decode("ascii") != encoded:
            raise AndroidGrpcCassetteError(f"{location}.protobuf_b64 is not canonical base64")
        return cls(type_name=type_name, wire_bytes=wire_bytes)


@dataclass(frozen=True)
class GrpcInteraction:
    method: str
    shape: RpcShape
    request: ProtoPayload
    response_type: str
    responses: tuple[ProtoPayload, ...]
    application_metadata_keys: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not _METHOD_RE.fullmatch(self.method):
            raise AndroidGrpcCassetteError(f"Invalid full gRPC method path: {self.method!r}")
        if self.shape not in {"unary_unary", "unary_stream"}:
            raise AndroidGrpcCassetteError(f"Unsupported gRPC shape: {self.shape!r}")
        if not _TYPE_RE.fullmatch(self.response_type):
            raise AndroidGrpcCassetteError("Interaction response type is not a protobuf FQN")
        if self.shape == "unary_unary" and len(self.responses) != 1:
            raise AndroidGrpcCassetteError("unary_unary interactions require exactly one response")
        if any(response.type_name != self.response_type for response in self.responses):
            raise AndroidGrpcCassetteError(
                "Interaction response payload type does not match its pinned protobuf type"
            )
        if self.application_metadata_keys is not None:
            if not self.application_metadata_keys or (
                tuple(sorted(set(self.application_metadata_keys))) != self.application_metadata_keys
            ):
                raise AndroidGrpcCassetteError(
                    "Interaction application metadata keys must be non-empty, sorted, and unique"
                )
            if any(
                key not in _RECORDED_APPLICATION_METADATA_KEYS
                for key in self.application_metadata_keys
            ):
                raise AndroidGrpcCassetteError(
                    "Interaction carries a non-allowlisted application metadata key"
                )

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "method": self.method,
            "request": self.request.to_json(),
            "response_protobuf_type": self.response_type,
            "responses": [response.to_json() for response in self.responses],
            "shape": self.shape,
        }
        if self.application_metadata_keys is not None:
            value["application_metadata_keys"] = list(self.application_metadata_keys)
        return value

    @classmethod
    def from_json(cls, value: Any, *, index: int) -> GrpcInteraction:
        location = f"interactions[{index}]"
        if not isinstance(value, dict):
            raise AndroidGrpcCassetteError(f"{location} must be an object")
        required_keys = {"method", "request", "response_protobuf_type", "responses", "shape"}
        actual_keys = set(value)
        allowed_keys = required_keys | {"application_metadata_keys"}
        if not required_keys <= actual_keys or not actual_keys <= allowed_keys:
            raise AndroidGrpcCassetteError(
                f"{location} keys must be {sorted(required_keys)!r} with optional "
                "'application_metadata_keys'"
            )
        method = value["method"]
        shape = value["shape"]
        response_type = value["response_protobuf_type"]
        responses = value["responses"]
        metadata_keys: tuple[str, ...] | None = None
        if "application_metadata_keys" in value:
            raw_metadata_keys = value["application_metadata_keys"]
            if not isinstance(raw_metadata_keys, list) or not all(
                isinstance(item, str) for item in raw_metadata_keys
            ):
                raise AndroidGrpcCassetteError(
                    f"{location}.application_metadata_keys must be a list of text keys"
                )
            metadata_keys = tuple(raw_metadata_keys)
        if not all(isinstance(item, str) for item in (method, shape, response_type)):
            raise AndroidGrpcCassetteError(
                f"{location} method, shape, and response type must be text"
            )
        if not isinstance(responses, list):
            raise AndroidGrpcCassetteError(f"{location}.responses must be a list")
        return cls(
            method=method,
            shape=cast(RpcShape, shape),
            request=ProtoPayload.from_json(value["request"], location=f"{location}.request"),
            response_type=response_type,
            responses=tuple(
                ProtoPayload.from_json(item, location=f"{location}.responses[{response_index}]")
                for response_index, item in enumerate(responses)
            ),
            application_metadata_keys=metadata_keys,
        )


@dataclass(frozen=True)
class AndroidGrpcCassette:
    interactions: tuple[GrpcInteraction, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "format": CASSETTE_FORMAT,
            "interactions": [interaction.to_json() for interaction in self.interactions],
            "version": CASSETTE_VERSION,
        }

    @classmethod
    def load(cls, path: Path) -> AndroidGrpcCassette:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AndroidGrpcCassetteError(f"Could not load Android gRPC cassette: {path}") from exc
        if not isinstance(value, dict):
            raise AndroidGrpcCassetteError("Android gRPC cassette must be an object")
        _exact_keys(value, {"format", "interactions", "version"}, location="cassette")
        if value["format"] != CASSETTE_FORMAT or value["version"] != CASSETTE_VERSION:
            raise AndroidGrpcCassetteError("Unsupported Android gRPC cassette format/version")
        interactions = value["interactions"]
        if not isinstance(interactions, list):
            raise AndroidGrpcCassetteError("cassette.interactions must be a list")
        return cls(
            interactions=tuple(
                GrpcInteraction.from_json(item, index=index)
                for index, item in enumerate(interactions)
            )
        )

    def dump(self, path: Path) -> None:
        serialized = json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


class _Recorder:
    def __init__(
        self,
        path: Path,
        sanitizer: MessageSanitizer,
        redactor: ProtoRedactor | None = None,
    ) -> None:
        if not callable(sanitizer):
            raise TypeError("Android gRPC recording requires an explicit protobuf sanitizer")
        self.path = path
        self._custom_sanitizer = sanitizer
        # A caller-provided sanitizer may preserve fields needed for a focused
        # assertion, but it is never the cassette's security boundary. Always
        # finish with the repository's exhaustive scalar redactor so an
        # identity or partial custom pass cannot persist private protobuf data.
        # A caller may supply that redactor only to pre-reserve placeholders
        # for known test inputs; it must still trust placeholders so values the
        # custom pass already scrubbed are not renumbered.
        if redactor is None:
            redactor = ProtoRedactor(trust_placeholders=True)
        elif not isinstance(redactor, ProtoRedactor) or not redactor.trusts_placeholders:
            raise AndroidGrpcCassetteError(
                "Recording redactor must be a ProtoRedactor(trust_placeholders=True)"
            )
        self._mandatory_redactor = redactor
        self.interactions: list[GrpcInteraction] = []

    def _sanitize(
        self,
        method: str,
        direction: MessageDirection,
        message: Message,
    ) -> Message:
        customized = self._custom_sanitizer(method, direction, _clone(message))
        if not isinstance(customized, Message):
            raise AndroidGrpcCassetteError("Cassette sanitizer must return a protobuf message")
        if _message_type(customized) != _message_type(message):
            raise AndroidGrpcCassetteError("Cassette sanitizer changed the protobuf type")
        return self._mandatory_redactor(method, direction, customized)

    def payload(
        self,
        method: str,
        direction: MessageDirection,
        message: Message,
    ) -> ProtoPayload:
        return ProtoPayload.from_message(method, direction, message, self._sanitize)

    def append(self, interaction: GrpcInteraction) -> None:
        self.interactions.append(interaction)
        AndroidGrpcCassette(tuple(self.interactions)).dump(self.path)


class _RecordingStreamCall:
    def __init__(
        self,
        call: Any,
        recorder: _Recorder,
        method: str,
        request: ProtoPayload,
        response_type: str,
        application_metadata_keys: tuple[str, ...] | None,
    ) -> None:
        self._call = call
        self._iterator = call.__aiter__()
        self._recorder = recorder
        self._method = method
        self._request = request
        self._response_type = response_type
        self._application_metadata_keys = application_metadata_keys
        self._responses: list[ProtoPayload] = []
        self._complete = False

    def __aiter__(self) -> _RecordingStreamCall:
        return self

    async def __anext__(self) -> Message:
        try:
            item = await self._iterator.__anext__()
        except StopAsyncIteration:
            if not self._complete:
                self._recorder.append(
                    GrpcInteraction(
                        method=self._method,
                        shape="unary_stream",
                        request=self._request,
                        response_type=self._response_type,
                        responses=tuple(self._responses),
                        application_metadata_keys=self._application_metadata_keys,
                    )
                )
                self._complete = True
            raise
        payload = self._recorder.payload(self._method, "response", item)
        if payload.type_name != self._response_type:
            raise AndroidGrpcCassetteError("Live stream returned an unexpected protobuf type")
        self._responses.append(payload)
        return item

    def cancel(self) -> Any:
        return self._call.cancel()


class _RecordingChannel:
    def __init__(self, channel: Any, recorder: _Recorder) -> None:
        self._channel = channel
        self._recorder = recorder

    def unary_unary(
        self, method: str, *, request_serializer: Any, response_deserializer: Any
    ) -> Any:
        live = self._channel.unary_unary(
            method,
            request_serializer=request_serializer,
            response_deserializer=response_deserializer,
        )
        response_type = _deserializer_type(response_deserializer)

        async def invoke(request: Message, *, metadata: Any, timeout: float | None) -> Message:
            request_payload = self._recorder.payload(method, "request", request)
            application_metadata_keys = _application_metadata_keys(metadata)
            response = await live(request, metadata=metadata, timeout=timeout)
            response_payload = self._recorder.payload(method, "response", response)
            if response_payload.type_name != response_type:
                raise AndroidGrpcCassetteError(
                    "Live unary call returned an unexpected protobuf type"
                )
            self._recorder.append(
                GrpcInteraction(
                    method=method,
                    shape="unary_unary",
                    request=request_payload,
                    response_type=response_type,
                    responses=(response_payload,),
                    application_metadata_keys=application_metadata_keys,
                )
            )
            return response

        return invoke

    def unary_stream(
        self, method: str, *, request_serializer: Any, response_deserializer: Any
    ) -> Any:
        live = self._channel.unary_stream(
            method,
            request_serializer=request_serializer,
            response_deserializer=response_deserializer,
        )
        response_type = _deserializer_type(response_deserializer)

        def invoke(request: Message, *, metadata: Any, timeout: float | None) -> Any:
            request_payload = self._recorder.payload(method, "request", request)
            application_metadata_keys = _application_metadata_keys(metadata)
            call = live(request, metadata=metadata, timeout=timeout)
            return _RecordingStreamCall(
                call,
                self._recorder,
                method,
                request_payload,
                response_type,
                application_metadata_keys,
            )

        return invoke

    async def close(self) -> None:
        await self._channel.close()


class RecordingGrpcModule:
    """Wrap a real grpc module and persist successful sanitized calls."""

    def __init__(
        self,
        live_grpc: Any,
        path: Path,
        *,
        sanitizer: MessageSanitizer,
        redactor: ProtoRedactor | None = None,
    ) -> None:
        self._live_grpc = live_grpc
        self._recorder = _Recorder(path, sanitizer, redactor)
        self.aio = SimpleNamespace(secure_channel=self.secure_channel)

    def ssl_channel_credentials(self) -> Any:
        return self._live_grpc.ssl_channel_credentials()

    def secure_channel(
        self, target: str, credentials: Any, *, options: Any = None
    ) -> _RecordingChannel:
        channel = self._live_grpc.aio.secure_channel(target, credentials, options=options)
        return _RecordingChannel(channel, self._recorder)


class _Player:
    def __init__(self, cassette: AndroidGrpcCassette, sanitizer: MessageSanitizer) -> None:
        self.cassette = cassette
        self.sanitizer = sanitizer
        self.cursor = 0

    def take(
        self,
        method: str,
        shape: RpcShape,
        request: Message,
        response_type: str,
        metadata: Any,
    ) -> GrpcInteraction:
        if self.cursor >= len(self.cassette.interactions):
            raise AndroidGrpcCassetteMismatch("Android gRPC cassette is exhausted")
        interaction = self.cassette.interactions[self.cursor]
        actual_request = ProtoPayload.from_message(method, "request", request, self.sanitizer)
        expected = (interaction.method, interaction.shape, interaction.request)
        actual = (method, shape, actual_request)
        if actual != expected:
            raise AndroidGrpcCassetteMismatch(
                f"Android gRPC cassette request mismatch at interaction {self.cursor}\n"
                f"  expected: {interaction.shape} {interaction.method}\n"
                f"  actual:   {shape} {method}\n"
                + _describe_request_diff(interaction.request, actual_request, type(request))
            )
        if interaction.response_type != response_type:
            raise AndroidGrpcCassetteMismatch(
                "Android gRPC cassette response protobuf type mismatch at interaction "
                f"{self.cursor}"
            )
        expected_metadata = interaction.application_metadata_keys
        actual_metadata = _application_metadata_keys(metadata)
        if actual_metadata != expected_metadata:
            raise AndroidGrpcCassetteMismatch(
                "Android gRPC cassette application metadata mismatch at interaction "
                f"{self.cursor}: expected {expected_metadata!r}, got {actual_metadata!r}"
            )
        self.cursor += 1
        return interaction

    def assert_consumed(self) -> None:
        remaining = len(self.cassette.interactions) - self.cursor
        if remaining:
            raise AndroidGrpcCassetteMismatch(
                f"Android gRPC cassette has {remaining} unconsumed interaction(s)"
            )


def _describe_request_diff(
    expected: ProtoPayload, actual: ProtoPayload, request_type: type[Message]
) -> str:
    """Render both sanitized requests as text so a mismatch names the field.

    Both payloads have already passed through the redactor, so the rendering
    contains placeholders only and is safe to show in a test failure.
    """

    from google.protobuf import text_format

    if expected.type_name != actual.type_name:
        return f"  request type: expected {expected.type_name}, actual {actual.type_name}\n"
    try:
        expected_text = text_format.MessageToString(request_type.FromString(expected.wire_bytes))
        actual_text = text_format.MessageToString(request_type.FromString(actual.wire_bytes))
    except Exception as error:  # noqa: BLE001 - diagnostics must never mask the mismatch
        return f"  (could not decode requests for diff: {type(error).__name__})\n"
    return f"  expected request:\n{expected_text}\n  actual request:\n{actual_text}"


class _ReplayStreamCall:
    def __init__(self, messages: tuple[Message, ...]) -> None:
        self._messages = iter(messages)
        self.cancelled = False

    def __aiter__(self) -> _ReplayStreamCall:
        return self

    async def __anext__(self) -> Message:
        try:
            return next(self._messages)
        except StopIteration:
            raise StopAsyncIteration from None

    def cancel(self) -> None:
        self.cancelled = True


class _ReplayChannel:
    def __init__(self, player: _Player) -> None:
        self._player = player
        self.closed = False

    def unary_unary(
        self, method: str, *, request_serializer: Any, response_deserializer: Any
    ) -> Any:
        del request_serializer
        response_type = _deserializer_type(response_deserializer)

        async def invoke(request: Message, *, metadata: Any, timeout: float | None) -> Message:
            del timeout
            interaction = self._player.take(method, "unary_unary", request, response_type, metadata)
            return response_deserializer(interaction.responses[0].wire_bytes)

        return invoke

    def unary_stream(
        self, method: str, *, request_serializer: Any, response_deserializer: Any
    ) -> Any:
        del request_serializer
        response_type = _deserializer_type(response_deserializer)

        def invoke(request: Message, *, metadata: Any, timeout: float | None) -> _ReplayStreamCall:
            del timeout
            interaction = self._player.take(
                method, "unary_stream", request, response_type, metadata
            )
            return _ReplayStreamCall(
                tuple(
                    response_deserializer(response.wire_bytes) for response in interaction.responses
                )
            )

        return invoke

    async def close(self) -> None:
        self.closed = True


class ReplayGrpcModule:
    """grpc-loader replacement that can only construct an in-memory channel."""

    def __init__(
        self,
        path: Path,
        *,
        sanitizer: MessageSanitizer | None = None,
        expected_target: str = ANDROID_GRPC_TARGET,
    ) -> None:
        self._player = _Player(
            AndroidGrpcCassette.load(path),
            sanitizer or ProtoRedactor(trust_placeholders=True),
        )
        self._expected_target = expected_target
        self.secure_channel_calls = 0
        self.channel: _ReplayChannel | None = None
        self.aio = SimpleNamespace(secure_channel=self.secure_channel)

    def ssl_channel_credentials(self) -> object:
        return object()

    def secure_channel(
        self, target: str, credentials: Any, *, options: Any = None
    ) -> _ReplayChannel:
        del credentials, options
        if target != self._expected_target:
            raise AndroidGrpcCassetteMismatch(f"Android gRPC replay target mismatch: {target!r}")
        self.secure_channel_calls += 1
        if self.channel is None:
            self.channel = _ReplayChannel(self._player)
        return self.channel

    def assert_consumed(self) -> None:
        self._player.assert_consumed()


class ReplayBearer(LoopBoundPrimitive):
    """Non-secret bearer provider used only with :class:`ReplayGrpcModule`."""

    def __init__(self) -> None:
        self.activations: list[int] = []
        self.gets: list[int] = []
        self.invalidated: list[int] = []
        self.closed = 0

    def reset_after_open(self) -> None:
        """Satisfy the root lifecycle without acquiring credential state."""

    async def close_resources(self) -> None:
        """No-op lifecycle half: replay owns no external credential resource."""

    async def activate_for_epoch(self, epoch: int) -> None:
        self.activations.append(epoch)

    async def get(self, expected_epoch: int) -> BearerCredential:
        self.gets.append(expected_epoch)
        return BearerCredential("android-grpc-cassette-replay", 0)

    def invalidate(self, generation: int) -> None:
        self.invalidated.append(generation)

    async def prepare_close(self) -> None:
        self.closed += 1


__all__ = [
    "AndroidGrpcCassette",
    "AndroidGrpcCassetteError",
    "AndroidGrpcCassetteMismatch",
    "ProtoPayload",
    "ProtoRedactor",
    "RecordingGrpcModule",
    "ReplayBearer",
    "ReplayGrpcModule",
    "compose_sanitizers",
]
