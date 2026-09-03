# ADR-0033: `_auth` consolidation policy — sanctioned-merge ceilings and a function-granular write boundary

## Status

Accepted (#2156). Amended by
[ADR-0034](0034-auth-storage-object-model.md), which preserves this record's consolidation safety
boundary while extracting independently owned state and lifecycles from the consolidated facade.

**Amended 2026-08-12 (module-size budget raise):** `MODULE_SIZE_BUDGET` moved 1000 → 1500 under
ADR-0008. This ADR's sanctioned-merge machinery is unaffected in substance, but one mechanical
consequence needed handling: the shrink-lock guarantee below was carried by
`ALLOWLISTED_CEILINGS`, which only holds a module while its pin sits *above* the budget. At 1500
all four sanctioned `_auth` pins fell under it, so dropping them — as the raise's first draft did
— would have silently repealed this ADR's "shrink-locked at its pin" rule and handed 275–410 lines
of unratcheted growth back to already-consolidated modules. The locks therefore moved to a
dedicated `SHRINK_LOCKED_CEILINGS` view loaded by
`tests/_guardrails/test_module_size_ratchet.py` from the regenerable
`tests/fixtures/baselines/module_size.json`, which
carries the same grow/tighten semantics but is deliberately exempt from the
budget-below-every-ceiling invariant. **The guarantee in this ADR is unchanged; only its
enforcement site moved.**

**Amended 2026-09-02 (early compatibility-shim removal):** the six private compatibility shims
created by this consolidation were removed in v0.8.x by explicit maintainer decision, before their
documented next-major removal window. This is an intentional compatibility-policy override for
out-of-tree private importers; no replacement shims were added. The canonical replacements are:

| removed private module | canonical replacement |
|---|---|
| `_auth._browser_cookie_filter` | `_auth.cookie_filter` |
| `_auth.browser_cookie_recovery` | `_auth.psidts_recovery` |
| `_auth.browser_state_validation` | `_browser.browser_capture` |
| `_auth.login_wait_trace` | `_browser.browser_capture` |
| `_auth.storage_transaction` | `_auth.profile_store` |
| `_auth.storage_writer` | `_auth.storage` |

**Amended 2026-09-02 (browser-package relocation):** the browser-acquisition
implementation moved from `_auth` to `_browser` under ADR-0036. The exact
`browser_capture.py` shrink lock moved with the file into the separate browser
module-size projection; it was not dropped or granted headroom. `_auth` now has
no `_browser`/Playwright edge and reaches L3 through the neutral installed-rung
contract in `_auth/recovery_rungs.py`.

**Amended during PR 1.2 (2026-08-08):** decision 1 gains a **third** sanctioned class, *template
adoption*. The effort's plan assumed PR 1.2 would shrink `storage.py`; it does not — converting a
hand-rolled preamble onto a shared template is net-additive in lines, as #2152 already demonstrated
before this ADR was written. With ceilings pinned exactly, the two original classes left no legal
way to finish ADR-0031 Stage 3, so the class was added rather than the measurement bent. See
decision 1 for its narrowing condition (a companion ratchet's exception list must shrink in the same
commit) and the reasoning.

Amends [ADR-0008](0008-cli-services-extraction-pattern.md)'s module-size ratchet for
`src/notebooklm/_auth/` only. Refines [ADR-0029](0029-canonical-storage-writer.md)'s enforcement
granularity without weakening its guarantee. Supersedes-by-deferral
[ADR-0031](0031-credential-tier-auth-model.md)'s Stage 5.

## Context

Before #2156, `_auth` was 27 modules / 12,730 lines (measured 2026-08-07; regenerate with
`python -c "from pathlib import Path; ps=sorted(Path('src/notebooklm/_auth').rglob('*.py')); print(len(ps), sum(len(p.read_text(encoding='utf-8').splitlines()) for p in ps))"`
— the `module_size.json` baseline lists per-module counts over the
budget, which is a different figure). The 2026-08-07 architecture review found the
residual friction has one dominant cause: **ADR-0008's 1000-line cap has been acting as the module
boundary inside `_auth` instead of the seams.** The evidence is in the code's own comments, which
name the budget as the reason a module exists:

- `browser_capture.py` cites ADR-0008 at three separate leaf splits (lines 68, 73, 87);
  `browser_launch_errors.py:8` ("Split out of `browser_capture.py` (ADR-0008 module-size budget)")
  and `login_wait_trace.py:11` ("so the capture core stays under the ADR-0008 module-size budget")
  say it outright.
- `storage_transaction.py:18` — "Split out of `storage_writer.py` to stay under the ADR-0008
  module-size budget."
- `psidts_recovery.validate_with_recovery` (`psidts_recovery.py:975`) is a pass-through **whose body
  is 4 lines**, carrying the comment at `:972-973` — "The browser-extraction validator lives in a
  leaf so this recovery module stays under the repository's 1,000-line module budget"; the
  delegation is a lazy import back to the leaf.
- `storage.py` / `storage_writer.py` / `storage_transaction.py` are **one persistence seam spelled
  as three files**, re-joined at runtime by function-local imports in **both directions across the
  `storage` ↔ `storage_writer` seam** (`storage.py:553`, `storage_writer.py:299`/`:355`) plus the
  transaction template's lazy import back into `storage_writer` (`storage_transaction.py:166`) —
  the lock primitive and the lock policy live in different files, and the template lazily imports
  back from the module it was extracted from. (The module-level edges run writer → storage,
  writer → transaction, and transaction → storage; it is the return paths that had to go lazy.)
- The pressure is still rising: 5 of 27 modules sit within 80 lines of the cap
  (`refresh.py` at exactly **1000**, `storage_writer.py` 981, `psidts_recovery.py` 981,
  `browser_capture.py` 980, `storage.py` 966), a 6th (`cookies.py`, 919) within 85.

A cap is a good gate against *accretion* — obesity arriving one unreviewed function at a time. It is
a bad gate against *consolidation*, because it cannot tell a 2,000-line module that grew from a
2,000-line module that was deliberately assembled from files that were only ever apart to satisfy
the cap. Splitting on line count produces shallow modules: an interface per file, a lazy import per
edge, a patch seam per boundary — the opposite of what depth means (depth is a property of the
*interface*, not of file length).

Two dependent questions fall out of lifting it.

**ADR-0029's enforcement argument.** ADR-0029 makes `storage_writer.py` the single sanctioned home
for `storage_state.json` mutations and calls the boundary enforced *by construction*: the public
`atomic_write_json` rejects storage-state paths at runtime, the canonical writer uses the
module-private `_atomic_io._atomic_write_json_unchecked` bypass, and
`tests/_guardrails/test_storage_writer_boundary.py` pins the bypass's importer set to exactly
`{storage_writer.py}`. That set is a *module*-granular assertion, and it is load-bearing precisely
because the module is small enough that "this module writes storage state" is a real constraint.
After the persistence merge (the step that folds `storage_writer.py` and `storage_transaction.py`
into `storage.py`) the sanctioned importer is a ~2,122-line `storage.py` that
also holds every read path and the CAS/merge math — at which point a module-granular gate says
almost nothing and ADR-0029's by-construction claim degrades to intra-module convention.

**ADR-0031 Stage 5.** Stage 5 committed the acquisition modes (master-token, browser-cookie import,
Playwright login) to becoming self-contained *packages*. Packages were the only consolidation shape
available while the cap stood: a package legalizes any amount of code by spreading it over files.
With the cap lifted, that motivation is gone, and the chosen shape (merged deep modules)
reaches the same seam-narrowing without the package tax.

## Decision

### 1. The cap constrains file size, not where seams go

`MODULE_SIZE_BUDGET` stays global. (It stood at 1000 when this ADR was accepted; raised to
1500 — see the 2026-08-12 amendment in Status. The number is not what this decision turns on:
what matters is that the cap constrains file size and does not dictate where seams go.)
What changes is that a **deliberate
consolidation under `src/notebooklm/_auth/`** may register its merged module at its **measured**
line count, annotated `# sanctioned merge (ADR-0033)` with a one-line note naming the absorbed
modules and the PR.

**Which authored policy set to register in** (the baseline derives the measured maps from these
sets, and the choice is mechanical — compare the measured LOC against `MODULE_SIZE_BUDGET`):

| measured LOC | authored policy | derived baseline view | enforced by |
|---|---|---|---|
| **over** budget | `OVER_BUDGET_EXEMPTIONS` with a durable reason | `ALLOWLISTED_CEILINGS` | `test_allowlisted_modules_do_not_exceed_their_ceiling`, `test_allowlisted_ceilings_ratchet_down`, `test_budget_is_below_every_allowlisted_ceiling` |
| **at or under** budget | `SHRINK_LOCKED_MODULES` | `SHRINK_LOCKED_CEILINGS` | `test_shrink_locked_modules_do_not_exceed_their_pin`, `test_shrink_locked_ceilings_ratchet_down` |

Both carry identical grow/tighten semantics, so the shrink-lock guarantee below is the same either
way. They differ only in the budget invariant: an `ALLOWLISTED_CEILINGS` entry must sit strictly
*above* the budget (a redundant entry is a sign the budget was raised without re-baselining), while
a `SHRINK_LOCKED_CEILINGS` entry sits *below* it by design. A module belongs to exactly one map —
`test_ceiling_sets_are_disjoint_and_not_stale` fails if it is in both. These maps are measured JSON
projections, not policy authoring surfaces; edit the authored sets in
`tests/_baselines/module_size.py`, then regenerate the baseline.

This is the **one** exception to the ratchet's "the allowlist only shrinks and ceilings only
tighten" convention, and it covers three cases:

- a **new** entry for a module that crosses the budget by absorbing a cap-split sibling, **or by
  taking a relocation that shrinks its donor by what it grows the recipient**;
- a **later raise** of an existing entry when a second sanctioned merge lands in the same module.
  `storage.py` takes three over the effort — the persistence merge, the write-time cookie-filter
  relocation, and the account-record relocation — each a fresh annotation; and
- a **raise for template adoption inside an already-sanctioned module**: converting hand-rolled
  logic onto a shared template removes duplication but is *net-additive in lines*, because the call
  site, the `body` closure header, and the explicit success return together cost more than the
  inline preamble they replace. Measured precedent: #2152, which introduced the storage-write
  transaction template and converted three writers, grew its module by 22 lines (959 → 981). Unlike
  the first two cases this class has **no donor**, so it must not be annotated as one — the growth
  is intra-module by construction. The machine-checkable evidence that duplication really was
  removed is the ratchet's own `_UNCONVERTED` list shrinking; a raise under this class is only
  legitimate when that list shrinks in the same commit. ADR-0031 Stage 3's completion (PR 1.2, which
  takes `_UNCONVERTED` to empty) is the first and — with the list now exhausted — the last use of
  this class for `storage.py`.

Entries are pinned at **measured LOC per PR, never pre-registered at an end-state estimate** — the
ratchet's slack check (`test_allowlisted_ceilings_ratchet_down`, or
`test_shrink_locked_ceilings_ratchet_down` for the under-budget map) fails on any ceiling above the
current count, so ceiling and measurement must agree in the same commit. A sanctioned entry is a
*pin*, not a budget.

**What that mechanism does and does not buy — precisely.** The pin prevents *post-entry drift*: once
the merge lands, the module is shrink-locked and cannot re-accrete. It does **not** prevent unrelated
code from riding along *inside* the merge PR itself — the gate compares a line count to a number and
has no notion of why the lines are there, so a contributor who added 300 lines of new feature code
alongside the merge would pin at the inflated count and the gate would pass. Nothing mechanical
distinguishes that from a clean merge. **That protection is review-based only**, which is exactly why
the annotation is mandatory and must name each absorbed path and the PR: it gives the reviewer the
claim to check the diff against. A merge PR must therefore be a **pure move** — new code,
simplifications, and behavior changes belong in separate commits (ideally separate PRs) so the pin
measures the merge and nothing else.

**Ordering rule (the pin is exact, so it leaves zero headroom).** Because a sanctioned entry pins at
the measured count, *any* later growth of that module is red unless it is itself sanctioned. So the
plan's work on a module must be sequenced with that in mind: a module that will keep growing under
the effort takes its pin **after** that growth, or takes the growth in the **same PR** as the
pin. Concretely, `refresh.py` sits at exactly 1000 today, and the plan both folds `headers.py` into
it and later restructures its cold-fallback path — those must land together (or the fold last), not
as two separately pinned steps. Structural *moves* between `_auth` modules under this plan (whole-
module absorption, or a relocation that shrinks the donor by what it grows the recipient), and
template adoptions evidenced by a shrinking `_UNCONVERTED` list, may re-pin under a fresh
annotation; **new** code never may.

The same zero-headroom property is what made the template-adoption class necessary rather than
optional. `storage.py` took its pin in PR 1.1 at the measured 2149; PR 1.2 then completed ADR-0031
Stage 3 inside it, and conversion is additive. With no third class the only ways out would have been
to delete ~34 lines of lock-policy prose to make the number fit — trading documented credential-write
semantics for a line count, the exact perverse incentive this ADR warns about above — or to leave
three writers hand-rolling the lock forever.

Everything else about the ratchet is unchanged and continues to apply to `_auth`:

- the global budget is untouched *by this ADR*, inside `_auth` and out (1000 when accepted,
  1500 since the 2026-08-12 amendment);
- **un-merged `_auth` modules stay under it** — `cookies.py` (919) must not silently grow, and
  neither may any module the plan deliberately does not merge;
- each sanctioned entry is **shrink-locked at its pin** the moment it lands: it may only ratchet
  down thereafter, so a merged module cannot re-accrete under cover of its own merge;
- the stale-entry and budget-below-every-ceiling invariants stand as-is.

Outside `_auth/`, nothing changes: a fat module is still split, not allowlisted.

### 2. ADR-0029's write boundary moves from module to function granularity

Today the `_atomic_io` write-bypass importer set is exactly `{storage_writer.py}`, and the bypass is
imported **under an alias** — `storage_writer.py:93` reads
`from .._atomic_io import _atomic_write_json_unchecked as atomic_write_json`, colliding with the
name of the public primitive the guard is protecting.

When the persistence merge lands, that module-granular assertion is replaced by a **function-granular
one**: an equality-asserted allowlist of the intent-writer function names inside `storage.py`
permitted to reach the bypass. ADR-0029's guarantee is preserved verbatim — "only the canonical
writers write `storage_state.json`" — at the granularity that still makes it true.

The alias is what makes this non-trivial, and the clause must be written accordingly (per the
in-repo AST-gate bypass checklist; `test_cookie_conversion_ratchet.py`'s `_local_bindings` is the
precedent):

- resolve the local binding from the `ImportFrom` **`asname`**, asserting exactly one such import —
  scanning for the original name would miss every real call site, and scanning for the alias
  literal would collide with the public primitive;
- flag bare **`Name`** references and module-level uses, not just `Call` nodes — a reference handed
  to a helper escapes a call-only scan;
- attribute uses by `ast.walk` from each enclosing `FunctionDef`, so calls inside a nested closure
  passed **positionally** as `in_storage_transaction`'s `body` argument (`storage_writer.py:506`,
  `:551`, `:976` — each passes a local `_write` closure, not a `body=` keyword) attribute to the
  **enclosing writer** rather than to no function at all;
- treat a bypass reference that **escapes** its writer — returned, stored on an object, or passed
  to a non-allowlisted callee — as a violation rather than as a call attributed to the enclosing
  function. Today every closure is defined and consumed in the same expression, so this costs
  nothing; it exists so the attribution rule above cannot be turned into a laundering route.

**Honest limit of a static gate.** AST analysis binds what is written, not what is reachable. A
dynamic construct — `globals()["…"]`, `getattr` on the module, a callable handed through a registry —
can reach the bypass without the clause seeing it. Python has no intra-module privacy to fall back
on, so once the bypass lives in `storage.py` the gate is a *review aid over the written code*, not a
sandbox. The module-granular set had the same hole (any function in `storage_writer.py` could always
call it); what changes is that the module is now larger, so the written-code check is doing more of
the work. The runtime rejection below is the part that is genuinely enforced.

The merge should also rename the local alias (e.g. `_write_state_unchecked`) so the gate and human
readers stop sharing a name with the public wrapper.

The runtime rejection is unchanged and still backstops **every module outside `storage.py`**: the
public `atomic_write_json` refuses `storage_state.json` paths, so the function-granular allowlist
governs only the one module that legitimately holds the bypass.

### 3. ADR-0031 Stage 5 is superseded by deferral

Stage 5 ("mode packages") is not scheduled. The cap-lift removes the size pressure that made
packages the only legal consolidation shape, and a merged deep module reaches the same
seam-narrowing with strictly less churn: no shim layer, no re-export surface, no `__init__`
interface to keep in sync with the implementation, no new patch seams for the ~171 `_auth`
monkeypatch sites to chase.

Stage 5 is **deferred, not rejected**. The trigger to revisit it is an **interface** signal, not a
size signal. "Outgrows one module" is deliberately not a line- or export-count threshold — a number
would be arbitrary and would invite gaming — but it must still be *observable*, so revisit when any
of these is true of a merged module:

- its public seam splits cleanly into **two disjoint caller populations** that never use each
  other's names (the package boundary is then already latent in the call graph);
- a caller must import from **more than one place** to use a single acquisition mode end to end;
- the module acquires a **second independent reason to change** on the ADR-0031 tier model — e.g. it
  starts owning both a mint strategy's internals and the profile-persistence contract.

Line count alone is never the trigger; any of the above is.

## Consequences

- The plan's merges become legal as pure structural PRs: each merge PR moves file contents wholesale
  and records one annotated ceiling, with no gate-code change required for the ratchet itself
  (mechanism (b): the authored policy sets carry the exception — `OVER_BUDGET_EXEMPTIONS` with a
  reason when the merged module lands over budget, `SHRINK_LOCKED_MODULES` when it lands under;
  regeneration records the corresponding measured ceiling; see decision 1).
- Re-accretion protection is preserved everywhere it still applies. The modules the plan does *not*
  merge keep the plain budget; the modules it does merge are shrink-locked at their measured pin.
- ADR-0029's boundary stays by-construction, but its assertion is now a name list that must be
  maintained: adding an intent writer to `storage.py` means adding its name to the allowlist, and a
  reviewer who does that carelessly widens the boundary. The old set had one element and needed no
  judgment; the new one has ~7 and needs some. This is a real, accepted downgrade in gate
  ergonomics, taken because the alternative — a module-granular gate over a 2,000-line module —
  asserts nothing.
- **Cost of the cap-lift, honestly:** `storage.py` at ~2,550 lines is harder to navigate than five
  files of 500. The mitigations are labeled sections mirroring the absorbed modules and the
  interface-based package trigger above; the bet is that one deep module with a narrow interface and
  no lazy-import cycles is easier to reason about — for humans and for agents — than three shallow
  ones that call each other through function-local imports.
- **Cost of deferring Stage 5:** ADR-0031's staged sequence loses its terminal shape, so "where does
  a new acquisition mode live?" is answered by ADR-0031's boundary principle (unattended → library,
  human-driven → CLI) plus this record's interface trigger, rather than by a package layout. If a
  fourth acquisition mode arrives, that question gets re-opened — which is the correct time to ask
  it, rather than pre-building packages for modes that do not exist.
- The exception is deliberately narrow, but the three narrowing properties are **not equally
  enforced** and the difference matters to anyone relying on them: the **measured pin** is enforced
  mechanically (the gate's slack and growth arms force ceiling == measured in the same commit); the
  **`_auth/` path scope** and the **`# sanctioned merge (ADR-0033)` annotation** are conventions
  recorded here and enforced by review. A reviewer who waves through an unannotated raise, or an
  annotated raise whose diff is not a pure move, defeats the exception — the gate will not.

## Alternatives considered

- **A directory-scoped budget for `_auth/**` (raise or remove the cap for the whole package).**
  Rejected on two grounds. It needs new path-aware gate code — the ratchet is currently one flat
  `{path: ceiling}` map with no notion of directories, and a second budget knob is a second thing to
  keep coherent with `test_budget_is_below_every_allowlisted_ceiling`. More importantly it
  **permanently forfeits shrink-lock and re-accretion protection for every `_auth` module**,
  including the ones the plan deliberately leaves alone: under a directory budget, `cookies.py`
  could drift from 919 to 1,400 with nothing to say so. Mechanism (b) buys the same legality for the
  merges while leaving every other module exactly as gated as it is today.
- **Leave the cap in force and consolidate via packages (ADR-0031 Stage 5 as written).** Rejected:
  it satisfies the gate without addressing the finding. The seams stay where the cap put them; the
  package adds a shim layer, a re-export surface, and an `__init__` to maintain, and every patch
  seam moves. It buys legality, not depth.
- **Case-by-case ceiling entries with no recorded policy.** Rejected: the ratchet's docstring and
  comments say ceilings only tighten, so the first merge PR would either contradict its own gate's
  documentation or quietly edit it. The exception has to be written down where the next contributor
  reads it, which is why PR 0.1 amends the gate's docstring in the same commit as this ADR.
- **Delete the ratchet for `_auth` and rely on review.** Rejected for the reason ADR-0029 gives for
  not relying on lints alone: the failure mode is silent accretion over many PRs, exactly what
  review is worst at catching.
- **Keep the write boundary module-granular and split `storage.py` to keep it meaningful.**
  Rejected: this is the original problem restated — it makes the gate the module boundary again, and
  the split it forces is the one whose lazy-import re-join this plan exists to remove.

## Related references

- [ADR-0008](0008-cli-services-extraction-pattern.md) — the module-size ratchet this record scopes
  an exception into; the gate is `tests/_guardrails/test_module_size_ratchet.py`.
- [ADR-0029](0029-canonical-storage-writer.md) — the single canonical `storage_state.json` writer
  and its by-construction enforcement argument (`tests/_guardrails/test_storage_writer_boundary.py`).
- [ADR-0031](0031-credential-tier-auth-model.md) — the credential-tier model; Stage 5 is superseded
  by deferral here.
- [ADR-0030](0030-one-recovery-ladder.md) — the recovery ladder; the ladder-sequencing step amends
  it to align cold start's rung order with the documented ladder.
- [ADR-0032](0032-auth-domain-types.md) — the auth domain types whose `ProfileStore` boundary the
  persistence merge realizes as one module.
- [Architecture](../architecture.md) — layered design and the `_auth/` file index, updated by each
  consolidation step as modules merge.
