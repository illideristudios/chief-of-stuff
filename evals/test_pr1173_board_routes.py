"""Board routes retain the forge identity across tracker history and store collisions."""
import sys
from pathlib import Path
import unittest
from dataclasses import replace
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import board_sources as bs
import fragment
import issue_page
import render_board as rb
import task_forge
import test_flow_board as fb
import test_issue_page as tip
from test_pr1173_identity import configured


def route(key):
    return '/issues/' + quote(key, safe='')


class BoardIssueRoutes(unittest.TestCase):
    def test_cards_and_flow_keep_both_current_identities(self):
        cases = [
            (bs.backlog.GitHubBacklog('o/app'), '#109', 'https://github.com/o/other/issues/109',
             'github:o/app#109', 'github:o/other#109'),
            (bs.backlog.Backlog('https://labs.example.test', 'team/app'), '#109',
             'https://foreign.example:8443/team/nested/other/-/issues/109',
             'gitlab:labs.example.test/team/app#109', 'gitlab:foreign.example:8443/team/nested/other#109'),
        ]
        for home, own, foreign, own_key, foreign_key in cases:
            with self.subTest(home=home):
                tracker = tip.flow_launch_tracker([('Home task', 'Home work', own), ('Other task', 'Other work', foreign)],
                                                  [('Home work', 'one', '00:10'), ('Other work', 'two', '00:20')])
                tracker = tracker.replace(' | S |  |  | ', ' | S | build | implement | ')
                src = replace(bs.EMPTY, home=home)
                cfg = configured(home)
                cards = rb.build_columns(rb.parse_tracker(tracker).tasks, tip.LANES, sources=src, now=tip.NOW)
                for name, key in [('Home task', own_key), ('Other task', foreign_key)]:
                    card = fb.card_named(cards, name)
                    self.assertEqual(card.href, route(key))
                    self.assertEqual(card.refs[0][1], route(key))
                    page = issue_page.render(key, [(tip.TODAY, tracker)], src, tip.NOW, tip.LANES, cfg)
                    self.assertIn(name, page)
                self.assertIsNone(issue_page.render(109, [(tip.TODAY, tracker)], src, tip.NOW, tip.LANES, cfg))
                html = rb.render(tracker, cfg, tip.NOW, lanes=tip.LANES, sources=src, tracker_day=tip.TODAY)
                flow = html[html.index('<h2>Flow ·'):]
                self.assertIn(route(own_key), flow)
                self.assertIn(route(foreign_key), flow)

    def test_historical_foreign_flow_link_does_not_open_current_home_task(self):
        home = bs.backlog.GitHubBacklog('o/app')
        old = tip.flow_launch_tracker([('Old task', 'Old work', 'o/other#109')], [('Old work', 'one', '00:10')])
        current = tip.flow_launch_tracker([('Current task', 'Current work', '#109')], [('Current work', 'two', '00:20')])
        src, cfg = replace(bs.EMPTY, home=home), configured(home)
        html = rb.render(current, cfg, tip.NOW, lanes=tip.LANES, sources=src, tracker_day=tip.TODAY,
                         stage_log=[(tip.YESTERDAY, old)])
        self.assertIn(route('github:o/other#109'), html[html.index('<h2>Flow ·'):])
        old_page = issue_page.render('github:o/other#109', [(tip.YESTERDAY, old), (tip.TODAY, current)], src, tip.NOW, tip.LANES, cfg)
        self.assertIn('Old task', old_page)
        self.assertNotIn('Current task', old_page)
        self.assertIn('Current task', issue_page.render(109, [(tip.YESTERDAY, old), (tip.TODAY, current)], src, tip.NOW, tip.LANES, cfg))

    def test_ambiguous_unqualified_reference_does_not_become_home_number(self):
        home = bs.backlog.Backlog('https://labs.example.test', 'team/app')
        task = rb.Task('Ambiguous', 'Robin', 'open', '09:00', '', 'c', name='Ambiguous',
                       issue='team/nested/other#109', lane='build', stage='implement')
        card = fb.card_named(rb.build_columns([task], tip.LANES, sources=replace(bs.EMPTY, home=home)), 'Ambiguous')
        self.assertNotEqual(card.href, '/issues/109')
        self.assertNotEqual(card.refs[0][1], '/issues/109')
        tracker = tip.flow_launch_tracker([('Ambiguous', 'Ambiguous work', task.issue)],
                                          [('Ambiguous work', 'one', '00:10')])
        html = rb.render(tracker, configured(home), tip.NOW, lanes=tip.LANES,
                         sources=replace(bs.EMPTY, home=home), tracker_day=tip.TODAY)
        self.assertNotIn('href="/issues/109"', html[html.index('<h2>Flow ·'):])

    def test_foreign_change_refs_and_merged_cards_keep_the_issue_identity(self):
        home = bs.backlog.GitHubBacklog('o/app')
        key = 'github:o/other#109'
        task = rb.Task('Other work', 'Robin', 'open', '09:00', '', 'c', name='Other task',
                       issue='https://github.com/o/other/issues/109', lane='build', stage='implement')
        change = fb.change('!60', issues=('o/other#109',))
        src = replace(bs.EMPTY, home=home, changes={'!60': change})
        card = fb.card_named(rb.build_columns([task], tip.LANES, sources=src, now=tip.NOW), 'Other task')
        self.assertEqual(card.refs[-1], ('!60', route(key)))
        merged = replace(change, state='merged', merged_at=tip.NOW)
        main = rb.build_columns([], tip.LANES, sources=replace(src, changes={'!60': merged}), now=tip.NOW)[-1]
        self.assertEqual(main.cards[0].href, route(key))
        self.assertTrue(all(link == route(key) for _, link in main.cards[0].refs))

    def test_homeless_bare_ref_with_a_historical_collision_is_not_linked(self):
        old = tip.flow_launch_tracker([('Old task', 'Old work', '#109')], [('Old work', 'one', '00:10')])
        current = tip.flow_launch_tracker([('Current task', 'Current work', 'o/other#109')], [('Current work', 'two', '00:20')])
        cfg = replace(configured(bs.backlog.GitHubBacklog('o/app')), backlog=None)
        html = rb.render(current, cfg, tip.NOW, lanes=tip.LANES, sources=bs.EMPTY, tracker_day=tip.TODAY,
                         stage_log=[(tip.YESTERDAY, old)])
        flow = html[html.index('<h2>Flow ·'):]
        self.assertNotIn('href="/issues/109"', flow)
        self.assertIn(route('github:o/other#109'), flow)
        tasks = [replace(rb.parse_tracker(old).tasks[0], lane='build', stage='implement'),
                 replace(rb.parse_tracker(current).tasks[0], lane='build', stage='implement')]
        card = fb.card_named(rb.build_columns(tasks, tip.LANES), 'Old task')
        self.assertEqual(card.href, '')
        self.assertEqual(card.refs[0][1], '')

    def test_store_board_keeps_canonical_routes_for_same_number(self):
        cases = [
            (bs.backlog.GitHubBacklog('o/app'), ('github:o/app#109', 'github:o/other#109')),
            (bs.backlog.Backlog('https://labs.example.test', 'team/app'),
             ('gitlab:labs.example.test/team/app#109', 'gitlab:foreign.example:8443/team/other#109')),
        ]
        for home, keys in cases:
            with self.subTest(home=home):
                case = tip.TaskPageLinksAndSafetyTest()
                db = case.make_db()
                try:
                    for i, key in enumerate(keys):
                        db.upsert_item(key, title=f'Task {i}', stage='implement')
                    data = task_forge.page_data(db)
                    src, cfg = replace(bs.EMPTY, home=home), configured(home)
                    html = rb.render(tip.TODAY_START, cfg, tip.NOW, lanes=tip.LANES, sources=src, db=data)
                    for key in keys:
                        self.assertGreaterEqual(html.count(route(key)), 2)
                        self.assertIsNotNone(issue_page.render(key, [], src, tip.NOW, tip.LANES, cfg, db=data))
                    self.assertIsNone(issue_page.render(109, [], src, tip.NOW, tip.LANES, cfg, db=data))
                finally:
                    case.doCleanups()

    def test_encoded_route_accepts_gitlab_host_port(self):
        import pages
        from urllib.parse import unquote
        key = 'gitlab:foreign.example:8443/team/nested/other#109'
        self.assertEqual(pages.issue_route(unquote(fragment.issue_href(key))), (key, False))

    def test_issue_route_parser_preserves_routes_and_rejects_malformed_input(self):
        import pages
        cases = {
            '/issues/7': ('7', False),
            '/issues/7/source': ('7', True),
            '/issues/github:o/app#109': ('github:o/app#109', False),
            '/issues/gitlab:foreign.example:8443/team/nested/other#109/source':
                ('gitlab:foreign.example:8443/team/nested/other#109', True),
            '/issues/github:o/app#109/source/extra': None,
            '/issues/github:o/app#109#110': None,
            '/issues/github:#109': None,
            '/issues/': None,
            '/issues/' + '-:' * 30000: None,
        }
        for path, expected in cases.items():
            with self.subTest(path=path[:80]):
                self.assertEqual(pages.issue_route(path), expected)
