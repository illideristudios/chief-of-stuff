"""Bounded GitHub reads, complete panels, and the persisted automatic retry cadence."""
import json
import re
import unittest
from datetime import timedelta

from test_board_sources import NOW, FakeGh, gh_issue, gh_pr, workspace
import board_sources as bs


class GitHubBudgetTests(unittest.TestCase):
    def test_admission_stops_at_request_or_point_limit_and_reserve(self):
        import github_scan
        def read(cost=1, remaining=4900):
            return lambda args: (0, json.dumps({'data': {'rateLimit': {'cost': cost, 'remaining': remaining, 'limit': 5000}}}), '')
        for admission in (github_scan.Admission(read(), 1, 200, 500),
                          github_scan.Admission(read(cost=200), 40, 200, 500)):
            admission.read('x')
            with self.assertRaisesRegex(github_scan.ReadError, 'budget'):
                admission.read('x')
        with self.assertRaisesRegex(github_scan.ReadError, 'rate limit'):
            github_scan.Admission(read(remaining=100), 40, 200, 500).read('x')

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

    def test_failed_inventory_preserves_last_good_changes_and_time(self):
        root = workspace('GitHub issues; repo o/app')
        first = bs.refresh(root, NOW, gh=FakeGh(), scheduled=True)
        gh = FakeGh()
        def failing(args):
            if 'inventory: repository' in args[-1]:
                return 1, '', 'offline'
            return gh(args)
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


class HomeAccessTests(unittest.TestCase):
    def test_empty_inventory_proves_access_but_missing_repository_does_not(self):
        import tempfile
        from pathlib import Path
        import github_scan
        def gh(accessible):
            def answer(args):
                repo = {'pullRequests': {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}} if accessible else None
                return 0, json.dumps({'data': {'inventory': repo, 'rateLimit': {'cost': 1, 'remaining': 4900, 'limit': 5000}}}), ''
            return answer
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / github_scan.CHECKPOINT
            def run(accessible):
                admission = github_scan.Admission(gh(accessible), 40, 200, 500)
                return github_scan.scan(path, bs.backlog.GitHubBacklog('o/app'), {}, NOW, '', False, admission, NOW, bs)
            issues, changes, errors, _ = run(False)
            self.assertIsNone(issues)
            self.assertIsNone(changes)
            self.assertEqual(set(errors), {'kanban', 'merge requests'})
            self.assertEqual(run(True)[:3], ({}, {}, {}))

    def test_failed_home_check_preserves_last_good_panels(self):
        from test_board_sources import DAY
        root = workspace('GitHub issues; repo o/app')
        first = bs.refresh(root, NOW, gh=FakeGh())
        (root / 'daily' / f'{DAY}-tracker.md').write_text('')
        def inaccessible(args):
            return 0, json.dumps({'data': {'home': None, 'rateLimit': {'cost': 1, 'remaining': 4900, 'limit': 5000}}}), ''
        got = bs.refresh(root, NOW + timedelta(minutes=10), gh=inaccessible)
        self.assertEqual((got.issues, got.changes), (first.issues, first.changes))
        self.assertEqual(got.fetched['kanban'], NOW)
        self.assertEqual(got.fetched['merge requests'], NOW)
        self.assertEqual(set(got.errors), {'kanban', 'merge requests'})

class RefreshConcurrencyTests(unittest.TestCase):
    def test_older_refresh_cannot_overwrite_newer_refresh(self):
        import threading
        from unittest import mock
        root = workspace('GitHub issues; repo o/app')
        old_entered, release_old, new_entered = threading.Event(), threading.Event(), threading.Event()
        original = bs.forge
        def forge(root, cfg, tracker, since, gh, call):
            if threading.current_thread().name == 'older':
                old_entered.set()
                if not release_old.wait(5):
                    raise RuntimeError('old refresh timed out')
            else:
                new_entered.set()
            return original(root, cfg, tracker, since, gh, call)
        old = threading.Thread(name='older', target=bs.refresh, args=(root, NOW), kwargs={'gh': FakeGh(n118=gh_issue(118))})
        newer_issue = {**gh_issue(118), 'title': 'newer'}
        new = threading.Thread(name='newer', target=bs.refresh, args=(root, NOW + timedelta(minutes=1)), kwargs={'gh': FakeGh(n118=newer_issue)})
        with mock.patch.object(bs, 'forge', side_effect=forge):
            old.start()
            self.assertTrue(old_entered.wait(5))
            new.start()
            new_entered.wait(0.3)
            release_old.set()
            old.join(5)
            new.join(5)
        self.assertFalse(old.is_alive() or new.is_alive())
        self.assertEqual(bs.load(root / 'pages').issues['#118'].title, 'newer')
        self.assertEqual(bs.load(root / 'pages').fetched['kanban'], NOW + timedelta(minutes=1))

    def test_late_older_request_keeps_newer_snapshot(self):
        root = workspace('GitHub issues; repo o/app')
        newer_issue = {**gh_issue(118), 'title': 'newer'}
        bs.refresh(root, NOW + timedelta(minutes=1), gh=FakeGh(n118=newer_issue))
        old_gh = FakeGh()
        result = bs.refresh(root, NOW, gh=old_gh)
        self.assertFalse(old_gh.calls)
        self.assertEqual(result.issues['#118'].title, 'newer')

    def test_same_timestamp_older_invocation_cannot_replace_newer(self):
        import threading
        from unittest import mock
        root = workspace('GitHub issues; repo o/app')
        old_entered, release_old = threading.Event(), threading.Event()
        original = bs.read_config
        def config(path):
            if threading.current_thread().name == 'older':
                old_entered.set()
                if not release_old.wait(5):
                    raise RuntimeError('old refresh timed out')
            return original(path)
        old = threading.Thread(name='older', target=bs.refresh, args=(root, NOW), kwargs={'gh': FakeGh()})
        newer_issue = {**gh_issue(118), 'title': 'newer'}
        with mock.patch.object(bs, 'read_config', side_effect=config):
            old.start()
            self.assertTrue(old_entered.wait(5))
            bs.refresh(root, NOW, gh=FakeGh(n118=newer_issue))
            release_old.set()
            old.join(5)
        self.assertFalse(old.is_alive())
        self.assertEqual(bs.load(root / 'pages').issues['#118'].title, 'newer')

    def test_refreshes_in_separate_processes_serialize(self):
        import multiprocessing
        ctx = multiprocessing.get_context('fork')
        root = workspace('GitHub issues; repo o/app')
        old_entered, release_old, new_entered = ctx.Event(), ctx.Event(), ctx.Event()
        def run_old():
            gh = FakeGh()
            def blocked(args):
                if not old_entered.is_set():
                    old_entered.set()
                    if not release_old.wait(5):
                        raise RuntimeError('old refresh timed out')
                return gh(args)
            bs.refresh(root, NOW, gh=blocked)
        def run_new():
            gh = FakeGh(n118={**gh_issue(118), 'title': 'newer'})
            def observed(args):
                new_entered.set()
                return gh(args)
            bs.refresh(root, NOW + timedelta(minutes=1), gh=observed)
        old, new = ctx.Process(target=run_old), ctx.Process(target=run_new)
        old.start()
        self.assertTrue(old_entered.wait(5))
        new.start()
        new_entered.wait(0.3)
        release_old.set()
        old.join(5)
        new.join(5)
        self.assertEqual((old.exitcode, new.exitcode), (0, 0))
        self.assertEqual(bs.load(root / 'pages').issues['#118'].title, 'newer')


if __name__ == '__main__':
    unittest.main()
