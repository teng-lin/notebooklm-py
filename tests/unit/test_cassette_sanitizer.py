"""Tests for the cassette sanitizer and the Python guard tool.

Coverage map:

1. Structural display-name scrub — positive + negative cases on
   ``tests/vcr_config.scrub_string``.
2. Two-Capitalized-word source title regression — confirms we don't reintroduce
   the broad ``>[A-Z][a-z]+\\s[A-Z][a-z]+<`` pattern that would clobber legit
   fixture content.
3. Broadened email scrub — positive + idempotency.
4. The Python guard ``tests/scripts/check_cassettes_clean.py``:
   - exits 0 on clean cassettes
   - exits 1 on email / cookie-header / JSON-key / storage_state leaks
   - explicit ``SCRUB_PLACEHOLDERS`` allowlist (NOT a "starts with S"
     heuristic) — closes I7
   - accepts the ``SCRUBBED`` sentinel in all three cookie shapes
   - honors the repair allowlist by default; ``--strict`` disables it
   - emits ``file:line`` for every leak
   - exits 0 when no cassettes are found at all

The legacy bash-script-driven tests on PR #477 were retired here in lockstep
with the deletion of ``tests/check_cassettes_clean.sh``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"
# ``tests/vcr_config.py`` lives directly under ``tests/`` (not in a package).
# Other test modules add it to ``sys.path``; we follow the same convention.
sys.path.insert(0, str(TESTS_DIR))

from vcr_config import scrub_string  # noqa: E402

GUARD_SCRIPT = TESTS_DIR / "scripts" / "check_cassettes_clean.py"
REGRESSION_FIXTURE = TESTS_DIR / "fixtures" / "bad_cassettes" / "bad_sid_starting_with_s.yaml"


# ---------------------------------------------------------------------------
# Structural display-name scrub
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key, value",
    [
        ("displayName", "Alice Example"),
        ("givenName", "Alice"),
        ("familyName", "Example"),
    ],
)
def test_structural_display_name_scrub_positive(key: str, value: str) -> None:
    """Each new key-anchored pattern scrubs the value to SCRUBBED_NAME."""
    text = f'{{"{key}":"{value}"}}'
    scrubbed = scrub_string(text)
    assert value not in scrubbed
    assert f'"{key}":"SCRUBBED_NAME"' in scrubbed


@pytest.mark.parametrize(
    "key, value",
    [
        ("displayName", "Alice Example"),
        ("givenName", "Alice"),
        ("familyName", "Example"),
    ],
)
def test_structural_display_name_scrub_whitespace_variants(key: str, value: str) -> None:
    """JSON ``"key": "value"`` with whitespace around the colon is scrubbed."""
    text = f'{{"{key}" : "{value}"}}'
    scrubbed = scrub_string(text)
    assert value not in scrubbed
    # Replacement does not preserve whitespace; we only assert the value is gone
    # and the key is now mapped to SCRUBBED_NAME.
    assert "SCRUBBED_NAME" in scrubbed


def test_structural_display_name_scrub_negative_sibling_keys() -> None:
    """Sibling keys (``title``, ``name``, ``label``) MUST NOT match."""
    text = '{"title":"My Title","name":"My Name","label":"My Label"}'
    scrubbed = scrub_string(text)
    # None of those keys should have been touched.
    assert scrubbed == text


def test_structural_display_name_no_match_on_substring_keys() -> None:
    """The regex requires the JSON key to be exactly ``displayName`` (the
    opening quote is part of the match). So keys that *contain* the substring
    ``displayName`` but are not equal to it MUST NOT match:

    - ``displayNamespace`` — extra trailing characters before the closing quote
    - ``userDisplayName`` — extra leading characters after the opening quote
    """
    extra_trailing = '{"displayNamespace":"keep-me"}'
    extra_leading = '{"userDisplayName":"Alice Example"}'
    assert scrub_string(extra_trailing) == extra_trailing
    assert scrub_string(extra_leading) == extra_leading


# ---------------------------------------------------------------------------
# Regression: legitimate two-Capitalized-word source title is preserved
# ---------------------------------------------------------------------------


def test_two_capital_word_source_title_not_scrubbed() -> None:
    """A cassette-style JSON snippet with a two-word source title must survive.

    This guards against re-introducing a broad ``>[A-Z][a-z]+\\s[A-Z][a-z]+<``
    pattern that would clobber legitimate fixture content.
    """
    snippet = '{"title": "Source Title"}'
    assert scrub_string(snippet) == snippet


def test_two_capital_word_in_html_text_not_scrubbed() -> None:
    """Same regression in an HTML-ish context ``>Source Title<``."""
    snippet = "<span>Source Title</span>"
    assert scrub_string(snippet) == snippet


# ---------------------------------------------------------------------------
# Broadened email scrub
# ---------------------------------------------------------------------------


_EMAIL_PROVIDERS = [
    "gmail",
    "googlemail",
    "google",
    "anthropic",
    "outlook",
    "hotmail",
    "yahoo",
    "icloud",
    "protonmail",
]


@pytest.mark.parametrize("provider", _EMAIL_PROVIDERS)
def test_broadened_email_scrub_positive(provider: str) -> None:
    """Quoted emails at any of the supported providers get scrubbed."""
    text = f'{{"email":"alice.example+tag@{provider}.com"}}'
    scrubbed = scrub_string(text)
    assert provider not in scrubbed
    assert "alice.example" not in scrubbed
    assert '"SCRUBBED_EMAIL@example.com"' in scrubbed


@pytest.mark.parametrize("provider", _EMAIL_PROVIDERS)
def test_broadened_email_scrub_unquoted_context(provider: str) -> None:
    """Unquoted emails in raw HTML/JS contexts get scrubbed too."""
    text = f'<a href="mailto:alice.example+tag@{provider}.com">Mail me</a>'
    scrubbed = scrub_string(text)
    assert provider not in scrubbed
    assert "alice.example" not in scrubbed
    assert "SCRUBBED_EMAIL@example.com" in scrubbed


def test_email_scrub_idempotent_on_example_com() -> None:
    """``SCRUBBED_EMAIL@example.com`` survives a second scrub pass unchanged."""
    once = scrub_string('{"email":"alice@gmail.com"}')
    twice = scrub_string(once)
    assert once == twice
    assert '"SCRUBBED_EMAIL@example.com"' in twice


def test_email_scrub_negative_unrelated_text() -> None:
    """Domains we don't cover (``@corp.internal``) are left alone — by design."""
    text = '{"contact":"bob@corp.internal"}'
    assert scrub_string(text) == text


# ---------------------------------------------------------------------------
# Python guard tool: ``tests/scripts/check_cassettes_clean.py``
#
# The guard is invoked as a subprocess so we exercise the real CLI entry
# point — including argparse wiring, exit codes, and stdout/stderr.  It is
# cross-platform pure-Python, so unlike the previous bash-script-driven
# tests these run on Windows too.
# ---------------------------------------------------------------------------


def _run_guard(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the guard with explicit args.  Returns the completed process."""
    return subprocess.run(
        [sys.executable, str(GUARD_SCRIPT), *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )


def test_python_guard_exits_zero_on_clean_cassette(tmp_path: Path) -> None:
    """A cassette containing only canonical placeholders passes the guard."""
    cassette = tmp_path / "clean.yaml"
    cassette.write_text(
        '{"email":"SCRUBBED_EMAIL@example.com","SID":"SCRUBBED"}\n'
        "Set-Cookie: SID=SCRUBBED; Path=/\n"
    )
    result = _run_guard(str(cassette))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Summary: 1 cassettes scanned" in result.stdout
    assert "0 leaks found" in result.stdout


def test_python_guard_exits_one_on_email_leak(tmp_path: Path) -> None:
    """A cassette with an unscrubbed real-provider email trips the guard."""
    cassette = tmp_path / "leak_email.yaml"
    cassette.write_text('{"email":"realname@gmail.com"}\n')
    result = _run_guard(str(cassette))
    assert result.returncode == 1
    assert "Leak (email)" in result.stdout
    assert "realname@gmail.com" in result.stdout
    # Line:column format — the leak was on line 1.
    assert ":1:" in result.stdout


def test_python_guard_exits_one_on_cookie_header_leak(tmp_path: Path) -> None:
    """Shape A — ``Set-Cookie: SID=value`` header with a real value."""
    cassette = tmp_path / "leak_header.yaml"
    cassette.write_text("Set-Cookie: SAPISID=abcdef1234567890; Path=/\n")
    result = _run_guard(str(cassette))
    assert result.returncode == 1
    assert "Leak (cookie header)" in result.stdout


def test_python_guard_exits_one_on_cookie_json_key_leak(tmp_path: Path) -> None:
    """Shape B — JSON dict with cookie name as top-level key."""
    cassette = tmp_path / "leak_json.yaml"
    cassette.write_text('{"SAPISID": "abcdef1234567890"}\n')
    result = _run_guard(str(cassette))
    assert result.returncode == 1
    assert "Leak (JSON key)" in result.stdout


def test_python_guard_exits_one_on_storage_state_name_first(tmp_path: Path) -> None:
    """Shape C — Playwright storage_state.json, ``name`` before ``value``."""
    cassette = tmp_path / "leak_ss.yaml"
    cassette.write_text('{"name":"SID","value":"abc1234567","domain":".google.com"}\n')
    result = _run_guard(str(cassette))
    assert result.returncode == 1
    assert "Leak (storage_state (name-first))" in result.stdout


def test_python_guard_exits_one_on_storage_state_value_first(tmp_path: Path) -> None:
    """Shape C — Playwright storage_state.json, ``value`` before ``name``."""
    cassette = tmp_path / "leak_ssv.yaml"
    cassette.write_text('{"value":"abc1234567","name":"__Secure-1PSID","domain":".google.com"}\n')
    result = _run_guard(str(cassette))
    assert result.returncode == 1
    assert "Leak (storage_state (value-first))" in result.stdout


def test_python_guard_catches_sid_starting_with_s(tmp_path: Path) -> None:
    """Regression for I7 — a real cookie value starting with ``S`` is a leak.

    The old bash guard's ``[^S"][^"]*`` capture rejected any value whose
    first byte was ``S``, which silently allowed a real session token that
    happened to start with ``S`` (~1/62 chance for base64).  The new Python
    guard uses an explicit ``SCRUB_PLACEHOLDERS`` allowlist instead, so this
    fixture (with ``SID=Sx7K9pQ2_realsessiontoken``) trips the guard.

    The fixture lives under ``tests/fixtures/bad_cassettes/`` precisely so
    the regression assertion has a real on-disk artifact to point at, not
    just a tmp_path-only synthetic string.
    """
    assert REGRESSION_FIXTURE.is_file(), (
        "Regression fixture missing — see tests/fixtures/bad_cassettes/bad_sid_starting_with_s.yaml"
    )
    result = _run_guard(str(REGRESSION_FIXTURE))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Leak (cookie header)" in result.stdout
    # The leaked value must actually appear in the output, otherwise the
    # operator has no way to find it.
    assert "Sx7K9pQ2_realsessiontoken" in result.stdout


def test_python_guard_allows_scrubbed_cookie_sentinel(tmp_path: Path) -> None:
    """All three cookie shapes carrying the ``SCRUBBED`` sentinel pass."""
    cassette = tmp_path / "ok.yaml"
    cassette.write_text(
        '{"SID": "SCRUBBED", "__Secure-1PSID": "SCRUBBED"}\n'
        "Set-Cookie: SID=SCRUBBED; Path=/\n"
        '{"name":"SAPISID","value":"SCRUBBED","domain":".google.com"}\n'
    )
    result = _run_guard(str(cassette))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 leaks found" in result.stdout


def test_python_guard_emits_file_line_for_every_leak(tmp_path: Path) -> None:
    """Each leak is reported as ``<path>:<line>: [<label>] <excerpt>``.

    The bash guard reported file:line by virtue of ``grep -n``; the Python
    guard does the same so a developer can jump to the offending interaction.
    """
    cassette = tmp_path / "multi.yaml"
    cassette.write_text(
        'line 1 ok\n{"email":"a@gmail.com"}\nline 3 ok\nSet-Cookie: SID=Real_tokenA; Path=/\n'
    )
    result = _run_guard(str(cassette))
    assert result.returncode == 1
    # Line 2 — email leak
    assert f"{cassette}:2:" in result.stdout or "multi.yaml:2:" in result.stdout
    # Line 4 — cookie header leak
    assert "multi.yaml:4:" in result.stdout or f"{cassette}:4:" in result.stdout


def test_python_guard_skips_allowlisted_basename(tmp_path: Path) -> None:
    """A cassette whose basename is in the allowlist is skipped by default."""
    cassette = tmp_path / "leak_in_allowlist.yaml"
    cassette.write_text('{"email":"realname@gmail.com"}\n')
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("# header comment\nleak_in_allowlist.yaml\n")
    result = _run_guard("--allowlist", str(allowlist), str(cassette))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 allow-listed" in result.stdout
    # Nothing was scanned.
    assert "0 cassettes scanned" in result.stdout


def test_python_guard_strict_flag_disables_allowlist(tmp_path: Path) -> None:
    """``--strict`` re-arms the guard against allow-listed cassettes."""
    cassette = tmp_path / "leak_in_allowlist.yaml"
    cassette.write_text('{"email":"realname@gmail.com"}\n')
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("leak_in_allowlist.yaml\n")
    result = _run_guard(
        "--strict",
        "--allowlist",
        str(allowlist),
        str(cassette),
    )
    assert result.returncode == 1
    assert "Leak (email)" in result.stdout


def test_python_guard_exits_zero_when_no_cassettes_found(tmp_path: Path) -> None:
    """An empty cassette directory is a valid clean state (matches bash)."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    result = _run_guard(str(empty_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: no cassettes to scan" in result.stdout


def test_python_guard_repo_allowlist_is_explicit_basename_list() -> None:
    """The repo-level repair allowlist is a literal basename list (no globs).

    Sanity check that the allowlist shipped in this PR exists, is non-empty,
    and contains the spec-explicit entries.  Future authors changing the file
    must keep these entries unless they're also removing the corresponding
    cassette.
    """
    allowlist = TESTS_DIR / "scripts" / "cassette_repair_allowlist.txt"
    assert allowlist.is_file()
    entries = {
        line.strip()
        for line in allowlist.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    # Spec-explicit entries that must always be in the allowlist while
    # cassette repair is outstanding.
    # ``sources_add_file.yaml`` is NOT in this required-set anymore — it
    # was repaired (upload-token leak scrubbed in place).
    # ``sources_add_drive.yaml`` + ``sources_check_freshness_drive.yaml``
    # are NOT in this required-set anymore — they were repaired (Drive
    # AONS-token leak scrubbed in place).
    # ``example_httpbin_{get,post}.yaml`` are NOT in this required-set
    # anymore — they were deleted (the origin-IP leak was in illustrative
    # VCR fixtures, not real NotebookLM cassettes).
    # ``chat_ask.yaml`` + ``chat_ask_with_references.yaml`` are NOT in this
    # required-set anymore — they were re-recorded against the current
    # 9-param streaming-chat builder (stale-shape regression).
    # ``artifacts_revise_slide.yaml`` is NOT in this required-set anymore
    # — it was repaired (re-recorded so f.req carries a real urlencoded
    # JSON payload with only sensitive scalars scrubbed inside).
    # ``sharing_get_status.yaml`` + ``sharing_set_public.yaml`` are NOT
    # in this required-set anymore — they were re-scrubbed.
    # With all cassette repairs landed, this loop has nothing to assert;
    # future regressions would re-introduce entries here.
    for required in ():
        assert required in entries, f"missing required allowlist entry: {required}"
