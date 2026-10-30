# Plan: #885 — failed tasks drawn as running; board counts disagree

## Base

This planning worktree (`work/issue-885-plan-1`) is `50091f80` (old `main`, 0.128.67). It has no store-backed board: no `build_from_store`, no `decision_actions.tab_counts`, no Dispatch badge count. Do not implement here.

Implement on `origin/develop` (`4417a8fe` or later). Every path below is on develop.

## Cause

The issue guesses the failed state "was never written back". That is the reason the items are *retried*, not the reason they are drawn as running. The drawing is wrong on its own.

What happened to each of the eight tasks:

1. `engine.run_pass` launched `plan` attempt 1. The launch failed ("not a repository root").
2. `_fail_start` set the local `human_review` gate and tried to restore the forge hold. That failed too ("missing label in GitLab"), so the forge never got the label.
3. `store.record_segment(..., "failed")` wrote a zero-length segment: `started_at == ended_at`, `outcome == "failed"`.
4. The next `store.sync_from_forge` overwrote `human_review` from the forge snapshot, which has no hold label. The item is open, unheld, with no open segment. The engine may dispatch it again (`reconcile` spends a retry on each failed start).

Each board surface then reads that state through its own rule:

| Surface | Code (develop) | Reads | Shows |
|---|---|---|---|
| RUNNING tile | `render_board._tasks_from_store` → `flow_tiles` | open segment (`ended_at is None`) | 0 |
| Workers page | `workers_page._store_rows` | open segment | Running · 0 |
| Dispatch badge | `decision_actions.tab_counts` | open items − held − items with an open segment | 8 |
| Flow header, bar, task chip | `flow_chart.build_from_store` | any segment at all (`elif raw_segs: "running"`) | 8 running, RUNNING |
| Flow bar style | `build_from_store`: `kind = "done"` for every ended segment | ended, whatever the outcome | `gantt-done`, "· done" |

The task page chip is `rows[0][0]` of the same `build_from_store` (`issue_page.render`, `store_rows`), so it needs no edit of its own.

Three of the four counts already agree: the tile, the Workers page and the Dispatch badge all say "no worker, ready again". `build_from_store` is the outlier. Fix the outlier; do not touch the other three.

## Logic

```
O := the item has an open segment (ended_at is None)
H := the item is held (human_review, an open or blocked decision)
C := the forge issue is closed
F := the item's latest segment ended with outcome "failed"
X := the item is shown as running (Flow bucket, chip, header count)
R := the item is counted ready to dispatch (tab_counts: open − held − open segment)
D := the item's ended segment is drawn as done

1. R ↔ ¬C ∧ ¬H ∧ ¬O            [decision_actions.tab_counts]
2. tile, Workers page count O   [render_board._tasks_from_store, workers_page._store_rows]
3. today: X ← (segments exist)  [build_from_store: elif raw_segs → "running"]
4. today: D ← (segment ended)   [build_from_store: kind = "done"]
5. F ∧ ¬O ∧ ¬H ∧ ¬C → R ∧ X ∧ D  [3, 4 on the eight tasks: the disagreement]
6. required: X ↔ O ∧ ¬H ∧ ¬C ; R ↔ ¬O ∧ ¬H ∧ ¬C ; D ↔ ended ∧ ¬F  [issue Expected]
∴ in build_from_store: running only with O; ended and not held, not approved, not closed → "open" (queued bucket, ready); an ended segment with outcome "failed" has its own kind
```

## Implementation

Two source files. No new module, no new dependency, no ORM change (the store already carries `outcome`).

### 1. `scripts/flow_chart.py` — `build_from_store`

- Segment kind: `kind = "failed" if s.outcome == "failed" else "done"` for an ended segment. An open segment keeps `"running"`.
- Status: replace the last two branches

  ```python
  elif any(s.ended_at is None for s in raw_segs):  status, note = "running", stage
  elif raw_segs:                                   status, note = "running", stage
  else:                                            status, note = "open", f"queued · {stage}"
  ```

  with

  ```python
  elif any(s.ended_at is None for s in raw_segs):  status, note = "running", stage
  else: status, note = "open", f"{'retry' if raw_segs and raw_segs[-1].outcome == 'failed' else 'queued'} · {stage}"
  ```

  `_bucket("open")` already maps to `queued`, so the Flow header reads "8 queued" and the chip reads OPEN. No change to `_bucket`.
- Fix the stale comment above `BUCKETS` (it says everything that moved counts as running) to say a row is running only while a segment is open.

### 2. `scripts/gantt.py` — draw `failed`

- CSS: `.gantt-bar.gantt-failed{...}`, a hatched bar in `var(--gantt-hold)`'s family (the existing warning colour), distinct from `done` (solid) and `hold` (hatched, ahead of now). Add the dark-scheme token only if the existing `--gantt-hold` does not already read on dark; it does for `hold`, so reuse it.
- `legend(...)`: add `"failed"` to the default `kinds` tuple so the legend names it.
- The tooltip already ends with `g.kind`, so it reads "· failed" with no further edit.

A zero-length bar is drawn at `MIN_WIDTH` (0.5%), so the failed attempt is visible.

### Not changed

- `decision_actions.tab_counts`, `render_board`, `workers_page`: already agree (see the table).
- `engine._fail_start` and the forge write-back: the failure to restore the hold label is a workspace fault (`00 - ARCHITECTURE / PLANNING` missing on the tracker project; the Gauntlet path is not a repository root). Retrying is correct engine behaviour. Fixing either is a separate issue. Name it in the PR description; do not touch it here.
- The unstyled `gantt-running` kind (an open segment has no CSS rule in `gantt.py` today, so it draws no fill). Adjacent and real, but not in this issue's acceptance. Mention it in the PR; do not fix it.

## Tests

`evals/test_m6_store_board.py` already builds a store workspace (`make_store_workspace`) and asserts `build_from_store` rows. Add one test class there, same fixtures, no new file.

Fixture: three items on one store, `NOW` frozen.

- A: one segment `plan`/1, `started_at == ended_at`, `outcome="failed"`, open, unheld.
- B: segment `implement`/1 ended `outcome="done"`, next stage not yet launched, open, unheld.
- C: segment with `ended_at=None` (a live worker).

Asserts:

1. `build_from_store` → A is `("open", note starts "retry")`, B is `"open"`, C is `"running"`.
2. A's segment kind is `"failed"`; B's is `"done"`; C's is `"running"`.
3. Parity, the issue's acceptance in one place: with A and B only, `tab_counts(...)["dispatch"] == sum(status == "open")` over the Flow rows, and `_tasks_from_store` gives zero `running` tasks. With C added, the running count is 1 on the tile source, 1 in the Flow rows, and the Dispatch count excludes C.
4. `flow_chart.section` for A: the header meta contains "queued" and not "running"; the HTML has `gantt-failed` and no `gantt-done` bar for the failed segment; the tooltip ends "failed".
5. `issue_page.render` for A: chip is `OPEN` (class `chip open`), not `RUNNING`.
6. `gantt.legend()` names `failed`.

Existing assertions that must still hold: `test_rework_on_one_issue_shows_one_gantt_row_with_several_segments` (done, done, done, running) and `test_blocked_decision_holds_gantt`. Run the full `evals/` suite; a green run with `passed > 0` and no failed, skipped or xfailed is the done report's evidence.

## Risks

- **Between-stage items change bucket.** An item whose last segment ended `done`, with the next stage not yet launched, was drawn "running"; it now draws "queued" and `open`. That matches the Dispatch badge, which already counts it ready, but a person who read the old chart as "in flight" will see a change. The issue asks for exactly one count everywhere, so take the change.
- **Held-by-outcome gap.** `tab_counts` also treats a latest segment with `outcome == "human_review"` as held; `build_from_store` holds only on `item.human_review` or an open decision. The two differ for a short window if the forge flag lags the segment. Test 3 does not cover it. Check it while in the file; if the two disagree, add the same outcome rule to `build_from_store` in one line, with a test. Do not add a shared helper for one call site.
- **Merge window.** `build_from_store` is also the source of `from_store` (24-hour window) and the issue page. Both take the new status without edits; the existing tests above cover them.
- **Not verified here.** This worktree is old `main`; nothing on develop was run. The eight live tasks' store rows were not read. The cause is read from code and the issue's excerpts.

## File ownership

- write: `scripts/flow_chart.py`, `scripts/gantt.py`, `evals/test_m6_store_board.py`
- read: `scripts/decision_actions.py`, `scripts/render_board.py`, `scripts/issue_page.py`, `scripts/workers_page.py`, `scripts/engine.py`, `scripts/store.py`
