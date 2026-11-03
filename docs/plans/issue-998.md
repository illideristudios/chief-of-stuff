# Issue 998 — Every task-history step names its day

## Problem

On a task page, "How it got here" prints `Sat 21:29` and `Mon 14:14` for earlier days, then a bare `02:51` once a step falls on the page's local today. A reader who has just seen `Mon 14:14` reads `02:51` as earlier that Monday. The step is Tuesday. The Flow bar labels on the same page already name the day; this bug is only the history times.

`scripts/issue_page.py` `_clock` is the cause: it returns `{at:%H:%M}` when `at.date() == now.date()` and `{at:%a %H:%M}` otherwise. Both history builders call it: the tracker Log path, and the store path that replaces those steps when the page has a store item. The live board (the pages in the report) is the store path.

```
D(s) := step s shows its weekday
T(s) := step s is today in the page zone
P(s) := an earlier step in the list shows a weekday
R(s) := the reader dates s to that earlier step's day

1. T(s) → ¬D(s)                         [_clock drops the weekday today]
2. P(s) ∧ ¬D(s) → R(s)                  [a bare time continues the labelled list]
3. T(s) ∧ P(s) → the earlier day ≠ s's day
∴ T(s) ∧ P(s) → R(s)                    [today's steps are read as the previous day]
```

## Decision

Every history step prints `{weekday} {HH:MM}` in the page's zone, including steps from today. `Mon 14:14` then `Tue 02:51`.

A "Today" separator row was the other option in the issue. It is not the change. The time column is already 70px in the shared timeline CSS (`decision_page.py` `HEAD`, `.step`), and past steps already render `%a %H:%M` in it, so today's steps fit with no CSS change. A separator would be a new row type in that shared stylesheet, and a bare time would still be right only while the reader kept the separator in mind. The weekday sits on the step.

One format for every history step. Do not special-case "only when a previous day is also on the page." A same-day list reads `Sat 00:05`, `Sat 00:20`.

Do not put the weekday inside `_clock`. That function also formats "What happens next" forecasts (`~HH:MM–HH:MM`, locked by `WhatHappensNextTest.test_forecast_stages_from_the_flow_row`) and Review thread times. Add a history-only helper and call it from the two history builders.

```python
def _step_when(at: datetime, now: datetime) -> str:
    if at.tzinfo is not None and now.tzinfo is not None:
        at = at.astimezone(now.tzinfo)
    return f"{at:%a %H:%M}"
```

Zone the aware timestamp before formatting. Store stamps are ISO and often UTC. `clock.parse_stamp` already converts to `now`'s zone; the helper does it again so a caller that passes UTC cannot label Monday night in Chicago as Tuesday. Naive timestamps keep `at.date()`, matching `_clock`.

`%a` stays. Older steps already use it. Do not add an English weekday table.

## Tests

Behaviour starts as a failing case in `evals/test_issue_page.py`. Run it and see it fail on today's bare `HH:MM` before editing `scripts/issue_page.py`. Then turn it green. Existing history tests search for `HH:MM` as a substring, so `Sat 00:05` still matches them; the new assertions must read the `<span class="when">` text inside the "How it got here" section only. Workers reuse that class for durations (`3h55m`) in a different section.

1. Tracker path, the existing `page()` fixture. `NOW` is Saturday 2026-09-26 02:10 America/Chicago, so yesterday's `22:00` is Friday and today's log lines are Saturday. Assert the section's when-texts are exactly `Fri 22:00` and then only `Sat HH:MM`, each matching `[A-Z][a-z]{2} \d{2}:\d{2}`, with no bare `HH:MM`.

2. Store path, the live board. Render the way `evals/test_panel643.py` `test_issue_timeline_and_workers_use_store_segments_and_logs` does: `issue_page.render(..., db=task_forge.page_data(db))` from `evals/test_m6_store_board.py` `make_store_workspace`. `now` is Tuesday 2026-10-20 02:51 America/Chicago. Record a log at `2026-10-20T04:51:00+00:00` (Monday 23:51 CT; Tuesday in UTC) and a log at `2026-10-20T07:51:00+00:00` (Tuesday 02:51 CT). Assert the when-texts are `Mon 23:51` and `Tue 02:51` in that order. A third log earlier on Tuesday local (for example `2026-10-20T06:10:00+00:00`, Tuesday 01:10 CT) asserts a same-day step is `Tue 01:10`, not `01:10`.

After the helper, `python3 -m unittest evals.test_issue_page evals.test_panel643` is green. The panel test is unchanged and must stay green. `test_forecast_stages_from_the_flow_row` must stay green without edits; if it fails, `_clock` was changed and the forecast format leaked.

## Out of scope

- Do not edit `architecture/ARCHITECTURE.md` or `architecture/compliance.md`. Nothing there specifies this clock. If an implementer thinks the doc disagrees, report that to the architecture agent and leave the doc alone.
- Do not change `_clock`, so forecasts and Review thread times stay as they are. Review threads can show the same bare-today pattern. That is a different surface.
- Do not change `decision_page.py` `Context.when`, the shared timeline CSS, `flow_chart._clock`, or `gantt.py`. Decision rows and the 24-hour Flow window still drop today's weekday. The reported Flow labels already name the day on the multi-day window. A full date such as `Tue 20 Oct 02:51` would overflow the 70px column and restyle every timeline.
- Two Mondays a week apart still both read `Mon HH:MM`. Past steps already do that. This issue does not widen the label.
- Workers durations, the merged-at chip on the change card, and step order are unchanged.

## Risks

- Fixing only the tracker join leaves the reported pages wrong. The store branch replaces `steps` whenever the page has a store item.
- Formatting before `astimezone` labels `2026-10-20T04:51:00Z` as Tuesday. The new test is that case.
- A locale other than C/English can make `%a` longer than `Mon`. The suite and the existing past-day labels already depend on `%a`. Do not special-case it here.
- Touching `decision_page.py` to widen `.step` changes decision pages that this issue does not cover.
