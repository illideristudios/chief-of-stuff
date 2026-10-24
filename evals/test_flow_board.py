"""The Flow board's top: render_board maps the tracker and the board sources onto panels.py and columns.py."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "scripts"), str(HERE)]
import backlog_ref as backlog  # noqa: E402
import board_sources as bs  # noqa: E402
import columns  # noqa: E402
import decision_page  # noqa: E402
import panels  # noqa: E402
import render_board as rb  # noqa: E402
import task_forge  # noqa: E402
import tracker_log  # noqa: E402
from tracker import parse_tracker  # noqa: E402
from workspace import parse_coordinator  # noqa: E402
import settings as st  # noqa: E402
from test_render_board import CLAUDE_MD, NOW  # noqa: E402


def change(ref: str, *, state="open", draft=False, pipeline="passed", approved=False, merged_at=None,
           files=(), issues=(), title="") -> bs.Change:
    return bs.Change(ref, f"https://forge/{ref[1:]}", title or f"Change {ref}", state, draft, pipeline, approved,
                     int(approved), merged_at, "main", files, issues)


SOURCES = bs.Sources(
    fetched={"kanban": NOW - timedelta(minutes=2), "merge requests": NOW - timedelta(minutes=3), "workers": NOW},
    issues={"#9": bs.Issue("#9", "https://forge/i/9", "Cut", "open", ("stage::review",)),
            "#7": bs.Issue("#7", "https://forge/i/7", "README", "open", ("stage::implement",))},
    changes={c.ref: c for c in (
        change("!41", approved=True, files=("a.py",), issues=("#2",), title="Password reset copy"),
        change("!42", approved=True, files=("b.py", "c.py"), issues=("#3",), title="Health check timeout"),
        change("!48", files=("c.py",), issues=("#9",), title="Upload size limit"),
        change("!50", draft=True, files=("c.py",), issues=("#5",)),
        change("!52", approved=True, pipeline="failed", files=("d.py",), issues=("#6",), title="Login throttle"),
        change("!37", state="merged", merged_at=NOW - timedelta(hours=2), issues=("#8",), title="Locale fallback"),
        change("!36", state="merged", merged_at=NOW - timedelta(days=1), issues=("#1",)),
    )},
    workers=(bs.Worker("impl-07", "one-shot", "claude", "opus", "Cut the release", "trees/cut", NOW - timedelta(minutes=42), None),
             bs.Worker("impl-04", "session", "codex", "", "Audit", "", NOW - timedelta(hours=1, minutes=23), NOW - timedelta(minutes=11))),
    errors={},
)

TRACKER = """# Tracker 2026-09-16

Coordinator: coordinator. Board: board-7.

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Cut the release | Cut the release branch | impl-2 | running 10:30 | 10:30 | 23:00 | M | build | review | #9 | Checklist: cut |
| Write README | Write eval README | Robin | open | 09:00 | 17:00 | S | build | triage | https://forge/i/7 | Checklist: readme |
| Webhook check | Check webhook signature | unassigned | orphaned | 09:00 |  | S | build | implement |  | Checklist: hook |
| Console | Rotate the key in the console | Robin | open | 09:00 | 17:00 | S |  |  |  | Checklist: key |

## Decisions

| time | item | Robin's words |
|---|---|---|
| 11:15 | option A of decision-old | "A" |

## Sessions

## Log

- 09:00 opened the day
"""
LANES = {"build": st.Lane(("implement", "pr", "review", "triage", "merge"), ("triage", "merge"))}
KANBAN = st.Kanban(("stage::implement", "stage::review", "stage::triage"), "needs-human",
                   {"implement": 0, "pr": 0, "review": 1, "triage": 2, "merge": 2})


class HoldCauseTest(unittest.TestCase):
    """#228: task_forge.hold_cause is the one place a hold is decided, and names what holds the task."""

    def setUp(self) -> None:
        self.tasks = {t.name: t for t in parse_tracker(TRACKER).tasks}
        self.kanban = replace(KANBAN, hold_stages=("triage",))

    def cause(self, name: str, refs=frozenset(), kanban=None, workers=frozenset(), task=None) -> str:
        return task_forge.hold_cause(task or self.tasks[name], kanban or self.kanban, None, workers=workers,
                                     pending_refs=refs, home=None, lane=LANES["build"])

    def test_a_pending_decision_on_the_issue_comes_first(self) -> None:
        self.assertEqual(self.cause("Write README", {"#7"}), "decision")

    def test_else_a_kanban_hold_stage_names_the_stage(self) -> None:
        self.assertEqual(self.cause("Write README"), "triage")

    def test_else_a_waiting_task_names_the_person_it_waits_on(self) -> None:
        waiting = replace(self.tasks["Console"], state="waiting")
        self.assertEqual(self.cause("", task=waiting), "Robin")
        self.assertEqual(self.cause("", task=waiting, workers={"Robin"}), "")

    def test_nothing_holds_a_running_task_off_a_hold_stage(self) -> None:
        self.assertEqual(self.cause("Cut the release"), "")

    def test_nothing_holds_a_done_task(self) -> None:
        # #240: a finished task is not held, though its issue has a pending decision and it sits at a hold stage.
        self.assertEqual(self.cause("", {"#7"}, task=replace(self.tasks["Write README"], state="done")), "")

    def test_a_waiting_task_with_no_owner_waits_on_no_one(self) -> None:
        # `unassigned` is the tracker's word for no owner (#193), not a person to wait on.
        self.assertEqual(self.cause("", task=replace(self.tasks["Console"], state="waiting", owner="unassigned")), "")

    def test_the_build_flag_and_the_tile_count_every_cause(self) -> None:
        # #228 review: the BUILD flag and the NEEDS INPUT tile read the same cause as the Flow note, not only a
        # [kanban] hold stage: a task waiting on a person, and a task whose issue a pending decision holds.
        waiting = TRACKER.replace("| Robin | open | 09:00 | 17:00 | S | build | triage |",
                                  "| Robin | waiting | 09:00 | 17:00 | S | build | triage |")
        for case, tracker, refs in (("waiting on Robin", waiting, {}), ("decision", TRACKER, {"#9": None})):
            with self.subTest(case=case):
                html = rb.render(tracker, parse_coordinator(CLAUDE_MD, today=NOW.date()), NOW, lanes=LANES, kanban=KANBAN,
                                 sources=SOURCES, pending_refs=refs)
                tiles = dict(re.findall(r'<span class="panels-label">([A-Z ]+)</span><b class="panels-count">(\d+)</b>', html))
                self.assertEqual(tiles["NEEDS INPUT"], "1")
                self.assertEqual(re.findall(r'<div class="columns-tag columns-flag" data-cat="hold">([^<]*)<', html),
                                 ["NEEDS INPUT"])


class SinceLookedTest(unittest.TestCase):
    """#229: the Since tab counts the holds that began after the last look: a gate stage from its last `stage:` move
    into it, a decision from when it was asked. A hold with no known start (a person-owned wait) is not counted."""

    def test_a_hold_logged_in_the_looks_minute_counts(self) -> None:
        # Codex on #579: the Log keeps minutes and a look keeps seconds; a move logged in the look's own minute may
        # have come after it, and a count shown twice is better than one missed.
        a, b, *_ = parse_tracker(TRACKER).tasks
        look = NOW.replace(second=10)
        self.assertEqual(task_forge.went_held({a: "triage", b: "triage"},
                                              {a: NOW.replace(second=0), b: NOW - timedelta(minutes=1)}, look), 1)

    def test_a_decision_asked_before_the_look_in_its_minute_does_not_count(self) -> None:
        # Codex on #579: a decision's asked time keeps seconds, so the Log's same-minute allowance is not its.
        a, b, *_ = parse_tracker(TRACKER).tasks
        look = NOW.replace(second=50)
        held = {a: "decision", b: "decision"}
        self.assertEqual(task_forge.went_held(held, {a: NOW.replace(second=10), b: NOW.replace(second=55)}, look), 1)

    def test_went_held_counts_only_holds_known_to_begin_after_the_look(self) -> None:
        a, b, c, d = parse_tracker(TRACKER).tasks
        held = {a: "decision", b: "triage", c: "Robin", d: "Robin"}
        began = {a: NOW, b: NOW - timedelta(hours=2), c: None}
        self.assertEqual(task_forge.went_held(held, began, NOW - timedelta(hours=1)), 1)

    def began(self, log: str, cause: str = "triage", name: str = "Write README"):
        tasks = parse_tracker(TRACKER).tasks
        task = next(t for t in tasks if t.name == name)
        moves = tracker_log.moves(TRACKER + log, NOW.date(), NOW.tzinfo)
        return task_forge.hold_began({task: cause}, moves, {}, None, tasks)[task]

    def test_a_move_under_an_earlier_rows_name_or_item_is_the_tasks(self) -> None:
        # Codex on #579: a move logged before a rename names the earlier row; it is today's task by the earlier row's
        # name, or, when the name changed too, by its issue.
        tasks = parse_tracker(TRACKER).tasks
        task = next(t for t in tasks if t.name == "Write README")
        day = NOW.date() - timedelta(days=1)
        for earlier_row, moved in (("| Write README | Draft the README |", "Draft the README"),
                                   ("| README draft | Draft the README |", "README draft")):
            with self.subTest(moved):
                earlier = TRACKER.replace("| Write README | Write eval README |", earlier_row)
                moves = tracker_log.moves(earlier + f"- 11:00 stage: {moved} → triage\n", day, NOW.tzinfo)
                began = task_forge.hold_began({task: "triage"}, moves, {}, None, tasks, parse_tracker(earlier).tasks)
                self.assertEqual(began[task], datetime.combine(day, time(11, 0), NOW.tzinfo))

    def test_a_gate_hold_begins_at_the_tasks_last_move_into_that_gate(self) -> None:
        # #579 review R3, R7: moves read as flow_chart reads them: by name or item, any case; the latest move into the
        # gate, not another stage's or another task's.
        at = lambda hhmm: NOW.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]))  # noqa: E731
        self.assertEqual(self.began("- 11:00 stage: Write README → triage\n- 12:00 stage: Write README → merge\n"
                                    "- 13:00 stage: Write README → triage\n"), at("13:00"))
        self.assertIsNone(self.began("- 11:00 stage: Write README → merge\n"))
        self.assertIsNone(self.began("- 11:00 stage: Cut the release → triage\n"))
        self.assertEqual(self.began("- 11:00 stage: write eval readme → triage\n"), at("11:00"))

    def test_a_launcher_line_moves_a_task_only_until_its_first_stage_move(self) -> None:
        # #579 review R7: flow_chart's rule; `completed` puts the task at pr.
        at = lambda hhmm: NOW.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]))  # noqa: E731
        started = "- 10:00 one-shot w1 started: claude, task Write eval README\n"
        self.assertEqual(self.began(started + "- 11:00 one-shot w1: completed; awaiting integration\n", "pr"), at("11:00"))
        self.assertIsNone(self.began("- 09:30 stage: Write README → implement\n" + started +
                                     "- 11:00 one-shot w1: completed; awaiting integration\n", "pr"))

    def test_the_since_time_is_shown_in_the_workspace_zone(self) -> None:
        # #579 review R4: a look stored in another zone is titled in the workspace's.
        from datetime import timezone
        gate = replace(KANBAN, hold_stages=("triage",))
        self.assertEqual(self.since((NOW - timedelta(hours=2)).astimezone(timezone.utc), kanban=gate,
                                    log="- 13:00 stage: Write README → triage\n"), [("1 went needs input since 12:30", "1")])

    def since(self, looked, refs=None, kanban=KANBAN, log="") -> list[str]:
        html = rb.render(TRACKER + log, parse_coordinator(CLAUDE_MD, today=NOW.date()), NOW, lanes=LANES, kanban=kanban,
                         sources=SOURCES, pending_refs=refs or {}, looked=looked)
        return re.findall(r'title="(\d+ went needs input since [\d:]+)">(\d+)<', html)

    def test_the_board_counts_a_gate_move_and_a_decision_after_the_look(self) -> None:
        gate, move = replace(KANBAN, hold_stages=("triage",)), "- 13:00 stage: Write README → triage\n"
        for case, args, want in (
                ("moved into the gate after", dict(kanban=gate, log=move), [("1 went needs input since 12:30", "1")]),
                ("asked after", dict(refs={"#9": NOW - timedelta(minutes=30)}), [("1 went needs input since 12:30", "1")]),
                ("asked at an unknown time", dict(refs={"#9": None}), []),
                ("held with no move", dict(kanban=gate), [])):
            with self.subTest(case=case):
                self.assertEqual(self.since(NOW - timedelta(hours=2), **args), want)
        self.assertEqual(self.since(NOW - timedelta(minutes=30), kanban=gate, log=move), [])
        self.assertEqual(self.since(None, kanban=gate, log=move), [("1 went needs input since 00:00", "1")])


class MergeOrder(unittest.TestCase):
    def setUp(self) -> None:
        self.panel = rb.merge_order(SOURCES)

    def test_approved_first_ready_before_failed_unshared_before_shared_then_in_review(self) -> None:
        self.assertEqual([(r.num, r.ref) for r in self.panel.rows], [("1", "!41"), ("2", "!42"), ("3", "!52"), ("4", "!48")])

    def test_drafts_and_closed_changes_are_not_in_the_order(self) -> None:
        self.assertNotIn("!50", [r.ref for r in self.panel.rows])
        self.assertNotIn("!37", [r.ref for r in self.panel.rows])

    def test_facts_are_approval_pipeline_base_and_shared_files(self) -> None:
        facts = {r.ref: r.facts for r in self.panel.rows}
        self.assertEqual(facts["!41"], "approved · pipeline passed · base main · no shared files")
        self.assertEqual(facts["!42"], "approved · pipeline passed · base main · shares 1 file with !48, !50")
        self.assertEqual(facts["!48"], "in review · pipeline passed · base main · shares 1 file with !42, !50")
        self.assertEqual(facts["!52"], "approved · pipeline failed · base main · no shared files")

    def test_head_counts(self) -> None:
        self.assertEqual((self.panel.id, self.panel.count, self.panel.note), ("merge", 4, "3 approved · 1 in review"))
        self.assertEqual((self.panel.rows[0].name, self.panel.rows[0].href), ("Password reset copy", "https://forge/41"))

    def test_the_order_shows_five_and_counts_the_rest(self) -> None:
        many = bs.Sources({}, {}, {f"!{n}": change(f"!{n}") for n in range(1, 9)}, (), {})
        p = rb.merge_order(many)
        self.assertEqual((p.count, len(p.rows), p.foot), (8, 5, "+ 3 more"))

    def test_empty_sources_leave_an_empty_order(self) -> None:
        p = rb.merge_order(bs.EMPTY)
        self.assertEqual((p.count, p.rows, p.note), (0, (), "0 approved · 0 in review"))


class Workers(unittest.TestCase):
    def test_one_row_per_worker_with_age_and_facts(self) -> None:
        p = rb.worker_panel(SOURCES, NOW)
        self.assertEqual((p.id, p.count, p.note), ("workers", 2, "1 one-shot · 1 session"))
        # (#199) facts are `<kind> · <runtime model> · <task> · <tree>`, empty parts skipped: impl-04 has
        # no tree, so nothing stands in for it — not the old "last reply" fallback.
        self.assertEqual([(r.name, r.side, r.facts) for r in p.rows], [
            ("impl-07", "0h42m", "one-shot · claude opus · Cut the release · trees/cut"),
            ("impl-04", "1h23m", "session · codex · Audit"),
        ])

    def test_empty(self) -> None:
        self.assertEqual(rb.worker_panel(bs.EMPTY, NOW).rows, ())


class Decisions(unittest.TestCase):
    def test_pending_rows_carry_asked_recommended_and_default(self) -> None:
        d = {"headline": "Cache warmup: keep the pool?", "recommended": "A", "default": "pool stays"}
        p = rb.decision_panel([("cache", d, NOW.replace(hour=1, minute=41), ""), ("bad", None, None, "no ask")], 4, NOW)
        self.assertEqual((p.id, p.count, p.foot, p.link), ("decisions", 1, "4 answered today", ("decisions page", "/decisions")))
        self.assertEqual(p.rows[0], panels.Row("Cache warmup: keep the pool?", "asked 01:41 · recommended A · default: pool stays",
                                               href="/decisions/cache"))
        self.assertEqual(p.rows[1], panels.Row("decision-bad.json", "unreadable: no ask"))

    def test_the_pending_list_is_the_decisions_page_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pages = Path(tmp)
            ask = {"headline": "H", "ask": "A?", "options": [{"key": "A", "title": "a"}, {"key": "B", "title": "b"}],
                   "recommended": "A", "why": "w", "default": "d"}
            for slug in ("one", "two", "done"):
                (pages / f"decision-{slug}.json").write_text(json.dumps(ask))
            (pages / "decision-broken.json").write_text("{")
            ctx = decision_page.Context(NOW, rows=(decision_page.Row(NOW.date(), None, "option A of decision-done", ""),))
            found = decision_page.entries(pages, ctx)
            self.assertEqual(sorted(slug for slug, *_ in found), ["broken", "one", "two"])
            self.assertIsNone(next(d for slug, d, *_ in found if slug == "broken"))
            self.assertEqual(decision_page.index(pages, ctx, "2026-09-16")[1], found)


class Build(unittest.TestCase):
    def cols(self, sources=SOURCES, kanban=None):
        return rb.build_columns(rb.parse_tracker(TRACKER).tasks, LANES, kanban, sources, NOW)

    def test_a_card_gains_its_change_ref_and_marks(self) -> None:
        # Both refs open the task page (#209): !48 names #9, so it lands there too.
        card = next(c for col in self.cols() for c in col.cards if c.name == "Cut the release")
        self.assertEqual(card.refs, (("#9", "/issues/9"), ("!48", "/issues/9")))
        self.assertEqual(card.marks, (columns.Mark("passed", "passed"),))

    def test_approval_and_a_failed_pipeline_are_marks(self) -> None:
        self.assertEqual(rb.change_marks((SOURCES.changes["!52"],)),
                         (columns.Mark("failed", "failed"), columns.Mark("approved", "approved")))
        self.assertEqual(rb.change_marks((change("!1", pipeline="running"),)), (columns.Mark("running", "pending"),))

    def test_an_issue_url_matches_its_number(self) -> None:
        # A ref's number, not a matching sources.issues URL, decides its link: it still opens /issues/7,
        # sources.issues["#7"] notwithstanding (#209).
        card = next(c for col in self.cols() for c in col.cards if c.name == "Write README")
        self.assertEqual(card.refs, (("#7", "/issues/7"),))

    def test_a_tracker_issue_url_links_without_sources(self) -> None:
        card = next(c for col in rb.build_columns(rb.parse_tracker(TRACKER).tasks, LANES) for c in col.cards if c.name == "Write README")
        self.assertEqual(card.refs, (("#7", "/issues/7"),))
        bare = next(c for col in self.cols() for c in col.cards if c.name == "Webhook check")
        self.assertEqual((bare.refs, bare.href), ((), ""))

    def test_main_holds_the_changes_merged_today(self) -> None:
        main = self.cols()[-1]
        self.assertEqual((main.name, [(c.name, c.refs, c.owner, c.state, c.kind) for c in main.cards]),
                         ("main", [("Locale fallback", (("#8", "/issues/8"), ("!37", "/issues/8")),
                                    "merged 12:30", "done", "merged today")]))
        html = columns.render([columns.Column("main", main.cards * 5)])
        self.assertIn("+ <span>2 merged today</span>", html)

    def test_drift_marks_the_card(self) -> None:
        card = next(c for col in self.cols(kanban=KANBAN) for c in col.cards if c.name == "Write README")
        self.assertIn(columns.Mark("drift", "drift"), card.marks)
        cut = next(c for col in self.cols(kanban=KANBAN) for c in col.cards if c.name == "Cut the release")
        self.assertNotIn(columns.Mark("drift", "drift"), cut.marks)

    def test_empty_sources_draw_the_cards_as_before(self) -> None:
        tasks = rb.parse_tracker(TRACKER).tasks
        self.assertEqual(rb.build_columns(tasks, LANES, KANBAN, bs.EMPTY, NOW), rb.build_columns(tasks, LANES, KANBAN))
        self.assertEqual([c.name for c in rb.build_columns(tasks, LANES)], ["implement", "review", "triage", "merge"])

    def test_a_merged_change_draws_the_card_at_its_lanes_last_stage(self) -> None:
        # (#240) A stalled coordinator can leave the tracker cell at `merge` for hours after the change actually
        # merged; the card draws at the lane's last stage instead, and carries neither drift nor the NEEDS INPUT flag for it —
        # the forge already answered the only question that stage cell was tracking.
        lanes = {"build": st.Lane(("implement", "pr", "review", "triage", "merge", "main"), ("triage", "merge"))}
        kanban = replace(KANBAN, hold_stages=("merge",), terminal_stages=("main",))
        sources = replace(SOURCES, issues={**SOURCES.issues, "#8": bs.Issue("#8", "https://forge/i/8", "Locale", "open", ())})
        tracker = TRACKER.replace("## Decisions",
                                  "| Ship the tool | Ship it | Robin | waiting | 09:00 |  | S | build | merge | #8 | Checklist: ship |\n"
                                  "\n## Decisions")
        tasks = rb.parse_tracker(tracker).tasks
        cols = rb.build_columns(tasks, lanes, kanban, sources, NOW)
        card = card_named(cols, "Ship the tool")
        self.assertEqual(next(col.name for col in cols if card in col.cards), "main")
        self.assertNotIn(columns.Mark("drift", "drift"), card.marks)
        self.assertIsNone(card.flag)

    def test_an_open_or_undrafted_change_still_draws_at_its_own_stage(self) -> None:
        # The Cut the release task's change (!48) is open, not merged: no forge answer yet, so it stays put.
        cols = self.cols()
        card = card_named(cols, "Cut the release")
        self.assertEqual(next(col.name for col in cols if card in col.cards), "review")


def card_named(cols: list[columns.Column], name: str) -> columns.Card:
    return next(c for col in cols for c in col.cards if c.name == name)


class HomeProjectUrlTest(unittest.TestCase):
    """#409 review: a home-project task named by its issue's URL reads the forge's `#8` keys through `sources.home`;
    each reader (task_changes, drifts, the board card) must pass it, or its change, drift or ref silently vanishes."""

    HOME = backlog.Backlog(host="https://example.com", project="o/app")
    CELL = "https://example.com/o/app/-/issues/8"

    def setUp(self) -> None:
        self.sources = bs.Sources({}, {"#8": bs.Issue("#8", self.CELL, "Locale", "open", ("stage::implement",))},
                                  {"!60": change("!60", issues=("#8",))}, (), {}, self.HOME)
        tracker = TRACKER.replace("## Decisions", f"| Ship it | Ship | Robin | waiting | 09:00 |  | S | build | review | {self.CELL} | c |\n\n## Decisions")
        self.tasks = rb.parse_tracker(tracker).tasks
        self.task = next(t for t in self.tasks if t.name == "Ship it")

    def test_task_changes_finds_the_change_closing_the_issue(self) -> None:
        self.assertEqual([c.ref for c in task_forge.task_changes(self.task, self.sources)], ["!60"])

    def test_drifts_reads_the_issues_labels(self) -> None:
        # stage review, but the issue carries stage::implement
        self.assertTrue(task_forge.drifts(self.task, KANBAN, self.sources))

    def test_the_card_names_the_issue_by_its_short_key(self) -> None:
        card = card_named(rb.build_columns(self.tasks, LANES, KANBAN, self.sources, NOW), "Ship it")
        self.assertEqual(card.refs, (("#8", "/issues/8"), ("!60", "/issues/8")))
        self.assertIn(columns.Mark("drift", "drift"), card.marks)

    def test_the_cards_own_key_decides_its_ref(self) -> None:
        # no change and no known issue: only the card's own issue_key call decides its ref
        bare = replace(self.sources, changes={}, issues={})
        card = card_named(rb.build_columns(self.tasks, LANES, KANBAN, bare, NOW), "Ship it")
        self.assertEqual(card.refs, (("#8", "/issues/8"),))


class ForeignProjectUrlTest(unittest.TestCase):
    """#416: a card naming another project on the home host links to that project's issue, not `/issues/<n>`."""

    HOME = backlog.Backlog(host="https://example.com", project="o/app")
    CELL = "https://example.com/acme/web/-/issues/8"

    def card(self, issues: dict) -> columns.Card:
        sources = bs.Sources({}, issues, {}, (), {}, self.HOME)
        tracker = TRACKER.replace("## Decisions", f"| Ship it | Ship | Robin | waiting | 09:00 |  | S | build | review | {self.CELL} | c |\n\n## Decisions")
        return card_named(rb.build_columns(rb.parse_tracker(tracker).tasks, LANES, KANBAN, sources, NOW), "Ship it")

    def test_the_name_and_the_ref_open_the_other_projects_issue(self) -> None:
        known = {"example.com/acme/web#8": bs.Issue("example.com/acme/web#8", self.CELL, "Locale", "open", ())}
        for issues in ({}, known):
            with self.subTest(cached=bool(issues)):
                card = self.card(issues)
                self.assertEqual(card.refs, (("example.com/acme/web#8", self.CELL),))
                self.assertEqual(card.href, self.CELL)


class CardLinkTest(unittest.TestCase):
    """A board card's name opens its task page (#194); its issue and change refs open their pages too, through
    the change's own first issue when it differs from the card's (#209)."""

    def test_a_card_whose_issue_ends_in_a_number_links_its_name_to_the_task_page(self) -> None:
        # "A card whose issue cell ends in a number links its name to `/issues/<number>`." "A card's `#N` ref
        # links to `/issues/N`, and its `!N` ref links to the change's page" — !48 names #9, so both land there.
        # Cut the release has an open change; Write README's issue cell is a URL.
        for sources in (SOURCES, bs.EMPTY):
            cols = rb.build_columns(rb.parse_tracker(TRACKER).tasks, LANES, None, sources, NOW)
            with self.subTest(sources="some" if sources.changes else "none"):
                self.assertEqual(card_named(cols, "Write README").href, "/issues/7")
                self.assertEqual(card_named(cols, "Write README").refs, (("#7", "/issues/7"),))
                self.assertEqual(card_named(cols, "Cut the release").href, "/issues/9")
        cut = card_named(rb.build_columns(rb.parse_tracker(TRACKER).tasks, LANES, None, SOURCES, NOW), "Cut the release")
        self.assertEqual(cut.refs, (("#9", "/issues/9"), ("!48", "/issues/9")))

    def test_a_card_whose_issue_has_no_number_keeps_its_link(self) -> None:
        # "A card whose issue cell has no number keeps its current link." Set beside a numbered card, which moves.
        tasks = [rb.Task(f"Item {i}", "Robin", "open", "09:00", "", "c", name=f"Task {i}", issue=issue, lane="build", stage="implement")
                 for i, issue in enumerate(("https://forge/i/readme", "", "TBD", "#12"))]
        cols = rb.build_columns(tasks, LANES)
        self.assertEqual([card_named(cols, f"Task {i}").href for i in range(4)], ["https://forge/i/readme", "", "", "/issues/12"])

    def test_merged_today_cards_link_through_their_issue(self) -> None:
        # "The merged-today cards in the main column follow the same rule through their issue." !37 names #8, so
        # both its refs land on /issues/8, whether or not #8 is a known issue. !38 names no issue at all — with
        # no number to route through, its own ref keeps the forge link (#209 gives no other page for it).
        sources = replace(SOURCES, changes={**SOURCES.changes, "!38": change("!38", state="merged", merged_at=NOW - timedelta(hours=1),
                                                                            title="Unfiled fix")})
        main = rb.build_columns(rb.parse_tracker(TRACKER).tasks, LANES, None, sources, NOW)[-1]
        self.assertEqual({c.name: (c.href, c.refs) for c in main.cards}, {
            "Locale fallback": ("/issues/8", (("#8", "/issues/8"), ("!37", "/issues/8"))),
            "Unfiled fix": ("https://forge/38", (("!38", "https://forge/38"),))})


class UnassignedCardTest(unittest.TestCase):
    """An owner cell of `unassigned`, in any case, is the tracker's word for no owner (#193)."""

    def test_an_unassigned_card_shows_no_owner(self) -> None:
        # "A board card of such a task shows no owner." "Any other owner cell still shows as written."
        for owner, shown in (("unassigned", ""), ("Unassigned", ""), (" UNASSIGNED ", ""), ("Robin", "Robin"),
                             ("unassigned-bot", "unassigned-bot")):
            task = rb.Task("Check the webhook signature", owner, "open", "09:00", "", "c", name="Webhook check",
                           lane="build", stage="implement")
            html = columns.render(rb.build_columns([task], LANES))
            with self.subTest(owner=owner):
                if shown:
                    self.assertIn(f'<span class="columns-owner">{shown}</span>', html)
                else:
                    self.assertNotRegex(html, r"(?i)unassigned")


class CardRefAndOwnerLinkTest(unittest.TestCase):
    """Card refs and worker owners link to their pages (#209): a card's `#N` ref links to `/issues/N`; its `!N`
    ref links to the change's own page, and pages.py serves no route for a change (only `/issues/<n>[/source]`,
    `/workers` and `/decisions[/<slug>]`), so it takes the task page of the issue the change names instead — the
    change's own first issue (the merged-today cards' `c.issues[:1]` rule), not necessarily the card's own issue.
    A live worker owner links to `/workers`; a person or `unassigned` owner stays plain text."""

    def test_an_issue_ref_links_to_its_issues_page(self) -> None:
        html = columns.render(rb.build_columns(rb.parse_tracker(TRACKER).tasks, LANES, None, SOURCES, NOW))
        self.assertIn('<a href="/issues/9">#9</a>', html)

    def test_a_change_ref_links_to_its_issues_task_page_not_the_forge(self) -> None:
        # !48 belongs to #9 (SOURCES). Today it opens https://forge/48; no change route exists to replace that
        # with, so it should take #9's task page instead.
        html = columns.render(rb.build_columns(rb.parse_tracker(TRACKER).tasks, LANES, None, SOURCES, NOW))
        self.assertIn('<a href="/issues/9">!48</a>', html)
        self.assertNotIn('href="https://forge/48"', html)

    def test_a_change_ref_takes_the_changes_own_first_issue(self) -> None:
        # A change naming two issues links through its own first issue, even when that differs from the card
        # it is shown on (which is keyed by #9, drawn here second in !99's issues).
        multi = change("!99", issues=("#3", "#9"), title="Cross-filed fix")
        sources = replace(SOURCES, changes={**SOURCES.changes, "!99": multi})
        html = columns.render(rb.build_columns(rb.parse_tracker(TRACKER).tasks, LANES, None, sources, NOW))
        self.assertIn('<a href="/issues/3">!99</a>', html)

    def test_only_a_worker_owner_becomes_a_link(self) -> None:
        # "A worker owner links to that worker on /workers; a person or unassigned owner stays plain text."
        # impl-07 is a live worker in SOURCES; Robin and unassigned are not.
        tasks = [rb.Task("Cut the release branch", "impl-07", "running 10:30", "10:30", "23:00", "c",
                         name="Cut the release", issue="#9", lane="build", stage="review"),
                 rb.Task("Write the eval README", "Robin", "open", "09:00", "17:00", "c",
                         name="Write README", lane="build", stage="triage"),
                 rb.Task("Check the webhook signature", "unassigned", "open", "09:00", "", "c",
                         name="Webhook check", lane="build", stage="implement")]
        html = columns.render(rb.build_columns(tasks, LANES, None, SOURCES, NOW))
        with self.subTest(owner="impl-07, a running worker"):
            m = re.search(r'<a[^>]*href="/workers"[^>]*>impl-07</a>', html)
            self.assertIsNotNone(m, html)
            self.assertNotIn("style=", m[0])  # muted look reused, not a new inline colour
        with self.subTest(owner="Robin, a person"):
            self.assertIn('<span class="columns-owner">Robin</span>', html)
        with self.subTest(owner="unassigned"):
            self.assertNotRegex(html, r"(?i)unassigned")


class Page(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.html = rb.render(TRACKER, self.cfg, NOW, lanes=LANES, kanban=KANBAN, sources=SOURCES,
                              decisions=[("x", {"headline": "Pick", "recommended": "B", "default": "d"}, NOW.replace(hour=10, minute=0), "")],
                              answered=1, tracker_at=NOW - timedelta(minutes=5))
        self.body = self.html.split("</style>", 1)[1]

    def test_the_board_is_the_flow_artboard_in_order(self) -> None:
        marks = ['class="panels-head"', 'class="panels-tiles"', '<section id="flow">', 'class="panels"']
        at = [self.body.index(m) for m in marks]
        self.assertEqual(at, sorted(at), [m for _, m in sorted(zip(at, marks))])

    def test_the_header_names_each_source_time_and_the_counts(self) -> None:
        self.assertIn("tracker 14:25 · kanban 14:28 · merge requests 14:27 · workers 14:30 · "  # (#215) no render clock
                      "4 tasks</div>", self.body)
        self.assertNotRegex(self.body, r"(?i)no[ -]lane")  # #350: NO LANE is gone; the 4 still counts the task with no lane

    def test_a_source_error_is_in_the_header(self) -> None:
        html = rb.render(TRACKER, self.cfg, NOW, lanes=LANES, sources=bs.Sources({}, {}, {}, (), {"workers": "no registry"}))
        self.assertIn('<div class="panels-warn">workers: no registry</div>', html)

    def test_tile_counts(self) -> None:
        # #349: the artboard's label. ALL counts every task (the header's 4 tasks); NEEDS INPUT is what BLOCKED counted, the held
        # cards (none: no kanban hold stage); RUNNING is the two workers; DRIFT: README at triage has stage::implement.
        # The DECISIONS tile's count is no tile now: the tab bar's badge and the DECISIONS panel carry it.
        tiles = re.findall(r'<span class="panels-label">([A-Z ]+)</span><b class="panels-count">(\d+)</b>', self.body)
        self.assertEqual(tiles, [("ALL", "4"), ("NEEDS INPUT", "0"), ("RUNNING", "2"), ("APPROVED", "3"), ("ORPHANED", "1"), ("DRIFT", "1")])

    def test_each_tile_links_where_it_linked_before(self) -> None:
        # #349: the artboard's label. BLOCKED linked to #flow, APPROVED to #merge, RUNNING to #workers, ORPHANED and DRIFT to #flow;
        # ALL had no tile, and the BUILD section whose cards it counts is #flow.
        links = re.findall(r'<a class="panels-tile" data-kind="([a-z]+)" href="#([a-z]+)"><span class="panels-label">([A-Z ]+)</span>', self.body)
        # #349 review: the kind is what the stylesheet colours a tile by, so each is pinned with its link and label.
        self.assertEqual(links, [("all", "flow", "ALL"), ("blocked", "flow", "NEEDS INPUT"), ("running", "workers", "RUNNING"),
                                 ("approved", "merge", "APPROVED"), ("orphaned", "flow", "ORPHANED"), ("drift", "flow", "DRIFT")])

    def test_every_tile_kind_has_a_colour_and_all_draws_in_the_artboards_ink(self) -> None:
        # #349 review: the artboard's tone map gives ALL ink (Flow.dc.html `all: '#2f2630'`, the light --fg); a tile with
        # no rule for its kind draws with no --k at all.
        css = self.html.split("<style>", 1)[1].split("</style>", 1)[0]
        for kind in re.findall(r'class="panels-tile" data-kind="([a-z]+)"', self.body):
            with self.subTest(kind=kind):
                self.assertIn(f'.panels-tile[data-kind="{kind}"]', css)
                self.assertIn(kind, rb.PANEL_COLOURS)
        self.assertEqual(rb.PANEL_COLOURS["all"], "var(--fg)")
        self.assertIn('[data-kind="all"]{--k:var(--fg)}', css)

    def test_all_counts_every_task_not_only_the_active_ones(self) -> None:
        # #349: the artboard's ALL counts every task, as the header does, finished ones too.
        tracker = TRACKER.replace("## Decisions", "| Old chore | Done long ago | Robin | done | 08:00 |  | S |  |  |  | c |\n\n## Decisions")
        html = rb.render(tracker, self.cfg, NOW, lanes=LANES, kanban=KANBAN, sources=SOURCES)
        self.assertIn("5 tasks</div>", html)
        self.assertIn('<span class="panels-label">ALL</span><b class="panels-count">5</b>', html)

    def test_the_old_tile_and_flag_words_are_drawn_nowhere(self) -> None:
        # #349: the artboard's label. A board with held cards and a pending decision draws none of BLOCKED, ON HOLD, or
        # DECISIONS as a tile: not in the tiles, the card flags, or any text after the style block.
        held = replace(KANBAN, hold_stages=("triage", "implement"))
        html = rb.render(TRACKER, self.cfg, NOW, lanes=LANES, kanban=held, sources=SOURCES,
                         decisions=[("x", {"headline": "Pick", "recommended": "B", "default": "d"}, NOW.replace(hour=10, minute=0), "")])
        drawn = re.sub(r"<[^>]*>", " ", html.split("</style>", 1)[1])  # the words a reader sees, not class or data- names
        self.assertNotRegex(drawn, r"(?i)\bblocked\b|on[ -]hold")
        self.assertNotIn('<span class="panels-label">DECISIONS</span>', html)

    def test_needs_input_counts_the_held_cards_and_each_is_flagged_so(self) -> None:
        # #349: the artboard's label. What BLOCKED counted, the held cards; the flag on each reads NEEDS INPUT too.
        # The board counts held cards only: the live canvas's `needs` list also takes in what the user owns and the
        # orphaned, which the tile does not (#383 asks what the tile should count).
        held = replace(KANBAN, hold_stages=("triage", "implement"))
        html = rb.render(TRACKER, self.cfg, NOW, lanes=LANES, kanban=held, sources=SOURCES)
        tiles = dict(re.findall(r'<span class="panels-label">([A-Z ]+)</span><b class="panels-count">(\d+)</b>', html))
        self.assertEqual(tiles["NEEDS INPUT"], "2")
        flags = re.findall(r'<div class="columns-tag columns-flag" data-cat="hold">([^<]*)<', html)
        self.assertEqual(flags, ["NEEDS INPUT", "NEEDS INPUT"])

    def test_tasks_with_no_name_are_held_each_by_its_own_stage(self) -> None:
        # Keyed by name, every blank-named task shared one stage: one hold held them all, or none of them.
        tracker = TRACKER.replace("| Write README |", "|  |").replace("| Webhook check |", "|  |")
        held = replace(KANBAN, hold_stages=("triage",))
        html = rb.render(tracker, self.cfg, NOW, lanes=LANES, kanban=held, sources=SOURCES)
        # #349: the artboard's label. One card is flagged, as strongly as ON HOLD counted before: flags only, not the tile.
        flags = re.findall(r'<div class="columns-tag columns-flag" data-cat="hold">([^<]*)<', html)
        self.assertEqual(flags, ["NEEDS INPUT"])

    def test_needs_input_and_drift_exclude_a_task_whose_change_already_merged(self) -> None:
        # (#240) The same forge answer that moves a stuck card off the `merge` column keeps it out of the
        # NEEDS INPUT and DRIFT tiles too — both read `held`/`drifts` on every task, not just the laned ones build_columns sees.
        lanes = {"build": st.Lane(("implement", "pr", "review", "triage", "merge", "main"), ("triage", "merge"))}
        kanban = replace(KANBAN, hold_stages=("merge",), terminal_stages=("main",))
        sources = replace(SOURCES, issues={**SOURCES.issues, "#8": bs.Issue("#8", "https://forge/i/8", "Locale", "open", ())})
        tracker = TRACKER.replace("## Decisions",
                                  "| Ship the tool | Ship it | Robin | waiting | 09:00 |  | S | build | merge | #8 | Checklist: ship |\n"
                                  "\n## Decisions")
        html = rb.render(tracker, self.cfg, NOW, lanes=lanes, kanban=kanban, sources=sources)
        tiles = dict(re.findall(r'<span class="panels-label">([A-Z ]+)</span><b class="panels-count">(\d+)</b>', html))
        # #349: the artboard's label (BLOCKED is NEEDS INPUT).
        self.assertEqual(tiles["NEEDS INPUT"], "0")
        self.assertEqual(tiles["DRIFT"], "1")  # unrelated, pre-existing: README at triage has stage::implement

    def test_tiles_link_to_their_sections(self) -> None:
        for anchor in re.findall(r'class="panels-tile" data-kind="[a-z]+" href="#([a-z]+)"', self.body):
            self.assertIn(f'id="{anchor}"', self.body)

    def test_empty_sources_render_and_running_falls_back_to_tasks(self) -> None:
        html = rb.render(TRACKER, self.cfg, NOW, lanes=LANES)
        tiles = re.findall(r'<span class="panels-label">([A-Z ]+)</span><b class="panels-count">(\d+)</b>', html)
        # #349: the artboard's label, in the artboard's order.
        self.assertEqual(tiles, [("ALL", "4"), ("NEEDS INPUT", "0"), ("RUNNING", "1"), ("APPROVED", "0"), ("ORPHANED", "1"), ("DRIFT", "0")])
        self.assertIn("tracker 14:30 · 4 tasks", html)  # (#215) no render clock

    def test_panel_and_tile_colours_are_board_tokens_light_and_dark(self) -> None:
        css = self.html.split("<style>", 1)[1].split("</style>", 1)[0]
        ours = [line for line in css.splitlines() if ".panels" in line and not line.startswith(".panels-head{display")]
        overrides = "\n".join(line for line in ours if "--panels-" in line or "{--k:" in line)
        self.assertTrue(overrides)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", overrides), [])
        self.assertIn(".panels-head,.panels-tiles,.panels{--panels-ink:var(--fg)", css)
        self.assertIn('[data-kind="merge"]{--k:var(--stage-review)}', css)

    def test_the_clocks_tick_every_second_with_seconds(self) -> None:
        script = self.html.split("<script>", 1)[1]
        self.assertIn("setInterval(tick,1000)", script)
        self.assertIn("second:'2-digit'", script)
        self.assertIn("+'s'", script)

    def test_the_build_columns_show_task_names(self) -> None:
        # BUILD is the stage columns (#259), each task a card named by its link.
        self.assertIn('<figure class="columns"', self.body)
        self.assertRegex(self.body, r'class="columns-card-name"[^>]*>Cut the release<')
        self.assertNotIn("<table", self.body)
        self.assertNotIn('href="#"', self.body)

    def test_the_page_fits_a_phone(self) -> None:
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', self.html)



class EpicRiskTest(unittest.TestCase):
    """#230: the Epics tab counts the tasks' milestones at risk and late, each milestone once by its URL."""

    def risk(self, rows, ends=None, now=NOW):
        """rows: (issue ref, milestone, issue state, task state); ends: {item: datetime}."""
        issues = {ref: bs.Issue(ref, f"https://forge/i/{ref[1:]}", ref, state, (), "", None, m)
                  for ref, m, state, _ in rows}
        tasks = [
            task_forge.Task(f"item {ref}", "Robin", task_state, "09:00", "", "", issue=ref) for ref, _, _, task_state in rows]
        return task_forge.epic_risk(tasks, replace(bs.EMPTY, issues=issues), ends or {}, NOW.tzinfo, now)

    @staticmethod
    def milestone(due, *, url="https://github.com/o/app/milestone/1", state="open", open_issues=2, title="v2"):
        return bs.Milestone(title, url, due, state, open_issues)

    def test_late_is_open_past_due_with_open_issues(self):
        past = NOW.date() - timedelta(days=1)
        self.assertEqual(self.risk([("#1", self.milestone(past), "open", "waiting")]), (0, 1))
        for why, m in (("closed", self.milestone(past, state="closed")), ("no open issues", self.milestone(past, open_issues=0)),
                       ("due today", self.milestone(NOW.date())), ("no due date", self.milestone(None))):
            with self.subTest(why):
                self.assertEqual(self.risk([("#1", m, "open", "waiting")]), (0, 0))

    def test_at_risk_is_a_forecast_after_its_due(self):
        due = NOW.date() + timedelta(days=1)
        rows = [("#1", self.milestone(due), "open", "waiting"), ("#2", self.milestone(due), "open", "waiting")]
        on_time = {"item #1": NOW + timedelta(hours=2), "item #2": NOW + timedelta(days=1)}
        self.assertEqual(self.risk(rows, on_time), (0, 0))
        self.assertEqual(self.risk(rows, {**on_time, "item #2": NOW + timedelta(days=2)}), (1, 0))  # the latest estimate
        # #580 review R5: the latest, not the last row's.
        self.assertEqual(self.risk(rows, {"item #1": NOW + timedelta(days=2), "item #2": NOW}), (1, 0))

    def test_only_open_tasks_on_open_issues_forecast(self):
        due = NOW.date() + timedelta(days=1)
        late_end = {"item #1": NOW + timedelta(days=3)}
        self.assertEqual(self.risk([("#1", self.milestone(due), "open", "done")], late_end), (0, 0))
        self.assertEqual(self.risk([("#1", self.milestone(due), "closed", "waiting")], late_end), (0, 0))
        self.assertEqual(self.risk([("#1", self.milestone(due), "open", "waiting")]), (0, 0))  # no estimate, no forecast
        self.assertEqual(self.risk([("#1", self.milestone(None), "open", "waiting")], late_end), (0, 0))
        self.assertEqual(self.risk([("#1", self.milestone(due, state="closed"), "open", "waiting")], late_end), (0, 0))  # R4

    def test_a_late_milestone_is_not_also_at_risk(self):
        past = NOW.date() - timedelta(days=1)
        self.assertEqual(self.risk([("#1", self.milestone(past), "open", "waiting")], {"item #1": NOW + timedelta(days=3)}), (0, 1))

    def test_the_forecast_is_judged_on_the_workspace_date(self):
        # #580 review R3: given in UTC. 01:00Z on the 17th is 20:00 on the 16th in Chicago, so a milestone due the 16th
        # is not yet late, and an end at 04:30Z on the 17th (23:30 on the 16th there) is on time.
        utc = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)
        due = utc.astimezone(NOW.tzinfo).date()
        end = datetime(2026, 9, 17, 4, 30, tzinfo=timezone.utc)
        self.assertEqual(self.risk([("#1", self.milestone(due), "open", "waiting")], {"item #1": end}, now=utc), (0, 0))

    def test_two_repos_with_one_title_are_two_epics(self):
        # COS Architecture on #230: keyed by URL, not title.
        past, due = NOW.date() - timedelta(days=1), NOW.date() + timedelta(days=1)
        rows = [("#1", self.milestone(past, url="https://github.com/o/app/milestone/1"), "open", "waiting"),
                ("o/lib#1", self.milestone(due, url="https://github.com/o/lib/milestone/1"), "open", "waiting"),
                ("#2", self.milestone(past, url="https://github.com/o/app/milestone/1"), "open", "waiting")]
        self.assertEqual(self.risk(rows, {"item o/lib#1": NOW + timedelta(days=3)}), (1, 1))


class EpicsOnTheBoardTest(unittest.TestCase):
    """#230: the board passes the tab bar its milestones at risk and late."""

    def test_the_board_counts_a_late_milestone(self):
        late = bs.Milestone("v2", "https://forge/m/1", NOW.date() - timedelta(days=1), "open", 1)
        sources = replace(SOURCES, issues={**SOURCES.issues, "#9": replace(SOURCES.issues["#9"], milestone=late)})
        cfg = parse_coordinator(CLAUDE_MD, today=NOW.date())
        self.assertIn('title="milestones: 1 late">1 late</span>', rb.render(TRACKER, cfg, NOW, lanes=LANES, sources=sources))
        self.assertNotIn("milestones:", rb.render(TRACKER, cfg, NOW, lanes=LANES, sources=SOURCES))


if __name__ == "__main__":
    unittest.main()
