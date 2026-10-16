"""The Gantt module scales, clips, ticks and renders rows without knowing what the rows are."""

from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import gantt  # noqa: E402

NOW = datetime(2026, 9, 26, 2, 10, tzinfo=timezone.utc)
H = timedelta(hours=1)
DAY = gantt.window(NOW, 6 * H, 18 * H)
WEEK = gantt.window(NOW, timedelta(days=2), timedelta(days=5))


def row(*segs: gantt.Segment, ref: str = "#98", name: str = "Locale fallback", note: str = "merged 00:41") -> gantt.Row:
    return gantt.Row(ref, name, note, segs)


def seg(a: float, b: float, category: str = "implement", kind: str = "done", title: str = "worker-01") -> gantt.Segment:
    return gantt.Segment(NOW + a * H, NOW + b * H, category, kind, title)


def bars(html: str) -> list[tuple[float, float]]:
    return [(float(l), float(w)) for l, w in re.findall(r'class="gantt-bar[^"]*"[^>]*style="left:([0-9.]+)%;width:([0-9.]+)%', html)]


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

    def test_a_bar_is_focusable_and_carries_its_detail(self) -> None:
        html = gantt.render([row(seg(0, 1.5, "fix", "forecast", "worker-04"))], DAY)
        bar = re.search(r'<span class="gantt-bar gantt-forecast"[^>]*>', html).group(0)
        self.assertIn('tabindex="0"', bar)
        self.assertIn('data-cat="fix"', bar)
        label = re.search(r'aria-label="([^"]*)"', bar).group(1)
        self.assertEqual(label, "fix 02:10–03:40 · worker-04 · forecast")
        self.assertIn(f'title="{label}"', bar)

    def test_the_week_names_the_day_in_a_bar_time(self) -> None:
        html = gantt.render([row(seg(0, 30))], WEEK)
        self.assertIn('aria-label="implement Sat 02:10–Sun 08:10 · worker-01 · done"', html)

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
        self.assert_bar_label(html, f"pr {start:%a %H:%M}–{NOW:%H:%M} · worker-01 · done")

    def test_a_clipped_tomorrow_end_names_its_weekday_and_original_time(self) -> None:
        end = NOW + 30 * H
        html = gantt.render([row(seg(0, 30, "review", "forecast"))], DAY)
        self.assertEqual(bars(html), [(25.0, 75.0)])
        self.assert_bar_label(html, f"review {NOW:%H:%M}–{end:%a %H:%M} · worker-01 · forecast")

    def test_both_endpoints_on_yesterday_name_the_day_in_a_short_window(self) -> None:
        start, end = NOW - 5 * H, NOW - 3 * H
        win = gantt.window(NOW, 6 * H, 6 * H)
        html = gantt.render([row(seg(-5, -3))], win)
        self.assert_bar_label(html, f"implement {start:%a %H:%M}–{end:%a %H:%M} · worker-01 · done")

    def test_same_day_endpoints_keep_clock_only_titles(self) -> None:
        for win in (DAY, gantt.window(NOW, H, 3 * H)):
            with self.subTest(span=win.end - win.start):
                html = gantt.render([row(seg(0, 1))], win)
                self.assert_bar_label(html, f"implement {NOW:%H:%M}–{NOW + H:%H:%M} · worker-01 · done")

    def test_a_clipped_start_on_the_same_day_number_in_another_month_names_its_weekday(self) -> None:
        # PR 469 R2: comparing day numbers alone mistakes a previous month for today.
        now = NOW.replace(day=1)
        start = now.replace(month=now.month - 1)
        win = gantt.window(now, 6 * H, 18 * H)
        segment = gantt.Segment(start, now, "implement", "done", "worker-01")
        html = gantt.render([row(segment)], win)
        self.assertEqual(bars(html), [(0.0, 25.0)])
        self.assert_bar_label(html, f"implement {start:%a %H:%M}–{now:%H:%M} · worker-01 · done")

    def test_week_window_keeps_weekdays_even_for_same_day_endpoints(self) -> None:
        html = gantt.render([row(seg(0, 1))], WEEK)
        self.assert_bar_label(html, f"implement {NOW:%a %H:%M}–{NOW + H:%a %H:%M} · worker-01 · done")


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
        for kind in ("done", "forecast", "hold"):
            self.assertIn(f".gantt-bar.gantt-{kind}", gantt.css())

    def test_the_legend_lists_categories_then_kinds(self) -> None:
        html = gantt.legend({"alpha": "#123456", "beta": "#654321"})
        self.assertEqual(re.findall(r"</span>([^<]+)</span>", html), ["alpha", "beta", "forecast", "needs input"])
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
