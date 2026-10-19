# Plan for Issue #288 — An answered decision reads as closed while its action is blocked

## Problem Statement

When a user answers a decision on its page (`/decisions/<slug>`) or in chat, the coordinator records the `## Decisions` row in the daily tracker **before** acting on it (`agents/chief-of-stuff.md:197`, `skills/decision-page/SKILL.md:57`) and then attempts the action the decision requires (such as launching a worktree or worker via `chief-of-stuff worktree` / `chief-of-stuff worker`). When that launch or follow-up action is refused (for example, by a permission check or a launcher refusal), `skills/dispatch/SKILL.md:71` keeps the task `open` and tells the coordinator to report what was refused, but every rendered surface immediately treats the decision as completed and closed and shows no blocked action waiting on the user.

Specifically, across the current codebase (`50091f80`):

1. `scripts/decision_page.py:entries()` (line 653) skips any `decision-<slug>.json` as soon as `ctx.answer(slug)` finds a `## Decisions` row (`if ctx.answer(slug): continue`), regardless of whether carrying out the decision's action succeeded or is blocked.
2. `scripts/render_board.py:decision_panel()` (lines 258–281) renders only `pending` decisions in the board's `DECISIONS` panel and counts every `decision-*.json` with `ctx.answer(slug)` in `{done} answered today`, so a decision whose action was refused disappears from the `DECISIONS` panel (or collapses into `"Every decision has an answer — N answered today"`).
3. `scripts/decision_page.py:index()` (lines 690–759) drops the decision out of `PENDING` and renders its `## Decisions` row under `COMPLETED` (`<div class="row done">`) on `decisions.html`.
4. `scripts/decision_page.py:render()` (lines 410–428) flips the decision page's header chip to `<span class="chip answered">ANSWERED · <option></span>` with no indication that carrying out the answer is blocked or what it waits on.
5. `scripts/decision_page.py:held_refs()` (lines 678–687) collects held issue/MR references only from `pending`, and `scripts/task_forge.py:hold_cause()` (lines 19–34) only flags a task as held (`NEEDS INPUT` on its `BUILD` card, the `NEEDS INPUT` stat tile, and `needs input` in `flow_chart.py`) when `issue_key(task.issue, home) in pending_refs`, `kanban.holds(stage)`, or `task.kind == "waiting"` on a non-worker owner. Because the refused launch leaves the task `open` and `decision-<slug>` is no longer in `pending_refs`, `hold_cause()` returns `""`, leaving the board with no card or row stating that the action is blocked on the user.

## Design Overview

An answered decision is not completed while carrying out its action is blocked; it must remain visible as an active, blocked decision on the board (`index.html`), on the decisions index (`decisions.html`), on the decision page (`decision-<slug>.html`), and on the held task's `BUILD` card and `NEEDS INPUT` count, naming both the user's answer and what carrying out the action waits on.

### 1. Recording a Blocked Decision Action (`scripts/decision_page.py` and `scripts/tracker_log.py`)

Support recording and reading back a decision's blocked action from either the decision JSON (`decision-<slug>.json`) or the daily tracker's `## Log`, so both CLI/page updates and append-only tracker logs work seamlessly:

- **Decision JSON (`scripts/decision_page.py`)**:
  - Allow an optional `blocked` field on `decision-<slug>.json` in `parse(data: dict)`: either a non-empty string naming what carrying out the answer waits on (e.g. `"permission check refused chief-of-stuff worker"`), or `{"reason": "<what it waits on>", "at": "<ISO-8601>"}` (and `None`/`""` when unblocked). Reject a `blocked` value that is neither a string nor a dict with a non-empty `reason`.
  - Add `block_decision(pages_dir: Path, slug: str, reason: str, now: datetime | None = None) -> Path` and CLI flags `--blocked <reason>` / `--unblock` on `decision_page.py` (reachable via `chief-of-stuff decision-page --root R --name <slug> --blocked "<what it waits on>"` and `--unblock`), which sets or clears `d["blocked"]` on `<pages_dir>/decision-<slug>.json` and re-renders `decision-<slug>.html` and `decisions.html`.
- **Tracker `## Log` (`scripts/tracker_log.py`)**:
  - Add `DECISION_BLOCKED = re.compile(r"^- (\d{1,2}):(\d{2}) decision-([a-z0-9]+(?:-[a-z0-9]+)*) blocked: (.*)\s*$")`, formatter `decision_blocked_line(at: str, slug: str, reason: str) -> str`, and reader `blocked_decisions(text: str) -> dict[str, str]` returning `slug -> reason` for the latest blocked line in `## Log`.
  - In `decision_page.Context.from_root()`, populate `blocked_by_slug: dict[str, str]` from `tracker_log.blocked_decisions(text)` (cleared for a slug if a later `stage:` or `one-shot ... started:` move in `## Log` advances the held task after the blocked line's timestamp, or if the held task is `done`).
  - Add `Context.blocked(slug: str, d: dict | None = None) -> str` returning the blocker string from `d.get("blocked")` if present, else `self.blocked_by_slug.get(slug, "")`.

### 2. Keeping Answered-but-Blocked Decisions Active (`scripts/decision_page.py`)

- **`entries(pages_dir: Path, ctx: Context)`**:
  - Change `if ctx.answer(slug): continue` to skip an answered decision only when `not ctx.blocked(slug, data)`.
  - When a decision has an answer (`ctx.answer(slug)` or `saved(data)`) AND `ctx.blocked(slug, data)` is non-empty, keep `(slug, data, asked, "")` in `entries(pages_dir, ctx)`.
  - Because `entries()` retains answered-but-blocked decisions, `held_refs(pending, ctx)` continues to include their `holds` and `references`, keeping the held issues/MRs in `pending_refs`.
- **`blocked_refs(pending: list, ctx: Context) -> dict[str, str]`**:
  - Add a helper in `decision_page.py` mapping each held issue/change ref (`ctx.hold_key(r)`) of an answered-but-blocked decision in `pending` to its blocker reason (`ctx.blocked(slug, d)`), so the board and task cards can state specifically what the action waits on.

### 3. Rendering Blocked Decisions on the Decision Page, Index, and Board (`scripts/decision_page.py`, `scripts/render_board.py`, `scripts/task_forge.py`)

- **Decision Page (`scripts/decision_page.py:render`)**:
  - Accept/read `blocked = _blocked_reason(d) or (ctx.blocked(slug, d) if ctx and slug else "")`.
  - When `answered` and `blocked`:
    - Render the header chip as `<span class="chip blocked">BLOCKED · {escape(chosen)}</span>` (or `<span class="chip blocked">BLOCKED</span>` when `chosen` is empty) styled with `--stop` (`#D55E00`) so it never reads as a green closed `ANSWERED` chip.
    - Render a blocker callout inside the Options / Ask area (`<div class="card answered blocked">`) showing the recorded answer (`chosen` / `tracked.words` / `saved(d)["words"]`) and `Action blocked — waits on: {md(blocked)}`.
- **Decisions Index (`scripts/decision_page.py:index`)**:
  - In `PENDING (N)`: when a row in `pending` has `why_blocked := ctx.blocked(slug, d)`, render `<div class="row pend blocked">`, displaying the recorded answer (`Answered · <key/words>`) and `<span class="blocked-reason">Blocked · waits on {escape(why_blocked)}</span>` in place of the unanswered fallback.
  - In `COMPLETED (N)`: exclude `ctx.rows` entries whose decision slug is currently blocked (`if slug and ctx.blocked(slug): continue`), so a blocked decision is never listed under `COMPLETED`.
- **Board `DECISIONS` Panel (`scripts/render_board.py:decision_panel`)**:
  - In the `done` counter (`answered today`), count only `decision-*.json` files where `ctx.answer(slug)` is truthy and `not ctx.blocked(slug, d)`.
  - For each row in `pending`, if `why_blocked := ctx.blocked(slug, d)` is non-empty, render `<div class="row blocked">` with `<span class="blocked-badge">BLOCKED · {escape(chosen_or_words)} — waits on {escape(why_blocked)}</span>` instead of option pills or `SAVED`, so the board's `DECISIONS` panel explicitly shows the answered decision as blocked and what it waits on.
- **Board `BUILD` Card, `NEEDS INPUT` Tile, and Flow (`scripts/task_forge.py`, `scripts/render_board.py`)**:
  - In `scripts/task_forge.py:hold_cause(task, stage, pending_refs, workers, home, blocked_refs=None)`:
    - When `key := issue_key(task.issue, home)` is in `blocked_refs` (an answered decision whose action is blocked), return `f"action blocked: {blocked_refs[key]}"` (or `"decision"` when `key` is in `pending_refs` for an unanswered decision).
    - Because `hold_cause()` returns a non-empty cause:
      - `render_board.py:_card()` renders `<span class="hold-pill">NEEDS INPUT</span>` on the task's `BUILD` card and renders `<div class="card-Sub">{escape(held_by)}</div>` (`action blocked: <what it waits on>`) so the card itself states that the action is pending on the user and why.
      - The board's `NEEDS INPUT` summary tile (`held_count`) and `flow_chart.py` (`needs input`) automatically include the blocked task.

### 4. Coordinator & Skill Instructions (`agents/chief-of-stuff.md`, `skills/decision-page/SKILL.md`, `skills/dispatch/SKILL.md`, `evals/test_agent_budget.py`)

- Update `skills/decision-page/SKILL.md` (and, only if needed and within byte ceilings, `skills/dispatch/SKILL.md` / `agents/chief-of-stuff.md`) so that when carrying out an answered decision is refused or blocked on the user, the coordinator records the blocker with `chief-of-stuff decision-page --root . --name <slug> --blocked "<what it waits on>"` (or logs `decision-<slug> blocked: <what it waits on>`) before re-rendering the board, and clears it (`--unblock`) once the action runs.
- Verify `evals/test_agent_budget.py` ceilings (`TOTAL`, `MAX_LINE`, `CEILING`, `SKILL_CEILING`, `PROMPT`) remain green.

## Architecture & Circle Compliance

Per `architecture/ARCHITECTURE.md` and `evals/test_architecture.py`:

- `scripts/tracker_log.py` is in `Entities` (draws nothing, imports no presenter or gateway).
- `scripts/decision_page.py`, `scripts/render_board.py`, and `scripts/task_forge.py` are in `Interface Adapters, presenters`.
- `render_board.py` already imports `decision_page` and `task_forge`; `decision_page.py` imports `tracker` and will import `blocked_decisions` from `tracker_log` (`Presenters -> Entities`).
- No new module is created, and no dependency direction in `architecture/ARCHITECTURE.md` or `evals/test_architecture.py` is violated.

## Step-by-Step Implementation Tasks

1. **Red tests first (`evals/test_tracker_log.py`, `evals/test_decision_page.py`, `evals/test_flow_board.py`)**:
   - In `evals/test_tracker_log.py`: add round-trip tests for `decision_blocked_line` and `blocked_decisions`.
   - In `evals/test_decision_page.py`: add tests reproducing `#288`:
     - Answering a decision on its page (`save_answer`), recording its `## Decisions` row in the tracker, and marking its action blocked (`--blocked "permission check refused chief-of-stuff worker"` or a `decision-<slug> blocked: ...` Log line) keeps the decision in `entries()`, renders `BLOCKED` (never `COMPLETED` or green `ANSWERED`) on `decision-<slug>.html` and `decisions.html`, and shows both the user's answer and what the action waits on.
     - Unblocking (`--unblock` or a subsequent launch/stage move in `## Log`) moves the decision out of `PENDING` and into `COMPLETED`.
   - In `evals/test_flow_board.py`: add the exact `#288` board reproduction test:
     - User saves an answer on `decision-<slug>`, coordinator writes the `## Decisions` row, and the launch it requires is refused by a permission check while the task row remains `open`.
     - Rendering the board (`render_board.write`) keeps `decision-<slug>` in the `DECISIONS` panel with `BLOCKED` and the permission-check blocker reason, does not count it as completed, marks the task's `BUILD` card `NEEDS INPUT` with `action blocked: <reason>`, and increments the `NEEDS INPUT` tile.
2. **Implement tracker log formatter & reader (`scripts/tracker_log.py`)**:
   - Add `DECISION_BLOCKED`, `decision_blocked_line()`, and `blocked_decisions()`.
3. **Implement blocked-decision state & rendering (`scripts/decision_page.py`)**:
   - Extend `parse()` to validate optional `blocked`.
   - Add `block_decision()` and `--blocked` / `--unblock` CLI arguments in `main()`.
   - Extend `Context` with `blocked_by_slug` and `Context.blocked(slug, d)`.
   - Update `entries()`, `held_refs()`, `blocked_refs()`, `render()`, `index()`, and `PAGE_CSS` / `INDEX_CSS` for `.chip.blocked`, `.card.answered.blocked`, and `.row.pend.blocked`.
4. **Implement board & task hold rendering (`scripts/task_forge.py`, `scripts/render_board.py`)**:
   - Extend `task_forge.hold_cause()` with optional `blocked_refs: dict[str, str] | None = None` so a task held by an answered-but-blocked decision returns `f"action blocked: {blocked_refs[key]}"`.
   - Update `render_board.decision_panel()` and `render_board.render_board()` to pass `blocked_refs` and render `.row.blocked` / `.blocked-badge` in `DECISIONS` while excluding blocked decisions from `{done} answered today`.
5. **Update skill documentation (`skills/decision-page/SKILL.md`, `evals/test_agent_budget.py`)**:
   - Document `--blocked "<what it waits on>"` and `--unblock` in `skills/decision-page/SKILL.md` within the skill byte ceiling (updating `evals/test_agent_budget.py` only if a ceiling comment/number requires adjustment).

## Verification Plan

Run the unit and architecture suites with the standard Python 3.13 environment:

- `python3 -m unittest evals/test_tracker_log.py`
- `python3 -m unittest evals/test_decision_page.py`
- `python3 -m unittest evals/test_flow_board.py`
- `python3 -m unittest evals/test_agent_budget.py`
- `python3 -m unittest evals/test_architecture.py`

## Proposed File Ownership

| Path | Mode | Purpose |
| --- | --- | --- |
| `scripts/tracker_log.py` | `write` | Add `DECISION_BLOCKED`, `decision_blocked_line()`, and `blocked_decisions()` |
| `scripts/decision_page.py` | `write` | Support `blocked` in JSON/CLI/Context, retain blocked decisions in `entries()`, and render blocked state on `decision-<slug>.html` and `decisions.html` |
| `scripts/task_forge.py` | `write` | Return `action blocked: <reason>` from `hold_cause()` for tasks held by a blocked decision |
| `scripts/render_board.py` | `write` | Render answered-but-blocked decisions in `DECISIONS` with what they wait on, exclude them from `answered today`, and thread `blocked_refs` into `hold_cause()` |
| `skills/decision-page/SKILL.md` | `write` | Document recording and clearing a blocked decision action (`--blocked` / `--unblock`) |
| `evals/test_tracker_log.py` | `write` | Test `decision_blocked_line()` and `blocked_decisions()` round-trip |
| `evals/test_decision_page.py` | `write` | Test blocked decision page and `decisions.html` rendering and unblocking |
| `evals/test_flow_board.py` | `write` | Test board `DECISIONS` panel, `BUILD` card `NEEDS INPUT`, and `NEEDS INPUT` count when a decision's launch is refused |
| `evals/test_agent_budget.py` | `write` | Keep skill/agent byte budget checks aligned if `skills/decision-page/SKILL.md` size changes |
| `scripts/tracker.py` | `read` | Decision row parsing and issue key helpers |
| `scripts/pages.py` | `read` | Decision answer POST endpoint and page refresh flow |
| `scripts/flow_chart.py` | `read` | Consumes `hold_cause()` for `needs input` lane status |
| `agents/chief-of-stuff.md` | `read` | Coordinator decision and tracker rules |
| `skills/dispatch/SKILL.md` | `read` | Refused launch handling rule |
| `architecture/ARCHITECTURE.md` | `read` | Clean Architecture layer rules |
