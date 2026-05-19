"""Tests for chat error-payload parsing fallbacks."""

import logging
from unittest.mock import MagicMock

from notebooklm._chat import ChatAPI


class MalformedErrorPayload(list):
    def __len__(self):
        raise TypeError("malformed payload")


def test_rate_limit_payload_parse_failure_logs_debug(caplog):
    api = ChatAPI(core=MagicMock())

    with caplog.at_level(logging.DEBUG, logger="notebooklm._chat"):
        api._raise_if_rate_limited(MalformedErrorPayload())

    records = [
        record
        for record in caplog.records
        if "Could not parse chat error payload" in record.message
    ]
    assert records
    assert records[0].exc_info is not None
