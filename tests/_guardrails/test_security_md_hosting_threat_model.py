"""Pin the operator MCP/REST hosting threat model in ``SECURITY.md`` (#2387).

``SECURITY.md`` once described a CLI cookie client and claimed
``No long-lived API keys or OAuth tokens``. That is false for
``notebooklm-mcp`` HTTP + OAuth. This gate fails if that claim returns
without the MCP OAuth correction, and requires the listed hosting facts
plus links to ``docs/security.md``, ``docs/mcp-guide.md``, and ADR-0024.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SECURITY_MD = REPO_ROOT / "SECURITY.md"
DOCS_SECURITY = REPO_ROOT / "docs" / "security.md"
MCP_GUIDE = REPO_ROOT / "docs" / "mcp-guide.md"
ADR_0024 = REPO_ROOT / "docs" / "adr" / "0024-mcp-remote-file-transfer.md"

FALSE_NO_LONG_LIVED_CLAIM = re.compile(
    r"No long-lived API keys or OAuth tokens",
    re.IGNORECASE,
)


def _normalized(text: str) -> str:
    """Strip emphasis markers and collapse whitespace for prose matching."""
    return re.sub(r"\s+", " ", text.replace("**", "").replace("`", ""))


def _has_mcp_oauth_correction(text: str) -> bool:
    """Return whether *text* documents MCP OAuth refresh-token lifetime."""
    compact = _normalized(text)
    has_password = "NOTEBOOKLM_MCP_OAUTH_PASSWORD" in compact
    has_no_revoke = re.search(
        r"NOTEBOOKLM_MCP_OAUTH_PASSWORD.{0,160}does not revoke",
        compact,
        re.IGNORECASE,
    )
    has_refresh = re.search(
        r"refresh tokens? are long-lived",
        compact,
        re.IGNORECASE,
    )
    return bool(has_password and has_no_revoke and has_refresh)


def uncorrected_no_long_lived_oauth_claim(text: str) -> bool:
    """True when the old Session Security bullet is back without the correction."""
    if not FALSE_NO_LONG_LIVED_CLAIM.search(text):
        return False
    return not _has_mcp_oauth_correction(text)


def hosting_threat_model_gaps(security_md: str) -> list[str]:
    """Return missing operator facts in an operator-facing ``SECURITY.md`` body."""
    compact = _normalized(security_md)
    gaps: list[str] = []

    if uncorrected_no_long_lived_oauth_claim(security_md):
        gaps.append("uncorrected no-long-lived-oauth claim")
    if not _has_mcp_oauth_correction(security_md):
        gaps.append("MCP OAuth refresh-token correction")

    if not re.search(
        r"master_token\.json.{0,240}account-equivalent"
        r"|account-equivalent.{0,240}master_token\.json",
        compact,
        re.IGNORECASE,
    ):
        gaps.append("master_token.json is account-equivalent")

    if not re.search(
        r"source_add.{0,120}path.{0,160}server[- ]host"
        r"|server[- ]host.{0,120}source_add",
        compact,
        re.IGNORECASE,
    ):
        gaps.append("stdio source_add(path) reads server-host files")

    if not (
        "/files/dl" in compact
        and "/files/ul" in compact
        and "HMAC" in compact
        and re.search(
            r"HMAC.{0,160}(not|without).{0,60}(bearer|OAuth)",
            compact,
            re.IGNORECASE,
        )
    ):
        gaps.append("/files/dl and /files/ul are HMAC-URL auth only")

    if not (
        re.search(r"\bDCR\b", compact)
        and re.search(
            r"(does not|cannot) bypass.{0,80}(login )?password",
            compact,
            re.IGNORECASE,
        )
    ):
        gaps.append("open OAuth DCR does not bypass the login password")

    if not re.search(
        r"loopback.{0,120}tokenless|tokenless.{0,120}loopback",
        compact,
        re.IGNORECASE,
    ):
        gaps.append("MCP loopback HTTP may be tokenless")
    if not re.search(r"Host-header|Host header", compact, re.IGNORECASE):
        gaps.append("Host-header rebinding guard")
    if "rebinding" not in compact.lower():
        gaps.append("DNS-rebinding guard")
    if not (
        "NOTEBOOKLM_SERVER_TOKEN_FILE" in compact
        and re.search(
            r"REST /v1 routes require a bearer token",
            compact,
            re.IGNORECASE,
        )
    ):
        gaps.append("REST /v1 routes require a bearer token, with token-file support")

    if not re.search(r"/healthz.{0,60}\b(public|token[- ]?less)\b", compact, re.IGNORECASE):
        gaps.append("GET /healthz is public or tokenless")

    if not (
        "/healthz" in compact
        and "liveness" in compact.lower()
        and "readiness" in compact.lower()
        and re.search(r'\{\s*"ok"\s*:\s*true\s*\}', compact)
    ):
        gaps.append("GET /healthz is liveness not readiness")

    if not (
        "pip-audit" in compact
        and re.search(
            r"browser.{0,40}dev.{0,40}markdown|browser\+dev\+markdown",
            compact,
            re.IGNORECASE,
        )
        and re.search(r"\bmcp\b", compact, re.IGNORECASE)
        and re.search(r"\bserver\b", compact, re.IGNORECASE)
        and re.search(
            r"internet-facing|internet facing",
            compact,
            re.IGNORECASE,
        )
    ):
        gaps.append("pip-audit default extras vs MCP/REST extras")

    if "docs/security.md" not in security_md:
        gaps.append("link to docs/security.md")
    if "docs/mcp-guide.md" not in security_md:
        gaps.append("link to docs/mcp-guide.md")
    if "ADR-0024" not in security_md:
        gaps.append("ADR-0024 reference")
    if "docs/adr/0024-mcp-remote-file-transfer.md" not in security_md:
        gaps.append("link to ADR-0024 file")

    if not re.search(
        r"oauth.{0,160}(?:0o600|0600)|(?:0o600|0600).{0,160}oauth",
        compact,
        re.IGNORECASE,
    ):
        gaps.append("OAuth state file mode 0600")
    if not re.search(
        r"delete.{0,80}(that file|the (OAuth )?state file|oauth).{0,80}restart"
        r"|real revocation",
        compact,
        re.IGNORECASE,
    ):
        gaps.append("OAuth revocation is delete state file + restart")

    return gaps


def test_uncorrected_no_long_lived_claim_detector() -> None:
    """The old Session Security bullet must fail until MCP OAuth is documented."""
    old_session = (
        "### Session Security\n"
        "- Sessions are cookie-based (standard web authentication)\n"
        "- CSRF tokens are required and automatically handled\n"
        "- No long-lived API keys or OAuth tokens\n"
    )
    assert uncorrected_no_long_lived_oauth_claim(old_session)
    assert "uncorrected no-long-lived-oauth claim" in hosting_threat_model_gaps(old_session)
    assert "MCP OAuth refresh-token correction" in hosting_threat_model_gaps(old_session)

    corrected = (
        old_session
        + "MCP HTTP + OAuth refresh tokens are long-lived. "
        + "Rotating NOTEBOOKLM_MCP_OAUTH_PASSWORD does not revoke them.\n"
    )
    assert not uncorrected_no_long_lived_oauth_claim(corrected)
    assert "uncorrected no-long-lived-oauth claim" not in hosting_threat_model_gaps(corrected)
    assert "MCP OAuth refresh-token correction" not in hosting_threat_model_gaps(corrected)

    assert not uncorrected_no_long_lived_oauth_claim("Sessions are cookie-based.\n")

    storage_only = "storage_state.json is written 0o600.\n"
    assert "OAuth state file mode 0600" in hosting_threat_model_gaps(storage_only)
    oauth_mode = "OAuth state file written 0600.\n"
    assert "OAuth state file mode 0600" not in hosting_threat_model_gaps(oauth_mode)


def test_healthz_access_must_be_explicit() -> None:
    probe = 'GET /healthz is a liveness probe, not readiness. It returns {"ok": true}.'
    gap = "GET /healthz is public or tokenless"
    assert gap in hosting_threat_model_gaps(probe)
    for access in ("public", "tokenless", "token-less"):
        documented = probe.replace("is a liveness", f"is a {access} liveness")
        assert gap not in hosting_threat_model_gaps(documented)


def test_security_md_documents_mcp_rest_hosting_threat_model() -> None:
    """Operator-facing SECURITY.md must carry the MCP/REST hosting facts."""
    text = SECURITY_MD.read_text(encoding="utf-8")
    gaps = hosting_threat_model_gaps(text)
    assert not gaps, (
        "SECURITY.md is missing MCP/REST hosting threat-model facts "
        "(issue #2387):\n- " + "\n- ".join(gaps)
    )


def test_linked_security_pages_retain_supporting_facts() -> None:
    """Linked pages must keep the details SECURITY.md points operators at."""
    docs_security = DOCS_SECURITY.read_text(encoding="utf-8")
    assert "master_token.json" in docs_security
    assert re.search(r"account-equivalent", docs_security, re.IGNORECASE)
    assert "SECURITY.md" in docs_security

    mcp_guide = MCP_GUIDE.read_text(encoding="utf-8")
    compact = _normalized(mcp_guide)
    assert "SECURITY.md" in mcp_guide
    assert "ADR-0024" in mcp_guide
    assert re.search(
        r"source_add.{0,160}(server host|server-host|path)",
        compact,
        re.IGNORECASE,
    )
    assert re.search(
        r"NOTEBOOKLM_MCP_OAUTH_PASSWORD.{0,200}does not revoke"
        r"|does not revoke.{0,200}NOTEBOOKLM_MCP_OAUTH_PASSWORD",
        compact,
        re.IGNORECASE,
    )
    assert re.search(r"HMAC", mcp_guide)
    assert "/files/dl" in mcp_guide and "/files/ul" in mcp_guide
    assert re.search(r"loopback.{0,160}tokenless|tokenless.{0,160}loopback", compact, re.I)

    adr = ADR_0024.read_text(encoding="utf-8")
    assert "/files/dl" in adr and "/files/ul" in adr
    assert re.search(r"HMAC", adr)
    assert re.search(
        r"sole.{0,40}auth|HMAC-signed token is the sole",
        _normalized(adr),
        re.IGNORECASE,
    )
    assert "SECURITY.md" in adr
