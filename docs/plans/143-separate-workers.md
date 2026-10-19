# Plan: tests and implementation come from separate workers

Issue: github:illideristudios/chief-of-stuff#143

A task's tests and its implementation are separate commits, written by separate workers. One author who writes both can bend either to fit the other.

```
T := the test stage has its own one-shot
R := that one-shot's commit contains only test paths and the new tests are red
I := the implement stage is a later one-shot on the same branch
F := I's diff shares a path with the test commit
H := the launcher routes the run to human review

T ∧ R                          [test stage ends red, and only tests]
I ∧ ¬(same launch as T)        [a different worker]
F → H                          [the refusal]
∴ the two stages are two launches, and a shared path stops implement
```

## Where the change lands

This worktree is `work/issue-143-plan-1` off `origin/main` at `50091f80` (0.128.67). The engine that dispatched this plan, and the launcher the acceptance names, are on `origin/develop` at `6edbb2cb` (0.129.98), 955 commits ahead. `assets/policy.toml`, `scripts/worker.py`, `scripts/reconcile.py`, and `scripts/engine_facts.py` are on develop.

The first implementation step rebases this branch onto `origin/develop` and keeps this plan commit. Later stages reuse this worktree and this branch name: `worker.launch` keeps `worktree` and `work_branch` for every non-review stage. Code edits belong on that rebased tree.

## Current behavior

On develop, `assets/policy.toml` `[workflows.standard]` runs `plan` to `implement` with gate `plan`, then `review`, `fix`, `review`, and `merge` with gate `merge`. There is no `test` stage. `[stages.plan]` and `[stages.implement]` both use persona `implementer`. `ISSUE_JOBS["implement"]` in `scripts/dispatch_prompt.py` is "Implement the issue and verify it with tests." One launch writes both.

`scripts/worker.py` `TASK_CLASS` maps `plan` to `deep`, and `implement` and `fix` to `implement`. A later non-review stage reuses the same worktree, with a new `starting_head`.

`reconcile.suite_holds` turns a `done` report into human review when `[workers] require_green_tests` is on, unless `passed > 0` and `failed`, `skipped`, and `xfailed` are all zero. A test stage that ends red would be held.

`docs/kanban.md` still lists `test` before `implement` in the old `[lanes]` example. The 1.0 workflow graph replaced those lanes. The stage names survive in that document, and the graph gives both jobs to the implement launch.

## Implementation

Add `[stages.test]` with `persona = "implementer"`. Point `plan` at `test` with `gate = "plan"`, and `test` at `implement` with no gate. Leave the rest of the graph. The same persona is two launches: each has its own run directory (`{issue}-test-{attempt}` and `{issue}-implement-{attempt}`), session, and `starting_head`. A new persona is a larger change than the acceptance asks for.

Set `TASK_CLASS["test"] = "test"`. With no `[models.test]` rotation, `_model` already falls back to the persona's model. This workspace's `deep` and `implement` rotations are the same list with `pick = "random"`, so a separate class does not by itself pick a different model. The acceptance asks for a different worker. A `[models.test]` rotation can come later.

Add a `test` job in `ISSUE_JOBS`. It tells the worker to commit only failing tests, to leave the behavior unimplemented, and to leave the pull request unopened. `compose_issue` attaches the red rule on the test stage and leaves the require-green rule off that stage. Implement, fix, and review keep the green rule.

Put one test-path predicate next to `suite_holds` in `scripts/reconcile.py`. A path is a test path when a component equals `test`, `tests`, `__tests__`, `evals`, `spec`, or `specs`, or the filename matches `test_*`, `*_test.*`, `*.test.*`, or `*.spec.*`. Equality is the whole component, so `contest` is not a test path. Fixture files under `evals/cases/.../fixture/` are test paths, which is what the test commit should hold.

Accept a test-stage `done` only when every committed path and every uncommitted path is a test path, that set is non-empty, the four suite counts are known, `failed > 0`, and `skipped` and `xfailed` are 0. `passed` may be 0. A green suite, an unknown count, an empty diff, or a non-test path is human review. Record the test range on the run: the first test attempt's `starting_head`, and the HEAD at the end of the last done test attempt.

On implement completion, refuse the run when any path in that run's diff shares a path with `git diff --name-only` across the recorded test range. The implement diff is the commits since that run's `starting_head`, plus uncommitted paths. A rename contributes both sides. A missing or unreadable test range is human review. The path check runs before the suite gate. Refusal sets human review and leaves the stage on implement.

Call the check from `reconcile.completion_verdict`, which `engine._finish_worker` uses, and from `one_shot.reconcile`, which the coordinator launcher uses. `engine_facts.gather` puts both path sets on `Facts`, because `completion_verdict` does not see the store.

Keep the implement worker on the same worktree and branch. A new tree would drop the test commits. `worker.launch` already reuses `data["worktree"]` for a non-review stage.

`evals/test_settings.py` `WorkflowStagesTest.test_shipped_policy_stages_are_sorted_and_complete` expects `["fix", "implement", "merge", "plan", "review"]`. The sorted list gains `"test"`.

## Tests

The failing tests come first, in `evals/test_stage_split.py`, plus the shipped-stage assertion above. The engine's planner (`reconcile`) is the dispatcher the acceptance calls the coordinator. An `evals/cases/` harness case would grade `agents/chief-of-stuff.md`, and that agent does not choose these stages. The agent file stays as it is.

- The shipped graph is `plan` → `test` with gate `plan`, then `test` → `implement` with no gate. There is no edge from `plan` straight to `implement`.
- With the plan gate passed and no test segment, reconcile returns one `Dispatch` whose stage is `test`.
- With that test segment `done`, reconcile returns one `Dispatch` whose stage is `implement`. The two results are separate actions.
- A test completion with only test paths, `failed > 0`, and zero skipped and xfailed is accepted while `require_green` is on.
- A test completion with a non-test path, an empty diff, an unknown count, or `failed == 0` is human review.
- An implement completion whose diff shares a path with the test range is human review. A disjoint diff is not held for that reason. Implement still has to satisfy the green suite rule.

## Risks

The rebase onto `origin/develop` happens before any code edit. This plan commit is the commit that moves.

The test stage is the exception to `require_green_tests`. Implement, fix, and review stay on the green rule. A red implement suite is still human review.

An unmapped `test` stage makes `Forge.mark_for_stage` return an empty label, so the issue can lose its stage label. The workspace `chief-of-stuff.toml` is gitignored. Map `test` onto the implement column before the first test dispatch. Shared columns keep the stored stage. A distinct `stage: test` label is optional and has to exist on the forge before the map names it.

The fix stage may still edit a test file. The acceptance names the implement run. A fix that cannot touch a bad test cannot carry out a review finding about that test.

An implement worker can add a new test path the test range does not contain. The check is that intersection. Widening it to every future test path is a later change.

This workspace will often give both launches the same model. The split that lands here is two launches and two commits.

A repository that keeps product source under `evals/` would treat that source as a test path and lock it to the test worker.

The plan gate stays on the edge out of `plan`. A gate between test and implement would put a person between the red tests and the implementation.

The implementation scope below is the proposal for approval after this plan. This plan file stays the plan stage's file and stays outside that scope.

## File ownership

- write: `assets/policy.toml`
- write: `scripts/dispatch_prompt.py`
- write: `scripts/reconcile.py`
- write: `scripts/worker.py`
- write: `scripts/one_shot.py`
- write: `scripts/engine_facts.py`
- write: `evals/test_settings.py`
- write: `evals/test_stage_split.py`

## Out of scope

A new persona, a required model rotation, a second worktree, coordinator agent prose, the pull request (the implement stage opens it once the suite is green), and a path check on the fix stage.
