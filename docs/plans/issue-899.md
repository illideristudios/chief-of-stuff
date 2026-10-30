# Issue 899: truthful board deadline clocks

## Outcome and scope

When all configured deadlines have expired, a newly rendered board selects the existing synthetic `end of day` deadline. The header and `data-deadline` describe the same instant. A clock whose selected instant subsequently passes displays `<name> passed <elapsed> ago`, with a nonnegative duration, rather than `<name> in -<duration>`.

This stage changes only this plan. Implementation should use generic test fixtures, leave workspace configuration intact, and require no changes to the live Gauntlet workspace, installed package, publisher, or engine state.

## Findings in this checkout

- `scripts/estimate.py:nearest_deadline` scans the chronologically sorted deadlines for `at >= now`, then incorrectly returns the last configured deadline when the scan finds none. Both configuration parsing and decision-deadline projection sort their results in `scripts/workspace.py`.
- `scripts/render_board.py:render` uses that result for `panels.Header` and the board's `data-deadline` / `data-deadline-name`. Its inline script captures the selected instant once and updates a signed duration every second without changing the `in` label.
- `scripts/panels.py:header` always prints `in`, including for negative durations. `evals/test_panels.py:Header.test_a_past_deadline_counts_negative` explicitly preserves that behavior today and must become a regression for truthful expired wording.
- In this checkout, Flow axes already use `flow_chart.WINDOWS` around `now`, through `flow_chart.section` and `gantt.window`. They do not receive `nearest_deadline`. The issue's description of deadline-anchored axes does not match this implementation; preserve the existing windows and verify them rather than redesigning chart geometry.
- `governing_deadline` already falls back to `_end_of_day`; `horizon` uses next local midnight for estimates. `_end_of_day` currently means 23:59 in the workspace zone. Those task-planning semantics are separately tested and should stay intact.
- The existing Node behavioral test pattern is in `evals/test_page_reload_node.py`, but `.github/ci/Dockerfile` does not install Node. The new clock tests need an explicit CI runtime rather than another silently skipped suite.

## Behavior contract

1. Keep the first configured deadline at or after `now`, including a deadline exactly equal to `now` and deadlines scoped to named tasks. Retain the distinction between the display clock and the deadline governing unnamed tasks.
2. If there is no such deadline, use `_end_of_day(cfg, now)`, whether the configured list is empty or entirely expired. Do not delete expired deadlines: explicit task dues can still reference them.
3. Preserve the current 23:59 fallback convention. Before or exactly at that instant, show `end of day in ...`; after it, show `end of day passed ... ago`. A fresh render after local midnight selects the new day's fallback. Changing the shared cutoff to midnight would also change governing task endpoints and is outside this fix.
4. A selected instant in the future shows `<name> in <H>h<MM>m<SS>s`. Exact equality shows `in 0h00m00s`. Any strictly expired instant shows `<name> passed <H>h<MM>m<SS>s ago`. Use absolute elapsed seconds, truncated to whole seconds; even a subsecond expiry must use `passed` without a negative zero. Hours may exceed 24; no day-formatting feature is required.
5. Use the same wording, duration rounding, and absolute elapsed-time calculation in initial HTML and browser ticks. Calculate elapsed seconds from instants (UTC/timestamps), so a timezone offset transition does not make Python and JavaScript disagree. Keep the separate `panels.hm` worker-runtime formatter unchanged.
6. On an already open page, retain the selected deadline's name and instant when it expires and switch its wording to `passed ... ago`. Do not clamp at zero, reload the page every second, or synthesize the next day's timezone rules in JavaScript. A fresh server render remains responsible for selecting a new target.
7. Keep `data-deadline`, `data-deadline-name`, and the countdown's `<time datetime>` synchronized at render. Browser ticks change visible clock text only. Deadline names continue to be escaped in HTML and inserted with `textContent` in JavaScript.

## Implementation sequence

1. **Add failing selection and render regressions.** Extend `evals/test_render_board.py` near the existing deadline-scope tests with a generic configured deadline a week before a fixed aware `now`. Assert the selected fallback's name and zoned instant, the board attributes, `<time datetime>`, and positive initial header text. Cover an empty list, mixed past/future deadlines, exact equality, and a future named-only deadline. Keep existing decision-deadline and named-task governing behavior covered.
2. **Specify expired header output.** Replace the negative-countdown expectation in `evals/test_panels.py` with the contract above. Test future, equal, expired, subsecond expiry, a duration over 24 hours, and a name needing HTML escaping. Include the final minute after the 23:59 fallback and an offset-transition case that verifies elapsed-time arithmetic. These tests should fail before the corresponding production changes.
3. **Run the actual clock JavaScript.** Add `evals/test_board_clock_node.py` using Python unittest, Node built-ins, and a minimal DOM/fake-clock driver following the existing reload-test approach. Extract the clock script from actual `render_board.render` output; do not copy its implementation into the driver. Stub only the selectors/attributes used, capture `setInterval`, and control `Date` so one page can cross its deadline. Assert displayed name, duration, current-time clock, and 1000 ms cadence. Treat runtime failures and timeouts as failures. Add `nodejs` to `.github/ci/Dockerfile` so these tests execute in CI; missing Node must not count as a passing or skipped clock regression.
4. **Correct target selection.** In `scripts/estimate.py`, remove the last-expired-deadline return and use the existing `_end_of_day` fallback after the scan. Update the function's docstring to describe the fallback. Keep `governing_deadline`, `_end_of_day`, `horizon`, and explicit task due resolution unchanged.
5. **Make HTML and ticks truthful.** In `scripts/panels.py`, render future/equal and expired states using the same duration contract. In `scripts/render_board.py`, update the due clock's label and elapsed/countdown text on every tick, using `root.dataset.deadlineName` for the original name. Scope the label selector to `.panels-due` so the separate `now` label is never overwritten. Keep the existing public header fields, escaped attributes, and per-second update cadence.
6. **Verify, then commit focused changes.** Run the focused suites and full Python suite below. Review the diff for accidental changes to governing deadlines, chart windows, worker durations, configuration, or per-request render clock precision. Commit the implementation and meaningful regressions without merging or changing deployment state.

## Regression matrix

| Case | Required observation |
| --- | --- |
| All deadlines a week in the past | Fresh render chooses local end of day; attributes and header agree; no old negative countdown |
| No configured deadlines | Same fallback as the all-expired case |
| Past and future deadlines | First upcoming deadline still wins |
| Deadline equals now | Configured target retained; `in 0h00m00s` |
| Named-only future deadline | Display still selects it; unnamed task horizon remains independent |
| Decision adds an upcoming deadline | Projected deadline still reaches header and attributes |
| Open tab crosses target | Future, zero, then `passed ... ago` on successive ticks; duration never negative |
| Immediate script execution on stale HTML | First tick corrects expired wording without waiting for an interval |
| Final minute of day / next local midnight | Existing 23:59 convention is explicit; stale tab says passed; fresh next-day render selects that day's fallback |
| Workspace zone differs from process zone | Fallback uses workspace date/offset under the renderer's aware-local-now contract |
| DST offset change between instants | Initial HTML and tick agree on absolute elapsed seconds and workspace current time |
| Escaped deadline name | HTML remains escaped; browser uses literal text, including after expiry |
| Flow rows with all deadlines expired | Both chart windows remain centered around `now`; task forecast horizon still follows existing planning rules |

## Validation and completion evidence

Use fixed aware datetimes, generic task/deadline names, temporary fixture roots, and fake browser time. No live server or user workspace data is required.

Focused implementation command:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=evals python3 -B -m unittest test_render_board test_panels test_flow_board test_flow_chart test_page_reload_node test_board_clock_node
```

Full regression command:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s evals -p 'test_*.py'
```

The full suite includes page-render stability checks; preserve the minute-rounded server clock in `render_board.write` so the new text does not cause unnecessary reloads. Record actual passed, failed, skipped, and xfailed counts, distinguishing focused results from full-suite results. Do not describe the bug as fixed until new regressions fail on the old implementation and pass on the change.

Planning-stage evidence: reproduced the issue with existing generic fixtures at a fixed 2026-10-18 17:42 America/Chicago render time: selection returned expired `Final` at 2026-09-20 12:00, and the header printed `Final in` with `-677h42m00s`. The unchanged focused baseline (`test_render_board`, `test_panels`, `test_flow_board`, `test_flow_chart`, `test_page_reload_node`) completed **360 passed, 0 failed, 0 skipped, 0 xfailed**. Python 3.15.0 and Node v26.12.0 resolve through the configured login/interactive shell. The full suite was not run during planning.

## Risks and boundaries

- Merely changing selection fixes a new render but leaves open tabs capable of showing negative time. The executable tick regressions are necessary to cover that second path.
- The 23:59 fallback can already be past during the day's final minute. The passed state handles it explicitly; changing the shared planning cutoff would require a separate decision and additional endpoint changes.
- Python wall-time subtraction and JavaScript timestamp subtraction can diverge across DST. Use instant-based arithmetic for the displayed duration and test both offset directions without changing task scheduling arithmetic.
- Server text must remain useful without JavaScript. Keep initial and ticking labels consistent, including fractional seconds and exact equality.
- This checkout differs from the installed version described in the issue. Apply the fix to these source paths; do not introduce an engine-specific clock dependency or assume historical axis wiring still exists.
- Adding Node to the CI image changes its build dependencies, but uses the existing apt installation and no JavaScript packages. Verify the image build and test discovery; report any environment limitation explicitly.

## Proposed implementation file ownership

| Repository-relative path | Mode | Purpose |
| --- | --- | --- |
| `scripts/estimate.py` | write | Correct all-expired deadline selection |
| `scripts/panels.py` | write | Truthful initial header state and duration |
| `scripts/render_board.py` | write | Truthful live clock updates |
| `evals/test_render_board.py` | write | Selection, attributes, and rendered integration regressions |
| `evals/test_panels.py` | write | Replace negative-clock expectation and cover header boundaries |
| `evals/test_board_clock_node.py` | write | Execute actual inline clock across deadline transitions |
| `.github/ci/Dockerfile` | write | Make Node clock regressions executable in CI |
| `scripts/workspace.py` | read | Sorted deadlines, zones, and decision projection |
| `scripts/flow_chart.py` | read | Existing now-centered chart windows |
| `scripts/gantt.py` | read | Window and axis semantics |
| `scripts/workers_page.py` | read | Shared duration formatter consumer |
| `evals/test_flow_board.py` | read | Existing board clock, layout, and integration checks |
| `evals/test_flow_chart.py` | read | Flow window and forecast regressions |
| `evals/test_page_reload_node.py` | read | Existing Node execution pattern |
| `evals/test_pages.py` | read | Render byte stability and page serving checks |
| `.github/workflows/tests.yml` | read | Canonical suite execution in CI |
