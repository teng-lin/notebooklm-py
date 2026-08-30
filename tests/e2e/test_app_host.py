"""Live assertion that real traffic reaches the configured app host.

Three layers already pin the host offline, and none of them can fail the way
that matters:

* ``tests/_guardrails/test_public_surface_manifest.py`` pins
  ``DEFAULT_BASE_URL`` to the rebrand host and asserts ``get_base_url()``
  agrees — but that is the *constant*, not the socket.
* ``tests/_guardrails/test_app_host_literals_centralized.py`` forbids a second
  source of truth in ``src/notebooklm/`` — but a path that reads the right
  constant can still be handed to the wrong client.
* VCR replay is structurally incapable of covering this: ``tests/vcr_config.py``
  matches on ``host``, so a cassette replays whatever host it was recorded
  against, and the corpus deliberately retains pre-rebrand entries (#2067).

So the whole offline suite can be green while every live request goes somewhere
else. That gap is what this module closes: it watches the requests a *real*
call actually issues, built through the public constructor the e2e ``client``
fixture uses, so it exercises the path users take rather than a test shell.

Asserted against ``get_base_url()`` rather than a literal, so a run using the
documented ``NOTEBOOKLM_BASE_URL`` rollback lever (ADR-0028) still passes — the
question here is "does traffic follow the configuration", and the guardrail
above is what pins what the configuration defaults to. When nothing is
configured, this additionally asserts the default resolves to the rebrand host,
so the CI run makes the unconditional statement too.
"""

from __future__ import annotations

import os

import httpx
import pytest

from notebooklm._env import PERSONAL_BASE_HOST, get_base_url
from notebooklm._runtime.init import _resolve_async_client_factory

from .conftest import requires_auth

#: Google's cookie-rotation endpoint. Not app traffic, and legitimately a
#: different host — ``RotateCookies`` is accounts-scoped by construction.
_ACCOUNTS_HOST = "accounts.google.com"

#: Path prefix of the data plane: batchexecute and streamed chat both live under
#: it. This is the traffic "are we hitting the right host?" is really about, and
#: the traffic for which a redirect is unambiguously wrong.
_DATA_PLANE_PREFIX = "/_/"


class _RequestLog:
    """Records ``(host, path, status, location)`` for every request issued."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, int, str | None]] = []

    def app_entries(self) -> list[tuple[str, str, int, str | None]]:
        """Entries excluding the accounts-scoped cookie rotation."""
        return [e for e in self.entries if e[0] != _ACCOUNTS_HOST]

    def data_plane_entries(self) -> list[tuple[str, str, int, str | None]]:
        """App entries for batchexecute / streamed chat only.

        Forward-looking, not currently load-bearing: the e2e ``client`` fixture
        is built from pre-loaded ``AuthTokens``, so ``notebooks.list()`` issues
        exactly one request today — the batchexecute POST — and this filter
        removes nothing (measured 2026-08-04).

        It exists because the session-bootstrapping paths *do* issue a landing
        ``GET /``, and pointed at the legacy host via the documented rollback
        lever that request legitimately 302s to the rebrand host while the data
        plane keeps serving on the configured host. Should the fixture ever grow
        such a request, asserting over it would report a working rollback as a
        failure. Scoping now costs nothing and removes that trap.
        """
        return [e for e in self.app_entries() if e[1].startswith(_DATA_PLANE_PREFIX)]

    def hosts(self) -> set[str]:
        return {e[0] for e in self.entries}


@pytest.fixture
def request_log(monkeypatch: pytest.MonkeyPatch) -> _RequestLog:
    """Record every HTTP request the client under test issues, hops included.

    Wraps ``httpx.AsyncClient.send`` rather than injecting a client factory:
    ``async_client_factory`` is deliberately kept off the public constructor
    (see ``client.py``), and routing this test around that constructor would
    stop it testing the thing it exists to test. Wrapping the transport instead
    observes whatever the shipped construction path actually built.

    Skips when the resolved transport is not ``httpx.AsyncClient``. Under
    ``NOTEBOOKLM_TRANSPORT=curl_cffi`` the client is a ``CurlCffiAsyncClient``,
    which exposes ``get``/``post``/``stream`` and no ``send`` at all — this
    wrapper would silently record nothing and the assertions would fail with an
    empty log despite perfectly good live traffic. The resolver is the library's
    own, so this check cannot drift from what the client actually builds.
    """
    if _resolve_async_client_factory(None) is not httpx.AsyncClient:
        pytest.skip("resolved transport is not httpx.AsyncClient (NOTEBOOKLM_TRANSPORT is set)")

    log = _RequestLog()
    original = httpx.AsyncClient.send

    async def recording_send(self: httpx.AsyncClient, request: httpx.Request, **kwargs: object):
        response = await original(self, request, **kwargs)
        # ``Kernel.open`` builds its client with ``follow_redirects=True``
        # (``_web/transport/kernel.py``), so ``send`` returns the TERMINAL response and the
        # 3xx hops survive only in ``response.history`` — while ``request`` is
        # the FIRST request, not the last. Recording that pair alone would log a
        # bounce off the configured host as ``(configured host, 200)``, leaving
        # this test blind to precisely the state it exists to catch. Walking
        # ``history`` and reading each hop's own ``request`` fixes both halves.
        for hop in (*response.history, response):
            log.entries.append(
                (
                    hop.request.url.host,
                    hop.request.url.path,
                    hop.status_code,
                    hop.headers.get("location"),
                )
            )
        return response

    monkeypatch.setattr(httpx.AsyncClient, "send", recording_send)
    return log


@pytest.fixture
def web_client(client):
    """Keep this HTTP-host assertion scoped to the web data plane."""
    if client.backends["notebooks"] != "web":
        pytest.skip("app-host routing applies only to the web backend")
    return client


@requires_auth
@pytest.mark.readonly
class TestAppHost:
    """Both tests issue a single ``notebooks.list()`` and mutate nothing.

    Marked ``readonly`` so the release-branch lane (``-m "readonly and not
    variants"`` in ``nightly.yml``) carries the host assertion too — a release
    build aimed at the wrong host is exactly the case worth catching.
    """

    @pytest.mark.asyncio
    async def test_rpc_traffic_reaches_the_configured_host(self, web_client, request_log):
        """A real RPC lands on the configured app host, with no redirect hop.

        The redirect assertion carries as much weight as the host one. Being
        *bounced* to the right host and *addressing* it are different states:
        the first means the client is aimed at the wrong host and the service is
        covering for it, which is precisely the condition that would rot
        unnoticed until the bounce stops.
        """
        await web_client.notebooks.list()

        expected = httpx.URL(get_base_url()).host
        data_plane = request_log.data_plane_entries()
        assert data_plane, (
            "no data-plane requests were observed — the recording fixture did not "
            f"engage; entries seen: {request_log.entries}"
        )

        wrong_host = [e for e in data_plane if e[0] != expected]
        assert not wrong_host, f"requests left the configured host {expected}: {wrong_host}"

        redirected = [e for e in data_plane if 300 <= e[2] < 400]
        assert not redirected, (
            f"the app host answered with a redirect, so the client is aimed at the "
            f"wrong host and is being bounced: {redirected}"
        )

        assert any("batchexecute" in e[1] for e in data_plane), (
            f"no batchexecute request was observed; paths seen: {[e[1] for e in data_plane]}"
        )

    @pytest.mark.asyncio
    async def test_unconfigured_runs_reach_the_rebrand_host(self, web_client, request_log):
        """With no override set, every app request reaches the rebrand host.

        This is the CI case, and it makes the unconditional claim over *all* app
        traffic rather than the data plane alone — so if the fixture ever grows
        a session-bootstrapping request, this test covers it too. That is safe
        on the default host, which serves the shell directly; only the legacy
        host bounces.

        Skips rather than fails when ``NOTEBOOKLM_BASE_URL`` is set: pointing a
        run at the legacy host is a supported configuration (ADR-0028), and a
        test that failed for it would punish the rollback lever for working.
        """
        if os.environ.get("NOTEBOOKLM_BASE_URL", "").strip():
            pytest.skip("NOTEBOOKLM_BASE_URL is set — this asserts the *unconfigured* default")

        await web_client.notebooks.list()

        assert httpx.URL(get_base_url()).host == PERSONAL_BASE_HOST
        assert request_log.hosts() - {_ACCOUNTS_HOST} == {PERSONAL_BASE_HOST}

        bounced = [e for e in request_log.app_entries() if 300 <= e[2] < 400]
        assert not bounced, f"app traffic was redirected on the default host: {bounced}"
