"""Cookie-domain preservation guardrail for the two maintenance scripts (#2019).

``scripts/check_rpc_health.py`` and ``scripts/capture_rpc_registry.py`` both
authenticate against live Google hosts, and both used to load credentials
through the *flat* ``load_auth_from_storage()`` mapping, which drops every
cookie's domain. Under ``NOTEBOOKLM_AUTH_JSON`` (how the nightly workflows run)
there is no storage file to recover the domains from, so:

* ``check_rpc_health`` fed the flat dict to ``fetch_tokens``, whose
  ``build_cookie_jar`` fallback re-pinned **every** cookie to ``.google.com``;
* ``capture_rpc_registry`` handed the flat dict straight to
  ``httpx.get(cookies=...)``, which is domain-less by construction.

Host-scoped cookies (``OSID`` / ``__Secure-OSID`` on the NotebookLM host,
``LSID`` on ``accounts.google.com``) were therefore broadcast to every
``*.google.com`` host. After Google's ``notebook.google.com`` cutover the
sign-in redirect chain mints a fresh host ``OSID``; the stale broadcast copy
collides with it and the chain dead-ends on
``accounts.google.com/CookieMismatch``, which the scripts then misreported as
"Authentication expired or invalid".

The live rpc-health workflow cannot gate a PR, so these offline tests pin the
contract instead. Two properties matter, and the fixture is built so neither can
be satisfied by accident:

* the jar each script authenticates with mirrors the ``storage_state`` domains;
* **a specific cookie value never reaches a host it was not scoped to.** The
  fixture deliberately carries ``OSID`` on *both* the NotebookLM host and
  ``accounts.google.com`` with different values, because same-name-different-
  domain is the entire reason domains matter — a name-only assertion
  (``"OSID" not in sent``) would be wrong here, and flattening destroys the
  accounts-scoped value outright.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm._auth.tokens import AuthTokens
from notebooklm._env import get_base_url
from scripts import capture_rpc_registry, check_rpc_health

_ACCOUNTS_HOST = "accounts.google.com"
_SIGN_IN_PATH = "/ServiceLogin"
_SIGN_IN_URL = f"https://{_ACCOUNTS_HOST}{_SIGN_IN_PATH}"

_HOMEPAGE_HTML = (
    '<html><script>window.WIZ_global_data={"SNlM0e":"csrf_ok","FdrFJe":"sess_ok"};</script></html>'
)

# The sign-in chain mints a *fresh* host-scoped OSID on the way back — the exact
# mechanism #2019 describes. Simulating it keeps the jar assertions honest: they
# must survive a rotation rather than pinning a jar that never changes.
_MINTED_OSID = "v-osid-minted"


def _notebook_host() -> str:
    """The NotebookLM host, read from the library rather than pinned literally.

    Resolved at call time (``get_base_url()`` is env-overridable) so the fixture
    host and the mocked URLs can never disagree, and so this file keeps working
    through the Gemini Notebook rebrand (ADR-0028).
    """
    return httpx.URL(get_base_url()).host


def _cookie_entry(name: str, value: str, domain: str) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    }


def _storage_state() -> dict[str, Any]:
    """Storage state spanning three hosts, mirroring a real logged-in profile.

    ``SID`` + ``__Secure-1PSIDTS`` are the ``MINIMUM_REQUIRED_COOKIES`` the auth
    validators demand. ``OSID`` / ``__Secure-OSID`` are the per-product binding
    cookies Google scopes to the app host, and ``LSID`` is scoped to the sign-in
    host — the population #2019 broadcast domain-wide. ``OSID`` appears on *two*
    hosts with different values so the wire assertions can distinguish "the right
    OSID arrived" from "an OSID arrived".
    """
    return {
        "cookies": [
            _cookie_entry("SID", "v-sid", ".google.com"),
            _cookie_entry("__Secure-1PSIDTS", "v-psidts", ".google.com"),
            _cookie_entry("OSID", "v-osid-app", _notebook_host()),
            _cookie_entry("__Secure-OSID", "v-secure-osid-app", _notebook_host()),
            _cookie_entry("OSID", "v-osid-accounts", _ACCOUNTS_HOST),
            _cookie_entry("LSID", "v-lsid", _ACCOUNTS_HOST),
        ],
        "origins": [],
    }


def _name_domain_pairs(storage_state: dict[str, Any]) -> set[tuple[str, str]]:
    return {(entry["name"], entry["domain"]) for entry in storage_state["cookies"]}


def _jar_pairs(jar: httpx.Cookies) -> set[tuple[str, str]]:
    return {(cookie.name, cookie.domain) for cookie in jar.jar}


def _jar_value(jar: httpx.Cookies, name: str, domain: str) -> str | None:
    for cookie in jar.jar:
        if cookie.name == name and cookie.domain == domain:
            return cookie.value
    return None


def _cookie_pairs(header: str) -> set[tuple[str, str]]:
    """``(name, value)`` pairs from a ``Cookie:`` header.

    Values are kept deliberately: the contract is "the *app-host* OSID never
    reaches the sign-in host", which a name-only view cannot express once the
    sign-in host legitimately has an OSID of its own.
    """
    pairs: set[tuple[str, str]] = set()
    for chunk in header.split(";"):
        chunk = chunk.strip()
        if "=" in chunk:
            name, value = chunk.split("=", 1)
            pairs.add((name.strip(), value.strip()))
    return pairs


def _cookies_sent_to(
    httpx_mock: HTTPXMock, host: str, path: str | None = None
) -> list[set[tuple[str, str]]]:
    """``(name, value)`` pairs carried by each recorded request to ``host``.

    ``path`` narrows to one endpoint. The sign-in assertions use it so they
    cannot be satisfied by the autouse ``accounts.google.com/RotateCookies``
    keepalive mock in ``tests/conftest.py`` instead of the redirect hop.
    """
    return [
        _cookie_pairs(request.headers.get("cookie", ""))
        for request in httpx_mock.get_requests()
        if request.url.host == host and (path is None or request.url.path == path)
    ]


def _register_sign_in_chain(
    httpx_mock: HTTPXMock, homepage_url: str, *, final_html: str = _HOMEPAGE_HTML
) -> None:
    """NotebookLM → sign-in → NotebookLM, minting a fresh host OSID at the end.

    This is the post-cutover redirect shape that broke the nightly canary. Both
    scripts follow redirects on their authenticated read, so both go through it;
    ``final_html`` is the only thing that differs (WIZ tokens for the health
    check, a bundle URL for the drift monitor).

    ``pytest_httpx`` defaults to ``is_reusable=False`` +
    ``assert_all_responses_were_requested=True``, so an extra hop errors loudly
    and a skipped hop fails teardown.
    """
    httpx_mock.add_response(url=homepage_url, status_code=302, headers={"Location": _SIGN_IN_URL})
    httpx_mock.add_response(url=_SIGN_IN_URL, status_code=302, headers={"Location": homepage_url})
    httpx_mock.add_response(
        url=homepage_url,
        status_code=200,
        html=final_html,
        headers={"Set-Cookie": f"OSID={_MINTED_OSID}; Path=/; Secure; HttpOnly"},
    )


def _assert_sign_in_hop_correctly_scoped(httpx_mock: HTTPXMock) -> None:
    """The sign-in hop gets the sign-in host's cookies and nothing else."""
    sent = _cookies_sent_to(httpx_mock, _ACCOUNTS_HOST, _SIGN_IN_PATH)
    assert sent, f"expected a {_SIGN_IN_URL} hop"
    for pairs in sent:
        assert ("OSID", "v-osid-app") not in pairs, (
            "the app-host OSID must not be broadcast to the sign-in host "
            f"(issue #2019); sent: {sorted(pairs)}"
        )
        assert ("OSID", _MINTED_OSID) not in pairs
        assert ("__Secure-OSID", "v-secure-osid-app") not in pairs
        # Positive controls: the cookies that *are* scoped to the sign-in host
        # still ride along, so the assertions above cannot pass by dropping
        # cookies wholesale — including the same-named OSID that belongs here.
        assert {("OSID", "v-osid-accounts"), ("LSID", "v-lsid"), ("SID", "v-sid")} <= pairs


def _assert_app_hop_correctly_scoped(httpx_mock: HTTPXMock) -> None:
    """The app host gets its own binding cookies and not the sign-in host's."""
    sent = _cookies_sent_to(httpx_mock, _notebook_host())
    assert sent, "expected at least one NotebookLM-host request"
    for pairs in sent:
        assert ("LSID", "v-lsid") not in pairs, (
            f"the sign-in-host LSID must not be sent to {_notebook_host()}; sent: {sorted(pairs)}"
        )
        assert ("OSID", "v-osid-accounts") not in pairs
        assert ("__Secure-OSID", "v-secure-osid-app") in pairs
        assert ("SID", "v-sid") in pairs


async def test_check_rpc_health_auth_preserves_cookie_domains(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """``check_rpc_health.load_auth`` must build a domain-preserving jar.

    Drives the real auth path under ``NOTEBOOKLM_AUTH_JSON`` — the CI
    configuration that exposed the bug — against the post-cutover redirect
    chain, and pins both halves of the contract: the resulting jar mirrors
    ``storage_state``, and each hop carries only the cookies scoped to it.

    Fails against the pre-#2019 ``load_auth_from_storage`` + ``fetch_tokens``
    pairing, which collapsed every cookie onto ``.google.com``.
    """
    storage_state = _storage_state()
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(storage_state))
    _register_sign_in_chain(httpx_mock, f"{get_base_url()}/")

    storage_path = check_rpc_health.resolve_storage_path()
    assert storage_path is None
    auth = await check_rpc_health.load_auth(storage_path)

    assert auth.csrf_token == "csrf_ok"
    assert auth.session_id == "sess_ok"

    # Jar-level: the set of (name, domain) pairs still mirrors storage_state
    # even though one cookie rotated, and the rotated value landed on the app
    # host only — it did not overwrite the sign-in host's same-named cookie.
    assert auth.cookie_jar is not None
    assert _jar_pairs(auth.cookie_jar) == _name_domain_pairs(storage_state)
    assert _jar_value(auth.cookie_jar, "OSID", _notebook_host()) == _MINTED_OSID
    assert _jar_value(auth.cookie_jar, "OSID", _ACCOUNTS_HOST) == "v-osid-accounts"

    # Wire-level, both directions.
    _assert_sign_in_hop_correctly_scoped(httpx_mock)
    _assert_app_hop_correctly_scoped(httpx_mock)

    # Routing: a storage state with no in-band account record must still resolve
    # to the default account, so the canary's batchexecute URLs are unchanged.
    assert auth.authuser == 0
    assert auth.account_email is None


async def test_check_rpc_health_auth_preserves_domains_for_file_backed_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """The same contract holds when auth comes from a profile file, not the env.

    This is the configuration #2037 moves CI to. It exercises a different branch
    of ``AuthTokens.from_storage`` (``get_authuser_for_storage`` rather than the
    in-band storage-state read) and, unlike the env-var path, actually persists
    rotated cookies — so the rotated ``OSID`` must reach disk still scoped to the
    app host.
    """
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))

    storage_path = check_rpc_health.resolve_storage_path()
    assert storage_path is not None
    storage_state = _storage_state()
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(json.dumps(storage_state), encoding="utf-8")

    _register_sign_in_chain(httpx_mock, f"{get_base_url()}/")

    auth = await check_rpc_health.load_auth(storage_path)

    assert auth.cookie_jar is not None
    assert _jar_pairs(auth.cookie_jar) == _name_domain_pairs(storage_state)
    _assert_sign_in_hop_correctly_scoped(httpx_mock)
    _assert_app_hop_correctly_scoped(httpx_mock)

    # The rotation was persisted, and persisted with its scope intact — the
    # accounts-scoped OSID must still be there, untouched, alongside it.
    persisted = json.loads(storage_path.read_text(encoding="utf-8"))
    persisted_triples = {(c["name"], c["value"], c["domain"]) for c in persisted["cookies"]}
    assert ("OSID", _MINTED_OSID, _notebook_host()) in persisted_triples
    assert ("OSID", "v-osid-accounts", _ACCOUNTS_HOST) in persisted_triples


def test_capture_rpc_registry_sends_domain_scoped_cookie_jar(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """``fetch_bundle``'s authenticated homepage read must be domain-scoped.

    The homepage read uses ``follow_redirects=True``, so a domain-less mapping
    handed to ``httpx.get(cookies=...)`` rides along to every hop in the chain.
    Asserted on the wire rather than on the argument, so a regression that builds
    the jar correctly and then flattens it before use is still caught.
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(_storage_state()))

    bundle_url = (
        f"https://www.gstatic.com/_/mss/{capture_rpc_registry._APP}/_/js/k=boq.en.abc123.js"
    )
    # Same chain the health check walks — only the final body differs. The
    # ``?authuser=0`` suffix is what ``authuser_query(0)`` produces, so this is
    # the real request shape, not an approximation of it.
    _register_sign_in_chain(
        httpx_mock,
        f"{get_base_url()}/?authuser=0",
        final_html=f'<script src="{bundle_url}"></script>',
    )
    httpx_mock.add_response(
        url=bundle_url,
        status_code=200,
        text="/* bundle chunk */",
        headers={"content-type": "text/javascript"},
    )

    assert capture_rpc_registry.fetch_bundle() == "/* bundle chunk */"

    _assert_sign_in_hop_correctly_scoped(httpx_mock)
    _assert_app_hop_correctly_scoped(httpx_mock)

    # The gstatic chunk read stays unauthenticated — it is a public CDN.
    cdn_requests = [r for r in httpx_mock.get_requests() if r.url.host == "www.gstatic.com"]
    assert cdn_requests
    assert all("cookie" not in r.headers for r in cdn_requests)


def _auth_tokens_with_collision() -> AuthTokens:
    """AuthTokens built from the module's own collision fixture.

    Reuses ``_storage_state()`` so the probe assertions cannot drift from the
    auth-path assertions above -- both see the same ``OSID`` on two hosts with
    two values, which is what makes an absence assertion meaningful.
    """
    cookies = {
        (entry["name"], entry["domain"], entry.get("path", "/")): entry["value"]
        for entry in _storage_state()["cookies"]
    }
    return AuthTokens(cookies=cookies, csrf_token="csrf", session_id="session")


# ---------------------------------------------------------------------------
# The probe POSTs themselves (#2054)
#
# Everything above pins the *auth* path. The three probe request paths -- two
# batchexecute calls and the streamed-chat call, which posts to a hardcoded v1
# path rather than a method id -- were a separate leak: each hand-built
# ``{"Cookie": auth.cookie_header}``, the flat projection, so the
# accounts-scoped ``LSID`` was sent to the app host on every request and one of
# several same-name cookies survived arbitrarily.
#
# These assert on the header of the POST itself, driven through the *shipped*
# ``build_probe_client``. That indirection is the point: httpx lets an explicit
# ``Cookie`` header silently override the client jar, with no merge and no
# error, so a test that supplied its own client would still pass if the fix
# were reverted at the construction site.
# ---------------------------------------------------------------------------


def test_probe_client_is_the_seam_that_carries_the_jar() -> None:
    """``build_probe_client`` must attach the domain-scoped jar itself."""
    auth = _auth_tokens_with_collision()
    client = check_rpc_health.build_probe_client(auth)
    request = httpx.Request("POST", f"https://{_notebook_host()}/probe")
    client.cookies.set_cookie_header(request)
    sent = _cookie_pairs(request.headers.get("cookie", ""))

    assert ("OSID", "v-osid-app") in sent, "the app host must receive its own OSID"
    assert ("LSID", "v-lsid") not in sent, "the accounts-scoped LSID must not leak"


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["rpc", "raw_rpc"])
async def test_probe_post_carries_domain_scoped_cookies(httpx_mock: HTTPXMock, probe: str) -> None:
    """The batchexecute POST itself must not carry another host's cookies.

    Asserted on the recorded request rather than on the client, because that is
    the only view that would notice a re-added manual ``Cookie`` header.
    """
    auth = _auth_tokens_with_collision()
    httpx_mock.add_response(method="POST", text=")]}'\n[]")

    async with check_rpc_health.build_probe_client(auth) as client:
        if probe == "rpc":
            await check_rpc_health.make_rpc_request(
                client, auth, check_rpc_health.RPCMethod.LIST_NOTEBOOKS, []
            )
        else:
            # A second entry point that also POSTs batchexecute. Parametrised
            # because each probe used to hand-build its own header, so fixing
            # one and leaving another would be invisible: httpx prefers an
            # explicit Cookie header over the jar, silently.
            await check_rpc_health.make_raw_rpc_request(
                client, auth, check_rpc_health.RPCMethod.LIST_NOTEBOOKS.value, []
            )

    posts = [r for r in httpx_mock.get_requests() if r.method == "POST"]
    assert posts, "the probe must have issued a POST"
    for request in posts:
        sent = _cookie_pairs(request.headers.get("cookie", ""))
        assert ("OSID", "v-osid-app") in sent
        assert ("OSID", "v-osid-accounts") not in sent
        assert ("LSID", "v-lsid") not in sent


def test_probe_client_construction_has_exactly_one_home() -> None:
    """``build_probe_client`` must be the *only* place a probe client is built.

    The seam makes the jar assertable, but a test that calls
    ``build_probe_client`` directly proves only that the helper is correct --
    it cannot notice ``main()`` quietly going back to a bare
    ``httpx.AsyncClient(timeout=30.0)``. That mutation is silent and total: the
    manual ``Cookie`` headers are gone, so the probes would send **no** cookies
    at all and every other test here would still pass.

    So this pins the structure instead: exactly one ``AsyncClient`` construction
    in the module, and it lives inside the helper that attaches the jar.
    """
    source = Path(check_rpc_health.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "AsyncClient"
    ]
    assert len(constructions) == 1, (
        "expected exactly one httpx.AsyncClient construction; a second one would "
        "bypass the jar that build_probe_client attaches"
    )

    builder = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_probe_client"
    )
    builder_lines = range(builder.lineno, (builder.end_lineno or builder.lineno) + 1)
    assert constructions[0].lineno in builder_lines, (
        "the AsyncClient construction moved out of build_probe_client -- the probes "
        "would no longer carry the domain-scoped jar"
    )

    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_probe_client"
        and node.lineno not in builder_lines
        for node in ast.walk(tree)
    ), "build_probe_client is defined but never called"
