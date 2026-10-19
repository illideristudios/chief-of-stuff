# Plan: #235 The Workers page draws its rows as the canvas does

Stage: plan. Nothing is implemented. Every claim about the code below was read in this worktree at `50091f80`.

## What is wrong today

`scripts/workers_page.py` (`render`, 122 lines) draws RUNNING as eight bare text columns (worker, runtime, kind, task, stage, running, last activity, tokens) and ENDED TODAY as four (at, how, worker, task). Header boxes read `need you`, `running`, `slots free`. State, stage and outcome are plain text, so nothing is scannable.

## The canvas component is not in this repo

The issue points at "the canvas Workers component, v53". The only canvas reference in the repo is `Flow.dc.html` (the Flow artboard), named in comments of `render_board.py` and `test_board_flow_design.py`. There is no Workers artboard, no `Workers.dc.html`, no v53 file. So the issue's Acceptance list is the only spec I can read, and the plan draws to it. Pixel parity with the canvas is unverified until the owner supplies the artboard or confirms the Acceptance list is enough (open question 1).

## Acceptance, mapped to the code

| # | Acceptance line | Change in `workers_page.py` |
|---|---|---|
| A1 | Running rows lead with a state chip (RUNNING, LOOPING) and the worker name over `runtime model · kind` | One first cell: `<span class="chip …">RUNNING</span>` + name, then a second line `claude opus · one-shot`. Replaces the worker, runtime and kind columns. |
| A2 | The task column carries the stage as a chip after the task name | `_task(...)` output, then `<span class="chip stage">implement</span>`. Replaces the stage column. Blank stage: no chip. |
| A3 | Tiles read NEEDS INPUT, RUNNING, SLOTS FREE `n of cap`, cap from the workers setting | Rename the box labels `need you` → `NEEDS INPUT`, `slots free` → `SLOTS FREE`; `RUNNING` stays. Values and the cap source (`[workers] max_concurrency`, already read in `write`) do not change. |
| A4 | Ended-today rows show a state chip (DONE, CRASHED), a one-line summary, and a link or re-dispatch | Replaces the `how` column with a chip; adds a summary cell; the task link moves into an action cell. See "Ended rows". |
| A5 | A session with no task is not listed among running workers | Drop `w.kind == "session" and not w.task.strip()` workers before building rows. Heading count, RUNNING tile and `shots` all derive from the filtered list. |

## Design decisions

### State chip: RUNNING vs LOOPING

No field in `board_sources.Worker` says "looping". It holds `name, kind ("one-shot"|"session"), runtime, model, task, tree, started, last`. Proposed rule, until the owner says otherwise: a one-shot is RUNNING, a session (a standing process that is re-prompted) is LOOPING. This is the smallest rule that needs no new data. If LOOPING must mean "the session is inside a `/loop` or wakeup cycle", the registration record (`process_status.registrations`) would need a new field, which is a larger change across `spawn_session.py` and `board_sources.workers` (open question 2).

### Ended rows: DONE vs CRASHED

The tracker Log has three ended outcomes (`tracker_log.ENDED` / `RELAUNCH`): `completed; awaiting integration`, `HUMAN REVIEW NEEDED`, and `relaunch requested`. No line says "crashed". A crash is a human-review line whose reason is one the launcher wrote for a failed run, from `one_shot.worker_result` and the adopted-run path:

- `worker did not write a valid TOON result …`
- `worker wrote an unreadable TOON result …`
- `worker result is incomplete or invalid …`
- `worker exited N: …`
- `the launcher died before reconciling this run; …`

Mapping: completed → `DONE`; human review with one of those reasons → `CRASHED`; any other human review (the worker asked for a person, or `task row changed …`) → `REVIEW`; relaunch → `RELAUNCH`. The issue names only DONE and CRASHED; `REVIEW` and `RELAUNCH` exist because dropping those two rows would silently shrink today's table, and the NEEDS INPUT tile still counts every human-review end. The crash patterns are a regex in `tracker_log.py` beside `ENDED`, because that module is where the writer and reader of Log lines live. A new test calls `one_shot.worker_result` with a missing, unreadable, incomplete and non-zero-exit result and asserts each classifies as CRASHED, so a reworded reason in `one_shot.py` fails there instead of silently downgrading to REVIEW.

### One-line summary

The end line is `- HH:MM one-shot <name>: <status> — <reason>. Changes: <files>.` (`tracker_log.ended_line`); a relaunch line is `… relaunch requested for <task> — <reason>.` Add `reason(line)` to `tracker_log.py`: the text after the first ` — `, cut at the last ` Changes: `, whitespace collapsed. The page escapes it and CSS clamps it to one line (`white-space:nowrap;text-overflow:ellipsis`, full text in `title`). A line with no ` — ` gives an empty summary, not an error.

### Link or re-dispatch

There is no dispatch route on the page server: `pages.do_POST` only saves decision answers. A one-click re-dispatch would add a process-launching POST to a localhost page, which is a security and scope decision, not part of this issue. Proposed: DONE rows link the task (`/issues/<n>`, as today); CRASHED, REVIEW and RELAUNCH rows show a `re-dispatch` link to the same task page, where the dispatch decision is made. Rows whose task has no issue number show the task name with no link, as `_task` does now (open question 3).

## File ownership

| Path | Access | Why |
|---|---|---|
| `scripts/workers_page.py` | write | cells, chips, tiles, CSS, the no-task filter |
| `scripts/tracker_log.py` | write | `reason(line)` and the crash pattern, beside `ENDED` |
| `evals/test_workers_page.py` | write | update the contract and add the new tests |
| `evals/test_tracker_log.py` | write | round-trip test for `reason` and the crash pattern |
| `evals/test_one_shot.py` | read | existing `worker_result` cases to copy fixtures from |
| `scripts/board_sources.py` | read | `Worker` shape; not changed |
| `scripts/decision_page.py` | read | `HEAD` / `PAGE_CSS` variables the chips use; not changed |
| `scripts/one_shot.py` | read | the crash reason strings; not changed |
| `architecture/ARCHITECTURE.md` | read | `workers_page.py` is already listed as an interface adapter; the architecture agent owns this file |

## Implementation order (tests first, per CLAUDE.md TDD)

1. Red: edit `evals/test_workers_page.py` to the new contract (below). Run `python -m unittest evals.test_workers_page`; expect the new assertions to fail and nothing else.
2. `tracker_log.py`: `reason(line)` and `CRASH`; green its round-trip test.
3. `workers_page.py`: filter, chips, two-line worker cell, stage chip, tile labels, ended cells, CSS. Check `decision_page.PAGE_CSS` for existing `.chip` rules before adding any; the vendored `assets/page-theme.css` `.chip` is not loaded by this page's `HEAD`. Colours use the variables `PAGE_CSS` already defines (`--inflight`, `--designed`, `--deployed`, `--dl` and their `-soft` pairs); no new colour literals, no new dependency.
4. Run the full suite (`python -m unittest discover -s evals`), then bump the plugin version as every change does (`.claude-plugin/plugin.json`; `main` is protected).

## Tests

Existing contract in `test_workers_page.py` that must change, and why:

- `box(page(), "need you")` → `"needs input"`; `"slots free"` stays (the helper casefolds, so the visible uppercase label still matches). The `RunningTest` row helper finds a row by worker name in any cell, so it survives the merged cell.
- `column(...)` heads: RUNNING keeps heads containing "activity" and "token"; the runtime, kind and stage heads go. `test_a_one_shot_row_names_its_runtime_task_stage_and_time` only reads row text, so it stays as is and still proves runtime, model, kind, task, stage, time are present.
- ENDED TODAY: `how` text now comes from the chip (`done`/`crashed`/`review`/`relaunch`). The fixture's `ENDED` table in the test file maps `impl-c3` completed, `impl-d4` human_review, `impl-e5` relaunch; the human-review fixture's reason is `"reason"`, so it must classify REVIEW, not CRASHED.

New tests (each fails before the change):

1. A running one-shot's first cell holds a RUNNING chip and the name; its second line is `claude opus · one-shot`. (A1)
2. A session's chip is LOOPING. (A1, rule above)
3. The task cell has the stage chip after the task name, and no chip for a blank stage. (A2)
4. The three tile labels read NEEDS INPUT, RUNNING, SLOTS FREE; SLOTS FREE reads `2 of 4` with cap 4 and the tile is absent with no cap. (A3)
5. A session with a blank task is absent from the RUNNING table, from the heading count and from the RUNNING tile; the existing three-worker fixture gains a fourth, taskless session. (A5)
6. Ended rows: `impl-c3` DONE with its reason as the summary; a fixture human-review end with reason `worker exited 1: boom` is CRASHED; `impl-d4` (reason `reason`) is REVIEW; summary text is escaped (a `<b>` in a reason prints literally); the action cell links `/issues/<n>`, with the label `re-dispatch` on every row except DONE. (A4)
7. `tracker_log`: `reason(ended_line(...))` returns the reason for both kinds of line; the crash pattern matches each string `worker_result` can return for a failed run and none of the non-crash ones. (guards the coupling)

`test_pages.py` references `workers.html` only as a route and a re-render counter, so it needs no change; run it to confirm. The existing `test_architecture.py` import probes cover `workers_page`; no new import is added.

## Risks

- **Spec gap.** No Workers artboard in the repo (above). The chip set and the LOOPING / CRASHED rules are my reading of the Acceptance text, not the canvas.
- **Board and page disagree on RUNNING.** The board's RUNNING tile (`render_board.flow_tiles`) and WORKERS panel count `sources.workers` unfiltered, so a taskless session still appears there and not here. Filtering in `board_sources.workers()` instead would change the board panel and `test_worker_facts.py`. I scope A5 to the Workers page, as the issue title says; flag for the owner if the board should follow.
- **NEEDS INPUT means two things.** This page's tile counts today's human-review ends; the board's NEEDS INPUT tile counts held cards (`hold_causes`). Same word, different number. Not changed here; renaming only.
- **Crash classification is string-matching** on the launcher's own reasons. The coupling test (7) is the guard; a reason reworded without updating the pattern fails a test, not the page.
- **`.chip` styling.** The vendored theme's `.chip` rules are not on this page; the page must carry its own CSS or the chips render as plain text.
- **Existing test contract changes** (column heads, `need you`). Intentional, listed above.

## Open questions for the owner

1. Is the Acceptance list enough, or is the v53 Workers artboard available to match exactly?
2. Does LOOPING mean "any session" (the proposal) or "a session inside a loop", which needs a new registration field?
3. Is a `re-dispatch` link to the task page acceptable, or should the page launch a dispatch itself (a new POST route)?

## Logic

```
Glossary
  r1 := the Acceptance list is the whole spec for this page        (no Workers artboard in the repo)
  c  := a worker's kind is "session"
  t  := the worker's task cell is non-blank
  l  := the worker is listed under RUNNING
  o  := an ended line's status is HUMAN REVIEW NEEDED
  k  := its reason matches a launcher failure string (worker_result / adopted run)
  K  := the ended chip reads CRASHED
  D  := the ended chip reads DONE
  p  := the line's status is "completed; awaiting integration"
  A  := a re-dispatch link is shown
  N  := the NEEDS INPUT tile value
  h  := count of today's human-review ends

Premises
  P1 [issue #235 A5]               c ∧ ¬t → ¬l
  P2 [board_sources.workers]       kind ∈ {one-shot, session}; a one-shot always has a task name
  P3 [tracker_log.ENDED, RELAUNCH] ended status ∈ {completed, human review, relaunch}; no "crashed" status exists
  P4 [one_shot.worker_result]      every failed-run reason matches k; a worker-requested human review does not
  P5 [issue #235 A4]               D ∨ K are the two named chips
  P6 [workers_page.render today]   need = count of human-review ends
  P7 [pages.do_POST]               no dispatch route exists

Derivation
  1. P1, P2:        l ↔ ¬(c ∧ ¬t)
  2. P3, P4:        K ↔ o ∧ k          (a crash is detectable only through its reason)
  3. P3:            D ↔ p
  4. P3, 2, 3:      human review without k, and relaunch, are neither D nor K → they need chips of their own (REVIEW, RELAUNCH) or the rows would vanish
  5. P6:            N = h, unchanged by the rename
  6. P7:            A = a link to the task page, not a launch
  7. r1 unverified: chip colours, LOOPING's meaning and the artboard layout stay provisional

∴ the page filters taskless sessions (1), classifies CRASHED from the reason text guarded by a coupling test (2),
  adds REVIEW and RELAUNCH so no ended row is lost (4), keeps the NEEDS INPUT value and renames its label (5),
  and offers a link, not a launch, for re-dispatch (6). Items resting on r1 are provisional until an owner answers 1-3.
```
