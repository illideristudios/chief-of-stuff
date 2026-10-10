# Requirements checklist template

Status: draft for Zach's review. Written 2026-10-10 for the 1.0 requirements-checklist feature (ledger H10, V24; plan M6 item 5).

## What the feature does

1. Zach feeds in a PRD (a project brief with sections such as Problem and Context, Functional Requirements, Performance Benchmarks, Time Constraints, Technology, Other Requirements, Submission and Deliverables).
2. The engine distils it into a requirements checklist **from this template**: one atomic, checkable `- [ ]` line per requirement, grouped under `## ` headings, each tied to the PRD text it came from.
3. A done task ticks one item with evidence (`- [x] ... — evidence: <commit, URL, or Log HH:MM>`). The board shows each deadline's done count.

The checklist file is the user's list. The engine ticks and never adds, removes, rewords or unticks an item without Zach (existing rule, kept).

## Rules for writing an item

- **Atomic.** One verifiable claim. "Build an ingestion pipeline that preserves history, handles retries and handles out-of-order arrival" is three items.
- **Verifiable.** A reader can say yes or no from a commit, URL, test or demo. A vague goal ("acceptable latency") becomes an item that asks for the missing definition ("A latency threshold is defined and justified in the docs").
- **Source-tagged.** Each item ends with `[PRD: <section>]` naming where it came from, so a reader can check the distillation.
- **Hard constraints are their own items.** Required technologies, forbidden things (paid services), a required endpoint and its fields, and a single start command each get an item.
- **Deliverables are items.** Each required deliverable (source code, documentation, demo video, usage log) is an item, so "done" cannot skip one.
- **Nothing invented.** If the PRD does not say it, it is not an item. A PRD sentence that was cut off or ambiguous becomes a `## Open questions` item for Zach, not a guess.

## The file

```markdown
# <Project> requirements

Distilled from <PRD title, version, date>. Tick an item only with evidence.

## Functional
- [ ] <one atomic behaviour> [PRD: Functional Requirements]

## Success criteria and metrics
- [ ] <one measurable outcome> [PRD: Impact Metrics]

## Performance and limits
- [ ] <one threshold, or the item that asks for it to be defined and justified> [PRD: Performance Benchmarks]

## Constraints
- [ ] <time box, required technology, forbidden thing, required endpoint, start command> [PRD: Time Constraints / Technology / Other Requirements]

## Deliverables
- [ ] <one required deliverable> [PRD: Required Deliverables]

## Process and policy
- [ ] <for example: AI usage is documented as the PRD requires> [PRD: Submission and AI Policy]

## Open questions
- [ ] <ambiguity or cut-off text in the PRD that needs Zach's answer> [PRD: <section>]
```

Section headings are fixed, so the board's grouping and counts are the same for every project. Empty sections are omitted (the board parser drops a heading with no items).

## Open

- Where the template lives at runtime (engine config or a room's setup) and how a project picks it. The PRD input path (paste, file, or forge issue) is Zach's to choose.
- Whether the distillation runs through the model adapter as a judgement call with the trio gate on open questions, or as a dispatched task as today (`Dispatch authority`).
