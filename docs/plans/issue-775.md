# Issue 775 — Decision gate hardening

Plan only. Implementation starts from `origin/develop` (engine at `956a5ef6`). This worktree is `main` at `50091f80`, which has no `scripts/engine.py`. Line numbers below are `origin/develop`.

Each behavior starts as a failing test, then the smallest change that makes that test pass. Do not re-fix a case the test already proves.

## Already closed

Quota-reset cleanup no longer requires `interrupt.stage == item.stage`. `evals/test_engine.py` `test_quota_reset_clears_a_limit_stamped_with_the_planned_stage` is on develop (`2697d8ef`). Leave it.

A completed restart with no model, and no quota/limit block, already records an unavailable-consultation row (`trio_gate.decide` when `model is None`) and the trailing `_link_replacement_asks` can create the ask. Add a test that this stays true. Change code only if that test is red.

## Implement

### 1. Expired quota hold with no `quota_percent`

`scripts/engine.py` clears a `reached quota limit` interrupt only inside `if quota and retry_at <= now and "quota_percent" in data`. A resume that already lost `quota_percent` keeps the interrupt forever (the stale hold this issue was filed for).

Test: resume has that interrupt (stage not `item.stage`), `quota.retry_at` in the past, and no `quota_percent`. After one pass the interrupt is gone. A `reached spend limit` interrupt beside the same expired quota stays.

Change: when `retry_at` has passed, drop `quota_percent` if present and drop only a `("limit", "reached quota limit")` interrupt. Write resume only if one of those was removed.

### 2. Answered ask with no decision row, and a human answer during consult

`decision_actions.answer_ask` returns `None` when no decision row exists, so `reverse` cannot see a valid answer that landed before the first consultation.

`issue_gate.start` can `store.answer_ask` while `_route_asks` is inside `_choice`. `trio_gate.record` then keeps the trio row (`human_answer` looks at decision rows, not asks). `engine.py` `store.answer_ask` of the trio choice raises `KeyError` on a different answer and is outside the per-action `try`. The ask and the decision row stay different, and the pass dies before `_settle_asks`.

Tests:

- Answer a valid ask, run a pass with no model call, then `reverse` that question. It reverses. An answer outside the options still does not create a row (`store.answer_ask` already refuses it).
- Ask is unanswered; during the trio call, answer it with the other option. The pass returns. The ask keeps the human answer. The latest decision row is that answer, `decided_by == "user"`. The same human answer as the trio does not add a second row.

Change, in `decision_actions.answer_ask`: if the ask has an answer and no row, return a user `DecisionUpdate` for `("ask", question, f"ask-{id}")` with the ask's options, context, stage, and attempt. If a non-user row has a different answer, return a user update with the ask's answer. A user row, a reversed row, and an already-matching answer still return `None`.

Change, in `engine._route_asks`: catch `KeyError` from `store.answer_ask` (the trio write and the copy onto the ask). Then run `_settle_asks` so the new update is recorded. Do not catch `ValueError`.

### 3. Blocked restart has no replacement ask

`_route_asks` `break`s out of the replacement loop when `_consultation_blocked` is true, so `_choice` never runs. The trailing link still runs, and `replacement_ask` returns `None` while the latest row is reversed or restarted. Quota backoff is this case: it blocks consultation and clears a limit interrupt, so the owner gets neither an ask nor an escalation.

Test: a reversed decision, its restart segment `done`, and `quota.retry_at` in the future. One pass makes no model call, leaves an ask whose question and options are the original decision's, and does not launch. A second pass does not add another ask or another decision row.

Change: on that block, call `_choice` with `model=None` (the existing unavailable-consultation row; no model spend), then `continue`. Do not `break`. The existing trailing `_link_replacement_asks` creates the ask. A later pass sees that row in `_settled` and does not consult.

### 4. Claim rejected because the segment exists

`slots.claim` raises `ValueError` before `launch` when `(stage, attempt)` already exists. The restart rollback pops `pending_stage` only when no matching segment exists, so it treats the old segment as proof this claim worked. `_route_asks` then skips the issue.

`test_failed_reversal_claim_does_not_leave_a_pending_stage` mocks `claim` with no segment. Keep it. Add a test that records the segment first and lets real `claim` raise. `pending_stage` is absent, the stage is not written, and a launch failure still keeps `_fail_start`'s pending stage (`launch_failed`).

Change: remember whether that segment existed before `claim`. Pop the restart route when `restart and not launch_failed` and the segment was already there, or is absent now. Do not pop a `release_hold` route.

### 5. Retry entries keep full context

`_choice` stores `trio_retries` under `json.dumps([topic, question, instance, stage, attempt, context, options])` and drops the entry only when that same key is consulted again. A direct answer never drops it, so the resume blob keeps the context.

The early return does not block a settled or reversed row: it requires `retryable_failure` and a blank answer. Do not add a reversal-backoff path.

Test: a retryable failure stores a retry; the value's key is a fixed-size token (hash), not the context text. Answering the ask and running a pass removes that entry. A different question's entry stays.

Change: key by topic, question, instance, stage, attempt, and `sha256` of the context and of the options (`hashlib` is already imported in `engine.py`). When `_settle_asks` records a user answer, drop every `trio_retries` entry for that topic, question, and instance, including a legacy key that still holds raw context.

### 6. `Escalate.stage` and `Limit.stage` are not part of equality

Both fields are `compare=False` (`scripts/reconcile.py`). `assertEqual` on those actions passes when the stage the interrupt is stamped with is wrong. Production does not compare these actions.

Change: remove `compare=False` from those two fields only. Leave `Ask`'s ignored fields alone. Update every `Escalate(...)` / `Limit(...)` expected value in `evals/test_engine.py` and `evals/test_reconcile.py` to pass the stage the planner already sets: `Ask.source`, `"review"` for `Review`, `Dispatch.stage` (`reconcile.py` around the `Limit(..., stage=target)` assignment). No other production change.

## Do not build

- The quota stage-match fix. It is already tested.
- A lock around the trio call. The human answer wins; the pass must not die.
- The `replacement_ask` `session_id` naming nit. It was not filed and has no behavior.
- Later #750 lows that were not filed on this issue (singleton ambiguity interrupt, `_sync_decision_interrupts` on closed items).
- `Ask` equality.

## Risks

- A stand-in from item 3 is a non-unanimous row, so the trio does not consult that replacement after the quota clears. That is the escalation. The test must show the owner gets one ask, not a second consult.
- Item 2 must not let a user row replace a reversed row, or a reversal disappears.
- Item 4 must not drop `pending_stage` after a real launch failure. The existing launch-exit test stays.
- Item 6 will fail every equality assertion that omitted `stage` until those expectations name it. That is the point.
- Legacy `trio_retries` keys remain until a user answer for that question drops them. No migration pass.

## File ownership

Write:

- `scripts/engine.py`
- `scripts/decision_actions.py`
- `scripts/reconcile.py`
- `evals/test_engine.py`
- `evals/test_reconcile.py`

Read:

- `scripts/store.py`
- `scripts/slots.py`
- `scripts/trio_gate.py`
- `scripts/issue_gate.py`
