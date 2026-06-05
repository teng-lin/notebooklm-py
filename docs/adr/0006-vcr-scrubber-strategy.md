# ADR-0006: VCR cassette scrubber strategy

## Status

Accepted (retroactive). Documents the cassette-scrubbing architecture shipped across the tier-8 RPC/VCR remediation arc (51 PRs, all merged) and the follow-up `audit-ref-scrub-2026-05-17` cleanup.

## Context

The integration test suite records live `batchexecute` traffic against Google's NotebookLM endpoints using `vcrpy` cassettes (YAML files under `tests/cassettes/`). Recording real traffic captures real authentication material — `__Secure-1PSID*` cookies, OAuth bearer tokens, `at` CSRF tokens, account IDs, email addresses, Drive file IDs, and other PII that must never land in version control.

Three classes of leak risk exist:

1. **Direct credential leaks** — full cookie values, full OAuth tokens, the `at` CSRF token, the `__Secure-1PSIDTS` rotation cookie.
2. **Domain-specific identifiers** — account email, Drive file IDs, notebook IDs, source IDs. These are not credentials, but they are PII and they are unique enough to fingerprint a real account.
3. **Indirect leaks via headers and URLs** — request headers (`Authorization`, `Cookie`, `x-goog-authuser`), URL query parameters (`authuser`, `at`), and response headers (`Set-Cookie`).

The codebase ships a layered scrubber:

- **Cassette patterns** (`tests/cassette_patterns.py`) define `SCRUB_PLACEHOLDERS` — the canonical replacement strings (`<SCRUBBED_COOKIE>`, `<SCRUBBED_BEARER_TOKEN>`, `<SCRUBBED_EMAIL>`, etc.) and the regex set that matches each provider-specific leak class.
- **VCR config** (`tests/vcr_config.py`) wires the scrubber into `vcrpy`'s `before_record_request` / `before_record_response` hooks, plus a body scrubber that operates on decoded JSON / urlencoded payloads.
- **Sanitizer unit tests** (`tests/unit/test_cassette_sanitizer.py`, `tests/_guardrails/test_cassette_shapes.py`) verify scrubbing on a fixed corpus of synthetic payloads.
- **Pre-commit guard** (`tests/scripts/check_cassettes_clean.py`) re-scans every cassette in `tests/cassettes/` and rejects any cassette whose payload contains a known leak pattern. The guard runs as a standalone script — it is not invoked by `pytest`, so contributors must run it explicitly before pushing. The guard has caught regressions that pytest alone did not surface.
- **Re-scrub script** (`scripts/rescrub-cassettes.py`, covered by `tests/unit/test_rescrub_cassettes_script.py`) re-runs the scrubber across the entire cassette corpus when a new leak class is added to the regex set, so older cassettes are upgraded in place.

The audit-ref-scrub cleanup (2026-05-17) added the `is_clean` filter helper so that *widening* a leak detector beyond its original provider allowlist would not re-flag the canonical placeholders (the placeholders contain provider-name substrings). The fix design is recorded in the Decision and Alternatives sections of this ADR — `SCRUB_PLACEHOLDERS` + `is_clean` filter, not regex negative-lookahead.

## Decision

Cassette scrubbing is a multi-stage pipeline with the following invariants:

1. **All scrubbing happens at recording time**, before the cassette is written to disk. Replay-time scrubbing is rejected — once a leak lands on disk it is already in git history.
2. **Replacement strings are canonical** and defined in one place (`SCRUB_PLACEHOLDERS` in `tests/cassette_patterns.py`). A placeholder is *idempotent*: running the scrubber twice produces the same output.
3. **Leak-class detection** is provider-aware. The regex set distinguishes Google credential shapes (`__Secure-1PSID*`, `at=…`) from email shapes from Drive-file-ID shapes from notebook-ID shapes; each leak class has its own scrub rule.
4. **The `is_clean` filter excludes canonical placeholders** from the leak-detection pass. This prevents the "widening a detector re-flags placeholders" failure mode by construction.
5. **The pre-commit guard (`check_cassettes_clean.py`) is the authoritative truth.** A cassette is considered clean iff this guard accepts it. Adding new leak classes means updating both the scrubber *and* the guard's expected-placeholder list in lock-step.
6. **Sanitizer unit tests are required**, not optional. Any new scrub rule lands with at least one positive case (the rule fires) and one negative case (the canonical placeholder is left alone).
7. **Cassettes that pre-date a new scrub rule must be re-scrubbed** before the new rule merges. The re-scrub script is the canonical mechanism; manual edits are forbidden.

## Consequences

**Wanted:**

- Credentials and PII never reach the on-disk cassette corpus. Tier 8 closed roughly 51 leak classes across as many PRs.
- The scrub pipeline is reviewable in isolation: regex set, replacement strings, hook wiring, guard script, sanitizer tests. Each can be audited as a unit.
- Idempotency means a contributor can run the scrubber locally during cassette capture without worrying about double-scrubbing.
- The guard catches leaks that survive scrubbing (a leak class the scrubber doesn't yet know about) by treating *any* non-placeholder credential-shaped substring as a violation.

**Unwanted:**

- The scrubber must run on `before_record_request` *and* `before_record_response` *and* on the JSON body, which means three call sites. A scrub rule added in only one site is a subtle leak path. The sanitizer unit tests cover this by including request, response, and body cases.
- The `is_clean` filter is a band-aid against the placeholder-re-flag failure mode. The fundamental issue is that placeholder strings contain provider-name substrings; widening any provider-aware regex risks re-flagging them. The filter handles this without resorting to regex negative-lookahead (which the audit explicitly rejected as a fix).
- The pre-commit guard is *not* run by `pytest`. Contributors who skip `python tests/scripts/check_cassettes_clean.py` before pushing may push a leak that CI catches but local tests do not. The guard is fast (≤ 5 seconds on the current corpus) so the cost of running it is low — but the failure mode is real and documented in memory.
- Re-scrubbing the corpus when a new leak class is added produces large, mechanical diffs that obscure the human-readable diff in the same PR. The convention is to split: "add scrub rule" and "re-scrub corpus" are two commits, ideally two PRs.

## Alternatives considered

- **No scrubbing — record real credentials.** Rejected. Cassettes are committed to the repository; real credentials in a public repo are unrecoverable from a security perspective. The cost of even one leak (rotation, revocation, account audit) dwarfs the maintenance cost of the scrubber.
- **Scrub per test (every test owns its own scrubber).** Rejected. The leak classes are global: any cassette can contain any leak shape. Per-test scrubbing means every test re-implements the scrub set, drift is inevitable, and the audit cannot easily verify coverage. Centralised scrub rules + central guard is the only design that scales past a handful of cassettes.
- **Use vcrpy's built-in `filter_headers` / `filter_query_parameters`.** Partially applied — they handle the header / query layer. They do not handle the response body, do not handle URL-encoded body fields, and do not handle JSON body fields. The scrubber covers what vcrpy does not.
- **Encrypt cassettes at rest, scrub at decryption time.** Rejected. Encryption adds key-management burden and does not solve the underlying problem: contributors run tests with the key, so a misconfigured local run can decrypt and re-record under leaked credentials. Plain-text-but-scrubbed is operationally simpler and the security model is "no credentials ever exist in the file" rather than "credentials exist behind a key we trust everyone with."
- **Regex negative-lookahead to skip placeholders in the leak detector.** Rejected (and explicitly captured in memory). Negative-lookahead is fragile, slow, and entangles the placeholder strings with every leak pattern. The `is_clean` filter applied after pattern matching is strictly more maintainable.
- **CI-only enforcement (skip the pre-commit guard).** Rejected. The CI feedback loop is minutes; the local guard's feedback loop is seconds. The audit (specifically the audit-ref-scrub-2026-05-17 cleanup) measured the cost of catching leaks at CI-only and chose to maintain the local guard for the fast loop.
