"""Shared test fixtures."""

import importlib.util
import json
import os
import re
from urllib.parse import parse_qs

import pytest

from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod

_PLAYWRIGHT_INSTALLED = importlib.util.find_spec("playwright") is not None

# Reality probes are intentionally an explicit, small set. Inferring the
# expected set from markers would let a deleted or deselected probe disappear
# while the required lane still passed.
REQUIRED_REALITY_PROBES = frozenset(
    {
        "tests/unit/cli/test_playwright_login_coverage.py::"
        "test_probe_source_detects_both_states_against_real_playwright",
        "tests/unit/cli/test_playwright_login_coverage.py::"
        "test_chromium_launches_headless_against_real_playwright",
    }
)
_REALITY_DEPENDENCY_MARKERS = frozenset({"requires_playwright", "requires_chromium"})
_REALITY_REPORTS: dict[str, list[tuple[str, str]]] = {}


# Mirror of ``tests/vcr_config._is_vcr_record_mode`` — duplicated (not imported)
# so the *root* conftest, loaded for every test, stays free of the heavier
# ``vcr_config`` (vcrpy) import. Kept byte-for-byte identical to the canonical
# (same ``.casefold()``, no ``.strip()``) so the two never disagree on a padded
# value and split the config into a half-recording state; ``test_home_isolation``
# pins the parity. (#1263)
_VCR_RECORD_ENV = "NOTEBOOKLM_VCR_RECORD"
_ANDROID_GRPC_RECORD_ENV = "NOTEBOOKLM_ANDROID_GRPC_RECORD"


def _vcr_recording() -> bool:
    """Whether VCR is in record mode (``NOTEBOOKLM_VCR_RECORD`` truthy)."""
    return os.environ.get(_VCR_RECORD_ENV, "").casefold() in ("1", "true", "yes")


def _android_grpc_recording() -> bool:
    """Whether the test-only Android gRPC seam is recording live traffic."""

    return os.environ.get(_ANDROID_GRPC_RECORD_ENV, "").casefold() in ("1", "true", "yes")


def _should_use_real_home(
    *,
    e2e: bool,
    vcr: bool,
    recording: bool,
    grpc_cassette: bool = False,
    grpc_recording: bool = False,
) -> bool:
    """Whether a test should see the developer's real ``~/.notebooklm`` profile
    rather than an isolated tmp ``NOTEBOOKLM_HOME``.

    - **E2E** tests always use it (they mint live tokens).
    - **VCR** tests use it only while *recording* (``NOTEBOOKLM_VCR_RECORD=1``):
      recording captures against the live API and needs real auth, which both
      ``get_vcr_auth()`` (via ``AuthTokens.from_storage()``) and the CLI auth
      path read out of ``NOTEBOOKLM_HOME``. Replay runs and non-VCR tests stay
      isolated, so the suite is reproducible and a stray ``NOTEBOOKLM_VCR_RECORD``
      on a normal run never lets a test touch the real profile (issue #1263).
    - **Android gRPC cassette** tests follow the same rule under the separate
      ``NOTEBOOKLM_ANDROID_GRPC_RECORD=1`` opt-in.
    """
    return e2e or (vcr and recording) or (grpc_cassette and grpc_recording)


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
        grpc_cassette=request.node.get_closest_marker("grpc_cassette") is not None,
        grpc_recording=_android_grpc_recording(),
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

    Three opt-outs use the real ``~/.notebooklm/`` profile instead (see
    :func:`_should_use_real_home` / :func:`_isolation_home`): ``@pytest.mark.e2e``
    tests (mint live tokens) and ``@pytest.mark.vcr`` tests while recording
    (``NOTEBOOKLM_VCR_RECORD=1``), plus ``@pytest.mark.grpc_cassette`` tests
    while ``NOTEBOOKLM_ANDROID_GRPC_RECORD=1``. The recording opt-ins let each
    cassette kind resolve real auth through pytest; replay remains isolated.
    """
    home = _isolation_home(request, tmp_path)
    if home is not None:
        monkeypatch.setenv("NOTEBOOKLM_HOME", home)


@pytest.fixture(autouse=True)
def _isolate_backend_preference(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep ambient backend preferences out of non-live constructor tests.

    E2E runs deliberately use ``NOTEBOOKLM_BACKEND`` to select the backend
    under test, so preserve the caller's explicit choice for those tests.
    """
    if request.node.get_closest_marker("e2e") is None:
        monkeypatch.delenv("NOTEBOOKLM_BACKEND", raising=False)


@pytest.fixture(autouse=True)
def _reset_poke_state():
    """Reset process-owned auth lifecycle state between tests.

    The process-default rotation owner keeps two pieces of state that persist
    across tests and would otherwise leak:

    1. ``_LAST_POKE_ATTEMPT_MONOTONIC`` (``dict[Path | None, float]``) — keyed
       per-profile. Without clearing, the first test to poke any profile sets
       the timestamp and subsequent tests in that file see "we just poked"
       and silently skip the POST they're asserting on.
    2. ``_POKE_LOCKS_BY_LOOP`` (``WeakKeyDictionary[loop, WeakValueDictionary[..., Lock]]``) —
       in production each per-loop entry is reclaimed automatically when its
       loop is GC'd. In tests the loop typically outlives the explicit
       cleanup point (pytest-asyncio's loop teardown happens after fixtures
       run), so we clear it eagerly to keep tests independent.
    3. ``_SECONDARY_BINDING_WARNED`` — one-shot flag for the Tier 2 cookie
       warning. Reset so tests can independently observe the warning fire.
    4. ``LegacyPromotionScheduler.process_default()`` — the detached retryable
       legacy-account promotion (ADR-0033 PR 5.1). A read of
       a legacy-only profile schedules a background writer, so teardown must
       JOIN it before clearing: a worker still running when the next test
       starts would write into a ``tmp_path`` that test believes it owns, and
       a leftover active-path entry would suppress the very promotion another
       test is asserting on (``tmp_path`` uniqueness makes real path collisions
       unlikely, but the drain makes the durable half deterministic).
    """
    from notebooklm._auth import cookie_policy as _cookie_policy
    from notebooklm._auth import keepalive as _keepalive
    from notebooklm._auth.profile_migration import LegacyPromotionScheduler

    scheduler = LegacyPromotionScheduler.process_default()

    # Rotation reset checks that no per-loop poke lock is still held.  The
    # cookie-warning reset takes the same lock as the production claim path,
    # so teardown cannot race an in-flight warning decision.
    _keepalive._reset_poke_state_for_tests()
    _cookie_policy._reset_secondary_binding_warning_for_tests()
    scheduler._reset_for_tests()
    yield
    # Join first, then reset every process owner. Clearing any owner while a
    # detached promotion is still alive would let that worker enter the next
    # test's lifecycle after teardown had declared the process quiescent.
    scheduler.drain(30.0)
    _keepalive._reset_poke_state_for_tests()
    _cookie_policy._reset_secondary_binding_warning_for_tests()
    scheduler._reset_for_tests()


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
    # in ``_web/transport/error_injection.py`` cascades automatically; the constant is also exposed
    # from ``tests/vcr_config.py`` but importing from the canonical seam
    # is the production-faithful path.
    from notebooklm._web.transport.error_injection import ERROR_INJECT_ENV_VAR

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
    parser.addoption(
        "--allow-growth",
        action="store_true",
        default=False,
        help=(
            "DEV ONLY: explicitly acknowledge growth in shrink-only baselines. "
            "Valid only together with --update-baselines."
        ),
    )
    parser.addoption(
        "--require-reality",
        action="store_true",
        default=False,
        help=(
            "Require every expected external-reality probe to be collected and "
            "pass exactly once; intended for the explicit browser CI lane."
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


@pytest.fixture
def allow_baseline_growth(request) -> bool:
    """Whether shrink-only baseline growth was explicitly acknowledged."""
    return bool(request.config.getoption("--allow-growth"))


def pytest_configure(config):
    """Register custom markers and configure test environment."""
    allow_growth = bool(config.getoption("--allow-growth"))
    if allow_growth and not config.getoption("--update-baselines"):
        raise pytest.UsageError("--allow-growth requires --update-baselines")
    if allow_growth and os.environ.get("CI", "").strip():
        raise pytest.UsageError(
            "--allow-growth must not be used in CI: growth acknowledgement is a local, "
            "reviewed baseline-regeneration action (ADR-0022)."
        )

    xdist_active = (
        config.getoption("numprocesses", default=None) not in (None, 0)
        or config.getoption("dist", default="no") != "no"
    )
    if config.getoption("--require-reality") and xdist_active:
        raise pytest.UsageError(
            "--require-reality cannot be combined with xdist; run the required "
            "reality lane serially so the controller can account for every probe"
        )
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
        chromium_available = None
        for item in items:
            if "requires_chromium" not in item.keywords:
                continue
            if chromium_available is None:
                chromium_available = _chromium_available()
            if not chromium_available:
                item.add_marker(
                    pytest.mark.skip(
                        reason=(
                            "Chromium is not installed or launchable; run: "
                            "uv run playwright install chromium"
                        )
                    )
                )
        return
    skip_marker = pytest.mark.skip(
        reason="playwright not installed; install with: uv sync --extra browser"
    )
    for item in items:
        if _REALITY_DEPENDENCY_MARKERS.intersection(item.keywords):
            item.add_marker(skip_marker)


def _chromium_available() -> bool:
    """Return whether Playwright can launch the installed Chromium executable."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            if not os.path.isfile(playwright.chromium.executable_path):
                return False
            browser = playwright.chromium.launch(headless=True)
            try:
                return True
            finally:
                browser.close()
    except Exception:
        return False


def _is_xdist_worker(config) -> bool:
    """Required reality accounting belongs to the xdist controller only."""
    return getattr(config, "workerinput", None) is not None


def pytest_collection_finish(session) -> None:
    """Validate the exact reality-probe set after all selection filters apply."""
    if not session.config.getoption("--require-reality") or _is_xdist_worker(session.config):
        return

    selected = {item.nodeid: item for item in session.items}
    missing = sorted(REQUIRED_REALITY_PROBES - selected.keys())
    unexpected = sorted(
        item.nodeid
        for item in session.items
        if "reality" in item.keywords and item.nodeid not in REQUIRED_REALITY_PROBES
    )
    invalid_dependencies = sorted(
        item.nodeid
        for item in session.items
        if item.nodeid in REQUIRED_REALITY_PROBES
        and not _REALITY_DEPENDENCY_MARKERS.intersection(item.keywords)
    )
    unmarked_expected = sorted(
        nodeid
        for nodeid in REQUIRED_REALITY_PROBES
        if nodeid in selected and "reality" not in selected[nodeid].keywords
    )
    if missing or unexpected or invalid_dependencies or unmarked_expected:
        problems = []
        if missing:
            problems.append(f"missing expected probes: {missing}")
        if unexpected:
            problems.append(f"unexpected reality probes: {unexpected}")
        if invalid_dependencies:
            problems.append(f"probes lack a recognized dependency marker: {invalid_dependencies}")
        if unmarked_expected:
            problems.append(f"expected probes lack the reality marker: {unmarked_expected}")
        raise pytest.UsageError(
            "--require-reality collection contract failed: " + "; ".join(problems)
        )

    _REALITY_REPORTS.clear()
    for nodeid in REQUIRED_REALITY_PROBES:
        _REALITY_REPORTS[nodeid] = []


def pytest_runtest_logreport(report) -> None:
    """Record every phase so skipped/setup-error probes cannot count as passes."""
    if report.nodeid in _REALITY_REPORTS:
        _REALITY_REPORTS[report.nodeid].append((report.when, report.outcome))


def pytest_sessionfinish(session, exitstatus) -> None:
    """Turn a missing or non-passing reality call phase into a hard failure."""
    if not session.config.getoption("--require-reality") or _is_xdist_worker(session.config):
        return

    failures = []
    for nodeid in sorted(REQUIRED_REALITY_PROBES):
        reports = _REALITY_REPORTS.get(nodeid, [])
        calls = [outcome for phase, outcome in reports if phase == "call"]
        if calls != ["passed"] or any(outcome != "passed" for _phase, outcome in reports):
            failures.append(f"{nodeid}: phases={reports!r}")
    if failures:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line("--require-reality execution contract failed:")
            for failure in failures:
                terminal.write_line(f"  {failure}")


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
def rpc_request_params():
    """Decode the positional params out of an outgoing ``batchexecute`` request.

    The inverse of :func:`build_rpc_response` for assertions on request shape:
    unwraps the ``f.req`` form body and returns the params list the client sent.
    Shared here rather than duplicated per test module, since both tiers assert on
    wire shape and ``tests/_guardrails/test_no_cross_test_imports.py`` forbids one
    test module importing another.
    """

    def _params(request) -> list:
        outer = json.loads(parse_qs(request.content.decode())["f.req"][0])
        return json.loads(outer[0][0][1])

    return _params


@pytest.fixture
def mock_get_conversation_id(httpx_mock, build_rpc_response):
    """Register batchexecute responses for an existing conversation.

    After issue #659, ``ChatAPI.ask`` calls ``get_conversation_id``
    (wire-level ``hPTbtc``) post-ask for new conversations to recover the
    real conversation_id — the server does NOT return it in the streaming
    chat response. Any test that exercises the new-conversation path
    through ``client.chat.ask(...)`` without a ``conversation_id``
    argument must register a response, or the SDK will time out retrying
    the unmocked call. The optional ``khqZz`` response gives that id one
    existing turn when ``ask`` probes implicit follow-up state (#1973).

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
        turns_response = build_rpc_response(
            RPCMethod.GET_CONVERSATION_TURNS,
            [[[None, None, 1, "Existing question?"]]],
        )
        httpx_mock.add_response(
            url=re.compile(r".*batchexecute.*rpcids=khqZz.*"),
            content=turns_response.encode(),
            method="POST",
            is_optional=True,
            is_reusable=True,
        )
        return conv_id

    return _add


@pytest.fixture
def legacy_vcr_follow_up_probe(monkeypatch):
    """Supply the prior-turn count omitted from legacy chat cassettes.

    The old recordings contain the current-conversation lookup and chat POST,
    but not the pre-POST ``khqZz`` count fetch added across #1973 and #1976.
    Keep those recordings immutable; dedicated characterization tests exercise
    the real request and its empty, non-empty, multi-turn, and failure branches.
    """

    from notebooklm._chat import ChatAPI

    async def _count_prior_server_turns(
        self: ChatAPI,
        notebook_id: str,
        conversation_id: str,
    ) -> int:
        """Replay a legacy cassette whose current conversation had one prior turn."""
        return 1

    monkeypatch.setattr(ChatAPI, "_count_prior_server_turns", _count_prior_server_turns)


@pytest.fixture
def legacy_vcr_add_url_baseline(monkeypatch):
    """Answer the pre-create source baseline omitted from legacy add_url cassettes.

    ``sources.add_url`` snapshots the notebook's source ids before issuing the
    create so its idempotency probe can tell a source it created from one that
    was already there (#2204). Cassettes recorded before that change hold no
    such ``GET_NOTEBOOK``. Without this fixture the read still *fires*: VCR
    refuses it, the add swallows the failure, and the call proceeds with the
    probe disabled — green, and silently not testing the thing it names. In a
    cassette that also records later ``GET_NOTEBOOK``s the miss is worse, since
    the baseline consumes a poll's response and desynchronises the journey.

    Keep those recordings immutable and answer only the missing read. Every
    consumer of this fixture replays a cassette whose create **succeeds**, so
    the probe never runs and the returned value is never compared against
    anything — ``[]`` is the neutral answer, not a claim about the notebook.
    That invariant is enforced rather than trusted: a second call means the
    probe fired, and the fixture fails the test instead of letting an invented
    empty baseline license a match. Every other list still replays from the
    cassette. The probe itself is covered against explicit request sequences in
    ``tests/integration/test_sources_idempotency.py``, so nothing here is its
    only coverage. Mirrors :func:`legacy_vcr_follow_up_probe`.
    """
    from notebooklm._web.sources.add import SourceAddService

    original_add_url = SourceAddService.add_url

    async def _add_url(self, notebook_id, url, *, list_sources, **kwargs):
        calls = 0

        async def _list_sources(nb_id: str):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise AssertionError(
                    "legacy_vcr_add_url_baseline: the idempotency probe fired, so this "
                    "cassette's create did not succeed. The stubbed empty baseline would "
                    "decide the probe's answer — record the probe's GET_NOTEBOOK instead "
                    "of stubbing the baseline."
                )
            return []

        result = await original_add_url(
            self, notebook_id, url, list_sources=_list_sources, **kwargs
        )
        assert calls == 1, (
            "legacy_vcr_add_url_baseline: add_url no longer captures a pre-create "
            "baseline, so this fixture is stale — drop it."
        )
        return result

    monkeypatch.setattr(SourceAddService, "add_url", _add_url)


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
