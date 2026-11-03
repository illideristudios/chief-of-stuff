# Issue #410: build coverage when an end precedes every move

## Outcome and scope

Add regression coverage proving that a closed issue whose close time precedes every move of its tasks cannot prevent unrelated live work from appearing on the board. Two tasks sharing the closed issue must pass through the real Flow build, lose all their segments, and merge safely into one empty row. The empty group is omitted from the rendered Flow charts; the live task remains visible.

This is a test coverage change. The current production code already produces the expected result. Implement in `evals/test_closed_issue_stop.py`; retain the existing low-level crash regression in `evals/test_flow_chart.py`. No runtime behavior change is planned.

## Existing behavior and coverage gap

- `scripts/task_forge.py:forge_ends` maps each task on a closed issue to its close time and the word `closed`, unless a merged change takes precedence.
- `scripts/flow_chart.py:build` constructs stage segments and then clips them using `ended`. A close time strictly before every move removes every segment. The `ends` argument is for estimates and does not reproduce this case.
- `scripts/flow_chart.py:_merge_issues` groups tasks sharing an issue. The #406 guard clips a finished group only when its winning row has segments. With no timed entries, the first row supplies the name and the last supplies status/note.
- `scripts/flow_chart.py:section` excludes rows with no segments intersecting its window, so an empty closed group has no chart row or summary count.
- `scripts/render_board.py:render` connects forge facts to `build(ended=forge_ends(...))` and renders the returned rows.
- `IssueRowTest.test_a_finished_group_with_no_segment_in_any_row_merges_without_raising` in `evals/test_flow_chart.py` constructs empty `gantt.Row` values and calls `_merge_issues` directly. It does not prove that end clipping creates this input.
- `FlowChartEndedTest` in `evals/test_closed_issue_stop.py` covers clipping that leaves segments. Its existing cross-project closure test does not produce an entirely empty shared-issue group.

## Implementation steps

1. Add a small, generic tracker fixture beside the closure/build tests in `evals/test_closed_issue_stop.py`. Reuse the module's timezone, date, lane and clock helpers. Derive tracker dates from `TODAY`; use fixed relative clock times consistent with `NOW`, with no wall-clock dependency or external forge access.
   - Two distinct tasks, in tracker and move order, share issue `#410` and have ordinary nonterminal stage moves at 01:00 and 01:10.
   - The source contains that issue as closed at 00:30, with no merged changes. Both tasks have moves strictly after closure; terminal-only moves must not be the reason their rows are empty.
   - A third task on another issue has a nonterminal move at 00:45 and remains running through `NOW` (02:10). Give it no forecast or hold so its expected segment is unambiguous.
2. Add a build-level regression using real parsed tracker tasks, `fc.moves`, `rb.forge_ends`, and `fc.build`.
   - Assert the end map includes both shared-issue tasks at 00:30 with `closed` and excludes the live task.
   - Assert exactly one built row for `#410`, with the first task's name, status `closed`, note `closed 00:30`, and `segments == ()`.
   - Assert exactly one separate live row for the other issue, status `running`, with its expected nonempty stage segment from 00:45 through `NOW`.
   - Do not fabricate empty Gantt rows or mock `build`/`_merge_issues`; the public build must exercise clipping, grouping and sorting.
3. Add a board-level regression using the same tracker and `bs.Sources` in the real `rb.render` path. Construct a minimal coordinator config with the existing workspace parser and the fixture's timezone/date.
   - Assert rendering succeeds and emits both Flow chart windows.
   - Assert each chart contains the live task's Gantt row and stage bar; check Flow row markup rather than a task name anywhere in the page, since Build cards can also contain names.
   - Assert the empty closed group has no Gantt row, and each chart's summary counts the single running row without a merged/closed group count. Avoid whole-page snapshots and exact CSS assertions.
4. Keep the implementation limited to these regressions. They should pass against the existing #406 fix. A new test need not fail on the fixed baseline; demonstrate its regression sensitivity by temporarily restoring the pre-#406 unguarded finished-row clipping in memory or in an isolated temporary copy, running the new tests to observe the empty-row failure, then running against the unchanged production code. Do not commit a deliberately broken production edit. If the real path unexpectedly fails on the current branch, investigate and obtain amended file ownership before adding a runtime change.

## Validation and acceptance

Run the focused suite from the repository root:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=evals python3 -m unittest test_flow_chart test_closed_issue_stop test_flow_board test_gantt
```

This suite covers the existing empty-group guard, finished-group clipping, closure wiring, chart summaries and Gantt rendering. Record final passed, failed, skipped and xfailed counts for the engine. Run the repository-wide Python suite if implementation changes extend beyond the planned test file or the focused checks reveal a wider concern:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s evals -p 'test_*.py'
```

Acceptance requires a public-build regression that genuinely creates a multirow empty finished group, and a rendered-board regression proving unrelated live work remains visible. Both must use real build/group/render behavior and pass alongside the existing focused suite.

Planning evidence: the focused baseline passed 237 tests (0 failed, 0 skipped, 0 xfailed). A read-only, in-memory reproduction using the three-task fixture above returned one `closed #410` row with no segments and one running live row ending at `NOW`; rendering produced two Flow charts, each containing the live task and neither containing the empty closed Flow row. No new regression tests were added during the plan stage.

## Risks

- A single affected task never enters the multirow merge branch; require two tasks sharing one issue.
- Earlier tracker history can leave a segment before closure and silently miss the bug; keep all affected moves strictly after the close time and supply no earlier history for those tasks.
- A task moved only to its terminal stage can be empty without clipping; use nonterminal moves and explicitly verify the generated end map.
- Merely asserting that rendering returns HTML can pass while all live work disappears; assert the live row, bar and per-window summary.
- Counting task names over the entire board can confuse Build cards with Flow rows; scope assertions to chart markup. Empty-group omission applies to Flow, not to removal of tracker tasks or Build cards.
- Changing group selection or showing placeholder rows would expand the issue into a behavior change; preserve the existing first-name/last-note fallback and window filtering.

## Proposed implementation file ownership

| Path | Mode | Purpose |
| --- | --- | --- |
| `evals/test_closed_issue_stop.py` | write | Add the shared fixture and build/board regressions. |
| `evals/test_flow_chart.py` | read | Reuse existing time/lane helpers and inspect #406 coverage. |
| `scripts/flow_chart.py` | read | Verify clipping, empty-group merging and window filtering. |
| `scripts/render_board.py` | read | Exercise real board-to-build wiring. |
| `scripts/task_forge.py` | read | Verify forge closure end mapping. |
| `scripts/board_sources.py` | read | Construct closed-issue source facts. |
| `scripts/tracker.py` | read | Verify task parsing and grouping identity. |
| `scripts/tracker_log.py` | read | Verify stage-log move parsing. |
| `scripts/workspace.py` | read | Construct the coordinator configuration for rendering. |
| `scripts/gantt.py` | read | Inspect stable row/bar markup for assertions. |
| `evals/test_flow_board.py` | read | Run neighboring board regressions. |
| `evals/test_gantt.py` | read | Run neighboring chart regressions. |

The plan-stage write scope is only this document. The implementation ownership above proposes one test-file write and no production-file writes.
