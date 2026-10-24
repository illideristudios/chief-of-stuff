# Plan: a needs-input stop names its cause (#203)

## What changes

Every one-shot human-review stop gets one **cause**, chosen by the launcher from evidence it already holds (exit code, result file, tree, row). The cause rides four places: the Log line, the report, the issue comment's first line, and the two displays (Workers page, board card). A closed set, so no worker text can forge one.

| cause | when | carries |
|---|---|---|
| `worker's call` | valid result, `status: human_review`, exit 0, and the launcher did not override it | the worker's stated reason |
| `timeout` | exit 124 when reconcile runs (`_run_worker`'s wait ran out) | exit code; the worker's reason if a result was written, otherwise what was missing |
| `runtime exit` | any other nonzero exit (quota 429, crash, 127 could not start) | exit code, what was missing; the worker's reason too when it wrote one |
| `no result file` | exit 0 but no valid result: missing, unreadable, incomplete | exit code, what was missing |
| `launcher check` | the launcher overrides a `done`/`relaunch`, or the task row changed | the existing reason text |

`launcher check` is a fifth class. The issue names four, and these stops match none of them: a `done` or `relaunch` the launcher overrides (`one_shot.py:359-386`) and the row-changed report (`:395`). Folding them into `worker's call` would label a launcher override as the worker's judgement, which is the field-report bug. The name is the plan's; approving the plan accepts it.

The first exit 124 with no result file never reaches reconcile. `_launch` relaunches it once (`one_shot.py:532-543`, #110) and writes `timed out after N minutes`, which is not a human-review end and gets no class. Reconcile sees 124 only when that retry is already spent, or a result file exists so the retry did not run.

"no result file" covers an invalid file as well (the reason text already says "incomplete or invalid"); the class name stays the issue's wording.

A read-only task whose worker already said `human_review` is `worker's call`. The tree overrides fire only on `status == "done"` (`:359`, `:363`). `test_a_read_only_task_whose_worker_asked_for_review_keeps_its_own_reason` (`test_one_shot.py:1014`) stays on that reason and gains `cause == "worker's call"`.

A quota repeat is not `launcher check`. The stop was already `timeout`, `runtime exit`, or `no result file`; the repeat keeps that class. A worker `relaunch` that the once-a-day cap refuses (`:386`) is `launcher check`.

### Logic

```
w := the worker wrote a valid result with status human_review     x := exit code, 0 = clean
t := x = 124 (wait ran out)    n := x ≠ 0    v := a valid result file exists
o := the launcher overrode a done/relaunch, refused a second non-quota relaunch, or the row changed
     a quota repeat is not o: it restores the class worker_result already chose

P1 [Acceptance, plus launcher overrides] ∀ human-review stop: exactly one of {worker's call, no result file, timeout, runtime exit, launcher check}
P2 [one_shot.py:88]    valid result ∧ n → the stop is already human_review today (reason "worker exited N: …")
P3 [one_shot.py:359-386, :395] o → human_review with no cause among the four
P4 [_run_worker]       t is the only source of 124; the first such exit with no result file relaunches (#110) and is not a stop

cause = launcher check          if o
      = timeout                 if t ∧ ¬o
      = runtime exit            if n ∧ ¬t ∧ ¬o
      = worker's call           if w ∧ ¬n ∧ ¬o
      = no result file          if ¬v ∧ ¬n ∧ ¬o
∴ every human-review stop has exactly one cause; precedence timeout > runtime exit > no result file puts a quota crash (nonzero exit, no result) under runtime exit, not under worker's call
```

Known limit (a code comment): a worker CLI that itself exits 124 reads as `timeout`; upgrade only if `_run_worker` ever reports its status apart from the exit code.

## Order

Tests first, red, then the script that turns them green. `update_tracker(..., cause="")` defaults to empty, so callers that do not pass it (`test_workers_page.py:148`, `test_issue_page.py`, `test_flow_chart.py`) keep today's class-less line.

## Edits, by file

1. `scripts/tracker_log.py`
   - `CAUSES` constant tuple (the five names).
   - `ended_line(..., cause="")`: `HUMAN REVIEW NEEDED (<cause>) — reason…` when a cause is given; a `done` line and a review line with no cause stay byte-for-byte as now.
   - `ENDED` gains an optional group after the status phrase: `(?: \(([^)]+)\))?`. It is group 5. Groups 1–4 do not move, so `m[3]` / `m[4]` readers stay put. `.groups()` grows from 4 to 5; `test_an_ended_line` (`test_tracker_log.py:48`) compares a 4-tuple and must expect `None` in the fifth. Historic lines with no class still match.
   - `review_causes(moves, resolve) -> dict`: `resolve(name) -> today's Task or None`. Walk moves in time order. A review end whose group 5 is set stores that class for the resolved task. A later start, stage move, relaunch, completed end, or class-less review end for that same task clears it. Return only tasks that still have a class.
   - Resolve the way `hold_began` finds today's task (`task_forge.py:80-89`): casefold of **both** the task's name and its item, earlier rows folded onto today's task by name or issue. Do not call `launcher(known)`. `known` mixes earlier `Task` objects with today's, and a flag lookup is by today's object. Do not copy `hold_began`'s skip of launcher moves after the first `stage:` line (`:90`). That skip would drop the review end that follows `stage: name → implement`, which is the normal launch. A stage line's `Move.name` is the task name (`tracker_write.set_stage`); an end move's `Move.name` is the launch item. Those differ when the item is longer than the name (#182). The find map covers both; `Move.name` equality does not.

2. `scripts/one_shot.py`
   - `worker_result` returns `(status, reason, changes, cause)`. `cause` is `""` for `done` and `relaunch`. Otherwise, in order: exit 124 → `timeout`; any other nonzero exit → `runtime exit`; valid `human_review` at exit 0 → `worker's call`; else `no result file`. The reason text keeps its exit code and what was missing (already true at `:75`, `:82`, `:86`, `:88`). A valid result with a nonzero exit is never `worker's call`.
   - `reconcile` saves that cause as `kept` before the quota promotion. Set `cause = "launcher check"` on the `done` overrides (`:362`, `:364`) and on a relaunch refused for changes (`:380`) or for a second non-quota relaunch (`:386`). Promoting a clean quota stop to `relaunch` (`:371`) clears `cause`. The quota-repeat flip (`:384`) restores `kept`. A relaunch that stays a relaunch carries `""`.
   - The row-changed return (`:395`) sets `cause` to `launcher check` and returns. It writes no Log line and no issue comment; do not add either.
   - The adopted prefix (`:402`) changes the reason only. The class stays.
   - Issue comment, when one is posted: first line `Human review needed — <cause>. One-shot worker <name> stopped.` The old opening sentence is replaced. Then the existing `Why:` / `Changes made:` body.
   - `update_tracker(..., cause="")` passes it to `ended_line`. A `relaunch` line takes no cause.
   - Every `report` dict gains `"cause"` (`""` when the status is not `human_review`).

3. `scripts/dispatch_prompt.py:60`: `REPORT_KEYS` gains `"cause"`, so `one_shot_result` prints it. The worker's result schema does not gain the field; the launcher chooses the class. `one_shot_result.clean(None)` is `None`, and TOON writes that as `null` (`NULL_LITERAL`). An older report with no `cause` key prints `null`. `read_report` already accepts an extra key and a missing one (`one_shot_report.py:23`); leave it.

4. `scripts/workers_page.py`: the ended row's `how` cell becomes `human review · <cause>` when `ENDED` carries one. Read `m[5]` only when `m.re is ENDED`; a `RELAUNCH` match has four groups and `m[5]` raises. `need` (`:91`) counts `startswith("human review")`, so a classed row still counts. Escape the cell (`html.escape` turns the apostrophe in `worker's call` into `&#x27;`).

5. `scripts/render_board.py`: build `known` and `flow_moves` before `build_columns` (today they are computed at `:339` and `:331`, after the call at `:328`). Pass `causes` from `review_causes(flow_moves, resolve)` using today's tasks plus earlier rows in `stage_log`. `look_log` stays on `hold_began` only. `build_columns(..., causes=None)`: a held task with a cause flags `NEEDS INPUT · <cause>`, otherwise `NEEDS INPUT`. `columns.Mark` already takes any text and `fragment.esc` escapes it. The flag's CSS uppercases the source; tests assert the source text. Do not edit `columns.py`. Existing `build_columns` callers pass nothing and stay on plain `NEEDS INPUT`. `render` of a tracker whose review lines have no parenthetical stays on plain `NEEDS INPUT`, which is what `test_flow_board.py:571` asserts.

### Skipped

- The issue page's "ended human review" line and the Flow note: not in Acceptance. `issue_page.py:242` reads `w[3]` and `w[4]` only; group 5 does not move them.
- `ARCHITECTURE.md` and `compliance.md`: the architecture agent owns them. The architect should record the cause set, the optional ENDED group, and `REPORT_KEYS`.
- A stored cause per task: nothing persists beyond the Log line; the board re-derives it from the Log each render.

## Tests (`evals/`, unittest)

Land these red before the script edits. Existing `assertIn("HUMAN REVIEW NEEDED"` checks stay green: the class is a parenthetical on that phrase.

- `test_tracker_log.py`: `test_an_ended_line` expects group 5 `None`. Round trip of `ended_line` with each cause. `ENDED` matches an old line (group 5 `None`) and a new one. `review_causes` returns the class. A later stage move whose name is only the task's short name, and a new started line, both clear it. A review end that follows `stage: name → implement` is kept. `ended_line` with no cause stays byte-for-byte, so `test_flow_chart.py` and `test_cases.py:2166` stay green without edits. `test_cases.py:2156` asserts the fixture report's keys; that file has no `cause` and is not regenerated.
- `test_one_shot.py`: the three `worker_result` call sites (`:409`, `:418`, `:427`) unpack four values. New cases: a valid `human_review` at exit 0 is `worker's call` with the worker's reason; exit 124 and no file is `timeout`; exit 3 and no file is `runtime exit` with `exit 3` in the reason; exit 0 and no file, and exit 0 and garbage, are `no result file`; a valid result with exit 1 is `runtime exit` and keeps the worker's reason; `done` with a dirty tree is `launcher check`. One `reconcile` case per class asserts the Log line, the report `cause`, and the comment body's first line (mock as in `test_review_adds_hold_and_comments_with_partial_changes`, `:2004`). `copy_of_a_run_that_never_got_a_result(127)` (`:821`) asserts `runtime exit`. `test_a_second_timed_out_run_is_held_for_review` (`:858`) asserts `timeout`. `test_a_timed_out_run_is_relaunched_once_into_its_tree` (`:824`) asserts no class and no human-review end. The quota-repeat cases (`:1239`, `:1263`) keep their reason and assert `runtime exit` (their `_stopped` default exit is 3 and they write no result). The forged-relaunch cap (`:1315`) asserts `launcher check`.
- `test_workers_page.py`: an ended row with `cause="timeout"` shows `human review · timeout`; an old class-less line still shows `human review`; the "need you" box counts both. Use `timeout` in the HTML fixture so the assertion is not the escaped apostrophe; the log test covers `worker's call`.
- `test_render_board.py`: a held card with a cause flags `NEEDS INPUT · <cause>`; without one, plain `NEEDS INPUT`. A stage line that uses the short name does not clear a review end whose launch item is longer. The `causes=None` default leaves every existing `build_columns` call alone.
- `test_one_shot_result.py`: a report that carries `cause` prints it. Extend `test_a_report_missing_a_key_still_prints_every_key` (`:589`): a file with no `cause` still prints the key, and the value is `null`.

## Risks

- **Log-format coupling.** Writer and readers live in `tracker_log.py` and are tested together. The optional group keeps historic trackers parseable. `moves()` reads Log lines across days, so old and new lines mix.
- **`.groups()` length.** Adding group 5 breaks the one assertion that compares the whole tuple (`test_tracker_log.py:48`). Indices 1–4 do not move, which is why `test_cases.py:2165` and `issue_page.py` stay green.
- **Arity of `worker_result`.** Only `reconcile` and three tests unpack it (grep before editing). `update_tracker`'s new argument defaults to `""`.
- **Quota cause across the relaunch flip.** Clearing `cause` at `:371` drops it unless `kept` was saved. The repeat at `:384` restores `kept`. A first quota stop stays a relaunch and carries `""`.
- **The board's cause is derived, not stored.** It is taken from the same moves the Flow charts read (`stage_log` plus today). An end older than that window shows plain `NEEDS INPUT`. A task whose newest relevant move is not a classed review end shows plain `NEEDS INPUT`. The flag is drawn only on a card `hold_causes` already holds; a review end that leaves the row waiting on the user is held, because `hold_cause` returns that person.
- **The first timeout is not this issue.** Classifying every exit 124 at `_run_worker` would mislabel the #110 retry. The class is applied only to the exit code `reconcile` receives.
- **Row changed.** Report only. Acceptance's comment line applies to the comment `reconcile` actually posts.
- **Flag width.** `no result file` on a `NEEDS INPUT ·` flag, with the flag's letter-spacing, can wrap in a 120px column. `columns.py` is out of scope; wrapping is acceptable.
- **Provisional requirement check.** The issue's four classes are buildable and ordinary; nothing in it is unethical or illegal. The fifth class is this plan's addition, flagged above for the user's yes.

## Proposed file ownership

- write: `scripts/tracker_log.py`, `scripts/one_shot.py`, `scripts/dispatch_prompt.py`, `scripts/workers_page.py`, `scripts/render_board.py`, `evals/test_one_shot.py`, `evals/test_tracker_log.py`, `evals/test_workers_page.py`, `evals/test_render_board.py`, `evals/test_one_shot_result.py`
- read: `scripts/columns.py`, `scripts/task_forge.py`, `scripts/tracker.py`, `scripts/tracker_write.py`, `scripts/one_shot_result.py`, `scripts/one_shot_report.py`, `scripts/issue_page.py`, `scripts/flow.py`, `evals/test_cases.py`, `evals/test_flow_chart.py`, `evals/test_flow_board.py`, `evals/test_issue_page.py`
