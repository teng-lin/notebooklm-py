# Concurrency Integration Test Harness

Reusable fixtures **and regression tests** for the thread-safety / concurrency
hardening work. This package contains the shared deterministic harness plus
per-bug regression modules that exercise core/upload/download/auth behavior with
`httpx.MockTransport` and scheduler-controllable timing.

## Fixture API

### `mock_transport_concurrent` → `ConcurrentMockTransport`

A class-based `httpx.AsyncBaseTransport` that records peak concurrent
in-flight requests and supports controllable per-request response
timing.

```python
async def test_my_fix(mock_transport_concurrent):
    transport = mock_transport_concurrent

    # Configure
    transport.set_delay(0.05)                 # 50ms artificial delay
    transport.queue_response((200, "..."))    # FIFO queue of responses
    # (or pass an httpx.Response, or a callable (req) -> Response)

    # ... drive the SUT ...

    # Observe
    assert transport.get_peak_inflight() >= 80
    assert transport.request_count() == 100
    assert transport.get_inflight_count() == 0
    requests = transport.captured_requests()  # list[httpx.Request]
```

**Method surface**:

| Method | Purpose |
|---|---|
| `queue_response(item)` | Append to FIFO queue. `item` may be `httpx.Response`, `(status, text)` tuple, or `Callable[[Request], Response]`. |
| `set_delay(seconds)` | Artificial per-request `asyncio.sleep` delay. Default `0.05`s. `0` disables delay (defeats fan-out). |
| `get_inflight_count()` | Current concurrent requests. |
| `get_peak_inflight()` | High-water mark since construction (or last `reset()`). |
| `request_count()` | Total requests served. |
| `captured_requests()` | Snapshot of every `httpx.Request` observed. |
| `reset()` | Clear counters and queue. |

**Default response**: empty `LIST_NOTEBOOKS` payload (decodes to `[]`).
Tests that need a different shape should `queue_response(...)` enough
items for their fan-out width.

### `barrier_factory` → `Callable[[int], EventBarrier]`

Returns a factory that builds N-arrival one-shot barriers (built on
`asyncio.Event`, NOT `asyncio.Barrier`, so behavior is identical on
Python 3.10).

```python
async def test_two_coroutines_meet(barrier_factory):
    barrier = barrier_factory(2)

    async def worker():
        # ... do setup ...
        await barrier.arrive()    # blocks until 2nd arrival
        # ... critical section both coroutines hit simultaneously ...

    await asyncio.gather(worker(), worker())
    assert barrier.is_set
```

Re-arming is intentionally NOT supported. Build a fresh barrier per
synchronization point — this matches the existing
`tests/unit/test_concurrency_refresh_race.py` pattern of one
`asyncio.Event` per checkpoint.

### `cancellation_helper` → `(coro, *, timeout, label) -> result`

Wraps a coroutine in `asyncio.wait_for` and emits a structured error
log on timeout/cancellation.

```python
async def test_no_deadlock(cancellation_helper):
    result = await cancellation_helper(
        suspect_coroutine(),
        timeout=2.0,
        label="suspect-coro",
    )
```

The diagnostic surfaces *which* coroutine deadlocked when several are
in flight — cheaper to debug than `pytest --timeout` killing the whole
process.

## Non-goals (read before adding to this directory)

- **NOT a load tester.** `get_peak_inflight()` assertions are coarse
  (`>= 80` of `100`) because asyncio scheduling is not perfectly
  parallel. Use a real load tool (k6, locust) for performance work.
- **NOT a property-based generator.** Hypothesis is intentionally NOT
  added as a dependency. Per-bug tests use specific seeded scenarios.
- **NOT a thread-pool stress harness.** Pure asyncio. If a future bug
  involves real threads, add a separate `tests/integration/threaded/`
  package.
- **NOT a place for unrelated integration tests.** Per-bug concurrency tests do
  live here when they need these fixtures; non-concurrency mock tests should use
  the normal unit or integration taxonomy.

## pytest-xdist + asyncio caveat

Each `pytest-xdist` worker has its own event loop. Fixture state is
**never** shared across workers, which means:

- `ConcurrentMockTransport` peak-inflight is per-worker; tests that
  need to observe global concurrency must run single-threaded.
- The `_reset_poke_state` autouse fixture in the parent
  `tests/conftest.py` resets module-level rotation guards per-loop, so
  each xdist worker starts clean — no cross-worker leakage.
- Tests asserting on process-global state (e.g. an `_LRU_CACHE`) should
  mark themselves with `@pytest.mark.xdist_group("name")` to keep all
  cases on the same worker.

Default `uv run pytest` runs single-process (no `-n`), so the caveat
only applies to contributors who pass `-n auto` locally or in CI.

## How to add a new fixture

1. Add the fixture function to `conftest.py` next to the existing three.
2. Document the API surface in this README under "Fixture API".
3. If the fixture needs new dependencies, **STOP** — harness ground
   rules forbid adding new deps in any harness PR. Open an RFC issue
   first.
4. Add a sanity check to `test_harness_smoke.py` so the fixture is
   exercised by `uv run pytest tests/integration/concurrency`.

## How to add a per-bug concurrency test

Per-bug concurrency tests may live in this package as
`tests/integration/concurrency/test_<bug-slug>.py`. They should:

- Use the fixtures by parameter name — pytest auto-discovers them via
  this package's `conftest.py` for tests underneath
  `tests/integration/concurrency/`. (pytest fixtures are not imported,
  they are injected by name.) Shared helper functions/classes should be imported
  from `.helpers` or `tests.integration.concurrency.helpers`; fixture classes may
  be imported from `conftest.py` for annotations.
- Be one PR per bug.
- Include a docstring linking the audit item (e.g.
  the relevant issue, PR, or regression identifier.

## Running the harness

```bash
uv run pytest tests/integration/concurrency -v
```

For a quick harness smoke check, run:

```bash
uv run pytest tests/integration/concurrency/test_harness_smoke.py -q
```

The full subtree includes real regression coverage; do not rely on a fixed wall
time in docs or tests.

## Why `concurrency/` and not `concurrent/`?

The natural directory name `concurrent/` collides with Python's stdlib
`concurrent` package. Pytest puts each test root on `sys.path` for
collection, so a `tests/integration/concurrent/` directory would shadow
`concurrent.futures` for any test that imports it (and ours and the
project's deps do). Renaming to `concurrency/` sidesteps the collision
without changing the semantic intent.

## Related work

- Thread-safety and concurrency hardening. Each fix PR references the
  specific bug or invariant it addresses directly in its description
  and commit message.
