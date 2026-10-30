# Requirements checklist template

Status: draft for the owner's review. Written 2026-10-10 for the 1.0 requirements-checklist feature (ledger H10, V24; plan M6 item 5).

## What the feature does

1. The owner feeds in a PRD (a project brief with sections such as Problem and Context, Functional Requirements, Performance Benchmarks, Time Constraints, Technology, Other Requirements, Submission and Deliverables).
2. The engine distils it into a requirements checklist **from this template**: one atomic, checkable `- [ ]` line per requirement, grouped under `## ` headings, each tied to the PRD text it came from.
3. A done task ticks one item with evidence (`- [x] ... — evidence: <commit, URL, or Log HH:MM>`). The board shows each deadline's done count.

The checklist file is the user's list. The engine ticks and never adds, removes, rewords or unticks an item without the owner (existing rule, kept).

## Rules for writing an item

- **Atomic.** One verifiable claim. "Build an ingestion pipeline that preserves history, handles retries and handles out-of-order arrival" is three items.
- **Verifiable.** A reader can say yes or no from a commit, URL, test or demo. A vague goal ("acceptable latency") becomes an item that asks for the missing definition ("A latency threshold is defined and justified in the docs").
- **Source-tagged.** Each item ends with `[PRD: <section>]` naming where it came from, so a reader can check the distillation.
- **Hard constraints are their own items.** Required technologies, forbidden things (paid services), a required endpoint and its fields, and a single start command each get an item.
- **Deliverables are items.** Each required deliverable (source code, documentation, demo video, usage log) is an item, so "done" cannot skip one.
- **Nothing invented.** If the PRD does not say it, it is not an item. A PRD sentence that was cut off or ambiguous becomes a `## Open questions` item for the owner, not a guess.

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
- [ ] <ambiguity or cut-off text in the PRD that needs the owner's answer> [PRD: <section>]
```

Section headings are fixed, so the board's grouping and counts are the same for every project. Empty sections are omitted (the board parser drops a heading with no items).

## What the real files show (2026-10-10)

Read from four requirements files in the owner's workspace (counts only; no content copied):

| File | Items | Ticked | With evidence |
|---|---|---|---|
| Final | 78 | 76 | 76 |
| Week 2 | 29 | 24 | 24 |
| Week 3 | 179 | 2 | 2 |
| Checklist (undated) | 38 | 0 | 0 |

- **The parser is not the cause.** Run on all four, the board parser read every checkbox-like line (none dropped). The "silent skip" suspect is ruled out for these files.
- **Ticking worked at 78 and 29 items and collapsed at 179.** The tick rule ticks only when a done task is evidence for *exactly one* item. At 179 fine-grained items, nearly every task could be evidence for several, so nothing ticks and the model names items in its reply instead. This is the leading hypothesis for "ticking didn't happen"; it is not proven.
- **The board shows nothing because every configured deadline is past.** The four deadlines on record are dated 2026-09-20 to 2026-10-02, and the board shows requirements counts only until the deadline passes. A new PRD with no deadline line shows no counts at all. That fits "no counts".
- **Granularity is therefore a template rule**, not just a style choice: items must be coarse enough that one task is evidence for one item, and the template's "atomic" rule needs a ceiling. Open: a target size (for example, a PRD of this length distils to 20 to 40 items) and a rule for splitting an item only when its parts are separately verifiable.
- **Judgement stays with the model, in collaboration with the owner** (the owner, 2026-10-10: "it's gotta be model judgement and collaboration with the user"). Distilling the PRD, deciding that a done task is evidence for an item, and ticking are model judgements made with the owner, not engine rules. The engine only parses and counts the file so the board shows the same numbers every time. This replaces my earlier proposal of a deterministic tick rule.

## Open

- Where the template lives at runtime (engine config or a room's setup) and how a project picks it. The PRD input path (paste, file, or forge issue) is the owner's to choose.
- How the collaboration works. Proposal, not decided: the distillation is proposed to the owner as a board item to adjust before the file is final; a done task that could be evidence for several items, or for none, becomes a board item asking the owner which to tick (or none), instead of today's silent "tick nothing and name it in the reply". Whether the trio gate sits in front of those items or they always go to the owner is for the owner.
- Whether the distillation runs through the model adapter or as a dispatched task as today (`Dispatch authority`).
