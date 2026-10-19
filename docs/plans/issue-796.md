# Issue 796: arm the suite hold after the segment closes

Read from `origin/develop` at `956a5ef6`. This worktree is `50091f80` (`origin/main`) and does not contain `scripts/engine.py`. Apply the change on develop.

## Problem

`_finish_worker` (`scripts/engine.py`, arm at the `verdict.suite_hold` branch) and the planner `Close` branch call `_arm_suite_hold` (`published: False`) and then `_close_segment`. On a kanban forge, `_close_segment` calls `forge.add_human_hold` and raises `ForgeError` on an error string before `upsert_item` and `end_segment`.

`worker.collect` already isolates that exception (#753), so the segment stays open. `_sync_suite_holds` then calls `reconcile.suite_hold_action`, which raises `ValueError` unless the segment has ended with outcome `human_review`. The handler sets a local human review and does not write the label. The forge never shows the hold.

`Close` runs after `_sync_suite_holds` (`run_pass` syncs, then the action loop). A successful label write still leaves `published: False`, so the next pass treats a human removal as `publish` and puts the label back.

## Rule

```
W := this call's add_human_hold succeeded
S := _sync_suite_holds still runs later this pass
A := suite_hold is stored
P := stored published is True

1. □(A → segment ended)     [arm only after _close_segment returns]
2. □(¬W → ¬A)               [a failed write stores nothing; contrapositive of arming after success]
3. finish: S. □(S → ¬P)     [snapshots are from before the write; published ∧ observed False accepts]
4. Close: ¬S. □(W ↔ P)      [the next pass must see a write this pass will not repeat]
∴ finish arms with published False; Close arms with published = W
```

The hold is stored only after the segment has closed. The finish path lets the later sync mark it published. The Close path records the write itself, because sync already ran.

## Change

`scripts/engine.py` only.

`_close_segment` returns `True` only after `add_human_hold` returns empty. No kanban, or `human_review` false, returns `False`. An error string still raises `ForgeError` before `upsert_item` and `end_segment`.

`_arm_suite_hold` takes `published: bool = False`.

`_finish_worker`, when `verdict.suite_hold`: call `_close_segment`, then `_arm_suite_hold` with the default `published=False`. Any other finish still only closes.

The `Close` branch, when `action.suite_hold`: call `_close_segment`, then arm with `published` equal to that return. Any other `Close` still only closes. The action's outcome is `human_review` (what `completion_verdict` already returns); `suite_hold_action` rejects anything else.

Leave `_sync_suite_holds` where it is. Do not change `suite_hold_action`, `worker.collect`, or `add_human_hold`. On the finish path sync calls `add_human_hold` again; that function returns empty when the label is already present.

## Tests

Add these to `evals/test_engine.py`. Write each one red, then make it pass.

1. `test_a_failed_suite_label_arms_nothing`. Open implement segment, dead worker, `require_green=True`, suite counts `passed=0, failed=1, skipped=0, xfailed=0`, kanban set, `add_human_hold` returns an error. `completion_verdict` reaches the suite hold only when the tree does not already hold (standard workflow, implement still has a next stage, no read-only or unreadable facts). Assert `ended_at` is None, resume has no `suite_hold`, `human_review` is false, and the log has no `suite hold must reference`.

2. `test_a_finished_suite_hold_stays_until_the_label_is_removed`. Same facts through `run_pass`, and `add_human_hold` succeeds. After that pass the segment outcome is `human_review`, `suite_hold.published` is true, and the hold is still present. A pass that still observes the label keeps the hold. A pass that observes `human_review` false accepts it: the hold is gone and the segment outcome is `done`.

3. `test_a_close_suite_hold_records_the_label_it_wrote`. Open segment, `plan` returns `Close` with outcome `human_review`, `human_review` true, `suite_hold` true. Kanban set, `add_human_hold` succeeds. After the pass `published` is true and the label was added once. The next pass, with the row's `human_review` false, accepts the hold and does not call `add_human_hold` again.

These stay green with no edits:

- `test_failed_completion_hold_write_keeps_the_segment_open`
- `test_a_close_that_holds_for_review_writes_the_hold_to_the_forge`
- `test_a_close_whose_hold_write_fails_leaves_the_segment_open`
- `evals/test_worker.py` `test_failed_close_stays_open_logs_once_and_other_collection_and_planning_continue` (the segment stays open; that is the failed write)
- `evals/test_worker.py` `test_suite_hold_survives_forge_errors_and_restart_until_release` (no kanban; sync remains the publisher)

Run `python3 -m unittest evals.test_engine evals.test_worker`, then `python3 -m unittest discover -s evals -p 'test_*.py'`. Report passed, failed, skipped, and xfailed from that full run.

## Risks

Setting `published` true inside `_finish_worker` makes the same pass accept the hold: snapshots are taken before collect, and `published` with `observed is False` is acceptance. Test 2 fails if that lands.

Ending the segment when the label write fails breaks `test_failed_close_stays_open_logs_once_and_other_collection_and_planning_continue`. The raise stays before `end_segment`, which `_close_segment` already does.

A second `add_human_hold` on a successful finish is one extra issue read. Skipping it by setting `published` true early is the false accept above.

## Ownership

- write: `evals/test_engine.py`
- write: `scripts/engine.py`
