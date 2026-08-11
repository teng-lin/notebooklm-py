# ADR-0028: Renaming the package for Google's "Gemini Notebook" rebrand

## Status

Proposed — v3; the identity flip is consolidated into a single 0.9.0 release
per maintainer review. v2 (same day) split it across 0.9/0.10 behind a
brand-stability gate; v1 renamed the import package too. Both are recorded in
Alternatives.

## Context

On 2026-07-16 Google renamed NotebookLM to **Gemini Notebook**
([announcement](https://blog.google/innovation-and-ai/products/gemini-notebook/notebooklm-gemini-notebook/)).
The wire protocol is unchanged, and as of 2026-08-04 **both hosts serve it**.
A live probe that day (issue #1977) reached `batchexecute` on each:

```text
GET  https://notebooklm.google.com/  -> 302 -> https://notebook.google.com/  (200, app shell)
POST https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute -> 400 (endpoint live)
POST https://notebook.google.com/_/LabsTailwindUi/data/batchexecute   -> 400 (endpoint live)
```

(A 400 on a deliberately malformed payload proves the endpoint exists.) So the
**app shell has already migrated** — the legacy host is now a redirect — while
`batchexecute` is **dual-served**. The RPC backend has not moved out from under
us, which is why most sessions still work against the legacy default.

The nightly health check later strengthened the rebrand-host evidence from an
endpoint-level 400 to an authenticated application response: at commit
`36221e0`, `LIST_NOTEBOOKS` returned HTTP 200 and echoed its `wXbhsf` RPC ID
(#2077). This records availability for that credential cohort; it is not, by
itself, a default-host policy decision.

The same authenticated nightly probe confirmed the rebrand host's
`GenerateFreeFormStreamed` chat endpoint too: HTTP 200 with a parsed stream on
2026-08-04 (#2078). This does not answer whether `/upload/_/` (Scotty) is served
there; the health check issues no upload POST, so that remains unprobed.

Two things follow, and they are easy to conflate:

- **Before the default flip, recorded integration coverage had never exercised
  rebrand-host RPC.** Every request then recorded against `notebook.google.com`
  in `tests/cassettes/` was a `GET /` returning the app shell (12 such, all in
  `collection_*`); no `batchexecute` POST to that host had been captured. That
  was a gap in our testing, not a statement about the service — before the
  allowlist change no client we shipped *could* have issued one.
- **Dual-serving is a transitional state, not a guarantee.** It is the thing to
  monitor, which is why `scripts/check_rpc_health.py` probes the rebrand host in
  its own reporting lane.

> **Amended (#2067): the trigger for moving the default has changed.**
>
> This ADR originally set the criterion as *"the legacy host ceasing to serve
> RPC, not the rebrand host starting to"* — i.e. move when the old host breaks.
> That is now superseded, for one reason: **it makes the migration an incident
> rather than a change.** The signal arrives *as* users start failing, the fix
> ships days later, and the window in which the move is invisible — the window
> we are in right now, with both hosts serving — has closed by the time we act.
>
> Waiting also buys less safety than it appears to. The evidence that the flip
> is survivable does not depend on legacy degrading: the app shell has already
> migrated (the legacy host is a 302), so a session's CSRF/session pair is
> *already* minted by `notebook.google.com` today and accepted by legacy
> `batchexecute` — visible in twelve recorded interactions in
> `tests/cassettes/collection_*.yaml`. Flipping makes the origin coherent; it
> does not introduce a new dependency. Pre-cutover profiles were measured
> against both hosts (#2067) and reached the rebrand host successfully
> *without* their legacy-scoped `OSID`, because the accounts-scoped `LSID`
> family is what actually gates the session.
>
> The revised criterion: **move while both hosts serve, keep the legacy host
> selectable via `NOTEBOOKLM_BASE_URL`, and treat the probe lane as a
> rollback-availability monitor** — a `PRESENT → ABSENT` transition on the
> legacy host now means "rollback has expired", which is the thing worth
> alarming on.
>
> What has *not* changed: dual-serving is still transitional and still the
> thing to monitor. This amendment moves the default; it does not claim the
> rebrand host is guaranteed.

Both hosts are accepted base URLs. The default is `notebook.google.com`
(#2067); the legacy host remains selectable via `NOTEBOOKLM_BASE_URL` and is
documented as the rollback lever.

Separately, the project's discoverable identity (PyPI listing, repo name, docs)
now points at a retired brand. New users will search for
"gemini notebook python". PyPI availability checked 2026-08-03:
`gemini-notebook`, `gemini-notebook-py`, `gemini-notebook-client` are all
unregistered — and so is the bare `notebooklm` dist name, which the permanent
dist/import mismatch this ADR adopts turns into a standing typosquat target
(`pip install notebooklm` becomes the most likely wrong guess, forever). PyPI
has no reservation mechanism, so squatting is a live risk on all of them.

The name is carried on two very different kinds of surface:

- **Discoverability surfaces**: PyPI dist name, GitHub repo, README/docs
  branding, MCP/desktop-extension display names, Docker image, skill archive.
  These are what searchers and new users see.
- **Operational plumbing**: the `notebooklm` import package (~2 000 in-tree
  references), ~40 `NOTEBOOKLM_*` env vars (including write-side protocol vars
  such as the credential scrub in `_auth/refresh.py` and compose-interpolation
  vars in `deploy/`), `~/.notebooklm` config home, `logging.getLogger("notebooklm")`
  namespaces (a documented API), `NotebookLMClient`, the MCP server identity
  `"notebooklm"` written into users' client configs by `notebooklm mcp install`,
  installed skill directories (`.claude/skills/notebooklm/`), and a dense
  lattice of name-pinning guardrail tests and `scripts/` tooling.

Constraints that shape the decision:

1. **Google renames products often**; this brand is weeks old. Anything
   expensive or irreversible keyed to the new name is a bet on brand stability.
2. **Renaming plumbing strands users where we have no deprecation channel**:
   configs `mcp install` already wrote to users' machines, self-host `.env` /
   `docker-compose.yml` files attached to past releases, users' `logging`
   configs, pickles of `notebooklm.*` classes, installed skill copies.
3. **pip has no conflict mechanism.** Any two dists that both ship the
   `notebooklm` package (an old `notebooklm-py` ≤0.8 install plus a renamed
   canonical dist) silently clobber shared files, and uninstalling either
   corrupts the survivor.
4. **Publishing is OIDC Trusted-Publishing only** (`publish.yml`), keyed on
   owner/repo + workflow. GitHub's redirect after a repo rename does **not**
   apply to OIDC claims, so a repo rename breaks every configured publisher
   until re-registered.
5. **README currently promises the opposite** ("The package keeps the
   `notebooklm-py` name", July 2026 note). This ADR reverses a published
   commitment and must retract it explicitly, not silently.
6. This is a single-maintainer project; every permanent dual surface is
   permanent toil. A third-party `pynotebooklm` dist already crowds the
   namespace. ADR-0018 provides the deprecation machinery for anything we do
   deprecate.

## Decision

Rename the **distribution and the discoverability surfaces** to
**`gemini-notebook-py`**, completing the flip in a **single 0.9.0 release**;
keep the **import package `notebooklm` and all operational plumbing
permanently**. Dist-name ≠ import-name is an established Python pattern
(`beautifulsoup4`/`bs4`, `scikit-learn`/`sklearn`, `pillow`/`PIL`), and every
argument this ADR's Context makes against renaming the wire layer applies
equally to the import package: churn with no functional gain, brand-stability
risk, and — decisive — the plumbing writes its name into places we cannot
patch after the fact.

| Surface | Disposition |
|---|---|
| PyPI dist `notebooklm-py` | → `gemini-notebook-py` canonical at 0.9.0; old name becomes a permanent extras-forwarding shim |
| Bare `gemini-notebook` | registered as redirect metapackage (anti-squat) |
| Bare `notebooklm` (unregistered today) | registered as a defensive placeholder depending on the canonical dist — the mismatch makes it the permanent likely typo; the `bs4` precedent |
| GitHub repo | → `teng-lin/gemini-notebook-py` (auto-redirects) |
| CLI | `gemini-notebook`, `gemini-notebook-mcp`, `gemini-notebook-server` added; `notebooklm*` scripts kept **indefinitely** (they match the import name; no deprecation) |
| Docker image / skill zip / `.mcpb` display name | new names added, old kept during wind-down |
| `import notebooklm`, `NOTEBOOKLM_*` env vars, `~/.notebooklm`, logger namespace, MCP identities (`SERVER_KEY`/`SERVER_NAME`/manifest `name`/skill dir), `[tool.notebooklm]` table | **permanent — never renamed** |
| `NotebookLMClient` | kept; `GeminiNotebookClient` added as a permanent (non-deprecated) alias |

No `gnb` short alias: a 3-letter binary is collision-prone (srsRAN ships a
`gnb` executable) and the acronym dies with the next rebrand.

The `-py` suffix signals continuity and, with the "Unofficial" description,
reduces the risk a bare `gemini-*` name reads as official Google. "Gemini" is
a hotter, more actively defended mark than "NotebookLM"; the bare-name
metapackage must carry a real dependency (not an empty squat, which PEP 541
frowns on) and we accept we may have to surrender it if challenged.

### Phase 0 — immediately

- Register `gemini-notebook-py` and `gemini-notebook` on PyPI. Placeholders
  (`0.1.0`) **depend on `notebooklm-py`** so an early `pip install` works
  rather than dead-ends; yank them once 0.9.0 ships. Register the bare
  **`notebooklm`** name in the same batch: with `import notebooklm` permanent,
  `pip install notebooklm` is the most likely typo forever, and the name is
  unregistered — exactly why the BeautifulSoup project registered `bs4`.
  Same placeholder treatment (depends on the canonical dist of the day, README
  redirects to `gemini-notebook-py`); unlike the two shims it is **not** part
  of the lockstep matrix — its dependency floor is refreshed opportunistically
  and it gets a one-time install smoke, not a per-release row. `publish.yml`'s
  tag/version validation rejects out-of-band versions, so this is a one-off
  manual/TestPyPI-style upload using PyPI *pending publishers* registered for
  both names (plus keeping `notebooklm-py`'s publisher) — all against the
  current repo name.
- README: replace the "keeps the name" note with the rename plan and its
  rationale; add `gemini`, `gemini-notebook` keywords (pyproject + manifest).

### Phase 1 — 0.9.0: the rename release (single completion checklist)

One release completes the rename. Steps are ordered; the abort point is
before step 1 (the repo rename) — from there everything ships together.
The 0.9.0 acceptance matrix in Guardrails is the executable definition of
done.

1. **Repo rename first, quiet window**: rename to
   `teng-lin/gemini-notebook-py`; immediately re-point the Trusted-Publishing
   configs for all three dists at the new repo; verify with a TestPyPI publish
   before the release. Same-PR sweep of hardcoded repo strings:
   `project.urls`, fancy-pypi-readme substitutions, badges, the OCI source
   label, the TestPyPI summary URL, and **every** `github.repository ==`
   guard — `publish-docker.yml`, `publish-mcpb.yml`,
   `verify-package.yml:150,158`, `nightly.yml`, `rpc-health.yml`, and
   `verify-artifacts.yml` (string compares don't get redirects; after a
   rename those jobs silently skip). A new repo-wide guardrail test fails CI
   on any stale `github.repository` / `teng-lin/notebooklm-py` literal
   outside historical records (CHANGELOG, ADRs), so no future guard can go
   stale silently. `tests/_guardrails/test_pypi_readme_substitutions.py`
   updates in the same PR.
2. **Dist rename**: `project.name = "gemini-notebook-py"`; update the
   self-referential `all` extra and re-lock `uv.lock`. Hatch config is
   untouched (the import package doesn't move — ever).
3. **Multi-dist publishing**: shim pyprojects live in `packaging/shims/
   {notebooklm-py,gemini-notebook}/`; `publish.yml` builds canonical + shims
   from one tag (versions asserted in lockstep), smoke-installs the shim with
   `--find-links dist/` (its pin isn't on PyPI yet), and uploads **canonical
   first**, shims after. Wheel globs/artifact names flip to
   `gemini_notebook_py-*` here (dist-keyed — `publish.yml:81`,
   `testpypi-publish.yml`, artifact names). `notebooklm-py` 0.9.0 is the
   **first shim release**; no real (file-shipping) release ever carries a
   version ≥ 0.9.0.
4. **Shim spec and isolated-tool installs**: the `notebooklm-py` shim ships
   zero Python files, zero console scripts, and **mirrors every extra**
   (`[mcp]`, `[browser]`, … → `gemini-notebook-py[<extra>]==<version>`) —
   the shipped desktop extension hardcodes `notebooklm-py[mcp]` and must
   keep resolving. Pin `==`, released in lockstep with every canonical
   release (automated by step 3). Zero scripts has a consequence for
   isolated tool installers: `pipx install` and `uv tool install` expose
   only the *requested* dist's entry points, so installing the shim that way
   yields no commands. The contract, verified by CI flow tests: (a)
   `uvx --from "notebooklm-py[mcp]" notebooklm-mcp` — the path baked into
   shipped desktop extensions — keeps working, because `uvx` resolves
   executables from the full environment; (b) `pipx install notebooklm-py`
   requires `--include-deps` and the docs/tombstone README say so; (c) tool
   installs are steered to `gemini-notebook-py` as the recommended form.
   Shipping duplicate legacy scripts from the shim instead was rejected: two
   dists owning the same `bin/` files recreates the uninstall-corruption
   hazard of constraint 3 inside every dual-install venv.
   The **`gemini-notebook` metapackage gets the identical shim contract**:
   zero Python files, zero console scripts, every extra mirrored as
   `gemini-notebook-py[<extra>]==<version>`, released in the same lockstep —
   so `pip install "gemini-notebook[mcp]"` yields the same environment and a
   working `import notebooklm`. It is a first-class row in the acceptance
   matrix and the CI flow tests, not just an anti-squat placeholder.
5. **New console scripts** `gemini-notebook{,-mcp,-server}` alongside the
   old three (in the canonical dist). No startup hints — the old names are
   not deprecated. Every entry point derives its displayed `prog` from the
   invoked name: the root Click CLI (`notebooklm_cli.py:134` hardcodes
   `prog_name="NotebookLM CLI"`), `mcp/__main__.py:181`, and
   `server/__main__.py:113` all pin the legacy identity today, so the new
   commands would advertise the old names in `--help`/`--version`.
6. **Client alias**: `GeminiNotebookClient = NotebookLMClient` exported from
   `notebooklm` (static assignment: subclassing, `isinstance`, pickle, and
   mypy all keep working; no `__getattr__` indirection).
7. **`__version__` provenance**: resolution keyed to the distribution that
   actually supplied the imported files — and ownership must be established
   by **content, not path membership**: in a dual install *both* dists'
   RECORDs list `notebooklm/__init__.py`, so `Distribution.files` path
   matching (and `packages_distributions()`) can return two candidates. The
   rule: hash the imported file's bytes and compare against each candidate
   RECORD's recorded hash, accepting only a **unique** match; when the match
   is ambiguous (identical hashes or missing RECORD hashes), fall back to
   **build-stamped** provenance (extending the existing `hatch_build.py`
   `_commit.py` bake to stamp the version), and never resolve ambiguity by
   dist-name ordering — that is exactly the wrong-version failure mode.
   `src/notebooklm/__init__.py:40` is currently keyed to the old dist only
   and would report `0.0.0.dev0` under the renamed dist. Same fix in the
   skill version stamp (`_app/skill.py`).
8. **Dual-install hazard**: `gemini-notebook-py` ships the `notebooklm`
   package, so it collides file-for-file with any pre-shim `notebooklm-py`
   install. Detection uses an **explicit real-files marker**, not a version
   threshold: warn when a co-installed `notebooklm-py` dist's RECORD lists
   `notebooklm/` files (true for every pre-shim release — all < 0.9.0 under
   this plan — and stays correct even if the boundary ever moves; a
   `packaging.version` compare is the fallback when RECORD is unavailable,
   with `PackageNotFoundError` guarded and `packaging` moved from the `dev`
   extra to base dependencies in the same PR). The warning names the fix:
   `pip uninstall notebooklm-py && pip install --force-reinstall gemini-notebook-py` —
   `--force-reinstall` is required, not `-U`: uninstalling the stale dist
   deletes the shared `notebooklm` files listed in its RECORD, and a plain
   upgrade would consider the already-current canonical install satisfied
   and never restore them. Mitigation is partial by construction: the check
   only runs when the canonical files win the collision; a stale-*last*
   install overwrites `__init__.py` and is undetectable at runtime — covered
   by docs/release notes only. Test matrix covers **install and uninstall
   orders**: canonical-only, stale-first, stale-last, uninstall-stale-after-
   dual, uninstall-shim-after-dual. Expect dependency-confusion-style
   scanner flags when a long-lived dist turns shim; pre-empt in the release
   notes.
9. **`verify-package.yml`**: replace the `--no-deps` old-name install steps
   (which break against an empty shim) with the dual-install smoke: shim +
   canonical in one venv, `import notebooklm` verified after installing each
   dist name (there is no second import path — the import package is
   permanent), all **six** scripts, plus the pipx/uv-tool/uvx flow tests
   from step 4.
10. **`mcp install`**: `_app/mcp_install.py` writes
    `uvx --from "gemini-notebook-py[mcp]" gemini-notebook-mcp` under the
    **unchanged** server key `"notebooklm"` (no duplicate entries on
    re-install, no orphaning); `desktop-extension/run_server.py` likewise.
    Previously written configs keep working via the shim indefinitely.
11. **`deploy/`**: compose/env.example/Makefile/tailscale move to the new
    image name; all `NOTEBOOKLM_*` vars stay (permanent), so existing `.env`
    files keep working. Docker pushed to both repositories;
    `tests/unit/test_deploy_compose_default.py` updated same PR.
12. **Docs and guardrails**: README and primary docs lead with
    "Gemini Notebook" (old name mentioned once per page);
    `test_install_docs.py` + SKILL.md/AGENTS.md install commands
    (wheel-embedded agent instructions must not keep teaching
    `pip install notebooklm-py`), the skill recovery hint in
    `cli/skill_cmd.py` (`pip install --force-reinstall notebooklm-py` → new
    dist), `test_skill_packaging.py`, `test_mcp_desktop_extension.py`,
    mcp-install tests, `tests/_guardrails/test_public_surface.py` (new
    export), and CLI contract baseline regen (ADR-0022 machinery) for the
    new scripts — all in the release PRs, not deferred.

### Phase 2 — post-0.9.0 bake (optional)

The long-tail docs sweep (~1 900 refs), `examples/` prose, issue/PR
templates, `SECURITY.md`, `CLAUDE.md`/`CONTRIBUTING.md`. Watch signals:
download split, shim bug reports, brand stability. `import notebooklm`
examples are **correct forever** — no code sample churn.

### Phase 3 — wind-down (gated; no removal cliff)

When the wind-down gate says so (Guardrails), dual asset publishing (Docker,
skill zip) ends. `notebooklm-py` gets a final shim pinned
`gemini-notebook-py>=<current major>,<next major>` with a tombstone README —
and because the import package never goes away, that terminal shim keeps
**working** (install + `import notebooklm`) rather than silently breaking.
The open range is chosen over a frozen `==` because a stale exact pin would
block an explicit `pip install -U gemini-notebook-py` in environments that
still carry the shim — not because upgrades flow on their own: pip's default
only-if-needed strategy never upgrades a dependency that still satisfies its
range, and the terminal shim has no further releases, so canonical upgrades
reach terminal-shim users only when they upgrade `gemini-notebook-py`
directly (the tombstone README states exactly that). The range still
carries an obligation: every canonical release inside a shim-advertised
range must preserve all legacy extra names and console scripts — the
shim-equivalence test below enforces this for as long as any published
shim's range is open, so a future release cannot drop `[mcp]` out from
under `notebooklm-py[mcp]` installs.

### Guardrails

- **0.9.0 acceptance matrix — the executable definition of done.** 0.9.0
  does not ship until every row passes:
  - *Publishing*: OIDC publishers verified for all three dists against the
    renamed repo via a TestPyPI rehearsal; canonical + both shims built from
    one tag with lockstep versions; upload order canonical-first exercised.
  - *Install*: `pip install gemini-notebook-py`,
    `pip install "notebooklm-py[mcp]"`, and
    `pip install "gemini-notebook[mcp]"` (the metapackage shim) all yield a
    working `import notebooklm`; extras parity across both shims and the
    canonical dist; dual install in one venv clean.
  - *CLI*: all six console scripts run; each reports the invoked name in
    `--help`/`--version`.
  - *Version*: `notebooklm.__version__` correct under canonical-only and
    under dual-install with mismatched versions, tested in **both install
    orders and with differing file contents** (hash-provenance unique-match
    path and the ambiguous → build-stamp fallback both exercised).
  - *Collision*: stale-install warning fires in the stale-first order,
    remediation command verified to restore a working environment, and the
    documented-only status of stale-last confirmed by test.
  - *Tool installers*: `uvx --from "notebooklm-py[mcp]" notebooklm-mcp`
    works; `pipx install --include-deps notebooklm-py` exposes the legacy
    scripts; plain-shim pipx behavior documented.
  - *Integrations*: `mcp install` writes the new uvx spec under the old
    server key; desktop-extension launcher updated; `deploy/` compose/env
    render with the new image and old env vars.
  - *Guardrails/baselines*: public-surface, install-docs, CLI-contract,
    deploy-compose, pypi-readme-substitution baselines regenerated; the new
    repo-wide stale-`github.repository` literal guard green.
- **Wind-down gate (Phase 3)**: ≥75 % of combined downloads on the new dist
  for 2 consecutive months, or 12 months after 0.9.0, whichever first.
- **Shim equivalence test** (in canonical repo CI): during dual publishing,
  shim metadata mirrors every canonical extra and pins the exact canonical
  version; after wind-down, every canonical release inside any published
  shim's open range must retain all legacy extra names and console scripts.

## Consequences

- Permanent dist/import mismatch (`pip install gemini-notebook-py`,
  `import notebooklm`) — well-precedented, documented in the README's first
  screenful. In exchange: no 2 000-reference tree move, no logger-namespace
  break, no pickle/mypy alias machinery, no `git blame` damage, no
  `scripts/`+mypy+coverage+ruff config sweep, and the plumbing keeps working
  in every config file we've ever written to a user's machine.
- Old-name users are never broken: the shim (with extras) resolves
  indefinitely, and its terminal pin still yields a working install.
- Consolidating the flip into 0.9.0 (a maintainer decision, replacing v2's
  two-release phasing) trades the brand-stability bake window for a single
  completion point with an executable acceptance matrix. The accepted risk:
  if Google renames again, we carry one stale-brand dist name — the same
  position we are in today, with the same playbook, and the plumbing
  unaffected. The abort point is before the repo rename (Phase 1 step 1).
- Dual publishing is bounded by the wind-down gate rather than open-ended
  maintainer toil; shim releases are automated in `publish.yml`, not manual.
- Risks accepted: a PEP 541 / trademark challenge on the `gemini-*` names
  (fallback: keep `notebooklm-py` canonical — everything still works);
  scanner noise when the old dist turns shim; `pynotebooklm` adjacency
  confusion (README disambiguates).
- Reversed commitment: the README's July 2026 "keeps the name" note is
  retracted in Phase 0 with rationale, not silently edited.
- "Permanent" for the import package is a governance commitment, not physics:
  it means unscheduled, with a named bar for reopening. A superseding ADR
  requires either (a) the Gemini Notebook brand stable for 2+ years **and** a
  1.0 major already planned on API-stability grounds (the only natural flip
  point), or (b) sustained evidence of real user confusion (recurring issues,
  measurable support burden). Another Google rename instead *confirms* this
  decision — the import name that never chases brands is the only one that
  cannot go stale twice.

## Alternatives considered

- **Full rename including the import package** (v1 of this ADR: alias package
  at 0.9, `git mv src/notebooklm src/gemini_notebook` at 1.0, removals at
  2.0). Rejected on review: the flip forces a semver-unrelated 1.0; `sys.modules`
  aliasing is invisible to mypy and breaks typed consumers; pickles of private
  `notebooklm._types.*` paths break across the flip; the logger namespace (a
  documented API) flips silently; env-var twinning requires write-side
  duplication including a credential scrub (`_auth/refresh.py:361`) where a
  missed twin is a security regression, a quiet-gate that self-recurses, and a
  Click `envvar=` bypass no grep gate can see; and the 2.0 removal cliff
  strands every config `mcp install` ever wrote. Each had a known fix; the sum
  was a large, risky program buying nothing the dist rename doesn't.
- **Two-release phasing** (v2 of this ADR: additive-only 0.9, identity flip
  at 0.10 behind a ≥3-month brand-stability gate). Rejected by maintainer
  review: it left 0.9.0 with no executable definition of done, and shipping
  a real (file-carrying) `notebooklm-py` 0.9.x before the first shim widened
  the dual-install collision window to include users who followed the
  planned rollout. The consolidated 0.9.0 accepts brand risk for a single
  completion point; the technical verification steps (TestPyPI/OIDC
  rehearsal before release) are retained.
- **`GEMINI_NOTEBOOK_*` env-var twins** (subset of v1). Deferred
  indefinitely; any future attempt must solve, from v1's review: non-warning
  resolve path for the quiet gate and diagnostics, write-side dual-export plus
  twinned credential scrub, a custom `click.Option` for `envvar=`, an
  allowlist-based (not grep) CI gate, and the test-suite home-isolation
  fixture (`tests/conftest.py:31-75`).
- **Bare `gemini-notebook` as canonical.** Most official-looking name, highest
  challenge risk; kept as a redirect instead.
- **Hard cutover / publish only the new name.** Breaks every installed user,
  MCP config, and CI pipeline at once; contradicts ADR-0018.
- **Never rename; keywords only.** Cheapest, and the old page's search rank is
  real — but it cedes the project's identity as "NotebookLM" disappears from
  Google's own UI.
- **Shim ships duplicate legacy console scripts** (for pipx/uv-tool
  ergonomics). Rejected in Phase 1 step 4: duplicate `bin/` ownership
  recreates the uninstall-corruption hazard inside every dual-install venv;
  documented `--include-deps` / canonical-dist guidance is the safer trade.
- **`gnb` short CLI alias.** Dropped: PATH collision (srsRAN's `gnb`) and a
  brand-coupled acronym — exactly the churn constraint 1 warns about.
