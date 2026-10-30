# Plan: #903 Board cards label own-backlog issues with the raw store key, not #N

## Where the code is

The store (`github:owner/repo#N`, `gitlab:host/project#N`, `forge.Forge.key`) exists only on `origin/develop` (0.129.x). This worktree's base is `main` (0.128.67), which has no store keys, no `_render_store_decisions` and no `decision-badge`. **The implementer must branch from `origin/develop`, not `main`**, or none of the lines below exist. Line numbers are `origin/develop` at 4417a8fe.

## Cause

`scripts/render_board.py:206` (`card`) reads `issue = issue_key(task.issue, home) if changes or url else raw_issue`, so a store key with no change and no known URL shows `raw_issue`, the whole key. With a change it shows `issue_key(...)`, which reaches `#N` only by the `TRAILING_NUMBER` fallback (`tracker.py:442`), because `issue_ref` has no store-key form (`_REF_SHORT` allows no `:`).

`scripts/render_board.py:283` (`_render_store_decisions`) prints `d.issue_key` raw. `IssueRef.label(home)` (`backlog_ref.py:171`) already gives the right text, and the task page uses it.

## Change (smallest that fixes both sites)

1. `scripts/backlog_ref.py`, beside `IssueRef`: add `_REF_STORE = re.compile(r"^(github|gitlab):(.+)#(\d+)$")` and `store_label(key, home) -> str`. It builds `IssueRef(repo, n)` for `github:`, or `IssueRef(repo, n, host)` (host is the text before the first `/`) for `gitlab:`, and returns `.label(home)`. A string that is no store key comes back unchanged.
2. `scripts/render_board.py:206`: when `full_key`, `issue = store_label(raw_issue, sources.home)`; otherwise the existing expression. A foreign-project card with a change now names its project too (today it reads `#N`, which collides with the home's `#N`).
3. `scripts/render_board.py:273`: `_render_store_decisions(decisions, home)` shows `store_label(d.issue_key, home)`; the `href` stays `issue_href(d.issue_key)`. The call at line 393 passes `sources.home`.

Not changed, on purpose: `issue_ref` (its callers `audit_tasks`, `board_sources:457`, `dispatch_prompt`, `kanban_cli`, `one_shot` and `tracker_read` would start resolving store keys and, for GitLab, fetching them: wider than a label bug) and `tracker.issue_key` (lookups in `task_forge` depend on its output).

## Tests (`unittest`, no new dependency)

- `evals/test_backlog.py`, `IssueRefTest`: `store_label` for the home GitHub key gives `#9`; a GitHub key in another repo gives `o/n#9`; the home GitLab key gives `#5`; a key in another GitLab project or on another host gives `host/project#5`; `home=None` gives the prefix-less label; a non-key (`workflow`, `#7`, a URL) comes back unchanged.
- `evals/test_flow_board.py` (uses `rb.build_columns(tasks, LANES, kanban, sources, NOW)`): a task whose issue cell is the home store key, with no change and no `sources.issues` entry, gives a card whose `refs[0][0]` is `#N`; the same task with a change still reads `#N`; a task naming another project's store key reads `owner/repo#N`. The link `refs[0][1]` is unchanged: `issue_href(key)`.
- `evals/test_render_board.py`: render `_render_store_decisions` with a decision for a home key and one for a foreign key; the anchor text is `#N` and `project#N`, and the `href` is the encoded full key.
- Run the whole suite: `python3 -m unittest discover -s evals -p 'test_*.py'`.

## Risks

- Wrong base (above). If `develop` has moved, re-read lines 203-207 and 273-300 first.
- A store key that does not match the regex (hand-edited) falls through unchanged, as today.
- Adjacent, not fixed: `issue_key` turns a foreign project's store key into the home's `#N` (`flow_ref`, `task_forge` lookups). Worth its own issue.

## Logic

```
G := the card or decision shows #N
H := the key is in the workspace's own backlog (home_of(home) == (host, repo))
F := the key matches github:|gitlab: ... #N
S := store_label is applied
1. IssueRef.label(home) = "#N" iff H, else the project-qualified label   [backlog_ref.py:171]
2. card and decision text today = the raw cell or key                    [render_board.py:206, :283]
3. S ∧ F → text = IssueRef.label(home)                                   [design]
4. ¬F → text unchanged                                                   [design]
∴ F ∧ S → (G ↔ H): an own-backlog issue reads #N, with or without a change; another project's names its project
```
