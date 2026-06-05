"""Unit tests for the ``notebooklm._deprecation`` warn helper + quiet gate."""

import warnings

import pytest

from notebooklm._deprecation import (
    deprecations_quiet,
    warn_deprecated,
)


class TestWarnDeprecated:
    """The generic gated primitive (issue #1369)."""

    def test_emits_deprecation_warning_with_message(self):
        with pytest.warns(DeprecationWarning, match="old thing is deprecated") as record:
            warn_deprecated("old thing is deprecated", removal="1.0")
        assert len(record) == 1
        assert "v1.0" in str(record[0].message)

    def test_appends_removal_version_when_absent(self):
        with pytest.warns(DeprecationWarning) as record:
            warn_deprecated("Bare message with no version.", removal="0.8.0")
        assert "v0.8.0" in str(record[0].message)

    def test_does_not_duplicate_removal_when_message_already_names_it(self):
        with pytest.warns(DeprecationWarning) as record:
            warn_deprecated("Removed in v1.0 already.", removal="1.0")
        msg = str(record[0].message)
        assert msg.count("v1.0") == 1

    def test_no_removal_emits_message_verbatim(self):
        # ``warn_deprecated(removal=None)`` emits the message verbatim (no
        # synthesized removal-version clause). The former removal=None callers
        # (NotebooksAPI.share(), ambiguous poll) were removed in v0.8.0 (#1363);
        # awaiting from_storage(...) remains a removal=None caller.
        with pytest.warns(DeprecationWarning) as record:
            warn_deprecated("Permanent shim warning with no version.", removal=None)
        msg = str(record[0].message)
        assert msg == "Permanent shim warning with no version."
        assert "removed" not in msg.lower()

    def test_quiet_env_suppresses_warning(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning would fail the test
            warn_deprecated("should be silent", removal="1.0")

    def test_quiet_env_unset_still_warns(self, monkeypatch):
        monkeypatch.delenv("NOTEBOOKLM_QUIET_DEPRECATIONS", raising=False)
        with pytest.warns(DeprecationWarning):
            warn_deprecated("loud by default", removal="1.0")


class TestDeprecationsQuiet:
    """The ``NOTEBOOKLM_QUIET_DEPRECATIONS`` suppression gate (read live)."""

    def test_quiet_env_suppresses_warn_deprecated(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
        assert deprecations_quiet() is True
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # would fail if a warning fired
            warn_deprecated("silent under quiet", removal="1.0")

    def test_quiet_env_unset_is_not_quiet(self, monkeypatch):
        monkeypatch.delenv("NOTEBOOKLM_QUIET_DEPRECATIONS", raising=False)
        assert deprecations_quiet() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
    def test_quiet_env_truthy_spellings(self, monkeypatch, value):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", value)
        assert deprecations_quiet() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "off", "2"])
    def test_quiet_env_falsey_spellings(self, monkeypatch, value):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", value)
        assert deprecations_quiet() is False
