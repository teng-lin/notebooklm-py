"""Shared fixtures for integration tests."""

import importlib.util
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm.auth import AuthTokens

# Load ``tests/vcr_config.py`` by file path. ``from tests.vcr_config import ...``
# now resolves in pytest via ``pythonpath = ["."]`` (pyproject, #1482); loading
# by file path is kept as a ``sys.path``-independent fallback (the same idiom
# ``vcr_config.py`` uses for its sibling ``cassette_patterns.py`` import).
_vcr_config_spec = importlib.util.spec_from_file_location(
    "tests_vcr_config", Path(__file__).resolve().parent.parent / "vcr_config.py"
)
assert _vcr_config_spec is not None and _vcr_config_spec.loader is not None, (
    "Could not load tests/vcr_config.py from tests/integration/conftest.py"
)
_vcr_config = importlib.util.module_from_spec(_vcr_config_spec)
_vcr_config_spec.loader.exec_module(_vcr_config)
_is_vcr_record_mode = _vcr_config._is_vcr_record_mode

# =============================================================================
# VCR Cassette Availability Check
# =============================================================================

CASSETTES_DIR = Path(__file__).parent.parent / "cassettes"

# Real cassettes live at the top level of ``tests/cassettes/``; illustrative
# fixtures (``example_*.yaml``) live in ``tests/cassettes/examples/`` per the
# naming convention documented in ``tests/cassettes/README.md``.
#
# This filter decides whether the VCR integration tier has anything to replay:
# - Globbing ``*.yaml`` (non-recursive) naturally skips the ``examples/``
#   subdirectory, so example fixtures cannot inflate the "real cassettes
#   present" signal.
# - The ``startswith("example_")`` guard is retained as a belt-and-braces
#   filter — if a future contributor lands an ``example_*.yaml`` file at the
#   top level by mistake, it still won't count as a real recording.
_real_cassettes = (
    [f for f in CASSETTES_DIR.glob("*.yaml") if not f.name.startswith("example_")]
    if CASSETTES_DIR.exists()
    else []
)

# Skip VCR tests if no real cassettes exist (unless in record mode)
_vcr_record_mode = _is_vcr_record_mode()
_cassettes_available = bool(_real_cassettes) or _vcr_record_mode

# Marker for skipping VCR tests when cassettes are not available
skip_no_cassettes = pytest.mark.skipif(
    not _cassettes_available,
    reason="VCR cassettes not available. Set NOTEBOOKLM_VCR_RECORD=1 to record.",
)


def install_post_as_stream(
    monkeypatch: pytest.MonkeyPatch | None,
    http_client: Any,
    fake_post: Callable[..., Awaitable[Any]],
) -> None:
    """Adapt legacy fake ``post`` callbacks to the streaming RPC POST API."""

    @asynccontextmanager
    async def fake_stream(method: str, url: str, **kwargs: Any) -> Any:
        response = await fake_post(url, **kwargs)
        if type(response) is httpx.Response:
            yield response
            return

        text = getattr(response, "text", "")
        payload = text.encode("utf-8") if isinstance(text, str) else bytes(text or b"")
        raw_status = getattr(response, "status_code", 200)
        status = raw_status if isinstance(raw_status, int) else 200
        try:
            raw_headers = getattr(response, "headers", None)
        except AttributeError:
            raw_headers = None
        try:
            headers = dict(raw_headers) if raw_headers else None
        except (TypeError, AttributeError):
            headers = None
        yield httpx.Response(
            status_code=status,
            headers=headers,
            content=payload,
            request=httpx.Request("POST", url),
        )

    if monkeypatch is not None:
        monkeypatch.setattr(http_client, "stream", fake_stream)
    else:
        http_client.stream = fake_stream


async def get_vcr_auth() -> AuthTokens:
    """Get auth tokens for VCR tests.

    In record mode: loads real auth from storage (required for recording).
    In replay mode: returns mock auth (cassettes have recorded responses).
    """
    if _vcr_record_mode:
        return await AuthTokens.from_storage()
    else:
        # Mock auth for replay - values don't matter, VCR replays recorded responses
        return AuthTokens(
            cookies={
                "SID": "mock_sid",
                "HSID": "mock_hsid",
                "SSID": "mock_ssid",
                "APISID": "mock_apisid",
                "SAPISID": "mock_sapisid",
            },
            csrf_token="mock_csrf_token",
            session_id="mock_session_id",
        )


def _has_use_cassette_decorator(item) -> bool:
    """Detect ``@notebooklm_vcr.use_cassette(...)`` on a test callable.

    ``VCR.use_cassette`` returns a ``CassetteContextDecorator``; applying it
    as a decorator wraps the test in a ``wrapt.FunctionWrapper`` whose
    ``_self_wrapper`` is a bound method on the ``CassetteContextDecorator``.
    We detect that wrapper class by name (``CassetteContextDecorator``) so
    the check stays robust if vcrpy ever moves the class — and so the check
    does not require importing ``vcr`` in this module.

    Walks ``__wrapped__`` to handle stacked decorators (e.g.
    ``@pytest.mark.parametrize`` on top of ``@notebooklm_vcr.use_cassette``).
    """
    func = getattr(item, "function", None)
    seen: set[int] = set()
    while func is not None and id(func) not in seen:
        seen.add(id(func))
        wrapper = getattr(func, "_self_wrapper", None)
        if wrapper is not None:
            owner = getattr(wrapper, "__self__", None)
            if owner is not None and type(owner).__name__ == "CassetteContextDecorator":
                return True
        func = getattr(func, "__wrapped__", None)
    return False


def pytest_collection_modifyitems(config, items):
    """Enforce the integration tier-VCR rule.

    Every collected test under ``tests/integration/`` MUST be VCR-tier: it must carry
    ``@pytest.mark.vcr``, be decorated with ``@notebooklm_vcr.use_cassette``,
    or explicitly opt out with ``@pytest.mark.allow_no_vcr`` (for mock-only
    or no-network tests that legitimately live under ``tests/integration/`` —
    e.g. ``test_auto_refresh.py``, ``test_sources_integration.py``,
    ``concurrency/test_*``). Violations
    raise ``pytest.UsageError`` so the test suite refuses to collect rather
    than silently letting a new mock test slip into the integration tier.
    """
    violations: list[str] = []
    for item in items:
        nodeid = item.nodeid
        if not nodeid.startswith("tests/integration/"):
            continue
        if item.get_closest_marker("vcr") is not None:
            continue
        if item.get_closest_marker("allow_no_vcr") is not None:
            continue
        if _has_use_cassette_decorator(item):
            continue
        violations.append(nodeid)
    if violations:
        joined = "\n  ".join(violations)
        raise pytest.UsageError(
            "tests/integration/ tests must be VCR-tier. Add "
            "@pytest.mark.vcr, @notebooklm_vcr.use_cassette, or — for "
            "mock-only tests — @pytest.mark.allow_no_vcr. Violations:\n  "
            f"{joined}"
        )


# =============================================================================
# Globalize keepalive-poke disable for VCR tests
# =============================================================================


@pytest.fixture(autouse=True)
def _disable_keepalive_poke_for_vcr(request, monkeypatch):
    """Auto-set ``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`` for VCR tests.

    The layer-1 ``RotateCookies`` keepalive poke (documented escape hatch in
    CHANGELOG ``[0.4.1]`` Fixed) fires from inside ``_fetch_tokens_with_jar``
    and is not part of any cassette recorded before that poke was added.
    Letting it fire during VCR replay produces a cassette mismatch on
    ``POST accounts.google.com/RotateCookies``, which under the typed CLI
    error handler surfaces as ``UNEXPECTED_ERROR`` (exit 2) —
    outside what most VCR tests accept. Disabling the poke aligns every
    replay with what the cassettes actually capture.

    A test is treated as VCR if ``request.node.get_closest_marker("vcr")``
    returns a marker. Every cassette-using test in this repo carries the
    ``vcr`` mark via either a module-level ``pytestmark = [pytest.mark.vcr,
    ...]`` or a per-test ``@pytest.mark.vcr`` decorator, so the marker check
    alone is sufficient — no stack inspection of
    ``@notebooklm_vcr.use_cassette`` is required. If a future test uses
    ``use_cassette`` without the marker, add ``@pytest.mark.vcr`` to it.

    Escape hatch: ``@pytest.mark.no_keepalive_disable`` opts a test out so
    it can capture or assert on real ``RotateCookies`` traffic (e.g. a
    future cassette that records the keepalive itself).

    Markers are read at SETUP TIME via ``get_closest_marker`` —
    ``request.applymarker()`` in the test body would be too late because the
    env var must be set before the client constructs its HTTP layer.
    """
    if request.node.get_closest_marker("no_keepalive_disable"):
        return
    if request.node.get_closest_marker("vcr") is None:
        return
    monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")


# =============================================================================
# Autouse network guard for VCR replay mode (P1-4)
# =============================================================================


def _has_active_vcr_cassette() -> bool:
    """Return ``True`` if a VCR cassette is currently installed on httpx stubs.

    VCR installs its httpx / httpcore stubs as a global side effect when a
    cassette context enters. Detecting that installation lets the network
    guard distinguish "test legitimately replaying a cassette" from "test
    accidentally leaking an unbound HTTP request out to the wire". We look
    for vcrpy's signature class-attribute mutations rather than poking at
    private state so this stays robust against vcrpy version bumps.

    The check is best-effort: if vcrpy's stubs are not detectable for any
    reason, the guard falls open (returns ``True``) so it doesn't break
    legitimate replay flows. The stricter ``record_mode='none'`` setting
    on ``notebooklm_vcr`` is the primary protection — this guard is a
    secondary belt-and-braces check for unbound requests.
    """
    # vcrpy installs ``functools.wraps``-style stubs (``__wrapped__`` points
    # back to the original) on its HTTP interception points. The exact point
    # moved across versions: vcrpy <=8.1 patched
    # ``httpcore.AsyncConnectionPool.handle_async_request``; vcrpy >=8.2 patches
    # ``httpx.AsyncHTTPTransport.handle_async_request`` (the transport layer)
    # instead. Probe both so the guard survives either vcrpy generation.
    candidates = []
    try:
        import httpx  # type: ignore[import-not-found]

        candidates.append((httpx.AsyncHTTPTransport, "handle_async_request"))
    except ImportError:
        pass
    try:
        import httpcore  # type: ignore[import-not-found]

        candidates.append((httpcore.AsyncConnectionPool, "handle_async_request"))
    except ImportError:
        pass

    if not candidates:
        # Neither library importable — fall open rather than break tests on
        # environmental quirks.
        return True
    try:
        return any(
            getattr(getattr(cls, attr, None), "__wrapped__", None) is not None
            for cls, attr in candidates
        )
    except AttributeError:
        return True


@pytest.fixture(autouse=True)
def _block_unbound_network_in_replay(request, monkeypatch):
    """Refuse unbound httpx network requests when in VCR replay mode (P1-4).

    Companion to the ``pytest_collection_modifyitems`` enforcement hook
    above: that hook gates **collection** by requiring every
    ``tests/integration/`` test to carry ``@pytest.mark.vcr``,
    ``@notebooklm_vcr.use_cassette``, or ``@pytest.mark.allow_no_vcr``.
    This fixture gates **runtime** — even a properly-marked test could try
    to make an HTTP request OUTSIDE its cassette context (e.g. setup or
    teardown that bypassed the cassette stub). When that happens during
    replay mode, the request would silently hit the real network. We
    monkeypatch ``httpx.AsyncClient.send`` to raise instead.

    The fixture is autouse + function-scoped so it runs alongside the
    existing ``_disable_keepalive_poke_for_vcr`` autouse. It only takes
    effect when:

    1. VCR record mode is OFF (replay mode), and
    2. The test is NOT marked ``allow_no_vcr``, and
    3. The test IS marked ``vcr`` OR carries a
       ``@notebooklm_vcr.use_cassette`` decorator OR uses the ``vcr``
       pytest fixture (any of the three known cassette-binding paths).

    When all three conditions hold, we wrap ``httpx.AsyncClient.send`` so
    that any request reaching it without an active vcrpy cassette context
    raises ``RuntimeError`` with a clear message instead of leaking
    traffic to the real backend.
    """
    if _vcr_record_mode:
        return  # Recording: real network calls are intentional.

    if request.node.get_closest_marker("allow_no_vcr") is not None:
        return  # Mock-only test legitimately doesn't use VCR.

    is_vcr_marked = request.node.get_closest_marker("vcr") is not None
    has_decorator = _has_use_cassette_decorator(request.node)
    # ``vcr`` pytest fixture (pytest-vcr) binds a cassette via fixture
    # resolution rather than a marker; detect by name in ``fixturenames``.
    uses_vcr_fixture = "vcr" in getattr(request.node, "fixturenames", ())

    if not (is_vcr_marked or has_decorator or uses_vcr_fixture):
        # Not a VCR-tier test (and not allow_no_vcr — that's already
        # filtered above). The collection hook should have rejected this
        # at collect time, so reaching here is a defensive no-op.
        return

    # Patch BOTH ``send`` and ``stream`` — the production RPC transport in
    # ``src/notebooklm/_streaming_post.py`` uses ``client.stream(...)`` for the
    # streaming-chat endpoint, which httpx routes through a separate codepath
    # from ``send``. Patching only ``send`` would let an unbound streaming
    # request slip past the guard. The vcrpy stubs intercept at the lower
    # ``httpcore`` layer so both routes are covered there; this fixture just
    # ensures the belt-and-braces wrapper covers the same two surfaces httpx
    # exposes to callers.
    original_send = httpx.AsyncClient.send
    original_stream = httpx.AsyncClient.stream

    def _refuse(verb: str, target: str) -> None:
        raise RuntimeError(
            f"VCR replay mode: refusing unbound httpx {verb} ({target}). "
            "The test is marked VCR-tier but no cassette context is "
            "currently active. Either wrap the request in "
            "@notebooklm_vcr.use_cassette / @pytest.mark.vcr cassette "
            "binding, or mark the test @pytest.mark.allow_no_vcr if it "
            "legitimately should not use a cassette."
        )

    async def _guarded_send(self: httpx.AsyncClient, request: httpx.Request, **kwargs):
        if not _has_active_vcr_cassette():
            _refuse("request", f"{request.method} {request.url}")
        return await original_send(self, request, **kwargs)

    def _guarded_stream(self: httpx.AsyncClient, method: str, url: Any, **kwargs):
        if not _has_active_vcr_cassette():
            _refuse("stream", f"{method} {url}")
        return original_stream(self, method, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", _guarded_send)
    monkeypatch.setattr(httpx.AsyncClient, "stream", _guarded_stream)


@pytest.fixture
def auth_tokens():
    """Create test authentication tokens for integration tests.

    Overrides the root-level fixture (single-cookie) with the full Tier 1
    cookie set so integration tests that exercise auth pre-flight validation
    have a realistic jar to work with.
    """
    return AuthTokens(
        cookies={
            "SID": "test_sid",
            "HSID": "test_hsid",
            "SSID": "test_ssid",
            "APISID": "test_apisid",
            "SAPISID": "test_sapisid",
        },
        csrf_token="test_csrf_token",
        session_id="test_session_id",
    )


# ``build_rpc_response`` and ``mock_list_notebooks_response`` are provided by
# the root ``tests/conftest.py`` and inherited here.
