# Release Checklist

**Status:** Active
**Last Updated:** 2026-05-23

Checklist for releasing a new version of `notebooklm-py`.

> **For Claude Code:** Follow this checklist step by step. **NO STEPS ARE OPTIONAL.** "Quick release" means efficient execution, NOT skipping steps.
>
> **Critical rules:**
> 1. **Always use a worktree** - never work directly on main for releases
> 2. **Use PRs, not direct pushes** - all release changes go through a PR
> 3. **Explicit confirmation required** for: creating PR, publishing to TestPyPI, creating tags, pushing tags
> 4. **"ok" is not confirmation** - restate what you're about to do and wait for explicit "yes"
> 5. **TestPyPI is mandatory** - it catches packaging issues that tests cannot detect
> 6. **Public API compatibility audit is mandatory** - do not publish while
>    `scripts/audit_public_api_compat.py` reports unapproved breaks

---

## Pre-Flight Summary

Before starting, present this summary to the user:

```
Release Plan for vX.Y.Z:
1. Create release worktree (`release/vX.Y.Z` branch)
2. Update pyproject.toml and CHANGELOG.md
3. Run public API compatibility audit
4. Run pre-commit checks (ruff, mypy, pytest)
5. Commit changes
6. ⏸️ CONFIRM: Create PR to main?
7. Wait for CI to pass on PR
8. Run E2E and RPC health checks on release branch
9. ⏸️ CONFIRM: Publish to TestPyPI?
10. Verify TestPyPI package
11. Merge PR to main
12. ⏸️ CONFIRM: Create and push tag vX.Y.Z?
13. Wait for PyPI publish
14. Create GitHub release
15. Clean up worktree

Proceed with release preparation?
```

---

## Setup

### Create Release Worktree

- [ ] Create a dedicated worktree for the release:
  ```bash
  git worktree add .worktrees/vX.Y.Z -b release/vX.Y.Z main
  cd .worktrees/vX.Y.Z
  ```
- [ ] Set up the development environment:
  ```bash
  # Canonical contributor install; add --extra mcp/--extra server when
  # validating those adapters locally. `[all]` also includes mcp+server
  # and deliberately excludes cookies (Python 3.13+ rookiepy issue).
  uv sync --frozen --extra browser --extra dev --extra markdown
  uv run playwright install chromium
  ```

---

## Pre-Release

### Documentation

- [ ] Verify README.md reflects current features
- [ ] Check CLI reference matches `notebooklm --help` output
- [ ] Verify Python API docs match public exports in `__init__.py`
- [ ] Update `Last Updated` dates in modified docs
- [ ] Verify example scripts have valid syntax:
  ```bash
  uv run python -m py_compile examples/*.py
  ```

**Related docs to check/update if relevant:**

| Doc | Update when... |
|-----|----------------|
| [README.md](../README.md) | New features, changed capabilities, Beyond the Web UI section |
| [SKILL.md](../SKILL.md) | New CLI commands, changed flags, new workflows |
| [cli-reference.md](cli-reference.md) | Any CLI changes |
| [python-api.md](python-api.md) | New/changed Python API |
| [troubleshooting.md](troubleshooting.md) | New known issues, fixed issues to remove |
| [development.md](development.md) | Architecture changes, new test patterns |
| [configuration.md](configuration.md) | New env vars, config options |
| [stability.md](stability.md) | Public API changes, deprecations |

### Version Bump

- [ ] Determine version bump type using this decision tree:

  ```
  Did you add new items to `__all__` in `__init__.py`?
  ├── YES → MINOR (new public API)
  └── NO → PATCH (fixes, logging, UX, internal improvements)

  When in doubt, it's PATCH.
  ```

  See [Version Numbering](#version-numbering) for full details.

- [ ] Update version in `pyproject.toml`:
  ```toml
  version = "X.Y.Z"
  ```

### Public API Compatibility Gate

- [ ] Run the cross-release public API audit before committing release changes:
  ```bash
  uv run python scripts/audit_public_api_compat.py
  ```
- [ ] Read every reported break. The audit compares this checkout with the
  latest reachable release tag (override with `--baseline-ref vX.Y.Z` only when
  auditing against a specific previous release).
- [ ] If the audit reports an unapproved break, prefer a compatibility shim or
  restored alias. Do **not** proceed to packaging while unapproved breaks remain.
- [ ] If a break is intentional and allowed by the stability policy, update all
  of these in the same PR:
  - `docs/stability.md` and `docs/deprecations.md` when the change is a
    deprecation removal or newly deprecated surface
  - `CHANGELOG.md` with the migration path
  - `scripts/api-compat-allowlist.json` with the exact `code`, `object`, and a
    reviewer-readable reason; use its `extra_public_names` section only for
    documented names that are intentionally public but not listed in
    `__all__`
- [ ] Re-run the audit. The acceptable release state is either no breaks or
  only reviewed allowlisted breaks printed by the script.
- [ ] If CLI commands, flags, arguments, help text, or env-var bindings changed,
  also run the CLI contract baseline:
  ```bash
  uv run pytest tests/unit/cli/test_cli_contract.py
  ```

The allowlist is not a bypass for accidental breakage. It is a paper trail for
intentional removals already permitted by `docs/stability.md`, such as a
completed deprecation cycle. Public enum members exposed through documented
imports, including `notebooklm.rpc.RPCMethod`, count as API surface. So do
documented client namespace methods under `NotebookLMClient.notebooks`,
`sources`, `artifacts`, `chat`, `research`, `notes`, `settings`, and
`sharing`. Function signatures include positional/keyword compatibility and
default values; changing a default is a public behavior change.

The allowlist is **release-scoped**: each entry records a break pending the
*next* tag, not a permanent exemption. Once `vX.Y.Z` ships, its entries are in
the baseline and must be pruned — see
[Prune the API-Compat Allowlist](#prune-the-api-compat-allowlist). The Code
Quality job runs the audit with `--check-stale`, so a stale entry (one matching
no break against the baseline) is a CI failure, not silent cruft.

### Changelog

- [ ] Get commits since last release:
  ```bash
  git log $(git describe --tags --abbrev=0)..HEAD --oneline
  ```
- [ ] Generate changelog entries in Keep a Changelog format:
  - **Added** - New features
  - **Fixed** - Bug fixes
  - **Changed** - Changes in existing functionality
  - **Deprecated** - Soon-to-be removed features
  - **Removed** - Removed features
  - **Security** - Security fixes
- [ ] Add entries under `## [Unreleased]` in `CHANGELOG.md`
- [ ] Move `[Unreleased]` content to new version section:
  ```markdown
  ## [Unreleased]

  ## [X.Y.Z] - YYYY-MM-DD
  ```
- [ ] Update comparison links at bottom of `CHANGELOG.md`:
  ```markdown
  [Unreleased]: https://github.com/teng-lin/notebooklm-py/compare/vX.Y.Z...HEAD
  [X.Y.Z]: https://github.com/teng-lin/notebooklm-py/compare/vPREV...vX.Y.Z
  ```

### Pre-Commit Checks

- [ ] Run all checks before committing:
  ```bash
  uv run pre-commit run --all-files && uv run mypy src/notebooklm --ignore-missing-imports && uv run pytest
  ```
- [ ] Ensure CI runs the same lint gate (`pre-commit run --all-files`) as local release prep
- [ ] Run documentation drift checks (mirror the CI gates in `.github/workflows/test.yml`):
  ```bash
  uv run python scripts/check_ci_install_parity.py
  uv run python scripts/check_claude_md_freshness.py
  uv run python scripts/check_docs_module_refs.py
  # second run confirms release edits did not introduce new API drift
  uv run python scripts/audit_public_api_compat.py --check-stale
  ```
- [ ] Fix any issues before proceeding

### Commit

- [ ] Verify changes:
  ```bash
  git diff
  ```
- [ ] Commit:
  ```bash
  git add pyproject.toml CHANGELOG.md docs/
  git commit -m "chore: release vX.Y.Z"
  ```
- [ ] Show commit to user:
  ```bash
  git show --stat
  ```

---

## CI Verification

### Create Pull Request

- [ ] **⏸️ CONFIRM:** Ask user "Ready to create PR for release vX.Y.Z?"
- [ ] Push branch and create PR:
  ```bash
  git push -u origin release/vX.Y.Z
  gh pr create --title "chore: release vX.Y.Z" --body "Release vX.Y.Z

  See CHANGELOG.md for details."
  ```
- [ ] Wait for **test.yml** to pass:
  - Linting and formatting
  - Type checking
  - Unit and integration tests (Python 3.10-3.14, all platforms)

### E2E Tests on Release Branch

- [ ] Go to **Actions** → **Nightly E2E**
- [ ] Click **Run workflow**, set **custom_branch** to `release/vX.Y.Z`
- [ ] Wait for E2E tests to pass
- [ ] If E2E tests fail:
  1. Fix issues in the release worktree
  2. Commit and push
  3. Re-run E2E tests

### RPC Health Check on Release Branch

- [ ] Go to **Actions** → **RPC Health Check**
- [ ] Click **Run workflow**, set **custom_branch** to `release/vX.Y.Z`
- [ ] Wait for RPC health check to pass
- [ ] If RPC health check fails:
  1. Fix issues in the release worktree
  2. Commit and push
  3. Re-run RPC health check

### MCP connector smoke (manual, per release)

The nightly E2E run now installs `--extra mcp` and exercises the MCP/CLI layers
against the live API (`tests/e2e/test_mcp*.py`, `test_cli_live.py`) — so the
on-demand Nightly run above already covers Layer A (tools ⇄ live API) and Layer B
(HTTP transport + signed-URL routes, in-process). What it **cannot** cover is
claude.ai actually driving the remote connector (OAuth login + the browser
upload/download pages). Verify that by hand, once per release (~10 min):

- [ ] `cd deploy && make dev` (or point at your deployed tunnel) and connect the
      connector in claude.ai.
- [ ] **OAuth login page** renders and the password gate accepts your password.
- [ ] List notebooks through the connector.
- [ ] Add a URL source.
- [ ] Ask a question and get a grounded answer.
- [ ] Generate **one** artifact (e.g. a report).
- [ ] **Upload** a local file via the signed link (open it in a browser, pick a
      file, confirm the source lands).
- [ ] **Download** the generated artifact via the signed link.

Bootstrap / sanity helper for the upload+download halves (drives a RUNNING
server's file routes, prints PASS/FAIL). Requires the `mcp` extra (e.g.
`uv sync --extra mcp`):

```bash
python scripts/mcp_live_smoke.py \
    --base-url https://your-tunnel.example.com \
    --bearer "$NOTEBOOKLM_MCP_TOKEN" \
    --notebook <notebook-id>
```

---

## Package Verification

> **⚠️ REQUIRED:** Do NOT skip TestPyPI verification. Always test on TestPyPI before publishing to PyPI. This catches packaging issues that unit tests cannot detect (missing files, broken imports, dependency problems).

### Publish to TestPyPI

- [ ] **⏸️ CONFIRM:** Ask user "Ready to publish to TestPyPI?"
- [ ] Go to **Actions** → **Publish to TestPyPI**
- [ ] Click **Run workflow**, select the **release/vX.Y.Z** branch
- [ ] Wait for upload to complete
- [ ] Verify package appears: https://test.pypi.org/project/notebooklm-py/

> **Note:** TestPyPI does not allow re-uploading the same version. If you need to fix issues after publishing, bump the patch version and start over.

### Verify TestPyPI Package

- [ ] Go to **Actions** → **Verify Package**
- [ ] Click **Run workflow** with **source**: `testpypi`
- [ ] Wait for all tests to pass (unit, integration, E2E)
- [ ] If verification fails:
  1. Fix issues in the release worktree
  2. Bump patch version in `pyproject.toml`
  3. Update `CHANGELOG.md` with fix
  4. Commit, push, and re-run **Publish to TestPyPI**

#### How the verify chain works

The **Verify Package** workflow (`.github/workflows/verify-package.yml`) exercises a published wheel in two phases so packaging bugs cannot silently fall through to a stale PyPI mirror:

1. **Dep tree from `uv.lock`.** `uv sync --frozen --extra browser --extra dev --extra markdown --extra mcp --extra server` installs the locked dependency tree for the full non-cookies extra set into `.venv/`: browser automation, developer tooling, Markdown export, MCP, and REST server dependencies. `cookies` stays excluded because of the Python 3.13+ `rookiepy` issue. This produces a deterministic dep tree without any TestPyPI lookups.
2. **Wheel from the chosen index, `--no-deps`.** `uv pip install --python .venv/bin/python --no-deps --reinstall --no-cache --only-binary=:all: --index-url <testpypi|pypi> "notebooklm-py==<version>"` swaps the editable install left behind by `uv sync` for the actual published wheel. `--no-deps` is load-bearing: without it the previous `--extra-index-url https://pypi.org/simple/` fallback would mask a broken/missing TestPyPI upload by resolving an older version from PyPI. `--reinstall --no-cache --only-binary=:all:` guarantee we test the freshly-uploaded wheel and never a cached sdist. The explicit `--python .venv/bin/python` is required because `uv sync` does not seed `pip` into the project venv — a bare `source .venv/bin/activate && pip install …` would silently fall back to the runner's system pip and leave the editable install in place.

The same chain runs for `source: pypi` (post-publish verification) — only the wheel index changes; the locked dep tree is identical.

The `Publish to PyPI` step in `publish.yml` also opts into **PEP 740 attestations** (`attestations: true`). `pypa/gh-action-pypi-publish` generates an in-toto attestation per uploaded artifact and signs it under Trusted Publishing (OIDC, no API token); PyPI accepts and stores the attestation alongside the wheel, giving downstream consumers cryptographic proof the wheel was built by this GitHub workflow on this tagged commit. The PyPA action enables attestations by default for Trusted Publishing flows, so the explicit `attestations: true` is documentation-as-code rather than a feature flag.

---

## Merge to Main

- [ ] Once TestPyPI verification passes, merge the PR:
  ```bash
  gh pr merge --squash --delete-branch
  ```
- [ ] Pull latest main (in main repo):
  ```bash
  cd /path/to/notebooklm-py
  git pull origin main
  ```

---

## Release

### Tag and Publish

- [ ] **⏸️ CONFIRM:** Ask user "TestPyPI verified. Ready to create tag vX.Y.Z and publish to PyPI? This is irreversible."
- [ ] Create tag (on main branch):
  ```bash
  git tag vX.Y.Z
  ```
- [ ] Push tag:
  ```bash
  git push origin vX.Y.Z
  ```
- [ ] Wait for **publish.yml** to complete
- [ ] Verify on PyPI: https://pypi.org/project/notebooklm-py/

### PyPI Verification

- [ ] Go to **Actions** → **Verify Package**
- [ ] Click **Run workflow** with:
  - **source**: `pypi`
- [ ] Wait for all tests to pass

### GitHub Release

- [ ] Create release from tag:
  ```bash
  gh release create vX.Y.Z --title "vX.Y.Z" --notes "$(cat CHANGELOG.md | sed -n '/## \[X.Y.Z\]/,/## \[/p' | sed '$d')"
  ```
  Or manually:
  - Go to **Releases** → **Draft a new release**
  - Select tag `vX.Y.Z`
  - Title: `vX.Y.Z`
  - Copy release notes from `CHANGELOG.md`
  - Publish release

### Prune the API-Compat Allowlist

Pushing the tag advances the audit baseline — `audit_public_api_compat.py`
resolves it from `git describe --tags --abbrev=0`, so the breaks you just
shipped are now part of the `vX.Y.Z` baseline and are **no longer breaks against
it**. The baseline advances automatically; the allowlist is the manual half that
must reset, or it accumulates dead entries that describe nothing.

The lifecycle to keep in mind:

- **baseline** = the last *released* version (automatic — it's the latest tag).
- **allowlist** = the intentional breaks pending the *next* release. It should
  reset to (near) empty at each release boundary.

Concretely, **after the tag is pushed**, prune the entries that just shipped:

- [ ] In a follow-up PR (on `main`, after the tag exists), remove from
  `scripts/api-compat-allowlist.json` every `allowed_breaks` entry that
  described a `vPREV → vX.Y.Z` change. These are now baked into the `vX.Y.Z`
  baseline. List the stale entries with:
  ```bash
  uv run python scripts/audit_public_api_compat.py --json \
    | python -c "import json,sys; print('\n'.join(f\"{e['code']}  {e['object']}\" for e in json.load(sys.stdin)['stale_allowances']))"
  ```
- [ ] Re-run the gate in strict mode — it must report **no stale entries**:
  ```bash
  uv run python scripts/audit_public_api_compat.py --check-stale
  ```

> **Forcing function:** the Code Quality job runs the audit with `--check-stale`,
> which **fails** on any allowlist entry that matches no break against the
> baseline. So the moment `vX.Y.Z` is tagged, the just-shipped entries become
> stale and CI goes red until this prune PR lands — the prune is mandatory, not
> a checklist nicety. The pair-aware rule keeps the two path-views of a callable
> (`notebooklm.X` and `notebooklm.client.X`) together: a unit is pruned only when
> *neither* view still matches a break.

---

## Cleanup

### Remove Release Worktree

- [ ] Return to main repo:
  ```bash
  cd /path/to/notebooklm-py
  ```
- [ ] Remove the release worktree:
  ```bash
  git worktree remove .worktrees/vX.Y.Z
  ```
- [ ] Delete the local branch (if not already deleted by PR merge):
  ```bash
  git branch -d release/vX.Y.Z
  ```

---

## Troubleshooting

### CI fails on PR

Fix issues in the release worktree and push again:
```bash
# In release worktree
git add -A
git commit -m "fix: address CI failures"
git push
```

### Need to abort release

```bash
# Close the PR without merging
gh pr close

# Remove worktree
git worktree remove .worktrees/vX.Y.Z

# Delete local branch
git branch -D release/vX.Y.Z

# Delete remote branch (if pushed)
git push origin --delete release/vX.Y.Z
```

### Tag already exists

```bash
# Delete local tag
git tag -d vX.Y.Z

# Delete remote tag (if pushed)
git push origin :refs/tags/vX.Y.Z
```

### TestPyPI upload fails

- Check if version already exists on TestPyPI
- TestPyPI doesn't allow re-uploading same version
- Bump to next patch version if needed

---

## Version Numbering

**IMPORTANT:** Read [stability.md](stability.md) before deciding version bump.

| Change Type | Bump | Example |
|-------------|------|---------|
| RPC method ID fixes | PATCH | 0.1.0 → 0.1.1 |
| Bug fixes | PATCH | 0.1.1 → 0.1.2 |
| Internal improvements (logging, auth UX, CI) | PATCH | 0.1.2 → 0.1.3 |
| **New public API** (new classes, methods in `__all__`) | MINOR | 0.1.3 → 0.2.0 |
| Breaking changes to public API | MAJOR | 0.2.0 → 1.0.0 |

**Key distinction:** "New features" means new **public API surface** (additions to `__all__` in `__init__.py`). Internal improvements, better error messages, logging enhancements, and UX improvements are PATCH releases.
