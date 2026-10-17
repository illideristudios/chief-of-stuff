"""#259: the board draws only the Flow artboard's sections, and draws each task once in BUILD.

The reference is the canvas's Flow artboard (Flow.dc.html), top to bottom: the tab bar; the header; six tiles;
BUILD, the stage columns (no NO LANE strip: #350); the panels; `FLOW · 24 HOURS` and `FLOW · 7 DAYS` with one legend.
Due next, Blocked, Sessions, the 24-hour day strip and the Lanes table are not in it, so none is drawn.

Artboard markers the stage columns map onto columns.py's classes: a column (`columns-col`, head `columns-name`, gate
columns `columns-gate`, count `columns-count`); a card (`columns-card`, name link `columns-card-name`, refs
`columns-refs`, owner `columns-owner`, state chip `columns-chip`, held card `columns-flagged` + `columns-flag`). The artboard's BUILD container is `id="flow"`, which the tiles
link to, so that id stays: it is the stage columns, not the Lanes table.

The Flow charts draw a task again by the artboard's own rows (one row per task, a bar per stage). That is the chart, not
a second BUILD, so these tests assert the BUILD drawing is single and leave the chart rows to test_flow_chart.py.
"""

from __future__ import annotations

import re
import sys
import unittest
from functools import cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "scripts"), str(HERE)]
import render_board as rb  # noqa: E402
from workspace import parse_coordinator  # noqa: E402
from test_flow_chart import KANBAN, LANES, NOW, TODAY, TODAY_TRACKER, YESTERDAY, YESTERDAY_TRACKER  # noqa: E402

CLAUDE = ("# W\n\n## Coordinator\n\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
          "- Timezone: America/Chicago\n- Settings: `cos.toml`\n")
# Four tasks in the build lane (one held at the triage gate, one merged into main) and four in no lane, which BUILD does
# not draw (#350) while the tiles and Flow charts still count them: one that never moved (no tile, no Flow row), one
# running and one orphaned (the RUNNING and ORPHANED tiles), and two with `stage:` Log lines (a Flow row each; the waiting
# one owned by a person, so its row ends in a hold bar).
LANED = ["Upload size limit", "Cache warmup", "Locale fallback", "Session timeout"]
IDLE = ["Rotate keys"]
MOVED = ["Renew certs", "Sign vendor form"]
NO_LANE = IDLE + MOVED + ["Archive logs"]
TRACKER = TODAY_TRACKER.replace(
    "\n\n## Log", """
| Rotate keys | Rotate the keys | Robin | open | 2026-09-26 |  | S |  |  |  | c |
| Renew certs | Renew the certs | impl-2 | running 01:20 | 2026-09-26 |  | S |  |  |  | c |
| Archive logs | Archive the logs | unassigned | orphaned | 2026-09-26 |  | S |  |  |  | c |
| Sign vendor form | Sign the form | Robin | waiting | 2026-09-26 |  | S |  |  |  | c |

## Log""").replace("- 01:40 stage: Cache warmup → triage\n", """- 01:20 stage: Renew certs → implement
- 01:40 stage: Cache warmup → triage
- 01:50 stage: Sign vendor form → review
""")

REMOVED = ('id="due-next"', 'id="blocked"', 'id="sessions"', 'id="day-strip"', "<h2>Due next</h2>", "<h2>Blocked</h2>",
           "<h2>Sessions</h2>", "<h2>24 hours</h2>", "<h2>Lanes</h2>", "lane-table", 'class="lt-', "session-row")


@cache
def page() -> str:
    cfg = parse_coordinator(CLAUDE, today=TODAY)
    html = rb.render(TRACKER, cfg, NOW, lanes=LANES, tracker_day=TODAY, kanban=KANBAN, stage_log=[(YESTERDAY, YESTERDAY_TRACKER)])
    return html.split("</style>", 1)[1]


def outline(body: str) -> tuple[list[str], list[str], int]:
    """What the page draws as sections: every <h2> text, every id, and the count of top-level <section>s.

    The column sections (`columns-col`) are BUILD's own parts, not sections of the board, so they are not counted.
    """
    h2 = [re.sub(r"<[^>]+>", "", t).strip() for t in re.findall(r"<h2\b[^>]*>(.*?)</h2>", body, re.S)]
    ids = re.findall(r'\sid="([^"]*)"', body)
    sections = [a for a in re.findall(r"<section\b([^>]*)>", body) if "columns-col" not in a]
    return h2, ids, len(sections)


def at(body: str, mark: str | re.Pattern) -> int:
    """Where `mark` first stands in `body`; a missing section is a failure that names it, not a ValueError."""
    m = re.search(mark, body, re.I) if isinstance(mark, str) else mark.search(body)
    if not m:
        raise AssertionError(f"the board does not draw {mark!r}")
    return m.start()


def build_region() -> str:
    """BUILD's drawing: from its heading to the panels."""
    body = page()
    return body[at(body, r"<h2>\s*Build\s*</h2>"):at(body, '<div class="panels">')]


class Board(unittest.TestCase):
    """A failure here names the section by a short message: the page is far too long to print."""

    @property
    def body(self) -> str:
        return page()

    def absent(self, mark: str, text: str) -> None:
        self.assertFalse(mark in text, f"the board draws {mark!r}, which the Flow artboard does not have")

    def seen(self, pattern: str, text: str, what: str) -> None:
        self.assertTrue(re.search(pattern, text, re.I | re.S), f"expected {what}: {pattern!r}")


class FlowDesign(Board):
    def test_the_sections_are_the_artboards_in_its_order(self) -> None:
        marks = ['<nav class="tabs"', '<header class="panels-head"', '<nav class="panels-tiles"', r"<h2>\s*Build\s*</h2>",
                 '<figure class="columns"', '<div class="panels">',
                 '<section class="panels-panel" id="merge"', '<section class="panels-panel" id="decisions"',
                 '<section class="panels-panel" id="workers"', "<h2>Flow · 24 hours</h2>", "<h2>Flow · 7 days</h2>", 'class="gantt-legend"']
        found = [(at(self.body, m), m) for m in marks]
        self.assertEqual([m for _, m in found], [m for _, m in sorted(found)])

    def test_the_panels_are_merge_order_decisions_and_workers(self) -> None:
        titles = re.findall(r'<h3 class="panels-title">(?:<a [^>]*>)?([^<]+)', self.body)
        self.assertEqual(titles, ["MERGE ORDER", "DECISIONS", "WORKERS"])

    def test_the_headings_and_section_ids_are_exactly_the_artboards(self) -> None:
        # An allow-list: a renamed or extra section ("Due soon", `id="due_next"`, "Sessions · 1") fails, not just the old names.
        # The tab bar has no <h2>; BUILD is `#flow`; the three panels are `h3`s with their own ids.
        h2, ids, sections = outline(self.body)
        self.assertEqual(h2, ["Build", "Flow · 24 hours", "Flow · 7 days"])
        self.assertEqual(ids, ["flow", "merge", "decisions", "workers"])
        self.assertEqual(sections, 4)

    def test_no_section_the_artboard_lacks_is_drawn(self) -> None:
        for mark in REMOVED:
            with self.subTest(mark=mark):
                self.absent(mark, self.body)

    def test_only_the_two_flow_charts_draw_a_gantt(self) -> None:
        # The 24-hour day strip was a third `<figure class="gantt">`; the artboard has the 24 HOURS and 7 DAYS charts only.
        self.assertEqual(self.body.count('<figure class="gantt"'), 2)
        self.assertEqual(self.body.count('class="gantt-legend"'), 1)

    def test_id_flow_is_the_build_columns_the_tiles_link_to_not_a_table(self) -> None:
        self.assertEqual(self.body.count('id="flow"'), 1)
        flow = self.body.split('id="flow"', 1)[1].split('<div class="panels">', 1)[0]
        self.seen(r"<h2>\s*Build\s*</h2>", flow, "BUILD inside #flow")
        self.seen('<figure class="columns"', flow, "the stage columns inside #flow")
        self.absent("<table", flow)


class OneBuildDrawing(Board):
    @property
    def build(self) -> str:
        return build_region()

    @property
    def before(self) -> str:
        return self.body[:at(self.body, 'id="flow"')]

    def drawn(self, name: str, within: str) -> int:
        return len(re.findall(rf">\s*{re.escape(name)}\s*<", within))

    def test_each_laned_task_is_drawn_once_between_the_tiles_and_the_panels(self) -> None:
        for name in LANED:
            with self.subTest(task=name):
                self.assertEqual(self.drawn(name, self.build), 1)

    def test_a_task_with_no_lane_is_not_drawn_in_build(self) -> None:
        # #350: the NO LANE strip is gone, so BUILD draws the laned tasks only.
        for name in NO_LANE:
            with self.subTest(task=name):
                self.assertEqual(self.drawn(name, self.build), 0)
                self.assertFalse(name in self.build, f"{name!r} is drawn in BUILD")

    def test_no_task_is_drawn_above_build(self) -> None:
        # Due next and Blocked sat above BUILD and named tasks; the header and tiles carry counts only.
        for name in LANED + NO_LANE:
            with self.subTest(task=name):
                self.assertFalse(name in self.before, f"{name!r} is drawn above BUILD")

    def test_a_task_with_no_lane_is_still_counted_as_before(self) -> None:
        # Pinned to what the code draws today, so the removal changes none of it: the header counts all 8 tasks, the
        # tiles read (#349: the artboard's label) ALL 8, NEEDS INPUT 2 (#228: the triage gate and the task waiting on Robin, the Flow's two holds), RUNNING 2 (Upload size limit and the lane-less Renew
        # certs), APPROVED 0, ORPHANED 1 (the lane-less Archive logs), DRIFT 0, and the Flow charts draw a row for each lane-less task that
        # moved, in both charts, and none for the one that never did.
        self.seen(r"\b8 tasks\b", self.body, "the header counting the tasks with no lane")
        tiles = re.findall(r'<span class="panels-label">([A-Z ]+)</span><b class="panels-count">(\d+)</b>', self.body)
        self.assertEqual(tiles, [("ALL", "8"), ("NEEDS INPUT", "2"), ("RUNNING", "2"), ("APPROVED", "0"), ("ORPHANED", "1"),
                                 ("DRIFT", "0")])
        flow = self.body[at(self.body, "<h2>Flow · 24 hours</h2>"):]
        for name in IDLE:
            with self.subTest(task=name):
                self.assertEqual(self.drawn(name, flow), 0)
        for name in MOVED:
            with self.subTest(task=name):
                self.assertEqual(self.drawn(name, flow), 2, "a row in each of the two Flow charts")

    def test_the_cards_are_the_laned_tasks_and_no_more(self) -> None:
        self.assertEqual(sorted(re.findall(r'<(?:a|div) class="columns-card-name"[^>]*>([^<]+)<', self.build)), sorted(LANED))


class BuildColumns(Board):
    @property
    def build(self) -> str:
        return build_region()

    @property
    def cols(self) -> list[tuple[str, str]]:
        return re.findall(r'<section class="columns-col([^"]*)" aria-label="[^"]*">(.*?)</section>', self.build, re.S)

    def column(self, name: str) -> str:
        for _, inner in self.cols:
            if re.search(rf'<h3 class="columns-name">{name}</h3>', inner, re.I):
                return inner
        raise AssertionError(f"no {name} column")

    def test_build_is_headed_with_its_issue_and_merge_request_counts(self) -> None:
        # Artboard: `{{laneCount}} issues · {{mrCount}} merge requests`, always both.
        self.seen(r'<h2>\s*Build\s*</h2>\s*<div class="meta">4 issues · 0 merge requests</div>', self.build, "BUILD's counts")

    def test_a_column_per_lane_stage_with_the_gates_marked(self) -> None:
        # The pr stage has no column of its own (its cards draw in the stage after, review).
        names = [re.search(r'<h3 class="columns-name">([^<]*)<', inner).group(1) for _, inner in self.cols]
        self.assertEqual(names, ["implement", "review", "triage", "merge", "main"])
        self.assertEqual([("columns-gate" in kind) for kind, _ in self.cols], [False, False, True, True, False])

    def test_each_column_head_counts_its_cards(self) -> None:
        for kind, inner in self.cols:
            with self.subTest(column=re.search(r'columns-name">([^<]*)<', inner).group(1)):
                self.assertEqual(int(re.search(r'<span class="columns-count">(\d+)</span>', inner).group(1)),
                                 len(re.findall(r'<div class="columns-card(?: columns-flagged)?"', inner)))

    def test_a_card_has_name_refs_owner_and_state(self) -> None:
        card = self.column("review").split('<div class="columns-card', 1)[1]
        for pattern in (r'class="columns-card-name"[^>]*>Upload size limit<', r'<div class="columns-refs">.*#109',
                        r'class="columns-owner">impl-1<', r'<span class="columns-tag columns-chip"[^>]*>running 01:00</span>'):
            self.seen(pattern, card, "a card's name, refs, owner and state chip")

    def test_a_held_card_is_flagged_across_its_foot(self) -> None:
        card = self.column("triage")
        self.seen(r'class="columns-card columns-flagged".*>Cache warmup<.*columns-flag"', card, "a flagged Cache warmup card")

    def test_there_is_no_no_lane_strip_or_count(self) -> None:
        # #350: no `columns-foot` strip, no "no lane" text anywhere, and the header reads `N tasks` with no clause after it.
        self.absent("columns-foot", self.body)
        self.assertIsNone(re.search(r"no[ _-]?lane", self.body, re.I), "the board says 'no lane'")
        self.seen(r"· 8 tasks</div>", self.body, "the header ending at its task count")


if __name__ == "__main__":
    unittest.main()
