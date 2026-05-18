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
    from notebooklm.cli.session import _filter_storage_state_cookies_by_domain_policy

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
