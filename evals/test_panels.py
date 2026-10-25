"""The Flow board's header, tiles and panels render from generic rows without knowing what a tile or a row means."""

from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import panels  # noqa: E402

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 26, 2, 10, 37, tzinfo=CT)


def _due(name: str, at: datetime, now: datetime = NOW) -> str:
    return panels.header(panels.Header("B", (), now, name, at))


class Header(unittest.TestCase):
    def setUp(self) -> None:
        self.html = panels.header(panels.Header("Board · Sat 26 Sep", ("rendered 02:10 CDT", "tracker 02:05", "38 tasks"),
                                                NOW, "demo", datetime(2026, 9, 26, 11, 0, tzinfo=CT),
                                                warnings=("merge requests: token expired",)))

    def test_title_and_the_meta_line_joined_with_dots(self) -> None:
        self.assertIn("<h1>Board · Sat 26 Sep</h1>", self.html)
        self.assertIn("rendered 02:10 CDT · tracker 02:05 · 38 tasks", self.html)

    def test_now_and_the_countdown_to_the_deadline(self) -> None:
        self.assertRegex(self.html, r'class="panels-now"[^>]*datetime="2026-09-26T02:10:37-05:00"[^>]*>02:10:37<')
        self.assertIn('<span class="panels-clock-name">demo in</span>', self.html)
        self.assertRegex(self.html, r'class="panels-left"[^>]*datetime="2026-09-26T11:00:00-05:00"[^>]*>8h49m23s<')

    def test_a_past_deadline_says_passed(self) -> None:
        html = _due("x", datetime(2026, 9, 26, 1, 40, 37, tzinfo=CT))
        self.assertIn('<span class="panels-clock-name">x passed</span>', html)
        self.assertRegex(html, r'class="panels-left"[^>]*datetime="2026-09-26T01:40:37-05:00"[^>]*>0h30m00s ago<')
        self.assertNotIn("x in", html)
        self.assertNotIn("-0h", html)

    def test_an_exact_deadline_is_zero_and_still_ahead(self) -> None:
        html = _due("demo", NOW)
        self.assertIn('<span class="panels-clock-name">demo in</span>', html)
        self.assertRegex(html, r'class="panels-left"[^>]*>0h00m00s<')
        self.assertNotIn("ago", html)

    def test_a_subsecond_expiry_says_passed_without_a_negative_zero(self) -> None:
        now = datetime(2026, 9, 26, 2, 10, 37, 400_000, tzinfo=CT)
        html = _due("demo", datetime(2026, 9, 26, 2, 10, 37, tzinfo=CT), now)
        self.assertIn('<span class="panels-clock-name">demo passed</span>', html)
        self.assertRegex(html, r'class="panels-left"[^>]*>0h00m00s ago<')
        self.assertNotIn("-0h", html)
        self.assertNotIn("demo in", html)

    def test_hours_may_pass_24(self) -> None:
        html = _due("demo", NOW - timedelta(hours=25, minutes=3, seconds=4))
        self.assertIn('<span class="panels-clock-name">demo passed</span>', html)
        self.assertRegex(html, r'class="panels-left"[^>]*>25h03m04s ago<')

    def test_a_name_that_needs_escaping_stays_text_after_it_passes(self) -> None:
        html = _due("<x>", NOW - timedelta(seconds=5))
        self.assertIn('<span class="panels-clock-name">&lt;x&gt; passed</span>', html)
        self.assertRegex(html, r'class="panels-left"[^>]*>0h00m05s ago<')
        self.assertNotIn("<x>", html)

    def test_the_final_minute_after_2359_says_passed(self) -> None:
        day = datetime(2026, 10, 18, 23, 59, tzinfo=CT)
        at_cutoff = _due("end of day", day, day)
        self.assertIn('<span class="panels-clock-name">end of day in</span>', at_cutoff)
        self.assertRegex(at_cutoff, r'class="panels-left"[^>]*>0h00m00s<')
        after = _due("end of day", day, day.replace(second=40))
        self.assertIn('<span class="panels-clock-name">end of day passed</span>', after)
        self.assertRegex(after, r'class="panels-left"[^>]*>0h00m40s ago<')

    def test_elapsed_time_follows_the_utc_instant_across_an_offset_change(self) -> None:
        # Wall subtraction says 2h across the spring gap and 0h across the fall overlap.
        # The clock counts the UTC instant, which is 1h either way.
        spring_now = datetime(2026, 3, 8, 1, 30, tzinfo=CT)
        spring = _due("gap", datetime(2026, 3, 8, 3, 30, tzinfo=CT), spring_now)
        self.assertIn('<span class="panels-clock-name">gap in</span>', spring)
        self.assertRegex(spring, r'class="panels-left"[^>]*>1h00m00s<')
        self.assertRegex(spring, r'class="panels-now"[^>]*>01:30:00<')
        earlier = datetime(2026, 11, 1, 1, 30, tzinfo=CT)
        later = earlier.replace(fold=1)
        fall = _due("overlap", earlier, later)
        self.assertIn('<span class="panels-clock-name">overlap passed</span>', fall)
        self.assertRegex(fall, r'class="panels-left"[^>]*>1h00m00s ago<')
        self.assertRegex(fall, r'class="panels-now"[^>]*>01:30:00<')

    def test_warnings_show_and_escape(self) -> None:
        self.assertIn('<div class="panels-warn">merge requests: token expired</div>', self.html)
        html = panels.header(panels.Header("<b>", ("<i>",), NOW, "<x>", NOW))
        self.assertNotIn("<b>", html)
        self.assertNotIn("<i>", html)
        self.assertNotIn("<x>", html)


class Tiles(unittest.TestCase):
    def test_one_anchor_per_tile_with_its_label_and_count(self) -> None:
        html = panels.tiles([panels.Tile("BLOCKED", 3, "blocked", "blocked"), panels.Tile("RUNNING", 4, "workers", "running")])
        self.assertEqual(re.findall(r'<a class="panels-tile" data-kind="([a-z-]+)" href="#([a-z-]+)">', html),
                         [("blocked", "blocked"), ("running", "workers")])
        self.assertEqual(re.findall(r'<b class="panels-count">(\d+)</b>', html), ["3", "4"])
        self.assertIn('<span class="panels-label">BLOCKED</span>', html)

    def test_a_kind_or_anchor_must_be_a_name(self) -> None:
        with self.assertRaises(ValueError):
            panels.tiles([panels.Tile("X", 1, 'a"b', "x")])
        with self.assertRaises(ValueError):
            panels.tiles([panels.Tile("X", 1, "x", "x y")])


class Panel(unittest.TestCase):
    def test_head_rows_link_and_foot(self) -> None:
        p = panels.Panel("merge", "MERGE ORDER", "merge", 2, (
            panels.Row("Password reset copy", facts="approved · pipeline passed", num="1", ref="!41", href="https://x/41"),
            panels.Row("Upload size limit", facts="in review", num="2", ref="!48"),
        ), note="1 approved · 1 in review", link=("decisions page", "decisions.html"), foot="4 answered today")
        html = panels.panels([p])
        self.assertIn('<section class="panels-panel" id="merge" data-kind="merge">', html)
        self.assertIn('<h3 class="panels-title">MERGE ORDER</h3>', html)
        self.assertIn('<span class="panels-note">1 approved · 1 in review</span>', html)
        self.assertIn('<a class="panels-link" href="decisions.html">decisions page</a>', html)
        self.assertIn('<b class="panels-count">2</b>', html)
        self.assertIn('<span class="panels-num">1</span><a class="panels-ref" href="https://x/41" target="_blank" rel="noopener">!41</a>', html)
        self.assertIn('<span class="panels-ref">!48</span>', html)
        self.assertIn('<span class="panels-facts">approved · pipeline passed</span>', html)
        self.assertIn('<div class="panels-foot">4 answered today</div>', html)

    def test_a_row_name_links_when_it_has_an_href_and_no_ref(self) -> None:
        html = panels.panels([panels.Panel("decisions", "D", "decisions", 1, (panels.Row("Cache warmup?", href="decision-cache.html"),))])
        self.assertIn('<a class="panels-name" href="decision-cache.html">Cache warmup?</a>', html)

    def test_a_side_value_sits_right_of_the_name(self) -> None:
        html = panels.panels([panels.Panel("workers", "W", "workers", 1, (panels.Row("impl-07", side="0h42m", facts="one-shot"),))])
        self.assertIn('<span class="panels-name">impl-07</span><span class="panels-side">0h42m</span>', html)

    def test_an_empty_panel_says_none(self) -> None:
        html = panels.panels([panels.Panel("workers", "W", "workers", 0, ())])
        self.assertIn('<div class="panels-row panels-empty">none</div>', html)

    def test_panels_share_one_row(self) -> None:
        html = panels.panels([panels.Panel(k, k, k, 0, ()) for k in ("a", "b", "c")])
        self.assertTrue(html.startswith('<div class="panels">'))
        self.assertEqual(html.count('<section class="panels-panel"'), 3)

    def test_text_is_escaped(self) -> None:
        html = panels.panels([panels.Panel("m", "<t>", "m", 1, (panels.Row("<n>", facts="<f>", ref="<r>", href='"><x'),))])
        for raw in ("<t>", "<n>", "<f>", "<r>", '"><x'):
            self.assertNotIn(raw, html)


class Css(unittest.TestCase):
    def test_one_rule_per_kind_from_the_palette(self) -> None:
        css = panels.css({"merge": "var(--stage-review)", "hot": "#c9533a"})
        self.assertIn('[data-kind="merge"]{--k:var(--stage-review)}', css)
        self.assertIn('[data-kind="hot"]{--k:#c9533a}', css)

    def test_a_value_cannot_escape_its_declaration(self) -> None:
        with self.assertRaises(ValueError):
            panels.css({"x": "red;}body{display:none"})

    def test_the_default_palette_covers_the_six_tiles_and_three_panels(self) -> None:
        self.assertLessEqual({"blocked", "decisions", "approved", "running", "drift", "orphaned", "merge", "workers"},
                             set(panels.PALETTE))

    def test_the_layout_wraps_on_a_phone(self) -> None:
        css = panels.css()
        self.assertRegex(css, r"\.panels-tiles\{[^}]*grid-template-columns:repeat\(auto-fit,minmax\(")
        self.assertRegex(css, r"\.panels\{[^}]*grid-template-columns:repeat\(auto-fit,minmax\(")
        self.assertRegex(css, r"\.panels-head\{[^}]*flex-wrap:wrap")


if __name__ == "__main__":
    unittest.main()
