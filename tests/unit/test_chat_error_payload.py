"""Tests for chat error-payload parsing fallbacks."""

import logging

from notebooklm._web.codec.chat_stream import raise_if_rate_limited


class MalformedErrorPayload(list):
    def __len__(self):
        raise TypeError("malformed payload")


def test_rate_limit_payload_parse_failure_logs_debug(caplog):
    # P10 R2.1: the delegating ``ChatAPI._raise_if_rate_limited`` wrapper is
    # deleted; the codec function it forwarded to is called directly. The
    # parser logs under the ``notebooklm._chat`` logger name either way.
    with caplog.at_level(logging.DEBUG, logger="notebooklm._chat"):
        raise_if_rate_limited(MalformedErrorPayload())

    records = [
        record
        for record in caplog.records
        if "Could not parse chat error payload" in record.message
    ]
    assert records
    assert records[0].exc_info is not None
