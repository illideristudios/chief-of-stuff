# Issue 1023 — Pin the root clone when cleanup has no `[repos]`

Cleanup deletes a finished worktree only after `layout_guards` and `git_guards` accept it. Both take `expected_common` from `trusted_commons`. That map is built only from `[repos]` (`scripts/cleanup.py` `execute`, the `trusted_commons(...)` call). Generated settings leave `[repos]` commented out (`scripts/init_workspace.py` `settings_text`), so the map is empty. The engine then cuts workers from the workspace root (`scripts/engine.py` `main`: `clone=root / selected if selected else root`). With no pin, `git_view.locate` follows a worker-written `.git` file. A decoy whose admin `gitdir` points back at that worktree, whose index matches the worktree files, and whose `HEAD` is on `origin/main` makes the unsaved and unlanded checks report nothing, and `--remove` deletes the real tree.

When `[repos]` is non-empty the engine-selected clone is already one of `settings.repos.values()`. Leave that map as it is. Pinning every configured clone is stricter than pinning only the selected one, and `evals/test_cleanup.py` `test_a_tree_redirected_to_another_repository_is_kept` already covers it.

## Change

In `scripts/cleanup.py` `execute`, build the clone list, and when it is empty and `root / ".git"` exists, append `root` before the existing `trusted_commons` call:

```python
clones = [root / path for path in settings.repos.values()]
if not clones and (root / ".git").exists():
    clones.append(root)  # engine cuts from the workspace when [repos] is omitted
commons = trusted_commons(clones)
```

`trusted_commons` already records each registered worktree from the clone's admin directory, not from the worktree's `.git` file. `commons.get(tree)` then feeds both guard calls and the re-check under `trees_lock`. No new helper, and no change to `git_trees.py`, `git_view.py`, or `engine.py`.

Use `.exists()`, the same check as `git_trees.sha_trees`, so a gitfile and a `.git` directory both count. Do not append `root` when it has no `.git`. `trusted_commons` turns a failed `locate` into `ValueError("known repository registrations are unreadable")`, `cleanup.main` does not catch `ValueError`, and the existing suite's workspace root is not a repository. Unconditional `root` fails those tests and every non-repo workspace. Do not catch that `ValueError` and continue with an empty map: an unreadable root registration must abort cleanup, the same way an unreadable `[repos]` path does.

## Tests

TDD. Add the failing cases to `evals/test_cleanup.py` first. Run `python3 -m unittest evals.test_cleanup` and see the attack case fail, then make the change above.

Fixture: a parent temp dir, `make_repo.build(parent, {"clone": "repo", "trees": "repo/trees", ...})`, cleanup `--root` = `parent / "repo"`. That root is the clone, `origin.git` stays outside it, and there is no `[repos]` table (no settings file). Do not use `clone: "."`. `make_repo` creates `origin.git` inside the directory it is given before `git add -A`, so a clone at `.` commits `origin.git/` and status no longer matches a decoy.

1. **Attack, red before the fix.** Finished worktree `done` with an uncommitted `scratch.txt`. Decoy repository whose main commit contains the same `scratch.txt` (and the same `README.md` and `.gitignore`), pushed, plus a worktree of that main. Point the decoy admin `gitdir` at `done/.git` and rewrite `done/.git` to that admin, as in `test_a_tree_redirected_to_another_repository_is_kept`. `--remove` must keep `done` with `git cannot read this tree`, leave `done/scratch.txt` and the decoy admin in place, and not call `git_view._opened` on `done`. Repeat the redirect inside `trees_lock`, as that test does: the locked re-check uses the same pin. Confirmed on this tree before the fix: that decoy makes cleanup print `removed done` and delete the worktree. A decoy that does not contain `scratch.txt` stays green for the wrong reason (`unsaved by git status --ignored`).

2. **Clean tree still removed.** Same root-as-clone layout, no redirect, `done` committed and merged. `--remove` deletes it. This fails if the new pin rejects the real common.

Leave the existing nested-clone tests alone. They have no `root/.git` and no `[repos]`, and they are the regression check that a non-repository workspace still cleans.

## Out of scope

`scripts/one_shot.py` and `scripts/audit_tasks.py` build the same `[repos]`-only map. They do not delete trees. Do not change them here. A worker can still aim those reads at a decoy when `[repos]` is empty; that is a separate issue.

Do not narrow a non-empty `[repos]` map to the one clone `engine.py` would select.

## Risks

- The pin trusts the clone's admin directory. A writer who can change that directory, rather than only the worktree `.git` file and a decoy back-pointer, is outside this fix.
- A workspace whose root has a `.git` that `locate` cannot read newly aborts the cleanup pass instead of running with an empty map. That is fail-closed, and it fails the whole engine pass (`engine.py` treats a `cleanup.execute` exception as a failed pass). Accept it. Do not fall back to `{}`.
- `one_shot` and `audit_tasks` keep the empty-map hole for non-deletion reads.

## File ownership

| path | mode |
| --- | --- |
| `scripts/cleanup.py` | write |
| `evals/test_cleanup.py` | write |
| `scripts/git_trees.py` | read |
| `scripts/git_view.py` | read |
| `scripts/engine.py` | read |
| `scripts/init_workspace.py` | read |
| `evals/make_repo.py` | read |
