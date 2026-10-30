"""Issue #160: every page's external links open in a new tab; its in-app links stay in the tab."""

import sys
import unittest
from dataclasses import replace
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent.parent / "scripts"), str(Path(__file__).resolve().parent)]
import decision_page as dp  # noqa: E402
import render_board as rb  # noqa: E402
from workspace import parse_coordinator  # noqa: E402
import test_decision_page as tdp  # noqa: E402
import test_flow_board as tfb  # noqa: E402
import test_issue_page as tip  # noqa: E402
from test_render_board import CLAUDE_MD, NOW  # noqa: E402
import test_source_page as tsp  # noqa: E402
from test_source_notes import OPEN_ADD  # noqa: E402


class Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a" and dict(attrs).get("href") is not None:
            self.found.append(dict(attrs))


def check_links(case: unittest.TestCase, html: str) -> None:
    """Issue #160: "External links open in a new tab (target="_blank" rel="noopener"); in-app routes stay in the tab."
    External is an absolute http(s):// href; in-app is a route (`/`, `/decisions`, `/decisions/<slug>`, `/issues/<n>`,
    `/issues/<n>/source`) or a same-page `#anchor`."""
    parser = Anchors()
    parser.feed(html)
    external = [a for a in parser.found if a["href"].lower().startswith(("http://", "https://"))]
    in_app = [a for a in parser.found if a["href"].startswith("#") or
              (a["href"].startswith("/") and not a["href"].startswith("//"))]
    case.assertTrue(external and in_app, "the fixture page needs an external and an in-app link")
    wrong = [f'{a["href"]} (target={a.get("target")!r}, rel={a.get("rel")!r})' for a in external
             if a.get("target") != "_blank" or "noopener" not in (a.get("rel") or "").split()]
    wrong += [f'{a["href"]} (in-app, target={a["target"]!r})' for a in in_app if "target" in a]
    case.assertEqual(wrong, [], f"{len(wrong)} of {len(external) + len(in_app)} links break the rule")


class ExternalLinksTest(tsp.SourcePage):
    def test_the_board(self):
        # #208: panels no longer supply external links; a merged card with no issue links to its forge page.
        sources = replace(tfb.SOURCES, changes={**tfb.SOURCES.changes,
                          "!90": tfb.change("!90", state="merged", merged_at=NOW)})
        html = rb.render(tfb.TRACKER, parse_coordinator(tfb.CLAUDE_MD, today=NOW.date()), NOW, lanes=tfb.LANES,
                         kanban=tfb.KANBAN, sources=sources,
                         decisions=[("x", {"headline": "Pick", "recommended": "B", "default": "d"},
                                     NOW.replace(hour=10, minute=0), "")],
                         tracker_at=NOW - timedelta(minutes=5))
        check_links(self, html)

    def test_the_decisions_list(self):
        warm = tdp.decision(topic="cache", headline="Cache warmup", holds=["#111"],
                            references=[{"kind": "MR", "value": "!58"}])
        ctx = dp.Context(tdp.NOW, log=((tdp.at("01:41"), "asked decision-cache-warmup"),),
                         tasks=(tdp.task("Budget", "Connection budget", "triage", "#111"),
                                tdp.task("Pool", "Warm pool !58", "merge")),
                         sources=tdp.SOURCES, hold="status::hold")
        check_links(self, dp.index(tdp.pages_dir(**{"cache-warmup": warm}), ctx, "2026-09-26")[0])

    def test_the_decision_page(self):
        check_links(self, dp.render(dp.parse(tdp.decision()), "2026-09-25", slug="uploads"))

    def test_the_issue_page(self):
        check_links(self, tip.page())

    def test_the_source_page(self):
        # The source page's external link is a review thread's (test_source_notes).
        check_links(self, tsp.sp.render(109, tsp.TRACKERS, self.sources(), tsp.NOW, self.graph, self.root / "pages",
                                        self.root, notes=[tsp.sp.Note(tsp.LIMITS, 7, "new", OPEN_ADD)]))


class MalformedLinksTest(unittest.TestCase):
    def test_malformed_url_in_shared_prose_does_not_break_rendering(self):
        from fragment import external_url, href
        from page_common import markdown_inline, markdown_blocks
        self.assertEqual(external_url('https://['), '')
        self.assertEqual(href('https://['), 'href="#"')
        self.assertIn('https://[', markdown_inline('before https://[ after'))
        self.assertIn('https://[', markdown_blocks('before https://[ after'))


if __name__ == "__main__":
    unittest.main()
