from datetime import datetime, timedelta, timezone

from notebooklm._core import MAX_RETRY_AFTER_SECONDS, _parse_retry_after


def test_parse_retry_after_integer():
    assert _parse_retry_after("30") == 30
    assert _parse_retry_after(" 120 ") == 120
    assert _parse_retry_after("0") == 0
    assert _parse_retry_after("-5") == 0
    assert _parse_retry_after(str(MAX_RETRY_AFTER_SECONDS + 1)) == MAX_RETRY_AFTER_SECONDS


def test_parse_retry_after_http_date():
    # Future date: now + 60 seconds
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    # Format as RFC 7231: Wed, 21 Oct 2015 07:28:00 GMT
    date_str = future.strftime("%a, %d %b %Y %H:%M:%S GMT")

    # Allow some slack (1-2 seconds) for test execution time
    result = _parse_retry_after(date_str)
    assert result is not None
    assert 58 <= result <= 60


def test_parse_retry_after_past_date():
    past = datetime.now(timezone.utc) - timedelta(seconds=60)
    date_str = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert _parse_retry_after(date_str) == 0


def test_parse_retry_after_invalid():
    assert _parse_retry_after("not a date") is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("   ") is None
    # Partially valid but not what we want
    assert _parse_retry_after("2026-05-13") is None
