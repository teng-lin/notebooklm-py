"""Cookie-domain allowlist + ``--include-domains`` policy helpers.

Leaf module within the ``cli/services/login/`` package (no in-package
imports). Owns:

- :data:`_INCLUDE_DOMAINS_ALL` — sentinel label for "include every optional
  sibling-product domain".
- :func:`_parse_include_domains` — Click value parser.
- :func:`_warn_missing_optional_domains` — migration warning.
- :func:`_resolve_optional_cookie_domains` — label → domain-set resolver.
- :func:`_build_google_cookie_domains` — final domain list builder.
"""

from __future__ import annotations

import logging

import click

from ....auth import (
    GOOGLE_REGIONAL_CCTLDS,
    OPTIONAL_COOKIE_DOMAINS_BY_LABEL,
    REQUIRED_COOKIE_DOMAINS,
)
from ...rendering import console

logger = logging.getLogger(__name__)


_INCLUDE_DOMAINS_ALL = "all"


def _parse_include_domains(values: tuple[str, ...]) -> set[str]:
    """Parse one or more ``--include-domains`` flag values into labels.

    Accepts both ``--include-domains=youtube --include-domains=docs`` and
    ``--include-domains=youtube,docs`` (and any mix). Whitespace around
    commas is tolerated. Empty fragments are dropped.

    Raises:
        click.BadParameter: if any label is not one of
            :data:`notebooklm.auth.OPTIONAL_COOKIE_DOMAINS_BY_LABEL` keys
            (or the literal ``"all"``).
    """
    labels: set[str] = set()
    for raw in values:
        for part in raw.split(","):
            label = part.strip().lower()
            if not label:
                continue
            labels.add(label)
    if not labels:
        return labels
    valid = set(OPTIONAL_COOKIE_DOMAINS_BY_LABEL) | {_INCLUDE_DOMAINS_ALL}
    bad = labels - valid
    if bad:
        supported = ", ".join(sorted(valid))
        raise click.BadParameter(
            f"unknown --include-domains label(s): {', '.join(sorted(bad))}. Supported: {supported}."
        )
    return labels


def _warn_missing_optional_domains(include_domains: set[str]) -> None:
    """Emit a migration warning when the default minimum-cookies set is used.

    The cookie-domain split narrows the default extraction set to
    :data:`REQUIRED_COOKIE_DOMAINS`. Users upgrading from the prior
    behavior need a heads-up that YouTube / Docs / myaccount / Mail
    cookies are no longer scraped at login. Telling them how to opt back
    in is the entire point of the warning.
    """
    if include_domains:
        return
    supported = ", ".join(sorted(OPTIONAL_COOKIE_DOMAINS_BY_LABEL))
    console.print(
        "[dim]Note: sibling-product cookies not included by default. "
        f"Pass --include-domains=<{supported}> (or =all) to extract them.[/dim]"
    )
    logger.info(
        "Login extracting REQUIRED_COOKIE_DOMAINS only (cookie-domain split default). "
        "Pass --include-domains=%s (or =all) to include sibling cookies.",
        supported,
    )


def _resolve_optional_cookie_domains(labels: set[str]) -> frozenset[str]:
    """Resolve ``--include-domains`` labels to the union of their domain sets.

    Contract: ``labels`` must be the output of
    :func:`_parse_include_domains`, which validates that every label is in
    :data:`OPTIONAL_COOKIE_DOMAINS_BY_LABEL` (or the literal ``"all"``).
    Callers are expected to surface the ``click.BadParameter`` from the
    parser before we ever reach this function; the dict lookup below is
    therefore unguarded by design.
    """
    if not labels:
        return frozenset()
    if _INCLUDE_DOMAINS_ALL in labels:
        return frozenset().union(*OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values())
    selected: set[str] = set()
    for label in labels:
        # ``_parse_include_domains`` guarantees ``label`` is a valid key
        # (or ``"all"``, handled above). Unguarded lookup is intentional —
        # a KeyError here would be a bug in our own validation, not user
        # input.
        selected.update(OPTIONAL_COOKIE_DOMAINS_BY_LABEL[label])
    return frozenset(selected)


def _build_google_cookie_domains(
    *,
    include_optional: bool = False,
    include_domains: set[str] | None = None,
) -> list[str]:
    """Return the cookie-domain list fed to extractors (rookiepy / Firefox).

    Defaults to :data:`REQUIRED_COOKIE_DOMAINS` plus all known regional
    ``.google.<ccTLD>`` variants. Sibling-product cookies (YouTube, Docs,
    myaccount, Mail) are excluded unless the caller opts in via
    ``include_optional=True`` or a non-empty ``include_domains`` label
    set.

    Args:
        include_optional: When ``True``, include every optional sibling
            domain (equivalent to ``--include-domains=all``). Preserves
            the pre-split behavior for callers that still need the broad
            set.
        include_domains: Set of optional-domain labels (output of
            :func:`_parse_include_domains`). Each label expands via
            :data:`OPTIONAL_COOKIE_DOMAINS_BY_LABEL`. ``"all"`` is
            accepted as a shortcut for every label.

    Returns:
        List of cookie-domain strings (suitable for ``rookiepy.load(
        domains=...)`` or :func:`extract_firefox_container_cookies`).
    """
    selected_optional: frozenset[str]
    if include_domains:
        selected_optional = _resolve_optional_cookie_domains(include_domains)
    elif include_optional:
        selected_optional = frozenset().union(*OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values())
    else:
        selected_optional = frozenset()

    domains: list[str] = list(REQUIRED_COOKIE_DOMAINS | selected_optional)
    for cctld in GOOGLE_REGIONAL_CCTLDS:
        domain = f".google.{cctld}"
        if domain not in domains:
            domains.append(domain)
    return domains
