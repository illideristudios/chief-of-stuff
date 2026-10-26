# Issue #774 — Engine recovery hardening (filed from M3.exit #637)

## Context and base branch

This planning worktree (`work/issue-774-plan-1`) is branched from coordinator-era `main` at `50091f80`, which predates the engine modules (`scripts/worker.py`, `scripts/engine.py`, `scripts/engine_facts.py`, `scripts/completion.py`, `scripts/reconcile.py`, `scripts/slots.py`).

Implement on `origin/main` once M3.exit (#637, PR #1146 on `origin/work/issue-637-plan-1` at `474e1623`) lands. Where the issue body and round 6–8 comments cite line numbers and helper names from PR #754 (`origin/loop/637` at `7b650e2a`), this plan maps each edge case to both the landed `#637` architecture (`engine_facts.recovery_candidates`, `completion.recovery`, `worker._recover`, `worker._quarantine`, `worker.recovery_stopped`, `worker.preserve_start`, `slots.claim`) and the corresponding PR #754 helpers (`_finish_quarantine`, `_apply_quarantine`, `_move_tree`, `_abort_workers_gone`, `held_stages`, `needs_recovery`, `KeepReservation`). Note that `#1039` (`47a06fc6`) removed `store.ownership` / `store.set_ownership` in favor of `store.set_tree`; do not reintroduce `set_ownership`.

## Acceptance

Harden **non-destructive** recovery edge cases filed under the architect's M3.exit scope ruling: anything that can lose work was fixed in #637; this issue closes the remaining cancellation-ordering, slot/evidence accounting, wedging, and fail-safe gaps without changing S1, S4, or the §8 quarantine contract (`abort` and recovery never delete work).

```
∀ path: ¬ destroys_commits ∧ ¬ deletes_unquarantined_work
cancellation_mark ⇒ (verified_stopped ∨ reservation_kept) ∧ ¬ adopt_canceled_start
launch_exc_after_start ⇒ segment_accounting_matches_cancel_evidence ∨ reservation_kept
abort_retire(unreadable) ⇒ quarantine_without_delete ∨ fail_safe_with_saved_recovery_state
hold_or_blocked_abort ⇒ ¬ dispatch_while_blocked ∧ ¬ log_spam
```

## Already green in #637 — do not rebuild

- **S1 + S4 adoption and quota resume** without prompts or tree loss (`evals/test_worker.py`).
- **Quarantine over deletion (§8):** `worker._quarantine` (and `_move_tree` / `_apply_quarantine` on `loop/637`) moves trees into `trees / "quarantine"` and refs into `refs/quarantine/<issue>/<stamp>/`, falling back from `git worktree move` to `shutil.move` / `os.rename` when worktree metadata is corrupt.
- **Branch reuse after `main` moves (PR #1146 A1):** `_recover` fast-forwards only a survivor branch with zero commits of its own (`rev-list --count refs/remotes/origin/main..refs/heads/<branch>`) and reuses a branch with its own commits as-is, logging each quarantine.
- **Per-issue recovery isolation (PR #1146 A2):** `engine.run_pass` catches `recovery_candidates` and decision errors per issue via `recovery_failed`, deduplicating `recovery read failed` log entries and continuing the pass for other issues.

## What is still red (filed scope)

### A. Launch cancellation ordering, marker durability, and slot accounting (issue body, round 6)

1. **Cancellation marker ordering and crash/failure window (`worker.py` `launch`):**
   - In `worker.launch`, if an exception occurs after `runner` starts (or after `started.json` is written), cancellation must stop the runner and session (`claude_adapter.cancel(runner)` and `process_status.stop_session(session_id)`), verify that the host, runner, and process group have stopped (`_assert_stopped` / `process_status.identity` + `carriers`), and only then mark/archive the run directory (`<run_dir>-canceled-<uuid>`).
   - A marker-write failure must never skip `claude_adapter.cancel(runner)` or `process_status.stop_session(session_id)` (run process teardown in `try ... finally` before marker persistence).
   - If `cancel` or stop verification fails or times out, do not mark the run as cleanly retired or leave an unverified `started.json` in place where `_existing_run` or `recovery_candidates` could misread it; retain the open segment reservation (or `canceling` resume state / `KeepReservation`) so `collect` verifies stop before releasing the slot and archiving the run directory.
2. **Ordinary `Exception` after `started.json` vs `slots.claim` (`worker.py`, `slots.py`):**
   - Currently `slots.claim` drops the open segment on `except Exception:`, while `preserve_start` is only consulted on `except BaseException:` (`KeyboardInterrupt` / `SystemExit`).
   - When an ordinary `Exception` occurs after `started.json` is on disk (e.g., identity verification timeout or `_save_run` error), `slots.claim` drops the segment while `started.json` (or a `canceled` marker) remains in `run_dir`. If the host did not actually die, or if a retry/pass inspects `run_dir`, the dropped segment and leftover evidence disagree.
   - Fix: in `worker.launch`, when an `Exception` occurs after `started.json` (or `command.json`), cancel and verify stop; if stop is verified, archive/rename `run_dir` to `<run_dir>-canceled-<uuid>` so no stale `started.json` remains in `run_dir`. If stop cannot be verified (or cleanup fails), keep the reservation open (`preserve_start` / `KeepReservation`) so `collect` finishes cancellation and drops/closes the segment only after stop is confirmed.

### B. Corrupt worktree metadata when both git move and filesystem move/repoint fail (issue body, round 6)

3. **Fail-safe quarantine when move or ref repoint fails (`worker.py` `_quarantine` / `_move_tree`):**
   - `_quarantine` falls back from `git worktree move` to `shutil.move` + `git worktree repair`. However:
     - If `shutil.move` / `os.rename` itself raises `OSError`, `_quarantine` must fail safe without deleting any tree or ref, leaving `data["recovery"]` (or `abort_quarantine`) intact in `resume` so the issue does not launch a fresh tree over unquarantined state.
     - If `candidate.readable` was `True` (or partially readable) when scanned, `git worktree move` failed due to corrupt metadata, and `shutil.move` moved the directory, a subsequent `symbolic-ref HEAD <target>` or `update-ref -d <source>` in `_quarantine` can fail if git still considers `<source>` checked out in a linked worktree with broken metadata. Run `git worktree prune` (after moving the directory to quarantine) or tolerate `symbolic-ref` failure on a broken moved checkout before `update-ref -d`, and if `update-ref -d` still refuses because a live/unmoved checkout holds the branch, preserve the ref (`refs/quarantine/...` is already written) rather than crashing half-way without state persistence.

### C. Hold / planner / facts hardening (issue comments, rounds 6–7 & PR #1146 A4)

4. **Shared run-dir helper between `worker` and `engine_facts` (PR #1146 A4):**
   - `engine_facts.recovery_candidates` currently rebuilds `root / ".chief-of-stuff" / "runs" / f"{key.rpartition('#')[2]}-{latest.stage}-{latest.attempt}"` by hand. Expose `worker.run_dir(root, key, stage, attempt)` (or call `worker._run_dir`) so `engine_facts` and `worker` share a single definition.
5. **Unstarted `command.json` archives and disk-only starts in `recovery_stopped` / `_abort_workers_gone` (round 7 High):**
   - `worker.recovery_stopped(store, key)` currently inspects only `_resume(store, key).get("runs", {}).values()`. An interrupted launch that wrote `started.json` or `command.json` in `run_dir` before `_save_run` is not in `data["runs"]`.
   - Conversely, a canceled or refused command-only directory (`command.json` without `started.json` or `exit.json`) that was already confirmed dead must be quarantined/renamed (`-canceled-` / `-quarantine-` / `-refused-`) so `recovery_stopped` / `_abort_workers_gone` does not block abort forever.
   - Harden `recovery_stopped` (and `_abort_workers_gone` if present) to also check any unarchived `<number>-*` run directory in `supervisor.state_dir(root) / "runs"`, quarantining dead expired `command.json`-only directories and returning `False` if any unarchived directory has a live carrier/group or unverified start.
6. **Blocked abort waits without consuming a slot or spamming the log (round 6 & round 7 Low):**
   - When a conflict decision is answered `abort` (or recovery requires quarantine), if `recovery_stopped` is `False` or quarantine cannot proceed, `engine.run_pass` must hold the issue in `recovery_pending` (`Wait("decision pending")` / `Wait("orphan recovery held")`) rather than letting `reconcile.plan` emit `Dispatch`, which would consume a worker slot and log `dispatch failed` on every pass.
7. **`clone is None` and unreadable recovery facts preserve existing conflict holds (round 6 Medium):**
   - If an issue has an open or `hold`-answered `recovery` / `conflict` decision and `clone is None` or `recovery_candidates` raises, the issue must remain held (`recovery_errors` / `recovery_pending` / `completion.held_stages(..., readable=False)`) and never bypass the hold to dispatch.
8. **Skip recovery candidate scans when `free_slots == 0` and no recovery state/decision is active (round 6 Low):**
   - In `engine.run_pass`, avoid scanning worktrees/refs for orphan recovery when `slots.free_slots(max_concurrency, slots.running(store)) == 0`, except for issues that have an open segment with a dead worker (which `collect` / `abandoned` needs to close) or an active `"recovery"` resume entry / open `recovery` decision.

### D. Partial quarantine, replacement verification, and resume migration (issue comments, round 7)

9. **Interrupted quarantine / partial replacement does not pollute candidates or reopen conflict asks (round 7 Low):**
   - If `_recover` (or `_finish_quarantine`) fails after moving one candidate or after creating a replacement worktree/branch, the next pass's `recovery_candidates` must not treat the half-quarantined state or a rejected fresh replacement as a new conflicting survivor that changes the candidate count and triggers a second conflict ask.
   - Persist quarantine progress (or update `data["recovery"]["candidates"]` as each candidate is quarantined and record `recovery_fresh` / `abort_quarantine` before building the fresh tree) so a retry resumes cleanly.
10. **Prepared head binding for fresh replacement trees (round 7 Low):**
    - When building and verifying a replacement tree, bind verification to the commit SHA actually prepared by `make_worktree.build` (or recorded in the plan), not a subsequent `rev-parse` of a mutable remote default branch ref that may have advanced after `build` released its lock.
11. **Saved worktree/replacement removed or committed into before launch completes (round 7 Low):**
    - If a saved `worktree` / replacement path was removed on disk or had new commits added before the run started, preserve any work in the modified tree (quarantine if it cannot be reused, never delete) and recover cleanly instead of wedging on `"existing worktree is missing or no longer on the work branch"`.
12. **Legacy `abort_replacement` / `abort_quarantine` resume records (round 7 compatibility):**
    - If `resume` contains a legacy `abort_replacement` (from pre-quarantine PR #754) or malformed `abort_quarantine` record, refuse destructive retirement, log once with manual quarantine instructions (or route through safe quarantine of the listed paths/refs), and keep all trees intact.

### E. Round 8 findings (content hashing and special files — no-op under §8 quarantine unless fingerprinting is present)

13. **Ignored directories and socket/FIFO entries (round 8 findings 4 & 5):**
    - Architecture §8 (`79465dd6`) eliminated `_tree_contents` recursive hashing in favor of unconditional quarantine (`Nothing is hashed, compared or deleted`). Do **not** reintroduce content hashing.
    - Add a fast regression test verifying that quarantining a worktree containing a Unix domain socket / FIFO (`os.mkfifo` or `AF_UNIX`) and an ignored directory (`node_modules/`) moves the directory in `O(1)` via `git worktree move` / `shutil.move` without opening special files or walking ignored trees.

## Tests first

Extend existing test modules only (`evals/test_worker.py`, `evals/test_completion.py`, `evals/test_engine.py`). Run each new test red before editing production code.

### `evals/test_worker.py`

1. **`test_cancel_runs_when_marker_or_archive_fails_and_preserves_reservation`:**
   - Force post-`started.json` launch failure and make marker write / archive rename raise `OSError`; assert `claude_adapter.cancel` and `process_status.stop_session` still run, and the segment reservation is retained until `collect` verifies the process stopped.
2. **`test_ordinary_launch_exception_after_started_json_archives_canceled_or_keeps_segment`:**
   - Raise `RuntimeError` after `started.json` is written; assert that when the host stops, `run_dir` is archived as `-canceled-` so no stale `started.json` remains in `run_dir`, and when the host is still alive, the segment reservation stays open for `collect` and is never adopted as a valid run.
3. **`test_quarantine_move_and_rename_failure_preserves_trees_and_recovery_state`:**
   - Make both `git worktree move` and the fallback `shutil.move` / `os.rename` fail (and test a broken checkout where `symbolic-ref` fails after fallback move); assert no files or refs are deleted, `recovery` state stays saved in `resume`, and no fresh worktree launches over the unquarantined state.
4. **`test_expired_command_only_run_dir_is_quarantined_and_does_not_block_abort`:**
   - Leave a dead, expired `command.json`-only directory (and an unrecorded `started.json` with a live pid) in `.chief-of-stuff/runs/`; assert `recovery_stopped` quarantines the dead command-only dir and proceeds, while blocking when an unrecorded `started.json` process is still live.
5. **`test_interrupted_abort_or_replacement_does_not_reopen_conflict_ask`:**
   - Simulate a failure mid-`_recover` (after one of two conflicting candidates is quarantined, or when replacement verification fails); assert the next pass finishes quarantine without creating a second conflict ask or leaving an extra `issue-N-*` candidate.
6. **`test_modified_or_removed_saved_worktree_preserves_commits_in_quarantine`:**
   - Commit to or remove the saved `worktree` before relaunch; assert any added commits are preserved (reused or quarantined under `refs/quarantine/`) instead of wedging or deleting work.
7. **`test_quarantine_tolerates_fifo_and_ignored_dir_without_opening_or_hashing`:**
   - Create `os.mkfifo` and a populated `node_modules/` in an unreadable/aborted orphan tree; assert `_quarantine` moves the tree without opening the FIFO or walking `node_modules/`.

### `evals/test_completion.py`

8. **`test_recovery_judgements_preserve_holds_when_unreadable_or_conflicted`:**
   - Verify `completion.recovery` (and `held_stages` / `needs_recovery` if present) keeps conflict holds active when facts are unreadable and never reuses a quarantined, unreadable, or merge-in-progress candidate.

### `evals/test_engine.py`

9. **`test_answered_abort_with_live_worker_waits_without_consuming_slot_or_repeating_log`:**
   - Answer `abort` on a recovery conflict while `recovery_stopped` returns `False` (or quarantine raises); run two passes and assert both return `Wait`, `free_slots` is not consumed for another ready issue, and the log entry is written at most once.
10. **`test_clone_none_or_unreadable_facts_does_not_bypass_recovery_hold`:**
    - With an active `recovery` hold decision and `clone=None` (or raising `recovery_candidates`), assert `run_pass` waits and does not dispatch.
11. **`test_zero_free_slots_skips_idle_recovery_candidate_scans`:**
    - When `max_concurrency` is saturated (`free_slots == 0`) and an issue has no open dead segment and no active recovery state/decision, assert `engine_facts.recovery_candidates` does not scan git worktrees/refs.

## Implementation steps

1. **Share `worker.run_dir` (`scripts/worker.py`, `scripts/engine_facts.py`):**
   - Make `run_dir(root, key, stage, attempt)` public in `scripts/worker.py` (keeping `_run_dir = run_dir` alias for existing tests) and call it from `engine_facts.recovery_candidates` instead of reconstructing `.chief-of-stuff/runs/...` inline.
2. **Harden launch cancellation and `slots.claim` preservation (`scripts/worker.py`, `scripts/slots.py`):**
   - In `worker.launch`'s post-spawn exception handler:
     - Always execute `claude_adapter.cancel(runner)` and `process_status.stop_session(session_id)` inside `try ... finally` before writing markers or renaming `run_dir`.
     - Check whether the spawned runner/host/session has stopped (`preserve_start` / `_assert_stopped`).
     - If stopped, write `(run_dir / "canceled")` and rename `run_dir` to `<run_dir.name>-canceled-<uuid>` so neither `_existing_run` nor `recovery_candidates` sees a live/uncollected `run_dir`.
     - If stop cannot be confirmed (or marker/rename raises `OSError`), record `canceling` in `resume` and keep the segment reservation in `slots.claim` (extend `slots.claim`'s `preserve_start` check to `except Exception:` as well as `except BaseException:`, or raise a reservation-preserving error) so `worker.collect` verifies stop and archives/drops the canceled attempt on a later pass.
   - In `worker._existing_run` and `worker.collect`:
     - Never adopt a run directory that has a `canceled` marker or `canceling` resume entry; once confirmed stopped, rename to `-canceled-<uuid>`, clear `canceling`, and drop/close the segment cleanly.
3. **Harden `recovery_stopped` for disk-only `started.json` and expired `command.json` (`scripts/worker.py`):**
   - Extend `worker.recovery_stopped(store, key, root=root)` to inspect `supervisor.state_dir(root) / "runs"` matching `<number>-*` (skipping `-canceled-`, `-quarantine-`, `-refused-` archives and `runs/quarantine/`):
     - If `started.json` exists without `exit.json`, verify both `pid` and `runner_pid` (and `carriers(session_id)`) are stopped.
     - If `command.json` exists without `started.json` or `exit.json`, check `preserve_start(run_dir)`; if no carriers are live, quarantine `run_dir` via `_quarantine_run` so it never wedges abort; if carriers are live or unreadable, return `False`.
4. **Harden `_quarantine` and `_recover` against partial failures and broken checkouts (`scripts/worker.py`):**
   - In `_quarantine`:
     - When `git worktree move` fails and `shutil.move` succeeds, run `git worktree prune` / `repair` and tolerate `symbolic-ref` failure on an unreadable moved checkout so `update-ref -d` can still retire the original branch ref safely after `refs/quarantine/...` is written.
     - If `shutil.move` fails (`OSError`), leave `refs/quarantine/...` in place and raise `RuntimeError` without deleting the tree or clearing `data["recovery"]`.
   - In `_recover`:
     - Update `data["recovery"]["candidates"]` as candidates are quarantined (or filter already-quarantined paths/refs before re-evaluating `completion.recovery`) so an interrupted `_recover` does not re-ask or misclassify on the next pass.
     - Handle legacy `abort_replacement` / `abort_quarantine` keys in `data` by routing their listed paths/refs through `_quarantine` (or logging once and holding) without deleting any tree.
   - In `worker.launch`:
     - When `data.get("worktree")` is missing on disk or on a different branch/head than expected, route through orphan recovery/quarantine rather than raising an unrecoverable error or deleting commits.
5. **Hold on unreadable facts, `clone=None`, and blocked abort; gate idle scans (`scripts/engine.py`, `scripts/engine_facts.py`, `scripts/completion.py`):**
   - In `engine_facts.recovery_candidates`:
     - When `clone is None`, if the issue has an open `recovery` decision (or `"recovery"` in `data`), signal that the recovery hold remains active (`recovery_pending`) rather than returning `None` and allowing dispatch.
   - In `engine.run_pass`:
     - Skip calling `recovery_candidates` when `free_slots == 0` for issues that have no open segment, no `"recovery"` in `resume`, and no open `recovery` decision.
     - When `choice == "abort"` (or quarantine is due) and `not worker.recovery_stopped(store, key, root=root)`, keep `key` in `recovery_pending` so `reconcile.plan` emits `Wait(key, "decision pending")` without consuming a slot.

## Risks

- **Over-eager slot retention on launch failure:** Keeping a segment open when `launch` fails before any process spawns would leak a concurrency slot. `preserve_start` must return `True` **only** when `started.json` exists (and was not verified stopped + archived) or `command.json` has live `carriers(session_id)` (or an unreadable process table). Pre-spawn failures (`cannot read worker issue`, worktree errors) must still drop the reservation immediately.
- **Quarantining a tree with a live host:** Every quarantine path (`_recover`, legacy migration, broken-worktree fallback) must gate on `recovery_stopped(store, key, root=root)` where unknown identity or `ps` failure returns `False` (wait).
- **Skipping recovery scans when `free_slots == 0`:** Must never skip an issue that has an active `recovery` decision or `"recovery"` in `resume`, or an open segment whose worker died (since closing abandoned segments frees slots).
- **Base branch drift:** `#1039` removed `store.set_ownership` / `store.ownership` on `origin/main` (`47a06fc6`), and PR #1146 (`474e1623`) updated `#637` to use `store.set_tree` / `resume["worktree"]`. Verify against the merged `main` head before editing `scripts/worker.py`.

## Validation

On the implementation branch (with Python dependencies installed):

```sh
python3 -m unittest evals.test_completion evals.test_worker evals.test_engine
python3 -m unittest discover -s evals -p 'test_*.py'
```

On this planning worktree (`50091f80`), run the full eval suite (`python3 -m unittest discover -s evals -p 'test_*.py'`) and report exact `passed`, `failed`, `skipped`, and `xfailed` counts via the `report` tool (`passed > 0`, `failed = 0`, `skipped = 0`, `xfailed = 0`).

## File ownership

- write: `scripts/worker.py`
- read: `scripts/slots.py`
- write: `scripts/engine.py`
- write: `scripts/engine_facts.py`
- read: `scripts/completion.py`
- read: `scripts/reconcile.py`
- write: `evals/test_worker.py`
- write: `evals/test_completion.py`
- write: `evals/test_engine.py`
- read: `scripts/make_worktree.py`
- read: `scripts/process_status.py`
- read: `architecture/ARCHITECTURE.md`
