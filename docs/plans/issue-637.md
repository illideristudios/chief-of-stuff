# Issue 637 — M3 exit: kill mid-run, fake quota, resume with no prompt

Plan against `origin/develop` at `0893424` (2026-10-18). This worktree does not contain the engine. Implement on a branch from `develop`. Do not revive PR #754: it conflicts, and it deletes work. Architecture §8 replaced that design.

## Acceptance

One eval, two named scenarios, no data loss.

- **S1.** Kill the engine while a worker is in progress. The next process adopts that worker. Same segment, same run record, no second launch.
- **S4.** That run then hits a fake quota (429 with `quota` or `RESOURCE_EXHAUSTED`). The engine waits until `retry_at`, then launches the next attempt. No ask and no interrupt on either side of the wait.
- **No state loss.** Issue row, worktree path, branch, and commits from before the kill are unchanged across both scenarios.
- **Orphan recovery the scenarios exercise.** One readable, unquarantined orphan is reused with its tree, branch, and commits, and nobody is asked. A genuine conflict (more than one safe survivor) asks once, options exactly `abort` and `hold`. Anything not safe to reuse is quarantined and the run starts fresh. Abort quarantines; it never deletes. A host that might still be running is not quarantined.

Choosing a specific surviving tree ("keep this one") is later work. Do not build it.

```
K := engine killed mid-run          Q := fake quota hit
R := the run resumes                P := an ask or interrupt
L := state lost                     S := one readable unquarantined orphan
C := more than one safe survivor    U := unreadable, quarantined, or a failed fast-forward
H := the host has been confirmed stopped

K ∧ Q → R ∧ ¬P ∧ ¬L
S → reuse ∧ ¬P
C → one ask, options = (abort, hold)
U ∧ H → quarantine ∧ fresh start ∧ ¬hold
abort ∧ H → quarantine ∧ ¬delete
¬H → wait
```

Kill plus quota resumes, and that path never asks. Asking happens only for two safe survivors.

## Already on develop — do not rebuild

- Live and completed runs are adopted in `worker._existing_run` and `worker.collect` (`evals/test_worker.py`: `test_restart_adopts_live_run_before_slot_record`, `test_restart_adopts_completed_run_before_slot_record`, `test_real_detached_adapter_is_adopted_and_collected_after_engine_restart`).
- A quota outcome stores `resume.quota.retry_at` and `reconcile.plan` returns `Wait("quota reset pending")` until then (`test_quota_waits_then_retries_without_consuming_limit`). A quota stop is not a failure.
- `slots.claim` commits the open segment before `launch()`. A dead process leaves that row. `KeyboardInterrupt` during `launch()` does not: `except BaseException` drops it.

## What is still red

1. Nothing chains S1 and S4 and asserts no ask plus an unchanged tree.
2. Both processes dead, no `exit.json`, but a store report or quota text in stdout/stderr: `collect` leaves the segment open forever (#99, #138). The comment says the planner closes it. The planner never sees it.
3. A recoverable orphan becomes `human_review` via `completion.abandoned`. Nothing reuses the tree (#725).
4. No `completion.recovery`. No quarantine directory or `refs/quarantine/<issue>/<stamp>/` namespace.
5. SIGINT during `launch()` after `started.json` drops the segment, so restart cannot adopt it (#375).

## Tests first

Add cases to the files that already own the behavior. No new test module.

`evals/test_completion.py` — pure judgement, no git:

- One readable, unquarantined candidate → reuse it.
- Zero candidates → no recovery.
- Two safe candidates → conflict.
- Unreadable, quarantined, or conflicted (merge in progress) → not reused.
- A failed fast-forward is not a hold.

`evals/test_worker.py` — the exit and the I/O, beside the adoption tests:

- **Exit.** Launch with the existing fake adapter. Clear `worker._runners` (the engine process is gone; the host pid is not). `run_pass` adopts: `start` is not called, the segment and run record match, `store.asks` is empty, no interrupt. The host then writes a quota stderr and `exit.json` and exits. A pass before `retry_at` waits, asks nothing, and leaves the worktree path, branch, and HEAD alone. A pass after `retry_at` dispatches the next attempt and still asks nothing.
- **Finished, no `exit.json`.** Both pids dead. A store report of `done` closes the segment `done`. A quota stderr closes it `quota` and sets `retry_at`. Neither records an ask. The tree stays.
- **Killed launch.** `started.json` is on disk, then `launch` raises `KeyboardInterrupt`. The segment remains. The next collect adopts it and does not start a second worker. `command.json` with no `started.json` does not wedge the issue: that run dir is quarantined and the empty segment is dropped.
- **One orphan.** A dead worker leaves one dirty worktree and its `work/issue-637-*` branch. The next launch reuses that checkout. HEAD and uncommitted files survive. No ask.
- **Conflict.** Two diverged survivors → one decision, options exactly `("abort", "hold")`, keyed by issue, stage, and reason, so a second pass does not ask again. `abort` moves the trees under the quarantine directory and the refs under `refs/quarantine/<issue>/<stamp>/`, then builds a fresh tree. The old commits stay reachable from the quarantined ref. `hold` leaves every tree and ref where it is and does not launch; a later pass that sees a single safe orphan reuses it.
- **Not safe.** An unreadable tree and a failed fast-forward are quarantined, then a fresh tree starts. No hold, no ask.
- **Live host.** A matching pid blocks quarantine. The run waits.

`evals/test_engine.py` — one pass-level case: gathering recovery facts for one issue raises; the other issue in that pass still plans. Recovery facts are gathered in `engine_facts` before planning, inside the issue's existing exception boundary.

## Implementation

Smallest cut that turns those cases green.

1. **`completion.recovery(candidates) -> Recovery | Conflict | None`** in `scripts/completion.py`. Pure. A candidate is a frozen record: path, branch, head, readable, quarantined, merge-in-progress. Reuse only a single readable, unquarantined, non-conflicted candidate. Two or more of those are a `Conflict`. Anything else is not reused. No I/O, no hashing, no retire predicate.
2. **`completion.abandoned`** stops sending a reusable orphan to human review. Unreadable trees stay a hold only until step 4 quarantines them. Update the completion tests that pin today's orphan hold.
3. **`worker.collect`**, when both pids are confirmed dead and `exit.json` is missing: classify stdout/stderr with the existing adapter (quota included), or take the store report if there is one, and finish through the path `exit.json` already uses. No second closer. If the host or runner might still be alive, keep waiting, as today.
4. **`engine_facts`** gathers orphan candidates only for an issue whose latest segment is open and whose worker is not alive, or that has an open recovery decision. Skip any path or ref whose name is the quarantine directory or `refs/quarantine/`. A branch checked out by a surviving tree is not a second candidate. `clone is None` means no recovery. One issue's gather error is caught at the existing per-issue boundary in `engine.run_pass`.
5. **`worker.launch`** executes the judgement. Reuse passes the existing path and branch into `make_worktree.build`. Conflict calls the existing decision path (`engine._choice`) with options `("abort", "hold")` — a unanimous trio adopts, a split escalates, and the no-conflict path never reaches it. `hold` is not a dispatch: while recovery still returns `Conflict`, the engine passes that issue as pending so `reconcile.plan` waits with `decision pending`. Do not add a planner branch and do not add assignment text.
6. **Abort** confirms the host has stopped (`process_status.identity` on pid and runner pid). Then move the tree into the quarantine directory and rename its refs into `refs/quarantine/<issue>/<stamp>/`. Then build the replacement fresh. No `git worktree remove`, no `branch -D`, no content hash. Discovery skips those names, so a later launch cannot fast-forward onto quarantined commits. A failed fast-forward of a reuse is the same quarantine, then a fresh tree, not a hold.
7. **`slots.claim`** drops the segment on `Exception` only. `KeyboardInterrupt` and `SystemExit` leave a segment that already has `started.json`. If `started.json` was never written, drop the segment and quarantine the run dir so the next pass can start.

Deleting quarantined work is 1.1. Do not build it.

## Risks

- `except BaseException` is what keeps a pre-start interrupt from leaving a ghost segment. Narrowing it without the "no `started.json`" cleanup wedges the issue.
- Quarantine spends disk until 1.1. That is the accepted cost. A path that deletes is out of scope even if a review round asks for a safer delete.
- `completion.abandoned` has callers that expect an orphan hold. Change the judgement once, and fix those tests in the same change. Do not add a parallel hold.
- The exit eval must use the fake adapter. It must not call a live model.
- A quota stop under the limit waits. It must not record the M5 interrupt `reached quota limit`.
