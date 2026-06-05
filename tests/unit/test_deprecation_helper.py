"""Unit tests for the ``notebooklm._deprecation`` keyword-alias helper."""

import warnings

import pytest

from notebooklm._deprecation import (
    DEFAULT_REMOVAL,
    deprecated_kwarg,
    deprecations_quiet,
    warn_deprecated,
)

_UNSET = object()


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


class TestDeprecatedKwarg:
    def test_new_only_returns_new_without_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning would fail the test
            result = deprecated_kwarg(
                None,
                3.0,
                old="interval",
                new="initial_interval",
                owner="X.m",
            )
        assert result == 3.0

    def test_neither_returns_sentinel_without_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            result = deprecated_kwarg(
                _UNSET,
                _UNSET,
                old="interval",
                new="initial_interval",
                owner="X.m",
                sentinel=_UNSET,
            )
        assert result is _UNSET

    def test_old_only_warns_and_returns_old(self):
        with pytest.warns(DeprecationWarning) as record:
            result = deprecated_kwarg(
                7.0,
                None,
                old="interval",
                new="initial_interval",
                owner="X.m",
            )
        assert result == 7.0
        msg = str(record[0].message)
        # Names both keywords, the removal version, and the suppress switch.
        assert "interval" in msg
        assert "initial_interval" in msg
        assert f"v{DEFAULT_REMOVAL}" in msg
        assert "NOTEBOOKLM_QUIET_DEPRECATIONS" in msg

    def test_both_passed_raises_type_error(self):
        with pytest.raises(TypeError, match="both 'initial_interval'"):
            deprecated_kwarg(
                1.0,
                2.0,
                old="interval",
                new="initial_interval",
                owner="X.m",
            )

    def test_custom_removal_named_in_message(self):
        with pytest.warns(DeprecationWarning, match="v9.9.9"):
            deprecated_kwarg(
                1.0,
                None,
                old="interval",
                new="initial_interval",
                owner="X.m",
                removal="9.9.9",
            )

    def test_quiet_env_suppresses_warning(self, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", "1")
        assert deprecations_quiet() is True
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # would fail if a warning fired
            result = deprecated_kwarg(
                7.0,
                None,
                old="interval",
                new="initial_interval",
                owner="X.m",
            )
        assert result == 7.0  # still resolves the value, just silently

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
