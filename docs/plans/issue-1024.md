# Plan: merge-queue submissions reach the terminal merge stage (#1024)

## Problem

GitHub merge queues accept a merge without closing the pull request immediately. `merge_approved.merge_head()` reports that outcome as `queued` (see `evals/test_merge_approved.py` and `evals/test_engine_merge.py`). `engine_merge.execute()` treats only a `merged …` message as success (`scripts/engine_merge.py:54–55`), so `engine.run_pass()` never schedules the kanban `merge` stage write on that pass.

While the request stays open, the engine correctly keeps planning `Merge` and may call `merge_head()` again (`evals/test_engine.py::test_queued_merge_keeps_stage_and_is_planned_again_next_pass`). That is fine.

The bug appears after the queue finishes: the forge snapshot shows `forge_state=closed` and `merge_state=merged`, but the store stage is still the pre-merge value (typically `review`). `reconcile._item()` returns no action for closed issues except the main-CI `completion_wait` path (`scripts/reconcile.py:414–424`), so nothing ever drives the terminal `merge` label write. Production snapshots often omit closed issues soon after (`evals/test_engine.py::test_merge_stage_write_is_retried_after_closed_issue_disappears`), so the existing `pending_stage` retry loop (`scripts/engine.py:815–825`) never runs.

## Goal

1. Record a successful queue submission as **pending** terminal reconciliation (distinct from an immediate `merged` result).
2. On a later pass, when a snapshot shows the closing request merged, move the item to the terminal `merge` stage (store + forge labels), reusing the same machinery as a synchronous merge.

## Non-goals

- Changing `merge_approved.merge_head()` queue detection or forge merge API behavior.
- Treating `unknown …` merge results as pending (out of scope unless a follow-up issue says otherwise).
- Architecture doc updates (`architecture/` is out of scope for implementers).

## Design

### Separate “queued” from `pending_stage`

`pending_stage` already blocks planning with `HOLD_STAGE_WRITE` while the issue is open (`reconcile.plan` at `scripts/reconcile.py:603–607`). Using it directly on a `queued` response would break the tests that expect repeated `Merge` actions while the PR stays open.

Introduce a resume field, e.g. `merge_pending`:

```python
{"expected": "<stage before merge>", "request": <closing request number>}
```

- Set when `engine_merge.execute()` logs a message starting with `queued ` (GitHub `#N` and GitLab `!N` prefixes already match forge messages).
- Clear when a synchronous `merged` result is recorded (normal path) or when terminal reconciliation completes.
- Do **not** set `pending_stage` until the forge reports `merge_state=merged` (and ideally `forge_state=closed`).

### Promote to `pending_stage` after the forge confirms merge

In `engine.run_pass()`, after `store.sync_from_forge(snapshots)` (same region as the existing `pending_stage` retry loop, ~`scripts/engine.py:765–825`):

For each item with `merge_pending` in resume:

- If `item.merge_state == "merged"` and `item.stage != "merge"`:
  - Set `pending_stage` to `{"stage": "merge", "expected": merge_pending["expected"]}`.
  - Pop `merge_pending`.
- If `item.forge_state == "closed"` and `merge_state != "merged"` and there is no open closing request still `ready`/`queued`, drop `merge_pending` (issue closed without merge — should not happen on the happy path; avoids stale resume).

The existing pre-plan retry that calls `_write_stage()` for `pending_stage` already handles closed issues and empty snapshot lists (`test_merge_stage_write_is_retried_after_closed_issue_disappears`). No new reconcile action type is required.

### Optional hardening in `reconcile._item`

If promotion is always done in the engine before `reconcile.plan()`, `_item` need not change. If we want defense in depth, a narrow branch before `if item.forge_state == "closed": return None` could return `Wait(key, HOLD_STAGE_WRITE)` when `merge_state == "merged"`, `stage != "merge"`, and resume carries `merge_pending` or `pending_stage` targeting `merge`. Prefer engine-only promotion first; add reconcile only if tests show a race.

### Resume merge record on synchronous merge

When `engine_merge.execute()` returns `True`, append to `resume["merges"]` as today and ensure any `merge_pending` for that issue is cleared so a later accidental re-queue does not duplicate work.

## Implementation steps (TDD)

1. **Red — engine integration** (`evals/test_engine.py`):
   - Extend the merge-queue fixture: first pass gets `queued #10` from forge merge; assert `merge_pending` (or chosen key) is stored with `expected: "review"`.
   - Assert `pending_stage` is **absent** and the next pass still plans `Merge` while the issue is open.
   - Simulate queue completion: update fake forge row to `forge_state=closed`, `merge_state=merged`, stage still `review`.
   - Next `run_pass()`: store `stage` becomes `merge`, `pending_stage` cleared, forge `write_stage` records `(…, "merge", "review", …)`.
   - Mirror with GitLab `queued !N` if the resume shape is shared.

2. **Red — unit** (`evals/test_engine_merge.py`):
   - After `execute()` with a mocked `queued` return, assert resume contains `merge_pending` with the current item stage and request number.
   - Assert a `merged` success clears `merge_pending`.

3. **Green — `scripts/engine_merge.py`**:
   - After logging a queued message (non-`OP_MERGE` log line), write `merge_pending` via `store.update_resume`.
   - On merged path, clear `merge_pending`.

4. **Green — `scripts/engine.py`**:
   - Add `_promote_merge_pending(store, item)` (or inline in the existing pre-plan loop) and call it for every item after sync / before the `pending_stage` retry block.

5. **Refactor**: share the `pending_stage = {"stage": "merge", "expected": …}` dict construction with the successful-merge branch in `run_pass()` (~`scripts/engine.py:981–992`) if duplication is obvious.

## Tests to preserve

- `test_a_queued_github_request_is_logged_once_and_returns_false` — log shape unchanged.
- `test_a_merge_that_is_not_confirmed_is_not_logged_as_a_merge` — `OP_MERGE` kind still empty for queued.
- `test_queued_merge_keeps_stage_and_is_planned_again_next_pass` — still plans `Merge` while open.
- `test_merge_stage_write_is_retried_after_closed_issue_disappears` — synchronous merge + failed label write still works.

## Risks

| Risk | Mitigation |
|------|------------|
| `merge_pending` never promoted if snapshots drop closed issues before `merge_state=merged` appears | Promotion runs on the same pass as sync while closed issues remain in the bounded closed window (`forge.snapshots()`); add test with snapshot list retaining the row once. |
| Stale `merge_pending` after manual close without merge | Drop when `closed` and not `merged` after sync. |
| Duplicate `merge` label writes | `_write_stage` is idempotent when already on `merge`; `hold_recovery` drops drifted `pending_stage`. |
| Merge-hold boards (`hold_stages` includes `merge`) | Queued path may set `human_review` on the merge stage write; follow `test_retried_merge_hold_write_cannot_merge_on_the_retry_pass` patterns if promotion interacts with holds. |
| Race: PR merged on forge but labels still `review` | Rely on `pending_stage` retry until `write_stage` succeeds; existing logging for `stage write failed`. |

## Verification

```bash
python3 -m unittest discover -s evals -p 'test_*.py' \
  evals.test_engine_merge evals.test_engine.MergeEngineTest \
  evals.test_reconcile
```

Full suite before marking implement done (workspace `require_green_tests`).

## File ownership (implementation stage)

| Path | Mode |
|------|------|
| `scripts/engine_merge.py` | write |
| `scripts/engine.py` | write |
| `evals/test_engine_merge.py` | write |
| `evals/test_engine.py` | write |

Read-only context: `scripts/reconcile.py`, `scripts/merge_approved.py`, `scripts/forge.py`, `evals/test_merge_approved.py`.
