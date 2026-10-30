# Issue #1016 plan: cleanup misses a container that bind-mounts a parent of the worktree

## Problem

`scripts/cleanup.py` keeps a tree when "a container binds a path in it". Both checks, the first pass (`:223`) and the re-check under the tree lock (`:238`), test only `inside(mount.resolve(), tree)`: the mount is the tree or below it. A container that bind-mounts a parent of the tree (the workspace root, the Worktrees dir) sees the tree and can write to it, yet no mount lies inside the tree, so the guard passes and `shutil.rmtree(tree)` runs.

## Decision

Put the overlap test in one helper, `binds(mounts, tree)`, true when any mount is at or below the tree **or** the tree is at or below the mount. Both call sites use it. `containers()` is unchanged: it already returns every mount source, running or stopped.

```
M := a container mount source (resolved host path)      T := the worktree (resolved path)
D := mount is at or below the tree: inside(M, T)        U := tree is at or below the mount: inside(T, M)
K := the tree is kept                                   X := the container can see or modify the tree

1.  X ↔ D ∨ U                  [issue #1016 Expected: a mount overlaps the tree in either direction]
2.  guard today: K ↔ D         [scripts/cleanup.py:223, :238]
3.  U ∧ ¬D → X ∧ ¬K            [from 1, 2: parent mount, tree is deleted while visible]
4.  guard after: K ↔ D ∨ U     [this plan, at both :223 and :238]
∴   X → K, in the first pass and in the final re-check
```

Consequence, accepted: a mount of `/` or `$HOME` keeps every tree while that container exists (stopped containers count, per `containers()`). That is the correct answer under premise 1, and it errs toward keeping, which the module's rule already prefers ("a guard that cannot be verified keeps the tree").

## Changes

1. `scripts/cleanup.py`, beside `inside` (`:94`): add

   ```python
   def binds(mounts: list[Path], tree: Path) -> bool:
       return any(inside(m, tree) or inside(tree, m) for m in (mount.resolve() for mount in mounts))
   ```

   `tree` is already resolved (the other guards compare it with `.resolve()`d paths), and `mount.resolve()` is kept as today.
2. `scripts/cleanup.py:223` and `:238`: replace `any(inside(mount.resolve(), tree) for mount in ...)` with `binds(mounts, tree)` / `binds(current_mounts, tree)`.
3. The keep message "a container binds a path in it" is false for a parent mount. Change it, at both sites, to "a container mount overlaps it". It is used only as operator text and in the keep-line log dedupe (`:269`), so no other consumer depends on it. One new log line per existing kept tree is acceptable.

Not touched: `architecture/ARCHITECTURE.md` and `compliance.md` (the architecture agent owns them), `containers()`, the other guards, any dependency.

## Tests (`evals/test_cleanup.py`, red first, in the existing container block near `:762`)

- `test_a_container_bound_to_a_parent_of_the_tree_keeps_it`: plant `docker` as in `test_a_container_bound_inside_the_tree_keeps_it`, with the mount `self.trees` (the Worktrees dir); `--remove` keeps `done`, the message contains "container", and the tree dir remains. Fails on today's code.
- `test_a_container_mount_of_a_parent_that_appears_before_the_delete_keeps_the_tree`: copy `test_a_container_mount_that_appears_before_the_delete_keeps_the_tree` (`:775`) with `mounts.return_value = [tree.parent]`, so the first check passes and only the final re-check at `:238` can catch it. Asserts the new message and that the tree remains. Fails on today's code.
- Update `:788` to the new message.
- Control: a mount of a sibling tree (`self.trees / "other"`) does **not** keep `done`, so the check is overlap and not "anything under Worktrees". Reuse an existing fixture sibling if one exists; otherwise a plain path that is not a prefix of `done`, such as `self.trees / "done-not"`, which also guards against a string-prefix implementation.

Run: `python3 -m unittest evals.test_cleanup` (the whole suite: `python3 -m unittest discover -s evals -p 'test_*.py'`). The `require_green_tests` rule needs passed > 0 with failed, skipped and xfailed all zero.

## Risks

- A broad mount (`/`, `$HOME`) keeps every tree, so trees pile up while such a container (including a stopped one) exists. Mitigation: none in this issue. The keep line names the cause, and removing the container releases the trees.
- Symlinked mount sources are handled by `mount.resolve()`. A mount source that no longer exists resolves lexically and can only match by path, which is the same as today.
- Message change shifts one operator-visible string. No consumer found beyond the tests.
- Compliance row C21 (`compliance.md`) describes the guard as depending on the installed runtime; this fix does not change that, and the architecture agent owns that row.

## File ownership (proposed for the implementation stage)

- `scripts/cleanup.py`: write
- `evals/test_cleanup.py`: write
