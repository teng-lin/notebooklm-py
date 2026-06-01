"""Thread-safety / concurrency integration test harness.

This package houses the reusable test infrastructure that the
concurrency-hardening regression PRs consume. It is intentionally
*infrastructure-only*: no production-code coverage, no per-bug regression
tests. Those live in dedicated per-fix PRs that ``import`` from this
package's ``conftest.py`` fixtures.

See ``README.md`` for fixture API and explicit non-goals.
"""
