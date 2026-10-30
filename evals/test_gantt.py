"""The Gantt module scales, clips, ticks and renders rows without knowing what the rows are."""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
import re
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import gantt  # noqa: E402

NOW = datetime(2026, 9, 26, 2, 10, tzinfo=timezone.utc)
H = timedelta(hours=1)
DAY = gantt.window(NOW, 6 * H, 18 * H)
WEEK = gantt.window(NOW, timedelta(days=2), timedelta(days=5))
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})


def row(*segs: gantt.Segment, ref: str = "#98", name: str = "Locale fallback", note: str = "merged 00:41",
        href: str = "", ref_title: str = "") -> gantt.Row:
    return gantt.Row(ref, name, note, segs, href=href, ref_title=ref_title)


def seg(a: float, b: float, category: str = "implement", kind: str = "done", title: str = "worker-01") -> gantt.Segment:
    return gantt.Segment(NOW + a * H, NOW + b * H, category, kind, title)


def bars(html: str) -> list[tuple[float, float]]:
    return [(float(l), float(w)) for l, w in re.findall(r'class="gantt-bar[^"]*"[^>]*style="left:([0-9.]+)%;width:([0-9.]+)%', html)]


class _A11yInspector(HTMLParser):
    """Inspect sequential keyboard tab stops and screen-reader-exposed nodes in rendered HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[dict] = []
        self.tab_stops: list[tuple[str, dict[str, str], str]] = []
        self.labelled: list[tuple[str, str | None, str]] = []
        self.prohibited_labels: list[tuple[str, dict[str, str]]] = []
        self.sr_text: list[str] = []

    def _hidden_from_sr(self) -> bool:
        return any(frame["aria_hidden"] or frame["tag"] in ("script", "style") for frame in self._stack)

    def _hidden_from_focus(self) -> bool:
        return any(frame["focus_hidden"] or frame["tag"] in ("script", "style") for frame in self._stack)

    @staticmethod
    def _is_focusable(tag: str, attrs: dict[str, str]) -> bool:
        if "disabled" in attrs:
            return False
        if "tabindex" in attrs:
            try:
                return int(attrs["tabindex"]) >= 0
            except ValueError:
                return False
        if tag in ("a", "area"):
            return "href" in attrs
        if tag == "input":
            return attrs.get("type", "").lower() != "hidden"
        return tag in ("button", "select", "textarea", "summary")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: ("" if v is None else v) for k, v in attrs}
        style = attr_map.get("style", "").replace(" ", "").lower()
        focus_hidden = "hidden" in attr_map or "display:none" in style or "visibility:hidden" in style
        aria_hidden = focus_hidden or attr_map.get("aria-hidden", "").lower() == "true"
        frame = {
            "tag": tag,
            "attrs": attr_map,
            "aria_hidden": aria_hidden,
            "focus_hidden": focus_hidden,
            "focusable": False,
            "text": [],
        }
        self._stack.append(frame)
        if not self._hidden_from_focus() and self._is_focusable(tag, attr_map):
            frame["focusable"] = True
        if not self._hidden_from_sr() and "aria-label" in attr_map:
            role = attr_map.get("role")
            if tag in ("span", "div") and not role:
                self.prohibited_labels.append((tag, attr_map))
            self.labelled.append((tag, role, attr_map["aria-label"]))
        if tag in VOID_TAGS:
            self._finish_top()

    def handle_data(self, data: str) -> None:
        for frame in self._stack:
            if not frame["focus_hidden"]:
                frame["text"].append(data)
        if not self._hidden_from_sr() and data.strip():
            self.sr_text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        while self._stack:
            top = self._finish_top()
            if top["tag"] == tag:
                break

    def _finish_top(self) -> dict:
        frame = self._stack.pop()
        if frame["focusable"]:
            text = "".join(frame["text"]).strip()
            self.tab_stops.append((frame["tag"], frame["attrs"], text))
        return frame


def inspect_a11y(html: str) -> _A11yInspector:
    inspector = _A11yInspector()
    inspector.feed(html)
    return inspector


class Scale(unittest.TestCase):
    def test_window_is_before_and_after_now(self) -> None:
        self.assertEqual((DAY.start, DAY.end, DAY.now), (NOW - 6 * H, NOW + 18 * H, NOW))

    def test_now_sits_a_quarter_in_on_the_day(self) -> None:
        self.assertAlmostEqual(gantt.position(NOW, DAY.start, DAY.end), 25.0)

    def test_now_sits_two_sevenths_in_on_the_week(self) -> None:
        self.assertAlmostEqual(gantt.position(NOW, WEEK.start, WEEK.end), 200 / 7)

    def test_position_clamps_to_the_window(self) -> None:
        self.assertEqual(gantt.position(NOW - 100 * H, DAY.start, DAY.end), 0.0)
        self.assertEqual(gantt.position(NOW + 100 * H, DAY.start, DAY.end), 100.0)

    def test_an_empty_window_does_not_divide_by_zero(self) -> None:
        self.assertEqual(gantt.position(NOW, NOW, NOW), 0.0)

    def test_place_floors_a_sliver(self) -> None:
        self.assertEqual(gantt.place(NOW, NOW, DAY.start, DAY.end, 0.5), "left:25.00%;width:0.50%")


class Ticks(unittest.TestCase):
    def test_a_day_ticks_every_two_hours(self) -> None:
        self.assertEqual(gantt.tick_step(24 * H), 2 * H)

    def test_a_week_ticks_every_day(self) -> None:
        self.assertEqual(gantt.tick_step(timedelta(days=7)), timedelta(days=1))

    def test_fewer_ticks_allowed_widens_the_step(self) -> None:
        self.assertEqual(gantt.tick_step(24 * H, max_ticks=9), 3 * H)

    def test_day_ticks_land_on_even_hours(self) -> None:
        got = gantt.ticks(DAY.start, DAY.end)
        self.assertEqual([label for _, label in got], ["22", "00", "02", "04", "06", "08", "10", "12", "14", "16", "18", "20"])

    def test_week_ticks_land_on_midnights(self) -> None:
        got = gantt.ticks(WEEK.start, WEEK.end)
        self.assertEqual([label for _, label in got], ["Fri 25", "Sat 26", "Sun 27", "Mon 28", "Tue 29", "Wed 30", "Thu 01"])
        self.assertTrue(all(at.hour == 0 for at, _ in got))

    def test_an_origin_anchors_the_ticks(self) -> None:
        got = gantt.ticks(DAY.start, DAY.end, 3 * H, "%H:%M", origin=DAY.start)
        self.assertEqual(got[0], (DAY.start, "20:10"))
        self.assertEqual(got[-1], (DAY.end, "20:10"))

    def test_explicit_ticks_are_drawn_as_given(self) -> None:
        html = gantt.render([row(seg(-1, 0))], DAY, ticks=[(NOW, "mid")])
        self.assertEqual(re.findall(r'<span class="gantt-tick"[^>]*>([^<]*)<', html), ["mid"])

    def test_each_tick_draws_a_gridline(self) -> None:
        html = gantt.render([row(seg(-1, 0))], DAY)
        self.assertEqual(html.count('class="gantt-grid"'), 12)


class Render(unittest.TestCase):
    def test_a_segment_is_clipped_to_the_window(self) -> None:
        html = gantt.render([row(seg(-9, -3), seg(12, 30))], DAY)
        self.assertEqual(bars(html), [(0.0, 12.5), (75.0, 25.0)])

    def test_a_segment_outside_the_window_is_dropped(self) -> None:
        html = gantt.render([row(seg(-9, -7), seg(-1, 0), seg(18, 20))], DAY)
        self.assertEqual(len(bars(html)), 1)

    def test_a_row_with_nothing_in_the_window_still_draws(self) -> None:
        html = gantt.render([row(seg(-9, -7))], DAY)
        self.assertIn('<span class="gantt-name">Locale fallback</span>', html)

    def test_kinds_become_classes(self) -> None:
        html = gantt.render([row(seg(-1, 0), seg(0, 1, kind="forecast"), seg(1, 2, kind="hold"))], DAY)
        self.assertEqual(re.findall(r'class="gantt-bar gantt-([a-z]+)"', html), ["done", "forecast", "hold"])

    def test_a_kind_that_is_not_a_class_name_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            gantt.render([row(seg(-1, 0, kind='x" onclick="y'))], DAY)

    def test_a_bar_is_not_a_tab_stop_and_carries_its_accessible_detail(self) -> None:
        html = gantt.render([row(seg(0, 1.5, "fix", "forecast", "worker-04"))], DAY)
        bar = re.search(r'<span class="gantt-bar gantt-forecast"[^>]*>', html).group(0)
        self.assertNotIn("tabindex", bar)
        self.assertIn('role="img"', bar)
        self.assertIn('data-cat="fix"', bar)
        label = re.search(r'aria-label="([^"]*)"', bar).group(1)
        self.assertEqual(label, "fix 02:10\u201303:40 \u00b7 worker-04 \u00b7 forecast")
        self.assertIn(f'title="{label}"', bar)

    def test_a_hold_bar_uses_needs_input_in_its_accessible_label(self):
        html = gantt.render([row(seg(0, 1, kind="hold"))], DAY)
        self.assertIn('aria-label="implement 02:10\u201303:10 \u00b7 worker-01 \u00b7 needs input"', html)
        self.assertIn('class="gantt-bar gantt-hold"', html)

    def test_the_week_names_the_day_in_a_bar_time(self) -> None:
        html = gantt.render([row(seg(0, 30))], WEEK)
        self.assertIn('aria-label="implement Sat 02:10\u2013Sun 08:10 \u00b7 worker-01 \u00b7 done"', html)

    def test_now_line_and_past_shade(self) -> None:
        html = gantt.render([row(seg(-1, 0))], DAY)
        self.assertIn('<span class="gantt-nowline" style="left:25.00%"></span>', html)
        self.assertIn('<span class="gantt-past" style="width:25.00%"></span>', html)
        self.assertIn('<span class="gantt-now" style="left:25.00%">now 02:10</span>', html)

    def test_the_week_now_sits_two_sevenths_in(self) -> None:
        html = gantt.render([row(seg(-1, 0))], WEEK)
        self.assertIn('<span class="gantt-nowline" style="left:28.57%"></span>', html)

    def test_the_figure_says_what_it_holds_in_one_sentence(self) -> None:
        html = gantt.render([row(seg(-1, 0)), row(seg(1, 2), ref="#99")], DAY)
        label = re.search(r'<figure class="gantt" aria-label="([^"]*)"', html).group(1)
        self.assertEqual(label, "2 rows, Fri 20:10 to Sat 20:10, now 02:10.")

    def test_text_is_escaped(self) -> None:
        html = gantt.render([row(seg(-1, 0, "<b>", title='"x"'), ref="<i>", name="a & b", note="<script>")], DAY)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<i>", html)
        self.assertNotIn("<b>", html)
        self.assertIn("a &amp; b", html)
        self.assertIn("&quot;x&quot;", html)

    def test_rows_keep_ref_name_and_note(self) -> None:
        html = gantt.render([row(seg(-1, 0))], DAY)
        self.assertIn('<span class="gantt-ref">#98</span><span class="gantt-name">Locale fallback</span>', html)
        self.assertIn('<span class="gantt-note">merged 00:41</span>', html)

    def test_position_is_the_only_inline_style(self) -> None:
        html = gantt.render([row(seg(-1, 0), seg(0, 1, kind="forecast"))], DAY)
        styles = re.findall(r'style="([^"]*)"', html)
        self.assertTrue(styles)
        self.assertTrue(all(re.fullmatch(r"(left:[0-9.]+%;?)?(width:[0-9.]+%)?", s) for s in styles), styles)


class Accessibility(unittest.TestCase):
    """#984: static chart bars are never keyboard stops; screen readers keep chart details and real controls stay focusable."""

    def test_bars_and_legend_swatches_never_create_keyboard_tab_stops(self) -> None:
        many_bars = [
            seg(-5 + i * 0.2, -4.8 + i * 0.2, cat, kind, f"worker-{i}")
            for i, (cat, kind) in enumerate(
                [("implement", "done"), ("review", "done"), ("fix", "failed"), ("fix", "done"),
                 ("verify", "running"), ("triage", "hold"), ("merge", "forecast")] * 6
            )
        ]
        html = gantt.render([row(*many_bars)] + [row(seg(-2, -1), ref=f"#{100 + i}") for i in range(10)], DAY)
        legend_html = gantt.legend()
        self.assertNotIn("tabindex", html)
        self.assertNotIn("tabindex", legend_html)
        self.assertEqual([(tag, text) for tag, _, text in inspect_a11y(html).tab_stops], [("summary", "Show segment details")])
        self.assertEqual(inspect_a11y(legend_html).tab_stops, [])

    def test_tab_stops_match_actual_links_and_do_not_grow_with_attempts(self) -> None:
        one_attempt = [
            row(seg(-2, 0), ref="#98", name="Locale fallback", note="merged 00:41", href="/issues/98"),
            row(seg(-1, 0), ref="workflow", name="Verify nightly", note="running", href=""),
            row(seg(-3, -1), ref="#109", name="Upload size limit", note="pr", href="/issues/109"),
        ]
        twenty_attempts = [
            row(*(seg(-5 + i * 0.2, -4.85 + i * 0.2, "fix" if i % 2 else "implement",
                      "failed" if i % 3 == 0 else "done", f"w-{i}") for i in range(20)),
                ref="#98", name="Locale fallback", note="merged 00:41", href="/issues/98"),
            row(*(seg(-4 + i * 0.2, -3.85 + i * 0.2, "review", "done", f"r-{i}") for i in range(15)),
                ref="workflow", name="Verify nightly", note="running", href=""),
            row(*(seg(-3 + i * 0.2, -2.85 + i * 0.2, "implement", "done", f"u-{i}") for i in range(25)),
                seg(0, 4, "triage", "hold", "Robin"),
                ref="#109", name="Upload size limit", note="needs input \u00b7 decision", href="/issues/109"),
        ]
        stops_one = inspect_a11y(gantt.render(one_attempt, DAY)).tab_stops
        stops_many = inspect_a11y(gantt.render(twenty_attempts, DAY)).tab_stops
        self.assertEqual([(tag, attrs.get("href"), text) for tag, attrs, text in stops_one], [
            ("a", "/issues/98", "#98"),
            ("a", "/issues/109", "#109"),
            ("summary", None, "Show segment details"),
        ])
        self.assertEqual([(tag, attrs.get("href"), text) for tag, attrs, text in stops_many], [
            ("a", "/issues/98", "#98"),
            ("a", "/issues/109", "#109"),
            ("summary", None, "Show segment details"),
        ])

    def test_a_linked_row_without_a_ref_links_its_name_so_every_tab_stop_is_named(self) -> None:
        html = gantt.render([row(seg(-1, 0), ref="", name="Locale fallback", href="/tasks/locale")], DAY)
        stops = inspect_a11y(html).tab_stops
        self.assertEqual([(tag, attrs.get("href"), text) for tag, attrs, text in stops], [
            ("a", "/tasks/locale", "Locale fallback"),
            ("summary", None, "Show segment details"),
        ])

    def test_issue_links_remain_visible_at_phone_width_and_linked_names_are_marked(self) -> None:
        import flow_chart
        phone = flow_chart.css().split("@media (max-width:520px)", 1)[1]
        self.assertNotIn(".gantt-note,.gantt-ref{display:none}", phone)
        self.assertIn("a.gantt-name{text-decoration:underline", gantt.css())
        html = gantt.render([row(seg(-1, 0), ref="#98", href="/issues/98")], DAY)
        self.assertIn('class="gantt-ref"', html)
        self.assertIn('href="/issues/98"', html)

    def test_disclosure_groups_visible_segments_by_row_with_full_details(self) -> None:
        html = gantt.render([
            row(seg(-9, -7, title="outside"), seg(-9, -3, "fix", "failed", 'worker <one>'),
                seg(0, 1, "triage", "hold", "Robin"), ref="#105", name="Audit & review", note="waiting"),
            row(seg(18, 20, title="outside too"), ref="#106", name="Empty"),
        ], DAY)
        details = re.search(r'<details class="gantt-details">(.*?)</details>', html, re.S).group(1)
        self.assertIn('<summary>Show segment details</summary>', details)
        self.assertIn('<dt>#105 Audit &amp; review</dt>', details)
        self.assertIn('<p>waiting</p>', details)
        self.assertIn('<li>fix Fri 17:10–Fri 23:10 · worker &lt;one&gt; · failed</li>', details)
        self.assertIn('<li>triage 02:10–03:10 · Robin · needs input</li>', details)
        self.assertIn('<dt>#106 Empty</dt>', details)
        self.assertIn('No segments in this window', details)
        self.assertNotIn('outside', details)
        self.assertNotIn('tabindex', details)
        labels = [unescape(label) for label in re.findall(r'<span class="gantt-bar[^"]*"[^>]*aria-label="([^"]*)"', html)]
        items = [unescape(item) for item in re.findall(r'<li>(.*?)</li>', details)]
        self.assertEqual(items, labels)

    def test_disclosure_text_can_wrap_and_summary_has_visible_focus(self) -> None:
        sheet = gantt.css()
        self.assertIn('.gantt-details', sheet)
        self.assertIn('overflow-wrap:anywhere', sheet)
        self.assertIn('.gantt summary:focus-visible', sheet)

    def test_css_styles_focus_on_interactive_links_not_non_interactive_bars(self) -> None:
        sheet = gantt.css()
        self.assertIn(".gantt a:focus-visible{outline:2px solid var(--gantt-ink);outline-offset:1px}", sheet)
        self.assertNotIn(".gantt-bar:focus-visible", sheet)

    def test_screen_readers_reach_chart_summary_rows_and_segment_details_without_axis_noise(self) -> None:
        html = gantt.render([
            row(
                seg(-3, -2, "implement", "failed", "worker-01"),
                seg(-2, -1, "fix", "done", "worker-02"),
                seg(0, 2, "triage", "hold", "Robin"),
                ref="#105", name="Audit log retention", note="needs input \u00b7 decision", href="/issues/105",
            ),
            row(
                seg(2, 5, "review", "forecast", "reviewer-01"),
                ref="#112", name="Export format", note="queued \u00b7 ~07:10",
            ),
        ], DAY) + "\n" + gantt.legend()
        a11y = inspect_a11y(html)
        self.assertEqual(a11y.prohibited_labels, [])
        self.assertEqual(a11y.labelled, [
            ("figure", None, "2 rows, Fri 20:10 to Sat 20:10, now 02:10."),
            ("span", "img", "implement Fri 23:10\u201300:10 \u00b7 worker-01 \u00b7 failed"),
            ("span", "img", "fix 00:10\u201301:10 \u00b7 worker-02 \u00b7 done"),
            ("span", "img", "triage 02:10\u201304:10 \u00b7 Robin \u00b7 needs input"),
            ("span", "img", "review 04:10\u201307:10 \u00b7 reviewer-01 \u00b7 forecast"),
        ])
        for row_text in ("#105", "Audit log retention", "needs input \u00b7 decision",
                         "#112", "Export format", "queued \u00b7 ~07:10",
                         "implement", "forecast", "needs input", "failed"):
            with self.subTest(text=row_text):
                self.assertIn(row_text, a11y.sr_text)
        for decorative_tick in ("22", "00", "02", "04", "now 02:10"):
            with self.subTest(tick=decorative_tick):
                self.assertNotIn(decorative_tick, a11y.sr_text)
        for swatch in re.findall(r'<span class="gantt-bar[^"]*"[^>]*>', gantt.legend()):
            self.assertIn('aria-hidden="true"', swatch)

    def test_rendered_board_flow_keeps_zero_bar_tab_stops_while_preserving_issue_links_and_bar_labels(self) -> None:
        import render_board as rb
        import settings as st
        from workspace import parse_coordinator

        ct = ZoneInfo("America/Chicago")
        now = datetime(2026, 9, 26, 2, 10, tzinfo=ct)
        today, yesterday = date(2026, 9, 26), date(2026, 9, 25)
        cfg = parse_coordinator(
            "# W\n\n## Coordinator\n\n- User: Robin\n- Daily log dir: `daily/`\n"
            "- Tracker: `daily/<date>-tracker.md`\n- Timezone: America/Chicago\n",
            today=today,
        )
        lanes = {"build": st.Lane(("implement", "pr", "review", "triage", "fix", "merge", "main"), ("triage", "merge"))}
        kanban = st.Kanban(("todo", "doing", "decide"), "needs-review",
                           {"implement": 0, "pr": 1, "review": 1, "triage": 2, "fix": 1, "merge": 2, "main": 1},
                           hold_stages=("triage",))
        base_tracker = (
            "# Tracker\n\n## Tasks\n\n"
            "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n"
            "|---|---|---|---|---|---|---|---|---|---|---|\n"
            "| Upload size limit | Cap uploads | impl-1 | running 01:00 | 2026-09-25 | 06:10 | M | build | pr | #109 | c |\n"
            "| Cache warmup | Warm the pool | Robin | waiting | 2026-09-26 |  | S | build | triage | #111 | c |\n"
            "| Locale fallback | Fall back to en | Robin | done 00:41 | 2026-09-26 |  | S | build | main | #98 | c |\n\n"
            "## Log\n\n"
            "- 00:10 stage: Locale fallback \u2192 merge\n"
            "- 00:41 stage: Locale fallback \u2192 main\n"
            "- 01:00 stage: Upload size limit \u2192 pr\n"
            "- 01:40 stage: Cache warmup \u2192 triage\n"
        )
        yesterday_tracker = (
            "# Tracker\n\n## Log\n\n"
            "- 22:00 stage: Upload size limit \u2192 implement\n"
            "- 23:00 stage: Cache warmup \u2192 review\n"
        )
        extra_attempts = "\n".join(
            f"- 01:{10 + i:02d} stage: Upload size limit \u2192 {'fix' if i % 2 else 'review'}"
            for i in range(20)
        )
        board_base = rb.render(base_tracker, cfg, now, lanes=lanes, tracker_day=today, kanban=kanban,
                               stage_log=[(yesterday, yesterday_tracker)])
        board_many = rb.render(base_tracker + extra_attempts + "\n", cfg, now, lanes=lanes, tracker_day=today,
                               kanban=kanban, stage_log=[(yesterday, yesterday_tracker)])

        for page in (board_base, board_many):
            self.assertNotIn('tabindex="0"', page)
            page_a11y = inspect_a11y(page)
            self.assertEqual(page_a11y.prohibited_labels, [])
            self.assertTrue(all(text or attrs.get("aria-label") for _, attrs, text in page_a11y.tab_stops))

        flow_base = inspect_a11y(board_base[board_base.index("<h2>Flow \u00b7"):])
        flow_many = inspect_a11y(board_many[board_many.index("<h2>Flow \u00b7"):])
        self.assertEqual(
            [(tag, attrs.get("href"), text) for tag, attrs, text in flow_base.tab_stops],
            [("a", "/issues/98", "#98"), ("a", "/issues/109", "#109"), ("a", "/issues/111", "#111"),
             ("summary", None, "Show segment details")] * 2,
        )
        self.assertEqual(flow_many.tab_stops, flow_base.tab_stops)
        self.assertGreater(len(flow_many.labelled), len(flow_base.labelled) + 30)


class BarWeekdayTest(unittest.TestCase):
    """#458: each off-day endpoint names its weekday, even when the chart clips the bar."""

    def assert_bar_label(self, html: str, expected: str) -> None:
        rendered = re.findall(r'<span class="gantt-bar[^"]*"[^>]*>', html)
        self.assertEqual(len(rendered), 1)
        for attribute in ("title", "aria-label"):
            with self.subTest(attribute=attribute):
                self.assertEqual(re.search(fr'{attribute}="([^"]*)"', rendered[0])[1], expected)

    def test_a_clipped_yesterday_start_names_its_weekday_and_original_time(self) -> None:
        start = NOW - 14 * H
        html = gantt.render([row(seg(-14, 0, "pr"))], DAY)
        self.assertEqual(bars(html), [(0.0, 25.0)])
        self.assert_bar_label(html, f"pr {start:%a %H:%M}\u2013{NOW:%H:%M} \u00b7 worker-01 \u00b7 done")

    def test_a_clipped_tomorrow_end_names_its_weekday_and_original_time(self) -> None:
        end = NOW + 30 * H
        html = gantt.render([row(seg(0, 30, "review", "forecast"))], DAY)
        self.assertEqual(bars(html), [(25.0, 75.0)])
        self.assert_bar_label(html, f"review {NOW:%H:%M}\u2013{end:%a %H:%M} \u00b7 worker-01 \u00b7 forecast")

    def test_both_endpoints_on_yesterday_name_the_day_in_a_short_window(self) -> None:
        start, end = NOW - 5 * H, NOW - 3 * H
        win = gantt.window(NOW, 6 * H, 6 * H)
        html = gantt.render([row(seg(-5, -3))], win)
        self.assert_bar_label(html, f"implement {start:%a %H:%M}\u2013{end:%a %H:%M} \u00b7 worker-01 \u00b7 done")

    def test_same_day_endpoints_keep_clock_only_titles(self) -> None:
        for win in (DAY, gantt.window(NOW, H, 3 * H)):
            with self.subTest(span=win.end - win.start):
                html = gantt.render([row(seg(0, 1))], win)
                self.assert_bar_label(html, f"implement {NOW:%H:%M}\u2013{NOW + H:%H:%M} \u00b7 worker-01 \u00b7 done")

    def test_a_clipped_start_on_the_same_day_number_in_another_month_names_its_weekday(self) -> None:
        # PR 469 R2: comparing day numbers alone mistakes a previous month for today.
        now = NOW.replace(day=1)
        start = now.replace(month=now.month - 1)
        win = gantt.window(now, 6 * H, 18 * H)
        segment = gantt.Segment(start, now, "implement", "done", "worker-01")
        html = gantt.render([row(segment)], win)
        self.assertEqual(bars(html), [(0.0, 25.0)])
        self.assert_bar_label(html, f"implement {start:%a %H:%M}\u2013{now:%H:%M} \u00b7 worker-01 \u00b7 done")

    def test_week_window_keeps_weekdays_even_for_same_day_endpoints(self) -> None:
        html = gantt.render([row(seg(0, 1))], WEEK)
        self.assert_bar_label(html, f"implement {NOW:%a %H:%M}\u2013{NOW + H:%a %H:%M} \u00b7 worker-01 \u00b7 done")


class Palette(unittest.TestCase):
    def test_the_default_palette_is_okabe_ito(self) -> None:
        self.assertEqual(gantt.PALETTE, {
            "implement": "#0072B2", "pr": "#56B4E9", "review": "#009E73", "triage": "#E69F00",
            "fix": "#D55E00", "verify": "#CC79A7", "merge": "#000000",
        })

    def test_the_css_colours_each_category_by_attribute(self) -> None:
        self.assertIn('[data-cat="implement"]{--c:#0072B2;--e:#0072B2}', gantt.css())

    def test_an_edge_colour_is_its_own(self) -> None:
        self.assertIn('[data-cat="pale"]{--c:#F0E442;--e:#8a7a1a}', gantt.css({"pale": ("#F0E442", "#8a7a1a")}))

    def test_a_custom_palette_colours_its_own_categories(self) -> None:
        sheet = gantt.css({"alpha": "#123456"})
        self.assertIn('[data-cat="alpha"]{--c:#123456;--e:#123456}', sheet)
        self.assertNotIn("implement", sheet)

    def test_a_category_cannot_close_the_style_element(self) -> None:
        sheet = gantt.css({'a"</style><script>': "#123456"})
        self.assertNotIn("</style>", sheet)
        self.assertNotIn('a"<', sheet)

    def test_a_colour_that_is_not_a_colour_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            gantt.css({"alpha": "red;}body{display:none"})

    def test_css_styles_every_kind(self) -> None:
        for kind in ("done", "forecast", "hold", "failed"):
            self.assertIn(f".gantt-bar.gantt-{kind}", gantt.css())

    def test_failed_bars_do_not_look_like_needs_input_bars(self) -> None:
        rule = lambda kind: re.search(rf"\.gantt-bar\.gantt-{kind}\{{([^}}]*)\}}", gantt.css())[1]  # noqa: E731
        self.assertNotEqual(rule("failed"), rule("hold"))

    def test_the_legend_lists_categories_then_kinds(self) -> None:
        html = gantt.legend({"alpha": "#123456", "beta": "#654321"})
        self.assertEqual(re.findall(r"</span>([^<]+)</span>", html),
                         ["alpha", "beta", "forecast", "needs input", "failed"])
        self.assertIn('data-cat="alpha"', html)
        self.assertIn('class="gantt-bar gantt-hold"', html)


class Standalone(unittest.TestCase):
    """gantt.py and columns.py lift out with fragment.py, their shared escaping and validation, and nothing else."""

    def imports(self, name: str) -> set[str]:
        src = (SCRIPTS / f"{name}.py").read_text()
        plugin = {p.stem for p in SCRIPTS.glob("*.py")} | {"_vendor", "scripts"}
        return {m.split(".")[0] for m in re.findall(r"^(?:from|import) ([\w.]+)", src, re.M)} & plugin

    def test_imports_no_plugin_module_but_fragment(self) -> None:
        for name in ("gantt", "columns"):
            self.assertEqual(self.imports(name), {"fragment"}, name)

    def test_fragment_imports_no_plugin_module(self) -> None:
        self.assertEqual(self.imports("fragment"), set())


if __name__ == "__main__":
    unittest.main()
