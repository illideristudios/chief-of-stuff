# Issue 75: least-conflict merge order

Plan for a read-only entry point that proposes a merge order for open pull requests and merge requests. Nothing here is built yet. Choices below are the plan's calls.

Issue: `illideristudios/chief-of-stuff#75`. The coordinator already trial-merged seven merge requests by hand (`glab api merge_requests?state=opened`, `/diffs`, fetch, `git merge-tree --write-tree --name-only` on overlapping pairs). The board's merge panel is a coarser stand-in: `scripts/render_board.py` `merge_order` sorts by approval, pipeline, then filename overlap, and its comment names this issue.

## Outcome

`chief-of-stuff merge-order --root R [--repo owner/name]` prints the open changes on the workspace's configured forge, the files each one changes, a merge order, and the overlapping pairs that conflict.

```
list open changes and their files
trial-merge a pair → the two file sets overlap and both lists are complete
¬overlap(pair) → ¬trial-merge(pair)
fetch is allowed; merge, push, and moving HEAD are not
```

Non-overlapping changes come first, then chains whose overlapping pairs trial-merged clean, then the remaining changes. Each conflicting pair is flagged with the paths `git merge-tree` named. A pairwise clean result is the acceptance check. It does not prove that merging the whole chain, one after another onto the base, stays clean.

## Command

Same shape as `merge-ready`: `--root` is the workspace that holds `CLAUDE.md`, `--repo` defaults to the `Backlog:` project. GitHub and GitLab both, chosen the way `forge_review.forge` already chooses them. Marks stay `#` on GitHub and `!` on GitLab.

Exit 2 when the workspace has no `Backlog:` line, the root is not a checkout, or that checkout's `origin` is not the backlog project. Exit 1 when the open set cannot be read in full. Exit 0 when the report is complete, including a pair the trial merge could not run: that pair is an `unverified` line, not a silent omission.

Register it in `chief_of_stuff.COMMANDS` as `merge-order` → `scripts/merge_order.py`.

## What it reads

One module, `scripts/merge_order.py`, classified with `merge_ready.py` (Use Cases in `architecture/ARCHITECTURE.md` section 4, and `DRAWS_NOTHING` in `evals/test_architecture.py`). It imports `backlog`, `forge_review`, and `git_trees`. It does not import `board_sources` or `render_board`, and it does not read the page cache. Forge calls take the same injected `gh` and `call` hooks `merge_ready.main` takes, plus an injected git runner, so tests never hit a network.

**GitHub.** `gh pr list -R <repo> --state open --limit 100 --json number,title,url,baseRefName,headRefOid`. Then, for each pull request, `gh api repos/<repo>/pulls/<n>/files?per_page=100&page=<k>` until a short page. A file contributes `filename` and, when present, `previous_filename`, so a rename overlaps either path. Paginate in a loop, the way `backlog.py` does, capped at `backlog.MAX_PAGES`. Do not use `gh api --paginate`: the cap has to be visible to the test.

**GitLab.** `GET <api>/merge_requests?state=opened&per_page=100&page=<k>` until a short page (iid, title, web_url, sha, target_branch). Then `GET <api>/merge_requests/<iid>/diffs?per_page=100&page=<k>`. Each diff contributes `old_path` and `new_path` when they are non-empty.

A list or a file page that fills `MAX_PAGES` is a failed read (exit 1), not an empty or partial order. One hundred open changes that end on a short next page are complete. Drafts are open, so they are included.

A change whose file pages hit the cap is printed with the paths that were read and the line `unverified <ref> files truncated`. It is left out of overlap, out of every trial merge, and out of the order line. Treating a cut-off list as disjoint would publish a false "non-overlapping".

## Ordering

Group by base branch. Compare nothing across bases: two changes into different bases are not a pair. Print one block per base, bases in lexicographic order, a blank line between blocks.

Inside one base, two changes overlap when their path sets intersect and both file lists are complete. Isolated changes (no overlap edge) are the non-overlapping prefix, in ascending number.

Trial-merge each remaining unordered pair once:

`git merge-tree --write-tree --name-only --no-messages -z --end-of-options <oid-a> <oid-b>`

Checked on git 2.54: exit 0 writes one NUL-terminated tree id and no paths; exit 1 writes the tree id, then the conflicted paths, NUL-terminated. Without `--no-messages` the conflict prose is on stdout after a blank line and would be read as paths. Any other exit, timeout, or a fetched oid that is not the forge's head is `unverified`, not a conflict and not a clean pair.

The conflict-free graph is the overlap edges that exited 0. Each connected component is one chain:

1. Start at the lowest number in the component.
2. Append the lowest-numbered conflict-free neighbor not yet in the chain.
3. When no neighbor remains, append the lowest-numbered remaining member of the component.

Chains are ordered by their starting number. Changes that overlap someone but sit on no clean edge come last, in ascending number. Every placed change appears on the `order` line exactly once.

```
T := the file list was read in full
O := the change overlaps another open change into the same base
E := every overlap edge of the change exited 0

1. non-overlapping ≡ T ∧ ¬O          printed first, by number
2. chain member    ≡ T ∧ O ∧ E       then, by the walk above
3. remainder       ≡ T ∧ O ∧ ¬E      then, by number
4. truncated       ≡ ¬T               not on the order line
∴ ¬T → ¬(non-overlapping)
```

A component that is not a path still becomes one sequence. The walk is deterministic; it is not a claim that consecutive entries are the only overlapping pair.

## Git, and nothing else

The checkout is `--root` when it is a git work tree. Refuse unless `origin`'s URL contains the backlog's `owner/repo` or GitLab project path. Do not consult `[graph] clone` and do not walk `Worktrees:`.

Fetch only heads that belong to at least one trial-merge pair. Destination refs are explicit, so several fetches do not depend on `FETCH_HEAD`:

- GitHub: `+refs/pull/<n>/head:refs/remotes/origin/pr/<n>`
- GitLab: `+refs/merge-requests/<n>/head:refs/remotes/origin/mr/<n>`

That is the ref `scripts/module_graph.py` already fetches (`pull/<n>/head`, `refs/merge-requests/<n>/head`), stored on a remote-tracking name instead of left only in `FETCH_HEAD`. It works for a fork head. After the fetch, the stored oid must equal the forge's head oid.

Run those git calls the way `git_trees.fetch_branch` does, not through `git_view`: `GIT_WRITE_PINS`, `write_env()`, `-C` the root, `--no-tags`, no shell. `git_view`'s allowlist stays as it is. `evals/test_git_view.py` lists `fetch` and `push` as disallowed, and `merge-tree` is not a read there. Adding either would weaken the view.

Allowed writes, and no others: those remote-tracking refs, the objects the fetch downloads, `FETCH_HEAD` (git updates it on every fetch, as `fetch_branch` already documents), and the tree object `merge-tree --write-tree` stores. The command must not run `merge`, `push`, `checkout`, `switch`, `reset`, `rebase`, or `commit`, and must not move `HEAD` or the index. Timeouts: 60s for a fetch, 30s for a merge-tree. A `cannot lock ref` fetch is retried once, matching `fetch_branch`.

## Output

Paths under a change are unique and sorted. Titles stay on the change's first line; a newline in a title is dropped. Conflict paths are indented under the pair, one path per line, sorted, so a path may contain spaces.

```
base main
#12 Add the reader
  docs/reader.md
#15 Share the writer
  scripts/writer.py
#20 Extend the writer
  scripts/extra.py
  scripts/writer.py
#18 Replace the writer
  scripts/writer.py
order: #12 #15 #20 #18
conflict #18 #20
  scripts/writer.py
```

`#12` touches nothing else, so it is first. `#15` and `#20` overlap and trial-merged clean, so they are the chain. `#18` overlaps `#20` and conflicted, so it is last and flagged. An unverified pair or a truncated file list is a line `unverified <ref> <why>` or `unverified <ref-a> <ref-b> <why>`, and those changes are absent from `order`. No open changes: empty stdout, exit 0.

## Tests

`evals/test_merge_order.py`, unittest, temp fixtures, injected `gh`, `call`, and git runner. No network. Behaviour starts red; `merge_order.py` changes only to turn it green.

1. GitHub: two open pull requests and the files from the files API, including a `previous_filename`, appear in the listing.
2. GitLab: two open merge requests and both diff paths appear, marked `!`.
3. Disjoint files: the git runner is never called; the order is both changes, lower number first.
4. One overlapping pair, merge-tree exit 0: fetched once each, trial-merged once, and placed as a chain after any disjoint change.
5. One overlapping pair, exit 1, stdout `<oid>\0<path>\0`: a `conflict` line names the pair and the path, and the two are not a chain.
6. A overlaps B (exit 0), B overlaps C (exit 1), A misses C: merge-tree is called for A-B and B-C only. Order is the A-B chain, then C, with one conflict line.
7. Two bases: two blocks, no cross-base trial merge.
8. The git argv is only `fetch` and `merge-tree`. Refspecs are the pull or merge-request head refs above, and only for numbers that overlap. No `merge`, `push`, `checkout`, `switch`, `reset`, `rebase`, or `commit` subcommand.
9. A file list that fills `MAX_PAGES` pages is `unverified`, not on the order line, and not trial-merged.
10. An open list that fills `MAX_PAGES` pages exits 1 and prints no `order` line.
11. merge-tree exit 128, or a fetched oid other than the forge head: `unverified`, exit 0.
12. No `Backlog:` line, and a root that is not a checkout of the backlog project, each exit 2.
13. No open changes: empty stdout, exit 0.
14. `COMMANDS["merge-order"]` is `scripts/merge_order.py`.
15. One real temp repository, no network: two branches that conflict and two that do not. `merge-tree --write-tree --name-only --no-messages -z` returns the exits and paths this plan parses, and `HEAD` and `git status --porcelain` are unchanged. This locks the parser to git's output rather than to a mock.

The pure walk (cases 3–7, 9, 11) is a function of changes plus trial results, called without git. The argv checks (case 8) and the forge listings (cases 1–2, 10, 12–13) go through `main`.

## Architecture

In the same change that adds the module:

- Add `merge_order.py` to section 4's Use Cases row, beside `merge_ready.py`.
- Add `"merge_order"` to `DRAWS_NOTHING`.

Leave `git_view.py` unchanged. Do not add a circle split that only this command would need: `merge_ready.py` already gathers forge facts and prints them from one Use Case module, and section 1 says not to add a layer no caller needs.

Replace the ponytail comment on `render_board.merge_order` with a pointer to `chief-of-stuff merge-order`. Do not change that function's order. The panel still sorts by approval, pipeline, and filename overlap. The issue page's "merge order K of M" stays number order into one base.

Do not edit `agents/chief-of-stuff.md`. Teaching the coordinator to run the command is a later behaviour change and starts as a failing eval.

## Risks

- Pairwise `merge-tree` of two heads is what the acceptance asks for. A clean A-B and a clean B-C can still conflict once A and B are both on the base. The order must not be described as a proven sequence.
- A truncated file list classified as disjoint is a wrong order. The cap has to fail closed.
- Fetch and `merge-tree --write-tree` write objects and the remote-tracking refs named above. A test that only mocks git can miss a `HEAD` move; case 15 is that test.
- The wrong checkout would fetch into someone else's `origin`. The origin check is the refusal.
- Conflict prose without `--no-messages` is on stdout. Parsing it as paths would flag the wrong files.
- An unrelated history makes `merge-tree` fail rather than exit 1. That pair is unverified. Do not pass `--allow-unrelated-histories`.

## Non-goals

- Replacing the board panel or the issue page queue.
- A sequential trial merge onto an accumulating tree.
- Sorting by approval, draft, or pipeline. `merge-ready` already prints those.
- Cloning a missing checkout, adding a remote, or fetching a change that overlaps nothing.
