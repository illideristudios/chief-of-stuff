"""Bounded GitHub reads, complete panels, and the persisted automatic retry cadence."""
import json
import re
import unittest
from datetime import timedelta

from test_board_sources import NOW, FakeGh, gh_issue, gh_pr, workspace
import board_sources as bs


class PagingGh:
    def __init__(self, opened=72, merged=50, fail_at=0, partial=False, remaining=4900):
        self.opened, self.merged, self.fail_at = opened, merged, fail_at
        self.partial, self.remaining, self.queries = partial, remaining, []

    def __call__(self, args, input_text=None):
        q = args[args.index('-f') + 1]
        self.queries.append(q)
        if len(self.queries) == self.fail_at and not self.partial:
            return 1, '', 'HTTP 502'
        data = {'rateLimit': {'cost': 5, 'remaining': self.remaining, 'limit': 5000}}
        named = re.search(r'(r\d+): repository\(', q)
        if named:
            data[named[1]] = {f'n{n}': gh_issue(int(n)) for n in re.findall(r'n(\d+): issueOrPullRequest', q)}
        else:
            alias = 'open' if 'open: search' in q else 'merged'
            count = self.opened if alias == 'open' else self.merged
            cursor = re.search(r'after: "(\d+)"', q)
            offset = int(cursor[1]) if cursor else 0
            stop = min(offset + bs.GH_PAGE, count)
            nodes = [gh_pr(1000 + n if alias == 'open' else 2000 + n,
                           None if alias == 'open' else '2026-09-26T05:41:00Z',
                           'OPEN' if alias == 'open' else 'MERGED') for n in range(offset, stop)]
            data[alias] = {'nodes': nodes, 'pageInfo': {'hasNextPage': stop < count, 'endCursor': str(stop)}}
        body = {'data': data}
        if self.partial and len(self.queries) == self.fail_at:
            body['errors'] = [{'message': 'partial GraphQL failure', 'path': ['open']}]
        return 0, json.dumps(body), ''


class GitHubBudgetTests(unittest.TestCase):
    def read(self, gh, wanted=None):
        return bs.github(bs.backlog.GitHubBacklog('o/app'), wanted or {'o/app': {1, 2}}, NOW, '', gh, True)

    def test_live_shape_load_is_bounded_and_paginates_all_open_and_recent_merged(self):
        gh = PagingGh()
        issues, changes, errors = self.read(gh)
        self.assertEqual(errors, {})
        self.assertEqual(len(issues), 2)
        self.assertEqual(len(changes), 122)
        self.assertEqual(len(gh.queries), 26)
        self.assertEqual(len(gh.queries) * 5, 130)
        self.assertLessEqual(bs.GH_POINT_BUDGET * 6, 1200)
        self.assertTrue(all('rateLimit { cost remaining limit }' in q for q in gh.queries))
        self.assertTrue(all('first: 5' in q for q in gh.queries[1:]))
        self.assertTrue(all(q.count('issueOrPullRequest(number:') <= 5 for q in gh.queries))

    def test_request_and_point_limits_never_publish_partial_panels(self):
        for gh in (PagingGh(opened=500, merged=50), PagingGh(remaining=100)):
            with self.subTest(remaining=gh.remaining):
                issues, changes, errors = self.read(gh)
                self.assertIsNone(changes)
                self.assertTrue(errors)
                self.assertLessEqual(len(gh.queries), bs.GH_MAX_REQUESTS)
                if gh.remaining == 100:
                    self.assertIsNone(issues)
                    self.assertEqual(len(gh.queries), 1)

    def test_later_page_failure_or_partial_error_keeps_issues_but_not_incomplete_changes(self):
        for partial in (False, True):
            gh = PagingGh(fail_at=4, partial=partial)
            issues, changes, errors = self.read(gh)
            self.assertEqual(len(issues), 2)
            self.assertIsNone(changes)
            self.assertIn('merge requests', errors)
            self.assertEqual(len(gh.queries), 4)

    def test_named_failure_invalidates_both_panels_and_transport_stops(self):
        gh = PagingGh(fail_at=1)
        issues, changes, errors = self.read(gh)
        self.assertIsNone(issues)
        self.assertIsNone(changes)
        self.assertEqual(set(errors), {'kanban', 'merge requests'})
        self.assertEqual(len(gh.queries), 1)

    def test_point_budget_stops_before_another_request(self):
        class Costly(PagingGh):
            def __call__(self, args, input_text=None):
                code, out, err = super().__call__(args, input_text)
                body = json.loads(out)
                body['data']['rateLimit']['cost'] = 100
                return code, json.dumps(body), err
        gh = Costly()
        issues, changes, errors = self.read(gh)
        self.assertEqual(len(gh.queries), 2)
        self.assertEqual(len(issues), 2)
        self.assertIsNone(changes)
        self.assertIn('budget', errors['merge requests'])

    def test_named_not_found_is_not_a_transport_failure(self):
        def gh(args):
            q = args[-1]
            data = {'rateLimit': {'cost': 1, 'remaining': 4900, 'limit': 5000}}
            if 'r0: repository' in q:
                data['r0'] = {'n1': None}
                return 1, json.dumps({'data': data, 'errors': [{'type': 'NOT_FOUND', 'path': ['r0', 'n1'], 'message': 'not found'}]}), 'not found'
            data['merged'] = {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}
            return 0, json.dumps({'data': data}), ''
        issues, changes, errors = bs.github(bs.backlog.GitHubBacklog('o/app'), {'o/app': {1}}, NOW, '', gh)
        self.assertEqual((issues, changes, errors), ({}, {}, {}))

    def test_missing_repository_never_caches_a_false_empty_panel(self):
        def gh(args):
            q = args[-1]
            data = {'rateLimit': {'cost': 1, 'remaining': 4900, 'limit': 5000}, 'r0': None,
                    'merged': {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}
            return 0, json.dumps({'data': data}), ''
        issues, changes, errors = bs.github(bs.backlog.GitHubBacklog('o/app'), {'o/app': {1}}, NOW, '', gh)
        self.assertIsNone(issues)
        self.assertIsNone(changes)
        self.assertEqual(set(errors), {'kanban', 'merge requests'})

    def test_scheduled_failure_retries_after_interval_even_without_a_good_cache(self):
        root = workspace('GitHub issues; repo o/app')
        gh = FakeGh(fail=True)
        first = bs.refresh(root, NOW, gh=gh, scheduled=True)
        self.assertIn('kanban', first.errors)
        skipped = bs.refresh(root, NOW + timedelta(minutes=1), gh=gh, scheduled=True)
        self.assertEqual(len(gh.calls), 1)
        self.assertEqual(skipped.errors['kanban'], first.errors['kanban'])
        recovered = FakeGh()
        result = bs.refresh(root, NOW + bs.GH_INTERVAL, gh=recovered, scheduled=True)
        self.assertTrue(recovered.calls)
        self.assertEqual(result.errors, {})
        self.assertIn('#118', result.issues)

    def test_failed_search_preserves_last_good_changes_and_time(self):
        root = workspace('GitHub issues; repo o/app')
        first = bs.refresh(root, NOW, gh=FakeGh(), scheduled=True)
        failing = PagingGh(fail_at=2)
        later = NOW + bs.GH_INTERVAL
        got = bs.refresh(root, later, gh=failing, scheduled=True)
        self.assertEqual(got.changes, first.changes)
        self.assertEqual(got.fetched['merge requests'], NOW)
        self.assertEqual(got.fetched['kanban'], later)
        self.assertIn('merge requests', got.errors)

class SourceStatusTests(unittest.TestCase):
    def test_issue_page_reports_unavailable_and_stale_reads(self):
        from dataclasses import replace
        import issue_page
        from test_board_sources import DAY
        root = workspace('GitHub issues; repo o/app')
        cfg = bs.read_config(root)
        tracker = (root / 'daily' / f'{DAY}-tracker.md').read_text()
        unavailable = bs.Sources({}, {}, {}, (), {'kanban': '<down>', 'merge requests': 'timeout'}, cfg.backlog)
        page = issue_page.render(118, [(NOW.date(), tracker)], unavailable, NOW, cfg=cfg)
        self.assertIn('kanban: unavailable', page)
        self.assertIn('merge requests: unavailable', page)
        self.assertIn('&lt;down&gt;', page)
        stale = replace(unavailable, fetched={'kanban': NOW, 'merge requests': NOW})
        page = issue_page.render(118, [(NOW.date(), tracker)], stale, NOW, cfg=cfg)
        self.assertIn('kanban: stale', page)
        self.assertIn('merge requests: stale', page)
        import render_board
        board = render_board.render(tracker, cfg, NOW, sources=stale)
        self.assertIn('kanban: stale', board)
        self.assertIn('merge requests: stale', board)
        board = render_board.render(tracker, cfg, NOW, sources=unavailable)
        self.assertIn('kanban: unavailable', board)
        self.assertIn('merge requests: unavailable', board)


if __name__ == '__main__':
    unittest.main()
