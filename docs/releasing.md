# Release Checklist

**Status:** Active
**Last Updated:** 2026-01-14

Checklist for releasing a new version of `notebooklm-py`.

> **For Claude Code:** When asked to prepare a release, follow this checklist step by step. Complete each checkbox before moving to the next. Ask the user to confirm before pushing or tagging.

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

### Version Bump

- [ ] Determine version bump type (see [Version Numbering](#version-numbering) for details)
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

### Commit

- [ ] Verify changes:
  ```bash
  git diff
  ```
- [ ] Commit:
  ```bash
  git add pyproject.toml CHANGELOG.md
  git commit -m "chore: release vX.Y.Z"
  ```

---

## CI Verification

### Push to Main

- [ ] Push to main:
  ```bash
  git push origin main
  ```
- [ ] Wait for **test.yml** to pass:
  - Linting and formatting
  - Type checking
  - Unit and integration tests (Python 3.10-3.14, all platforms)

### E2E Tests on Main

- [ ] Go to **Actions** → **Nightly E2E**
- [ ] Click **Run workflow**, select `main` branch
- [ ] Wait for E2E tests to pass

---

## Package Verification

### Publish to TestPyPI

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
  1. Fix issues locally
  2. Bump patch version in `pyproject.toml`
  3. Update `CHANGELOG.md` with fix
  4. Amend or create new commit
  5. Push and re-run **Publish to TestPyPI**

---

## Release

### Tag and Publish

- [ ] Create tag:
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

## Troubleshooting

### CI fails after push

> **Warning:** Only do this immediately after your own push, before anyone else pulls.

```bash
# Fix locally, then amend
git add -A
git commit --amend --no-edit
git push --force origin main
```

### Need to abort after commit

> **Warning:** Force pushing rewrites history. Only do this if you haven't shared the commit.

```bash
# Undo release commit (local only)
git reset --hard HEAD~1

# If already pushed (use with caution)
git push --force origin main
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

| Change Type | Bump | Example |
|-------------|------|---------|
| RPC method ID fixes | PATCH | 0.1.0 → 0.1.1 |
| Bug fixes | PATCH | 0.1.1 → 0.1.2 |
| New features | MINOR | 0.1.2 → 0.2.0 |
| Breaking changes | MAJOR | 0.2.0 → 1.0.0 |

See [stability.md](stability.md) for full versioning policy.
