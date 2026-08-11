"""Unit tests for notebooklm._env (NOTEBOOKLM_HL / NOTEBOOKLM_BL handling)."""

from datetime import date

import pytest

from notebooklm._env import (
    BUILD_LABEL_RE,
    BUILD_LABEL_STALE_AFTER_DAYS,
    DEFAULT_BL,
    build_label_date,
    build_label_days_behind,
    extract_build_label,
    get_default_bl,
    get_default_language,
)


def test_get_default_language_defaults_to_en(monkeypatch):
    """When NOTEBOOKLM_HL is unset, get_default_language() returns 'en'."""
    monkeypatch.delenv("NOTEBOOKLM_HL", raising=False)
    assert get_default_language() == "en"


def test_get_default_language_reads_env(monkeypatch):
    """When NOTEBOOKLM_HL is set, get_default_language() returns its value."""
    monkeypatch.setenv("NOTEBOOKLM_HL", "ja")
    assert get_default_language() == "ja"


def test_get_default_language_empty_env_falls_back(monkeypatch):
    """An empty NOTEBOOKLM_HL value falls back to 'en' rather than ''."""
    monkeypatch.setenv("NOTEBOOKLM_HL", "")
    assert get_default_language() == "en"


def test_get_default_language_strips_whitespace(monkeypatch):
    """Surrounding whitespace in NOTEBOOKLM_HL is stripped."""
    monkeypatch.setenv("NOTEBOOKLM_HL", "  ja  ")
    assert get_default_language() == "ja"


def test_get_default_language_whitespace_only_falls_back(monkeypatch):
    """A whitespace-only NOTEBOOKLM_HL value falls back to 'en'."""
    monkeypatch.setenv("NOTEBOOKLM_HL", "   ")
    assert get_default_language() == "en"


# ---------------------------------------------------------------------------
# NOTEBOOKLM_BL — chat-endpoint build label (consumed by ChatAPI.ask)
# ---------------------------------------------------------------------------


def test_get_default_bl_defaults(monkeypatch):
    """Unset NOTEBOOKLM_BL falls back to the pinned DEFAULT_BL constant."""
    monkeypatch.delenv("NOTEBOOKLM_BL", raising=False)
    assert get_default_bl() == DEFAULT_BL


def test_get_default_bl_reads_env(monkeypatch):
    """A custom NOTEBOOKLM_BL value flows through unchanged."""
    monkeypatch.setenv("NOTEBOOKLM_BL", "boq_labs-custom_20990101.00_p0")
    assert get_default_bl() == "boq_labs-custom_20990101.00_p0"


def test_get_default_bl_empty_env_falls_back(monkeypatch):
    """An empty NOTEBOOKLM_BL falls back to DEFAULT_BL, not ''."""
    monkeypatch.setenv("NOTEBOOKLM_BL", "")
    assert get_default_bl() == DEFAULT_BL


def test_get_default_bl_strips_whitespace(monkeypatch):
    """Surrounding whitespace in NOTEBOOKLM_BL is stripped."""
    monkeypatch.setenv("NOTEBOOKLM_BL", "  custom_build  ")
    assert get_default_bl() == "custom_build"


def test_get_default_bl_whitespace_only_falls_back(monkeypatch):
    """A whitespace-only NOTEBOOKLM_BL value falls back to DEFAULT_BL."""
    monkeypatch.setenv("NOTEBOOKLM_BL", "   ")
    assert get_default_bl() == DEFAULT_BL


# ---------------------------------------------------------------------------
# Build-label freshness (#2073) — reading the served label and dating the pin
# ---------------------------------------------------------------------------

SHELL = '<script>var x="{label}";</script>'


def test_pinned_default_bl_is_a_well_formed_label():
    """DEFAULT_BL must match the shape the canary parses.

    This is the guardrail half of the drift lane: a bump that lands a typo'd or
    reformatted label would otherwise leave ``build_label_date`` returning None
    forever, which the lane reads as "no conclusion" — silently disabling the
    very check the constant needs.
    """
    assert BUILD_LABEL_RE.fullmatch(DEFAULT_BL) is not None
    assert build_label_date(DEFAULT_BL) is not None


def test_extract_build_label_reads_the_shell():
    label = "boq_labs-tailwind-frontend_20260802.02_p0"
    assert extract_build_label(SHELL.format(label=label)) == label


def test_extract_build_label_returns_none_when_absent():
    """No label means 'no observation' — never an empty string."""
    assert extract_build_label("<html><body>Sign in</body></html>") is None
    assert extract_build_label("") is None


def test_extract_build_label_picks_the_newest_of_several():
    """A shell naming several builds resolves to the freshest one."""
    html = (
        SHELL.format(label="boq_labs-tailwind-frontend_20260301.03_p0")
        + SHELL.format(label="boq_labs-tailwind-frontend_20260802.02_p0")
        + SHELL.format(label="boq_labs-tailwind-frontend_20260715.01_p0")
    )
    assert extract_build_label(html) == "boq_labs-tailwind-frontend_20260802.02_p0"


def test_extract_build_label_ignores_a_foreign_bundle_name():
    """Only the tailwind frontend label counts; other ``boq_`` tokens do not."""
    assert extract_build_label(SHELL.format(label="boq_something-else_20260802.02_p0")) is None


def test_build_label_date_parses_the_stamp():
    assert build_label_date("boq_labs-tailwind-frontend_20260802.02_p0") == date(2026, 8, 2)


@pytest.mark.parametrize(
    "label",
    [
        "boq_labs-tailwind-frontend_20261332.02_p0",  # month 13, day 32
        "boq_labs-tailwind-frontend_2026080.02_p0",  # too few digits
        "not-a-build-label",
        "",
    ],
)
def test_build_label_date_is_none_when_unreadable(label):
    """An undatable label yields None, so callers cannot invent a verdict."""
    assert build_label_date(label) is None


def test_build_label_days_behind_measures_the_gap():
    """The exact gap #2073 measured: the old pin vs what Google served."""
    assert (
        build_label_days_behind(
            "boq_labs-tailwind-frontend_20260301.03_p0",
            "boq_labs-tailwind-frontend_20260802.02_p0",
        )
        == 154
    )


def test_build_label_days_behind_is_negative_when_the_pin_is_ahead():
    assert (
        build_label_days_behind(
            "boq_labs-tailwind-frontend_20260802.02_p0",
            "boq_labs-tailwind-frontend_20260801.02_p0",
        )
        == -1
    )


def test_build_label_days_behind_ignores_the_patch_suffix():
    """Same day, different build/patch numbers is a zero-day gap."""
    assert (
        build_label_days_behind(
            "boq_labs-tailwind-frontend_20260802.01_p0",
            "boq_labs-tailwind-frontend_20260802.09_p3",
        )
        == 0
    )


@pytest.mark.parametrize(
    ("pinned", "served"),
    [
        ("garbage", "boq_labs-tailwind-frontend_20260802.02_p0"),
        ("boq_labs-tailwind-frontend_20260802.02_p0", "garbage"),
        ("garbage", "garbage"),
    ],
)
def test_build_label_days_behind_is_none_when_either_side_is_undatable(pinned, served):
    assert build_label_days_behind(pinned, served) is None


def test_staleness_window_is_wider_than_a_release_cadence():
    """The window must not be so tight that ordinary weekly drift alarms.

    Google ships a new build roughly weekly; a window under a month would put
    the canary in a permanent alarm state and train operators to ignore it.
    """
    assert BUILD_LABEL_STALE_AFTER_DAYS >= 30


def test_extract_build_label_orders_the_patch_suffix_numerically():
    """``_p10`` is newer than ``_p9`` — the patch field is not zero-padded.

    Plain lexicographic ordering over the raw label ranks ``_p9`` first, which
    would report the wrong served label (and could score a matching pin as
    DRIFTED rather than CURRENT).
    """
    html = SHELL.format(label="boq_labs-tailwind-frontend_20260802.02_p9") + SHELL.format(
        label="boq_labs-tailwind-frontend_20260802.02_p10"
    )
    assert extract_build_label(html) == "boq_labs-tailwind-frontend_20260802.02_p10"


def test_extract_build_label_orders_the_build_number_before_the_patch():
    """A higher build on the same day wins regardless of patch number."""
    html = SHELL.format(label="boq_labs-tailwind-frontend_20260802.09_p0") + SHELL.format(
        label="boq_labs-tailwind-frontend_20260802.02_p99"
    )
    assert extract_build_label(html) == "boq_labs-tailwind-frontend_20260802.09_p0"


def test_extract_build_label_orders_the_date_first():
    """A later date wins over a higher build/patch on an earlier one."""
    html = SHELL.format(label="boq_labs-tailwind-frontend_20260802.99_p99") + SHELL.format(
        label="boq_labs-tailwind-frontend_20260803.01_p0"
    )
    assert extract_build_label(html) == "boq_labs-tailwind-frontend_20260803.01_p0"
