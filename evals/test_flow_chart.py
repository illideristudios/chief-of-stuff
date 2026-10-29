"""flow_chart.py: the tracker's `stage:` Log lines become gantt rows, across days, with hold, merge and forecast."""

import re
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import board_sources as bs  # noqa: E402
import flow_chart as fc  # noqa: E402
import gantt  # noqa: E402
import one_shot  # noqa: E402
import render_board as rb  # noqa: E402
from workspace import Backlog, parse_coordinator  # noqa: E402
import settings as st  # noqa: E402
import tracker_log as tl  # noqa: E402
import tracker_write as tw  # noqa: E402

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 26, 2, 10, tzinfo=CT)
TODAY, YESTERDAY = NOW.date(), NOW.date() - timedelta(days=1)
H = timedelta(hours=1)
LANES = {"build": st.Lane(("implement", "pr", "review", "triage", "merge", "main"), ("triage", "merge"))}
KANBAN = st.Kanban(("todo", "doing", "decide"), "needs-review", {"implement": 0, "pr": 1, "review": 1, "triage": 2,
                   "merge": 2, "main": 1}, hold_stages=("triage",))

TODAY_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Upload size limit | Cap uploads | impl-1 | running 01:00 | 2026-09-25 |  | M | build | pr | #109 | c |
| Cache warmup | Warm the pool | Robin | waiting | 2026-09-26 |  | S | build | triage | #111 | c |
| Locale fallback | Fall back to en | Robin | done 00:41 | 2026-09-26 |  | S | build | main | #98 | c |
| Session timeout | Pick a timeout | unassigned | open | 2026-09-26 |  | S | build | implement | #107 | c |

## Log

- 00:05 opened the day
- 00:30 stage: Locale fallback → merge
- 00:41 stage: Locale fallback → main
- 01:00 stage: Upload size limit → pr
- 01:40 stage: Cache warmup → triage
"""
YESTERDAY_TRACKER = """# Tracker

## Resume

- 21:00 stage: Upload size limit → review

## Log

- 22:00 stage: Upload size limit → implement
- 23:00 stage: Cache warmup → review
"""


def moves() -> list[fc.Move]:
    return fc.moves(YESTERDAY_TRACKER, YESTERDAY, CT) + fc.moves(TODAY_TRACKER, TODAY, CT)


def rows(ends: dict | None = None, durations: dict | None = None, slots: int = 1,
         tracker: str = TODAY_TRACKER, approved: frozenset = frozenset()) -> dict[str, tuple[str, gantt.Row]]:
    tasks = rb.parse_tracker(tracker).tasks
    log = fc.moves(YESTERDAY_TRACKER, YESTERDAY, CT) + fc.moves(tracker, TODAY, CT)
    built = fc.build(log, tasks, LANES, {"Cache warmup": "Robin"}, ends or {}, NOW, durations or {}, slots, approved)
    return {row.name: (status, row) for status, row in built}


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time(), tzinfo=CT)


class MovesTest(unittest.TestCase):
    def test_only_the_log_is_read(self):
        self.assertEqual(fc.moves(YESTERDAY_TRACKER, YESTERDAY, CT), [
            fc.Move(at(YESTERDAY, "22:00"), "Upload size limit", "implement"),
            fc.Move(at(YESTERDAY, "23:00"), "Cache warmup", "review")])

    def test_the_writer_line_is_the_line_read(self):
        body, name = tw.set_stage(TODAY_TRACKER, "session timeout", "pr", LANES)
        line = tw.append_log(body, f"- 02:05 stage: {name} → pr")
        self.assertEqual(fc.moves(line, TODAY, CT)[-1], fc.Move(at(TODAY, "02:05"), "Session timeout", "pr"))


class BuildTest(unittest.TestCase):
    def test_a_segment_runs_from_each_move_to_the_next_and_the_last_to_now(self):
        status, row = rows()["Upload size limit"]
        self.assertEqual((status, row.ref, row.note), ("running", "#109", "pr"))
        self.assertEqual([(g.category, g.start, g.end, g.kind) for g in row.segments], [
            ("implement", at(YESTERDAY, "22:00"), at(TODAY, "01:00"), "done"),
            ("pr", at(TODAY, "01:00"), NOW, "done")])

    def test_a_held_task_is_hatched_red_from_now_through_the_window(self):
        # #227 Acceptance: "A `waiting` task owned by a person: its last stage bar ends at its last move;
        # a hold bar runs now to the end." Cache warmup is `waiting`, owned by Robin (a person), and is
        # named in `held` the same way a kanban gate hold is (rendering-board wiring for a person-owned
        # `waiting` task, not caught by any `hold_stages` gate, is WaitingOnPersonTest below). This
        # replaces the old expectation that the last stage's bar ran solid to now before the hold began —
        # that read as work in progress when nobody was working on it (#227's own "Why").
        _, row = rows()["Cache warmup"]
        self.assertEqual([(g.category, g.kind) for g in row.segments], [("review", "done"), ("triage", "hold")])
        self.assertEqual(row.segments[0].end, at(TODAY, "01:40"))  # its own last move, not now
        hold = row.segments[-1]
        self.assertEqual(hold.start, NOW)
        self.assertGreaterEqual(hold.end, NOW + max(after for _, _, after in fc.WINDOWS))
        # #227 Acceptance: "The row's note reads `needs input · <cause>`, the cause as the task page
        # names it." #228 supplies the cause (task_forge.hold_causes); here it is the person waited on.
        self.assertEqual(row.note, "needs input · Robin")

    def test_the_last_stage_of_the_lane_ends_the_row(self):
        status, row = rows()["Locale fallback"]
        self.assertEqual((status, row.note), ("merged", "merged 00:41"))
        self.assertEqual([(g.category, g.start, g.end) for g in row.segments], [("merge", at(TODAY, "00:30"), at(TODAY, "00:41"))])

    def test_a_task_without_a_move_or_a_duration_has_no_row(self):
        self.assertNotIn("Session timeout", rows())

    def test_an_estimated_end_lays_the_remaining_stages_out_as_forecast(self):
        end = NOW + 4 * H
        _, row = rows({"Cap uploads": end})["Upload size limit"]
        ahead = [(g.category, g.start, g.end) for g in row.segments if g.kind == "forecast"]
        self.assertEqual(ahead, [("pr", NOW, NOW + H), ("review", NOW + H, NOW + 2 * H),
                                 ("triage", NOW + 2 * H, NOW + 3 * H), ("merge", NOW + 3 * H, end)])
        self.assertEqual(row.note, "pr · ~06:10")

    def test_an_approved_task_ends_with_its_forecast_merge(self):
        # Flow.dc.html's 24-hour chart: "approved · merge ~02:40"; with no estimate, its 7-day label "approved".
        got = rows({"Cap uploads": NOW + 4 * H}, approved=frozenset({"Upload size limit"}))
        status, row = got["Upload size limit"]
        self.assertEqual((status, row.note), ("running", "approved · merge ~06:10"))
        self.assertEqual(rows(approved=frozenset({"Upload size limit"}))["Upload size limit"][1].note, "approved")

    def test_a_held_task_gets_no_forecast(self):
        _, row = rows({"Warm the pool": NOW + 4 * H})["Cache warmup"]
        self.assertNotIn("forecast", [g.kind for g in row.segments])


DONE_NO_CLOCK = TODAY_TRACKER.replace("| Upload size limit | Cap uploads | impl-1 | running 01:00 |",
                                      "| Upload size limit | Cap uploads | impl-1 | done |")
DONE_HHMM = TODAY_TRACKER.replace("| Upload size limit | Cap uploads | impl-1 | running 01:00 |",
                                  "| Upload size limit | Cap uploads | impl-1 | done 01:30 |")
DONE_RANGE = TODAY_TRACKER.replace("| Upload size limit | Cap uploads | impl-1 | running 01:00 |",
                                   "| Upload size limit | Cap uploads | impl-1 | done 00:50–01:30 |")

DONE_MERGE_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Part one | Part one work | w1 | done | 2026-09-26 |  | S | build | pr | #400 | c |
| Part two | Part two work | w2 | done | 2026-09-26 |  | S | build | review | #400 | c |

## Log

- 00:10 stage: Part one → implement
- 00:40 stage: Part one → pr
- 00:50 stage: Part two → review
"""

STILL_RUNS_TO_NOW = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Open task | Open work | w1 | open | 2026-09-26 |  | S | build | implement | #401 | c |
| Running task | Running work | w2 | running 00:20 | 2026-09-26 |  | S | build | pr | #402 | c |
| Waiting task | Waiting work | w3 | waiting | 2026-09-26 |  | S | build | review | #403 | c |

## Log

- 00:10 stage: Open task → implement
- 00:20 stage: Running task → pr
- 00:30 stage: Waiting task → review
"""


class DoneNoClockTest(unittest.TestCase):
    """A `done` task's last bar ends at its last move, not now (#196)."""

    def test_a_done_task_with_no_clock_draws_no_bar_past_its_last_move(self):
        last_move = at(TODAY, "01:00")  # Upload size limit's last `stage:` move in TODAY_TRACKER's Log
        _, row = rows(tracker=DONE_NO_CLOCK)["Upload size limit"]
        self.assertTrue(row.segments)
        for g in row.segments:
            with self.subTest(segment=g):
                self.assertLessEqual(g.end, last_move)
        self.assertEqual(row.segments[-1].end, last_move)

    def test_a_done_time_or_range_still_ends_the_last_bar_there(self):
        for label, tracker in (("done HH:MM", DONE_HHMM), ("done HH:MM–HH:MM", DONE_RANGE)):
            with self.subTest(state=label):
                _, row = rows(tracker=tracker)["Upload size limit"]
                self.assertEqual(row.segments[-1].end, at(TODAY, "01:30"))

    def test_several_done_no_clock_tasks_sharing_an_issue_merge_ending_at_the_latest_move(self):
        tasks = rb.parse_tracker(DONE_MERGE_TRACKER).tasks
        built = fc.build(fc.moves(DONE_MERGE_TRACKER, TODAY, CT), tasks, LANES, {}, {}, NOW)
        mine = [(s, r) for s, r in built if r.ref == "#400"]
        self.assertEqual(len(mine), 1)
        _, row = mine[0]
        latest_move = at(TODAY, "00:50")  # Part two's `stage:` move, the last move in the whole group
        for g in row.segments:
            with self.subTest(segment=g):
                self.assertLessEqual(g.end, latest_move)
        # (#196) line 1: no bar past ITS OWN task's last move, even once the group merges the two tasks'
        # segments into one row. A bar's title is its owner (no launcher lines in this fixture), so map
        # owner back to the task that owns it and its own last `stage:` move, both read from the fixture,
        # not hardcoded.
        own_last_move = {}
        for m in fc.moves(DONE_MERGE_TRACKER, TODAY, CT):
            own_last_move[m.name] = max(own_last_move.get(m.name, m.at), m.at)
        owner_task = {t.owner.strip(): t.name.strip() for t in tasks}
        for g in row.segments:
            with self.subTest(segment=g):
                self.assertLessEqual(g.end, own_last_move[owner_task[g.title]])

    def test_an_open_running_or_waiting_task_still_runs_its_last_bar_to_now(self):
        built = {row.name: (status, row) for status, row in
                 fc.build(fc.moves(STILL_RUNS_TO_NOW, TODAY, CT), rb.parse_tracker(STILL_RUNS_TO_NOW).tasks,
                          LANES, {}, {}, NOW)}
        for name in ("Open task", "Running task", "Waiting task"):
            with self.subTest(task=name):
                _, row = built[name]
                self.assertTrue(row.segments)
                self.assertEqual(row.segments[-1].end, NOW)


DONE_AFTER_MIDNIGHT = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Night owl | Ship night owl | impl-1 | done 22:21 | 2026-09-25 |  | S | build | pr | #120 | c |

## Log

- 21:00 stage: Night owl → implement
"""

DONE_SAME_DAY = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Night owl | Ship night owl | impl-1 | done 00:50 | 2026-09-26 |  | S | build | pr | #120 | c |

## Log

- 00:10 stage: Night owl → implement
"""


class DoneClockAfterMidnightTest(unittest.TestCase):
    """(#213) A done task's `HH:MM` clock names its latest occurrence at or before now, not a same-date
    occurrence that may not have happened yet."""

    def test_a_done_clock_from_the_previous_evening_stops_the_bar_there_not_at_now(self):
        # NOW is 02:10 the next day; "done 22:21" is later in the day than NOW's own time of day, so it
        # names YESTERDAY 22:21 — the only occurrence of 22:21 that has actually happened — not a today
        # 22:21 that is still 20 hours away.
        tasks = rb.parse_tracker(DONE_AFTER_MIDNIGHT).tasks
        built = fc.build(fc.moves(DONE_AFTER_MIDNIGHT, YESTERDAY, CT), tasks, LANES, {}, {}, NOW)
        _, row = {r.name: (s, r) for s, r in built}["Night owl"]
        self.assertTrue(row.segments)
        self.assertEqual(row.segments[-1].end, at(YESTERDAY, "22:21"))
        self.assertLessEqual(row.segments[-1].end, NOW)

    def test_a_done_clock_earlier_today_still_resolves_to_today(self):
        # Regression guard: just after midnight, a clock before now's own time of day still means today —
        # the fix must not walk every done clock back a day regardless of which is earlier.
        tasks = rb.parse_tracker(DONE_SAME_DAY).tasks
        built = fc.build(fc.moves(DONE_SAME_DAY, TODAY, CT), tasks, LANES, {}, {}, NOW)
        _, row = {r.name: (s, r) for s, r in built}["Night owl"]
        self.assertTrue(row.segments)
        self.assertEqual(row.segments[-1].end, at(TODAY, "00:50"))


QUEUED = TODAY_TRACKER.replace("| Session timeout |", "| Export format | Export CSV | unassigned | open | 2026-09-26 |  | M | build | implement | #112 | c |\n| Session timeout |")


class QueueTest(unittest.TestCase):
    """Open tasks at their lane's first stage with no move yet: forecast only, after the running forecasts, in tracker order."""

    def queued(self, slots: int = 1, ends: dict | None = None) -> dict[str, tuple[str, gantt.Row]]:
        return rows(ends if ends is not None else {"Cap uploads": NOW + 4 * H},
                    {"Export CSV": 5 * H, "Pick a timeout": 30 * H}, slots, QUEUED)

    def test_one_slot_runs_them_back_to_back_after_the_running_forecast(self):
        got = self.queued()
        export, timeout = got["Export format"][1], got["Session timeout"][1]
        self.assertEqual((export.segments[0].start, export.segments[-1].end), (NOW + 4 * H, NOW + 9 * H))
        self.assertEqual((timeout.segments[0].start, timeout.segments[-1].end), (NOW + 9 * H, NOW + 39 * H))
        self.assertEqual({g.kind for g in export.segments + timeout.segments}, {"forecast"})
        self.assertEqual([g.category for g in export.segments], ["implement", "pr", "review", "triage", "merge"])
        self.assertEqual((got["Export format"][0], export.ref, export.note), ("open", "#112", "queued · ~11:10"))
        self.assertEqual(timeout.note, "queued · Sun")
        self.assertEqual(list(got)[-2:], ["Export format", "Session timeout"])

    def test_free_slots_start_now(self):
        got = self.queued(slots=2)
        self.assertEqual(got["Export format"][1].segments[0].start, NOW)
        self.assertEqual(got["Session timeout"][1].segments[0].start, NOW + 4 * H)

    def test_more_running_than_slots_waits_for_a_slot(self):
        got = self.queued(ends={"Cap uploads": NOW + 4 * H, "Warm the pool": NOW + 2 * H})
        # The held task draws no forecast and so holds no slot: one running forecast, one slot.
        self.assertEqual(got["Export format"][1].segments[0].start, NOW + 4 * H)

    def test_nothing_is_laid_out_past_the_longest_window(self):
        got = rows({}, {"Export CSV": 200 * H, "Pick a timeout": H}, 1, QUEUED)
        self.assertIn("Export format", got)
        self.assertNotIn("Session timeout", got)

    def test_queued_rows_count_as_queued(self):
        # #217 Acceptance: "The summary counts the chart's own rows: merged, approved, running, needs
        # input, queued, in that order" — `held` is renamed `needs input`.
        html = fc.section(list(self.queued().values()), NOW)
        self.assertIn("1 merged · 1 running · 1 needs input · 2 queued · Thu 24 02:10 → Thu 1 02:10", html)


class SectionTest(unittest.TestCase):
    def test_no_move_no_section(self):
        self.assertEqual(fc.section([], NOW), "")

    def test_two_charts_with_counts_and_one_legend(self):
        # #217 Acceptance: "held" is renamed "needs input", and "a zero count is left out" — no "0 queued".
        html = fc.section(list(rows().values()), NOW)
        self.assertIn("<h2>Flow · 24 hours</h2>\n<div class=\"meta\">1 merged · 1 running · 1 needs input · Fri 20:10 → Sat 20:10</div>", html)
        self.assertIn("<h2>Flow · 7 days</h2>\n<div class=\"meta\">1 merged · 1 running · 1 needs input · Thu 24 02:10 → Thu 1 02:10</div>", html)
        self.assertEqual(html.count('<figure class="gantt"'), 2)
        self.assertEqual(html.count('<div class="gantt-legend">'), 1)
        self.assertLess(html.index("7 days"), html.index("gantt-legend"))

    def test_a_chart_draws_only_rows_in_its_window(self):
        old = fc.Move(NOW - 30 * H, "Old", "implement"), fc.Move(NOW - 29 * H, "Old", "main")
        built = fc.build(list(old), rb.parse_tracker(TODAY_TRACKER).tasks, {**LANES}, {}, {}, NOW)
        html = fc.section(built, NOW)
        self.assertNotIn("24 hours", html)
        self.assertIn("7 days", html)

    def test_the_legend_forecast_swatch_wears_implement(self):
        self.assertIn(".gantt-legend .gantt-forecast:not([data-cat]){--c:var(--stage-implement)}", fc.css())
        self.assertIn(".gantt-bar.gantt-forecast{background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--c) 40%,transparent)",
                      fc.css())

    def test_bars_take_the_board_stage_tokens(self):
        css = fc.css()
        for stage in gantt.PALETTE:
            self.assertIn(f'.gantt-bar[data-cat="{stage}"]{{--c:var(--stage-{stage});', css)


GROUPED_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Delta merged | Delta work | Robin | done 01:55 | 2026-09-26 |  | S | build | main | #501 | c |
| Echo approved | Echo work | Robin | open | 2026-09-26 |  | S | build | implement | #502 | c |
| Foxtrot running | Foxtrot work | impl-1 | running 00:45 | 2026-09-26 |  | S | build | implement | #503 | c |
| Golf running | Golf work | impl-2 | running 01:20 | 2026-09-26 |  | S | build | review | #504 | c |
| Charlie needs input | Charlie work | Robin | waiting | 2026-09-26 |  | S | build | triage | #505 | c |
| Hotel queued | Hotel work | unassigned | open | 2026-09-26 |  | S | build | implement | #506 | c |
| India queued | India work | unassigned | open | 2026-09-26 |  | S | build | implement | #507 | c |

## Log

- 01:50 stage: Delta merged → merge
- 01:55 stage: Delta merged → main
- 00:30 stage: Echo approved → implement
- 00:45 stage: Foxtrot running → implement
- 01:20 stage: Golf running → review
- 00:01 stage: Charlie needs input → triage
"""


class GroupedFlowTest(unittest.TestCase):
    """#226 Acceptance: "Rows group in the order merged, approved, running, needs input, queued; within a
    group, earliest first." #217 Acceptance: "The summary counts the chart's own rows: merged, approved,
    running, needs input, queued, in that order," "a row in implement, review or fix counts as running,"
    and "a zero count is left out." Charlie needs input moves earliest of anyone (00:01) yet must group
    second-to-last, not first — proof grouping outranks move order; Delta merged moves latest (01:55)
    yet must group first — proof merged still wins over move order too."""

    def built(self) -> list[tuple[str, gantt.Row]]:
        tasks = rb.parse_tracker(GROUPED_TRACKER).tasks
        return fc.build(fc.moves(GROUPED_TRACKER, TODAY, CT), tasks, LANES, {"Charlie needs input": "Charlie"}, {}, NOW,
                        {"Hotel work": 2 * H, "India work": 2 * H}, 1, frozenset({"Echo approved"}))

    def test_rows_group_merged_approved_running_needs_input_queued_earliest_first(self):
        names = [row.name for _, row in self.built()]
        self.assertEqual(names, ["Delta merged", "Echo approved", "Foxtrot running", "Golf running",
                                 "Charlie needs input", "Hotel queued", "India queued"])

    def test_summary_counts_merged_approved_running_needs_input_queued_in_order(self):
        html = fc.section(self.built(), NOW)
        self.assertIn("1 merged · 1 approved · 2 running · 1 needs input · 2 queued · Fri 20:10 → Sat 20:10", html)


class BoardTest(unittest.TestCase):
    CLAUDE = "# W\n\n## Coordinator\n\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n- Timezone: America/Chicago\n- Settings: `cos.toml`\n"

    def cfg(self):
        return parse_coordinator(self.CLAUDE, today=TODAY)

    def test_flow_sits_after_build(self):
        html = rb.render(TODAY_TRACKER, self.cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN,
                         stage_log=[(YESTERDAY, YESTERDAY_TRACKER)])
        self.assertLess(html.index("<h2>Build</h2>"), html.index("<h2>Flow · 24 hours</h2>"))
        self.assertNotIn('class="panels"', html)
        self.assertIn('title="implement Fri 22:00–Sat 01:00', html)
        self.assertIn("gantt-hold", html)

    def test_queued_tasks_take_their_size_from_closed_tasks(self):
        tracker = TODAY_TRACKER.replace("| Locale fallback | Fall back to en | Robin | done 00:41 |", "| Locale fallback | Fall back to en | Robin | done 00:10–00:41 |")
        html = rb.render(tracker, self.cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN)
        self.assertIn("Session timeout", html[html.index("Flow · 24 hours"):])
        self.assertIn("queued · ~", html)

    def test_an_open_approved_change_labels_its_task_approved(self):
        change = bs.Change("!48", "u", "Cap uploads", "open", False, "passed", True, 1, None, "main", (), ("#109",))
        html = rb.render(TODAY_TRACKER, self.cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN,
                         sources=bs.Sources({}, {}, {"!48": change}, (), {}, bs.backlog.Backlog('https://forge.example', 'grp/app')))
        self.assertIn(">approved · merge Sun<", html[html.index("Flow · 24 hours"):])
        html = rb.render(TODAY_TRACKER, self.cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN,
                         sources=bs.Sources({}, {}, {"!48": replace(change, approved=False)}, (), {}, bs.backlog.Backlog('https://forge.example', 'grp/app')))
        self.assertNotIn(">approved", html[html.index("Flow · 24 hours"):])

    def test_no_stage_line_no_flow_and_no_chart_css(self):
        """Without a stage line the board draws no Flow chart and none of flow_chart.css():
        `.gantt-legend .gantt-forecast:not([data-cat])` is its own addition on top of gantt.css()."""
        plain = TODAY_TRACKER.split("- 00:30")[0]
        html = rb.render(plain, self.cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN)
        self.assertNotIn("Flow ·", html)
        self.assertNotIn(".gantt-legend .gantt-forecast", html)

    def test_every_stage_colour_is_set_light_and_dark(self):
        html = rb.render(TODAY_TRACKER, self.cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN)
        used = set(re.findall(r"--c:var\((--stage-[\w-]+)\)", html))
        self.assertEqual(used, {f"--stage-{s}" for s in gantt.PALETTE})
        blocks = [re.search(r"^:root\{(.*?)\}$", html, re.M)[1],
                  re.search(r'^@media \(prefers-color-scheme:dark\)\{:root:not\(\[data-theme="light"\]\)\{(.*?)\}', html, re.M)[1],
                  re.search(r'^:root\[data-theme="dark"\]\{(.*?)\}', html, re.M)[1]]
        for block in blocks:
            self.assertEqual(used - set(re.findall(r"(--stage-[\w-]+):#", block)), set())

    def test_main_reads_the_trackers_the_window_covers(self):
        root = Path(tempfile.mkdtemp())
        (root / "CLAUDE.md").write_text(self.CLAUDE)
        (root / "cos.toml").write_text('[lanes]\nbuild = { stages = ["implement", "pr", "review", "triage", "merge", "main"] }\n')
        (root / "daily").mkdir()
        today = datetime.now(CT).date()
        (root / f"daily/{today}-tracker.md").write_text(TODAY_TRACKER)
        (root / f"daily/{today - timedelta(days=1)}-tracker.md").write_text(YESTERDAY_TRACKER)
        (root / f"daily/{today - timedelta(days=5)}-tracker.md").write_text(YESTERDAY_TRACKER.replace("Upload size limit", "Too old"))
        html = rb.main(["--root", str(root)]).read_text()
        self.assertIn('title="implement ', html)
        self.assertNotIn("Too old", html)


WAITING_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Dark mode tokens | Token pass | Robin | waiting | 2026-09-26 |  | S | build | review | #119 | c |
| Worker retry | Retry work | impl-9 | waiting | 2026-09-26 |  | S | build | review | #130 | c |

## Log

- 01:00 stage: Dark mode tokens → review
- 01:05 stage: Worker retry → review
"""


def gantt_row(html: str, name: str) -> str:
    """The rendered `<div class="gantt-row">` for the Flow row named `name`, first match across both
    Flow charts (24 hours, 7 days); the search starts at the first Flow heading."""
    flow_start = html.index("Flow · 24 hours")
    return next(r for r in re.findall(r'<div class="gantt-row">.*?</div>', html[flow_start:])
                if f'<span class="gantt-name">{name}</span>' in r)


class WaitingOnPersonTest(unittest.TestCase):
    """#227 Acceptance: "A `waiting` task owned by a person: its last stage bar ends at its last move; a
    hold bar runs now to the end" and "counts as needs input in the chart summary" — a `waiting` task
    owned by a worker does not. render_board.py already tells a worker owner from a person by checking
    `sources.workers` (build_columns's `worker_names`, reused by evals/test_flow_board.py's
    `test_only_a_worker_owner_becomes_a_link`); the Flow chart is asked to reuse that same check, not a
    kanban `hold_stages` gate — KANBAN here only holds "triage", never "review", so neither row is a
    kanban-style hold."""

    def html(self) -> str:
        sources = bs.Sources({}, {}, {}, (bs.Worker("impl-9", "session", "", "", "", "", None, None),), {})
        return rb.render(WAITING_TRACKER, BoardTest().cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN,
                         sources=sources)

    def test_a_person_owned_waiting_row_holds_a_worker_owned_one_does_not(self):
        html = self.html()
        self.assertIn("gantt-hold", gantt_row(html, "Dark mode tokens"))
        self.assertNotIn("gantt-hold", gantt_row(html, "Worker retry"))

    def test_a_person_owned_waiting_row_counts_as_needs_input(self):
        html = self.html()
        self.assertIn("1 needs input", html[html.index("Flow · 24 hours"):])


ONE_SHOT_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Audit headers | Security audit | unassigned | open | 2026-09-26 |  | S |  |  | #120 | c |
| Trim logs | Log trim | unassigned | open | 2026-09-26 |  | S |  |  | #121 | c |
| Pin fonts | Font pin | unassigned | open | 2026-09-26 |  | S |  |  | #122 | c |

## File ownership

| context | paths |
|---|---|
| Security audit | `src/a/` |
| Log trim | `src/b/` |
| Font pin | `src/c/` |

## Log

- 00:05 opened the day
"""

# #182: a launch records its task as the item read then; the row's item changes afterwards.
UPLOAD_ITEM = "Cap uploads at 25 MB"
UPLOAD_PROMPT = (f"{UPLOAD_ITEM}. The form refuses a larger file before sending it and names the limit. Keep the limit "
                 "in one setting that both sides read. Add tests for exactly 25 MB and one byte over.")
KEY_PROMPT = ("Rotate the signing key for the token service (#105). Swap the key in the vault before the restart, "
              "and note the new key id in the runbook.")
SWEEP_PROMPT = ("Sweep the stale branches older than thirty days in every repository and list each one that still "
                "has an open change against it.")
PROMPT_SENTENCES = ("Keep the limit in one setting that both sides read", "Swap the key in the vault before the restart")
# (row name, launch-time item, worker, tree, launched at, the item afterwards; None = the row is gone)
LAUNCHES = (("Upload size limit", UPLOAD_PROMPT, "impl-1", "upload", "00:20", UPLOAD_ITEM),
            ("Key rotation", KEY_PROMPT, "impl-2", "keys", "00:40", "Monthly key rotation"),
            ("Branch sweep", SWEEP_PROMPT, "impl-3", "sweep", "01:00", None))
LAUNCHED_TRACKER = f"""# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Upload size limit | {UPLOAD_PROMPT} | unassigned | open | {TODAY} |  | M |  |  | #104 | c |
| Key rotation | {KEY_PROMPT} | unassigned | open | {TODAY} |  | S |  |  | #105 | c |
| Branch sweep | {SWEEP_PROMPT} | unassigned | open | {TODAY} |  | S |  |  | #106 | c |

## File ownership

| context | paths |
|---|---|
| Upload size limit | `src/upload/` |
| Key rotation | `src/keys/` |
| Branch sweep | `tools/sweep/` |

## Log

- 00:05 opened the day
"""


def launched_tracker() -> str:
    """The launcher's real writer records each launch; then Upload's item shrinks to the prompt's first words, Key
    rotation's item is reworded (its prompt still carries #105), and Branch sweep's row is removed."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tracker.md"
        path.write_text(LAUNCHED_TRACKER)
        for _, prompt, worker, tree, hhmm, _ in LAUNCHES:
            one_shot.record_launch(path, prompt, worker, f"worktree `trees/{tree}` ({tree})", "claude", "opus", hhmm)
        text = path.read_text()
    for name, prompt, *_, after in LAUNCHES:
        row = f"| {name} | {prompt} |"
        text = (text.replace(row, f"| {name} | {after} |") if after else
                "".join(line for line in text.splitlines(keepends=True) if not line.startswith(row)))
    return text


def many_launched_tracker(n: int) -> tuple[str, dict[str, tuple[str, str, str]]]:
    """n rows, each launched once by the launcher's real writer, and {row name: (its ref, its launch HH:MM, the
    launch's task text)}. Even
    rows' items shrink afterwards to the launch text's first words; odd rows' items are reworded, so only the issue
    ref their launch text carries maps them (#184). Each launch is recorded on its own one-row tracker, then the
    rows and lines are joined, so the fixture costs O(n)."""
    head = LAUNCHED_TRACKER.split("| Upload size limit |", 1)[0]
    parts, expected = {"## Tasks": [], "## File ownership": [], "## Log": []}, {}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tracker.md"
        for i in range(n):
            name, ref, hhmm = f"Chore {i:04d}", f"#{10000 + i}", f"{i % 120 // 60:02d}:{i % 60:02d}"
            item, after = ((f"Tidy module {i:04d}. Keep its public names and add a test for each change.", f"Tidy module {i:04d}")
                           if i % 2 == 0 else
                           (f"Rename setting {i:04d} ({ref}). Update every reader in the same change.", f"Setting {i:04d} rename"))
            path.write_text(f"{head}| {name} | {item} | unassigned | open | {TODAY} |  | S |  |  | {ref} | c |\n\n"
                            f"## File ownership\n\n| context | paths |\n|---|---|\n| {name} | `src/m{i:04d}/` |\n\n## Log\n")
            one_shot.record_launch(path, item, f"w{i}", f"worktree `trees/t{i}` (t{i})", "claude", "opus", hhmm)
            text = path.read_text().replace(f"| {name} | {item} |", f"| {name} | {after} |")
            for heading, lines in parts.items():
                lines += [line for line in rb._section(text, heading) if line.startswith(("| C", "- "))]
            expected[name] = (ref, hhmm, item)
    return (head + "\n".join(parts["## Tasks"]) + "\n\n## File ownership\n\n| context | paths |\n|---|---|\n"
            + "\n".join(parts["## File ownership"]) + "\n\n## Log\n\n" + "\n".join(parts["## Log"]) + "\n"), expected


REVIEW_LOOP = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Documentation upload | Upload docs | w1 | waiting | 2026-09-26 |  | S | build | pr | #307 | c |
| Review !182 | Review 182 | w2 | done 00:50–01:20 | 2026-09-26 |  | S | build | review | #307 | c |
| Fix !182 R1 | Fix 182 | w3 | running 01:20 | 2026-09-26 |  | S | build | triage | #307 | c |
| Tidy config | Tidy | w4 | running 00:40 | 2026-09-26 |  | S |  |  |  | c |
| Trim cache | Trim | w5 | running 00:45 | 2026-09-26 |  | S |  |  |  | c |

## Log

- 00:10 stage: Documentation upload → implement
- 00:40 stage: Documentation upload → pr
- 00:40 stage: Tidy config → implement
- 00:45 stage: Trim cache → implement
- 00:50 stage: Review !182 → review
- 01:20 stage: Fix !182 R1 → triage
"""


class IssueRowTest(unittest.TestCase):
    """The review loop makes a Tasks row per pass; the chart draws one row per issue (#155)."""

    def built(self) -> list[tuple[str, gantt.Row]]:
        return fc.build(fc.moves(REVIEW_LOOP, TODAY, CT), rb.parse_tracker(REVIEW_LOOP).tasks, LANES, {}, {}, NOW)

    def test_tasks_sharing_an_issue_draw_on_one_row_in_time_order(self):
        mine = [(s, r) for s, r in self.built() if r.ref == "#307"]
        self.assertEqual(len(mine), 1)
        status, row = mine[0]
        # Named for the first task, with the status of the latest.
        self.assertEqual((status, row.name), ("running", "Documentation upload"))
        starts = [g.start for g in row.segments]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual([g.category for g in row.segments][:4], ["implement", "pr", "review", "triage"])

    def test_a_task_with_no_segment_still_merges(self):
        # A task whose only move is its lane's last stage draws no segment; its issue row still renders.
        text = REVIEW_LOOP.replace("| Trim cache | Trim | w5 | running 00:45 | 2026-09-26 |  | S |  |  |  | c |",
                                   "| Land docs | Land | w6 | done 01:00 | 2026-09-26 |  | S | build | main | #307 | c |")
        text = tw.append_log(text, "- 01:00 stage: Land docs → main")
        got = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        self.assertEqual(len([r for _, r in got if r.ref == "#307"]), 1)
        fc.section(got, NOW)

    def test_a_finished_group_with_no_segment_in_any_row_merges_without_raising(self):
        # #406: nothing in the group has a segment, so there is no last move to finish on; the board must still build.
        for status in ("merged", "closed", "done"):
            with self.subTest(status=status):
                rows_ = [("running", gantt.Row("#406", "First", "first note", ())),
                         (status, gantt.Row("#406", "Second", "second note", ()))]
                got = fc._merge_issues(rows_)
                self.assertEqual(len(got), 1)
                got_status, row = got[0]
                # name from the first row by position; status and note from the last
                self.assertEqual((got_status, row.name, row.note, row.segments), (status, "First", "second note", ()))

    def test_a_finished_group_clips_segments_to_the_finishing_rows_own_end(self):
        # Guard for #406's fix: with a winning row that has segments, behaviour is unchanged.
        seg = lambda a, b: gantt.Segment(at(TODAY, a), at(TODAY, b), "implement", "done")  # noqa: E731
        first, overrun, last = seg("00:10", "00:30"), seg("00:35", "02:00"), seg("00:50", "01:00")
        rows_ = [("running", gantt.Row("#406", "Early", "", (first, overrun))),
                 ("merged", gantt.Row("#406", "Late", "", (last,)))]
        (status, row), = fc._merge_issues(rows_)
        self.assertEqual((status, row.name), ("merged", "Early"))
        self.assertEqual(row.segments, (first, replace(overrun, end=last.end), last))

    def test_tasks_without_an_issue_keep_a_row_each(self):
        names = [r.name for _, r in self.built() if not r.ref]
        self.assertEqual(sorted(names), ["Tidy config", "Trim cache"])


class OneShotTest(unittest.TestCase):
    """A task with no `stage:` lines draws from the one-shot launcher's own Log lines, at the times they record (#149)."""

    def tracker(self) -> str:
        # The launcher's real writers produce the lines the chart reads.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracker.md"
            path.write_text(ONE_SHOT_TRACKER)
            one_shot.record_launch(path, "Security audit", "w1", "worktree `a` (a)", "codex", "", "00:20")
            one_shot.record_launch(path, "Log trim", "w2", "worktree `b` (b)", "codex", "", "01:00")
            one_shot.record_launch(path, "Font pin", "w3", "worktree `c` (c)", "codex", "", "00:30")
            one_shot.update_tracker(path, "Security audit", "w1", "Robin", "done", "fixed", "a.py", "01:30")
            one_shot.update_tracker(path, "Font pin", "w3", "Robin", "human_review", "unclear", "c.py", "01:10")
            return path.read_text()

    def built(self, text: str) -> dict[str, tuple[str, gantt.Row]]:
        got = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        return {row.name: (status, row) for status, row in got}

    def test_a_running_one_shot_draws_from_its_launch_to_now(self):
        status, row = self.built(self.tracker())["Trim logs"]
        self.assertEqual((status, row.ref), ("running", "#121"))
        self.assertEqual([(g.category, g.start, g.end) for g in row.segments],
                         [("implement", at(TODAY, "01:00"), NOW)])

    def test_a_finished_one_shot_moves_on_at_its_completion(self):
        status, row = self.built(self.tracker())["Audit headers"]
        self.assertEqual(status, "waiting")
        self.assertEqual([(g.category, g.start, g.end) for g in row.segments],
                         [("implement", at(TODAY, "00:20"), at(TODAY, "01:30")), ("pr", at(TODAY, "01:30"), NOW)])

    def test_a_one_shot_needing_review_moves_to_review(self):
        _, row = self.built(self.tracker())["Pin fonts"]
        self.assertEqual([(g.category, g.start, g.end) for g in row.segments],
                         [("implement", at(TODAY, "00:30"), at(TODAY, "01:10")), ("review", at(TODAY, "01:10"), NOW)])

    def test_a_done_task_ends_at_its_done_time(self):
        text = self.tracker().replace("| Robin | waiting |", "| Robin | done 00:20–01:50 |", 1)
        _, row = self.built(text)["Audit headers"]
        self.assertEqual(row.segments[-1].end, at(TODAY, "01:50"))
        self.assertTrue(all(g.end <= at(TODAY, "01:50") for g in row.segments))

    def test_stage_lines_take_over_from_their_first_move(self):
        # A coordinator that starts writing stage moves mid-run keeps the history the launcher recorded before them.
        text = tw.append_log(self.tracker(), "- 01:40 stage: Audit headers → review")
        text = tw.append_log(text, "- 01:50 one-shot w9 started: codex, worktree `a`, task Security audit")
        _, row = self.built(text)["Audit headers"]
        self.assertEqual([(g.category, g.start, g.end) for g in row.segments],
                         [("implement", at(TODAY, "00:20"), at(TODAY, "01:30")), ("pr", at(TODAY, "01:30"), at(TODAY, "01:40")),
                          ("review", at(TODAY, "01:40"), NOW)])


class LaunchRowTest(unittest.TestCase):
    """A one-shot's launch is drawn on its task's row even after the row's item stops matching the launch (#182)."""

    def setUp(self):
        text = launched_tracker()
        self.got = [row for _, row in fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)]

    def launched_at(self, hhmm: str) -> gantt.Row:
        rows = [r for r in self.got if any(g.start == at(TODAY, hhmm) for g in r.segments)]
        self.assertEqual(len(rows), 1, [r.name[:40] for r in self.got])
        return rows[0]

    def test_a_launch_is_drawn_on_its_tasks_own_row(self):
        # Rules 1–3: a started line whose task begins with a row's item, or carries the row's issue ref, is drawn on
        # that row, with its ref and name; no launcher-only row is named by the prompt.
        for hhmm, ref, name in (("00:20", "#104", "Upload size limit"), ("00:40", "#105", "Key rotation")):
            with self.subTest(task=name):
                row = self.launched_at(hhmm)
                self.assertEqual((row.ref, row.name), (ref, name))
        html = fc.section([("running", r) for r in self.got], NOW)
        for sentence in PROMPT_SENTENCES:
            with self.subTest(sentence=sentence):
                self.assertFalse(sentence in html, "the prompt's sentence is drawn")

    def test_no_row_name_is_longer_than_its_tasks_name(self):
        # Rule 4: no Flow row name is longer than the row name it maps to.
        for name, _, _, _, hhmm, after in LAUNCHES:
            if after:
                with self.subTest(task=name):
                    self.assertLessEqual(len(self.launched_at(hhmm).name), len(name))

    def test_a_launch_with_no_row_keeps_a_short_name(self):
        # Rule 5: a started line that maps to no row is named by its first line, cut to a fixed length.
        name = self.launched_at("01:00").name
        self.assertTrue(name)
        self.assertLessEqual(len(name), 80)
        self.assertTrue(SWEEP_PROMPT.splitlines()[0].startswith(name), name)


CHANGE_URL = "https://forge.example/group/project/-/merge_requests/50"
ISSUE_URL = "https://forge.example/group/project/-/issues/7"
FLOW_LAUNCH_TEMPLATE = """# Tracker {{today}}

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
{tasks}

## File ownership

| context | paths |
|---|---|
{ownership}

## Log
"""


def flow_launch_tracker(tasks: list[tuple[str, str, str]], launches: list[tuple[str, str, str]]) -> str:
    """Generic (name, item, issue) rows and (item, worker, HH:MM) real dispatches; no stage lines."""
    text = FLOW_LAUNCH_TEMPLATE.replace("{tasks}", "\n".join(
        f"| {name} | {item} | unassigned | open | {{{{today}}}} |  | S |  |  | {issue} | c |"
        for name, item, issue in tasks)).replace("{ownership}", "\n".join(
            f"| {name} | `src/task{i}/` |" for i, (name, _, _) in enumerate(tasks)))
    text = text.replace("{{today}}", str(TODAY))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tracker.md"
        path.write_text(text)
        for i, (prompt, worker, hhmm) in enumerate(launches):
            one_shot.record_launch(path, prompt, worker, f"worktree `trees/task{i}` (task{i})", "claude", "opus", hhmm)
        return path.read_text()


class RelaunchStopTest(unittest.TestCase):
    """#457: a recorded relaunch stop ends that worker's bar, even while the task remains active."""

    def tracker(self):
        return flow_launch_tracker([("Check parser", "Check parser", "#701")],
                                   [("Check parser", "worker-1", "00:10")])

    def row(self, text):
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        self.assertEqual([r.name for _, r in built], ["Check parser"])
        return built[0][1]

    def test_a_relaunch_requested_stop_ends_the_workers_bar_at_the_stop_time_not_now(self):
        text = tw.append_log(self.tracker(),
                             tl.relaunch_line("00:40", "worker-1", "Check parser", "base moved"))
        row = self.row(text)
        self.assertTrue(row.segments)
        self.assertEqual(row.segments[0].end, at(TODAY, "00:40"))
        # A stop supplies a boundary, not a new working stage stretching from the stop to now.
        self.assertEqual([(g.category, g.start, g.end, g.title) for g in row.segments],
                         [("implement", at(TODAY, "00:10"), at(TODAY, "00:40"), "worker-1")])

    def test_a_later_launch_leaves_a_gap_after_the_relaunch_requested_stop(self):
        text = tw.append_log(self.tracker(), tl.relaunch_line("00:40", "worker-1", "Check parser", "base moved"))
        text = tw.append_log(text, tl.started_line("01:00", "worker-2", "codex", "gpt-test",
                                                  "worktree `trees/retry` (retry)", "Check parser"))
        row = self.row(text)
        self.assertEqual([(g.category, g.start, g.end, g.title) for g in row.segments], [
            ("implement", at(TODAY, "00:10"), at(TODAY, "00:40"), "worker-1"),
            ("implement", at(TODAY, "01:00"), NOW, "worker-2")])

    def test_a_worker_without_a_matching_stop_still_runs_to_now(self):
        for unmatched in (False, True):
            with self.subTest(unmatched_relaunch=unmatched):
                text = self.tracker()
                if unmatched:
                    text = tw.append_log(text, tl.relaunch_line("00:40", "worker-2", "Check parser", "base moved"))
                row = self.row(text)
                self.assertEqual([(g.category, g.start, g.end, g.title) for g in row.segments],
                                 [("implement", at(TODAY, "00:10"), NOW, "worker-1")])

    def test_completion_and_human_review_still_advance_the_flow_stage(self):
        for status, stage in (("done", "pr"), ("human_review", "review")):
            with self.subTest(status=status):
                text = tw.append_log(self.tracker(),
                                     tl.ended_line("00:40", "worker-1", status, "check finished", "parser.py"))
                row = self.row(text)
                self.assertEqual([(g.category, g.start, g.end) for g in row.segments], [
                    ("implement", at(TODAY, "00:10"), at(TODAY, "00:40")),
                    (stage, at(TODAY, "00:40"), NOW)])

    def ready_tracker(self):
        # After reconciliation the task is open again, with its previous lane/stage and owner.
        return self.tracker().replace("| worker-1 | running 00:10 |", "| Robin | open |").replace(
            "| S |  |  | #701 |", "| S | build | implement | #701 |")

    def stopped_tracker(self):
        return tw.append_log(self.ready_tracker(),
                             tl.relaunch_line("00:40", "worker-1", "Check parser", "base moved"))

    def test_prose_mentioning_a_relaunch_mid_line_leaves_the_worker_running(self):
        quoted = tl.relaunch_line("00:40", "worker-1", "Check parser", "base moved")
        text = tw.append_log(self.tracker(), f"- 00:40 note: retry example {quoted} was discussed; work continues.")
        row = self.row(text)
        self.assertEqual([(g.category, g.start, g.end, g.title) for g in row.segments],
                         [("implement", at(TODAY, "00:10"), NOW, "worker-1")])

    def test_a_stop_keeps_the_workers_bar_title_and_releases_ownership_for_a_later_stage(self):
        text = tw.append_log(self.stopped_tracker(), tl.stage_line("01:00", "Check parser", "review"))
        row = self.row(text)
        self.assertEqual([(g.category, g.start, g.end, g.title) for g in row.segments], [
            ("implement", at(TODAY, "00:10"), at(TODAY, "00:40"), "worker-1"),
            ("review", at(TODAY, "01:00"), NOW, "Robin")])
        rendered = titles([("running", row)], "Check parser")
        self.assertTrue(rendered)
        for title in rendered:
            with self.subTest(title=title):
                if title.startswith("implement "):
                    self.assertIn("00:10", title)
                    self.assertIn("00:40", title)
                    self.assertIn(" · worker-1 · done", title)
                else:
                    self.assertIn(" · Robin · done", title)

    def test_relaunching_the_earlier_overlapping_worker_leaves_the_other_running(self):
        text = tw.append_log(self.tracker(), tl.started_line("00:20", "worker-2", "codex", "gpt-test",
                                                           "worktree `trees/parallel` (parallel)", "Check parser"))
        text = tw.append_log(text, tl.relaunch_line("00:40", "worker-1", "Check parser", "base moved"))
        row = self.row(text)
        self.assertEqual([(g.category, g.start, g.end, g.title) for g in row.segments], [
            ("implement", at(TODAY, "00:10"), at(TODAY, "00:40"), "worker-1"),
            ("implement", at(TODAY, "00:20"), NOW, "worker-2")])

    def test_a_relaunch_naming_another_task_leaves_the_workers_bar_running(self):
        text = tw.append_log(self.tracker(),
                             tl.relaunch_line("00:40", "worker-1", "Check formatter", "base moved"))
        row = self.row(text)
        self.assertEqual([(g.category, g.start, g.end, g.title) for g in row.segments],
                         [("implement", at(TODAY, "00:10"), NOW, "worker-1")])

    def test_the_same_worker_relaunched_twice_keeps_only_the_first_bar_boundary(self):
        text = tw.append_log(self.stopped_tracker(),
                             tl.relaunch_line("00:50", "worker-1", "Check parser", "base moved again"))
        row = self.row(text)
        self.assertEqual([(g.category, g.start, g.end, g.title) for g in row.segments],
                         [("implement", at(TODAY, "00:10"), at(TODAY, "00:40"), "worker-1")])

    def test_a_worker_name_reused_for_a_new_launch_can_be_stopped_again(self):
        text = tw.append_log(self.stopped_tracker(), tl.started_line("01:00", "worker-1", "codex", "gpt-test",
                                                                   "worktree `trees/retry` (retry)", "Check parser"))
        text = tw.append_log(text, tl.relaunch_line("01:20", "worker-1", "Check parser", "base moved again"))
        row = self.row(text)
        self.assertEqual([(g.category, g.start, g.end, g.title) for g in row.segments], [
            ("implement", at(TODAY, "00:10"), at(TODAY, "00:40"), "worker-1"),
            ("implement", at(TODAY, "01:00"), at(TODAY, "01:20"), "worker-1")])

    def test_a_relaunch_as_the_last_move_keeps_the_prior_stage_note(self):
        row = self.row(self.stopped_tracker())
        self.assertEqual(row.note, "implement")

    def test_an_open_task_without_a_stop_keeps_its_due_date_forecast(self):
        # Control: the same open row, lane and due-date estimate used by the relaunch regression.
        text = self.ready_tracker()
        end = NOW + 2 * H
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {},
                         {"Check parser": end}, NOW)
        self.assertEqual(len(built), 1)
        _, row = built[0]
        stages = ["implement", "pr", "review", "triage", "merge"]
        step = (end - NOW) / len(stages)
        self.assertEqual([(g.category, g.start, g.end) for g in row.segments if g.kind == "forecast"],
                         [(s, NOW + i * step, NOW + (i + 1) * step) for i, s in enumerate(stages)])
        self.assertEqual(row.note, f"implement · ~{end:%H:%M}")

    def test_a_relaunch_as_the_last_move_keeps_the_due_date_forecast(self):
        text = self.stopped_tracker()
        end = NOW + 2 * H
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {},
                         {"Check parser": end}, NOW)
        self.assertEqual(len(built), 1)
        _, row = built[0]
        stages = ["implement", "pr", "review", "triage", "merge"]
        step = (end - NOW) / len(stages)
        self.assertEqual([(g.category, g.start, g.end) for g in row.segments if g.kind == "forecast"],
                         [(s, NOW + i * step, NOW + (i + 1) * step) for i, s in enumerate(stages)])
        self.assertEqual(row.note, f"implement · ~{end:%H:%M}")
        self.assertEqual({g.title for g in row.segments if g.kind == "forecast"}, {"Robin"})
        self.assertEqual([(g.start, g.end, g.title) for g in row.segments if g.kind == "done"],
                         [(at(TODAY, "00:10"), at(TODAY, "00:40"), "worker-1")])

    def test_a_held_relaunched_rows_hold_bar_uses_the_prior_stage_category(self):
        text = self.stopped_tracker().replace("| Robin | open |", "| Robin | waiting |")
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {"Check parser": "implement"}, {}, NOW)
        self.assertEqual(len(built), 1)
        status, row = built[0]
        self.assertEqual(status, "needs input")
        self.assertEqual([(g.category, g.start, g.end, g.title) for g in row.segments if g.kind == "hold"],
                         [("implement", NOW, NOW + fc.WINDOWS[-1][2], "Robin")])
        self.assertEqual(row.note, "needs input · implement")
        self.assertNotIn("relaunch", [g.category for g in row.segments])

    def test_a_stage_log_named_relaunch_keeps_its_working_bar(self):
        # The word is a valid custom stage; only a launcher-built relaunch is a stop.
        lanes = {"build": st.Lane(("implement", "relaunch", "pr", "main"))}
        text = tw.append_log(self.ready_tracker(), tl.stage_line("00:40", "Check parser", "relaunch"))
        text = tw.append_log(text, tl.stage_line("01:00", "Check parser", "pr"))
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, lanes, {}, {}, NOW)
        self.assertEqual(len(built), 1)
        _, row = built[0]
        self.assertEqual([(g.category, g.start, g.end, g.kind) for g in row.segments], [
            ("implement", at(TODAY, "00:10"), at(TODAY, "00:40"), "done"),
            ("relaunch", at(TODAY, "00:40"), at(TODAY, "01:00"), "done"),
            ("pr", at(TODAY, "01:00"), NOW, "done")])


class RelaunchReviewPassTwoTest(unittest.TestCase):
    """#470: run boundaries, launch identity, and controls for the resolved successor rule."""

    def tracker(self):
        return flow_launch_tracker([("Check parser", "Check parser", "#701")],
                                   [("Check parser", "worker-1", "00:10")])

    def launch(self, text, worker, clock):
        return tw.append_log(text, tl.started_line(clock, worker, "codex", "gpt-test",
                                                  "worktree `trees/retry` (retry)", "Check parser"))

    def row(self, text, held=None):
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, held or {}, {}, NOW)
        self.assertEqual([r.name for _, r in built], ["Check parser"])
        return built[0][1]

    def worker_bars(self, row):
        return [(g.start, g.end, g.title) for g in row.segments if g.category == "implement" and g.kind == "done"]

    def test_a_held_row_with_an_open_worker_draws_only_the_hold_bar(self):
        text = self.tracker().replace("| worker-1 | running 00:10 |", "| Robin | waiting |")
        for held in (False, True):
            with self.subTest(held=held):
                row = self.row(text, {"Check parser": "Robin"} if held else {})
                self.assertEqual([(g.category, g.start, g.end, g.kind, g.title) for g in row.segments],
                                 [("implement", NOW, NOW + fc.WINDOWS[-1][2], "hold", "worker-1")] if held else
                                 [("implement", at(TODAY, "00:10"), NOW, "done", "worker-1")])

    def test_simultaneous_launch_bars_name_each_launching_worker_despite_the_current_owner(self):
        text = self.launch(self.tracker(), "worker-2", "00:10")
        text = text.replace("| worker-1 | running 00:10 |", "| Robin | open |")
        text = tw.append_log(text, tl.relaunch_line("00:40", "worker-1", "Check parser", "base moved"))
        text = tw.append_log(text, tl.relaunch_line("00:50", "worker-2", "Check parser", "base moved"))
        self.assertEqual(rb.parse_tracker(text).tasks[0].shown_owner, "Robin")
        self.assertEqual(self.worker_bars(self.row(text)), [
            (at(TODAY, "00:10"), at(TODAY, "00:40"), "worker-1"),
            (at(TODAY, "00:10"), at(TODAY, "00:50"), "worker-2")])

    def test_a_relaunch_closes_the_latest_of_two_open_runs_with_the_same_worker_name(self):
        text = self.launch(self.tracker(), "worker-1", "01:00")
        text = tw.append_log(text, tl.relaunch_line("01:20", "worker-1", "Check parser", "base moved"))
        bars = self.worker_bars(self.row(text))
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[1], (at(TODAY, "01:00"), at(TODAY, "01:20"), "worker-1"))
        self.assertNotEqual(bars[0][1], at(TODAY, "01:20"), "the latest stop must not close the first run")

    def test_an_open_launch_run_on_a_done_task_ends_at_its_done_clock(self):
        text = self.tracker().replace("| running 00:10 |", "| done 00:40 |")
        # Reconciliation records the terminal stage later than the actual task completion.
        # That later stage must not hide a fallback from the task's stop clock to now.
        text = tw.append_log(text, tl.stage_line("01:00", "Check parser", "main"))
        self.assertEqual(self.worker_bars(self.row(text)),
                         [(at(TODAY, "00:10"), at(TODAY, "00:40"), "worker-1")])

    def test_a_successor_closes_a_run_without_an_explicit_end_or_stop(self):
        for worker in ("worker-2", "worker-1"):
            with self.subTest(successor=worker):
                text = self.launch(self.tracker(), worker, "01:00")
                self.assertEqual(self.worker_bars(self.row(text)), [
                    (at(TODAY, "00:10"), at(TODAY, "01:00"), "worker-1"),
                    (at(TODAY, "01:00"), NOW, worker)])

    def test_a_later_explicit_end_preserves_overlap_with_a_same_task_successor(self):
        for status in ("done", "human_review"):
            with self.subTest(status=status):
                text = self.launch(self.tracker(), "worker-2", "01:00")
                text = tw.append_log(text, tl.ended_line("01:20", "worker-1", status, "finished", "parser.py"))
                self.assertEqual(self.worker_bars(self.row(text)), [
                    (at(TODAY, "00:10"), at(TODAY, "01:20"), "worker-1"),
                    (at(TODAY, "01:00"), NOW, "worker-2")])

    def test_launches_on_different_tasks_can_overlap_without_explicit_ends(self):
        text = flow_launch_tracker([("Check parser", "Check parser", "#701"),
                                    ("Check formatter", "Check formatter", "#702")],
                                   [("Check parser", "worker-1", "00:10"),
                                    ("Check formatter", "worker-2", "01:00")])
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        self.assertEqual({r.name: self.worker_bars(r) for _, r in built}, {
            "Check parser": [(at(TODAY, "00:10"), NOW, "worker-1")],
            "Check formatter": [(at(TODAY, "01:00"), NOW, "worker-2")]})


class FullIssueMergeKeyTest(unittest.TestCase):
    """#459 control: a shared display number must not combine different projects' issue histories."""

    def test_forge_issue_url_passes_merge_by_full_key_not_number(self):
        first = "https://forge.example/group/project/-/issues/7"
        other = "https://forge.example/group/other/-/issues/7"
        text = flow_launch_tracker([
            ("Build parser", "Implement parser", first),
            ("Review parser", "Check parser diagnostics", first),
            ("Build formatter", "Implement formatter", other),
        ], [
            ("Implement parser", "worker-1", "00:10"),
            ("Check parser diagnostics", "worker-2", "00:40"),
            ("Implement formatter", "worker-3", "00:50"),
        ])
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        self.assertEqual(len(built), 2)
        self.assertEqual([(r.ref, r.name) for _, r in built], [
            ("gitlab:forge.example/group/project#7", "Build parser"),
            ("gitlab:forge.example/group/other#7", "Build formatter"),
        ])
        self.assertEqual([[(g.start, g.end, g.title) for g in r.segments] for _, r in built], [
            [(at(TODAY, "00:10"), NOW, "worker-1"), (at(TODAY, "00:40"), NOW, "worker-2")],
            [(at(TODAY, "00:50"), NOW, "worker-3")],
        ])


class GrownLaunchRowTest(unittest.TestCase):
    """#455 sits beside #182: growing an item must keep the launch on its original Flow row."""

    def test_a_grown_items_launch_draws_on_its_own_row_not_the_latest_issue_row(self):
        prompt = "Build the parser for #7 and reject empty input with a helpful message."
        text = flow_launch_tracker([
            ("Build parser", prompt, "#7"),
            ("Verify parser", "Check the parser diagnostics in the example client", "#7")], [(prompt, "w1", "00:20")])
        # The append happens AFTER record_launch writes the original item into the Log.
        text = text.replace(f"| Build parser | {prompt} |",
                            f"| Build parser | {prompt} Reviewed 15:09 by w2: checks pass. |")
        got = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        self.assertEqual(len(got), 1)
        status, row = got[0]
        self.assertEqual((status, row.ref, row.name), ("running", "#7", "Build parser"))
        self.assertEqual([(g.start, g.end, g.category, g.title) for g in row.segments],
                         [(at(TODAY, "00:20"), NOW, "implement", "w1")])


class WorkflowFlowRowTest(unittest.TestCase):
    """#456: the workflow token never joins unrelated changes; a real change still joins its passes."""

    def test_merge_issues_does_not_group_the_literal_workflow_token(self):
        review = gantt.Segment(at(TODAY, "00:20"), at(TODAY, "00:40"), "review", "done", "w1")
        verify = gantt.Segment(at(TODAY, "00:50"), NOW, "implement", "done", "w2")
        entries = [("running", gantt.Row("workflow", "Review change 50", "review", (review,))),
                   ("running", gantt.Row("workflow", "Verify change 51", "implement", (verify,)))]
        got = fc._merge_issues(entries)
        self.assertEqual(len(got), 2, "workflow is a token, never a merge key")
        self.assertEqual([(r.name, r.segments) for _, r in got],
                         [("Review change 50", (review,)), ("Verify change 51", (verify,))])

    def test_workflow_rows_for_different_changes_stay_separate_in_build(self):
        other_url = "https://forge.example/group/project/-/merge_requests/51"
        review = f"Review {CHANGE_URL} and check the parser diagnostics."
        verify = f"Verify {other_url} and check the format examples."
        text = flow_launch_tracker([
            ("Review change 50", review, "workflow"), ("Verify change 51", verify, "workflow")],
            [(review, "w1", "00:20"), (verify, "w2", "00:50")])
        got = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        self.assertEqual(len(got), 2, "unrelated changes share only the workflow token")
        self.assertEqual([(r.name, [(g.start, g.end, g.title) for g in r.segments]) for _, r in got], [
            ("Review change 50", [(at(TODAY, "00:20"), NOW, "w1")]),
            ("Verify change 51", [(at(TODAY, "00:50"), NOW, "w2")])])
        for _, row in got:
            self.assertNotEqual(row.ref, "workflow")

    def test_build_review_and_verify_of_one_change_merge_under_the_build_name_in_time_order(self):
        build = f"Build the parser in {CHANGE_URL}; closes #7."
        review = f"Review {CHANGE_URL}; closes #7; check empty input handling."
        verify = f"Verify {CHANGE_URL}; issue #7; check the client diagnostics."
        # Tracker order differs from launch order: the build's earliest segment supplies the name.
        text = flow_launch_tracker([
            ("Verify parser", verify, "workflow"), ("Review parser", review, "workflow"),
            ("Build parser", build, ISSUE_URL)],
            [(build, "w1", "00:10"), (review, "w2", "00:40"), (verify, "w3", "01:10")])
        got = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        self.assertEqual(len(got), 1, "the real issue must join its build, review and verify passes")
        status, row = got[0]
        self.assertEqual((status, row.name), ("running", "Build parser"))
        self.assertTrue(row.ref)
        self.assertNotEqual(row.ref, "workflow")
        self.assertEqual([(g.start, g.end, g.category, g.title) for g in row.segments], [
            (at(TODAY, "00:10"), NOW, "implement", "w1"),
            (at(TODAY, "00:40"), NOW, "implement", "w2"),
            (at(TODAY, "01:10"), NOW, "implement", "w3")])

    def test_unmatched_dispatches_naming_no_change_keep_separate_short_names_and_no_ref(self):
        # Control: deleting the Tasks rows leaves the writer's authentic launch lines intact.
        prompts = ["Inspect the local workflow helpers and document the accepted input shapes for the next maintainer.",
                   "Compare the local format examples and describe how the decoder treats empty fields in each sample."]
        text = flow_launch_tracker([
            ("Inspect helpers", prompts[0], "workflow"), ("Compare examples", prompts[1], "workflow")],
            [(prompts[0], "w1", "00:20"), (prompts[1], "w2", "00:50")])
        text = "".join(line for line in text.splitlines(keepends=True)
                       if not line.startswith(("| Inspect helpers |", "| Compare examples |")))
        got = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        self.assertEqual(len(got), 2)
        self.assertEqual([(r.ref, r.name) for _, r in got], [("", prompt.split("\n", 1)[0][:80]) for prompt in prompts])
        self.assertEqual([[(g.start, g.end, g.title) for g in r.segments] for _, r in got], [
            [(at(TODAY, "00:20"), NOW, "w1")], [(at(TODAY, "00:50"), NOW, "w2")]])


class ChangeBridgeReviewTest(unittest.TestCase):
    """R1: a change-only pass needs a unique bridge, including evidence retained in the Log."""

    def build(self, text):
        return fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)

    def test_a_change_only_pass_joins_the_issue_through_another_rows_bridge(self):
        # Both bridge sources matter: explicit issue cell, and a workflow item's own closes clause.
        for bridge_issue, suffix in ((ISSUE_URL, ""), ("workflow", "; closes #7")):
            with self.subTest(bridge=bridge_issue):
                build = f"Build the parser for {ISSUE_URL}"
                bridge = f"Verify {CHANGE_URL}{suffix}"
                review = f"Review {CHANGE_URL} and check empty input handling."
                text = flow_launch_tracker([
                    ("Build parser", build, ISSUE_URL), ("Verify parser", bridge, bridge_issue),
                    ("Review parser", review, "workflow")],
                    [(build, "w1", "00:10"), (bridge, "w2", "00:40"), (review, "w3", "01:10")])
                got = self.build(text)
                self.assertEqual(len(got), 1, "a change URL alone must use the unique issue bridge")
                status, row = got[0]
                self.assertEqual((status, row.name), ("running", "Build parser"))
                self.assertEqual(row.ref, "gitlab:forge.example/group/project#7")
                self.assertEqual([(g.start, g.end, g.title) for g in row.segments], [
                    (at(TODAY, "00:10"), NOW, "w1"), (at(TODAY, "00:40"), NOW, "w2"),
                    (at(TODAY, "01:10"), NOW, "w3")])

    def test_conflicting_bridges_leave_the_change_only_pass_unkeyed_in_either_order(self):
        review = f"Review {CHANGE_URL} and check empty input handling."
        for source in ("closes clause", "issue cell"):
            for numbers in ((7, 8), (8, 7)):
                with self.subTest(source=source, order=numbers):
                    tasks = []
                    for number in numbers:
                        item = f"Verify {CHANGE_URL} for parser case {number}"
                        issue = "workflow"
                        if source == "closes clause":
                            item += f"; closes #{number}"
                        else:
                            issue = f"https://forge.example/group/project/-/issues/{number}"
                        tasks.append((f"Verify case {number}", item, issue))
                    text = flow_launch_tracker([*tasks, ("Review parser", review, "workflow")],
                                              [(review, "w3", "01:10")])
                    got = self.build(text)
                    self.assertEqual(len(got), 1)
                    self.assertEqual(got[0][1].ref, "", "conflicting evidence must not choose the latest issue")
                    self.assertEqual(got[0][1].name, "Review parser")

    def test_a_stale_launch_bridge_conflicting_with_the_item_leaves_a_change_only_pass_unkeyed(self):
        original = f"Verify {CHANGE_URL}; closes #7"
        current = f"Verify {CHANGE_URL}; closes #8"
        review = f"Review {CHANGE_URL} and check empty input handling."
        text = flow_launch_tracker([
            ("Verify parser", original, "workflow"), ("Review parser", review, "workflow")],
            [(original, "w1", "00:10"), (review, "w2", "00:40")])
        # The old bridge stays in the launch Log; only the item cell is corrected.
        text = text.replace(f"| Verify parser | {original} |", f"| Verify parser | {current} |")
        got = self.build(text)
        review_rows = [r for _, r in got if any(g.title == "w2" for g in r.segments)]
        self.assertEqual(len(review_rows), 1)
        self.assertEqual(review_rows[0].ref, "", "the stale Log must not win over contradictory item evidence")
        self.assertEqual(review_rows[0].name, "Review parser")


class HomeFlowReviewTest(unittest.TestCase):
    """R2: Flow uses the same sources.home that the board's other issue-key callers use."""

    HOME = Backlog("https://forge.example", "group/project")

    def tracker(self):
        build = f"Build the parser in {CHANGE_URL}; closes #7."
        review = f"Review {CHANGE_URL}; closes #7; check empty input handling."
        return flow_launch_tracker([
            ("Build parser", build, "#7"), ("Review parser", review, "workflow")],
            [(build, "w1", "00:10"), (review, "w2", "00:40")])

    def test_build_accepts_home_by_keyword_and_merges_a_bare_issue_with_its_review(self):
        text = self.tracker()
        got = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks,
                       LANES, {}, {}, NOW, home=self.HOME)
        self.assertEqual(len(got), 1, "home #7 and the home project's closes #7 are the same issue")
        status, row = got[0]
        self.assertEqual((status, row.ref, row.name), ("running", "#7", "Build parser"))
        self.assertEqual([(g.start, g.end, g.title) for g in row.segments], [
            (at(TODAY, "00:10"), NOW, "w1"), (at(TODAY, "00:40"), NOW, "w2")])

    def test_render_board_passes_sources_home_to_build_by_keyword(self):
        sources = bs.Sources({}, {}, {}, (), {}, home=self.HOME)
        # Deliberately different: config.backlog must not replace the source cache's home.
        cfg = replace(BoardTest().cfg(), backlog=Backlog("https://forge.example", "other/project"))
        with mock.patch.object(rb.flow_chart, "build", wraps=fc.build) as build:
            rb.render(self.tracker(), cfg, NOW, lanes=LANES, tracker_day=TODAY, sources=sources)
        self.assertEqual(build.call_count, 1)
        self.assertIs(build.call_args.kwargs.get("home"), sources.home)

    def test_build_without_home_preserves_the_existing_bare_issue_key(self):
        text = self.tracker()
        got = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        self.assertEqual([(r.name, r.ref) for _, r in got], [
            ("Build parser", "#7"), ("Review parser", "gitlab:forge.example/group/project#7")])


class WorkflowKeyReviewTest(unittest.TestCase):
    """R3: context references cannot steal a workflow row or stop another task's live bar."""

    OTHER_CHANGE = "https://forge.example/group/project/-/merge_requests/51"

    def build(self, text):
        return fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)

    def context_tracker(self, context, done=False):
        build = f"Build the parser for {ISSUE_URL}"
        review = f"Review {self.OTHER_CHANGE} and check diagnostics; context: {context}"
        text = flow_launch_tracker([
            ("Build parser", build, ISSUE_URL), ("Review formatter", review, "workflow")],
            [(build, "w1", "00:10"), (review, "w2", "00:40")])
        if done:
            text = text.replace(f"| Review formatter | {review} | w2 | running 00:40 |",
                                f"| Review formatter | {review} | w2 | done 00:50 |")
        return text

    def test_a_later_context_issue_url_does_not_join_another_tasks_row(self):
        got = self.build(self.context_tracker(ISSUE_URL))
        self.assertEqual(len(got), 2, "a context issue is not this change's closing issue")
        self.assertEqual([(r.name, r.ref) for _, r in got], [
            ("Build parser", "gitlab:forge.example/group/project#7"),
            ("Review formatter", self.OTHER_CHANGE)])
        self.assertEqual([[(g.start, g.end, g.title) for g in r.segments] for _, r in got], [
            [(at(TODAY, "00:10"), NOW, "w1")], [(at(TODAY, "00:40"), NOW, "w2")]])

    def test_a_done_context_review_never_clips_another_tasks_running_segment(self):
        got = self.build(self.context_tracker(ISSUE_URL, done=True))
        live = [g for _, r in got for g in r.segments if g.title == "w1"]
        self.assertEqual([(g.start, g.end) for g in live], [(at(TODAY, "00:10"), NOW)],
                         "a different task's done 00:50 must not clip the parser's still-running bar")
        self.assertEqual(len(got), 2)
        own = {r.name: (s, r) for s, r in got}
        self.assertEqual(own["Build parser"][0], "running")
        self.assertEqual(own["Review formatter"][0], "done")
        self.assertEqual([(g.start, g.end, g.title) for g in own["Review formatter"][1].segments],
                         [(at(TODAY, "00:40"), at(TODAY, "00:50"), "w2")])

    def test_a_workflow_rows_own_closing_issue_wins_over_another_tasks_context_issue(self):
        other_issue = "https://forge.example/group/project/-/issues/8"
        parser = f"Build the parser for {ISSUE_URL}"
        formatter = f"Build the formatter for {other_issue}"
        review = f"Review {self.OTHER_CHANGE}; closes #8; context: {ISSUE_URL}"
        text = flow_launch_tracker([
            ("Build parser", parser, ISSUE_URL), ("Build formatter", formatter, other_issue),
            ("Review formatter", review, "workflow")],
            [(parser, "w1", "00:10"), (formatter, "w2", "00:20"), (review, "w3", "00:40")])
        got = self.build(text)
        self.assertEqual(len(got), 2)
        self.assertEqual({r.ref: [(g.start, g.end, g.title) for g in r.segments] for _, r in got}, {
            "gitlab:forge.example/group/project#7": [(at(TODAY, "00:10"), NOW, "w1")],
            "gitlab:forge.example/group/project#8": [(at(TODAY, "00:20"), NOW, "w2"),
                                               (at(TODAY, "00:40"), NOW, "w3")]})

    def test_a_later_context_change_url_leaves_the_workflow_row_unkeyed(self):
        build = f"Build the parser in {CHANGE_URL}; closes #7"
        review = f"Review {self.OTHER_CHANGE} and check diagnostics; context: {CHANGE_URL}"
        for done in (False, True):
            with self.subTest(done=done):
                text = flow_launch_tracker([
                    ("Build parser", build, ISSUE_URL), ("Review formatter", review, "workflow")],
                    [(build, "w1", "00:10"), (review, "w2", "00:40")])
                if done:
                    text = text.replace(f"| Review formatter | {review} | w2 | running 00:40 |",
                                        f"| Review formatter | {review} | w2 | done 00:50 |")
                got = self.build(text)
                self.assertEqual(len(got), 2)
                own = {r.name: r for _, r in got}
                self.assertEqual(own["Review formatter"].ref, "", "two distinct changes are ambiguous")
                self.assertEqual(own["Build parser"].segments[-1].end, NOW)

    def test_a_workflow_item_with_two_distinct_closing_issues_keeps_its_own_unkeyed_row(self):
        build = f"Build the parser for {ISSUE_URL}"
        review = f"Review {CHANGE_URL}; closes #7; fixes #8"
        text = flow_launch_tracker([
            ("Build parser", build, ISSUE_URL), ("Review ambiguous change", review, "workflow")],
            [(build, "w1", "00:10"), (review, "w2", "00:40")])
        got = self.build(text)
        self.assertEqual(len(got), 2)
        own = {r.name: r for _, r in got}
        self.assertEqual(own["Review ambiguous change"].ref, "")
        self.assertEqual([(g.start, g.end, g.title) for g in own["Review ambiguous change"].segments],
                         [(at(TODAY, "00:40"), NOW, "w2")])

    def test_the_same_issue_number_in_different_change_projects_stays_separate(self):
        other = "https://forge.example/other/project/-/merge_requests/50"
        prompts = [f"Review {url}; closes #7" for url in (CHANGE_URL, other)]
        text = flow_launch_tracker([
            ("Review parser", prompts[0], "workflow"), ("Review formatter", prompts[1], "workflow")],
            [(prompts[0], "w1", "00:10"), (prompts[1], "w2", "00:40")])
        got = self.build(text)
        self.assertEqual([(r.name, r.ref) for _, r in got], [
            ("Review parser", "gitlab:forge.example/group/project#7"),
            ("Review formatter", "gitlab:forge.example/other/project#7")])


# #204: a launch names its task by the text before ":"; that name outranks a later row that merely shares,
# or also carries, the same issue ref.
RENAMED_ROW_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Fix !182 review findings | Patch the null check flagged in review | w1 | running 00:20 | 2026-09-26 |  | S | build | triage | !182 | c |
| Verify !182 third pass | Verify !182 third pass | w2 | running 00:40 | 2026-09-26 |  | S | build | triage | !182 | c |
"""
THIRD_ROW_SHARES_REF_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Review !182 first pass | Review 182 | w1 | running 00:10 | 2026-09-26 |  | S | build | triage | !182 | c |
| Verify !182 third pass | Confirm the deploy is clean | w2 | running 00:40 | 2026-09-26 |  | S | build | triage | !182 | c |
| Ship !182 release notes | Ship !182 release notes | w3 | running 01:00 | 2026-09-26 |  | S | build | triage | !182 | c |
"""


class LaunchNamesItsRowTest(unittest.TestCase):
    """A launch lands on the row its text names, even once that row's item cell has changed and a later row
    shares, or mentions, the same issue ref (#204)."""

    def test_a_launch_lands_on_its_renamed_row_not_a_later_row_sharing_the_ref(self):
        tasks = rb.parse_tracker(RENAMED_ROW_TRACKER).tasks
        text = "Fix !182 review findings: patch the null check the second reviewer flagged"
        task, name = fc.launch_row(text, tasks)
        self.assertEqual(name, "Fix !182 review findings")
        self.assertEqual(task.item, "Patch the null check flagged in review")

    def test_a_launch_lands_on_its_named_row_not_a_third_row_that_mentions_the_ref(self):
        tasks = rb.parse_tracker(THIRD_ROW_SHARES_REF_TRACKER).tasks
        text = "Verify !182 third pass: confirm the rollback plan is documented"
        task, name = fc.launch_row(text, tasks)
        self.assertEqual(name, "Verify !182 third pass")
        self.assertEqual(task.item, "Confirm the deploy is clean")


WORKFLOW_ROWS_TRACKER = """# Tracker

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Fix the parser | Patch the parser | w1 | running 00:10 | 2026-09-26 |  | S | build | triage | #8 | c |
| Review MR 14 | Look over the change | w2 | running 00:20 | 2026-09-26 |  | S | build | review | workflow | c |
| Review MR 15 | Look over the other change | w3 | running 00:30 | 2026-09-26 |  | S | build | review | workflow | c |
"""


class LaunchSkipsWorkflowRowsTest(unittest.TestCase):
    """#366 review R7: `workflow` in an issue cell is a token, not a search pattern: a launch text that holds the word
    binds to no workflow row, however late that row is in the tracker."""

    def test_a_launch_that_mentions_the_word_workflow_binds_to_no_workflow_row(self):
        tasks = rb.parse_tracker(WORKFLOW_ROWS_TRACKER).tasks
        for text in ("Repair the CI workflow file", "Update workflows.md", "Document the release workflow"):
            with self.subTest(text=text):
                task, name = fc.launch_row(text, tasks)
                self.assertIsNone(task)
                self.assertEqual(name, text)

    def test_a_launch_naming_a_real_issue_still_binds_to_its_row_not_a_later_workflow_row(self):
        tasks = rb.parse_tracker(WORKFLOW_ROWS_TRACKER).tasks
        for text in ("Fix #8 in the workflow config", "Fix #8: patch the parser"):
            with self.subTest(text=text):
                task, _ = fc.launch_row(text, tasks)
                self.assertEqual(task.issue, "#8")


class ManyLaunchesTest(unittest.TestCase):
    """A board over a tracker with hundreds of launches maps each to its row within seconds (#184)."""

    N = 400

    @classmethod
    def setUpClass(cls):
        text, cls.expected = many_launched_tracker(cls.N)
        log, tasks = fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks
        start = time.perf_counter()
        cls.got = fc.build(log, tasks, LANES, {}, {}, NOW)
        cls.took = time.perf_counter() - start

    def test_the_chart_builds_within_seconds(self):
        self.assertLess(self.took, 5, f"{self.N} launches took {self.took:.1f}s")

    def test_every_launch_lands_on_its_own_row(self):
        # Half map by item prefix, half only by issue ref; each draws on its own row with that row's ref and name.
        got = {row.name: (row.ref, [g.start for g in row.segments]) for _, row in self.got}
        self.assertEqual(got, {name: (ref, [at(TODAY, hhmm)]) for name, (ref, hhmm, _) in self.expected.items()})


def titles(built: list[tuple[str, gantt.Row]], name: str) -> list[str]:
    """The rendered bar titles on the Flow rows named `name`, across both charts."""
    html = fc.section(built, NOW)
    return [t for row in re.findall(r'<div class="gantt-row">.*?</div>', html) if f'<span class="gantt-name">{name}</span>' in row
            for t in re.findall(r' title="([^"]*)"', row)]


class OwnerTest(unittest.TestCase):
    """Each bar names who held its stage (#186)."""

    def test_a_bar_after_a_started_line_names_its_worker(self):
        # "A bar that follows a one-shot started line carries that worker in its title, e.g.
        # `implement 20:05–21:35 · W · done`."
        text = OneShotTest().tracker()  # w1 launched Audit headers at 00:20; its owner cell is now Robin
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        implement = [t for t in titles(built, "Audit headers") if t.startswith("implement ")]
        self.assertTrue(implement)
        for title in implement:
            self.assertIn(" · w1 · ", title)
        # A launch that maps to no tracker row has no owner cell; its worker still names the bar.
        text = launched_tracker()
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        sweep = next(r for _, r in built if any(g.start == at(TODAY, "01:00") for g in r.segments))
        got = titles(built, sweep.name)
        self.assertTrue(got)
        for title in got:
            self.assertIn(" · impl-3 · ", title)

    def test_a_bar_with_no_running_launch_names_the_owner_cell(self):
        # "A bar with no running launch carries the task's tracker owner cell in its title"
        for name, owner in (("Upload size limit", "impl-1"), ("Cache warmup", "Robin")):
            with self.subTest(task=name):
                done = [t for t in titles(list(rows().values()), name) if t.endswith(" · done")]
                self.assertTrue(done)
                for title in done:
                    self.assertIn(f" · {owner} · ", title)
        # w1 completed Audit headers at 01:30; the pr bar after it has no running launch.
        text = OneShotTest().tracker()
        built = fc.build(fc.moves(text, TODAY, CT), rb.parse_tracker(text).tasks, LANES, {}, {}, NOW)
        pr = [t for t in titles(built, "Audit headers") if t.startswith("pr ")]
        self.assertTrue(pr)
        for title in pr:
            self.assertIn(" · Robin · ", title)


def bar_titles(row: gantt.Row, kind: str) -> set[str]:
    """The titles of `row`'s bars of `kind`; fails when it has none."""
    got = {g.title for g in row.segments if g.kind == kind}
    assert got, f"{row.name} has no {kind} bar"
    return got


UPLOAD_LAUNCH = "- 01:30 one-shot w7 started: claude, worktree `trees/up`, task Cap uploads"


class ForecastOwnerTest(unittest.TestCase):
    """Forecast and hold bars name who holds the task, as done bars do (#192)."""

    def test_a_forecast_bar_names_the_worker_running_now(self):
        # "A forecast bar's title names the worker whose launch is running now, else the task's owner cell"
        tracker = tw.append_log(TODAY_TRACKER, UPLOAD_LAUNCH)  # Upload size limit's owner cell is impl-1
        _, row = rows({"Cap uploads": NOW + 4 * H}, tracker=tracker)["Upload size limit"]
        self.assertEqual(bar_titles(row, "forecast"), {"w7"})

    def test_a_forecast_bar_with_no_launch_running_now_names_the_owner_cell(self):
        ended = tw.append_log(tw.append_log(TODAY_TRACKER, UPLOAD_LAUNCH), "- 01:50 one-shot w7: completed; awaiting integration")
        for label, tracker in (("no launch", TODAY_TRACKER), ("a launch that ended", ended)):
            with self.subTest(label):
                _, row = rows({"Cap uploads": NOW + 4 * H}, tracker=tracker)["Upload size limit"]
                self.assertEqual(bar_titles(row, "forecast"), {"impl-1"})

    def test_a_hold_bar_names_the_same_owner(self):
        # "A hold bar's title names the same owner"
        self.assertEqual(bar_titles(rows()["Cache warmup"][1], "hold"), {"Robin"})
        tracker = tw.append_log(TODAY_TRACKER, "- 01:45 one-shot w8 started: claude, worktree `trees/warm`, task Warm the pool")
        self.assertEqual(bar_titles(rows(tracker=tracker)["Cache warmup"][1], "hold"), {"w8"})

    def test_a_queued_tasks_forecast_names_its_owner_cell(self):
        tracker = QUEUED.replace("| Export format | Export CSV | unassigned |", "| Export format | Export CSV | Robin |")
        got = rows({"Cap uploads": NOW + 4 * H}, {"Export CSV": 5 * H}, 1, tracker)
        self.assertEqual(bar_titles(got["Export format"][1], "forecast"), {"Robin"})

    def test_a_task_with_no_owner_has_no_owner_part(self):
        # "A task with no owner keeps bar titles with no owner part." The same rows with an owner cell name it.
        for owner in ("Robin", ""):
            tracker = (QUEUED.replace("| Export format | Export CSV | unassigned |", f"| Export format | Export CSV | {owner} |")
                       .replace("| Cap uploads | impl-1 |", f"| Cap uploads | {owner} |")
                       .replace("| Warm the pool | Robin |", f"| Warm the pool | {owner} |"))
            got = rows({"Cap uploads": NOW + 4 * H}, {"Export CSV": 5 * H}, 1, tracker)
            for name, kind in (("Upload size limit", "forecast"), ("Cache warmup", "hold"), ("Export format", "forecast")):
                with self.subTest(owner=owner, task=name):
                    self.assertEqual(bar_titles(got[name][1], kind), {owner})


class UnassignedOwnerTest(unittest.TestCase):
    """An owner cell of `unassigned`, in any case, is the tracker's word for no owner (#193)."""

    def built(self, owner: str) -> dict[str, tuple[str, gantt.Row]]:
        """TODAY_TRACKER's rows with Cache warmup's owner cell set to `owner`, whitespace kept."""
        tasks = [replace(t, owner=owner) if t.name == "Cache warmup" else t for t in rb.parse_tracker(TODAY_TRACKER).tasks]
        return {row.name: (status, row) for status, row in fc.build(moves(), tasks, LANES, {"Cache warmup": "Robin"}, {}, NOW)}

    def test_an_unassigned_owner_cell_draws_no_owner_part(self):
        # "A Flow bar of a task whose owner cell is `unassigned` (any case) has no owner part in its title."
        # "Any other owner cell still shows as written."
        for owner, want in (("unassigned", ""), ("Unassigned", ""), (" UNASSIGNED ", ""), ("unassigned-bot", "unassigned-bot")):
            with self.subTest(owner=owner):
                _, row = self.built(owner)["Cache warmup"]
                self.assertEqual(bar_titles(row, "done"), {want})
                self.assertEqual(bar_titles(row, "hold"), {want})
                queued = QUEUED.replace("| Export format | Export CSV | unassigned |", f"| Export format | Export CSV | {owner.strip()} |")
                got = rows({"Cap uploads": NOW + 4 * H}, {"Export CSV": 5 * H}, 1, queued)
                self.assertEqual(bar_titles(got["Export format"][1], "forecast"), {want})


def merged(issue: str, hhmm: str, n: int = 1) -> bs.Change:
    """A change the forge reports merged at `hhmm` today, closing `issue`, stamped in UTC as the forge cache holds it."""
    return bs.Change(f"!{n}", "u", "t", "merged", False, "passed", True, 1, at(TODAY, hhmm).astimezone(timezone.utc),
                     "main", (), (issue,))


def board(tracker: str, changes: list[bs.Change], stage_log: tuple = ((YESTERDAY, YESTERDAY_TRACKER),)) -> tuple[str, dict[str, tuple[str, gantt.Row]]]:
    """The rendered board, and the Flow rows it drew by name (as render handed them to flow_chart.section)."""
    seen, real = [], fc.section
    with mock.patch.object(rb.flow_chart, "section", lambda rows, now: seen.append(rows) or real(rows, now)):
        html = rb.render(tracker, BoardTest().cfg(), NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN,
                         stage_log=list(stage_log), sources=bs.Sources({}, {}, {c.ref: c for c in changes}, (), {},
                                                                      bs.backlog.Backlog('https://forge.example', 'grp/app')))
    return html, {row.name: (status, row) for status, row in seen[0]}


class ForgeMergeTest(unittest.TestCase):
    """A row ends at the forge's merge time for its task's change (#186)."""

    def test_a_forge_merge_ends_the_row_with_no_stage_line_to_main(self):
        # "A task whose change the forge reports merged notes `merged HH:MM` at that time, with no bar past it and no
        # forecast or hold."
        html, got = board(TODAY_TRACKER, [merged("#109", "01:15")])
        status, row = got["Upload size limit"]
        self.assertEqual((status, row.note), ("merged", "merged 01:15"))
        self.assertIn('<span class="gantt-name">Upload size limit</span>', html)
        self.assertIn('<span class="gantt-note">merged 01:15</span>', html)
        self.assertTrue(row.segments)
        for g in row.segments:
            self.assertLessEqual(g.end, at(TODAY, "01:15"), g)
            self.assertNotIn(g.kind, ("forecast", "hold"), g)

    def test_a_held_task_whose_change_merged_is_merged(self):
        # "A held task whose change merged shows as merged, not held."
        html, got = board(TODAY_TRACKER, [merged("#111", "01:50")])
        status, row = got["Cache warmup"]
        self.assertEqual((status, row.note), ("merged", "merged 01:50"))
        self.assertNotIn("hold", [g.kind for g in row.segments])
        self.assertIn('<span class="gantt-note">merged 01:50</span>', html)

    def test_the_forge_merge_time_beats_the_stage_line(self):
        # "When a stage line to main and the forge disagree on the merge time, the forge time wins."
        tracker = tw.append_log(TODAY_TRACKER, "- 01:30 stage: Upload size limit → main")
        for forge in ("01:15", "01:45"):
            with self.subTest(forge=forge):
                html, got = board(tracker, [merged("#109", forge)])
                self.assertEqual(got["Upload size limit"][1].note, f"merged {forge}")
                self.assertNotIn('<span class="gantt-note">merged 01:30</span>', html)


class ManyMergedTest(unittest.TestCase):
    """A board over hundreds of launched tasks, each with a merged change, renders within seconds (#186)."""

    N = 400

    @classmethod
    def setUpClass(cls):
        text, cls.expected = many_launched_tracker(cls.N)
        changes = [merged(ref, "02:05", i) for i, (ref, _, _) in enumerate(cls.expected.values())]
        start = time.perf_counter()
        cls.html, cls.got = board(text, changes, ())
        cls.took = time.perf_counter() - start

    def test_the_board_renders_within_seconds(self):
        # "A build over 400 tasks, each with launches and a merged change, finishes within 5 seconds."
        self.assertLess(self.took, 5, f"{self.N} merged launches took {self.took:.1f}s")

    def test_every_row_ends_at_its_merge(self):
        # The timing covers the merge join: "A task whose change the forge reports merged notes `merged HH:MM`".
        self.assertEqual({name: row.note for name, (_, row) in self.got.items()},
                         {name: "merged 02:05" for name in self.expected})


if __name__ == "__main__":
    unittest.main()
