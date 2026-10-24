# Plan: #927, one open scope decision, and a consult must not hold worker close

## Finding: where the code is

`scripts/engine.py` and `scripts/trio_gate.py` do not exist on this branch's base (`main`, 0.128.x, `50091f80`). They live on `origin/develop`. The implement stage must start from `origin/develop`, or every path below is missing. Line numbers are `origin/develop` at `4417a8fe`.

## What already landed (do not rebuild)

PR #947 / #948 already did the pass budget and the owner-wait stop, and the issue is still open because neither stops the observed loop.

- `TRIO_PASS_BUDGET_S = 480` (`engine.py:54`). `_settle_scopes` and `_route_asks` stop starting model calls once it is spent (`engine.py:827-829`, `trio_gate._ask`).
- A split or an exhausted retry (`retry_exhausted`, `decided_by == "zach"`) is not consulted again (`trio_gate._settled`, `decision_escalation.retryable_failure`).
- `worker.collect` (`engine.py:713`) closes a segment that already has `exit.json` before the trio runs, through `_finish_worker`. `workers_page._store_rows` shows RUNNING only while `seg.ended_at is None` (`workers_page.py:171`). No page change.

## Why #927 still fails

The store rows in the issue have an empty `decided_by` and `unanimous = 0`. That is only the retryable-outage write (`trio_gate.consult`, the branch that sets `decided_by` to `""`). A timeout is `Failure("unknown")`, and `unknown` is retryable (`model_port.RETRYABLE`). `_settled` then returns None (`trio_gate.py` around the `retryable_failure` check), so the next pass after `trio_retries.retry_at` calls the trio again and appends another unanswered scope row for the same question. kernel-sanders#21 is that loop: one question, seven open rows, none of them an owner wait, so "How it got here" never mentions them (`needs_owner` requires `decided_by == "zach"`).

`worker.collect` runs once, at the start of `run_pass`. A worker that exits during `_route_asks` / `_settle_scopes` is not closed, reaped, or dropped from `/workers` until a later pass. That is the drago#33 / #35 shape: `exit.json` and a done report exist, the segment stays open, the runner is `<defunct>`, and the same pass is busy appending scope decisions. The 480s budget only bounds how long that stall lasts. It does not close the worker inside the pass.

Asks and permission gates keep the #928 retry. This issue is topic `scope` only.

## Change

`scripts/trio_gate.py`

- `consult` and `decide` take `escalate_outage: bool = False`. `_choice` passes it only when `topic == "scope"`.
- When the flag is set and the verdicts are a retryable outage, the one row this consult appends is already the owner wait: `decided_by="zach"`, `answer=None`, `retry_exhausted` set, `error` naming the question. No model retry, no second row. `retryable_failure` is then false, so `_settled` returns `Outcome(None, False)` on later passes and `decide` does not call `consult`.
- Every other topic keeps today's retry, backoff, and cap of 8.

`scripts/engine.py`

- `_settle_scopes`, before `_choice`: if this issue already has an unanswered scope row for this proposal's question, do not call the model.
  - Newest row already `decided_by == "zach"`: leave `scope_proposal` in place and continue. Zach's later answer is a new row; the pass after that hits `_settled` and the existing approve / reject write.
  - Newest row is a legacy retryable outage (`decided_by` empty, as on the live stores): append one owner escalation through `trio_gate.record` (same topic, question, and record, `decided_by="zach"`, `retry_exhausted` set) and continue. No model call. Append-only, so the historical null rows stay; the pile stops growing.
- After `_route_asks` and `_settle_scopes`, call `worker.collect` again with the same `finish` and `gather` before `reconcile.plan`. Segments already ended are skipped. A segment whose `exit.json` appeared during the trio block is reaped (`runner.poll`), torn down (`teardown_once` no-ops when `torn_down` is set), and closed by `_finish_worker` before planning. Re-apply the existing post-collect ownership refresh (`engine.py` `still_running` / `refreshed`) for issues whose segment ended in this second collect, then plan from the facts the second collect returns when it gathered any.

A changed proposal is a different question, so it is a different decision. One open scope decision per issue per proposal.

### Behaviour the red tests lock

1. One scope proposal, model returns `Failure("unknown", "no answer within 300s")`: the pass appends one scope row, `decided_by="zach"`, `answer` null, and writes nothing. The next pass makes zero model calls and appends nothing. `owner_waits` names that row.
2. The same with a row already stored the old way (`decided_by=""`, retryable verdicts, no answer): the pass appends one escalation, makes zero model calls, and a further pass appends nothing.
3. A split still appends one owner row and a second pass does not consult. Zach answering `approve` on the next pass writes the section and clears `scope_proposal`, with no model call.
4. An open segment whose `exit.json` and done report appear only inside the scope consult's model call is ended when `run_pass` returns. A second pass is not required.

## Tests (write first, see them fail)

`evals/test_engine.py`, next to `test_a_split_on_a_scope_proposal_goes_to_the_owner_and_writes_nothing` and `test_trio_consults_stop_starting_once_the_pass_budget_is_spent`:

- The four behaviours above. The exit-during-consult test writes `exit.json` and the done report from the model callable, after `run_pass` has already passed the first `worker.collect`.

`evals/test_trio_gate.py`:

- `consult(..., escalate_outage=True)` on an unknown failure records one owner row and `decide` on that row does not call the model. The same failure without the flag still records `decided_by=""` and is asked again after the retry time. An ask is unchanged.

## Decision record

```
Glossary
Q(I) := issue I has a scope_proposal, and its newest scope row for that question has no answer
Z(I) := that newest row waits on Zach (decided_by = zach)
K(I) := this pass calls the trio for I's scope proposal
C(W) := worker W has exit.json and a report
E(W) := W's segment is ended when the pass returns

Premises
1. a retryable scope outage is stored with decided_by ""          [trio_gate.consult]
2. _settled returns None for a retryable row                      [trio_gate._settled]
3. 1 ∧ 2 → the next pass calls consult and appends another null row
4. needs_owner requires decided_by = zach                         [decision_escalation.py]
5. worker.collect runs once, before the trio block                [engine.py:713, 827]
6. RUNNING on /workers is an open segment                         [workers_page.py:171]
7. retry_exhausted makes retryable_failure false                  [decision_escalation.retryable_failure]

Derivation
8.  scope outage sets decided_by = zach and retry_exhausted on the only new row  [1, 7]
9.  Q(I) → the pass does not call the model; a legacy "" row gets one zach row   [3, 4, 8]
10. 8 ∧ 9 → ¬K(I) ∧ Z(I)
11. a second collect after the trio block ends any C(W) the first collect missed [5]
12. 11 → E(W), and 6 → /workers no longer lists W

∴ Q(I) → ¬K(I) ∧ Z(I), and C(W) → E(W) inside the same pass
```

One open scope decision per proposal. A pass that finds it does not consult. An outage or a split waits on Zach. A worker that has exited and reported is closed before the pass returns, so the consult cannot keep it on the Workers page.

## Risks

- **Base branch.** Implement from `origin/develop`, not this worktree's `main`.
- **#928 stays for every topic except scope.** A scope quota or timeout goes to Zach on the first outage, instead of retrying up to 8 times. That is the issue's "does not consult the trio again." Asks and gates are untouched.
- **ARCHITECTURE.md §6** ("a retryable outage is asked again only at its retry time") and **§8** ("an unavailable trio is retried on a later pass") become wrong for topic `scope`. The architecture agent owns that edit. This plan does not change the file.
- **Legacy piles stay in the store.** Append-only. They gain one owner row and no further consults. Nothing deletes the old null rows.
- **A changed proposal is a new question** and may be consulted while an older question's row is still open. One open row per question, which is the proposal.
- **`teardown_once` is safe to meet twice** (`torn_down`). The second collect must use the same `finish` callback or a done report is closed without the suite hold.
- **An exception thrown out of the trio block still skips the second collect.** The budget path returns normally; this plan does not add a new try/finally.

## Proposed file ownership

```
file_ownership:
- write scripts/engine.py
- write scripts/trio_gate.py
- write evals/test_engine.py
- write evals/test_trio_gate.py
- read  scripts/worker.py
- read  scripts/decision_escalation.py
- read  scripts/workers_page.py
- read  scripts/model_port.py
- read  architecture/ARCHITECTURE.md
```
