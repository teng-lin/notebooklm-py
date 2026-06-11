# cli_vcr — CLI integration tests over VCR cassettes

These tests run the **real** `CLI → Client → RPC` path, but VCR replays the HTTP
traffic from `tests/cassettes/*.yaml` instead of hitting the live NotebookLM
service. They are the integration tier between pure unit tests (no network) and
e2e tests (real API, real auth).

## How cassette matching actually works

The default VCR matcher (`tests/vcr_config.py`) matches on:

1. HTTP method, scheme, host, port, and path,
2. the RPC **method id** (`rpcids=` in the `batchexecute` URL), and
3. the decoded request **body shape** (`freq` matcher).

It does **not** match on the notebook id, source id, or any other id in the
request. The cassette corpus changes over time; do not pin docs or tests to a
literal cassette count or notebook count. `mock_context` injects a single
notebook id (`PLACEHOLDER_NOTEBOOK_ID`, `c3f6285f…`) for most tests, and replay
still succeeds because ids are scrubbed/decorative for matcher purposes.

### Consequence: the ids are decorative

The placeholder ids in [`_fixtures.py`](_fixtures.py) are **arbitrary**. The only
thing that matters about an id on the command line is its *shape* — a full
36-char UUID where the CLI's resolver expects one, so it short-circuits the
`LIST_NOTEBOOKS` / `LIST_SOURCES` preflight (which the single-purpose cassettes
do not record). The specific value is never compared against the recorded
request.

There is therefore **no canonical-fixture registry and no cassette-membership
guard** — they would police a relationship that does not exist.

The one place a placeholder is load-bearing is the **input-echo** assertion: a
mutation command threads the id the test passed into its own `--json` output, so
`output["notebook_id"] == MUTATION_NOTEBOOK_ID` holds for *any* cassette.
`MUTATION_NOTEBOOK_ID` is present in **zero** cassettes on purpose, which proves
the echoed value comes from the input.

## Re-record-safe assertion tiers

Every assertion must survive a re-record that uses a **different notebook with
different data**. The allowed vocabulary:

1. **Schema** — the `--json` envelope field names + types
   (`assert_json_envelope(result, schema=...)`, schemas defined in
   `conftest.py`).
2. **Invariants** — each id is UUID-shaped, each title is non-empty,
   `count > 0`.
3. **Cross-render** — text vs `--json` of the *same* cassette agree on row count
   / id set.
4. **Filter correctness** — e.g. `artifact list --type audio` ⇒ every item is
   audio (proves decoder logic, not a recorded value). `test_sources.py` and
   `test_artifacts.py` also use `_cassette_expectations.py` to derive expected
   projections from the same cassette payload.
5. **Input-echo** — a mutation's output id `==` the placeholder the test passed.

❌ **Never** pin a value that came from the recorded *response*: a recorded
title, a server-returned id, or a literal count. Exact count equality is fine
only when the expected value is derived from the same cassette projection or
the same JSON array length.

`test_sources.py` is the worked template for tiers 1–3 + 5;
`test_artifacts.py::TestArtifactListByType` covers tier 4.

## Re-recording

Cassettes replay at `record_mode="none"` by default. Setting
`NOTEBOOKLM_VCR_RECORD=1` switches VCR to `record_mode="new_episodes"`: existing
matching episodes replay, and only missing episodes are appended. To truly
re-record, delete or move the target cassette first, then run (maintainer, with
a valid local profile):

```bash
NOTEBOOKLM_VCR_RECORD=1 uv run pytest tests/integration/cli_vcr/<file>.py -m vcr
```

Record against **whatever notebook is handy** — the assertions are
notebook-agnostic, so a fresh cassette recorded against a different notebook
drops in without any fixture surgery. Some command families (`settings`,
`profile`, `doctor`) pin `NOTEBOOKLM_HOME` or local profile state in their tests;
they are not always auto-recordable through the normal live-profile path.

You only need to touch the tests when the response **shape** changes (a new
field, a renamed field, a changed type). In that case update the affected schema
constant in `conftest.py` — the shape change is a real signal worth catching.
You should **not** need to update the placeholder ids in `_fixtures.py` or the
per-test assertions just because the underlying notebook changed.

## Layout

| File | Purpose |
|------|---------|
| `_fixtures.py` | Flat set of decorative placeholder ids + back-compat aliases. `_`-prefixed + non-`test_*` so the cross-test-import gate (#1445) permits tests to import it. |
| `conftest.py` | Shared fixtures, `assert_json_envelope`, and the per-family `*_SCHEMA` constants. |
| `test_*.py` | One module per CLI command group. |
