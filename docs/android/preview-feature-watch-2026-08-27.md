# Preview feature watch: Canvas, Lit Review, and current Google signals

**Snapshot date:** 2026-08-27

This note refreshes [#1609](https://github.com/teng-lin/notebooklm-py/issues/1609) and
[#1610](https://github.com/teng-lin/notebooklm-py/issues/1610) against the current GitHub state,
the current NotebookLM/Gemini Notebook web bundle, a live generation probe, and official Google
announcements. Bundle strings are implementation signals, not product promises.

## Executive status

| item | GitHub state | current evidence | recommendation |
|---|---|---|---|
| #1609 Canvas | open; `enhancement`, `do-not-implement-yet` | client scaffolding advanced, but exact variant-5 generation still returns `null` on this cohort | keep open and gated |
| #1610 Lit Review | open; `enhancement`, `do-not-implement-yet` | still a display stub with no wire type, encoder, progress path, or live request to send | keep open and do not implement |

Both issues have no assignee, milestone, or comments. Their last GitHub event was the
`do-not-implement-yet` label on 2026-06-27; neither has a linked closing pull request or commit.

## #1609 — Canvas

The issue was correct when filed: Canvas was already modeled as app-family variant `5`, but its
bundle content-type gate returned zero and a controlled `CreateArtifact` probe returned `null`.

The current bundle has moved forward. Its `canvas` registry record now includes:

- a concrete `canvas` type identifier;
- generation and error text;
- a “Customize Canvas” action;
- a result-data accessor;
- nonzero numeric codes (`316566`, plus a conditional `316565`/`316564` pair); and
- the same interactive/customizable metadata family as Notebook Apps.

That is stronger staging evidence than the June bundle and satisfies the issue's original
*structural* tripwire. It does not satisfy the behavioral tripwire. On 2026-08-27 an exact web
`CreateArtifact` request using the already-proven type-4 shape with variant changed from interactive
mind map `4` to Canvas `5` returned `null`. The disposable notebook and source controls succeeded,
and cleanup completed.

The current practical status is therefore:

```text
bundle/model: wired
account cohort: still disabled
safe library support: not yet
```

The inspected APK contains `APP_TYPE_CANVAS = 5` in its recovered enum but no exposed Canvas UI.
An enum value alone never proves rollout. A direct mobile `CreateArtifact` reconstruction returned
`INVALID_ARGUMENT`; because no real mobile Canvas request exists to compare against, the exact web
probe is the authoritative gate test.

Keep #1609 open. Its description should eventually be updated from “content code is zero” to
“content codes are assigned, but generation remains cohort-gated.” Implement only after a real
artifact ID is returned and a completed Canvas payload can be captured and decoded.

## #1610 — Lit Review

Lit Review has not made the same transition. The current bundle still contains exactly one
`lit_review` registry key and two “Lit Review” labels. Its record has a description and display
metadata, but:

- its content/generation code remains `0`;
- progress and failure strings remain empty;
- it has no concrete artifact type identifier;
- it has no data accessor;
- it has no create encoder or dispatch branch; and
- there is still no valid request shape to probe.

The nonzero display-category value `270541` is shared with the equally unwired `nos_app`/“Web Page”
entry. It is not an artifact wire code and does not make either feature addressable.

Keep #1610 open and `do-not-implement-yet`. The first useful tripwire remains structural: a real
artifact oneof/type, encoder, or create dispatch must appear before a live probe is meaningful.

Google announced a separate experimental product called **Literature Insights**, built with
NotebookLM, at I/O 2026. That product searches scientific literature and presents a structured
table. It is not evidence that the consumer bundle's `lit_review` stub is about to ship; the names
and concepts are adjacent, but no bundle or wire link was found.

## What Google has publicly announced

These are stronger than bundle inference because Google has stated a rollout or release window:

1. **Secure cloud computers and richer generated files.** Google's June research update says
   notebooks can run code for deeper analysis and create downloadable reports, charts,
   spreadsheets, slide decks, images, documents, and structured data. The initial rollout targeted
   AI Ultra and selected Workspace accounts. The July rebrand post says the secure-computer update
   will roll out to all Pro users on the web “over the coming weeks.” See
   [Do better research with NotebookLM](https://blog.google/innovation-and-ai/products/notebooklm/better-research-notebooklm/)
   and
   [NotebookLM is now Gemini Notebook](https://blog.google/innovation-and-ai/products/gemini-notebook/notebooklm-gemini-notebook/).

2. **Cross-product notebooks.** Gemini and Gemini Notebook already synchronize notebooks. Google
   said in July that notebooks would come to AI Mode in Search; an August update now says the
   English rollout is occurring across more than 180 countries, with local-language support to
   follow. See
   [Notebooks in Gemini Apps](https://support.google.com/notebooklm/answer/17003757) and
   [Google Search study tools](https://blog.google/products-and-platforms/products/search/back-to-school-study-tools/).

3. **Study notebooks.** Google announced adaptive lessons, diagnostic quizzes, progress tracking,
   more diagrams and interactive visualizations later in the summer, more standardized tests, and
   mobile support later in the summer. See
   [5 ways to learn with study notebooks in the Gemini app](https://blog.google/innovation-and-ai/products/gemini-app/gemini-study-notebooks/).

4. **Notebook copy.** Google's current help page now documents creating a private copy and an
   “Allow copies” share permission. It says sources and Studio content are copied; notes and chat
   history are not. See
   [Create a notebook in Gemini Notebook](https://support.google.com/gemininotebook/answer/16206563?hl=en).
   A separate FAQ still says duplication is unsupported, so Google's help corpus is temporarily
   inconsistent. The live `CopyProject` test in
   [the mobile organization report](labels-collections-copy-mobile-grpc-2026-08-27.md)
   resolves the backend question independently.

5. **Source labels and Deep Research.** Google's source help already documents automatic/manual
   labels and Deep Research. The direct mobile-backend validation in this directory shows those
   features are not limited to the APK's compiled caller set. See
   [Add or discover sources](https://support.google.com/notebooklm/answer/16215270?hl=en).

6. **More mobile inputs and Studio features.** The current Android help page explicitly says new
   input types will be added over time, and that notes, mind maps, reports, and data tables are not
   yet available in the mobile UI but may be added. This is the clearest official mobile roadmap
   statement found. See
   [Get started with the Gemini Notebook mobile app](https://support.google.com/gemininotebook/answer/16296687?co=GENIE.Platform%3DAndroid&hl=en).

7. **Cinematic Video Overviews are already shipped, not speculative.** Google launched them in
   March for AI Ultra users over 18, in English, on both web and mobile. The current bundle's
   Cinematics quota category is therefore confirmation of an existing gated product, not a future
   feature. See
   [Generate your own Cinematic Video Overviews](https://blog.google/innovation-and-ai/products/notebooklm/generate-your-own-cinematic-video-overviews-in-notebooklm/).

No official Google source found in this check announced consumer Canvas, consumer Lit Review,
Magic View, or the bundle's “Web Page” artifact. Do not describe those as planned launches.

## Bundle-only signals worth watching

The current RPC registry contains several current-family methods absent from `notebooklm-py`'s
public method map. Some support already-shipped UI paths; others may be experiments. Their presence
only proves that code was shipped in the bundle.

| signal | current methods/strings | evidence level |
|---|---|---|
| notebook copy | `CopyProject`, `CopySources[Async]`, `CopyArtifacts[Async]` | shipped and live-tested |
| Canvas | full registry entry, app variant `5` | wired but cohort-gated |
| Lit Review | one display-only registry entry | pre-wire stub |
| Web Page | `nos_app`, “Generate an interactive web page…” | pre-wire display stub |
| Magic View | `GenerateMagicView`, `GetMagicView`; experiment `45701857` still defaults false | real read/generate routes, client gate off |
| Magic Index / knowledge graph | `GetMagicIndex`, `GenerateKnowledgeTree`, `IndexKnowledge` | implementation signal only |
| generated tables | `GenerateTableArtifact` | likely related to announced structured outputs; exact public seam unverified |
| notebook discovery | `SearchNotebooks`, `SearchAllNotebooks`, `ComputeTailwindRecommendations` | implementation signal only |
| notebook images/title cards | `AddCustomNotebookImage[Async]`, `GenerateNotebookTitleCard` | implementation signal only |
| artifact editing | `ActOnArtifactStreamed`, `UpdateArtifactPlan` | consistent with Google's announced post-generation editing; exact availability unverified |
| analytics | `GetProjectAnalytics`, `BatchGetProjectUserActivities` | partly reflected in current help documentation |
| new overview formats | Audio format `5` = “Lecture”; Video format `5` = “Whiteboard Animation” | current enum additions; no official announcement found |
| Cinematics | quota category `17` | already officially shipped for eligible Ultra users on web/mobile |

`Magic View`'s known consumer experiment flag remains declared with default `false` in the current
bundle. Its methods being routable is not enough to expose it safely.

## Repeatable tracking procedure

Check issue metadata without depending on a logged-in GitHub CLI:

```bash
for number in 1609 1610; do
  curl -fsSL \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/teng-lin/notebooklm-py/issues/$number" \
  | jq '{number,title,state,state_reason,updated_at,closed_at,labels:[.labels[].name],comments}'
done
```

Capture and analyze the exact current bundle:

```bash
cd /Users/blackmyth/src/notebooklm-py

uv run scripts/capture_rpc_registry.py \
  --save-bundle /tmp/notebooklm-bundle.js \
  --json > /tmp/notebooklm-rpc-registry.json

jq -r '
  .unmapped
  | to_entries[]
  | select(.value.family == "current")
  | select(.value.method | test("Copy|Magic|Artifact|Knowledge|ProjectAnalytics"))
  | [.key, .value.method]
  | @tsv
' /tmp/notebooklm-rpc-registry.json

rg -o 'lit_review|nos_app|Customize Canvas|Generate an interactive canvas|Generate a Literature Review matrix|Generate an interactive web page' \
  /tmp/notebooklm-bundle.js \
| sort \
| uniq -c
```

For #1609, repeat the behavioral variant-5 probe only in a disposable notebook and delete any
created artifact and notebook. For #1610, do not invent a request: wait for an encoder or artifact
wire type to appear. A `null` Canvas result means the gate is still closed; a real artifact UUID,
followed by a decodable completed payload, is the implementation signal.
