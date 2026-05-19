# ADR-007: Test-monkeypatch policy — constructor-injection via `tests/_fixtures/`

## Status

Accepted.

This ADR ships in the `arch-d1-fixtures-scaffolding` PR (D1 PR-1). It defines the *forbidden* test patterns going forward; the migration of the ~273 existing offenders is scheduled across D1 PR-2 (`arch-d1-auth-side`) and D1 PR-3 (`arch-d1-cli-side`), at which point the meta-lint's file-level allowlist shrinks to empty.

## Context

The `.sisyphus/plans/arch-biggest-problem-audit.md` audit identifies "test-monkeypatch gravity" as the first of three architectural diseases — a *gravity well* in which the test suite's preferred mocking strategy welds module boundaries shut and forces every seam extraction to ship a write-through facade, a property bridge, or a parallel-implementation invariant.

The audit's verified counts at HEAD `22355cf` are:

| Pattern | Count |
|---|---|
| Total `monkeypatch.setattr` sites under `tests/` | 236 |
| String-target patches — `monkeypatch.setattr("notebooklm.X.Y", …)` | 58 |
| Object-attribute patches — `monkeypatch.setattr(obj, "attr", …)` | 152 |
| Direct attribute assignment — `core.rpc_call = AsyncMock(…)` etc. | 63 |
| `tests/unit/test_auth.py` size | 4,090 LOC · 70 patches |
| `tests/unit/cli/test_session.py` size | 4,431 LOC |

Each of these patterns shares one root cause: production code is mutated from the outside *after* construction, rather than receiving its collaborators *during* construction. Three artifacts visible in `src/notebooklm/` exist solely to keep that style working as the architecture shifts beneath it:

- `_AuthFacadeModule` (`auth.py:288-339`) — a `types.ModuleType` subclass whose `__setattr__` mirrors writes from `notebooklm.auth.<name>` across `_auth/storage`, `_auth/account`, `_auth/keepalive`, `_auth/refresh`, plus header/cookie/policy seams. The shim exists because ~152 patches target the `notebooklm.auth` namespace and would silently no-op if the facade were a passive re-export. See ADR-003.
- The `_core.py` property-bridge zoo (lines 450-774, ~324 LOC) — read/write properties that delegate to the seam where the storage actually lives. They exist because `monkeypatch.setattr(core, "_save_lock", fake)` is a load-bearing idiom across dozens of tests. See ADR-001 and ADR-002.
- The `cli/session.py` proxy block (lines 141-490, ~350 LOC) — module-level functions that mirror service-layer symbols so `monkeypatch.setattr("notebooklm.cli.session.X", fake)` reaches the real implementation in `cli/services/login.py`.

Every previous seam extraction (tier 7 through tier 10) has had to add or extend one of these structures rather than break the patches. The cost compounds: each new refactor that crosses one of these load-bearing boundaries either preserves the shim (cementing it) or breaks ~100 tests at once.

The mitigation is a policy change, not a code change. New tests stop using these patterns; existing ones migrate to constructor injection through factories. With no new offenders entering the codebase, the shims become finite, retire-able artifacts rather than permanent fixtures.

## Decision

Going forward, every test that needs to substitute a collaborator on `Session` or any sub-client (`NotebooksAPI`, `SourcesAPI`, `ArtifactsAPI`, `ChatAPI`, `ResearchAPI`, `NotesAPI`, `SettingsAPI`, `SharingAPI`) **must** acquire that collaborator through constructor injection, using the factory substrate in `tests/_fixtures/`:

```python
from _fixtures import make_fake_core  # pytest adds tests/ to sys.path

async def test_notebooks_list_returns_payload() -> None:
    fake = make_fake_core(rpc_call=AsyncMock(return_value=[fake_payload]))
    api = NotebooksAPI(core=fake)
    result = await api.list()
    fake.rpc_call.assert_awaited_once()
```

The following patterns are **forbidden** in new test code and enforced by `tests/_lint/test_no_forbidden_monkeypatches.py`:

1. **String-target monkeypatches into the `notebooklm` namespace** — `monkeypatch.setattr("notebooklm.X.Y", ...)`. These rely on import-string resolution and silently no-op when storage relocates.
2. **Object-attribute monkeypatches via the `notebooklm` module** — `monkeypatch.setattr(notebooklm.X, "attr", ...)`. Same failure mode; written slightly differently.
3. **Direct AsyncMock attribute assignment to the core's transport/RPC surface** — `core.rpc_call = AsyncMock(...)`, `core._perform_authed_post = AsyncMock(...)`, `core._begin_transport_post = AsyncMock(...)`, `core._finish_transport_post = AsyncMock(...)`, `core.query_post = AsyncMock(...)`, including chained variants like `self._client._core.rpc_call = AsyncMock(...)`. These mutate a constructed instance instead of injecting a fake at construction.

`tests/_fixtures/fake_core.py` provides `make_fake_core(**overrides) -> FakeSession`. `FakeSession` is a `types.SimpleNamespace`-shaped plain class whose default fields cover every attribute the narrow Protocols in `src/notebooklm/_capabilities.py` require (`rpc_call`, `_perform_authed_post`, `_begin_transport_post`, `_finish_transport_post`, `next_reqid`, `poll_registry`, `authuser`, `account_email`, `authuser_query`, `authuser_header`, `live_cookies`, `get_upload_semaphore`, `record_upload_queue_wait`, `bound_loop`, plus the private `_route_url`/`_next_reqid` aliases tests use today). Defaults are benign `AsyncMock`s for the async surface and `MagicMock`s for the sync surface; tests override only the slice they exercise via keyword arguments.

The choice of `types.SimpleNamespace`-shaped attribute storage (rather than `MagicMock(spec=...)` against the fat union or a Protocol-typed class) keeps the factory's per-attribute assignment explicit, prevents `MagicMock` from auto-vivifying attributes the production code does not actually use, and lets reviewers diff the factory against the narrow Protocols a sub-client structurally requires.

`tests/_fixtures/conftest.py` exposes exactly two thin pytest fixtures — `fake_core` (default factory call) and `make_fake_core` (the factory itself, for tests that need per-call overrides). The deliberately small fixture surface avoids the failure mode `/grill-me` Q11 flagged: a "full fixture menu" creates one parameterless pytest fixture per per-test override combination, which scales O(N tests × M overrides) and quickly becomes worse than the monkeypatch pattern it replaces.

The meta-lint (`tests/_lint/test_no_forbidden_monkeypatches.py`) runs in `pytest` (no skip markers), scans each test file as a single string (so multi-line `monkeypatch.setattr(\n  "notebooklm.X", …)` forms are caught — `\s` already matches newlines in Python's `re` engine), and reports each violation with `(file, line, matched pattern)` for actionable failure messages. Its file-level allowlist starts at 49 files (the union of audit-flagged offenders at PR-start) and is expected to shrink to zero by D1 PR-3.

## Consequences

**Wanted:**

- New tests cannot rebuild the gravity well. The meta-lint blocks the patterns at PR review.
- Sub-clients become testable in isolation. `NotebooksAPI(core=fake)` exercises the sub-client without standing up a real `Session`, without import-string resolution, and without after-construction mutation.
- The `_AuthFacadeModule` shim, the `_core.py` property bridges, and the `cli/session.py` proxy block become *removable* artifacts. They keep existing tests passing until those tests migrate; once the allowlist is empty, the shims can be deleted with no test churn (D2 cutover and D1 PR-2/PR-3 do exactly this).
- Test diffs become smaller and more readable. A test that overrides one collaborator now shows one keyword argument instead of one `monkeypatch.setattr` line per substituted symbol.

**Unwanted:**

- Tests that previously relied on string-target patches to substitute imports from modules they don't directly construct (e.g. patching `notebooklm._core.asyncio.sleep` to skip a real delay) need a different mechanism. The migration plan absorbs these on a case-by-case basis. Most either move to constructor injection of a clock-like collaborator or switch from the *string-target* form — `monkeypatch.setattr("notebooklm._auth.refresh.X", …)`, which the lint forbids because string-target patches resolve at import time and silently no-op on relocation — to the *object-attribute* form against a locally-imported seam alias, which the lint accepts because the alias variable provides a real Python object reference rather than a string the lint cannot validate. The seam-aliased form is the recommended migration target for legitimate `unittest.mock.patch`-style needs that don't fit the constructor-injection model.
- The factory carries a small amount of duplication with the narrow Protocols defined in `src/notebooklm/_capabilities.py` (and, after D2 PR-1, in each sub-client file). Keeping the factory aligned with those Protocols is a manual step; the alternative — generating it — would couple test plumbing to source code in ways that defeat the "tests don't pin shape" property the policy is trying to recover.
- The initial allowlist is non-trivial (49 files). Migrating them is a large project, scheduled explicitly as D1 PR-2 (auth-side, 6 files) and D1 PR-3 (CLI-side, 7 files), with the residual ~35 files reassessed at PR-3 close.

## Alternatives considered

- **Pure pytest fixtures (no factory function).** Rejected per `/grill-me` Q11. A fixture-only world requires one `@pytest.fixture` per override combination — `fake_core_with_rpc_returning_x`, `fake_core_with_authuser_42`, `fake_core_raising_chat_error_on_post`, and so on. The combinatorial explosion is worse than the current monkeypatch sprawl. Hybrid factory-first (factory takes `**overrides`, plus two thin fixtures wrapping the no-override and the factory-itself cases) keeps per-test customization at the call site where it's most readable.
- **`MagicMock(spec=SessionCapabilities)` instead of an explicit dataclass.** Rejected. `MagicMock(spec=…)` auto-vivifies attributes against the spec, which means: (a) it silently accepts attribute access the sub-client doesn't actually use, defeating the type-safety benefit of the narrow Protocols, and (b) it ties the test factory to the fat-union `SessionCapabilities` *which D2 PR-2 deletes*. A `types.SimpleNamespace`-shaped explicit class survives the D2 cutover and surfaces shape drift as a hard `AttributeError` rather than as a silent auto-vivified `MagicMock`.
- **Per-site allowlist entries (one allowlist entry per `(file, line)` pair).** Rejected per `/grill-me` Q12. Line numbers change on every rebase, generating spurious merge conflicts in the allowlist and making the meta-lint's failures look like merge churn rather than real findings. File-level allowlists trade a small amount of precision (we accept "this file still has offenders") for a stable substrate that survives reorderings.
- **One big-bang test rewrite (delete the 273 sites in a single PR).** Rejected. The audit measured 4,090 LOC in `test_auth.py` and 4,431 LOC in `cli/test_session.py`; touching both atomically would block every other test-touching PR for the duration of the cutover and offer no incremental verification. The chosen migration sequence (D1 PR-1 ships the substrate; D1 PR-2 migrates auth tests and deletes `_AuthFacadeModule`; D1 PR-3 migrates CLI tests and deletes the proxy block + property bridges) lets each PR's pytest run prove that the *next* shim removal is safe.
- **Deferring the lint to a CI-only check (mypy plugin or separate runner).** Rejected. A pytest-resident lint runs on every contributor's local `uv run pytest` and surfaces violations before push, which is where new violations are easiest to fix. A CI-only check would let local development continue to introduce offenders; the round-trip cost (push → CI fail → fix → push) is exactly the friction the policy is trying to remove.
- **Why this matters now.** The audit's 273-site gravity-well analysis (`arch-biggest-problem-audit.md` §D1) shows the cost compounds with every refactor: each new seam extraction either ships a new shim or breaks ~100 tests. Deferring the policy means the next architectural arc (tier 12+ or a hypothetical greenfield split) pays the same tax at higher magnitude. Establishing the substrate now — when the shims that need retirement are still finite and named — keeps the eventual migration bounded.
