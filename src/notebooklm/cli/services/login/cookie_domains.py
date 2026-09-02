"""Cookie-domain allowlist + ``--include-domains`` policy helpers.

Leaf module within the ``cli/services/login/`` package (no in-package
imports). Owns:

- :data:`_INCLUDE_DOMAINS_ALL` — sentinel label for "include every optional
  sibling-product domain".
- :func:`_parse_include_domains` — pure label parser.
- :func:`_warn_missing_optional_domains` — migration-warning message builder.
- :func:`_resolve_optional_cookie_domains` — label → domain-set resolver.
- :func:`_build_google_cookie_domains` — final domain list builder.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ...._app.login_cookie import (
    CookieDomainFailure,
    parse_cookie_domain_labels,
    resolve_cookie_domains,
)

logger = logging.getLogger(__name__)


_INCLUDE_DOMAINS_ALL = "all"


class IncludeDomainsParseError(ValueError):
    """Raised when ``--include-domains`` contains an unknown label."""


def _parse_include_domains(values: tuple[str, ...]) -> set[str]:
    """Parse one or more ``--include-domains`` flag values into labels.

    Accepts both ``--include-domains=youtube --include-domains=docs`` and
    ``--include-domains=youtube,docs`` (and any mix). Whitespace around
    commas is tolerated. Empty fragments are dropped.

    Raises:
        IncludeDomainsParseError: if any label is not one of
            :data:`notebooklm.auth.OPTIONAL_COOKIE_DOMAINS_BY_LABEL` keys
            (or the literal ``"all"``).
    """
    selection = None
    try:
        selection = parse_cookie_domain_labels(values)
        if isinstance(selection, CookieDomainFailure):
            raise IncludeDomainsParseError(
                "unknown --include-domains label(s): "
                f"{', '.join(selection.invalid_labels)}. "
                f"Supported: {', '.join(selection.supported_labels)}."
            )
        return set(selection.labels)
    finally:
        del values, selection


def _warn_missing_optional_domains(
    include_domains: set[str],
    *,
    warn: Callable[[str], None] | None = None,
) -> str | None:
    """Build or emit the migration warning for the reduced request default.

    The cookie-domain split narrows the default extraction set to
    :data:`REQUIRED_COOKIE_DOMAINS`. Users upgrading from the prior
    behavior need a heads-up that sibling hosts are no longer explicitly
    requested at login. Trusted Google-root cookies returned by the extractor
    are still retained for compatibility; distinct roots such as YouTube are
    excluded unless opted in.

    ``warn`` is injected by the command layer for interactive CLI runs so
    this service module does not import presentation helpers.
    """
    selection = supported = message = None
    try:
        if include_domains:
            return None
        selection = resolve_cookie_domains(set())
        supported = ", ".join(
            label for label in selection.supported_labels if label != _INCLUDE_DOMAINS_ALL
        )
        message = (
            "[dim]Note: sibling-product domains are not explicitly requested by default. "
            "Cookies under trusted Google roots may still be retained for compatibility. "
            f"Pass --include-domains=<{supported}> (or =all) to request them.[/dim]"
        )
        if warn is not None:
            warn(message)
        logger.info(
            "Login explicitly requesting REQUIRED_COOKIE_DOMAINS only. Trusted Google-root "
            "subdomains returned by the extractor remain compatible. Pass --include-domains=%s "
            "(or =all) to request known sibling hosts.",
            supported,
        )
        return message
    finally:
        del include_domains, warn, selection, supported, message


def _resolve_optional_cookie_domains(labels: set[str]) -> frozenset[str]:
    """Resolve ``--include-domains`` labels to the union of their domain sets.

    Contract: ``labels`` must be the output of
    :func:`_parse_include_domains`, which validates that every label is in
    :data:`OPTIONAL_COOKIE_DOMAINS_BY_LABEL` (or the literal ``"all"``).
    Callers are expected to surface parser errors before we ever reach
    this function; the dict lookup below is
    therefore unguarded by design.
    """
    selection = None
    try:
        selection = resolve_cookie_domains(labels)
        return selection.optional_domains
    finally:
        del labels, selection


def _build_google_cookie_domains(
    *,
    include_optional: bool = False,
    include_domains: set[str] | None = None,
) -> list[str]:
    """Return the cookie-domain list fed to extractors (rookie-cookies / Firefox).

    Defaults to :data:`REQUIRED_COOKIE_DOMAINS` plus all known regional
    ``.google.<ccTLD>`` variants. Sibling-product hosts (YouTube, Docs,
    myaccount, Mail) are not explicitly requested unless the caller opts in via
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
        Sorted cookie-domain strings (suitable for ``rookie_cookies.load(
        domains=...)`` or :func:`extract_firefox_container_cookies`).
    """
    labels = selection = None
    try:
        labels = include_domains if include_domains is not None else set()
        selection = resolve_cookie_domains(labels, include_optional=include_optional)
        return list(selection.extraction_domains)
    finally:
        del include_domains, labels, selection
