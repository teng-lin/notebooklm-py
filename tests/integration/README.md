# `tests/integration/` — recorded-seam rule

This directory holds the **integration tier** of the test pyramid. Anything
collected here exercises real or recorded-real NotebookLM traffic. Web
`batchexecute` calls use [VCR.py](https://github.com/kevin1024/vcrpy) HTTP
cassettes. Android calls use the test-only protobuf-aware gRPC channel seam in
`tests/_helpers/android_grpc_cassette.py`, because `grpc.aio` performs its I/O
in gRPC C-core and is not intercepted by vcrpy.

To keep the tier honest — i.e. to keep "integration" from quietly slipping
back into "unit with extra ceremony" — every test collected under
`tests/integration/` MUST satisfy one of these four rules. The
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
3. **`@pytest.mark.grpc_cassette`** is applied to a test replaying an Android
   `.grpc.json` cassette through the custom channel adapter.
4. **`@pytest.mark.allow_no_vcr`** is applied as an explicit opt-out.

If none of the four is present, collection fails with a message naming the
violating node IDs.

## Android gRPC cassettes

Android cassettes live in `tests/cassettes/android/` and intentionally use the
`.grpc.json` suffix rather than vcrpy's YAML format. Each interaction pins the
full method path, unary-unary versus unary-stream shape, deterministic request
protobuf FQN/bytes, and deterministic response protobuf FQN/bytes. Metadata is
not part of the model, so bearer credentials cannot be serialized. Recording
requires an explicit application-level sanitizer and then unconditionally runs
the generalized protobuf redactor as its final security boundary. The redactor
discards unknown fields, replaces every string and byte string, and maps
integers/floats to safe non-zero placeholders while preserving scalar presence,
message structure, booleans, and schema-defined enum values. Replay injects an
in-memory channel and a non-secret bearer provider; it must never construct a
live gRPC channel or mint OAuth credentials.

Set `NOTEBOOKLM_ANDROID_GRPC_RECORD=1` only for an explicitly reviewed,
read-only recording test. In that mode, `@pytest.mark.grpc_cassette` keeps the
real profile home available. Replay remains isolated from the developer's
profile. Hand-built fixtures must be named `*_synthetic.grpc.json`; do not call
them recorded traffic.

The initial representative recorded case is a sanitized read of a temporary,
empty scratch notebook through public `notebooks.get` over `GetProject`; the
scratch notebook was deleted immediately after capture. The staged decoder
matrix should add recordings, one bounded family at a time, for:

1. settings via unary `GetOrCreateAccount`;
2. a rich `GetProject` project/source response;
3. `LoadSource` structured content;
4. `ListArtifacts` plus `GetNotes`;
5. both `GetLabels` label/collection response arms;
6. `ListDiscoverSourcesJob` research state;
7. sharing `GetProjectDetails`;
8. chat `ListChatSessions` plus `ListChatTurns`; and
9. server-streaming `GenerateFreeFormStreamed`.

## When to use `allow_no_vcr`

`allow_no_vcr` exists for tests that legitimately live under
`tests/integration/` for tree-organization reasons but make no real (or
recorded) HTTP calls. The authoritative allowlists live in:

- `tests/_fixtures/integration_allow_no_vcr_files.txt`
- `tests/_fixtures/integration_allow_no_vcr_nodeids.txt`
- `tests/_fixtures/integration_vcr_allow_no_vcr_nodeids.txt` for the rare
  intentional VCR/allow-no-VCR overlap

Current categories include:

- `test_auto_refresh.py` — asserts that the refresh callback is *wired*;
  doesn't fire a real refresh.
- `test_session_integration.py` — `httpx.MockTransport` + `AsyncMock` exercising error
  paths; no real socket.
- `test_*_idempotency.py` — mock-transport regression tests for retry /
  idempotency behavior; no live or recorded HTTP.
- The whole `concurrency/` subtree — uses `httpx.MockTransport` to inject
  scheduler-controllable behavior into the core/upload/download paths
  (real HTTP would defeat the determinism these tests need).

Per the project's testing strategy, **new mock-only tests should land in
`tests/unit/`** (or `tests/unit/concurrency/`). `allow_no_vcr` is a
transitional marker for the legacy mock-tier files above. Adding more of
them under `tests/integration/` should be a conscious decision, with the
allowlist manifests updated in the same PR. Real cassettes live in
`tests/cassettes/`, not under `tests/integration/`.

`test_gzip_cassette_replay.py` is VCR-tier, not `allow_no_vcr`: it uses a scoped
VCR instance over a derived cassette in `tests/cassettes/gzip_coverage/`.

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
- Taxonomy guard: `tests/_guardrails/test_integration_allow_no_vcr_allowlist.py`
- Replay network guard: `tests/integration/conftest.py` refuses live sockets when
  cassette replay should be deterministic.
