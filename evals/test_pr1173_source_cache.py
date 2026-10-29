"""Regressions for source identity and offline issue caches on PR 1173."""
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import board_sources as bs
import module_graph
import source_page
from settings import Graph
from test_board_sources import gitlab_workspace, NOW
import issue_page


def change(ref='#42', url='https://github.com/o/app/pull/42', base='main'):
    return bs.Change(ref, url, 'change', 'open', False, None, False, 0, None, base, (), (), head='a' * 40)


class SourceCacheIdentity(unittest.TestCase):
    def test_sibling_fetch_is_unavailable_without_an_authorized_source(self):
        sibling = change('o/sibling#42', 'https://github.com/o/sibling/pull/42')
        with mock.patch.object(module_graph, 'git') as git:
            with self.assertRaisesRegex(ValueError, 'sibling'):
                module_graph.fetch(sibling, Path('/clone'))
            git.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            html = module_graph.section(sibling, Graph('clone', 'src'), Path(tmp), Path(tmp))
            self.assertIn('module graph is unavailable', html)
            self.assertIn('sibling pull request', html)
            with self.assertRaisesRegex(ValueError, 'sibling'):
                source_page.diff(sibling, Path(tmp) / 'clone', Path(tmp))

    def test_same_head_different_merge_base_or_repository_cannot_reuse_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            pages, clone = Path(tmp), Path(tmp) / 'clone'
            first = change()
            second = change('#43', 'https://github.com/o/other/pull/43', 'other')
            with mock.patch.object(module_graph, 'fetch', side_effect=['base1', 'base1', 'base2', 'base2']), \
                 mock.patch.object(module_graph, 'run', side_effect=['diff1', 'diff2', 'diff3']) as run:
                self.assertEqual(source_page.diff(first, clone, pages), ('base1', 'diff1'))
                self.assertEqual(source_page.diff(first, clone, pages), ('base1', 'diff1'))
                self.assertEqual(source_page.diff(replace(first, base='other'), clone, pages), ('base2', 'diff2'))
                self.assertEqual(source_page.diff(second, clone, pages), ('base2', 'diff3'))
                self.assertEqual(run.call_count, 3)

    def test_real_git_same_head_with_different_bases_has_different_diffs(self):
        import subprocess
        from test_module_graph import repo, commit, git, FIXTURE_ENV
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed = repo(root / 'seed', 'main')
            initial = git(seed, 'rev-parse', 'HEAD')
            middle = commit(seed, 'middle.txt')
            head = commit(seed, 'head.txt')
            git(seed, 'branch', 'old-base', initial)
            git(seed, 'branch', 'new-base', middle)
            remote = root / 'remote.git'
            subprocess.run(['git', 'clone', '-q', '--bare', str(seed), str(remote)], check=True, env=FIXTURE_ENV)
            git(seed, 'push', '-q', str(remote), f'{head}:refs/pull/42/head', f'{head}:refs/pull/43/head')
            clone = root / 'clone'
            subprocess.run(['git', 'clone', '-q', str(remote), str(clone)], check=True, env=FIXTURE_ENV)
            a = replace(change(base='old-base'), head=head)
            b = replace(change('#43', 'https://github.com/o/app/pull/43', 'new-base'), head=head)
            base_a, diff_a = source_page.diff(a, clone, root / 'pages')
            base_b, diff_b = source_page.diff(b, clone, root / 'pages')
            self.assertEqual((base_a, base_b), (initial, middle))
            self.assertIn('middle.txt', diff_a)
            self.assertNotIn('middle.txt', diff_b)
            self.assertIn('head.txt', diff_b)

    def test_graph_cache_includes_merge_base_repository_and_all_graph_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            pages, clone = Path(tmp), Path(tmp) / 'clone'
            first = change()
            graph = Graph('clone', 'src')
            calls = []
            def run(cmd):
                calls.append(cmd)
                Path(cmd[-1]).with_suffix('.mmd').write_text('graph')
                return 'views=1'
            with mock.patch.object(module_graph, 'fetch', side_effect=['base1', 'base2', 'base2', 'base2']), \
                 mock.patch.object(module_graph, 'run', side_effect=run):
                module_graph.draw(first, graph, clone, pages)
                module_graph.draw(replace(first, base='other'), graph, clone, pages)
                module_graph.draw(change('#43', 'https://github.com/o/other/pull/43', 'other'), graph, clone, pages)
                module_graph.draw(first, replace(graph, root='other', depth=3), clone, pages)
            self.assertEqual(len(calls), 4)


class OfflineIssueMigration(unittest.TestCase):
    def test_failed_refresh_migrates_verified_legacy_issues_and_preserves_host_identity(self):
        import shutil
        root = gitlab_workspace([('https://labs.example.test/team/other/-/issues/7', '!42')])
        self.addCleanup(shutil.rmtree, root, True)
        home = bs.backlog.Backlog('https://labs.example.test', 'team/app')
        issues = {
            'team/other#7': bs.Issue('team/other#7', 'https://labs.example.test/team/other/-/issues/7', 'Sibling issue', 'open', ()),
            'foreign.example/team/other#7': bs.Issue('foreign.example/team/other#7', 'https://foreign.example/team/other/-/issues/7', 'Foreign issue', 'open', ()),
            'team/other#9': bs.Issue('team/other#9', '', 'Unverified', 'open', ()),
        }
        bs._write(root / 'pages' / bs.CACHE, bs.Sources({}, issues, {}, (), {}, home))
        with mock.patch.object(bs, 'forge', return_value=(None, None, {'kanban': 'offline'})):
            loaded = bs.refresh(root, NOW)
        self.assertEqual(loaded.issues['gitlab:labs.example.test/team/other#7'].title, 'Sibling issue')
        self.assertEqual(loaded.issues['gitlab:foreign.example/team/other#7'].title, 'Foreign issue')
        self.assertNotIn('team/other#7', loaded.issues)
        self.assertNotIn('team/other#9', loaded.issues)
        tracker = (root / 'daily' / f'{NOW.date()}-tracker.md').read_text()
        self.assertIn('Sibling issue', issue_page.render(7, [(NOW.date(), tracker)], loaded, NOW))
        foreign = tracker.replace('https://labs.example.test/team/other/-/issues/7', 'https://foreign.example/team/other/-/issues/7')
        self.assertIn('Foreign issue', issue_page.render(7, [(NOW.date(), foreign)], loaded, NOW))
        self.assertNotIn('Sibling issue', issue_page.render(7, [(NOW.date(), foreign)], loaded, NOW))
        github = tracker.replace('https://labs.example.test/team/other/-/issues/7', 'https://github.com/team/other/issues/7')
        self.assertNotIn('Sibling issue', issue_page.render(7, [(NOW.date(), github)], loaded, NOW))

    def test_conflicting_entries_for_one_canonical_identity_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = bs.backlog.Backlog('https://labs.example.test', 'team/app')
            url = 'https://labs.example.test/team/other/-/issues/7'
            issues = {'team/other#7': bs.Issue('team/other#7', url, 'Old', 'open', ()),
                      'gitlab:labs.example.test/team/other#7': bs.Issue('gitlab:labs.example.test/team/other#7', url, 'New', 'closed', ())}
            bs._write(Path(tmp) / bs.CACHE, bs.Sources({}, issues, {}, (), {}, home))
            self.assertNotIn('gitlab:labs.example.test/team/other#7', bs.load(Path(tmp)).issues)

class PartialRefreshIdentity(unittest.TestCase):
    def test_home_change_does_not_mix_panels_even_after_offline_reload(self):
        import shutil
        import task_forge
        from settings import Lane
        from tracker import Task
        from test_board_sources import workspace
        old_home = bs.backlog.GitHubBacklog('o/old')
        new_home = bs.backlog.GitHubBacklog('o/new')
        old_issue = bs.Issue('#7', 'https://github.com/o/old/issues/7', 'old issue', 'open', ())
        new_issue = bs.Issue('#7', 'https://github.com/o/new/issues/7', 'new issue', 'open', ())
        old_change = replace(change('#42', 'https://github.com/o/old/pull/42'), issues=('#7',), state='merged')
        new_change = replace(change('#42', 'https://github.com/o/new/pull/42'), issues=('#7',), state='merged')
        task = Task('item', '', '', '', '', '', issue='#7', lane='build', stage='merge')
        lanes = {'build': Lane(('merge', 'done'))}
        for fresh_issues, fresh_changes in (({'#7': new_issue}, None), (None, {'#42': new_change})):
            with self.subTest(issues=fresh_issues is not None):
                root = workspace('GitHub issues; repo o/new')
                self.addCleanup(shutil.rmtree, root, True)
                bs._write(root / 'pages' / bs.CACHE, bs.Sources(
                    {'kanban': NOW, 'merge requests': NOW}, {'#7': old_issue}, {'#42': old_change}, (), {}, old_home))
                with mock.patch.object(bs, 'forge', return_value=(fresh_issues, fresh_changes, {})):
                    got = bs.refresh(root, NOW)
                self.assertEqual(got.home, new_home)
                self.assertEqual(got.issues, fresh_issues or {})
                self.assertEqual(got.changes, fresh_changes or {})
                self.assertEqual(task_forge.task_changes(task, got), (new_change,) if fresh_changes else ())
                self.assertEqual(task_forge.effective_stage(task, got, lanes), 'done' if fresh_changes else 'merge')
                self.assertNotIn('merge requests' if fresh_changes is None else 'kanban', got.fetched)
                self.assertEqual(bs.load(root / 'pages').issues, got.issues)
                self.assertEqual(bs.load(root / 'pages').changes, got.changes)
                with mock.patch.object(bs, 'forge', return_value=(None, None, {'kanban': 'offline'})):
                    offline = bs.refresh(root, NOW)
                self.assertEqual((offline.home, offline.issues, offline.changes), (got.home, got.issues, got.changes))

    def test_homeless_bare_issue_is_only_kept_under_verified_full_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            issues = {
                '#7': bs.Issue('#7', 'https://github.com/foreign/repo/issues/7', 'foreign', 'open', ()),
                '#8': bs.Issue('#8', '', 'unknown', 'open', ()),
                '#9': bs.Issue('#9', 'https://github.com/foreign/repo/issues/10', 'mismatch', 'open', ()),
            }
            bs._write(Path(tmp) / bs.CACHE, bs.Sources({}, issues, {}, (), {}))
            loaded = bs.load(Path(tmp))
            self.assertEqual(set(loaded.issues), {'foreign/repo#7'})
            self.assertEqual(loaded.issues['foreign/repo#7'].ref, 'foreign/repo#7')

class ReviewRegressions(unittest.TestCase):
    def test_homeless_bare_closer_cannot_finish_bare_tracker_task(self):
        import task_forge
        from settings import Lane
        from tracker import Task
        task = Task('item', '', '', '', '', '', issue='#7', lane='build', stage='merge')
        merged = replace(change(), state='merged', issues=('#7',))
        with tempfile.TemporaryDirectory() as tmp:
            bs._write(Path(tmp) / bs.CACHE, bs.Sources({}, {}, {'#42': merged}, (), {}))
            sources = bs.load(Path(tmp))
            self.assertEqual(task_forge.task_changes(task, sources), ())
            self.assertEqual(task_forge.effective_stage(task, sources, {'build': Lane(('merge', 'done'))}), 'merge')
            known = replace(sources, home=bs.backlog.GitHubBacklog('o/app'))
            self.assertEqual(task_forge.effective_stage(task, known, {'build': Lane(('merge', 'done'))}), 'done')

    def test_gitlab_credentials_change_keeps_stale_panels_but_project_change_does_not(self):
        import shutil
        from test_board_sources import workspace
        old_home = bs.backlog.Backlog('https://labs.example.test', 'team/app', env='OLD', account='old')
        issue = bs.Issue('#7', 'https://labs.example.test/team/app/-/issues/7', 'old', 'open', ())
        old_change = replace(change('!42', 'https://labs.example.test/team/app/-/merge_requests/42'), issues=('#7',))
        for host, project, retained in [('labs.example.test', 'team/app', True),
                                        ('labs.example.test', 'team/other', False),
                                        ('other.example.test', 'team/app', False)]:
            for fresh_issues in (True, False):
                root = workspace(f'GitLab; host https://{host}; project {project}; token env NEW')
                self.addCleanup(shutil.rmtree, root, True)
                bs._write(root / 'pages' / bs.CACHE, bs.Sources({'kanban': NOW, 'merge requests': NOW}, {'#7': issue}, {'!42': old_change}, (), {}, old_home))
                result = ({}, None, {'merge requests': 'offline'}) if fresh_issues else (None, {}, {'kanban': 'offline'})
                with mock.patch.object(bs, 'forge', return_value=result):
                    got = bs.refresh(root, NOW)
                self.assertEqual(bool(got.changes if fresh_issues else got.issues), retained)
                self.assertEqual(bs.load(root / 'pages').home, got.home)

    def test_offline_cached_diff_and_graph_are_explicitly_stale_and_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            pages, clone, graph = Path(tmp), Path(tmp) / 'clone', Graph('clone', 'src')
            first = change()
            def run(cmd):
                if cmd[0] == 'branch-graph':
                    Path(cmd[-1]).with_suffix('.mmd').write_text('graph TD\n A-->B')
                    return 'views=1'
                return 'diff text'
            with mock.patch.object(module_graph, 'fetch', return_value='base1'), mock.patch.object(module_graph, 'run', side_effect=run):
                source_page.diff_result(first, clone, pages)
                module_graph.draw_result(first, graph, clone, pages)
            with mock.patch.object(module_graph, 'fetch', side_effect=ValueError('offline')), mock.patch.object(module_graph, 'run') as run:
                self.assertEqual(source_page.diff_result(first, clone, pages), ('base1', 'diff text', True))
                views, _, stale = module_graph.draw_result(first, graph, clone, pages)
                self.assertTrue(stale)
                self.assertIn('A-->B', views[0])
                self.assertIn('stale', module_graph.section(first, graph, pages, Path(tmp)).lower())
                for other in (replace(first, base='other'), replace(first, head='b' * 40), change('#43', 'https://github.com/o/other/pull/43')):
                    with self.assertRaisesRegex(ValueError, 'offline'):
                        source_page.diff_result(other, clone, pages)
                    with self.assertRaisesRegex(ValueError, 'offline'):
                        module_graph.draw_result(other, graph, clone, pages)
                with self.assertRaisesRegex(ValueError, 'offline'):
                    module_graph.draw_result(first, replace(graph, root='other'), clone, pages)
                index = pages / source_page.CACHE / '.comparisons' / module_graph.cache_key(first, clone, '')
                index.write_text('{"base":"other", "key":"wrong"}')
                with self.assertRaisesRegex(ValueError, 'offline'):
                    source_page.diff_result(first, clone, pages)
                sibling = change('o/sibling#42', 'https://github.com/o/sibling/pull/42')
                with self.assertRaisesRegex(ValueError, 'sibling'):
                    source_page.diff_result(sibling, clone, pages)
                run.assert_not_called()
