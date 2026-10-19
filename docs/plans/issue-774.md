# Issue #774 — Engine recovery hardening (filed from M3.exit #637)

Plan against `origin/loop/637` at `7b650e2a` (2026-10-19). That branch carries the M3.exit worker, quarantine abort, and `KeepReservation` cancellation path from PR #754. This planning worktree is at `50091f80` and does not contain the engine modules; implement on a branch from `origin/loop/637` (or `develop` once #637 lands). Do not revive or merge PR #754 wholesale.

## Acceptance

Harden **non-destructive** recovery edge cases filed under the architect’s M3.exit scope ruling: anything that can lose work was fixed in #754 / #637; this issue closes the remaining measurement, wedging, and fail-safe gaps without changing S1, S4, or the named orphan-recovery contract from #637.

```
∀ path: ¬ destroys_commits ∧ ¬ deletes_unquarantined_work
cancellation_mark ⇒ (verified_stopped ∨ reservation_kept) ∧ ¬ adopt_canceled_start
launch_exc_after_start ⇒ segment_accounting_matches_cancel_evidence ∨ KeepReservation
abort_retire(unreadable) ⇒ quarantine_without_delete ∨ fail_safe_with_saved_abort_quarantine
hold_answered ⇒ ¬ dispatch_while_blocked ∧ ¬ log_spam
```

Data-loss guards from #754 (`_abort_workers_gone`, identity checks, quarantine move/rename) stay; this issue only tightens ordering, accounting, and blocked-state behavior around them.

## Already green on `origin/loop/637` — do not rebuild

- Quarantine abort via `_move_tree` / `_apply_quarantine` (no `git worktree remove --force`); corrupt `worktree move` falls back to `os.rename` (`test_abort_quarantines_a_tree_git_cannot_move_without_deleting_it`).
- `KeepReservation` keeps the segment when cancel cleanup cannot finish; `collect` archives only `canceled`-marked starts after `_assert_stopped` (`test_a_launch_exception_holds_cancellation_until_the_login_shell_stops`, `test_collect_keeps_the_reservation_until_a_canceled_start_is_verified_stopped`).
- `_existing_run` ignores canceled marks (`test_adoption_honors_a_cancel_mark`); dead hosts without a mark fail the attempt (`test_a_dead_start_without_a_cancel_mark_counts_as_a_failed_attempt`).
- `engine_facts.recovery` + `completion.held_stages` + `reconcile.plan(..., recovery_held=...)` wait on hold without consuming a slot when facts are readable (`test_no_clone_does_not_bypass_a_recovery_hold`).
- Per-issue gather errors do not stop the pass (`test_one_issues_recovery_facts_failing_does_not_stop_the_pass`).

## What is still red (filed scope)

### A. Cancellation marker ordering and accounting (issue body, round 6)

1. **`canceled` written before `cancel(runner)` completes** — On `loop/637` the mark is written in the `finally` after `cancel`, but a partial crash between mark and rename can leave a marked dir with ambiguous segment state. Harden: single durable phase ordering (cancel → verified stop → mark+rename → clear `canceling`), and document crash windows that still fail safe.
2. **Marker-write failure must not skip `cancel`** — `test_a_marker_write_failure_does_not_skip_the_cancel` exists; extend to assert `canceling` resume flag and segment stay paired when the mark fails.
3. **Ordinary `Exception` after `started.json` with dropped segment** — When cleanup raises `KeepReservation`, segment stays (good). When cleanup completes but `slots.claim` already dropped on a non-`KeepReservation` path, evidence can outlive the segment so `collect` never archives and adoption may ignore the mark. Close: either always raise `KeepReservation` until archive completes, or teach `collect` to reconcile orphaned canceled evidence without a segment (measurement only; must not adopt).

### B. Abort retirement for unreadable trees (issue body, round 6)

4. **Corrupt metadata when both git move and rename fail** — Today rename failure raises and leaves `abort_quarantine` saved for retry. Add regression where `os.rename` also fails: tree stays, plan stays, no fresh tree pretends success, no branch delete.

### C. Hold / planner / facts (issue comments, rounds 6–7)

5. **Failed fast-forward → abort path** — Ensure `completion.recovery` conflict after FF failure does not dispatch; hold waits without slot (`recovery_held`).
6. **`clone is None` and unreadable facts** — `held_stages` with `readable=False` must keep all conflict holds; no recovery scan that raises out of `launch` without a planner `Wait`.
7. **Answered abort while retirement still refused** — If `_finish_quarantine` or `_abort_workers_gone` still raises, subsequent passes must `Wait` (blocked abort), not `Dispatch` + repeated `dispatch failed` logs.
8. **Recovery fact gather under load** — `recovery_conflict` takes `clone_lock` via orphan reads; defer scan when `free_slots == 0` and no open conflict decision requires facts (performance hardening; must not skip facts when a hold is active).

### D. Partial quarantine / replacement (issue comments, round 7)

9. **Unstarted canceled archives block abort** — `command.json` without `started.json` in a non-renamed run dir: after verified cancel, write terminal evidence (rename/quarantine) so `_abort_workers_gone` does not treat it as live forever.
10. **Failed replacement pollutes candidates** — On `_verify_rejection` or head-read failure, quarantine only the rejected fresh tree/ref; do not leave a half-built `issue-N-implement-*` in the candidate glob.
11. **Mutable base ref after `build`** — Pin replacement verification to the prepared commit recorded in `abort_quarantine`, not a later `rev-parse` of a moving default branch.
12. **Saved replacement removed or committed into** — Resume `abort_quarantine` when the saved `fresh` path is gone or no longer matches prepared branch/head; preserve work on any modified replacement tree (quarantine, do not delete).
13. **Legacy `abort_replacement` resume shape** — Refuse unsafe retirement without invented snapshots; add explicit migration: detect old key, log once, require manual quarantine or clear with documented safe steps.

### E. Round 8 (only if content hashing returns)

PR #754’s `_tree_contents` retirement hash is **not** on `loop/637`. If a future change reintroduces content fingerprinting under `clone_lock`, file follow-ups 14–15 apply then:

14. Bound ignored-directory hashing or move fingerprinting outside the lock with the same retire guarantee.
15. Hash socket/FIFO metadata without opening; device nodes remain rejected.

Do not add hashing solely to satisfy 14–15 on the current quarantine design.

## Tests first

Extend existing modules only; no new test package.

`evals/test_worker.py` (beside cancellation and abort fixtures):

| Case | Assertion |
| --- | --- |
| Crash window: mark without rename | Next `collect` either completes archive or keeps reservation; never adopts |
| Non-`KeepReservation` launch error after `started.json` | Segment and canceled evidence agree; or orphan archive path runs |
| `os.rename` fails after failed `worktree move` | `abort_quarantine` retained; old trees untouched; no false fresh launch |
| Verified cancel of command-only run | Run dir renamed/quarantined; `_abort_workers_gone` proceeds |
| Rejected replacement | No extra `issue-N-*` candidate; retry reuses same conflict decision |
| Prepared head binding | Moving `origin/main` after build does not false-pass `_verify_replacement` |
| Removed `fresh` path with saved plan | Safe resume or hold; no dispatch loop |
| Answered abort + live worker guard | `run_pass` → `Wait`, not `Dispatch`; log not duplicated |

`evals/test_completion.py`:

| Case | Assertion |
| --- | --- |
| `held_stages` when `readable=False` | Hold applies even if `conflict` is unknown |
| `needs_recovery` with conflict decision | Facts gathered even with no open segment |

`evals/test_engine.py`:

| Case | Assertion |
| --- | --- |
| Blocked abort | Answered abort + raising `_finish_quarantine` → `Wait("orphan recovery held")` or dedicated blocked reason |
| Zero free slots, no active hold | `recovery_conflict` not called (mock/spy), or cheap early return |

`evals/test_engine_facts.py` (add only if cases do not fit `test_engine.py`):

| Case | Assertion |
| --- | --- |
| Gather exception | Per-issue log + conservative holds unchanged |

Run red cases on the implementation base before editing production code.

## Implementation (smallest cut)

1. **`worker.launch` cancel cleanup** — Reorder to: stop processes → `_assert_stopped` → write `canceled` + rename → clear `canceling` and run record pop. On any failure before verified stop, raise `KeepReservation` only (never leave mark without stop proof). Never drop segment in `slots.claim` while canceled evidence remains unarchived.
2. **`worker.collect`** — If canceled evidence exists for a stage/attempt with no open segment, archive or quarantine in one pass (mirror existing canceled branch); do not adopt.
3. **`_move_tree` / `_finish_quarantine`** — When both git move and rename fail, raise with `abort_quarantine` already persisted; no candidate scan includes paths listed only in pending rejected plans.
4. **`_abort_workers_gone`** — Treat command-only dirs with `-canceled-`/`-quarantine-`/`-refused-` suffixes or explicit terminal marker as stopped; quarantine unstarted command-only dirs after verified cancel.
5. **`_finish_quarantine` / `_verify_replacement`** — Compare against prepared branch/head/commit stored in plan; on mismatch, append rejected pending entry instead of dispatching.
6. **`recovery_conflict` / `_resolve_worktree`** — Short-circuit when `abort_quarantine` present and retirement cannot proceed: raise hold-style error consumed by engine as `Wait`, not slot-consuming dispatch failure.
7. **`engine.run_pass`** — When `free_slots == 0` and no issue needs `needs_recovery(...)` true, skip `worker.recovery_conflict` lock work (pass `conflict=None` into holds for those issues only).
8. **Resume migration** — On read, map deprecated `abort_replacement` to safe refusal + log pointer; never synthesize retire snapshots.

Touch `scripts/slots.py` only if `KeepReservation` contract needs a one-line comment or a test-visible hook; prefer fixing in `worker.launch`.

## Risks

- **Over-broad `collect` orphan archive** — Could drop a segment that still has a live host; always call `_assert_stopped` before archiving; never archive without cancel mark or verified unstarted dead.
- **`KeepReservation` starvation** — Holding slots on broken cancel paths blocks concurrency; cap retry logging; surface one log line per pass.
- **Skipping recovery scans** — Must not skip when any open conflict hold or `abort_quarantine` needs facts; gate only the no-slots/no-recovery-needed case.
- **Migration** — Legacy resume keys must fail safe (retain trees) even if annoying; document manual recovery in log text, not a new UI.
- **Branch base drift** — If #637 merges with renames, re-anchor line numbers; behavior anchors are the tests above, not PR #754 line refs.

## Validation

On the implementation base:

```text
python3 -m unittest discover -s evals -p 'test_*.py'
```

Report exact passed/failed/skipped/xfailed from the final run. Planning-stage validation on worktree `50091f80` uses the coordinator-era eval suite only; engine suites (`evals/test_worker.py`, etc.) exist only after checking out `origin/loop/637`.

## Proposed implementation file ownership

| Path | Mode | Purpose |
| --- | --- | --- |
| `scripts/worker.py` | write | Cancel ordering, collect orphan archive, quarantine/abort resume, replacement verification |
| `scripts/slots.py` | read | `KeepReservation` contract with `claim` |
| `scripts/engine.py` | write | Blocked-abort wait, conditional skip of recovery scans |
| `scripts/engine_facts.py` | write | Conservative holds when facts unreadable; optional gather shortcuts |
| `scripts/completion.py` | read | `recovery`, `needs_recovery`, `held_stages` |
| `scripts/reconcile.py` | read | `recovery_held` wait wiring |
| `evals/test_worker.py` | write | Cancellation, abort, quarantine regressions |
| `evals/test_completion.py` | write | Hold/readability judgements |
| `evals/test_engine.py` | write | Pass-level blocked abort and gather isolation |
| `scripts/make_worktree.py` | read | `git_trusted`, `build`, clone lock |
| `scripts/process_status.py` | read | `_assert_stopped` / identity |
| `architecture/ARCHITECTURE.md` | read | §4 placement (recovery judgement vs drivers) |
