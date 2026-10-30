# Issue 895 — Engine Decisions page reads 0 pending

## Problem

On engine workspaces (`store.db` present), the board and Decisions tab badge show open decisions from the store (14 / 26 / 22 in the report), and the board Build section lists them with Answer / Reverse / Re-route forms. `/decisions` still renders from the coordinator model only: `decision-*.json` files plus tracker **Decisions** rows. Engine workspaces typically have neither, so the page shows `0 pending · 0 completed today` while the store holds dozens of open rows.

## Root cause

Two parallel decision surfaces were added for the engine; only one was wired into `decisions.html`.

| Surface | Data source | Wired today |
|--------|-------------|-------------|
| Board list + tab badge | `PageData` / `decision_actions.tab_counts` / `render_board._render_store_decisions` | Yes |
| `/decisions` (`decision_page.index` / `write_all`) | `pages_dir/decision-*.json`, tracker Decisions rows | Yes (coordinator only) |

`render_board.write` already receives `db` from `pages._page_data` but calls `decision_page.write_all(pages_dir, ctx, day, root)` without `db`. `index()` never reads `store.decision` rows. `workers_page.write` already adds `store_pending` to its tab badge; the decisions index does not.

Relevant anchors (engine-capable tree, e.g. 0.129.x):

- `scripts/decision_page.py` — `entries`, `index`, `write_all`, `pending_count`
- `scripts/render_board.py` — `_render_store_decisions`, `open_store_decisions`, `write` → `write_all`
- `scripts/decision_actions.py` — `state_of`, `states`, `current_rows`, `OPEN_STATES`, `tab_counts`
- `scripts/pages.py` — `_page_data`, `fresh` → `render_board.write` for `decisions.html`
- `scripts/task_forge.py` — `PageData`, `page_data`

## Goal

When `store.db` is present:

1. **PENDING** lists every store decision whose latest row is `open` or `blocked`, scoped like `tab_counts` (issue still open in `work_item`, i.e. `forge_state != "closed"`).
2. **Pending count** on `/decisions` (header, `PENDING · N`, Decisions tab badge) matches `decision_actions.tab_counts(...)["decisions"]` and the board badge.
3. **COMPLETED** includes store decisions answered today in the workspace timezone (owner or trio): latest row closed (`answered`, `reversed`, `re-routed`, etc.) with `answered_at` or answer row `created_at` on `day`.
4. Coordinator workspaces without `store.db` behave exactly as today (JSON + tracker rows only).

Non-goal for this issue: rich per-slug `/decisions/<slug>` pages for store rows (link to `/issues/<issue_key>` like the board).

## Implementation plan

### 1. Shared store decision presentation

Extract or reuse the board’s store decision row markup so board and index stay consistent.

- Prefer moving the HTML builder from `render_board._render_store_decisions` into `decision_actions` or a small `decision_store.py` helper (imported by both `render_board` and `decision_page`), keeping the same POST shape (`decision_id`, `action`, `answer`, `target_stage` → `/`).
- If extraction is too large for one PR, duplicate minimally in `decision_page` with a one-line comment pointing at the board helper — but one shared function is better.

### 2. Extend `decision_page.index` / `write_all`

Add optional `db: PageData | None = None` and `navigation: dict | None = None` (same pattern as `workers_page.write`).

When `db` is not `None`:

- **Pending store rows**: `pending_store = [d for d in states(db.decisions()) if d.status in OPEN_STATES and d.issue_key in opened_keys]` where `opened_keys` matches `tab_counts` (`forge_state != "closed"`).
- Append HTML rows for each pending store decision (after JSON-derived pending rows, or interleaved by `asked_at` — prefer **newest asked first** among store rows, then JSON entries as today).
- **Completed store rows**: For each decision in `states(db.decisions())`, if status ∉ `OPEN_STATES` and the answer timestamp (parse `answered_at` from `state_of`, else `created_at` on the answering row) falls on `today` in `ctx.now.tzinfo`, append a `row done` line: issue link, question, answer, `decided_by`.
- **Counts**: `store_pending = navigation.get("decisions")` if `navigation` else `len(pending_store)`; use `tab_bar("Decisions", json_pending + store_pending, decisions=store_pending + json_pending)` only if we need to split — align with `workers_page`: badge = JSON pending + store pending, with `decisions=` kwarg when navigation supplies the canonical store count. Simplest: `tab_bar(..., pending_count_json + store_pending, **(navigation or {}))` so badge matches board when `navigation` is passed.
- **Meta line**: `N pending` must include store pending; `M completed today` must include store completions.

When `db` is `None`, unchanged code path.

### 3. Wire `db` and `navigation` through render pipeline

- `decision_page.write_all(..., db=None, navigation=None)` → pass into `index`.
- `render_board.write`: after `ctx = decision_context(...)`, compute `navigation = decision_actions.tab_counts(db.items(), db.decisions(), db.segments()) if db else {}`, then `pending = decision_page.write_all(..., db=db, navigation=navigation)`.
- `pages.fresh` for `decisions.html`: already ends in `render_board.write(root, day, db=data)` when not a decision slug — verify `data` is passed (engine path). No separate branch needed if `write` is fixed.

### 4. `pending_count` / board panel (coordinator + engine)

- For engine boards, `held_refs` and JSON-only `pending` in `render_board.render` may still be JSON-only; store holds are already merged via `pending_refs` from `store_decisions`. No change required unless tests show DECISIONS panel drift; panel is JSON-centric today on coordinator workspaces only.

### 5. Docs / module graph

- Update module docstring at top of `decision_page.py` to state that engine workspaces also list store decisions on `decisions.html`.

## Tests (TDD)

Add failing tests first, then implement.

| Test file | What to assert |
|-----------|----------------|
| `evals/test_decision_page.py` (or new `evals/test_decision_page_store.py`) | With a minimal migrated `store.db` fixture (no `decision-*.json`): `write_all` produces `decisions.html` with `PENDING · N` where N matches `tab_counts`; question text and issue key appear; `COMPLETED · M` includes a row answered today. |
| `evals/test_tab_bar.py` or `evals/test_pages.py` | HTTP GET `/decisions` on a pages server rooted at engine fixture: body contains pending count consistent with badge on `/`. |
| `evals/test_render_board.py` (optional) | Regression: coordinator fixture with JSON decisions unchanged. |

Fixture notes:

- Use temp dir + `store` migration / test helper if one exists in tree; otherwise create `work_item` + `decision` rows via the same path other store tests use once on main.
- Use template dates (`{{today}}`) in fixtures per project rules.

Run: `python3 -m unittest discover -s evals -p 'test_*.py'` (full suite must stay green).

## Risks

| Risk | Mitigation |
|------|------------|
| **Double-counting** JSON + store for the same ask | Engine workspaces should not duplicate; if both exist, document precedence (show both until product says otherwise) or dedupe by issue_key + normalized question. |
| **Badge mismatch** between board and `/decisions` | Single source: `decision_actions.tab_counts` passed as `navigation`. |
| **Timezone “completed today”** | Use `ctx.now.tzinfo` and same date as board `day` parameter, not UTC midnight. |
| **Closed issues with open decision rows** | Match `tab_counts`: exclude decisions for closed `work_item`. |
| **Worktree behind main** | Implementation branch must include engine store code (`store.db`, `PageData`, `decision_actions`) from main / 0.129+ before this fix applies. |

## Rollout / verify

1. Engine board on :8788 / :8789 / :8790: `/decisions` pending count equals tab badge and `sqlite3` open count (modulo closed-issue filter).
2. Answer one decision on board; reload `/decisions` — moves to COMPLETED today.
3. Coordinator workspace with `decision-*.json` only: unchanged evals and manual spot check.

## File ownership (implementation)

| Path | Mode |
|------|------|
| `scripts/decision_page.py` | write |
| `scripts/render_board.py` | write |
| `scripts/decision_actions.py` | read |
| `scripts/pages.py` | read |
| `scripts/task_forge.py` | read |
| `scripts/fragment.py` | read |
| `evals/test_decision_page.py` | write |
| `evals/test_pages.py` | write |
| `evals/test_tab_bar.py` | read |

Optional if extracting shared HTML: `scripts/decision_actions.py` or new `scripts/decision_store_html.py` (write).
