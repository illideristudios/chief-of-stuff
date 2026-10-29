"""Durability, convergence and publication boundaries for GitHub source scans."""
import json
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from test_board_sources import NOW, FakeGh, gh_issue, gh_pr, workspace
import board_sources as bs
import github_scan as scan

UPDATED = '2026-09-26T06:00:00Z'


class Forge:
    def __init__(self, count=1205):
        self.nodes = {n: {**gh_pr(n), 'updatedAt': UPDATED} for n in range(1001, 1001 + count)}
        self.calls = []
        self.fail = False
        self.partial = False
        self.repeat = False
        self.mutate_after_page = False
        self.shift_after_page = False

    def __call__(self, args):
        q = args[-1]
        self.calls.append(q)
        if self.fail:
            return 1, '', 'offline'
        data = {'rateLimit': {'cost': 1, 'remaining': 4900, 'limit': 5000}}
        if 'inventory: repository' in q:
            cursor = re.search(r'after: "(\d+)"', q)
            offset = int(cursor[1]) if cursor else 0
            phase = 'OPEN' if 'states: [OPEN]' in q else 'MERGED'
            entries = sorted((n for n in self.nodes.values() if n['state'] == phase), key=(lambda n: n['number']) if phase == 'OPEN' else (lambda n: (n['updatedAt'], n['number'])), reverse=phase == 'MERGED')
            nodes = entries[offset:offset + scan.INVENTORY_PAGE]
            end = offset + len(nodes)
            data['inventory'] = {'pullRequests': {'nodes': [{k: n[k] for k in ('number', 'state', 'updatedAt', 'mergedAt')} for n in nodes],
                'pageInfo': {'hasNextPage': end < len(entries), 'endCursor': str(offset if self.repeat and offset else end)}}}
            if self.shift_after_page and not offset:
                self.shift_after_page = False
                self.nodes = {999: {**gh_pr(999), 'updatedAt': UPDATED}, **self.nodes}
            if self.mutate_after_page and not offset:
                self.mutate_after_page = False
                self.nodes[1001] = {**self.nodes[1001], 'updatedAt': '2026-09-26T06:01:00Z'}
        elif 'issueOrPullRequest' in q:
            data['r0'] = {f'n{n}': gh_issue(int(n)) for n in re.findall(r'n(\d+): issueOrPullRequest', q)}
        else:
            data['r0'] = {f'n{n}': self.nodes.get(int(n)) for n in re.findall(r'n(\d+): pullRequest\(', q)}
        body = {'data': data}
        if self.partial:
            body['errors'] = [{'message': 'partial', 'path': ['inventory']}]
        return 0, json.dumps(body), ''


class DurableScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / scan.CHECKPOINT
        self.home = bs.backlog.GitHubBacklog('o/app')
        self.wanted = {'o/app': set(range(1, 501))}
        self.gh = Forge()
        self.now = NOW

    def run_scan(self, budget=7, *, wanted=None, home=None, since=None, every_open=True):
        admission = scan.Admission(self.gh, budget, 200, 500)
        result = scan.scan(self.path, home or self.home, self.wanted if wanted is None else wanted,
                           since or NOW, '', every_open, admission, self.now, bs)
        self.now += timedelta(minutes=10)
        return result

    def until_done(self, limit=100):
        for _ in range(limit):
            result = self.run_scan()
            if result[1] is not None:
                return result
        self.fail('scan did not converge')

    def test_large_scan_interleaves_and_survives_many_restarts(self):
        first = self.run_scan()
        self.assertEqual(first[:2], (None, None))
        state = json.loads(self.path.read_text())
        self.assertGreater(state['named_index'], 0)
        self.assertTrue(state['inventory'])
        result = self.until_done()
        self.assertEqual(len(result[0]), 500)
        self.assertEqual(len(result[1]), 1205)
        self.assertEqual(result[2], {})
        self.assertIsNotNone(result[3])
        self.assertTrue(all('search(' not in q for q in self.gh.calls))
        self.assertTrue(all(q.count('issueOrPullRequest(') <= 5 for q in self.gh.calls))

    def test_shifted_inventory_requires_another_complete_sweep(self):
        self.gh = Forge(110)
        self.wanted = {}
        self.gh.mutate_after_page = True
        result = self.until_done()
        sweeps = sum('inventory: repository' in q and 'after:' not in q for q in self.gh.calls)
        self.assertGreaterEqual(sweeps, 2)
        self.assertEqual(len(result[1]), 110)

    def test_shifted_pages_with_duplicate_restart_and_converge(self):
        self.gh = Forge(150)
        self.wanted = {}
        self.gh.shift_after_page = True
        first = self.run_scan()
        self.assertIsNone(first[1])
        self.assertIn('merge requests', first[2])
        self.assertEqual(len(self.until_done()[1]), 151)

    def test_checkpoint_survives_new_interpreter_processes(self):
        import subprocess
        import sys
        script = """import sys
from pathlib import Path
from datetime import timedelta
from test_github_durable_scan import Forge, NOW
import github_scan, board_sources
p = Path(sys.argv[1])
github_scan.scan(p, board_sources.backlog.GitHubBacklog('o/app'), {'o/app': set(range(1, 501))},
                 NOW, '', True, github_scan.Admission(Forge(), 7, 200, 500), NOW + timedelta(minutes=int(sys.argv[2])), board_sources)
"""
        import os
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(Path(__file__).parent), str(Path(bs.__file__).parent)))}
        for i in range(3):
            subprocess.run([sys.executable, '-c', script, str(self.path), str(10 * i)], env=env, check=True)
        self.assertGreater(json.loads(self.path.read_text())['named_index'], 15)
        self.assertEqual(len(self.until_done()[1]), 1205)

    def test_recent_merged_selection_and_non_default_branch_links(self):
        self.gh = Forge(0)
        self.wanted = {}
        for n in range(60):
            stamp = f'2026-09-{(n % 28) + 1:02}T06:00:00Z'
            node = {**gh_pr(1001 + n, stamp, 'MERGED'), 'updatedAt': stamp,
                    'baseRefName': 'release', 'headRefName': 'topic/test', 'headRef': {'name': 'topic/test'},
                    'headRepository': {'url': 'https://github.com/fork/app'}}
            self.gh.nodes[1001 + n] = node
        _, changes, errors, _ = self.until_done()
        self.assertEqual(errors, {})
        self.assertEqual(len(changes), 50)
        change = next(iter(changes.values()))
        self.assertEqual(change.base, 'release')
        self.assertEqual(change.branch_url, 'https://github.com/fork/app/tree/topic/test')
        self.assertIsNotNone(change.merged_at)
        self.path.unlink()
        self.now = NOW
        _, today, errors, _ = self.run_scan(budget=40, every_open=False)
        self.assertEqual(errors, {})
        self.assertTrue(today)
        self.assertTrue(all(c.merged_at >= NOW for c in today.values()))

    def test_missing_rich_field_is_not_a_complete_result(self):
        self.gh = Forge(1)
        self.wanted = {}
        del self.gh.nodes[1001]['files']
        self.assertIsNone(self.run_scan()[1])
        self.gh.nodes[1001]['files'] = {'nodes': []}
        self.assertEqual(len(self.until_done()[1]), 1)

    def test_repeated_cursor_and_partial_response_never_publish(self):
        self.gh = Forge(250)
        self.wanted = {}
        self.gh.repeat = True
        self.assertIsNone(self.run_scan()[1])
        self.assertIn('cursor', self.run_scan()[2]['merge requests'])
        self.gh.repeat = False
        self.gh.partial = True
        self.assertIsNone(self.run_scan()[1])
        self.gh.partial = False
        self.assertEqual(len(self.until_done()[1]), 250)

    def test_change_in_details_restarts_inventory(self):
        self.gh = Forge(10)
        self.wanted = {}
        self.run_scan(budget=2)
        self.gh.nodes[1001]['updatedAt'] = '2026-09-26T06:02:00Z'
        self.assertEqual(len(self.run_scan()[1]), 10)

    def test_identity_changes_discard_incompatible_checkpoints(self):
        self.run_scan(budget=3)
        before = json.loads(self.path.read_text())['fingerprint']
        self.run_scan(budget=1, wanted={'o/app': {999}})
        self.assertEqual(before, json.loads(self.path.read_text())['fingerprint'])
        self.run_scan(budget=1, wanted={'o/app': {999}}, since=NOW + timedelta(days=1))
        self.assertEqual(json.loads(self.path.read_text())['named_index'], 1)
        self.run_scan(budget=1, wanted={'o/app': {999}}, home=bs.backlog.GitHubBacklog('o/other'))
        self.assertFalse(json.loads(self.path.read_text())['home_verified'])

    def test_corrupt_checkpoint_is_discarded_and_query_change_restarts(self):
        self.run_scan(budget=3)
        state = json.loads(self.path.read_text())
        state['named_index'] = 500
        self.path.write_text(json.dumps(state))
        self.run_scan(budget=1)
        self.assertEqual(json.loads(self.path.read_text())['named_index'], 5)
        with mock.patch.object(bs, 'GH_ISSUE', bs.GH_ISSUE + ' '):
            self.run_scan(budget=1)
        self.assertEqual(json.loads(self.path.read_text())['named_index'], 5)

    def test_target_add_remove_preserves_inventory_and_completed_named_observations(self):
        self.gh = Forge(110)
        self.wanted = {'o/app': set(range(1, 11))}
        self.run_scan(budget=3)
        before = json.loads(self.path.read_text())
        self.assertTrue(before['inventory'])
        self.wanted = {'o/app': {1, 2, 3, 4, 5, 11}}
        self.run_scan(budget=1)
        after = json.loads(self.path.read_text())
        self.assertEqual(after['inventory'][:len(before['inventory'])], before['inventory'])
        self.assertEqual(after['named']['o/app#1'], before['named']['o/app#1'])
        self.assertNotIn('o/app#6', after['named'])
        result = self.until_done()
        self.assertIn('o/app#11', json.loads(self.path.read_text())['named'])
        self.assertEqual(len(result[0]), 6)

    def test_detail_churn_completes_without_restarting_or_losing_prior_details(self):
        self.gh = Forge(30)
        self.wanted = {}
        self.run_scan(budget=5)
        for _ in range(20):
            for node in self.gh.nodes.values():
                node['updatedAt'] = self.now.isoformat()
            result = self.run_scan(budget=1)
            if result[1] is not None:
                break
        self.assertEqual(len(result[1]), 30)
        self.assertEqual(sum('inventory: repository' in q for q in self.gh.calls), 4)

    def test_irrelevant_merged_history_does_not_exhaust_item_cap(self):
        self.gh = Forge(0)
        self.wanted = {}
        for n in range(1, 20051):
            self.gh.nodes[n] = {**gh_pr(n, '2020-01-01T00:00:00Z', 'MERGED'), 'updatedAt': '2020-01-01T00:00:00Z'}
        self.gh.nodes[30001] = {**gh_pr(30001), 'updatedAt': UPDATED, 'baseRefName': 'release',
                                 'body': 'Fixes #118'}
        self.gh.nodes[30002] = {**gh_pr(30002, UPDATED, 'MERGED'), 'updatedAt': UPDATED}
        _, changes, errors, _ = self.until_done()
        self.assertEqual(errors, {})
        self.assertIn('#30001', changes)
        self.assertEqual(changes['#30001'].issues, ('#118',))
        self.assertIn('#30002', changes)
        self.assertEqual(len(changes), 51)
        self.assertLess(sum('inventory: repository' in q for q in self.gh.calls), 10)

    def test_named_freshness_is_oldest_contributor_across_batches_and_target_changes(self):
        self.gh = Forge(0)
        self.wanted = {'o/app': set(range(1, 11))}
        self.run_scan(budget=1)
        first = self.now - timedelta(minutes=10)
        self.until_done()
        state = json.loads(self.path.read_text())
        self.assertEqual(state['named_observed'], first.isoformat())
        self.wanted = {'o/app': {1, 11}}
        self.run_scan(budget=1)
        state = json.loads(self.path.read_text())
        self.assertEqual(state['named_observed'], first.isoformat())
        self.wanted = {'o/app': {11}}
        self.run_scan(budget=1)
        state = json.loads(self.path.read_text())
        self.assertGreater(state['named_observed'], first.isoformat())

    def test_malformed_nested_details_are_retried_without_poisoning_checkpoint(self):
        self.gh = Forge(1)
        self.wanted = {}
        self.gh.nodes[1001]['files'] = {'nodes': [{}]}
        self.assertIsNone(self.run_scan()[1])
        self.assertNotIn('1001', json.loads(self.path.read_text())['details'])
        self.gh.nodes[1001]['files'] = {'nodes': [{'path': 'fixed.py'}]}
        result = self.until_done()
        self.assertEqual(result[1]['#1001'].files, ('fixed.py',))

    def test_checkpoint_write_failure_does_not_publish(self):
        self.gh = Forge(0)
        self.wanted = {}
        with mock.patch.object(scan, '_save', side_effect=OSError('disk full')):
            issues, changes, errors, _ = self.run_scan()
        self.assertIsNone(issues)
        self.assertIsNone(changes)
        self.assertIn('disk full', errors['kanban'])

    def test_completed_checkpoint_survives_cache_write_failure(self):
        root = workspace('GitHub issues; repo o/app')
        import shutil
        self.addCleanup(shutil.rmtree, root, True)
        gh = Forge(0)
        with mock.patch.object(bs, '_write', side_effect=OSError('cache full')):
            got = bs.refresh(root, NOW, gh=gh)
        self.assertIn('cache', got.errors)
        checkpoint = root / 'pages' / scan.CHECKPOINT
        self.assertTrue(checkpoint.exists())
        calls = len(gh.calls)
        recovered = bs.refresh(root, NOW + timedelta(minutes=1), gh=gh)
        self.assertEqual(recovered.errors, {})
        self.assertEqual(len(gh.calls), calls)
        self.assertFalse(checkpoint.exists())
        self.assertEqual(recovered.fetched['merge requests'], NOW)

    def test_completed_older_checkpoint_cannot_replace_newer_cached_panels(self):
        root = workspace('GitHub issues; repo o/app')
        import shutil
        self.addCleanup(shutil.rmtree, root, True)
        gh = FakeGh()
        with mock.patch.object(bs, '_write', side_effect=OSError('cache full')):
            old = bs.refresh(root, NOW, gh=gh)
        self.assertIn('cache', old.errors)
        newer_at = NOW + timedelta(minutes=10)
        newer_issue = bs.Issue('#118', 'https://github.com/o/app/issues/118', 'Newer', 'open', ())
        newer = bs.Sources({'kanban': newer_at, 'merge requests': newer_at, 'github sweep': newer_at},
                           {'#118': newer_issue}, {}, (), {}, home=old.home)
        bs._write(root / 'pages' / bs.CACHE, newer, newer_at)
        got = bs.refresh(root, newer_at + timedelta(minutes=1), gh=gh)
        self.assertEqual(got.issues['#118'].title, 'Newer')
        self.assertEqual(got.fetched['kanban'], newer_at)
        self.assertEqual(got.fetched['merge requests'], newer_at)
        self.assertIn('revalidating', got.errors['kanban'])
        self.assertIn('revalidating', got.errors['merge requests'])
        recovered = bs.refresh(root, newer_at + timedelta(minutes=2), gh=gh)
        self.assertEqual(recovered.errors, {})
        self.assertEqual(recovered.fetched['kanban'], newer_at + timedelta(minutes=2))

    def test_moved_merged_pr_from_first_sweep_survives_second_cursor(self):
        self.gh = Forge(0)
        self.wanted = {}
        for n in range(1001, 1151):
            self.gh.nodes[n] = {**gh_pr(n, NOW.isoformat(), 'MERGED'),
                                'updatedAt': NOW.isoformat()}
        original = self.gh
        merged_starts = 0
        def moved(args):
            nonlocal merged_starts
            query = args[-1]
            if 'states: [MERGED]' in query and 'after:' not in query:
                merged_starts += 1
            code, out, err = original(args)
            if 'states: [MERGED]' in query and 'after:' in query and merged_starts == 2:
                body = json.loads(out)
                page = body['data']['inventory']['pullRequests']
                page['nodes'] = [p for p in page['nodes'] if p['number'] != 1001]
                original.nodes[1001]['updatedAt'] = '2026-09-26T08:00:00Z'
                out = json.dumps(body)
            return code, out, err
        self.gh = moved
        _, changes, errors, _ = self.until_done()
        self.assertEqual(errors, {})
        self.assertEqual(len(changes), 150)
        self.assertIn('#1001', changes)

    def test_out_of_order_merged_page_cannot_publish(self):
        self.gh = Forge(0)
        self.wanted = {}
        for n in range(1001, 1102):
            self.gh.nodes[n] = {**gh_pr(n, NOW.isoformat(), 'MERGED'), 'updatedAt': NOW.isoformat()}
        original = self.gh
        def disordered(args):
            code, out, err = original(args)
            if 'states: [MERGED]' in args[-1] and 'after:' in args[-1]:
                body = json.loads(out)
                body['data']['inventory']['pullRequests']['nodes'][0]['updatedAt'] = (NOW + timedelta(hours=1)).isoformat()
                out = json.dumps(body)
            return code, out, err
        self.gh = disordered
        result = self.run_scan(budget=40)
        self.assertIsNone(result[1])
        self.assertIn('order shifted', result[2]['merge requests'])
        self.gh = original
        self.assertEqual(len(self.until_done()[1]), 101)

    def test_named_pr_closed_at_detail_replaces_open_observation(self):
        self.gh = Forge(1)
        self.wanted = {'o/app': {1001}}
        original = self.gh
        def closing(args):
            if 'issueOrPullRequest' in args[-1]:
                data = {'r0': {'n1001': dict(original.nodes[1001])},
                        'rateLimit': {'cost': 1, 'remaining': 4900, 'limit': 5000}}
                return 0, json.dumps({'data': data}), ''
            if 'pullRequest(number:' in args[-1]:
                original.nodes[1001]['state'] = 'CLOSED'
                original.nodes[1001]['updatedAt'] = '2026-09-26T08:00:00Z'
            return original(args)
        self.gh = closing
        _, changes, errors, _ = self.until_done()
        self.assertEqual(errors, {})
        self.assertEqual(changes['#1001'].state, 'closed')

    def test_non_store_skips_irrelevant_open_inventory(self):
        self.gh = Forge(20050)
        self.gh.nodes[30001] = {**gh_pr(30001, NOW.isoformat(), 'MERGED'), 'updatedAt': NOW.isoformat()}
        self.wanted = {'o/app': {1001}}
        original = self.gh
        def named_pr(args):
            if 'issueOrPullRequest' in args[-1]:
                original.calls.append(args[-1])
                return 0, json.dumps({'data': {'r0': {'n1001': original.nodes[1001]},
                    'rateLimit': {'cost': 1, 'remaining': 4900, 'limit': 5000}}}), ''
            return original(args)
        self.gh = named_pr
        _, changes, errors, _ = self.run_scan(budget=40, every_open=False)
        self.assertEqual(errors, {})
        self.assertIn('#1001', changes)
        self.assertIn('#30001', changes)
        self.assertTrue(all('states: [OPEN]' not in q for q in original.calls))

    def test_rollover_retains_named_and_completed_open_details(self):
        self.gh = Forge(12)
        self.wanted = {'o/app': {1, 2}}
        self.run_scan(budget=6)
        before = json.loads(self.path.read_text())
        self.assertEqual(before['sweep'], 2)
        self.assertTrue(before['details'])
        named = before['named'].copy()
        first_details = set(before['details'])
        day = NOW + timedelta(days=1)
        self.now = day
        self.gh.nodes[30001] = {**gh_pr(30001, day.isoformat(), 'MERGED'), 'updatedAt': day.isoformat()}
        result = self.run_scan(budget=1, since=day)
        self.assertIsNone(result[1])
        after = json.loads(self.path.read_text())
        self.assertEqual(after['named'], named)
        self.assertTrue(first_details <= set(after['details']) or first_details <= set(after['seed_details']))
        for _ in range(20):
            result = self.run_scan(budget=2, since=day)
            if result[1] is not None:
                break
        self.assertEqual(result[2], {})
        self.assertEqual(len(result[1]), 13)
        self.assertIn('#30001', result[1])
        self.assertEqual(sum('states: [OPEN]' in q for q in self.gh.calls), 2)

    def test_rollover_during_sweep_requires_fresh_merged_pass(self):
        self.gh = Forge(0)
        self.wanted = {}
        self.run_scan(budget=3)
        day = NOW + timedelta(days=1)
        self.now = day
        self.gh.nodes[30001] = {**gh_pr(30001, day.isoformat(), 'MERGED'), 'updatedAt': day.isoformat()}
        result = self.run_scan(budget=1, since=day)
        self.assertIsNone(result[1])
        for _ in range(10):
            result = self.run_scan(budget=2, since=day)
            if result[1] is not None:
                break
        self.assertIn('#30001', result[1])
        self.assertEqual(result[2], {})

    def test_daily_rollover_does_not_starve_detail_batches(self):
        self.gh = Forge(25)
        self.wanted = {}
        self.run_scan(budget=4)
        for day_offset in range(1, 20):
            day = NOW + timedelta(days=day_offset)
            self.now = day
            result = self.run_scan(budget=1, since=day)
            if result[1] is not None:
                break
        self.assertIsNotNone(result[1])
        self.assertEqual(len(result[1]), 25)
        self.assertEqual(result[2], {})
        self.assertLess(day_offset, 19)

    def test_malformed_inventory_checkpoint_timestamps_recover(self):
        for collection, field, invalid in (('inventory', 'updatedAt', 'garbage'),
                                            ('inventory', 'mergedAt', '2026-99-99'),
                                            ('selected', 'updatedAt', '2026-01-01T00:00:00'),
                                            ('selected', 'mergedAt', 'bad'),
                                            ('first', 'updatedAt', 'bad'),
                                            ('details', 'updatedAt', 'bad')):
            with self.subTest(collection=collection, field=field):
                self.path.unlink(missing_ok=True)
                self.gh = Forge(1)
                self.wanted = {}
                budget = {'inventory': 1, 'first': 2, 'selected': 4, 'details': 5}[collection]
                self.run_scan(budget=budget)
                state = json.loads(self.path.read_text())
                entry = state[collection]['1001'] if collection == 'details' else state[collection][0]
                entry[field] = invalid
                self.path.write_text(json.dumps(state))
                result = self.until_done()
                self.assertEqual(result[2], {})
                self.assertIn('#1001', result[1])

    def test_bounded_targets_and_disk(self):
        self.wanted = {'o/app': set(range(scan.MAX_TARGETS + 1))}
        self.assertIsNone(self.run_scan()[0])
        self.assertFalse(self.path.exists())
        self.wanted = {}
        with mock.patch.object(scan, 'MAX_BYTES', 100):
            self.assertIsNone(self.run_scan()[1])
        self.assertFalse(self.path.exists())

    def test_refresh_keeps_old_panel_on_failure_and_reports_sweep(self):
        root = workspace('GitHub issues; repo o/app')
        import shutil
        self.addCleanup(shutil.rmtree, root, True)
        import sqlite3
        with sqlite3.connect(root / 'store.db') as db:
            db.execute('CREATE TABLE work_item (issue_key TEXT)')
            db.execute('CREATE TABLE resume (issue_key TEXT, data TEXT)')
        db.close()
        gh = Forge(3)
        first = bs.refresh(root, NOW, gh=gh)
        self.assertEqual(len(first.changes), 3)
        self.assertEqual(first.fetched['github sweep'], NOW)
        gh.fail = True
        later = bs.refresh(root, NOW + timedelta(minutes=10), gh=gh)
        self.assertEqual(later.changes, first.changes)
        self.assertEqual(later.fetched['merge requests'], NOW)
        self.assertIn('merge requests', later.errors)
        import render_board
        cfg = bs.read_config(root)
        tracker = (root / cfg.tracker_path(NOW.date().isoformat())).read_text()
        self.assertIn('GitHub inventory observation interval began 2026-09-26 02:10 (non-atomic sweeps)', render_board.render(tracker, cfg, NOW, sources=later))


if __name__ == '__main__':
    unittest.main()
