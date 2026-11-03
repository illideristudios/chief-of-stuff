# Issue 648 — M7.4 bake

Plan the 1.0 exit: S1–S4 hold for a calendar week of real use by the owner. The milestone text is M7 item 4 in `docs/specs/2026-10-09-chief-of-stuff-1.0-plan.md`. The signs are the design's four goals (`docs/specs/2026-10-09-chief-of-stuff-1.0-design.md`).

Implement on current `develop`. This plan was written from that tree. The planning worktree is still at v0.128.67 and has no `scripts/engine.py`.

## Exit

```
W := the window covers at least 7 calendar days in the workspace zone
U := the store has a segment, log entry, or decision in the window
H := S1 ∧ S2 ∧ S3 ∧ S4

hold        ↔  W ∧ U ∧ H
incomplete  ↔  ¬W ∨ ¬U ∨ the start file is missing
fail        ↔  W ∧ U ∧ the start file exists ∧ ¬H
```

A missing start file or a short window is incomplete, not a pass and not a sign failure. The week is real use of the 1.0.0 build, so the clock starts only after #647 is on `main` and the owner is running that build. #645 and #646 land first; baking beside the old harness mixes its prompts into S1 and S4.

The judge does not close #648. A `hold` line is the evidence. No daily ping: a bake reminder would itself be a routine nudge.

## What this issue does not add

No schema migration, no dashboard, no new notifier, no second copy of the M3–M6 evals, no other-host adapters. Those signs already have evals. This issue only checks that they survived a week of the owner's workspace.

## Judge

New driver `scripts/bake.py`. Pure `judge(...)` over plain records the CLI loads. CLI:

`python3 scripts/bake.py --root <workspace> --start <iso> --end <iso>`

Exit 0 `hold`, 1 `fail`, 2 `incomplete`, plus one line per sign naming the first contradicting record. No forge calls and no writes except reading the store and the start file.

Day boundaries use the workspace zone from `workspace.Config` (`America/Chicago` on the owner's workspace; tests pass a zone in). `W` is `(end in zone).date - (start in zone).date >= 7`. Timestamps that the store already requires to carry an offset compare in that zone.

### S1 — zero routine nudges

An owner-facing event in the window is a decision `decision_actions.needs_owner` still waits on, or a `zach` decision created in the window. It is allowed only when its record is one of:

- a split: `unanimous` is false and `verdicts` is non-empty
- a gate the trio unanimously held: `recommendation == "hold"`
- retries given up: `retry_exhausted`
- a limit or exhausted interrupt already logged by `engine._interrupt` (`limit` / `exhausted`)

Anything else that reached the owner is routine, including a `zach` row written because the trio was missing or `decide` raised. That is the "questions it should not ask" case. Do not treat every `human_review` flag as a nudge: suite holds and `required_human_review` are policy stops. A `human_review` with none of those causes, and no allowed decision on that issue, is routine.

### S2 — one gantt line per issue

`flow_chart.build_from_store` returns one row per included item, and segments for an issue sit on that item's row. Compare full `issue_key` values, not the short `#n` ref (two repos can share a number). Two stored keys that are equal casefolded fail. The primary key already forbids exact duplicates; this catches a renderer that draws a second line.

### S3 — merges land unprompted

For each `reconcile.OP_MERGE` log in the window, fail when that issue has a `decided_by == "zach"` answer at or before the merge whose topic or recorded stage is `merge`, or a `human_review` hold that covered the merge stage and cleared before the merge log. An earlier ambiguity on another stage does not make the merge prompted. If the workspace kanban holds the merge stage, say so in the S3 line: that configuration cannot hold.

### S4 — a restart resumes with no catch-up prompt

`supervisor.record_engine_start(root, at, pid)` appends one JSON object per line to `<workspace>/.chief-of-stuff/engine-starts.jsonl`:

`{"at":"<iso8601 with offset>","pid":<int>}`

`engine.main` calls it once, after `engine.lock` is held, before the pass loop. A handoff `exec` is a new process and so a new line. No rotation. shortcut: append-only with no cap; a one-week file stays small, rotate it if the record outlives the bake.

The first start in the window is the process coming up, not a restart. For each later start `S`, if any open issue has a segment or log before `S` and is still open at `S` (its close, if any, is after `S`):

- an autonomous event exists after `S`: a segment start or end, an `OP_MERGE`, or a `wait:` log (a quota wait is autonomous)
- no new owner-facing event sits strictly between `S` and that event

An owner wait that was already open before `S` is the same decision, not a catch-up prompt.

## Tests first

`evals/test_bake.py` fails before `scripts/bake.py` exists. Fixture records only; no live store and no workspace data.

- exceptions over seven days, with use and a restart that logs a wait before any new ask → `hold`
- a `zach` row with no verdicts → S1 `fail`
- a unanimous non-hold recommendation that still reaches the owner → S1 `fail`
- one item, three segments, one rendered row → S2 holds; two rows for one key → S2 `fail`
- a `zach` merge-stage answer before `OP_MERGE` → S3 `fail`; an ambiguity on `implement` before that merge → S3 holds
- a later start with progress before any new ask → S4 holds; an ask between that start and the next progress → S4 `fail`
- a six-day clean window, a seven-day window with no store activity, and a window with no start file → `incomplete`
- `record_engine_start` appends one JSON line and does not rewrite earlier lines

Register `bake` in the drivers set of `evals/test_architecture.py` and in the Frameworks and Drivers cell of `architecture/ARCHITECTURE.md` in the same change. No `compliance.md` row: a module placed in its circle is not a gap.

## Risks

- Too-wide "allowed" makes a week of real nudges print `hold`. The allow-list is the four record shapes above; do not add "any `zach` row" or "any `human_review`".
- Too-narrow "allowed" fails the week on a split trio, a quota limit, or a red suite. Those are the exceptions the engine already records.
- Starts written only by the build that contains `record_engine_start`. A restart of an older build is invisible, so `hold` is refused when the file is absent.
- S2 sees the store at judgement time. The primary key means a second row could not have been committed in between.
- S3 cannot pass while the kanban map holds `merge` for a human.

## File ownership

| path | mode |
|---|---|
| `scripts/bake.py` | write |
| `evals/test_bake.py` | write |
| `scripts/supervisor.py` | write |
| `scripts/engine.py` | write |
| `evals/test_architecture.py` | write |
| `architecture/ARCHITECTURE.md` | write |
| `scripts/store.py` | read |
| `scripts/flow_chart.py` | read |
| `scripts/decision_actions.py` | read |
| `scripts/reconcile.py` | read |
| `scripts/workspace.py` | read |
| `docs/specs/2026-10-09-chief-of-stuff-1.0-plan.md` | read |
| `docs/specs/2026-10-09-chief-of-stuff-1.0-design.md` | read |
