# Issue #717: teardown before worktree cleanup

## Scope and prerequisite

Implement cleanup of a workspace stack before removing its finished, engine-owned linked worktree. Change only `scripts/cleanup.py` and its existing regression suite, `evals/test_cleanup.py`, during implementation. Reuse `shell_setup.teardown`; do not add a shell runner, change settings, remove guards, or expand orphan-stack auditing.

PR [#713](https://github.com/illideristudios/chief-of-stuff/pull/713) merged into `develop` on 2026-10-17 at 13:54:48 UTC, merge commit `2e7228c53e5d65be611719d4c6fc6e0d17ee2f22`. The dependency is satisfied. This planning worktree starts at `50091f80bed51d55e16666f830fa37b9b0a4c1b5`, before the engine cleanup and teardown modules were added. Implementation must start from a base containing #713 and the existing cleanup guards; do not recreate those modules from this older checkout. Source was inspected read-only through Git at the dependency merge and local `origin/develop` (`956a5ef6aa306b27685e0b3a25f2b3cb9b9e035e`). Reconcile newer guard changes before implementation, especially verified completion evidence for merged owners.

This stage writes only this plan. No implementation or failing regression is introduced in the planning stage.

## Existing behavior and contracts

- `cleanup.execute` loads workspace settings and ownership, takes a single `containers()` snapshot before iterating over discovered trees, then evaluates layout, owners, worker/process claims, container mounts, and Git state. A retained mount currently prevents entry into removal, so adding teardown only immediately before deletion would never recover the stranded-stack case.
- Removal holds `trees_lock`, repeats layout, claims, Git and container checks, refreshes store ownership, and deletes the tree followed by its Git admin directory only if all checks pass. Preserve these checks and completion evidence. Unknown evidence remains a refusal.
- `shell_setup.teardown(root, tree, tree.name, settings.workers.teardown)` returns a list of diagnostic strings. Empty command returns no warnings. Nonzero exits and shell/timeout errors become bounded diagnostics.
- The helper executes commands from the trusted workspace `root`, exposing the target tree through `CHIEF_OF_STUFF_WORKTREE` and its name through `CHIEF_OF_STUFF_WORKER`. Although the issue describes running teardown “in” a worktree, pass that tree to the helper without changing the helper's trusted execution directory or executing contributor scripts from it.
- Architecture §4 classifies both modules as Frameworks and Drivers and reserves workspace teardown execution to `shell_setup.py`. A direct import and call from cleanup is permitted. `teardown_list` is outside this change; preserve it as the audit safety-net contract for absent worktrees.

## Implementation sequence

1. **Red regression first.** Extend the existing real-repository/store fixture in `evals/test_cleanup.py`. Configure teardown through a workspace TOML file and `Settings:` declaration. Model a stack with mutable mounted paths: the shared teardown mock clears this tree's mount, and `containers()` reads the current state. Run cleanup with `--remove`. Assert the exact teardown arguments, teardown before the relevant mount query, removal of the tree and admin directory, and a removal log for its owning item. Confirm this test fails against the implementation base before editing cleanup.
2. **Gate the side effect.** Import `shell_setup` in cleanup. Teardown runs only during `--remove`, with a nonempty command, for a validated linked tree whose refreshed store owners are finished and whose existing non-container guards all pass. Never stop a stack for a preview, an unowned/unfinished tree, a live or unverifiable worker/process, unsafe Git state, locked/invalid layout, or missing completion evidence.
3. **Make mounted candidates reachable.** For configured removal, defer the initial container guard until after teardown; a pre-teardown mount must not keep the candidate out of the locked removal path. Keep the unset-command path's current behavior. Do not reuse the global pre-teardown snapshot for a configured candidate. Any existing initial query retained for the unset/preview paths must have no authority over a tree whose teardown subsequently ran.
4. **Keep the lock through the operation.** Inside the existing `trees_lock`, refresh ownership and all non-container removal guards before invoking teardown, so a worker registering after the first pass prevents teardown as well as deletion. Run the helper once per eligible tree per invocation, with the exact call above. Keep the lock through teardown, the fresh mount check, final validations, and deletion; do not introduce a second teardown during the final checks.
5. **Check fresh facts and report errors.** Always query `containers()` after a teardown attempt, including one returning warnings. Unknown mounts or any remaining mount keep the tree. Collect teardown diagnostics as keep reasons, mark the operation failed, and include them in the existing per-tree output and owning-item log. This conservatively satisfies the explicit acceptance that a failed teardown leaves the tree, including a failure that happens after releasing mounts. Still perform the mount check; teardown never substitutes for it. A successful command with a surviving mount also keeps the tree.
6. **Revalidate and remove.** Retain the existing final owner, completion, claims, layout, Git and container protections immediately before removal. Re-read guards affected by the arbitrary teardown command rather than treating pre-command checks as permission to delete. Preserve current partial-removal handling, logging/deduplication, and tree/admin-directory deletion order. A refused or failed tree must not prevent independent eligible trees from being evaluated.

Use the existing execution function and reporting format. Add no new public abstraction, command option, dependency, setting, retry loop, or parallel teardown orchestration.

## Regression matrix

All new cleanup cases belong in `evals/test_cleanup.py`. Patch the shared helper for orchestration tests; use existing shell-helper coverage for actual command execution, timeouts and environment handling. No real Docker/Podman stack is required.

| Case | Required assertion |
| --- | --- |
| Finished tree with mounted stack; teardown clears mount | Red case above becomes green; exact helper call occurs once before fresh mount verification; tree/admin removed and removal logged. |
| Teardown returns a diagnostic; mount persists | Tree/admin remain, output and store log name the teardown error and mount refusal; cleanup returns failure. |
| Teardown returns a diagnostic after clearing mount | Tree remains and error is visible; mount probe still runs. |
| Successful teardown leaves a mount, or mount query is unknown | Tree remains under the existing container guard. |
| No teardown setting, and explicitly empty setting | Helper is never called; existing preview/removal and mount-refusal behavior remains unchanged. |
| Preview with configured teardown | No helper call, no deletion or store-log write; current mount refusal is shown without stopping the stack. |
| Multiple eligible trees and mounted stacks | Each helper gets its own tree/name once; fresh checks reflect only stacks actually released; one failure does not stop another tree's cleanup. |
| Unowned/unfinished owner, live/unverifiable claim, unsafe Git state, locked/invalid layout, or missing completion evidence | No teardown and no deletion. Use representative parameterized/subtest cases alongside existing guard coverage. |
| Worker/owner/unsafe layout appears between initial pass and lock | Locked recheck prevents teardown and deletion. |
| Teardown changes Git state/layout or ownership; mount appears at final verification | Final guards retain the tree; existing race tests continue to pass. |

Verify that the shared-helper error contract covers nonzero exit, missing executable/shell errors, and timeout through the existing `evals/test_worker_teardown.py` tests rather than copying the runner or its tests into cleanup.

## Validation and delivery

On the implementation base, first run the new mounted-stack test alone and record its expected failure. After the implementation, run:

```text
python3 -m unittest evals.test_cleanup evals.test_worker_teardown evals.test_architecture
python3 -m unittest discover -s evals -p 'test_*.py'
```

Use the repository's configured Python environment and dependencies. Report exact counts from the final suite, including errors as failures and explicit skipped/expected-failure counts; do not infer implementation coverage from this plan's baseline checks. Inspect the diff for only the approved implementation files and commit the red regression and minimal fix in small reviewable steps. Never merge or remove a worktree.

Planning validation ran `python3 -m unittest evals.test_shell_setup evals.test_settings evals.test_tree_state evals.test_architecture` through a counting unittest runner with bytecode writes disabled: **85 passed, 0 failed, 0 skipped, 0 xfailed**. These are baseline checks available in this older worktree. The cleanup and worker-teardown suites do not exist in this checkout, so their execution is reserved for implementation on the required base.

## Risks and controls

- **Stale mount state:** the current global snapshot can strand a successfully stopped stack or miss changes across trees. Require per-candidate post-teardown reads and preserve final verification.
- **Stopping an active stack:** holding the existing launch/registration lock and refreshing ownership/claims before teardown prevents racing a compliant new worker. Keep every finding-27 and completion guard; teardown is never authority to bypass one.
- **Arbitrary command effects:** teardown can change files or fail after partially stopping a stack. Revalidate deletion facts afterward, retain the tree on diagnostics, and surface the helper's bounded error rather than hiding it behind a generic mount message.
- **Lock duration:** a teardown command can hold up launches while cleanup owns the lock. Reuse the shared runner's bounded command timeout; do not add unbounded waiting or a second timeout implementation.
- **Trust boundary:** the target tree is worker-controlled. Preserve trusted-root execution and pass the tree only through the established helper API.
- **Preview compatibility:** a read-only preview may continue to label a mounted tree as kept even though configured removal could stop the stack. Do not make preview destructive to predict that result.
- **Older planning base:** port the plan to a branch containing #713 and current cleanup safety changes. This plan authorizes no checkout reset, merge, or changes outside its stage scope.

## Proposed implementation file ownership

| Path | Mode | Purpose |
| --- | --- | --- |
| `scripts/cleanup.py` | write | Invoke shared teardown at the guarded removal boundary and refresh mount state/reporting. |
| `evals/test_cleanup.py` | write | Red case, teardown failure/no-setting compatibility, ordering and guard/race regressions. |
| `scripts/shell_setup.py` | read | Shared execution and diagnostic contract; remains the only teardown runner. |
| `scripts/settings.py` | read | Existing workers teardown settings and defaults. |
| `scripts/tree_state.py` | read | Existing unsaved/unlanded-work protections. |
| `scripts/one_shot.py` | read | Existing tree-lock contract. |
| `scripts/git_view.py` | read | Existing linked-tree layout and safe Git inspection contract. |
| `evals/test_worker_teardown.py` | read | Existing shell execution, timeout and trusted-root regression coverage. |
| `evals/test_architecture.py` | read | Existing dependency checks. |
| `architecture/ARCHITECTURE.md` | read | §4 driver boundary and teardown execution rule. |
