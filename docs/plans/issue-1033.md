# Issue 1033 — Cursor `done` is held for the engine's own channel file

A Cursor run that reports `done` on an otherwise clean tree is held for human review. The segment stays `done` with `required_human_review = 0`. The hold is real: the audit counts `.cursor/mcp.json` as uncommitted work.

## Cause

`host_adapter._register_cursor` writes `.cursor/mcp.json` and `_exclude` appends `/.cursor/mcp.json` to the repository's real `info/exclude` (`git rev-parse --git-path info/exclude`, the common dir for a linked worktree). Plain `git status --porcelain` in that tree is empty. `git check-ignore -v` names that exclude line.

The audit does not read that file. `git_view._opened` builds a throwaway `GIT_DIR` and never creates `info/exclude`. `_config` sets `core.excludesFile` to `/dev/null`, which disables the global excludes file and leaves `info/exclude` as a separate file. `git_trees.read_state` counts `git status --porcelain` lines through that view, so the channel file is one untracked directory: `?? .cursor/`.

Measured on a linked worktree whose only extra file was `.cursor/mcp.json`, with `/.cursor/mcp.json` in the real exclude:

| reader | `status --porcelain` | `status --porcelain --ignored` |
|---|---|---|
| real git | empty | `!! .cursor/` |
| `git_view.run` today | `?? .cursor/` | `?? .cursor/` |

`?? .cursor/` is one dirty line and one unsaved path. That is the keep line `1 unsaved by git status --ignored (.cursor/); 1 uncommitted`.

Collection then holds without flipping the segment outcome. Implement has a successor, so `landing_required` is false. `completion.abandoned` returns `Verdict("done", "1 uncommitted", human_review=True)` when `dirty > 0` and `unpushed == 0`. `engine._finish_worker` keeps that outcome and reason. `engine._close_segment` calls `set_human_review` and `forge.add_human_hold`, then `end_segment` with outcome `done`. `store.end_segment` sets `required_human_review` only when the outcome is `human_review`. The next pass waits with `human review` because the item flag is set (`reconcile._item`).

```
E := the engine excluded /.cursor/mcp.json in the real info/exclude
V := the audit view applies that same pattern
P := view porcelain lists the channel (?? .cursor/)
D := read_state.dirty counts that line
L := the stage has a successor
U := unpushed is 0
H := the item is held and the segment stays done

1. E ∧ ¬V                         [view GIT_DIR has no info/exclude]
2. E ∧ ¬V → P                     [table above]
3. P → D = 1                      [git_trees.read_state]
4. D > 0 ∧ L ∧ U → H              [completion.abandoned; engine._close_segment]
∴ a cursor done on a readable, otherwise clean, pushed tree is held
```

`required_human_review = 0` rules out the suite hold and `host_adapter.moved_outside_engine`. Both store outcome `human_review`.

The same view feeds `cleanup.git_guards`, `engine_facts._tree_facts`, and `audit_tasks`' porcelain count. Fixing the view clears every uncommitted count. It does not clear the unsaved half: once the file is ignored, `--ignored` still prints `!! .cursor/`, and `tree_state.unsaved` keeps that path. `--untracked-files=all` prints `!! .cursor/mcp.json` instead.

## Fix

One engine-authored pattern, applied in the view. The real exclude write stays, so the worker's own git still ignores the file.

1. In `tree_state.py`, name the pattern once: `CURSOR_CHANNEL_EXCLUDE = "/.cursor/mcp.json"`. The porcelain path is that string without the leading slash. Add `cursor_channel_only(porcelain: str) -> bool`, true only when the only non-empty line is exactly `!! .cursor/mcp.json`.
2. In `git_view._opened`, write `<view>/info/exclude` containing only that pattern and a trailing newline. Leave `core.excludesFile = /dev/null`. Set no `GIT_COMMON_DIR` and do not read the repository's `info/exclude`.
3. In `host_adapter._register_cursor`, pass that constant to `_exclude`. Replace the comment: the real exclude is what the worker's git reads; the audit view authors its own and does not copy this file.
4. In `cleanup.git_guards`, when `unsaved` returns `.cursor/` or `.cursor`, run `git status --porcelain --ignored --untracked-files=all -- .cursor` through the same view. Drop those lost entries only when `cursor_channel_only` is true. Any other status, an empty result, or a failed read leaves the path in the lost set.

`completion.py`, `reconcile.py`, `engine.py`, and `read_state` stay as they are. A real dirty count must still hold.

## Tests first

Behaviour is red before `git_view` or `cleanup` changes. `python3 -m unittest` on the two new cases, then the edit.

`evals/test_git_trees.py`, on a linked worktree from `ReadStateTest.tree`, with the exclude line appended to the common `info/exclude`:

- Only `.cursor/mcp.json` present: `read_state(tree).dirty == 0`. Today it is 1.
- Also `secret.txt`, and `secret.txt` added to the real exclude, and `.cursor/other.txt` untracked: `dirty == 2`. The worker's exclude line must not hide `secret.txt`. A nested `pkg/.cursor/mcp.json` counts too.
- `pkg/.cursor/mcp.json` alone, with no root channel file: `dirty == 1`. The pattern is anchored to the work-tree root.

`evals/test_cleanup.py`, on the finished `done` tree:

- Write only `.cursor/mcp.json` and append the exclude line. The preview still lists that tree as removable, with no `unsaved` and no `uncommitted` in its keep line. Today it is kept for both.
- A sibling file `.cursor/other.txt` keeps the tree. A second ignored file under `.cursor/` (a worktree `.gitignore` of `*` plus `notes.md`) keeps the tree: `cursor_channel_only` is false for two lines, and `unsaved` does not grow a `.cursor/` exemption.

`evals/test_tree_state.py`: `cursor_channel_only` is true for `!! .cursor/mcp.json` and false for empty, `?? .cursor/mcp.json`, `!! .cursor/`, and two lines. `evals/test_host_adapter.py` already asserts the real exclude line and a clean worker `git status`; it stays green when `_exclude` takes the constant.

## Risks

Copying the repository `info/exclude` into the view would hide whatever a worker appends. The `secret.txt` case is the lock against that.

Exempting `!! .cursor/` inside `unsaved`, or ignoring `/.cursor/`, would drop worker files that happen to live there. Finding 27 was a lost ignored snapshot. The directory form stays unsaved unless the pathspec status is exactly the one ignored channel file.

`status.showUntrackedFiles=all` in the view config would expand `node_modules/` into one unsaved line per file. The pathspec read is only for `.cursor` and only after the collapsed directory is already lost.

`init_workspace.ENGINE_STATE` has the same view blindness and is out of scope. Those names are not written into a worker tree.

No architecture edit. `git_view` and `host_adapter` importing `tree_state` points inward. `architecture/ARCHITECTURE.md` does not mention this exclude.

## Implementation file ownership

- write: `scripts/tree_state.py`
- write: `scripts/git_view.py`
- write: `scripts/host_adapter.py`
- write: `scripts/cleanup.py`
- write: `evals/test_git_trees.py`
- write: `evals/test_cleanup.py`
- write: `evals/test_tree_state.py`
- read: `scripts/git_trees.py`
- read: `scripts/completion.py`
- read: `scripts/engine.py`
- read: `scripts/reconcile.py`
- read: `scripts/engine_facts.py`
- read: `scripts/audit_tasks.py`
- read: `evals/test_host_adapter.py`
- read: `evals/test_completion.py`
- read: `evals/test_architecture.py`
