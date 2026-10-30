# Issue 911 — Task history lists "plan failed" before "plan started"

Plan only. Implementation starts from `origin/develop` (`4417a8fe`). This worktree is `main` at `50091f80`, which has no store-backed task page (`db.segments`). Line numbers below are `origin/develop`.

## Cause

`scripts/issue_page.py` (store-backed branch, `if item is not None and db is not None`, ~295–315) builds `steps_list` as `(datetime, text)` tuples: a `"<stage> started · <who>"` row at `seg.started_at`, a `"<stage> <outcome> · <who>"` row at `seg.ended_at`, then the issue's `db.log` rows. It then renders `sorted(steps_list)`.

Tuples compare by time, then by text. A dispatch that fails at start records `started_at == ended_at`, so the times tie and the text decides. `"plan failed"` sorts before `"plan started"` (`f` < `s`). That is the reported order on kernel-sanders#1, tracker#16, drago#1 and chief-of-stuff #690. The same tie breaks log rows alphabetically against segment rows (`dispatch failed…`, `permission allow…`, then `plan …`), which is why the dispatch line shows above the pair.

It is not minute rounding: `_clock` only formats the time; the sort uses the full stamps.

## Implement

One test first, then one line.

### Test (red first)

`evals/test_m6_store_board.py`, beside `test_workers_only_count_local_today_including_midnight_boundary` (it has the `self.db`, `KEY`, `NOW` fixtures and renders `issue_page.render(12, [], bs.EMPTY, NOW, root=self.root, db=...)`).

1. `test_a_segment_that_starts_and_ends_at_one_instant_lists_started_first`: `record_segment(KEY, "plan", 2, started_at=T, ended_at=T, outcome="failed", runner="plan-2")` and a second one for attempt 3 at a later `T2`. Render the page. Assert `"plan started · plan-2"` appears before `"plan failed · plan-2"`, and the same for `plan-3`. Fails today on the alphabetical tie.
2. Same test, add `db.add_log(KEY, "dispatch failed: …", at=T)` at the same instant. Assert it follows `plan failed · plan-2` (log rows come after the segment rows they report on; the stable order below gives this).

### Change

`scripts/issue_page.py`: `for at, text in sorted(steps_list)` → `sorted(steps_list, key=lambda step: step[0])`. `sorted` is stable, so ties keep append order: each segment's start, then its end, then log rows. No other change. No new dependency.

## Checked and left alone

- Every other `sorted(` over timeline tuples in `scripts/`: none (`git grep "sorted(steps\|sorted(events\|sorted(timeline"` finds only this one).
- Legacy tracker-Log branch (`log = sorted(..., key=lambda m: m.at)`) already sorts by time only. Unchanged.
- Flow/Gantt segments are keyed by (issue, stage, attempt), not text, so they are not affected.
- Segments with `ended_at` earlier than `started_at` (clock skew) still sort by time; out of scope.

## Risks

- Existing tests that assert order of rows sharing one instant could have relied on the alphabetical tie. Run `evals/test_m6_store_board.py`, `evals/test_panel643.py`, `evals/test_issue_page.py`; if one fails, the expectation was the bug.
- Tie order between a segment's end and an unrelated same-instant log row now follows insertion (segments first). Acceptable: the log row never needs to precede its own segment.
- Base-branch mismatch: implement on `origin/develop`, not `main`.

## Logic

```
t := the Task history list sorts by timestamp alone, ties keeping append order
S := a segment's start row is appended before its end row
E := a segment has started_at == ended_at
O := the start row is listed before the end row
A := the current sort compares text on a time tie

1. A                      [scripts/issue_page.py:315, sorted(steps_list) over (datetime, text)]
2. E ∧ A → ¬O             ["plan failed" < "plan started"; observed on four boards]
3. S                      [scripts/issue_page.py:305–307]
4. t ∧ S → O              [sorted is stable]
∴ replacing sorted(steps_list) with sorted(steps_list, key=lambda step: step[0]) gives O, ties included
```
