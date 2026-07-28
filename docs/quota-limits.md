# NotebookLM quota & tier limits (reference)

A human-reference table of NotebookLM's **static, published plan limits** per tier — how many
notebooks, sources, chats, and studio artifacts each subscription level allows.

**What this is NOT:** live per-account *remaining* counts or *reset* timestamps. NotebookLM does
**not** expose those through any known RPC — `GET_USER_SETTINGS` carries only the static plan
limits, not usage counters. See [#1825](https://github.com/teng-lin/notebooklm-py/issues/1825) for
the research trail. This document is therefore **prose reference, not shipped code** (see
[Why this lives in docs](#why-this-lives-in-docs-not-code)).

> ⚠️ **Captured from Google's public pages on the dates noted below. Every table Google publishes is
> headed "Usage Limits (Subject to Change)".** Google restructured consumer tiers as recently as
> May 2026 and has changed *enforced* limits without updating the published tables (see
> [Caveats](#caveats)). Treat these numbers as a dated snapshot, re-verify against the source links
> before relying on a specific value, and prefer the authoritative live signal —
> [`AccountLimits.tier`](#relationship-to-accountlimitstier) — over hard-coding any of this.

## Relationship to `AccountLimits.tier`

`(await client.settings.get_account_limits()).tier` (and the `tier` field in MCP/REST
`server_info(include_account=True)`) returns the subscription tier as an **opaque integer** read
from `GET_USER_SETTINGS` `limits[4]`. This table is what those integers *mean* in quota terms.

| `tier` int | Plan | Live-confirmed? |
|---|---|---|
| `1` | Standard / Free | ✅ yes (source limit 50 matches Google) |
| `2` | Pro | ✅ yes (source limit 300 matches Google) |
| `4` | Plus | decoded from the web pro-badge bundle |
| `3` | Ultra (20 TB) | decoded |
| `6` | Ultra (30 TB) | decoded |
| `5` | "Expanded" | decoded — aligns with the **Workspace "Expanded"** access level below (not a consumer plan, which is why it is absent from Google's consumer page) |

The `tier` int is an **opaque key, not an ordinal** — `4` (Plus) is numerically higher than `2`
(Pro) but a lower plan. Look it up in this table; never compare tier ints with `<`/`>`.

**Two things the tier int alone cannot tell you:**

1. **Consumer vs Workspace vs Enterprise.** Pro (consumer), Higher (Workspace), and Enterprise all
   report `notebook_limit=500` / `source_limit=300`, so the two quota numbers do not disambiguate
   the surface. The three tables below diverge — pick the row that matches how the account was
   provisioned.
2. **The per-source size cap.** `limits[3]` (e.g. `500000`) is the per-source size limit; it matches
   the Enterprise "500,000 words per source" figure. `notebook_limit` = `limits[1]`, `source_limit`
   = `limits[2]`, `tier` = `limits[4]`. See [rpc-reference.md](rpc-reference.md).

---

## Consumer — notebooklm.google.com

Source: [support.google.com/notebooklm/answer/16213268](https://support.google.com/notebooklm/answer/16213268)
· captured **2026-07-09**. Five tiers since May 2026 (Standard, "in Plus" = Google AI Plus, "in Pro"
= Google AI Pro, "in Ultra 20 TB", "in Ultra 30 TB"). *"NotebookLM in Plus" ≠ the retired 2024–25
"NotebookLM Plus" product.*

| Feature | Standard (`1`) | Plus (`4`) | Pro (`2`) | Ultra 20TB (`3`) | Ultra 30TB (`6`) |
|---|---|---|---|---|---|
| Notebooks (per user) | 100 | 200 | 500 | 500 | 500 |
| Sources (per notebook) | 50 | 100 | 300 | 500 | 600 |
| Chats /day | 50 | 200 | 500 | 2,500 | 5,000 |
| Audio Overviews /day | 3 | 6 | 20 | 100 | 200 |
| Video Overviews /day | 3 | 6 | 20 | 100 | 200 |
| — Cinematic Video /day | — | — | 2 | 10 | 20 |
| Reports /day | 10 | 20 | 100 | 500 | 1,000 |
| Flashcards /day | 10 | 20 | 100 | 500 | 1,000 |
| Quizzes /day | 10 | 20 | 100 | 500 | 1,000 |
| Mind Maps /day | 10 | 20 | 100 | 500 | 1,000 |
| Deep Research | **10/month** | 3/day | 20/day | 75/day | 200/day |
| Data Tables | *Limited* | *More* | *High* | *Higher* | *Highest* |
| Infographics | *Limited* | *More* | *High* | *Higher* | *Highest* |
| Slide Decks & Revisions | *Limited* | *More* | *High* | *Higher* | *Highest* |
| Watermark removal | No | No | No | Yes | Yes (Infographics + Slide Decks) |

The column headers show the `tier` int this package returns for each plan. *Italic* cells are
qualitative labels Google publishes instead of numbers — see
[Unquantified features](#unquantified-features).

## Workspace / EDU

Source: [support.google.com/notebooklm/answer/16337734](https://support.google.com/notebooklm/answer/16337734)
· captured **2026-07-09**. Five access levels mapped to editions: **Standard** (Business Starter,
Enterprise Essentials, Frontline, Nonprofits, Education Fundamentals/Standard, …) · **More**
(Education Plus, Teaching & Learning add-on) · **Higher** (Business Standard/Plus, Enterprise
Standard/Plus, Google AI Pro for Education) · **Expanded** (AI Expanded Access add-on) · **Highest**
(AI Ultra Access add-on).

| Feature | Standard | More | Higher | Expanded | Highest |
|---|---|---|---|---|---|
| Notebooks (per user) | 100 | 200 | 500 | 500 | 500 |
| Sources (per notebook) | 50 | 100 | 300 | **400** | 600 |
| Chats /day | 50 | 200 | 500 | **1,000** | 5,000 |
| Audio Overviews /day | 3 | 6 | 20 | **40** | 200 |
| Video Overviews /day | 3 | 6 | 20 | **40** | 200 |
| — Cinematic Video /day | — | — | 2 | **4** | 20 |
| Reports / Flashcards / Quizzes / Mind Maps /day (each) | 10 | 20 | 100 | **200** | 1,000 |
| Deep Research | **10/month** | 3/day | 20/day | **30/day** | 200/day |
| Data Tables / Infographics / Slide Decks | *Limited* | *More* | *Higher* | *Expanded* | *Highest* |
| Watermark removal | No | No | No | Yes | Yes (Infographics + Slide Decks) |

The Workspace ladder deliberately diverges from consumer at the **Expanded** rung (bold): weaker than
consumer Ultra-20TB (400 vs 500 sources, 1K vs 2.5K chats, 40 vs 100 audio/video, 200 vs 500 assets,
30 vs 75 Deep Research). The `tier=5` ("Expanded") the badge bundle exposes lines up with this level.

## Enterprise (Gemini Enterprise / Agentspace)

Source: [docs.cloud.google.com … notebooklm-enterprise/overview](https://docs.cloud.google.com/gemini/enterprise/notebooklm-enterprise/docs/overview)
· page stamped **2026-07-07**.

| Feature | Limit |
|---|---|
| Notebooks | 500 /user |
| Sources | 300 /notebook |
| Source size | **500 MB or 500,000 words per source** (only tier with a documented per-source cap) |
| Chats | 500 /user/day |
| Audio Overviews / Video Overviews / Mind Maps / Reports | 20 /user/day each |
| Slide Decks / Infographics | **15 /user/day each** (the only surface publishing numbers for these) |
| Flashcards · Quizzes · Deep Research · Data Tables · narrated slideshows | no published quota |

---

## Unquantified features

For **Data Tables, Infographics, and Slide Decks & Revisions**, Google publishes qualitative labels
(*Limited / More / High / Higher / Highest*) rather than numbers on consumer and Workspace tiers —
and Wayback snapshots (Nov 2025 → Jul 2026) confirm the numbers **never existed**, they were
qualitative from the day the rows appeared. Best-known values, with an evidence class per cell:

| Feature | Tier | Best-known limit | Evidence |
|---|---|---|---|
| Infographics | Free/Standard | ~3/day | **observed** — community/third-party testing (tenorshare, 2026-05-26); in-app error *"You have reached your daily Infographic limits"*. No official number. |
| Infographics | Plus / Pro / Ultra / all Workspace | unknown | qualitative labels only |
| Infographics | Enterprise | 15/user/day | **official** (cloud docs) |
| Slide Decks & Revisions | consumer / Workspace | unknown | [help page](https://support.google.com/notebooklm/answer/16757456) confirms revision quotas *exist* but publishes no value |
| Slide Decks & Revisions | Enterprise | 15/user/day | **official** (cloud docs) |
| Data Tables | any tier, incl. Enterprise | unknown | no number in any source; the Enterprise table has no Data Tables row at all |
| Narrated slideshows | any tier | — | no separate quota row; subsumed under Slide Decks |

**Evidence classes:** *official* = published by Google · *observed* = community/third-party report or
in-app error text, no official figure · *unknown* = acknowledged-to-exist but unpublished.

---

## Reset & counting semantics

- **Daily quotas reset after 24 hours; monthly quotas after 30 days.** Rolling windows (from first
  use), not calendar-midnight.
- **Notebook** caps are **per-user**; **source** caps are **per-notebook**; all chat/generation
  quotas are **per-user per-day**. *"Sharing a notebook does not change the source limit for any
  collaborator."*
- **Deep Research is the only monthly quota**, and only at Standard/free (10/month) — a
  counterintuitive kink (Standard 10/month vs Plus 3/day) Google publishes on both consumer and
  Workspace pages.
- **First-add auto-generated artifacts** (the report/flashcards/infographic/slide-deck/audio/video
  overview generated when sources are first added) are generated once and **do not count** against
  limits.

## Caveats

- **Dated snapshot.** Consumer tiers were restructured May 2026 (4 tiers → 5, Ultra split into
  20 TB / 30 TB). Re-check the source links before trusting a value.
- **Enforced limits change without doc updates.** On 2025-11-25 NotebookLM temporarily rolled
  Infographics/Slide Decks back to **0 for Free users** and imposed unspecified extra limits on Pro,
  due to demand ([@NotebookLM](https://x.com/NotebookLM/status/1993387297081827346)); access was
  later restored. The published tables did not reflect this.
- **No per-source word/size cap is published for consumer/Workspace** — the "500K words/source"
  figure is documented only for Enterprise. (`limits[3]` ≈ `500000` in the settings vector may be the
  same cap surfaced per-account; unconfirmed.)
- **No sharing/collaborator-count limits** are documented on any tier.

## Why this lives in docs, not code

This package ships the **authoritative live signal** as code — `AccountLimits.tier` (the opaque int
from the quota block). It deliberately does **not** ship this table as constants, because the data is:

1. **Heterogeneous** — a mix of official numbers, community-observed values, and
   acknowledged-but-unknown quotas. `Optional[int]` + provenance could model it, but a typed lookup
   would imply a false authority the source data doesn't have.
2. **Google-owned and volatile** — the tier scheme itself changed in May 2026, and enforced limits
   have changed without the published tables updating. Baking it into code guarantees stale,
   authoritative-looking values.
3. **Not needed at runtime** — the library never enforces these limits; the server surfaces the
   `tier` int and the actual `notebook_limit` / `source_limit` the account reports live. Callers that
   want a human answer look here.

This mirrors the same call made for the tier→label mapping: ship the int, keep the table in docs.

## Sources

- [Consumer limits](https://support.google.com/notebooklm/answer/16213268) (live + 5 Wayback snapshots Nov 2025–Jul 2026)
- [Workspace / school limits](https://support.google.com/notebooklm/answer/16337734) (EN + JA)
- [Enterprise limits](https://docs.cloud.google.com/gemini/enterprise/notebooklm-enterprise/docs/overview)
- [Slide Deck revision help page](https://support.google.com/notebooklm/answer/16757456)
- [Create a notebook — usage-limit note](https://support.google.com/notebooklm/answer/16206563)
- [@NotebookLM rollback, 2025-11-25](https://x.com/NotebookLM/status/1993387297081827346) · [XDA coverage](https://www.xda-developers.com/google-rolls-back-notebooklms-slide-decks-infographics/)
- [Data Tables launch](https://workspaceupdates.googleblog.com/2025/12/transform-sources-structured-data-tables-notebooklm.html) · [blog.google](https://blog.google/technology/google-labs/notebooklm-data-tables/)
- [tenorshare infographic-limit report](https://www.tenorshare.ai/ai-tips/notebooklm-infographic-limit.html)

Research provenance: multi-angle deep-research runs on 2026-07-09 and 2026-07-10 (adversarial
verification; Wayback archaeology; EN + JA locales), recorded in
[#1825](https://github.com/teng-lin/notebooklm-py/issues/1825).
