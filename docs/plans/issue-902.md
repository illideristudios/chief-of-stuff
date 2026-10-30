# Issue 902 — source page diff when the workspace is the clone

## Problem

`write()` always passes the workspace root and the pages directory. `render` draws a diff only when `settings.graph` is also set (`if graph and pages and root`), and otherwise prints "The diff needs the clone". Engine workspaces have no `[graph]` table, and the workspace root is the repository, so the diff never renders.

The file list always links `#` + `anchor(path)`. A diff table with that id is emitted only when the file has rows. With no diff, every link goes nowhere. The task page's `open source & diff` links use the same ids; they start resolving once those tables exist.

The issue text points at about line 305. That line is the table id. The gate is the `if graph and pages and root` in `render`.

## Change

In `scripts/source_page.py` `render`, keep `diff()` (and so `module_graph.fetch`). Do not add a second git call, a setting, or a `[graph]` table.

```python
clone = root / graph.clone if graph else root
if pages and root and (graph or (clone / ".git").exists()):
    try:
        base, text = diff(change, clone, pages)
        ...
    except (OSError, ValueError) as e:
        note = f"The diff is unavailable: {escape(str(e))}"
else:
    note = "The diff needs the clone: the settings have no <code>[graph]</code> table."
```

`root` is checked before `.git`, so today's four-argument `render` (no pages, no root) still takes the note branch.

In the file-list loop, link a path only when that file has rows. That is the same condition as emitting its table, so the href exists only when the id does. A path with no rows is escaped text, not an `<a>`.

Update the two contract sentences that say a missing `[graph]` table never diffs: the module docstring in `scripts/source_page.py`, and the opening docstring of `evals/test_source_page.py`.

## Tests

TDD. Both cases go in `evals/test_source_page.py` and are red before the `render` edit.

1. Extend `test_without_a_graph_table_the_files_are_listed_and_the_clone_is_named`. Leave `render(graph=False)` as the four-argument call. Assert the `<ul class="files">` block contains no `href="#`. The note and the empty diff tables stay as they are. This is red today because that list links `#f-` ids the page does not define.

2. Add `test_a_workspace_repo_without_a_graph_table_shows_its_diff`. Call `render(109, TRACKERS, self.sources(), NOW, None, pages, self.root / "repos" / "app")`. `pages` is a new directory, not `self.root / "pages"`. Assert the three fixture paths each have a diff table, each file-list `href` fragment equals that table's id, and the page does not say the diff needs `[graph]`.

The fresh pages directory is required. `diff()` caches `pages/.diffs/<head>.diff` by head only. The class's other tests write that cache under `self.root / "pages"`. A shared pages directory lets this test pass without reading the clone.

Then run:

```
python3 -m unittest evals.test_source_page evals.test_source_split evals.test_source_notes
```

`test_source_split` and `test_source_notes` keep a configured clone. They must stay green.

## Risks

- The first view of a head runs `git fetch` in the workspace, through the existing pinned `module_graph.fetch`. It updates remote-tracking refs and does not touch the index or worktree. A lock, a missing `origin`, or no network leaves the "unavailable" note and a file list with no anchors. Later views of the same head read the cache.
- A workspace that is a git repo of a different project than the change shows "unavailable" instead of "needs the clone". The reported engine workspaces are the change's own clone.
- A linked worktree stores `.git` as a file. `exists()` is true and `git -C` fetches it. The fixture clone is a normal repo, so that shape is not executed.
- Names in the no-diff list drop the monospace rule, which targets `ul.files a`. Left that way.
- Task-page `#f-` links still dangle when no diff can be drawn. For a repo workspace the diff is drawn, which is the crawl's finding.

## Out of scope

- No `[graph]` entries in engine `chief-of-stuff.toml` files. The next workspace would hit the same page.
- No change to `issue_page._files`, `module_graph.fetch`, or settings.
- No local `merge-base` before fetch. `diff()` already skips fetch once the head is cached.

## File ownership

| path | mode |
|---|---|
| `scripts/source_page.py` | write |
| `evals/test_source_page.py` | write |
| `scripts/module_graph.py` | read |
| `scripts/issue_page.py` | read |

```
G := [graph] names a clone
R := the root passed to render has .git
P := pages and root were both passed
S := fetch and git diff succeed
D := the page shows that diff
L := the file list links #f- anchors

1. P ∧ G ∧ S → D                         unchanged clone: root / graph.clone
2. P ∧ ¬G ∧ R ∧ S → D                    no table, workspace is the repo
3. ¬P ∨ (P ∧ ¬G ∧ ¬R) → ¬D ∧ note       today's "needs the clone" note
4. ¬D → ¬L                              no table, no dead anchor
∴  (P ∧ ¬G ∧ R ∧ S → D) ∧ (¬D → ¬L)
```

Show the workspace repo's diff when settings name no other clone and that repo can produce one. Otherwise keep the note, and never link a file to an anchor the page does not render.
