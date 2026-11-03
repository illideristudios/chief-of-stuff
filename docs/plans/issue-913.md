# Issue #913: count waiting after the last worker

## Goal and acceptance

For an open task with at least one worker interval, the task page's `Worked A of B elapsed · C waiting` line must use the render's supplied `now` as the elapsed endpoint. Keep the origin at the earliest worker start. Worked time remains the union of worker intervals, so concurrent workers count once; waiting remains elapsed minus worked. Time after a failed or completed attempt counts as waiting while the task remains open, whether it awaits a decision, a hold, or a worker slot. A worker's outcome does not close its task.

An authoritative task close, merge, or existing terminal completion can still freeze elapsed at that completion time. Preserve the established completion precedence, duration formatting, worker runtime totals, and no-worker presentation. Do not infer closure from the last worker's end, the presence of a hold, or a forecast's endpoint. Waiting does not start at a decision's creation time: it includes the entire idle interval since the worker stopped.

## Checkout evidence and implementation prerequisite

This plan was prepared against commit `50091f80`. The assignment describes an installed 0.129.96 store-backed page, including `end = max((e for _, e in spans), default=now)`. That implementation is absent from this checkout. Here, `scripts/issue_page.py` renders tracker-based `/issues/<number>` pages, not store-key pages, and there is no store module in `scripts/`.

The current tracker renderer already has the intended behavior for the reported shape:

- `scripts/issue_page.py:246` gets the elapsed endpoint from completed Flow segments, defaulting to `now`; it does not take the maximum worker end.
- Lines 260–267 merge worker intervals and render elapsed minus worked.
- `scripts/task_forge.py:115` supplies authoritative forge merge/close times, and `scripts/flow_chart.py` clips completed rows and resolves tracker done clocks.
- `evals/test_issue_page.py` covers overlapping workers, merge caps, hold causes, and completion clocks across midnight. `evals/test_closed_issue_stop.py` covers closed issues and grouped terminal rows.

A deterministic probe using the existing generic tracker fixture launched a worker at 20:05, ended it at 22:15, left the task open with a pending decision, and rendered at 00:05 the next day. It produced `Worked 2h10m of 4h00m elapsed · 1h50m waiting`, as expected. This is evidence about this checkout only, not verification of the installed store-backed page.

Before implementation, inspect the engine-provided implementation baseline for the store-backed function and its render tests. Reproduce the bug through that function before editing it. Do not import an installed release into this worktree or build a store migration to address this issue. If the implementation baseline is still this tracker-only tree, add the focused regression coverage here and report that the installed-path correction remains unverified; a passing legacy test alone is insufficient evidence that the reported defect is fixed.

## Implementation steps

1. Add a focused regression in `evals/test_issue_page.py` using a fixed, timezone-aware render clock and generic fixture data. On a store-backed implementation baseline, use its existing temporary-store fixture and public task-page render entry point. Create an open task with one ended failed attempt and an unanswered decision. Assert the exact worked/elapsed/waiting line. Render the same unchanged task at a later `now`: worked must stay constant while elapsed and waiting increase. Confirm this fails against the reported worker-end calculation. Keep this test separate from the existing fixture whose last worker is still running, since that fixture hides the defect.
2. In the store-backed path of `scripts/issue_page.py`, separate the accounting endpoint from worker endpoints. Use supplied `now` for an open task and the existing authoritative completion timestamp for a terminal task. Preserve the existing fallback when a terminal task has no recorded close time; do not fabricate a close from decision timestamps. Apply the same accounting boundary when ending a live interval. Keep ended workers' actual ends and the existing union calculation for worked time. The minimal open-task correction replaces the last-worker-derived elapsed endpoint, without changing how decisions or dispatch work.
3. Add the remaining boundary cases below using existing helpers. Reuse existing completion metadata and status rules rather than introducing a new lifecycle abstraction. If a small local accounting helper is warranted to share the calculation, keep it in `scripts/issue_page.py`; no dependency or schema change is needed.
4. Run the targeted suites, then the repository's normal Python suite on the implementation baseline. Review the diff for scope and verify that both open and terminal accounting still use the expected clocks. Commit the focused tests and implementation without merging.

## Regression matrix

Derive fixture dates from the suite's fixed clock/date constants; use generic issues and paths, not the real workspace database or issue payloads. Assert rendered text through the task-page entry point rather than matching the source expression.

| Case | Expected result |
| --- | --- |
| Open task; a failed attempt runs for 1 minute; render 8h25m after its start; pending decision | `Worked 1m of 8h25m elapsed · 8h24m waiting` |
| Same task rendered one hour later, no new events | Worked stays 1m; elapsed is 9h25m; waiting is 9h24m |
| Ended attempt reported successful while the task still awaits review/input | Same open-task accounting; worker success does not freeze elapsed |
| Hold or free-slot wait without an unanswered decision | Elapsed still reaches `now`; no dependency on decision creation or chip text |
| Two separated ended attempts | Worked includes both intervals; waiting includes both the internal gap and the trailing idle period |
| Overlapping ended workers followed by idle time | Union duration counts overlap once, and trailing idle time contributes to waiting |
| Live worker reaching `now` | Elapsed reaches `now`; the live interval contributes work rather than trailing waiting |
| Worker ends, task later closes with a known timestamp | Include the wait between worker end and close, then freeze elapsed/waiting across later renders |
| Existing merge, tracker done clock, and grouped terminal completion | Preserve existing caps, timezone handling, and across-midnight results |
| No worker intervals | Preserve the current absence of a worked/elapsed line; do not invent a task start |

The late-close test should use an already ended worker: a live worker capped at close cannot distinguish “close time” from “last worker end.” Retain coverage for nonnegative waiting with valid intervals. Do not expand this fix into malformed timestamp repair or a new definition of elapsed time.

## Validation

Planning-stage baseline: `python3 -m unittest discover -s evals -p 'test_issue_page.py'` passed 54 tests, with no failures, skips, or expected failures. The final combined baseline run loaded `test_issue_page`, `test_closed_issue_stop`, and `test_flow_chart` through `unittest.defaultTestLoader.loadTestsFromNames`: 178 passed, 0 failed, 0 skipped, and 0 xfailed. These existing tests do not exercise the absent store-backed implementation. No product code or test files were changed during planning.

Implementation validation commands (use the implementation baseline's equivalent store tests if its test layout changed):

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evals -p 'test_issue_page.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evals -p 'test_closed_issue_stop.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evals -p 'test_flow_chart.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evals -p 'test_*.py'
```

Capture passed, failed, skipped, and xfailed counts from the final actual run. When the store-backed runtime is available, additionally render an encoded issue-key task page from a temporary store, advance its render clock without changing store data, and verify that waiting increases. Live reproduction URLs are supporting evidence, not deterministic test dependencies; do not change their databases or answer their decisions.

## Risks and mitigations

- **Wrong implementation baseline:** the cited expression is absent here and the tracker probe already passes. Require a regression against the store-backed path before claiming a fix to the installed behavior; leave the baseline mismatch explicit in the implementation outcome.
- **Terminal totals keep growing:** setting every endpoint unconditionally to `now` would regress prior close/merge/done behavior. Select the endpoint from task lifecycle state and retain existing completion tests, including a close after the last ended worker.
- **Hold graphics distort accounting:** a held Flow bar can stop at a historical boundary or extend into a forecast window. Use lifecycle state and the render clock for open-task elapsed; do not use hold/forecast geometry as a completion signal.
- **Worked time changes accidentally:** retain the interval union and worker totals; cover overlaps, multiple attempts, and a live worker independently of trailing waiting.
- **Clock and formatting drift:** use the single supplied aware `now`, existing timestamp normalization, and existing duration formatter. Test exact minute boundaries and a wait across midnight; do not add a client-side timer or promise live updates without a new render.

## Proposed implementation ownership

| Repository-relative path | Mode | Purpose |
| --- | --- | --- |
| `scripts/issue_page.py` | write | Correct the accounting endpoint in the task-page implementation |
| `evals/test_issue_page.py` | write | Add trailing-wait and lifecycle regression coverage |
| `evals/test_closed_issue_stop.py` | read | Preserve close and grouped-row caps |
| `evals/test_flow_chart.py` | read | Reuse deterministic clocks and Flow fixtures |
| `scripts/flow_chart.py` | read | Understand completion boundaries and held/forecast rows |
| `scripts/task_forge.py` | read | Preserve merge/close precedence |
| `scripts/board_sources.py` | read | Understand completion timestamp fields |
| `scripts/clock.py` | read | Preserve formatting and done-clock semantics |
| `scripts/pages.py` | read | Verify route-to-render integration |

If the store-backed baseline requires different support or test files, identify the concrete repository-relative paths and have the engine reconcile ownership before editing outside this set. This planning stage changes only `docs/plans/issue-913.md`.
