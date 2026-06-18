"""Shared test fixtures."""

import importlib.util
import json
import os
import re

import pytest

from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod

_PLAYWRIGHT_INSTALLED = importlib.util.find_spec("playwright") is not None


# Mirror of ``tests/vcr_config._is_vcr_record_mode`` — duplicated (not imported)
# so the *root* conftest, loaded for every test, stays free of the heavier
# ``vcr_config`` (vcrpy) import. Kept byte-for-byte identical to the canonical
# (same ``.casefold()``, no ``.strip()``) so the two never disagree on a padded
# value and split the config into a half-recording state; ``test_home_isolation``
# pins the parity. (#1263)
_VCR_RECORD_ENV = "NOTEBOOKLM_VCR_RECORD"


def _vcr_recording() -> bool:
    """Whether VCR is in record mode (``NOTEBOOKLM_VCR_RECORD`` truthy)."""
    return os.environ.get(_VCR_RECORD_ENV, "").casefold() in ("1", "true", "yes")


def _should_use_real_home(*, e2e: bool, vcr: bool, recording: bool) -> bool:
    """Whether a test should see the developer's real ``~/.notebooklm`` profile
    rather than an isolated tmp ``NOTEBOOKLM_HOME``.

    - **E2E** tests always use it (they mint live tokens).
    - **VCR** tests use it only while *recording* (``NOTEBOOKLM_VCR_RECORD=1``):
      recording captures against the live API and needs real auth, which both
      ``get_vcr_auth()`` (via ``AuthTokens.from_storage()``) and the CLI auth
      path read out of ``NOTEBOOKLM_HOME``. Replay runs and non-VCR tests stay
      isolated, so the suite is reproducible and a stray ``NOTEBOOKLM_VCR_RECORD``
      on a normal run never lets a test touch the real profile (issue #1263).
    """
    return e2e or (vcr and recording)


def _isolation_home(request, tmp_path):
    """The ``NOTEBOOKLM_HOME`` the autouse fixture should pin, or ``None`` to
    leave the developer's real ``~/.notebooklm`` profile in place.

    Split out from the fixture so the marker/env wiring is directly unit-testable
    (see ``tests/unit/test_home_isolation.py``) without unwrapping the fixture.

    Keys on the ``vcr`` *marker* only (not the ``@notebooklm_vcr.use_cassette``
    decorator / ``vcr`` fixture that the integration tier also recognizes): a
    cassette test must carry ``@pytest.mark.vcr`` to record against the real
    profile — most do via a module-level ``pytestmark``. Tests that re-pin
    ``NOTEBOOKLM_HOME`` themselves (e.g. the settings/profile/doctor cli_vcr
    tests, which isolate config writes on purpose) override this deferral and are
    not auto-recordable through pytest. Both gaps fail safe (isolated home, not a
    leaked real one).
    """
    if _should_use_real_home(
        e2e=request.node.get_closest_marker("e2e") is not None,
        vcr=request.node.get_closest_marker("vcr") is not None,
        recording=_vcr_recording(),
    ):
        return None
    return str(tmp_path / "notebooklm-home")


@pytest.fixture(autouse=True)
def _isolate_notebooklm_home(request, tmp_path, monkeypatch):
    """Pin ``NOTEBOOKLM_HOME`` at a per-test tmp dir.

    Without this, tests that route through the real CLI auth path read the
    developer's actual ``~/.notebooklm/`` state. An empty or partial
    ``storage_state.json`` there fails ``_validate_required_cookies`` inside
    ``build_cookie_jar`` and produces hundreds of ``exit_code=2`` failures
    locally while CI (with a clean ``HOME``) passes. Pinning
    ``NOTEBOOKLM_HOME`` at a tmp dir gives every test the same empty-storage
    view CI sees, so the suite is reproducible across machines.

    Two opt-outs use the real ``~/.notebooklm/`` profile instead (see
    :func:`_should_use_real_home` / :func:`_isolation_home`): ``@pytest.mark.e2e``
    tests (mint live tokens) and ``@pytest.mark.vcr`` tests while recording
    (``NOTEBOOKLM_VCR_RECORD=1``) — the latter lets a cassette be recorded
    through pytest rather than a standalone script (issue #1263).
    """
    home = _isolation_home(request, tmp_path)
    if home is not None:
        monkeypatch.setenv("NOTEBOOKLM_HOME", home)


@pytest.fixture(autouse=True)
def _reset_poke_state():
    """Reset module-level rotation guards between tests.

    The ``notebooklm.auth`` rotation throttle keeps two pieces of module-global
    state that persist across tests and would otherwise leak:

    1. ``_LAST_POKE_ATTEMPT_MONOTONIC`` (``dict[Path | None, float]``) — keyed
       per-profile. Without clearing, the first test to poke any profile sets
       the timestamp and subsequent tests in that file see "we just poked"
       and silently skip the POST they're asserting on.
    2. ``_POKE_LOCKS_BY_LOOP`` (``WeakKeyDictionary[loop, dict[..., Lock]]``) —
       in production each per-loop entry is reclaimed automatically when its
       loop is GC'd. In tests the loop typically outlives the explicit
       cleanup point (pytest-asyncio's loop teardown happens after fixtures
       run), so we clear it eagerly to keep tests independent.
    3. ``_SECONDARY_BINDING_WARNED`` — one-shot flag for the Tier 2 cookie
       warning. Reset so tests can independently observe the warning fire.
    """
    from notebooklm import auth as _auth
    from notebooklm._auth import cookie_policy as _cookie_policy
    from notebooklm._auth import storage as _auth_storage

    # ``_LAST_POKE_ATTEMPT_MONOTONIC`` and ``_POKE_LOCKS_BY_LOOP`` are shared
    # by identity across ``notebooklm.auth`` and ``notebooklm._auth.keepalive``
    # (the auth-module re-export captures the same dict object). ``.clear()``
    # mutates in place so reaching through either reference is equivalent.
    #
    # ``_SECONDARY_BINDING_WARNED`` lives on the cookie_policy seam since D1
    # PR-2 retired the ``_AuthFacadeModule`` write-through. Reset on the
    # owner directly; the auth-module re-export captured at import time was
    # never the canonical store.
    # ``_FLOCK_UNAVAILABLE_WARNED`` is reset for the same reason — the
    # storage seam owns the flag.
    _auth._LAST_POKE_ATTEMPT_MONOTONIC.clear()
    _auth._POKE_LOCKS_BY_LOOP.clear()
    _cookie_policy._SECONDARY_BINDING_WARNED = False
    _auth_storage._FLOCK_UNAVAILABLE_WARNED = False
    yield
    _auth._LAST_POKE_ATTEMPT_MONOTONIC.clear()
    _auth._POKE_LOCKS_BY_LOOP.clear()
    _cookie_policy._SECONDARY_BINDING_WARNED = False
    _auth_storage._FLOCK_UNAVAILABLE_WARNED = False


@pytest.fixture(autouse=True)
def _synthetic_error_mode(request, monkeypatch):
    """opt a test into ``NOTEBOOKLM_VCR_RECORD_ERRORS=<mode>``.

    When a test (or its enclosing module/class) carries
    ``@pytest.mark.synthetic_error("429"|"5xx"|"expired_csrf")``, this fixture
    sets the env var for the test's lifetime via ``monkeypatch`` (so it's
    auto-reverted on teardown). Without the marker, the env var is left
    untouched — preserving the spec's "opt-in" contract.

    Set before the client constructs its runtime and enters the middleware chain
    (markers are read at setup time): ``_error_injection._get_error_injection_mode``
    is consulted by the construction guard and by ``ErrorInjectionMiddleware``, so
    the var must be in place before the fixture under test enters its
    ``async with`` block.

    Production behavior is unchanged when the marker is absent.
    """
    marker = request.node.get_closest_marker("synthetic_error")
    if marker is None:
        return
    if not marker.args:
        raise pytest.UsageError(
            "@pytest.mark.synthetic_error requires one positional arg: "
            "the mode (429, 5xx, or expired_csrf)."
        )
    mode = marker.args[0]
    valid = {"429", "5xx", "expired_csrf"}
    if mode not in valid:
        raise pytest.UsageError(
            f"@pytest.mark.synthetic_error: invalid mode {mode!r}; valid modes are {sorted(valid)}."
        )
    # Import the env-var name from the production module so a future rename
    # in ``_error_injection.py`` cascades automatically; the constant is also exposed
    # from ``tests/vcr_config.py`` but importing from the canonical seam
    # is the production-faithful path.
    from notebooklm._error_injection import ERROR_INJECT_ENV_VAR

    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, mode)


@pytest.fixture(autouse=True)
def _mock_keepalive_poke(request):
    """Default-mock the auth keepalive poke so tests don't trip on it.

    ``_fetch_tokens_with_jar`` makes a best-effort POST to
    ``accounts.google.com/RotateCookies`` to rotate SIDTS. Tests that use
    ``httpx_mock`` would otherwise fail with "no response set" when this
    request fires. The mock is optional+reusable so tests that don't trigger
    the poke aren't penalised.

    Tests that need full control over the poke response (e.g. to assert on
    rotated Set-Cookie or simulate failure) should mark themselves with
    ``@pytest.mark.no_default_keepalive_mock`` to skip this default and
    register their own response.
    """
    if "httpx_mock" not in request.fixturenames:
        return
    if request.node.get_closest_marker("no_default_keepalive_mock"):
        return
    httpx_mock = request.getfixturevalue("httpx_mock")
    httpx_mock.add_response(
        url=re.compile(r"^https://accounts\.google\.com/RotateCookies$"),
        is_optional=True,
        is_reusable=True,
        status_code=200,
    )


def pytest_addoption(parser):
    """Register the dev-only ``--update-baselines`` regen flag (ADR-0022).

    When set, the regenerable-baseline freeze test
    (``test_baseline_matches_committed_file``) REWRITES each committed baseline
    file from ``derive()`` instead of asserting. ``scripts/regen_baselines.py``
    is the discoverable wrapper that shells ``pytest ... --update-baselines``.

    **Dev-only-regen invariant (ADR-0022):** CI must NEVER pass this flag — it
    only ever diffs. The ``update_baselines`` fixture additionally refuses to
    regenerate when a CI environment is detected, so wiring the flag into a CI
    command can't silently rewrite baselines; it fails loudly instead.
    """
    parser.addoption(
        "--update-baselines",
        action="store_true",
        default=False,
        help=(
            "DEV ONLY: rewrite committed baseline fixtures from live code instead "
            "of asserting against them. CI must never pass this (it only diffs). "
            "Prefer `python scripts/regen_baselines.py`."
        ),
    )


@pytest.fixture
def update_baselines(request) -> bool:
    """Whether the dev-only baseline regen was requested (``--update-baselines``).

    Enforces the dev-only-regen invariant: if the flag is set while a CI
    environment is detected (``CI`` env var truthy, as GitHub Actions and most
    CI providers set), this fails the test rather than silently rewriting the
    committed baselines. Locally (no ``CI``), the flag enables regen.
    """
    requested = bool(request.config.getoption("--update-baselines"))
    if requested and os.environ.get("CI", "").strip():
        raise pytest.UsageError(
            "--update-baselines must not be used in CI: baselines are dev-only "
            "regenerated and CI only diffs (ADR-0022). Unset CI or drop the flag."
        )
    return requested


def pytest_configure(config):
    """Register custom markers and configure test environment."""
    config.addinivalue_line(
        "markers",
        "vcr: marks tests that use VCR cassettes (may be skipped if cassettes unavailable)",
    )
    config.addinivalue_line(
        "markers",
        "no_default_keepalive_mock: skip the default accounts.google.com/RotateCookies "
        "mock so the test can register its own response",
    )
    config.addinivalue_line(
        "markers",
        "synthetic_error(mode): opts a test into "
        "NOTEBOOKLM_VCR_RECORD_ERRORS=<mode> for the duration of the test. "
        "Used by error-cassette recording to produce cassettes with "
        "synthetic error shapes. Mode must be one of: 429, 5xx, expired_csrf.",
    )
    config.addinivalue_line(
        "markers",
        "requires_playwright: skip the test unless the ``playwright`` Python "
        "package is importable. Install with ``uv sync --extra browser``. "
        "Apply to tests that import from ``playwright.sync_api`` at runtime; "
        "leave OFF tests that intentionally exercise the playwright-missing "
        "code path via ``patch.dict('sys.modules', {'playwright': None})``. "
        "CI always installs the browser extra so marked tests run there.",
    )
    # Disable Rich/Click formatting in tests to avoid ANSI escape codes in output
    # This ensures consistent test assertions regardless of -s flag
    # NO_COLOR disables colors, TERM=dumb disables all formatting (bold, etc.)
    # Force these values to ensure consistent behavior across all environments
    os.environ["NO_COLOR"] = "1"
    os.environ["TERM"] = "dumb"


def pytest_collection_modifyitems(config, items):
    """Auto-skip ``@pytest.mark.requires_playwright`` items when playwright is missing.

    Resolves the marker at collection time so local runs without the ``browser``
    extra (``uv sync`` without ``--extra browser``) skip cleanly instead of
    raising ``ImportError`` at runtime. CI installs the extra, so this is a
    no-op there.
    """
    if _PLAYWRIGHT_INSTALLED:
        return
    skip_marker = pytest.mark.skip(
        reason="playwright not installed; install with: uv sync --extra browser"
    )
    for item in items:
        if "requires_playwright" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture
def sample_storage_state():
    """Sample Playwright storage state with valid cookies.

    Carries the full Tier 1 set (``SID`` + ``__Secure-1PSIDTS``) plus
    ``APISID`` + ``SAPISID`` as the secondary binding so it satisfies the
    library's pre-flight validation. See ``MINIMUM_REQUIRED_COOKIES`` and
    ``_has_valid_secondary_binding`` in ``src/notebooklm/auth.py``.
    """
    return {
        "cookies": [
            {"name": "SID", "value": "test_sid", "domain": ".google.com"},
            {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
            {"name": "SSID", "value": "test_ssid", "domain": ".google.com"},
            {"name": "APISID", "value": "test_apisid", "domain": ".google.com"},
            {"name": "SAPISID", "value": "test_sapisid", "domain": ".google.com"},
            {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
        ]
    }


@pytest.fixture
def sample_homepage_html():
    """Sample NotebookLM homepage HTML with tokens."""
    return """
    <!DOCTYPE html>
    <html>
    <head><title>NotebookLM</title></head>
    <body>
    <script>window.WIZ_global_data = {
        "SNlM0e": "test_csrf_token_123",
        "FdrFJe": "test_session_id_456"
    }</script>
    </body>
    </html>
    """


@pytest.fixture
def mock_list_notebooks_response():
    inner_data = json.dumps(
        [
            [
                [
                    "My First Notebook",
                    [["src_001"], ["src_002"]],
                    "nb_001",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ],
                [
                    "Research Notes",
                    None,
                    "nb_002",
                    "📚",
                    None,
                    [None, None, None, None, None, [1704153600, 0]],
                ],
            ]
        ]
    )
    rpc_id = RPCMethod.LIST_NOTEBOOKS.value
    chunk = json.dumps([["wrb.fr", rpc_id, inner_data, None, None]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


@pytest.fixture
def build_rpc_response():
    """Factory for building RPC responses.

    Args:
        rpc_id: Either an RPCMethod enum or string RPC ID.
        data: The response data to encode.
    """

    def _build(rpc_id: RPCMethod | str, data) -> str:
        # Convert RPCMethod to string value if needed
        rpc_id_str = rpc_id.value if isinstance(rpc_id, RPCMethod) else rpc_id
        inner = json.dumps(data)
        chunk = json.dumps(["wrb.fr", rpc_id_str, inner, None, None])
        return f")]}}'\n{len(chunk)}\n{chunk}\n"

    return _build


@pytest.fixture
def mock_get_conversation_id(httpx_mock, build_rpc_response):
    """Register a batchexecute response for ``ChatAPI.get_conversation_id``.

    After issue #659, ``ChatAPI.ask`` calls ``get_conversation_id``
    (wire-level ``hPTbtc``) post-ask for new conversations to recover the
    real conversation_id — the server does NOT return it in the streaming
    chat response. Any test that exercises the new-conversation path
    through ``client.chat.ask(...)`` without a ``conversation_id``
    argument must register a response, or the SDK will time out retrying
    the unmocked call.

    Usage::

        async def test_thing(httpx_mock, mock_get_conversation_id, ...):
            mock_get_conversation_id()                  # default fake id
            mock_get_conversation_id(conv_id="my-id")    # specific id
            mock_get_conversation_id(reusable=True)      # for gathered asks
            # ... then mock chat-ask response and call client.chat.ask ...
    """

    def _add(conv_id: str = "real-conv-from-hptbtc", *, reusable: bool = False) -> str:
        response = build_rpc_response(
            RPCMethod.GET_LAST_CONVERSATION_ID,
            [[[conv_id]]],
        )
        # Narrow the URL pattern to ``rpcids=hPTbtc`` so the mock only
        # intercepts the get_conversation_id call and not unrelated
        # batchexecute RPCs that may fire in the same test (per CodeRabbit
        # review on PR #667 — defensive against future tests that exercise
        # additional batchexecute traffic).
        httpx_mock.add_response(
            url=re.compile(r".*batchexecute.*rpcids=hPTbtc.*"),
            content=response.encode(),
            method="POST",
            is_reusable=reusable,
        )
        return conv_id

    return _add


@pytest.fixture
def auth_tokens():
    """Canonical mock ``AuthTokens`` for unit tests.

    Carries a minimal single-cookie jar plus deterministic CSRF and session
    identifiers. Unit tests typically don't assert on these values directly —
    they just need a valid ``AuthTokens`` instance to construct a client.

    Notes:
        - ``tests/integration/conftest.py`` defines its own ``auth_tokens``
          with the full Tier 1 cookie set (SID/HSID/SSID/APISID/SAPISID)
          since integration tests exercise auth pre-flight validation.
        - ``tests/e2e/conftest.py`` defines a session-scoped fixture that
          loads real tokens from storage.
        - Tests that need a ``MagicMock`` rather than a real ``AuthTokens``
          instance (e.g. ``tests/unit/test_rate_limit_retry.py``) keep their
          own inline fixture.
    """
    return AuthTokens(
        cookies={"SID": "test"},
        csrf_token="test_csrf",
        session_id="test_session",
    )
