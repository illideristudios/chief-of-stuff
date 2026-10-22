# Issue #886 — Task page header chip vs board for closed issues

## Problem

Closed forge issues whose tracker rows are **done** show a **done** chip on the board (`data-cat="done"`) but the task page header shows **`OPEN`** (`<span class="chip open">OPEN</span>`). Repro: tracker GitLab iid 1 (and #3, #4, #5, #8, #9, #12, #14, #17, #18) — board card done, `/issues/…#1` header open while `glab` reports `state=closed`.

## Root cause (code)

The header chip is `status.upper()` from `issue_page.render`, where:

```python
status = rows[0][0] if rows else mine[-1].kind
```

and `rows` are Flow rows from `flow_chart.build` filtered by `issue_number(r.ref) == number`.

Three gaps explain **OPEN** while the board shows **done**:

1. **`home` not passed to `flow_chart.build` on the task page** — `render_board.py` passes `home=sources.home`; `issue_page.py` does not. Row `ref` values then disagree with the board (`#N` vs `host/project#N`), so `_merge_issues` may leave **multiple rows for one issue**. `rows[0]` can be a **queued `open`** row instead of the finished row the board draws.

2. **Issue and change lookups use bare `#<n>`** — `ref = f"#{number}"` drives `sources.issues.get(ref)` and `ref in c.issues`. Elsewhere (`task_forge.forge_ends`, board columns, #409) uses `issue_key(task.issue, sources.home)`. Wrong key → `issue` is `None` in `why_not` / References even when the cache has the closed issue; change cards may be missing too.

3. **No header test for closed-without-merge** — #200 pins **merged** on the header; #220 pins Flow clipping and elapsed for **closed** but not the header chip. A fix can regress without a red case.

The board chip comes from the tracker (`task.kind` / `task.state` on `columns.Card`), not from `rows[0][0]`. The task page should **agree with the board and forge** after the same `forge_ends` / Flow inputs the board uses, not expose a stale Flow bucket word.

## Implementation

### 1. Tests first (TDD)

Add cases to `evals/test_issue_page.py` (and reuse fixtures from `evals/test_closed_issue_stop.py` where useful):

| Case | Fixture sketch | Assert |
|------|----------------|--------|
| Closed issue, tracker **done**, no MR | `ISSUE` `state=closed` + `closed_at`; tracker row `done …`; `Sources.home` set to GitLab backlog | Header chip is **done** or **closed**, not `open` / `waiting` / `running` |
| Same as board | Shared `sources`, trackers, `LANES` as `ForgeMergedTaskPageTest` | Board Flow row note/status for the task equals task page header status (parity test for **closed**, mirroring `test_the_board_and_the_task_page_share_one_merged_map`) |
| Issue key not bare `#N` | Issue cell = GitLab URL or `host/project#N`; cache keyed via `issue_key` | Header still **done**/**closed**; References show `closed` |
| `home` on sources | `bs.Sources(..., home=GitLab Backlog)` | With two tasks on same issue (one historical **open** row in log), header is not **OPEN** |

Optional narrow regression in `evals/test_closed_issue_stop.py`: one header assertion on the existing closed elapsed fixture.

### 2. `scripts/issue_page.py`

- Pass **`home=sources.home`** into `flow_chart.build` (match `render_board.py`).
- Resolve the page’s issue key from **`mine`** tasks: `key = issue_key(mine[-1].issue, sources.home)` (or the union of keys for grouped tasks), not `f"#{number}"`.
- Use `key` for `sources.issues.get`, change filtering (`key in c.issues` or normalize keys), and eyebrow issue link where applicable.
- **Header status** — smallest correct rule:
  - Build Flow rows with the same arguments as the board (including `home`, `ended=forge_ends(known, sources, zone)`).
  - Select the row for this issue: prefer the row whose `issue_number(r.ref) == number` after build; if several, take the same row the board would show (merged group — status of the **last-moving** row, not an arbitrary `rows[0]` on an unmerged duplicate).
  - If every current task on the issue is `kind == "done"`, header **`done`** (board chip), even when Flow still carries an `open` queued artifact.
  - Else if `forge_ends` gives **`closed`** or **`merged`** for the named task(s), use that word (as #200 does for merged).
  - Else use Flow status / `mine[-1].kind` as today.

Keep chip CSS class = status word (`done`, `closed`, `merged`, `running`, …) so existing styles hold.

### 3. No change unless needed

- **`flow_chart.py` / `task_forge.py`** — only if parity tests show a shared helper is smaller than duplicating row-pick logic in `issue_page`; prefer fixing call site first.
- **`pages.py` routes** — out of scope unless product wants forge-key URLs in repro; numeric `/issues/<n>` is what this repo serves today.

## Risks

| Risk | Mitigation |
|------|------------|
| Header shows **CLOSED** while board shows **done** | Accept both per issue text; prefer **`done`** when all tracker rows on the issue are `done`, **`closed`** when forge closed but tracker not yet `done` |
| Multi-task / review loops on one issue | Rely on `_merge_issues` with consistent `home`; test grouped issue |
| Missing `closed_at` in cache | `forge_ends` already skips; header falls back to tracker `done` — document; no new forge fetch |
| #409 cross-project `#N` | Always `issue_key(..., sources.home)` for lookups |

## Verification

```bash
python3 -m unittest evals.test_issue_page evals.test_closed_issue_stop -q
python3 -m unittest discover -s evals -p 'test_*.py' -q
```

Manual: board card done for a closed GitLab issue → open `/issues/<n>` → header chip matches.

## File ownership (implementation stage)

| Path | Mode |
|------|------|
| `scripts/issue_page.py` | write |
| `evals/test_issue_page.py` | write |
| `evals/test_closed_issue_stop.py` | write |
| `scripts/render_board.py` | read |
| `scripts/flow_chart.py` | read |
| `scripts/task_forge.py` | read |
| `scripts/board_sources.py` | read |
| `scripts/tracker.py` | read |
| `evals/test_flow_board.py` | read |
