# Plan: a needs-input stop names its cause (#203)

## What changes

Every one-shot human-review stop gets one **cause**, chosen by the launcher from evidence it already holds (exit code, result file, tree). The cause rides four places: the Log line, the report, the issue comment's first line, and the two displays (Workers page, board card). A closed set, so no worker text can forge one.

| cause | when | carries |
|---|---|---|
| `worker's call` | valid result file, `status: human_review`, exit 0 | the worker's stated reason |
| `timeout` | exit 124 (`_run_worker`'s wait ran out) | exit code, what was missing (result file) |
| `runtime exit` | any other nonzero exit (quota 429, crash, 127 could not start) | exit code, what was missing; the worker's reason too when it wrote one |
| `no result file` | exit 0 but no valid result: missing, unreadable, incomplete | exit code, what was missing |
| `launcher check` | the launcher overrides a `done`/`relaunch` it read: dirty tree, read-only task left changes, relaunch refused, task row changed | the existing reason text |

`launcher check` is a fifth class beyond the issue's four. Acceptance says "each human-review stop", and these stops exist today (`one_shot.py:359-386`, `:395`); leaving them unclassed would leave the field-report gap open for them. If the user wants exactly four, fold them into `worker's call`; that is a one-constant change.

"no result file" covers an invalid file as well (the reason text already says "incomplete or invalid"); the class name stays the issue's wording.

### Logic

```
w := the worker wrote a valid result with status human_review     x := exit code, 0 = clean
t := x = 124 (wait ran out)    n := x ≠ 0    v := a valid result file exists
o := the launcher overrode a done/relaunch the worker reported (tree evidence)

P1 [issue: Acceptance] ∀ human-review stop: exactly one of {worker's call, no result file, timeout, runtime exit}
P2 [one_shot.py:88]    valid result ∧ n → the stop is already human_review today (reason "worker exited N: …")
P3 [one_shot.py:359-386] o → human_review with no cause among the four
P4 [_run_worker]       t is the only source of 124

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
   - `ENDED` gains an optional trailing group: `(...)\b(?: \(([^)]+)\))?`. It is group 5, so `m[3]`/`m[4]` readers do not move. Historic Log lines with no class still match.
   - `review_causes(moves) -> dict[str, str]`: a task key to its class when its **last** launcher move is a review end that names one. A later `stage:` move or a new `started` line clears it. It reads `Move.line` with `ENDED`.
2. `scripts/one_shot.py`
   - `worker_result` returns `(status, reason, changes, cause)`; `cause` is `""` unless the status is `human_review`. Order per the derivation above. The reason text keeps its exit code and what was missing (already true at `:75`, `:82`, `:86`, `:88`).
   - `reconcile`: set `cause = "launcher check"` where it flips to `human_review` (`:362`, `:364`, `:380`, `:386`); the quota-repeat flip (`:384`) keeps the `worker_result` cause. The row-changed report (`:395`) carries `launcher check`. A relaunch result carries no cause.
   - Issue comment first line: `Human review needed — <cause>. One-shot worker <name> stopped.`, then the existing `Why:` / `Changes made:` body.
   - `update_tracker(..., cause)` passes it to `ended_line` (`:300`).
   - The `report` dicts gain `"cause"`.
3. `scripts/dispatch_prompt.py:60`: `REPORT_KEYS` gains `"cause"`, so `one_shot_result` prints it. An older report prints `null` for it.
4. `scripts/workers_page.py`: the ended row's `how` cell becomes `human review · <cause>` when `ENDED` carries one (`m[5]` only when `m.re is ENDED`; `RELAUNCH` has four groups). `need` counts `startswith("human review")`. The cell is escaped.
5. `scripts/render_board.py`: compute `flow_moves` before `build_columns` and pass `causes` (task to class, via `launcher(known)` and `tracker_log.review_causes`, the same row lookup `hold_began` uses). `build_columns(..., causes=None)`: the flag reads `NEEDS INPUT · <cause>` for a held task with a cause, else `NEEDS INPUT`. `columns.Mark` already takes any text.

### Skipped

- The issue page's "ended human review" line and the Flow note: not in Acceptance. One expression each (`w[5]` at `issue_page.py:242`) if wanted.
- `ARCHITECTURE.md` and `compliance.md`: the architecture agent owns them, so the implementer does not edit them. The architect should record the cause set, the optional ENDED group and `REPORT_KEYS`.
- A stored cause per task: nothing persists beyond the Log line; the board re-derives it from the Log each render.

## Tests (`evals/`, unittest)

- `test_one_shot.py`: update the three `worker_result` unpackings (`:409`, `:418`, `:427`) to four values. New: a valid human_review result gives `worker's call` with the worker's reason; exit 124 and no file gives `timeout`; exit 3 and no file gives `runtime exit` with `exit 3` in the reason; exit 0 and no file, and exit 0 and garbage, give `no result file`; a valid result with exit 1 gives `runtime exit` and keeps the worker's reason; done with a dirty tree gives `launcher check`. One `reconcile` case per class asserts the Log line, the report `cause`, and the issue comment body's first line (the comment is mocked as in `:946`-`:1533`). The quota-repeat case keeps its cause.
- `test_tracker_log.py`: round trip of `ended_line` with each cause; `ENDED` matches an old line with no class (group 5 `None`) and a new one; `review_causes` returns the class, and a later stage move or started line clears it.
- `test_workers_page.py`: the ended row shows `human review · <cause>`; an old class-less line still shows `human review`; the "need you" box counts both.
- `test_render_board.py`: a held card with a cause flags `NEEDS INPUT · <cause>`; without one it flags plain `NEEDS INPUT`; the `causes=None` default leaves every existing call alone.
- `test_one_shot_result.py`: `cause` appears in the printed report; an old report without it prints `null` and does not fail.
- `test_cases.py:2165` stays green (group 4 is unchanged).

## Risks

- **Log-format coupling.** Writer and readers live in `tracker_log.py` and are tested together; the optional group keeps historic trackers parseable. `moves()` reads Log lines across days, so old and new lines mix: covered by the class-less test.
- **Arity change** in `worker_result`: only `reconcile` and three tests call it (grep before editing).
- **The board's cause is derived, not stored**: a task whose newest launcher move is not a review end shows plain `NEEDS INPUT`. That is correct by design; stated so a reviewer does not call it a gap.
- **Quota repeat** keeps `runtime exit` or `no result file`; it is the field report's case, so it gets its own assertion.
- **Provisional requirement check.** The issue's four classes are buildable and ordinary; nothing in it is unethical or illegal. The fifth class is this plan's addition, flagged above for the user's yes.

## Proposed file ownership

- write: `scripts/tracker_log.py`, `scripts/one_shot.py`, `scripts/dispatch_prompt.py`, `scripts/workers_page.py`, `scripts/render_board.py`, `evals/test_one_shot.py`, `evals/test_tracker_log.py`, `evals/test_workers_page.py`, `evals/test_render_board.py`, `evals/test_one_shot_result.py`
- read: `scripts/columns.py`, `scripts/task_forge.py`, `scripts/tracker.py`, `scripts/issue_page.py`, `scripts/flow.py`, `evals/test_cases.py`
