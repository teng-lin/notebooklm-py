"""Unit tests for the friendly rookiepy error message formatter.

``_handle_rookiepy_error`` is a pure helper: it classifies a rookiepy
``OSError``/``RuntimeError`` into one of four user-facing message shapes
(locked DB, permission denied, decryption failure, generic) and returns
Rich-markup text. These tests pin each classification branch.
"""

from __future__ import annotations

import pytest

from notebooklm.cli.services.login.rookiepy_errors import _handle_rookiepy_error


@pytest.mark.parametrize(
    "error_text",
    ["database is locked", "could not acquire LOCK on profile"],
)
def test_locked_database_branch(error_text):
    msg = _handle_rookiepy_error(RuntimeError(error_text), "chrome")
    assert "browser database is locked" in msg
    assert "Close your browser" in msg
    assert "chrome" in msg


@pytest.mark.parametrize(
    "error_text",
    ["Permission denied", "access is denied to profile"],
)
def test_permission_denied_branch(error_text):
    msg = _handle_rookiepy_error(OSError(error_text), "brave")
    assert "Permission denied reading brave cookies" in msg
    assert "grant Terminal/Python access" in msg


@pytest.mark.parametrize(
    "error_text",
    ["keychain entry unavailable", "failed to decrypt value"],
)
def test_decryption_branch(error_text):
    msg = _handle_rookiepy_error(RuntimeError(error_text), "edge")
    assert "Could not decrypt edge cookies" in msg
    assert "Keychain access" in msg


def test_generic_branch_includes_original_message():
    err = RuntimeError("some totally unexpected failure")
    msg = _handle_rookiepy_error(err, "firefox")
    assert "Failed to read cookies from firefox" in msg
    assert "some totally unexpected failure" in msg


def test_classification_is_case_insensitive():
    # "LOCK" upper-cased still routes to the locked-database branch because
    # the helper lower-cases the message before matching.
    msg = _handle_rookiepy_error(RuntimeError("DATABASE IS LOCKED"), "chrome")
    assert "browser database is locked" in msg
