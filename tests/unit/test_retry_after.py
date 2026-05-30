from datetime import datetime, timedelta, timezone

from notebooklm._deadline import RuntimeDeadline
from notebooklm._transport_errors import MAX_RETRY_AFTER_SECONDS, parse_retry_after


def test_parse_retry_after_integer():
    assert parse_retry_after("30") == 30
    assert parse_retry_after(" 120 ") == 120
    assert parse_retry_after("0") == 0
    assert parse_retry_after("-5") == 0
    assert parse_retry_after(str(MAX_RETRY_AFTER_SECONDS + 1)) == MAX_RETRY_AFTER_SECONDS


def test_parse_retry_after_http_date():
    # Future date: now + 60 seconds
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    # Format as RFC 7231: Wed, 21 Oct 2015 07:28:00 GMT
    date_str = future.strftime("%a, %d %b %Y %H:%M:%S GMT")

    # Allow some slack (1-2 seconds) for test execution time
    result = parse_retry_after(date_str)
    assert result is not None
    assert 58 <= result <= 60


def test_parse_retry_after_past_date():
    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    date_str = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after(date_str) == 0


def test_parse_retry_after_fractional_seconds():
    # Fractional seconds are non-RFC-7231 but emitted by some servers/proxies.
    # Round UP so we never retry faster than the server asked.
    assert parse_retry_after("1.5") == 2
    assert parse_retry_after("0.4") == 1
    assert parse_retry_after("0.0") == 0
    assert parse_retry_after(" 2.5 ") == 3
    # Integer-seconds form still parses unchanged (no spurious ceil).
    assert parse_retry_after("3") == 3
    # Negative fractional clamps to 0, same as integer.
    assert parse_retry_after("-1.5") == 0
    # Fractional is still capped.
    assert parse_retry_after(f"{MAX_RETRY_AFTER_SECONDS + 0.5}") == MAX_RETRY_AFTER_SECONDS


def test_parse_retry_after_invalid():
    assert parse_retry_after("not a date") is None
    assert parse_retry_after("") is None
    assert parse_retry_after("   ") is None
    # Partially valid but not what we want
    assert parse_retry_after("2026-05-13") is None
    # Non-finite floats must be rejected, not crash math.ceil.
    assert parse_retry_after("inf") is None
    assert parse_retry_after("nan") is None
    assert parse_retry_after("-inf") is None


def test_runtime_deadline_pins_expired_and_exceeded_boundaries():
    clock = 10.0

    def monotonic() -> float:
        return clock

    deadline = RuntimeDeadline.start(1.0, monotonic=monotonic)
    assert deadline.started_at == 10.0
    assert deadline.remaining() == 1.0
    assert deadline.clamp_sleep(5.0) == 1.0

    clock = 11.0
    assert deadline.remaining() == 0.0
    assert deadline.expired() is True
    assert deadline.exceeded() is False
    assert deadline.timeout_message("retry") == "retry timed out after 1.0s"

    clock = 11.01
    assert deadline.exceeded() is True
