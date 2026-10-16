"""#279: the read-only open-change view shares the board's forge parsing."""
import contextlib
import io
import json
import shutil
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'scripts'), str(Path(__file__).resolve().parent)]
import board_sources as bs
import backlog
import start_coordinator as start
import test_board_sources as fixtures
from _vendor.toon_format import decode
from tracker import parse_tracker


class OpenChangesTest(unittest.TestCase):
    def test_read_only_command_lists_open_changes_without_writing_cache(self):
        root = fixtures.workspace('GitHub issues; repo o/app')
        self.addCleanup(shutil.rmtree, root)
        opened = bs._gh_change('#91', {**fixtures.gh_pr(91), 'mergeable': 'UNKNOWN'}, '')
        merged = bs._gh_change('#92', fixtures.gh_pr(92, '2026-09-26T06:00:00Z', 'MERGED'), '')
        cache = root / 'pages' / bs.CACHE
        cache.parent.mkdir()
        cache.write_text('last good cache')
        with mock.patch.object(bs, 'forge', return_value=({}, {'#91': opened, '#92': merged}, {})) as read, \
             mock.patch.object(bs, 'workers') as workers, contextlib.redirect_stdout(io.StringIO()) as output:
            code = bs.main(['--root', str(root), '--open'])
        self.assertEqual(code, 0)
        row, = decode(output.getvalue())['changes']
        self.assertEqual((row['ref'], row['pipeline'], row['mergeability'], row['files']),
                         ('#91', opened.pipeline, 'unknown', list(opened.files)))
        self.assertTrue(read.call_args.kwargs['include_open'])
        workers.assert_not_called()
        self.assertEqual(cache.read_text(), 'last good cache')

    def test_open_view_reports_forge_errors_as_failure(self):
        root = fixtures.workspace('GitHub issues; repo o/app')
        self.addCleanup(shutil.rmtree, root)
        with mock.patch.object(bs, 'forge', return_value=(None, None, {'merge requests': 'forbidden'})), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(bs.main(['--root', str(root), '--open']), 1)
        self.assertIn('forbidden', output.getvalue())

    def test_github_includes_unnamed_open_changes_on_every_page_using_board_parser(self):
        calls = []
        def gh(args):
            calls.append(args[-1])
            if len(calls) == 1:
                data = {'merged': {'nodes': []}}
            else:
                n = 91 if len(calls) == 2 else 92
                data = {'open': {'pullRequests': {'nodes': [fixtures.gh_pr(n)], 'pageInfo': {
                    'hasNextPage': n == 91, 'endCursor': 'next' if n == 91 else None}}}}
            return 0, json.dumps({'data': data}), ''
        issues, changes, error = bs.github(backlog.GitHubBacklog('o/app'), {}, fixtures.NOW, '', gh, include_open=True)
        self.assertEqual(error, '')
        self.assertEqual(set(changes), {'#91', '#92'})
        self.assertEqual(changes['#91'], bs._gh_change('#91', fixtures.gh_pr(91), ''))
        self.assertIn('states: OPEN', calls[1])
        self.assertIn('after: "next"', calls[2])

    def test_gitlab_includes_unnamed_open_changes_on_every_page_using_board_parser(self):
        calls = []
        def call(method, url, secret, timeout, body):
            query = body['query']
            calls.append(query)
            if 'state: opened' in query:
                n = 91 if 'after:' not in query else 92
                data = {'open': {'nodes': [fixtures.gl_mr(n)], 'pageInfo': {
                    'hasNextPage': n == 91, 'endCursor': 'next' if n == 91 else None}}}
            else:
                data = {'merged': {'nodes': []}}
            return {'data': {'p0': data}}, {}, ''
        home = backlog.Backlog('https://gl.example', 'o/app', 'TOKEN')
        with mock.patch.object(backlog, 'token', return_value='fixture'):
            issues, changes, errors = bs.gitlab(home, {}, set(), fixtures.NOW, '', call, include_open=True)
        self.assertEqual(errors, {})
        self.assertEqual(set(changes), {'!91', '!92'})
        self.assertEqual(changes['!91'].files, bs._gl_change(fixtures.gl_mr(91), '').files)
        self.assertEqual(sum('state: opened' in q for q in calls), 2)


class ChangeReferenceTest(unittest.TestCase):
    def task(self, text):
        return parse_tracker('## Tasks\n| item | owner | state | since | due | checklist |\n'
                             '|---|---|---|---|---|---|\n'
                             f'| Retry | Robin | waiting | 09:00 | | {text} |\n').tasks[0]

    def test_urls_are_case_insensitive_and_cannot_match_inside_another_host_or_path(self):
        home = backlog.GitHubBacklog('o/app')
        for url in ('https://fakegithub.com/o/app/pull/12', 'https://evil.test/github.com/o/app/pull/12',
                    'https://github.com/o/app/pull/123abc'):
            with self.subTest(url=url):
                self.assertEqual(bs.change_numbers(self.task(url), home), set())
        self.assertEqual(bs.change_numbers(self.task('HTTPS://GITHUB.COM/O/App/pull/12'), home), {12})

    def test_url_path_tail_can_acknowledge_a_merge(self):
        for home, url in ((backlog.GitHubBacklog('o/app'), 'https://github.com/o/app/pull/12/files'),
                          (backlog.Backlog('https://gl.example', 'o/app', 'TOKEN'),
                           'https://gl.example/o/app/-/merge_requests/12/diffs')):
            with self.subTest(url=url):
                self.assertEqual(bs.change_numbers(self.task(url + ' merged; suite pending'), home,
                                                    r'(?i:\W+merged\b)'), {12})


class WatcherFindingsTest(unittest.TestCase):
    def test_third_merged_finding_is_never_silently_cut(self):
        findings = [f'merged: Task {n} — PR #{n} merged into develop abcdef0; '
                    'write the task done once develop is green with it, or write "merged" after the ref'
                    for n in range(1, 7)]
        results = [mock.Mock(returncode=0, stdout='[]'),
                   mock.Mock(returncode=1, stdout='\n'.join(findings)),
                   mock.Mock(returncode=0, stdout='[]')]
        with mock.patch.object(start.subprocess, 'run', side_effect=results):
            message = start.check_once(Path('/workspace'), Path('/release'))
        self.assertIn(findings[2], message)
        self.assertIn(findings[5], message)
