# ADR-NNNN: <Title>

## Status

One of: `Proposed`, `Proposed — <short explanation>`, `Accepted`, `Accepted (retroactive)`, `Accepted (#PR)`, `Accepted (Sunset = <event>)`, `Superseded — <short explanation>`, `Superseded by ADR-NNNN (#PR)`, `Deprecated`, `Rejected`.

A *sunset clause* records the conditions under which the decision is expected to be revisited (e.g. "Accepted (Sunset = D2 cutover)" means the ADR is in force today but will be re-statused when the named work lands).

## Context

What is the problem? What forces are in play (technical, organizational, historical)? What constraints does the codebase impose that make this decision non-obvious? Cite specific files / line ranges so a future reader can find the artifact the decision shaped.

Keep this section short and load-bearing. Anything that does not help the reader make the same call again is noise.

## Decision

The choice we made, stated as a single declarative sentence (or a tight bulleted list when the decision has multiple inseparable parts). No hedging — the ADR is the place where the team's intent is unambiguous.

## Consequences

What follows from the decision. Include both wanted and unwanted effects: maintenance cost, test patterns, performance, ergonomics, deprecation paths. Be honest about the downsides — an ADR that lists only benefits is a marketing brief, not a decision record.

## Alternatives considered

Each rejected alternative, one short paragraph, explaining *why it was rejected*. The bar is "would a future reader, lacking this ADR, plausibly re-propose this alternative?" If yes, it belongs here.

---

**Authoring notes (delete on copy):**

- Numbering is append-only. Never re-use a retired number.
- Use lowercase-kebab-case filenames: `NNNN-short-title.md`.
- Update `docs/adr/README.md` index when adding or re-statusing an ADR.
- Keep ADRs short (target: ≤ 250 lines). If a decision needs more, it probably needs to split.
