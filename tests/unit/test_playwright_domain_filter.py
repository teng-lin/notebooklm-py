"""Unit tests for the Playwright storage-state domain filter (P1-17).

The Playwright login flow (``cli.session._run_playwright_login``) writes
``storage_state.json`` directly from ``context.storage_state()``. Without
filtering, that captures every cookie Playwright observed during the login —
including sibling Google products the user happens to be signed into in the
same browser session (``mail.google.com``, ``myaccount.google.com``,
``docs.google.com``, ``.youtube.com``, …). Those cookies are not exercised
by any NotebookLM code path and inflate the blast radius if
``storage_state.json`` is ever leaked.

The rookiepy / ``--browser-cookies`` path has applied this extraction-time
filter since the cookie-domain split landed (#360). This test suite locks in
parity for the Playwright path:

1. Cookies whose domain is in ``REQUIRED_COOKIE_DOMAINS`` are kept.
2. Cookies whose domain matches a regional ``.google.com.<ccTLD>`` variant
   are kept.
3. Cookies on optional sibling-product domains (mail, myaccount, docs,
   youtube) are rejected by default.
4. ``--include-domains=mail`` (etc.) opts them back in.
5. ``--include-domains=all`` opts in every optional label.
6. The ``origins`` array is left untouched — it carries localStorage / IndexedDB
   keys per origin and is not part of the cookie blast-radius reduction.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest


def _filter() -> Any:
    """Lazily import the helper so red-phase failures are import-clean.

    The helper does not exist yet in red phase; pytest's collection still
    succeeds because the import is deferred to call time.
    """
    from notebooklm.cli.services.playwright_login import (
        filter_storage_state_cookies_by_domain_policy as _filter_storage_state_cookies_by_domain_policy,
    )

    return _filter_storage_state_cookies_by_domain_policy


def _state(
    cookies: list[dict[str, Any]], origins: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {"cookies": cookies, "origins": origins or []}


def _names(state: dict[str, Any]) -> set[str]:
    return {c["name"] for c in state["cookies"]}


def test_keeps_required_google_com_cookies() -> None:
    state = _state(
        [
            {"name": "SID", "value": "v1", "domain": ".google.com", "path": "/"},
            {"name": "__Secure-1PSID", "value": "v2", "domain": ".google.com", "path": "/"},
            {
                "name": "OSID",
                "value": "v3",
                "domain": "accounts.google.com",
                "path": "/",
            },
        ]
    )
    out = _filter()(state)
    assert _names(out) == {"SID", "__Secure-1PSID", "OSID"}


def test_keeps_notebooklm_host_cookies() -> None:
    state = _state(
        [
            {
                "name": "NID",
                "value": "v1",
                "domain": "notebooklm.google.com",
                "path": "/",
            },
            {
                "name": "Secure-LM",
                "value": "v2",
                "domain": ".notebooklm.cloud.google.com",
                "path": "/",
            },
        ]
    )
    out = _filter()(state)
    assert _names(out) == {"NID", "Secure-LM"}


def test_keeps_regional_cctld_cookies() -> None:
    state = _state(
        [
            {"name": "REG_SG", "value": "v1", "domain": ".google.com.sg", "path": "/"},
            {"name": "REG_UK", "value": "v2", "domain": ".google.co.uk", "path": "/"},
            {"name": "REG_DE", "value": "v3", "domain": ".google.de", "path": "/"},
        ]
    )
    out = _filter()(state)
    assert _names(out) == {"REG_SG", "REG_UK", "REG_DE"}


def test_rejects_mail_google_com_by_default() -> None:
    """Acceptance criterion (P1-17): mail.google.com is dropped by default."""
    state = _state(
        [
            {"name": "MAIL_SID", "value": "v1", "domain": "mail.google.com", "path": "/"},
            {"name": "MAIL_SID2", "value": "v2", "domain": ".mail.google.com", "path": "/"},
        ]
    )
    out = _filter()(state)
    assert _names(out) == set()


def test_rejects_other_sibling_product_domains_by_default() -> None:
    state = _state(
        [
            {"name": "Y_SID", "value": "v1", "domain": ".youtube.com", "path": "/"},
            {"name": "MYA_SID", "value": "v2", "domain": "myaccount.google.com", "path": "/"},
            {"name": "DOCS_SID", "value": "v3", "domain": "docs.google.com", "path": "/"},
        ]
    )
    out = _filter()(state)
    assert _names(out) == set()


def test_rejects_lookalike_domains() -> None:
    """A cookie on ``evil-google.com`` must never pass the filter."""
    state = _state(
        [
            {"name": "EVIL", "value": "v1", "domain": "evil-google.com", "path": "/"},
            {"name": "EVIL2", "value": "v2", "domain": "google.com.evil.com", "path": "/"},
        ]
    )
    out = _filter()(state)
    assert _names(out) == set()


def test_include_domains_mail_opts_in_mail_google_com() -> None:
    state = _state(
        [
            {"name": "MAIL_SID", "value": "v1", "domain": "mail.google.com", "path": "/"},
            {"name": "Y_SID", "value": "v2", "domain": ".youtube.com", "path": "/"},
        ]
    )
    out = _filter()(state, include_domains={"mail"})
    assert _names(out) == {"MAIL_SID"}  # mail in, youtube still out


def test_include_domains_all_opts_in_every_optional_label() -> None:
    state = _state(
        [
            {"name": "MAIL_SID", "value": "v1", "domain": "mail.google.com", "path": "/"},
            {"name": "Y_SID", "value": "v2", "domain": ".youtube.com", "path": "/"},
            {"name": "MYA_SID", "value": "v3", "domain": "myaccount.google.com", "path": "/"},
            {"name": "DOCS_SID", "value": "v4", "domain": "docs.google.com", "path": "/"},
        ]
    )
    out = _filter()(state, include_domains={"all"})
    assert _names(out) == {"MAIL_SID", "Y_SID", "MYA_SID", "DOCS_SID"}


def test_origins_array_preserved_verbatim() -> None:
    state = _state(
        [{"name": "SID", "value": "v1", "domain": ".google.com", "path": "/"}],
        origins=[
            {
                "origin": "https://notebooklm.google.com",
                "localStorage": [{"name": "k", "value": "v"}],
            }
        ],
    )
    out = _filter()(state)
    assert out["origins"] == state["origins"]


def test_empty_storage_state_round_trips() -> None:
    out = _filter()({"cookies": [], "origins": []})
    assert out == {"cookies": [], "origins": []}


def test_filter_does_not_mutate_input() -> None:
    """The helper must return a new dict so the caller can compare before/after.

    CodeRabbit feedback: deep-copy each cookie dict, not just the outer list,
    so any in-place mutation of an individual cookie dict (e.g. accidental
    ``cookie["domain"] = …`` inside the filter) is caught — a shallow
    ``list(state["cookies"])`` would let nested mutations slip through.
    """
    state = _state(
        [
            {"name": "SID", "value": "v1", "domain": ".google.com", "path": "/"},
            {"name": "MAIL_SID", "value": "v2", "domain": "mail.google.com", "path": "/"},
        ]
    )
    original_cookies = copy.deepcopy(state["cookies"])
    _filter()(state)
    # Source dict unchanged at every depth.
    assert state["cookies"] == original_cookies


@pytest.mark.parametrize(
    "domain",
    [
        "mail.google.com",
        ".mail.google.com",
        "myaccount.google.com",
        ".myaccount.google.com",
        "docs.google.com",
        ".docs.google.com",
        ".youtube.com",
        "youtube.com",
        "accounts.youtube.com",
    ],
)
def test_acceptance_sibling_domains_all_rejected_by_default(domain: str) -> None:
    state = _state([{"name": "C", "value": "v", "domain": domain, "path": "/"}])
    out = _filter()(state)
    assert _names(out) == set()


# ---------------------------------------------------------------------------
# Hardening (#1513): malformed-row skips + exact-identity duplicate dedup
#
# Identity is the full RFC 6265 triple (name, domain, path) — NOT the bare
# name. Same-name rows on different domains/paths must ALL survive: the
# runtime loader ``build_httpx_cookies_from_storage`` keys (name, domain,
# path) and legitimately holds e.g. per-product ``OSID`` rows on
# ``notebooklm.google.com`` and ``myaccount.google.com`` as distinct jar
# entries. Cross-domain same-name resolution stays a LOAD-time concern
# (the flat loaders rank by ``_auth_domain_priority``).
# ---------------------------------------------------------------------------

_FILTER_LOGGER = "notebooklm._auth.browser_capture"


def test_non_dict_cookie_entry_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """Malformed non-dict rows (rookiepy/Playwright drift) are skipped, not raised.

    Pre-#1513 the filter called ``.get`` on every entry and a non-dict row
    crashed the whole persist; now the bad row is dropped with a bounded
    warning and the surviving cookies still make it to disk.
    """
    state: dict[str, Any] = {
        "cookies": [
            {"name": "SID", "value": "v1", "domain": ".google.com", "path": "/"},
            "not-a-cookie",
            42,
            None,
            ["domain", ".google.com"],
            {"name": "OSID", "value": "v2", "domain": "accounts.google.com", "path": "/"},
        ],
        "origins": [],
    }
    with caplog.at_level("WARNING", logger=_FILTER_LOGGER):
        out = _filter()(state)
    assert _names(out) == {"SID", "OSID"}
    skip_records = [r for r in caplog.records if "not a dict" in r.getMessage()]
    assert len(skip_records) == 4  # one bounded warning per malformed row


def test_non_str_domain_cookie_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """Cookies whose ``domain`` is not a str are skipped instead of raising in ``.lstrip``."""
    state: dict[str, Any] = {
        "cookies": [
            {"name": "SID", "value": "v1", "domain": ".google.com", "path": "/"},
            {"name": "BAD_INT", "value": "v2", "domain": 123, "path": "/"},
            {"name": "BAD_NONE", "value": "v3", "domain": None, "path": "/"},
        ],
        "origins": [],
    }
    with caplog.at_level("WARNING", logger=_FILTER_LOGGER):
        out = _filter()(state)
    assert _names(out) == {"SID"}
    skip_records = [r for r in caplog.records if "non-str domain" in r.getMessage()]
    assert len(skip_records) == 2


def test_missing_or_non_str_name_cookie_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """Rows without a usable name are malformed under Playwright's own schema.

    Skipped with the same bounded warning as non-str domains (and matching the
    flat loaders, which drop falsy names), so every kept row carries the full
    ``(name, domain, path)`` identity the dedup and the runtime loader key on.
    """
    state: dict[str, Any] = {
        "cookies": [
            {"name": "SID", "value": "v1", "domain": ".google.com", "path": "/"},
            {"value": "v2", "domain": ".google.com", "path": "/"},  # missing name
            {"name": "", "value": "v3", "domain": ".google.com", "path": "/"},  # empty name
            {"name": 123, "value": "v4", "domain": ".google.com", "path": "/"},  # non-str name
        ],
        "origins": [],
    }
    with caplog.at_level("WARNING", logger=_FILTER_LOGGER):
        out = _filter()(state)
    assert _names(out) == {"SID"}
    skip_records = [r for r in caplog.records if "non-str name" in r.getMessage()]
    assert len(skip_records) == 3


def test_non_str_path_cookie_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """A present-but-non-str ``path`` is malformed and skipped.

    ``path`` participates in the dedup identity and is normalized with
    ``or "/"``; an int/list path would slip past that guard and later crash
    ``http.cookiejar``/``httpx`` path matching. Absent/``None`` path is fine
    (it normalizes to the root path), so the well-formed row survives.
    """
    state: dict[str, Any] = {
        "cookies": [
            {"name": "SID", "value": "v1", "domain": ".google.com"},  # path absent -> "/"
            {"name": "OSID", "value": "v2", "domain": ".google.com", "path": 5},  # non-str
            {"name": "HSID", "value": "v3", "domain": ".google.com", "path": ["/"]},  # non-str
        ],
        "origins": [],
    }
    with caplog.at_level("WARNING", logger=_FILTER_LOGGER):
        out = _filter()(state)
    assert _names(out) == {"SID"}
    skip_records = [r for r in caplog.records if "non-str path" in r.getMessage()]
    assert len(skip_records) == 2


@pytest.mark.parametrize("base_first", [True, False], ids=["base-first", "subdomain-first"])
def test_duplicate_name_across_domains_both_kept(base_first: bool) -> None:
    """Same name on two allowed domains: BOTH rows persist, in either input order.

    ``OSID`` is a per-product cookie (docs/auth-cookie-lifecycle.md) that
    legitimately exists on multiple domains at once; the (name, domain,
    path)-keyed runtime loader keeps both as distinct jar entries, so write
    time must never collapse them by name.
    """
    base = {"name": "OSID", "value": "base", "domain": ".google.com", "path": "/"}
    sub = {"name": "OSID", "value": "sub", "domain": "notebooklm.google.com", "path": "/"}
    cookies = [base, sub] if base_first else [sub, base]
    out = _filter()(_state(cookies))
    assert out["cookies"] == cookies


def test_same_name_same_domain_different_paths_both_kept() -> None:
    """Path is part of cookie identity (RFC 6265 §5.3, issue #369): both rows persist."""
    root = {"name": "NID", "value": "root", "domain": ".google.com", "path": "/"}
    scoped = {"name": "NID", "value": "scoped", "domain": ".google.com", "path": "/foo"}
    out = _filter()(_state([root, scoped]))
    assert out["cookies"] == [root, scoped]


@pytest.mark.parametrize("newer_last", [True, False], ids=["newer-last", "newer-first"])
def test_exact_identity_duplicate_last_occurrence_wins(newer_last: bool) -> None:
    """Exact ``(name, domain, path)`` duplicates collapse to ONE row: last wins, whole.

    Mirrors ``save_cookies_to_storage``'s persistence merge, where the newer
    observation overwrites the stored row for the same key — the rule is a
    deterministic function of input order (later row = newer observation),
    and the winner keeps its full metadata (never field-merged).
    """
    older = {
        "name": "SID",
        "value": "stale",
        "domain": ".google.com",
        "path": "/",
        "expires": 111.0,
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    }
    newer = {
        "name": "SID",
        "value": "fresh",
        "domain": ".google.com",
        "path": "/",
        "expires": 1893456000.5,
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    }
    cookies = [older, newer] if newer_last else [newer, older]
    out = _filter()(_state(cookies))
    assert out["cookies"] == [cookies[-1]]


def test_empty_path_normalizes_to_root_for_identity() -> None:
    """``"path": ""`` and ``"path": "/"`` are the same identity (loader/merge parity).

    Every loader and the ``save_cookies_to_storage`` merge key normalize the
    path component via ``or "/"``; the write-time dedup must agree or an
    empty-path twin would survive as a phantom duplicate row.
    """
    empty = {"name": "SID", "value": "old", "domain": ".google.com", "path": ""}
    root = {"name": "SID", "value": "new", "domain": ".google.com", "path": "/"}
    out = _filter()(_state([empty, root]))
    assert out["cookies"] == [root]


def test_filtered_output_round_trips_into_path_aware_loader(tmp_path: Any) -> None:
    """Write-time filtering never starves the (name, domain, path)-keyed loader.

    The filtered storage_state — holding per-product ``OSID`` rows on BOTH
    ``notebooklm.google.com`` and the opt-in ``myaccount.google.com`` — feeds
    ``build_httpx_cookies_from_storage`` (the ``AuthTokens.from_storage``
    runtime loader), and both rows must land as distinct jar entries.
    """
    import json

    from notebooklm._auth.cookies import build_httpx_cookies_from_storage

    state = _state(
        [
            {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
            {"name": "__Secure-1PSIDTS", "value": "ts", "domain": ".google.com", "path": "/"},
            {"name": "OSID", "value": "osid-nblm", "domain": "notebooklm.google.com", "path": "/"},
            {"name": "OSID", "value": "osid-mya", "domain": "myaccount.google.com", "path": "/"},
        ]
    )
    filtered = _filter()(state, include_domains={"myaccount"})
    assert len(filtered["cookies"]) == 4  # both OSID rows survive write time

    storage_path = tmp_path / "storage_state.json"
    storage_path.write_text(json.dumps(filtered), encoding="utf-8")
    jar = build_httpx_cookies_from_storage(storage_path).jar

    osid_entries = {(c.domain, c.value) for c in jar if c.name == "OSID"}
    assert osid_entries == {
        ("notebooklm.google.com", "osid-nblm"),
        ("myaccount.google.com", "osid-mya"),
    }


def test_no_duplicates_well_formed_passthrough_unchanged() -> None:
    """Characterization: distinct, well-formed cookies pass through verbatim, in order."""
    cookies = [
        {"name": "SID", "value": "v1", "domain": ".google.com", "path": "/"},
        {"name": "OSID", "value": "v2", "domain": "accounts.google.com", "path": "/"},
        {"name": "NID", "value": "v3", "domain": "notebooklm.google.com", "path": "/"},
    ]
    out = _filter()(_state(cookies))
    assert out["cookies"] == cookies
