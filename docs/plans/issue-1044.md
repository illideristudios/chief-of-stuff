# Plan: Issue #1044 — A released implement hold reruns implement when its PR is already open

## Goal & Acceptance Criteria

- **Outcome**: Releasing an `implement` hold whose PR is already open moves the item to `review`, not `implement`.
- **Why**: On 2026-10-24 (`work/issue-932-plan-1`), `implement` attempt 1 opened its PR (`31ae4989`) and was held (`"main gained commits the engine did not merge: 24b7bcf0e16c"`). After the hold was released, the engine reran `implement` repeatedly (attempt 2 failed on DNS; attempts 3 and 4 found the implementation already finished and reported `blocked`; attempt 5 started again) instead of advancing to `review`.
- **Acceptance Criteria**:
  1. **Red eval first**: An item held after `implement` opened its PR goes to `review` when the hold is released.
  2. It does not start another `implement` attempt.
  3. An `implement` run that reports `blocked` because the work is done and the PR is open is not a failed attempt.

### Formal Logic Specification

```text
Variables:
  S = item is in stage "implement" with at least one completed "implement" segment
  H = item is held for human review (item.human_review or key in facts.suite_holds)
  P = open closing PR exists on the item's recorded implement work branch with a head SHA
  C = worktree is clean and pushed (changed is False and key not in unreadable)
  A = unanswered ask exists for the latest implement attempt
  D = latest implement segment outcome is "done"
  B = latest implement segment outcome is "blocked"
  R = latest implement segment was held (outcome == "human_review" or required_human_review)

Premises:
  P1: (S ∧ H) → PlanNoAction
  P2: (S ∧ ¬H ∧ D) → AdvanceToReview
  P3: (S ∧ ¬H ∧ (R ∨ B ∨ PriorImplementSegmentHeldOrDone) ∧ P ∧ C ∧ ¬A) → AdvanceToReview ∧ ¬DispatchImplement
  P4: (S ∧ B ∧ P ∧ C ∧ ¬A) → ¬FailedAttempt
  P5: (S ∧ ¬H ∧ ¬D ∧ ¬(P ∧ C ∧ ¬A)) → PreserveExistingRetryOrBlockBehavior

Trace:
  Case 1 (Released hold after implement opened PR):
    Given: S=T, H=F (hold released), R=T, P=T, C=T, A=F
    By P3: AdvanceToReview (Review(key, 1)) ∧ ¬DispatchImplement
  Case 2 (Implement reports blocked because work is done and PR is open):
    Given: S=T, H=F, B=T, P=T, C=T, A=F
    By P4: ¬FailedAttempt (does not increment failed rounds toward retry_limit or escalate)
    By P3: AdvanceToReview (Review(key, 1)) ∧ ¬DispatchImplement
  Case 3 (Implement held or blocked without an open PR or with dirty/unpushed tree):
    Given: S=T, H=F, D=F, (P=F ∨ C=F ∨ A=T)
    By P5: PreserveExistingRetryOrBlockBehavior

Conclusion:
  Releasing an implement hold when its PR is open and tree is clean/pushed moves the item directly to review without starting another implement attempt, and an implement run reporting blocked under those conditions is never counted as a failed attempt.
```

---

## Root-Cause Analysis

Tracing the `#932` (`work/issue-932-plan-1`) incident through the engine pipeline exposes three interacting gaps:

1. **Post-PR hold leaves segment outcome non-`done` without a release-to-`review` path (`scripts/worker.py`, `scripts/engine.py`, `scripts/reconcile.py`)**:
   - In `worker.collect` (`scripts/worker.py:554-560`), when a non-Claude `implement` worker reports `done` after pushing its branch and opening its PR, `host_adapter.moved_outside_engine(...)` checks whether `main` gained commits outside `engine_merges`. When triggered, `worker.collect` overwrites `outcome = "human_review"`, sets `reason = held`, and calls `store.set_human_review(key, True)`.
   - In `engine._finish_worker` (`scripts/engine.py:663-678`), `reconcile.completion_verdict` is called with `report=outcome` (`"human_review"` rather than `"done"`). Because the worktree is clean and pushed, `verdict.human_review` is `False`, so `held = verdict.human_review or reason.startswith("unreadable worker run:")` evaluates to `False`. `_close_segment` is called with `human_review=False` (failing to set `required_human_review=True` on the segment and skipping `forge.add_human_hold`) and stores `segment.outcome = "human_review"`.
   - When the human hold is later released (`item.human_review` becomes `False`), `reconcile._item` (`scripts/reconcile.py:464-465`) checks `if latest.outcome != "done": return _work(key, stage, ...)`. Because `latest.outcome` is `"human_review"` (not `"done"`), the planner ignores the already-open PR and dispatches `implement` attempt 2.

2. **No open-PR fact in `reconcile.Facts` (`scripts/engine_facts.py`, `scripts/reconcile.py`)**:
   - `reconcile.plan` is pure and cannot query `forge` directly, while `engine_facts.gather` does not currently record whether an `implement` item already has an open closing PR on its recorded work branch (`resume["work_branch"]` or the `implement` run's `branch`).

3. **`implement` reporting `blocked` when work is done and PR is open is counted as a failed attempt (`scripts/reconcile.py`, `scripts/engine.py`, `scripts/engine_facts.py`)**:
   - When a redundant `implement` run enters the worktree, sees the work already committed/pushed and the PR already open, makes no changes (`changed is False`), and reports `blocked`:
     - `engine._finish_worker` closes the segment with `outcome = "blocked"`.
     - `reconcile._work` (`scripts/reconcile.py:276`) computes `rounds = sum(s.outcome not in (*RETRYABLE, "done") for s in past)`, counting `"blocked"` as a failed attempt toward `retry_limit` and either dispatching another `implement` attempt (if an ask was answered in that attempt) or escalating once `rounds >= retry_limit`.

---

## Implementation Plan (Red-Green TDD)

### Phase 1: Write Red Evals First

Add failing tests covering all three acceptance criteria before modifying production code:

1. **`evals/test_reconcile.py`**:
   - `test_released_implement_hold_with_open_pr_moves_to_review`:
     - Item in `implement`, `human_review=False`, with a completed `implement` segment held after opening its PR (`outcome="human_review"` or `required_human_review=True`), `changed=((KEY, False),)`, and `open_prs=(KEY,)`.
     - Assert `reconcile.plan(...)` returns `[reconcile.Review(KEY, 1)]` and never `reconcile.Dispatch(KEY, "implement", ...)`.
     - Also verify the multi-attempt sequence from `#932`: `implement-1` held (`human_review`), `implement-2` failed on DNS (`failed` or `unknown`), with `open_prs=(KEY,)` and `changed=((KEY, False),)` -> returns `[reconcile.Review(KEY, 1)]`.
   - `test_implement_blocked_when_work_is_done_and_pr_is_open_is_not_a_failed_attempt`:
     - Item in `implement` with a segment `outcome="blocked"`, `changed=((KEY, False),)`, `open_prs=(KEY,)`, and `retry_limit=1`.
     - Assert `reconcile.plan(...)` does not count the `blocked` segment as a failed round (never emits `Escalate`), does not dispatch another `implement` attempt, and returns `[reconcile.Review(KEY, 1)]`.
     - Contrast case: when `open_prs=()` or `changed=((KEY, True),)`, the item does not advance to `review`.

2. **`evals/test_engine_facts.py`**:
   - `test_gather_records_open_pr_for_implement_item_with_matching_work_branch`:
     - Item in `implement` with `work_branch: "work/issue-1-plan-1"` (or `runs["implement-1"]["branch"]`) in `resume`.
     - Mock `forge.requests()` returning an open `MergeRequest` closing `KEY` with `branch="work/issue-1-plan-1"` and `head="31ae4989"`.
     - Assert `KEY in facts.open_prs` and `forge.requests` is called at most once per `gather` pass across multiple items.
     - Verify that a PR on a different branch or a `ForgeError` during `requests()` leaves `KEY` out of `facts.open_prs` without failing the pass.

3. **`evals/test_engine.py` & `evals/test_worker.py`**:
   - `test_released_implement_hold_after_moved_outside_engine_moves_to_review`:
     - Simulate `implement-1` reporting `done` with an open PR on `work/issue-1-plan-1` while `moved_outside_engine` returns a hold reason.
     - Verify `_finish_worker` closes the segment with `required_human_review=True` and calls `forge.add_human_hold`.
     - Release the hold (`store.set_human_review(KEY, False)`) and run `engine.tick`; assert the item transitions to `review` (`Review(KEY, 1)`) and does not launch `implement` attempt 2.
   - `test_implement_blocked_with_open_pr_and_clean_tree_is_not_failed_and_moves_to_review`:
     - Simulate an `implement` worker reporting `blocked` (with no unanswered ask) when the worktree is clean and pushed (`changed=False`) and the issue's closing PR is already open on its work branch.
     - Verify `_finish_worker` closes the segment as `"done"` (not a failed attempt) and the subsequent `engine.tick` transitions the item to `review` rather than stopping on `"worker stopped"` or launching another `implement` attempt.

### Phase 2: Gather Open-PR Evidence (`scripts/engine_facts.py`)

1. Add a helper `_implement_branch(data: dict) -> str`:
   - Returns `data.get("work_branch")` if set, else the `branch` of the latest `implement-*` entry in `data.get("runs", {})`, or `""`.
2. Add `_open_implement_prs(forge_port, items, resumes) -> tuple[str, ...]`:
   - Collect `(item.key, branch)` for items where `item.stage == "implement"` and `branch := _implement_branch(resumes.get(item.key, {}))` is non-empty.
   - If none exist or `not hasattr(forge_port, "requests")` or `not hasattr(forge_port, "closing_request")`, return `()`.
   - Call `forge_port.requests()` once inside a `try / except ForgeError` block (if `requests()` returns `None` or raises `ForgeError`, return `()`).
   - For each `(key, branch)`, call `mr = forge_port.closing_request(key, requests=requests)`. Include `key` if `mr is not None and mr.branch == branch and bool(mr.head)`.
3. Pass `open_prs=_open_implement_prs(forge, items, resumes)` when constructing `reconcile.Facts(...)`.
4. In `engine_facts.gather`, when building `reports` for `readiness.why_not`:
   - If the latest segment for `r.issue_key` has `seg.ended_at` and `seg.outcome == "done"`, do not let a `"blocked"` report from that already-completed segment mark the item `"worker stopped"`.

### Phase 3: Planner Transitions & Non-Failure of Done+Open-PR `blocked` Runs (`scripts/reconcile.py`)

1. Extend `Facts` (`scripts/reconcile.py:85-106`):
   - Add `open_prs: tuple[str, ...] = ()`.
2. Add helper `_implement_pr_ready(key: str, stage: str, segs: list, facts: Facts) -> bool`:
   - Returns `True` iff:
     - `stage == "implement"`
     - `key in facts.open_prs`
     - `dict(facts.changed).get(key) is False` and `key not in facts.unreadable`
     - No unanswered ask exists for `key` (`not any(a.issue_key == key and a.answer is None for a in facts.asks)`)
     - At least one completed `implement` segment exists in `segs`.
3. Update `reconcile._item` (`scripts/reconcile.py:461-467`):
   - When `stage == "implement"` and `_implement_pr_ready(key, stage, segs, facts)` is `True` (and the item is not held by `item.human_review` or `facts.suite_holds`), advance via `_gate(key, stage, latest, workflow, facts, retry_limit=retry_limit)` even when `latest.outcome != "done"` (e.g., after a released `human_review` hold, a `blocked` run reporting work is already done, or a transient retry failure after the PR was already opened).
4. Update `reconcile._work` (`scripts/reconcile.py:267-288`):
   - Exclude `implement` segments with `outcome == "blocked"` when `key in facts.open_prs` and `dict(facts.changed).get(key) is False` from `rounds`:
     ```python
     pr_open = stage == "implement" and key in facts.open_prs and dict(facts.changed).get(key) is False
     rounds = sum(
         s.outcome not in (*RETRYABLE, "done") and not (pr_open and s.outcome == "blocked")
         for s in past
     )
     ```
   - If `pr_open` and `past` has a `blocked` or held segment (and no unanswered ask), return `_next(key, stage, workflow, facts, retry_limit=retry_limit)` rather than dispatching `implement` or escalating.

### Phase 4: Worker Closure & Hold Recording (`scripts/engine.py`)

1. In `engine._finish_worker` (`scripts/engine.py:663-678`):
   - Ensure `outcome == "human_review"` from `worker.collect` (such as `host_adapter.moved_outside_engine`) sets `held = True`:
     ```python
     held = outcome == "human_review" or verdict.human_review or reason.startswith("unreadable worker run:")
     ```
     so `_close_segment` records `required_human_review=True` on the segment and invokes `forge.add_human_hold`.
   - When `stage == "implement"`, `outcome == "blocked"`, `not held`, there is no unanswered ask for `(key, stage, attempt)`, `changed is False`, and the issue's closing PR is open on its recorded work branch (`_implement_pr_open(forge, store, key)`):
     - Set `final = "done"` before calling `_close_segment(..., final, human_review=False, ...)`, so an `implement` run that reported `blocked` because the work is already done and the PR is open is recorded as a completed segment rather than a failed attempt.

---

## Verification & Test Plan

1. Run targeted unit tests during red-green iteration:
   - `python -m unittest evals.test_reconcile evals.test_engine_facts evals.test_engine evals.test_worker`
2. Run the full evaluation suite:
   - `python -m unittest discover -s evals -p 'test_*.py'`
   - Confirm `passed > 0`, `failed == 0`, `skipped == 0`, `xfailed == 0`.
3. Verify git status and diff scope before committing.

---

## Risks, Edge Cases & Boundaries

- **Uncommitted or unpushed work**: If an `implement` worktree has uncommitted or unpushed changes (`changed is True` or `changed is None` / `key in facts.unreadable`), `_implement_pr_ready` is `False`. The engine will not advance to `review` with unpushed local changes or an unreadable tree.
- **Wrong-branch or missing-head PRs**: `_open_implement_prs` checks `forge.closing_request(key, requests=requests)` and requires `mr.branch == branch and bool(mr.head)` — matching the exact condition `worker.launch` requires when detaching the `review` worktree at `request.head`.
- **Pending unanswered ask**: If a worker calls `ask` and is genuinely waiting on a human answer (`ask.answer is None`), `_implement_pr_ready` is `False` and `_finish_worker` preserves `blocked`.
- **Single forge read per pass**: `engine_facts.gather` calls `forge.requests()` at most once per pass when needed and degrades gracefully on `ForgeError` without aborting fact gathering.
- **Architecture boundaries**: No changes to `architecture/ARCHITECTURE.md` or `architecture/compliance.md`.

---

## Proposed File Ownership (`implement` stage)

| Path | Mode | Purpose |
| --- | --- | --- |
| `scripts/reconcile.py` | `write` | Add `open_prs` to `Facts`; advance released/completed `implement` items with an open PR and clean/pushed tree to `review`; do not count `blocked` with open PR as a failed attempt |
| `scripts/engine_facts.py` | `write` | Gather `open_prs` for `implement` items from `forge.requests()` / `forge.closing_request()`; avoid `"worker stopped"` on segments closed as `"done"` |
| `scripts/engine.py` | `write` | Treat `outcome == "human_review"` as `held` in `_finish_worker`; close `implement` runs reporting `blocked` when work is done and PR is open as `"done"` |
| `evals/test_reconcile.py` | `write` | Unit tests for released `implement` hold -> `review` and `blocked` with open PR not counting as a failed attempt |
| `evals/test_engine_facts.py` | `write` | Unit tests for gathering `open_prs` and completed-segment report filtering |
| `evals/test_engine.py` | `write` | End-to-end engine tick tests for released `moved_outside_engine` hold and `blocked` with open PR |
| `scripts/worker.py` | `read` | Reference for `worker.collect` (`moved_outside_engine`) and `review` launch PR checks |
| `scripts/readiness.py` | `read` | Reference for `why_not` report and ask readiness rules |
| `scripts/forge.py` | `read` | Reference for `closing_request` and `requests` contract |
| `docs/plans/issue-1044.md` | `read` | Implementation plan reference |
