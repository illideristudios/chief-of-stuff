# Plan: a needs-input stop names its cause (#203)

## What changes

Every one-shot human-review stop gets one **cause**, chosen by the launcher from evidence it already holds (exit code, result file, tree). The cause rides four places: the Log line, the report, the issue comment's first line, and the two displays (Workers page, board card). A closed set, so no worker text can forge one.

| cause | when | carries |
|---|---|---|
| `worker's call` | valid result file, `status: human_review`, exit 0 | the worker's stated reason |
| `timeout` | exit 124 when reconcile runs (`_run_worker`'s wait ran out) | exit code; the worker's reason if a result was written, otherwise what was missing |
| `runtime exit` | any other nonzero exit (quota 429, crash, 127 could not start) | exit code, what was missing; the worker's reason too when it wrote one |
| `no result file` | exit 0 but no valid result: missing, unreadable, incomplete | exit code, what was missing |
| `launcher check` | the launcher overrides a `done`/`relaunch` it read: dirty tree, read-only task left changes, relaunch refused, task row changed | the existing reason text |

`launcher check` is a fifth class. The issue names four, and these stops match none of them: a `done` or `relaunch` the launcher overrides (`one_shot.py:359-386`) and the row-changed report (`:395`). Folding them into `worker's call` would label a launcher override as the worker's judgement, which is the field-report bug. The name is the plan's; approving the plan accepts it.

The first exit 124 with no result file never reaches reconcile. `_launch` relaunches it once (`one_shot.py:532-543`, #110) and writes `timed out after N minutes`, which is not a human-review end and gets no class. Reconcile sees 124 only when that retry is already spent, or a result file exists so the retry did not run.

"no result file" covers an invalid file as well (the reason text already says "incomplete or invalid"); the class name stays the issue's wording.

### Logic

```
w := the worker wrote a valid result with status human_review     x := exit code, 0 = clean
t := x = 124 (wait ran out)    n := x ≠ 0    v := a valid result file exists
o := the launcher overrode a done/relaunch the worker reported (tree evidence)

P1 [Acceptance, plus launcher overrides] ∀ human-review stop: exactly one of {worker's call, no result file, timeout, runtime exit, launcher check}
P2 [one_shot.py:88]    valid result ∧ n → the stop is already human_review today (reason "worker exited N: …")
P3 [one_shot.py:359-386] o → human_review with no cause among the four
P4 [_run_worker]       t is the only source of 124; the first such exit with no result file relaunches (#110) and is not a stop

cause = launcher check          if o
      = timeout                 if t ∧ ¬o
      = runtime exit            if n ∧ ¬t ∧ ¬o
      = worker's call           if w ∧ ¬n ∧ ¬o
      = no result file          if ¬v ∧ ¬n ∧ ¬o
∴ every human-review stop has exactly one cause; precedence timeout > runtime exit > no result file puts a quota crash (nonzero exit, no result) under runtime exit, not under worker's call
```

Known limit (a code comment): a worker CLI that itself exits 124 reads as `timeout`; upgrade only if `_run_worker` ever reports its status apart from the exit code.

## Edits, by file

1. `scripts/tracker_log.py`
   - `CAUSES` constant tuple (the five names).
   - `ended_line(..., cause="")`: `HUMAN REVIEW NEEDED (<cause>) — reason…` when a cause is given; a `done` line and every old call are unchanged.
   - `ENDED` gains an optional trailing group on the pattern it already has: `(?: \(([^)]+)\))?`. It is group 5, so `m[3]`/`m[4]` readers do not move. Historic Log lines with no class still match.
   - `review_causes(moves, row_of) -> dict`: a task to its class when its **last** launcher move is a review end that names one. A later `stage:` move or a new `started` line for that same task clears it. It reads `Move.line` with `ENDED`. A stage line names the task's name (`tracker_write.set_stage`); an end move's `Move.name` is the launch item. Those strings differ when the item is longer than the name (#182), so the clear goes through `row_of` (`launcher`, the lookup `hold_began` uses), not `Move.name` equality.
2. `scripts/one_shot.py`
   - `worker_result` returns `(status, reason, changes, cause)`; `cause` is `""` unless the status is `human_review`. Order per the derivation above. The reason text keeps its exit code and what was missing (already true at `:75`, `:82`, `:86`, `:88`).
   - `reconcile`: set `cause = "launcher check"` where it flips to `human_review` (`:362`, `:364`, `:380`, `:386`); the quota-repeat flip (`:384`) keeps the `worker_result` cause. The row-changed report (`:395`) carries `launcher check`. When quota promotes the stop to `relaunch` (`:371`), clear `cause`: a relaunch result carries none.
   - Issue comment first line: `Human review needed — <cause>. One-shot worker <name> stopped.`, then the existing `Why:` / `Changes made:` body.
   - `update_tracker(..., cause)` passes it to `ended_line` (`:300`).
   - The `report` dicts gain `"cause"`.
3. `scripts/dispatch_prompt.py:60`: `REPORT_KEYS` gains `"cause"`, so `one_shot_result` prints it. An older report prints `null` for it.
4. `scripts/workers_page.py`: the ended row's `how` cell becomes `human review · <cause>` when `ENDED` carries one (`m[5]` only when `m.re is ENDED`; `RELAUNCH` has four groups). `need` counts `startswith("human review")`. The cell is escaped.
5. `scripts/render_board.py`: `render` computes `flow_moves` after `build_columns` (`:328`, `:331`); move that computation above the call and pass `causes` (task to class, via `launcher(known)` and `tracker_log.review_causes`). `build_columns(..., causes=None)`: the flag reads `NEEDS INPUT · <cause>` for a held task with a cause, else `NEEDS INPUT`. `columns.Mark` already takes any text. Existing `build_columns` callers pass nothing and stay on plain `NEEDS INPUT`.

### Skipped

- The issue page's "ended human review" line and the Flow note: not in Acceptance. One expression each (`w[5]` at `issue_page.py:242`) if wanted.
- `ARCHITECTURE.md` and `compliance.md`: the architecture agent owns them, so the implementer does not edit them. The architect should record the cause set, the optional ENDED group and `REPORT_KEYS`.
- A stored cause per task: nothing persists beyond the Log line; the board re-derives it from the Log each render.

## Tests (`evals/`, unittest)

- `test_one_shot.py`: update the three `worker_result` call sites (`:409`, `:418`, `:427`) to four values. New: a valid human_review result gives `worker's call` with the worker's reason; exit 124 and no file gives `timeout`; exit 3 and no file gives `runtime exit` with `exit 3` in the reason; exit 0 and no file, and exit 0 and garbage, give `no result file`; a valid result with exit 1 gives `runtime exit` and keeps the worker's reason; done with a dirty tree gives `launcher check`. One `reconcile` case per class asserts the Log line, the report `cause`, and the issue comment body's first line (mock as in `test_review_adds_hold_and_comments_with_partial_changes`, ~`:2004`). Existing `assertIn("HUMAN REVIEW NEEDED"` checks stay green: the class is a parenthetical on that phrase. The quota-repeat cases (`:1250` and below) keep their reason and gain a `cause` assertion. A first-timeout relaunch (`#110`) asserts no class and no human-review end.
- `test_tracker_log.py`: round trip of `ended_line` with each cause; `ENDED` matches an old line with no class (group 5 `None`) and a new one; `review_causes` returns the class. A later stage move whose name is only the task's short name, and a new started line, both clear it. `ended_line` with no cause stays byte-for-byte as now, so `test_flow_chart.py` and `test_cases.py:2166` stay green without edits.
- `test_workers_page.py`: the ended row shows `human review · <cause>`; an old class-less line still shows `human review`; the "need you" box counts both.
- `test_render_board.py`: a held card with a cause flags `NEEDS INPUT · <cause>`; without one it flags plain `NEEDS INPUT`; the `causes=None` default leaves every existing call alone.
- `test_one_shot_result.py`: `cause` appears in the printed report; an old report without it prints `null` and does not fail.
- `test_cases.py:2165` stays green (group 4 is unchanged).

## Risks

- **Log-format coupling.** Writer and readers live in `tracker_log.py` and are tested together; the optional group keeps historic trackers parseable. `moves()` reads Log lines across days, so old and new lines mix: covered by the class-less test.
- **Arity change** in `worker_result`: only `reconcile` and three tests call it (grep before editing).
- **The board's cause is derived, not stored**: a task whose newest launcher move is not a review end shows plain `NEEDS INPUT`. That is correct by design; stated so a reviewer does not call it a gap. Clearing that cause has to go through `launcher`, because a stage line and a launch item are not the same string (#182).
- **The first timeout is not this issue.** Classifying every exit 124 at `_run_worker` would mislabel the #110 retry. The class is applied only to the exit code `reconcile` receives.
- **Quota repeat** keeps `runtime exit` or `no result file`; it is the field report's case, so it gets its own assertion.
- **Provisional requirement check.** The issue's four classes are buildable and ordinary; nothing in it is unethical or illegal. The fifth class is this plan's addition, flagged above for the user's yes.

## Proposed file ownership

- write: `scripts/tracker_log.py`, `scripts/one_shot.py`, `scripts/dispatch_prompt.py`, `scripts/workers_page.py`, `scripts/render_board.py`, `evals/test_one_shot.py`, `evals/test_tracker_log.py`, `evals/test_workers_page.py`, `evals/test_render_board.py`, `evals/test_one_shot_result.py`
- read: `scripts/columns.py`, `scripts/task_forge.py`, `scripts/tracker.py`, `scripts/tracker_write.py`, `scripts/one_shot_result.py`, `scripts/issue_page.py`, `scripts/flow.py`, `evals/test_cases.py`, `evals/test_flow_chart.py`
