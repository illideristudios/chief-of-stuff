# Plan: #890 — Permission requests never become owner decisions; widen the known-safe set

## Base and verdict

This planning worktree (`work/issue-890-plan-1`) is branched from `main` at `50091f80`, which predates the engine worker guard (`scripts/guard_commands.py`, `scripts/worker_guard.py`, `scripts/engine.py`) and their evals. Do not implement on `50091f80`.

On `origin/develop` (`4417a8fe`), issue `#890` is already implemented and merged:

| Commit | Role |
|--------|------|
| `4fd8040f` | Red: `evals/test_guard.py` and `evals/test_engine.py` permission cases |
| `2bc26837` | Green: `guard_commands` known-safe widening; `engine._route_asks` / `_settle_permission` |
| `0b4657b8` | Test fix: `sed` `$` address vs tokenizer |
| `85126c13` | Merge develop; trio exhausted / nothing launchable denies permission (#891 alignment) |
| `756e63ed` | Green: legacy permission asks with empty `options` settle too (#890 follow-up) |

Merged via PRs `#892` and `#900`. The implementation stage should cut from `origin/develop`, confirm `git merge-base --is-ancestor 756e63ed HEAD`, run the focused suites below, and **make no code changes** unless a test fails.

`scripts/decision_actions.py` is in the issue's write scope for coordination with other slices but was **not** modified for `#890`; settlement lives in `scripts/engine.py` (`_permission`, `_settle_permission`, `_route_asks`). Do not edit `decision_actions.py` unless a failing test points there.

Operational note: the 29 drago permission asks and any open copies on other engine hosts (e.g. kernel-sanders) close when those engines run `_route_asks` after deploy — answers are **appended** via `store.answer_ask` and a guard `trio_gate.record` row, never by deleting ask or decision rows. No one-off migration script is in scope.

## Logic

```
P := a worker PermissionRequest (Bash or file tool) the guard cannot auto-allow
R := permission_judgement returns ("allow", "")
T := permission_judgement returns ("trio", why)
D := denial_reason is non-empty (engine-owned git/forge writes)
J := _route_asks re-judges stored event context with today's rule
A := trio unanimous allow or deny on ("allow", "deny")
S := trio split, unavailable, quota-exhausted, or pass budget spent
O := owner_waits / decision_interrupts / NEEDS INPUT for this ask

1. D → PreToolUse deny; no ask                                              [unchanged]
2. R → allow at hook; no ask                                                [#874 + #890 reads/writes]
3. P ∧ T → worker_guard records one ask; hook denies until answered         [#750, #874]
4. J ∧ R → _settle_permission(allow|deny); model not consulted                [#890 (3)]
5. P ∧ A → _settle_permission(choice); logged on run                        [#890]
6. P ∧ S → _settle_permission(deny, reason); ¬O                             [#890 (2), #891]
7. Legacy ask with options () ∧ question prefix "Permission requested for" → P  [#890 follow-up]
∴ every permission path ends in allow, trio allow/deny, or deny — never needs_owner
```

Stripped of modality: routine commands auto-allow; everything else gets at most one trio vote; failure modes deny with a log line and never surface an owner decision card.

## Landed behavior on `origin/develop`

| Acceptance item | Where | Verification |
|-----------------|-------|----------------|
| **Wider known-safe reads:** `sed -n` with line-range `p` only; `git worktree list`; `git branch` listing flags (`-a`, `-r`, `--list`, `-v`) | `scripts/guard_commands.py`: `SED_PRINTS`, `_reads_only` (`sed`, `git branch`, `git worktree list` via `_tree_write` / `GIT_READS`); `FIND_PATTERNS` / `_patterns` so grep/find patterns are not paths | `evals/test_guard.py::GuardPermissionTest.test_routine_reads_and_own_tree_git_writes_are_allowed_without_anyone` |
| **Known-safe writes** in own tree: `git add`, `git commit` (no `--amend` / `--no-verify`), `git switch -c` / `git checkout -b`; denials still first | `_tree_write`; `denial_reason` / `_denied` unchanged for push, force, reset --hard, etc. | Same test; `test_sed_scripts_that_write_or_run_and_history_rewrites_are_not_auto_allowed` |
| **Permission never owner decision** | `engine._settle_permission`, `_route_asks`; `worker_guard` unchanged trio-first path | `evals/test_engine.py`: `test_an_open_permission_ask_raised_under_an_older_rule_is_answered_by_todays`, `test_a_permission_ask_raised_before_874_with_no_options_is_settled_too`, `test_a_split_trio_denies_a_permission_and_never_asks_the_owner`, `test_a_trio_with_nothing_launchable_or_every_entry_exhausted_denies_a_permission`, `test_an_unavailable_trio_denies_a_permission` |
| **Open asks close by appended answer** | `store.answer_ask` + `trio_gate.record` with `decided_by` `guard` when rule settles | `test_an_open_permission_ask_raised_under_an_older_rule_is_answered_by_todays` (log line `permission allow:`) |
| **Red tests:** 29 recorded shapes allowed; bad `sed` / `git commit --amend` not auto-allowed; split → deny, no owner | Test data uses invented paths but covers all drago **shapes** (compound commands in one tuple, not 29 separate strings) | Tests named above |

Negative cases that must stay **deny → ask → trio**, not auto-allow: `sed -n '1p;w /tmp/x'`, `git commit --amend`, `git commit --no-verify`, outside paths, merge commands, architect #875 holes (`test_the_audits_four_holes_never_auto_allow`).

## Implementation stage steps

1. Merge `origin/develop` into the stage branch (engine owns merges; never force). Re-read `permission_judgement`, `_tree_write`, and `_route_asks` after merge.
2. Verify ancestry: `git merge-base --is-ancestor 756e63ed HEAD` must succeed.
3. Run (with the engine venv / deps used for other develop workers):

   ```sh
   python3 -B -m unittest \
     evals.test_guard.GuardPermissionTest \
     evals.test_guard_commands \
     evals.test_engine.PermissionAskTest
   python3 -B -m unittest discover -s evals -p 'test_*.py'
   ```

   Report exact `passed`, `failed`, `skipped`, `xfailed` from the final discover run (`passed > 0`, others `0`).
4. If all green, report `done` with no diff. Edit only within file ownership below if something fails.
5. After deploy, confirm drago (and peer hosts) clear held issues: open permission asks should gain `answer` and run log lines without new `decision_interrupts`.

### Candidate gaps (none confirmed on `4417a8fe`)

- **`git add` paths:** `_tree_write` allows `git add` but `judgement` still requires every path operand inside the worktree. If a worker adds only basenames, today they are treated as inside (`inside` lexical rule). Do not tighten without a failing case.
- **`#876` follow-up (symlink realpath):** not on `origin/develop` at plan time; if it lands before implement, re-run the guard suites — permission ask identity must stay consistent across rewrite.
- **Exact 29 CLI strings:** acceptance is covered by representative compounds; do not add 29 duplicate subtests unless product asks for byte-for-byte replay from a store export.

## Tests (TDD reference)

Red commits are already in history. Implement stage is verify-only unless broken.

| Layer | Module | Tests |
|-------|--------|-------|
| Pure grammar | `guard_commands.judgement` | `evals/test_guard_commands.py` (existing redirection / parse cases) |
| Hook + store | `worker_guard.permission` | `evals/test_guard.py` `GuardPermissionTest` |
| Engine pass | `engine._route_asks` | `evals/test_engine.py` `PermissionAskTest` |

## Risks

- **Wrong base (`50091f80`):** re-implementing `#890` on main duplicates work and misses `#891`, `#900`, and `#874` context.
- **Treating permission as escalation:** any path that calls `decision_escalation` owner wait or writes `decision_interrupts` for a `Permission requested for` ask regresses `#890`.
- **Deleting rows** to clear drago backlog violates acceptance; only appended answers.
- **Overlapping slices:** `#876` touches the same guard modules; take develop tip and minimal diffs.

## Plan-stage baseline

On this unchanged `50091f80` checkout (no `test_guard.py` / `test_engine.py`):

`python3 -B -m unittest evals.test_architecture` — **12 passed**, 0 failed, 0 skipped, 0 xfailed.

Full discover and `#890` tests require `origin/develop` and engine dependencies (SQLAlchemy, alembic, etc.).

## File ownership

- write (only if a step-3 failure is in that file): `scripts/guard_commands.py`, `scripts/worker_guard.py`, `scripts/decision_actions.py`, `scripts/engine.py`, `evals/test_guard_commands.py`, `evals/test_guard.py`, `evals/test_engine.py`
- read: `scripts/guard_rules.py`, `scripts/decision_escalation.py`, `scripts/trio_gate.py`, `architecture/ARCHITECTURE.md`, `docs/plans/issue-890.md`
