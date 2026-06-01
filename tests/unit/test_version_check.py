"""Tests for Python version check (#117)."""

import subprocess
import sys
import textwrap


def test_version_check_exits_on_old_python():
    """Verify that _version_check produces a clear error on unsupported Python."""
    script = textwrap.dedent("""\
        import sys
        from unittest.mock import patch

        with patch.object(sys, "version_info", (3, 9, 0)):
            from notebooklm._version_check import check_python_version
            check_python_version()
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "requires Python 3.10 or later" in result.stderr
    assert "3.9.0" in result.stderr


def test_version_check_passes_on_supported_python():
    """Verify that the version check passes on the current (supported) Python."""
    from notebooklm._version_check import check_python_version

    # Should not raise on current Python (>= 3.10)
    check_python_version()


def test_version_check_in_process_exits_on_old_python(monkeypatch):
    """In-process variant so coverage records the ``sys.exit`` branch.

    The subprocess test above proves the user-facing message, but runs in a
    child interpreter where coverage isn't tracked. Patch ``version_info``
    in-process and assert the ``SystemExit`` carries the expected guidance.
    """
    import pytest

    from notebooklm import _version_check

    monkeypatch.setattr(_version_check.sys, "version_info", (3, 9, 5))
    with pytest.raises(SystemExit) as exc_info:
        _version_check.check_python_version()

    message = str(exc_info.value)
    assert "requires Python 3.10 or later" in message
    assert "3.9.5" in message
