# Release Checklist

**Status:** Active
**Last Updated:** 2026-01-21

Checklist for releasing a new version of `notebooklm-py`.

> **For Claude Code:** Follow this checklist step by step. **NO STEPS ARE OPTIONAL.** "Quick release" means efficient execution, NOT skipping steps.
>
> **Critical rules:**
> 1. **Always use a worktree** - never work directly on main for releases
> 2. **Use PRs, not direct pushes** - all release changes go through a PR
> 3. **Explicit confirmation required** for: creating PR, publishing to TestPyPI, creating tags, pushing tags
> 4. **"ok" is not confirmation** - restate what you're about to do and wait for explicit "yes"
> 5. **TestPyPI is mandatory** - it catches packaging issues that tests cannot detect

---

## Pre-Flight Summary

Before starting, present this summary to the user:

```
Release Plan for vX.Y.Z:
1. Create release worktree (release/X.Y.Z branch)
2. Update pyproject.toml and CHANGELOG.md
3. Run pre-commit checks (ruff, mypy, pytest)
4. Commit changes
5. ⏸️ CONFIRM: Create PR to main?
6. Wait for CI to pass on PR
7. Merge PR to main
8. ⏸️ CONFIRM: Publish to TestPyPI?
9. Verify TestPyPI package
10. ⏸️ CONFIRM: Create and push tag vX.Y.Z?
11. Wait for PyPI publish
12. Create GitHub release
13. Clean up worktree

Proceed with release preparation?
```

---

## Setup

### Create Release Worktree

- [ ] Create a dedicated worktree for the release:
  ```bash
  git worktree add .worktrees/release-X.Y.Z -b release/X.Y.Z
  cd .worktrees/release-X.Y.Z
  ```
- [ ] Set up the development environment:
  ```bash
  uv venv .venv
  uv pip install -e ".[all]"
  playwright install chromium
  source .venv/bin/activate
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
  python -m py_compile docs/examples/*.py
  ```

**Related docs to check/update if relevant:**

| Doc | Update when... |
|-----|----------------|
| [SKILL.md](../src/notebooklm/data/SKILL.md) | New CLI commands, changed flags, new workflows |
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
  ruff format src/ tests/ && ruff check src/ tests/ && mypy src/notebooklm --ignore-missing-imports && pytest
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
  git push -u origin release/X.Y.Z
  gh pr create --title "chore: release vX.Y.Z" --body "Release vX.Y.Z

  See CHANGELOG.md for details."
  ```
- [ ] Wait for **test.yml** to pass:
  - Linting and formatting
  - Type checking
  - Unit and integration tests (Python 3.10-3.14, all platforms)

### Merge to Main

- [ ] Once CI passes, merge the PR:
  ```bash
  gh pr merge --squash --delete-branch
  ```
- [ ] Pull latest main (in main repo or update worktree):
  ```bash
  git checkout main && git pull origin main
  ```

### E2E Tests on Main (Optional)

- [ ] Go to **Actions** → **Nightly E2E**
- [ ] Click **Run workflow**, select `main` branch
- [ ] Wait for E2E tests to pass

---

## Package Verification

> **⚠️ REQUIRED:** Do NOT skip TestPyPI verification. Always test on TestPyPI before publishing to PyPI. This catches packaging issues that unit tests cannot detect (missing files, broken imports, dependency problems).

### Publish to TestPyPI

- [ ] **⏸️ CONFIRM:** Ask user "Ready to publish to TestPyPI?"
- [ ] Go to **Actions** → **Publish to TestPyPI**
- [ ] Click **Run workflow**
- [ ] Wait for upload to complete
- [ ] Verify package appears: https://test.pypi.org/project/notebooklm-py/

> **Note:** TestPyPI does not allow re-uploading the same version. If you need to fix issues after publishing, bump the patch version and start over.

### Verify TestPyPI Package

- [ ] Go to **Actions** → **Verify Package**
- [ ] Click **Run workflow** with **source**: `testpypi`
- [ ] Wait for all tests to pass (unit, integration, E2E)
- [ ] If verification fails:
  1. Create a new release branch with bumped patch version
  2. Fix issues
  3. Create new PR
  4. Merge and re-run **Publish to TestPyPI**

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

---

## Cleanup

### Remove Release Worktree

- [ ] Return to main repo:
  ```bash
  cd /path/to/notebooklm-py
  ```
- [ ] Remove the release worktree:
  ```bash
  git worktree remove .worktrees/release-X.Y.Z
  ```
- [ ] Delete the local branch (if not already deleted by PR merge):
  ```bash
  git branch -d release/X.Y.Z
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
git worktree remove .worktrees/release-X.Y.Z

# Delete local branch
git branch -D release/X.Y.Z

# Delete remote branch (if pushed)
git push origin --delete release/X.Y.Z
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
