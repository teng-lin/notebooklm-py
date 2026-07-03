# REST server vs MCP server — gap review (2026-07-02)

**Inputs (4 lenses):** 2 native Claude (capability-parity matrix · shared-capability contract
divergence) + Codex CLI (REST security/hardening vs MCP) + agy CLI (unique-to-each inventory). All
read the current `src/notebooklm/{server,mcp}/` tree. Both adapters are thin wrappers over the same
`src/notebooklm/_app/` cores, so a "gap" = an `_app/` capability one adapter exposes and the other
doesn't (or exposes with a materially different contract).

## Headline

The gap is **overwhelmingly one-directional**. The REST server is missing ~11 capability areas the
MCP server has; the MCP server is missing **~zero** real capabilities REST has. On top of that, REST
lags MCP on a whole tier of **hardening** (confirm gates, redaction, resource caps, poll semantics).

- **MCP → REST (things REST lacks):** deep research (entire core), source **content** read,
  `chat_configure`, studio lifecycle (delete/retry/rename/get_prompt), `source_wait`,
  `suggest_prompts`, notebook/source rename, `server_info`, Drive/YouTube/batch source-add.
- **REST → MCP (things MCP lacks):** **none of substance.** REST's dedicated `/notes` CRUD looks
  unique but MCP covers all of it through the cross-type Studio surface (`note_save` upsert +
  `studio_list` read/enumerate with `kind="note"` + `studio_delete`). This is an interface-shape
  difference, not a capability gap. *(Corrects one lens that called MCP a "write-only notes client" —
  `_studio_items.py:95-111` reads `client.notes.list`; `studio_delete` deletes notes.)*

**Bottom line:** REST is **not** a viable substitute for MCP today. It can create notebooks, add
url/text/file sources, run a blocking chat, and generate + download artifacts — but a REST client
**cannot read what a source actually says, cannot run deep research, and cannot tune chat.** It is
also the softer target of the two on security.

---

## A. Capability gaps — MCP has it, REST doesn't

Ranked by user impact. Backing `_app/` core in parens.

| # | Gap (REST-missing) | `_app/` core | Impact | Close cost |
|---|---|---|---|---|
| 1 | **Deep research** (start/status/cancel/import) — no research router mounted at all | `research`, `source_research` | **HIGH** | New `/research` router |
| 2 | **Source content read** — `GET /sources/{id}` returns metadata/status only (`get_or_none`), never the extracted fulltext MCP's `source_read` serves | `source_content` | **HIGH** | New content route |
| 3 | **`chat_configure`** — REST chat is locked to notebook defaults (no goal/persona/length) | `chat.execute_configure` | **MED-HIGH** | New route |
| 4 | **Studio delete** — REST generates artifacts but can never remove them | `artifacts.delete_artifact` | **MED** | New route |
| 5 | **Studio retry** — a failed generation can't be retried in place | `generate_retry` | **MED** | New route |
| 6 | **`source_wait`** — no readiness primitive; REST callers hand-roll polling | `source_wait` | **MED** | New route |
| 7 | **Drive/YouTube/batch source-add** — REST has only url/text/file, one at a time | `source_add` | **MED** | Extend routes/models |
| 8 | **`suggest_prompts`** — no recommended-question affordance | (client namespace) | **LOW-MED** | New route |
| 9 | **`studio_get_prompt`** — can't retrieve the prompt an artifact was generated with | `artifacts.get_artifact_prompt` | **LOW** | New route |
| 10 | **notebook/source/artifact rename** — no PATCH/rename verbs | `notebooks`, `source_mutations`, `artifacts` | **LOW** | Trivial passthrough routes |
| 11 | **`server_info`** — REST has only unauthed `/healthz` liveness, no account/version block | `profile`, `auth_check` | **LOW** | New route |

## B. Capability gaps — REST has it, MCP doesn't

**Effectively none.** REST's `/notes` list/get/put/delete map to MCP's `note_save` + `studio_list`
(`kind="note"`, single-fetch by ref returns `content`) + `studio_delete`. Same underlying
`_app.notes` ops, different URL shape. Not a gap.

---

## C. Contract divergence on shared capabilities (both surfaces expose these, but differently)

Consensus items (2+ lenses) first.

1. **Async generation poll semantics are genuinely incompatible — HIGH.** MCP `studio_status` is
   **stateless**: any `task_id` polls its real status and terminal states (FAILED/REMOVED) come back
   as fields. REST records the id in a **process-local in-memory `PendingRegistry`**
   (`server/_pending.py:18-22`) and maps unknown→404, REMOVED→410, FAILED→409. A real, still-running
   task the process didn't create (or created before a restart) returns **404 on REST but live status
   on MCP**. (`artifacts.py:298-324`) *(Lens B)*

2. **Destructive + outward ops: MCP confirm-gated, REST fires immediately — HIGH.** *(Codex + Lens B
   consensus)* MCP two-steps every delete and gates public-widening / user-grant behind `confirm=`.
   REST: `POST /share/public` makes a notebook public, and `POST /share/users` grants access **and
   emails the user**, each on a single unconfirmed request. Deletes are bare `DELETE` verbs (defensible
   HTTP idiom) but the **outward sharing** ops have no guard at all. (`share.py:74,86,115`)

3. **`notify` default is inverted — MED.** *(Codex + Lens B consensus)* `share_set_user` defaults
   `notify=False` on MCP; REST `POST /share/users` defaults `notify=True` (`share.py:31`) — the same
   grant emails a third party by default on REST, silently on MCP. (REST's `PATCH /users/{email}`
   carries no notify flag at all.)

4. **Output leaks MCP intentionally strips — MED.** *(Lens B)* REST `to_jsonable`s raw results:
   sharing status ships bare integer enums (`access=1`, `permission=3`) instead of MCP's string labels
   (`share.py:71,83,143`); chat ships the `raw_response` debug blob MCP pops (`chat.py:47`). Source
   list ships raw integer status/type codes with no `kind`/`status_label`.

5. **Error contract thinner + a redaction hole — MED.** *(Codex + Lens B)* MCP errors carry
   `retriable` + `hint` and redact home paths / file tokens; REST returns `{category, message}` + HTTP
   status with **no `retriable`, no `hint`, and no home-path redaction** (`server/_errors.py:94-107`).
   A file-upload path error can leak `/home/<user>/…` in a REST body.

6. **Identifiers: MCP resolves name-or-id-prefix, REST is id-only — MED.** *(Lens B)* REST resolvers
   are literal passthroughs (`_passthrough.py:27-31`); a REST caller must hold canonical UUIDs.

7. **Lists: MCP paginated (limit/offset/total/has_more), REST returns the whole collection raw — MED.**
   *(Lens B)* Large notebooks/accounts dump unbounded raw-code payloads over REST.

8. **Note create/update ergonomics inverted — MED.** *(Lens B)* MCP requires title+content on create,
   allows partial update; REST allows an empty-body create (`title="New Note"`, `content=""`) but
   forces a full title+content replacement on `PUT` (`notes.py:35-47,70-79`).

> Verified non-issue: the `source_ids` **None=all vs []=none** footgun is honored correctly on **both**
> sides (`artifacts.py:269` + `_passthrough.py:52` mirror `studio.py:424` + `173`). No divergence.

---

## D. Security / hardening asymmetry (REST lags MCP)

From Codex, all REST-side, all citing an MCP protection REST doesn't replicate:

| Sev | Finding | Where |
|---|---|---|
| HIGH | Public-share + user-grant widen access with no confirm gate (dup of C.2) | `share.py:74,86,115` |
| HIGH | Share invite **emails by default** (`notify=True`) (dup of C.3) | `share.py:31` |
| HIGH | Destructive deletes have no confirm/preview | `notebooks.py:62`, `sources.py:235`, `notes.py:82` |
| MED | Multipart uploads spool to disk before the size cap runs (chunked/no-`Content-Length` bypass) | `app.py:97`, `sources.py:204` |
| MED | Artifact downloads unbounded + cleanup only via background task — no disconnect-safe slot cap like MCP | `artifacts.py:339,353,382` |
| MED | `--token` CLI arg writes the bearer token where `ps` can read it; MCP is env-only on purpose | `__main__.py:107,152` |
| MED | Loopback guard is launcher-only + spoofable `Host` header in-app; running the ASGI app directly on `0.0.0.0` is reachable | `__main__.py:55,157`, `_auth.py:114` |
| LOW | Error redaction lacks MCP's home-path / file-link patterns (dup of C.5) | `_errors.py:94` |
| LOW | Artifact generate silently accepts wrong-kind options and drops mind-map `instructions` | `artifacts.py:264,275` |
| LOW | Upload filename sanitization weaker than MCP's `_safe_upload_name` | `sources.py:57` |

*(Auth token comparison itself: worth confirming `server/_auth.py` uses `compare_digest` — Codex
didn't flag a timing leak, so assume present; verify if hardening this.)*

---

## Recommended priority if REST is to be taken seriously

**Tier 1 (makes REST usable at all):** source content read (A.2), deep research router (A.1),
`chat_configure` (A.3). Without the first, a REST client is blind to its own documents.

**Tier 2 (safety parity):** confirm-gate outward sharing + flip `notify` default (C.2/C.3/D),
port MCP's home-path redaction (C.5/D), add download concurrency cap (D). These are small diffs that
close the sharpest security asymmetries.

**Tier 3 (completeness):** studio delete/retry (A.4/A.5), `source_wait` (A.6), rename verbs (A.10),
pagination (C.7), fix the stateful-poll 404 (C.1 — either document it or back the registry with a real
status probe on cache-miss).

**Cheapest high-value single fix:** flip `notify` default to `False` and confirm-gate `POST
/share/public` — one-line-ish changes that remove two HIGH outward-facing footguns.
