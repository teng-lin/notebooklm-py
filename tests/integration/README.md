# `tests/integration/` — VCR-tier rule

This directory holds the **integration tier** of the test pyramid. Anything
collected here exercises real (or recorded-real) HTTP traffic against the
NotebookLM `batchexecute` endpoints via [VCR.py](https://github.com/kevin1024/vcrpy)
cassettes in `tests/cassettes/`.

To keep the tier honest — i.e. to keep "integration" from quietly slipping
back into "unit with extra ceremony" — every test collected under
`tests/integration/` MUST satisfy one of these three rules. The
`pytest_collection_modifyitems` hook in `conftest.py` raises
`pytest.UsageError` at collection time if none of them holds, so a violation
fails CI immediately rather than degrading the tier silently.

## The rule

A `tests/integration/` test is accepted if **any** of the following is true:

1. **`@pytest.mark.vcr`** is applied (per-test decorator or module-level
   `pytestmark = [pytest.mark.vcr, ...]`).
2. **`@notebooklm_vcr.use_cassette("…")`** decorates the test function. The
   hook detects the VCR-wrapped function by walking the function's
   `wrapt.FunctionWrapper` chain and matching `CassetteContextDecorator` on
   the bound `_self_wrapper`.
3. **`@pytest.mark.allow_no_vcr`** is applied as an explicit opt-out.

If none of the three is present, collection fails with a message naming the
violating node IDs.

## When to use `allow_no_vcr`

`allow_no_vcr` exists for tests that legitimately live under
`tests/integration/` for tree-organization reasons but make no real (or
recorded) HTTP calls. Today these include:

- `test_skill_packaging.py` — runs `uv build` on the wheel and inspects
  packaged files; no HTTP.
- `test_artifacts_drift.py`, `test_get_summary_drift.py` — exercise
  `_parse_*` parsers against constructed dicts to pin RPC-shape drift; the
  parser doesn't go through HTTP.
- `test_auto_refresh.py` — asserts that the refresh callback is *wired*;
  doesn't fire a real refresh.
- `test_core.py` — `httpx.MockTransport` + `AsyncMock` exercising error
  paths; no real socket.
- `test_download_multi_artifact.py` — pure helper-logic tests for
  `select_artifact` / `artifact_title_to_filename`.
- The whole `concurrency/` subtree — uses `httpx.MockTransport` to inject
  scheduler-controllable behavior into the core/upload/download paths
  (real HTTP would defeat the determinism these tests need).

Per the project's testing strategy, **new mock-only tests should land in
`tests/unit/`** (or `tests/unit/concurrency/`). `allow_no_vcr` is a
transitional marker for the legacy mock-tier files above — adding more of
them under `tests/integration/` should be a conscious decision (e.g. the
test needs `tests/integration/cassettes/` discovery to round-trip a path)
not a default.

## When to use `@pytest.mark.vcr` vs `@notebooklm_vcr.use_cassette`

- Module-level `pytestmark = [pytest.mark.vcr, skip_no_cassettes]` is the
  baseline for files where every test is VCR-tier. It also wires
  `skip_no_cassettes` so the run is skipped (not failed) when no real
  cassettes are present on disk.
- `@notebooklm_vcr.use_cassette("cassette_name.yaml")` pins a specific
  cassette to a specific test. Always pair with `@pytest.mark.vcr` (a)
  for self-documentation and (b) so the
  `_disable_keepalive_poke_for_vcr` autouse fixture activates — that
  fixture reads the marker, not the wrapper.

## Reference

- Hook implementation: `tests/integration/conftest.py`
  (`pytest_collection_modifyitems` + `_has_use_cassette_decorator`)
- Marker registration: `pyproject.toml` `[tool.pytest.ini_options].markers`
- Regression test (committed, pytester-based):
  `tests/unit/test_tier_enforcement_hook.py`
