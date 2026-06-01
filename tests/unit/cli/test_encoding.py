"""Tests for encoding-safe CLI output helpers."""

from unittest.mock import patch

import notebooklm.cli._encoding as encoding_module
from notebooklm.cli._encoding import replace_unencodable, safe_echo


class DummyStream:
    def __init__(self, encoding):
        self.encoding = encoding


class TestReplaceUnencodable:
    def test_preserves_text_encodable_by_stream(self):
        assert replace_unencodable("plain text", DummyStream("cp950")) == "plain text"

    def test_replaces_text_not_encodable_by_stream(self):
        assert replace_unencodable("web 🌐", DummyStream("cp950")) == "web ?"

    def test_uses_utf8_when_stream_encoding_is_missing(self):
        assert replace_unencodable("web 🌐", DummyStream(None)) == "web 🌐"

    def test_uses_utf8_when_stream_is_none(self):
        assert replace_unencodable("web 🌐", None) == "web 🌐"

    def test_unknown_encoding_falls_back_to_ascii_replacement(self):
        assert replace_unencodable("web 🌐", DummyStream("not-a-codec")) == "web ?"


class TestSafeEcho:
    def test_echoes_original_message_when_first_write_succeeds(self):
        with patch("notebooklm.cli._encoding.click.echo") as mock_echo:
            safe_echo("web 🌐")

        mock_echo.assert_called_once_with("web 🌐", err=False)

    def test_replaces_stdout_text_when_first_write_cannot_encode(self):
        class DummyStdout:
            encoding = "cp950"

        calls = []

        def flaky_echo(message=None, **kwargs):
            if not calls:
                calls.append((message, kwargs.get("err", False)))
                raise UnicodeEncodeError(
                    "cp950",
                    str(message),
                    4,
                    5,
                    "illegal multibyte sequence",
                )
            calls.append((message, kwargs.get("err", False)))

        with (
            patch("notebooklm.cli._encoding.click.echo", side_effect=flaky_echo),
            patch.object(encoding_module.sys, "stdout", DummyStdout()),
        ):
            safe_echo("web 🌐")

        assert calls == [("web 🌐", False), ("web ?", False)]

    def test_replaces_stderr_text_when_first_write_cannot_encode(self):
        class DummyStderr:
            encoding = "cp950"

        calls = []

        def flaky_echo(message=None, **kwargs):
            if not calls:
                calls.append((message, kwargs.get("err", False)))
                raise UnicodeEncodeError(
                    "cp950",
                    str(message),
                    4,
                    5,
                    "illegal multibyte sequence",
                )
            calls.append((message, kwargs.get("err", False)))

        with (
            patch("notebooklm.cli._encoding.click.echo", side_effect=flaky_echo),
            patch.object(encoding_module.sys, "stderr", DummyStderr()),
        ):
            safe_echo("web 🌐", err=True)

        assert calls == [("web 🌐", True), ("web ?", True)]
