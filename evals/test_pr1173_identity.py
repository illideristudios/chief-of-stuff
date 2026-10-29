"""Regression coverage for task and forge identity across projects and hosts."""
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import test_issue_page as tip
import board_sources as bs
import issue_page
import source_page
import task_forge
from tracker import parse_tracker
from test_board_sources import gl_mr


def configured(home):
    return replace(tip.BoardTest().cfg(), backlog=home)


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.home = bs.backlog.Backlog("https://labs.example.test", "team/app")

    def test_malformed_qualified_references_never_select_home_forge_facts(self):
        home = bs.backlog.GitHubBacklog("example/repo")
        merged = bs.Change("#12", "https://github.com/example/repo/pull/12", "home change", "merged", False,
                           "passed", True, 1, tip.NOW, "main", (), ("#7",))
        closed = bs.Issue("#7", "https://github.com/example/repo/issues/7", "home issue", "closed", (), closed_at=tip.NOW)
        src = bs.Sources({}, {"#7": closed}, {merged.ref: merged}, (), {}, home=home)
        for ref in ("github:bad#7", "https://foreign.example/unsupported/7"):
            with self.subTest(ref=ref):
                task = parse_tracker(tip.TODAY_START.replace("#109", ref)).tasks[0]
                self.assertNotIn("#7", task_forge.lookup_keys((ref,), home))
                self.assertEqual(task_forge.task_changes(task, src), ())
                self.assertEqual(task_forge.forge_ends([task], src, tip.NOW.tzinfo), {})
        valid = parse_tracker(tip.TODAY_START.replace("#109", "#7")).tasks[0]
        self.assertEqual(task_forge.task_changes(valid, src), (merged,))
        self.assertIn(valid.name, task_forge.forge_ends([valid], src, tip.NOW.tzinfo))

    def test_gitlab_closing_references_are_host_safe_and_shorthand_matches(self):
        description = ("Fixes #7, team/app#8, team/other#9, team/nested/other#10 and "
                       "https://foreign.example/team/app/-/issues/11")
        change = bs._gl_change({**gl_mr(42), "description": description}, "", self.home)
        src = bs.Sources({}, {}, {change.ref: change}, (), {}, home=self.home)
        def matches(ref):
            row = tip.TODAY_START.replace("#109", ref)
            task = parse_tracker(row).tasks[0]
            return task_forge.task_changes(task, src)
        for ref in ("#7", "https://labs.example.test/team/app/-/issues/8", "gitlab:labs.example.test/team/other#9", "https://labs.example.test/team/nested/other/-/issues/10"):
            with self.subTest(ref=ref):
                self.assertEqual(matches(ref), (change,))
        for ref in ("#9", "team/app#8", "team/other#9", "team/another#9", "https://foreign.example/team/app/-/issues/11", "#11"):
            with self.subTest(ref=ref):
                self.assertEqual(matches(ref), ())
        # Caches produced before this fix can still contain a raw foreign closing URL.
        from dataclasses import replace
        foreign = "https://foreign.example/team/app/-/issues/11"
        src = replace(src, changes={change.ref: replace(change, issues=(foreign,))})
        self.assertEqual(matches(foreign), ())
        foreign_tracker = tip.TODAY_START.replace("#109", foreign)
        for render in (issue_page.render, source_page.render):
            html = render(11, [(tip.TODAY, foreign_tracker)], src, tip.NOW)
            self.assertIsNotNone(html)
            self.assertNotIn("MR 42", html)

    def test_foreign_port_store_key_never_selects_home_change(self):
        home = bs.backlog.Backlog("https://labs.example.test", "team/app")
        key = "gitlab:foreign.example:8443/team/app#7"
        self.assertNotIn("#7", task_forge.lookup_keys((key,), home))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE work_item (issue_key TEXT)")
                db.execute("CREATE TABLE resume (issue_key TEXT, data TEXT)")
                db.execute("INSERT INTO resume VALUES (?, ?)", (key, json.dumps({"merges": [{"request": 99}]})))
                db.execute("INSERT INTO resume VALUES (?, ?)", ("gitlab:labs.example.test/team/app#7", json.dumps({"merges": [{"request": 12}]})))
                db.execute("INSERT INTO resume VALUES (?, ?)", ("gitlab:labs.example.test/team/nested/other#7", json.dumps({"merges": [{"request": 13}]})))
            self.assertEqual(bs._store_targets(path, home), ({}, {12}))
            self.assertEqual(task_forge.source_key(key, home), key)

    def test_numeric_pages_use_current_tracker_identity_without_historical_union(self):
        home = bs.backlog.GitHubBacklog("example/repo")
        old = tip.TODAY_START.replace("#109", "example/other#109").replace("Upload size limit", "Old task")
        current = tip.TODAY_START.replace("Upload size limit", "Current task")
        src = bs.Sources({}, {
            "#109": bs.Issue("#109", "https://github.com/example/repo/issues/109", "Home issue", "open", ()),
            "example/other#109": bs.Issue("example/other#109", "https://github.com/example/other/issues/109", "Other issue", "open", ()),
        }, {
            "#1": bs.Change("#1", "https://github.com/example/repo/pull/1", "Home change", "open", False, "passed", True, 1, None, "main", (), ("#109",)),
            "#2": bs.Change("#2", "https://github.com/example/repo/pull/2", "Other change", "open", False, "passed", True, 1, None, "main", (), ("example/other#109",)),
        }, (), {}, home=home)
        trackers = [(tip.TODAY - timedelta(days=1), old), (tip.TODAY, current)]
        for render in (issue_page.render, source_page.render):
            with self.subTest(render=render.__module__):
                html = (render(109, trackers, src, tip.NOW, cfg=configured(home)) if render is issue_page.render
                        else render(109, trackers, src, tip.NOW))
                self.assertIn("Home", html)
                self.assertNotIn("Other issue", html)
                self.assertNotIn("Other change", html)
                self.assertNotIn("Old task", html)
        other_row = next(line for line in old.splitlines() if line.startswith("| Old task |"))
        ambiguous = [(tip.TODAY, current.replace("\n\n## File ownership", "\n" + other_row + "\n\n## File ownership"))]
        for render in (issue_page.render, source_page.render):
            html = (render(109, ambiguous, src, tip.NOW, cfg=configured(home)) if render is issue_page.render
                    else render(109, ambiguous, src, tip.NOW))
            self.assertIsNone(html)

    def test_historical_nested_projects_do_not_share_flow_or_history(self):
        home = self.home
        old = (tip.TODAY_START.replace('#109', 'team/nested/old#109')
               .replace('Upload size limit', 'Old task')
               .replace('- 00:01 opened the day', '- 00:03 stage: Old task → implement'))
        current = (tip.TODAY_START.replace('#109', 'team/nested/new#109')
                   .replace('Upload size limit', 'New task')
                   .replace('- 00:01 opened the day', '- 00:05 stage: New task → pr'))
        src = bs.Sources({}, {}, {}, (), {}, home=home)
        html = issue_page.render(109, [(tip.YESTERDAY, old), (tip.TODAY, current)], src, tip.NOW, tip.LANES, configured(src.home))
        self.assertIsNotNone(html)
        self.assertIn('New task', html)
        self.assertNotIn('Old task', html)
        self.assertNotIn('00:03', html)
        self.assertIn('00:05', html)

    def test_foreign_qualified_port_label_does_not_match_home_change(self):
        key = 'foreign.example:8443/team/app#7'
        change = bs._gl_change({**gl_mr(42), 'description': 'Fixes #7'}, '', self.home)
        src = bs.Sources({}, {}, {change.ref: change}, (), {}, home=self.home)
        tracker = tip.TODAY_START.replace('#109', key)
        task = parse_tracker(tracker).tasks[0]
        self.assertEqual(task_forge.source_key(key, self.home), key)
        self.assertEqual(task_forge.task_changes(task, src), ())
        html = issue_page.render(7, [(tip.TODAY, tracker)], src, tip.NOW, tip.LANES)
        self.assertNotIn('MR 42', html)

    def test_gitlab_shorthand_refresh_fetches_issue_and_matches_change(self):
        from test_board_sources import gitlab_workspace, refresh_gitlab, FakeGitLab, gl_issue, NOW
        import shutil
        root = gitlab_workspace([('https://labs.example.test/team/nested/other/-/issues/7', '!42')])
        self.addCleanup(shutil.rmtree, root, True)
        issue = {**gl_issue(7), 'webUrl': 'https://labs.example.test/team/nested/other/-/issues/7', 'title': 'Sibling issue'}
        fake = FakeGitLab(issues=[issue], mrs=[{**gl_mr(42), 'description': 'Fixes team/nested/other#7'}])
        src = refresh_gitlab(root, NOW, fake)
        self.assertIn('gitlab:labs.example.test/team/nested/other#7', src.issues)
        self.assertTrue(any('team/nested/other' in q for q in fake.queries))
        tracker = (root / 'daily' / f'{NOW.date()}-tracker.md').read_text()
        html = issue_page.render(7, [(NOW.date(), tracker)], src, NOW, cfg=bs.read_config(root))
        self.assertIn('Sibling issue', html)
        self.assertIn('MR 42', html)

    def test_sibling_github_pr_bare_closer_stays_in_origin_repo(self):
        from test_board_sources import gh_pr, gh_issue, NOW
        home = bs.backlog.GitHubBacklog('o/app')
        pr = {**gh_pr(42), 'url': 'https://github.com/o/sibling/pull/42', 'body': 'Fixes #7',
              'closingIssuesReferences': {'nodes': [{'number': 7, 'repository': {'name': 'sibling', 'owner': {'login': 'o'}}}]}}
        def gh(args):
            return 0, json.dumps({'data': {'r0': {'n7': gh_issue(7)}, 'r1': {'n42': pr}, 'merged': {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}, 'rateLimit': {'cost': 1, 'remaining': 4900, 'limit': 5000}}}), ''
        issues, changes, error = bs.github(home, {'o/app': {7}, 'o/sibling': {42}}, NOW, '', gh)
        self.assertEqual(error, {})
        self.assertEqual(changes['o/sibling#42'].issues, ('o/sibling#7',))
        src = bs.Sources({}, issues, changes, (), {}, home=home)
        home_task = parse_tracker(tip.TODAY_START.replace('#109', '#7')).tasks[0]
        sibling_task = parse_tracker(tip.TODAY_START.replace('#109', 'o/sibling#7')).tasks[0]
        self.assertEqual(task_forge.task_changes(home_task, src), ())
        self.assertEqual(task_forge.task_changes(sibling_task, src), (changes['o/sibling#42'],))
        sibling_tracker = tip.TODAY_START.replace('#109', 'o/sibling#7')
        home_tracker = tip.TODAY_START.replace('#109', '#7')
        self.assertIn('PR 42', issue_page.render(7, [(tip.TODAY, sibling_tracker)], src, tip.NOW, cfg=configured(home)))
        self.assertNotIn('PR 42', issue_page.render(7, [(tip.TODAY, home_tracker)], src, tip.NOW, cfg=configured(home)))
        body_only = {**pr, 'closingIssuesReferences': {'nodes': []}}
        self.assertEqual(bs._gh_change('o/sibling#42', body_only, '', home.repo, 'o/sibling').issues, ('o/sibling#7',))

    def test_reused_alias_history_and_ambiguous_moves_are_not_attributed(self):
        old = (tip.TODAY_START.replace('#109', 'team/nested/old#109')
               .replace('- 00:01 opened the day', '- 00:03 stage: Upload size limit → implement'))
        current = (tip.TODAY_START.replace('#109', 'team/nested/new#109')
                   .replace('- 00:01 opened the day', '- 00:05 stage: Upload size limit → pr'))
        src = bs.Sources({}, {}, {}, (), {}, home=self.home)
        html = issue_page.render(109, [(tip.YESTERDAY, old), (tip.TODAY, current)], src, tip.NOW, tip.LANES, configured(src.home))
        self.assertNotIn('00:03', html)
        self.assertIn('00:05', html)
        duplicate = next(line for line in old.splitlines() if line.startswith('| Upload size limit |'))
        ambiguous = current.replace('\n\n## File ownership', '\n' + duplicate + '\n\n## File ownership')
        self.assertIsNone(issue_page.render(109, [(tip.TODAY, ambiguous)], src, tip.NOW, tip.LANES))

    def test_refresh_fetches_explicit_gitlab_project_but_not_foreign_host(self):
        from test_board_sources import gitlab_workspace, refresh_gitlab, FakeGitLab, gl_issue, NOW
        import shutil
        root = gitlab_workspace([('gitlab:labs.example.test/team/other#7', '!42'), ('foreign.example:8443/team/app#8', '')])
        self.addCleanup(shutil.rmtree, root, True)
        issue = {**gl_issue(7), 'webUrl': 'https://labs.example.test/team/other/-/issues/7'}
        fake = FakeGitLab(issues=[issue], mrs=[{**gl_mr(42), 'description': 'Fixes team/other#7'}])
        src = refresh_gitlab(root, NOW, fake)
        self.assertIn('gitlab:labs.example.test/team/other#7', src.issues)
        self.assertIn('gitlab:labs.example.test/team/other#7', src.changes['!42'].issues)
        self.assertTrue(any('team/other' in q for q in fake.queries))
        self.assertFalse(any('foreign.example' in q or '"8"' in q for q in fake.queries))
        self.assertTrue(all(url == 'https://labs.example.test/api/graphql' for _, url, _ in fake.posts))

    def test_home_port_and_other_port_are_distinct_through_refresh(self):
        from test_board_sources import gitlab_workspace, refresh_gitlab, FakeGitLab, gl_issue, NOW
        import shutil
        root = gitlab_workspace([('#7', '!42'), ('labs.example.test:8444/team/app#8', '')])
        self.addCleanup(shutil.rmtree, root, True)
        config = root / 'CLAUDE.md'
        config.write_text(config.read_text().replace('https://labs.example.test;', 'https://labs.example.test:8443;'))
        issue = {**gl_issue(7), 'webUrl': 'https://labs.example.test:8443/team/app/-/issues/7'}
        fake = FakeGitLab(issues=[issue], mrs=[{**gl_mr(42), 'description': 'Fixes #7'}])
        src = refresh_gitlab(root, NOW, fake)
        self.assertEqual(src.issues['#7'].url, issue['webUrl'])
        self.assertEqual(task_forge.source_key(issue['webUrl'], src.home), '#7')
        self.assertIsNotNone(bs.backlog.issue_ref(issue['webUrl'], src.home))
        self.assertEqual(src.changes['!42'].issues, ('#7',))
        self.assertEqual(task_forge.source_key('labs.example.test:8444/team/app#8', src.home), 'labs.example.test:8444/team/app#8')
        self.assertTrue(all(url == 'https://labs.example.test:8443/api/graphql' for _, url, _ in fake.posts))
        self.assertFalse(any('8444' in q or '"8"' in q for q in fake.queries))

    def test_nested_shorthand_stays_distinct_when_cached_home_is_missing(self):
        old = (tip.TODAY_START.replace('#109', 'team/nested/old#109')
               .replace('Upload size limit', 'Old task')
               .replace('- 00:01 opened the day', '- 00:03 stage: Old task → implement'))
        current = (tip.TODAY_START.replace('#109', 'team/nested/new#109')
                   .replace('Upload size limit', 'New task')
                   .replace('- 00:01 opened the day', '- 00:05 stage: New task → pr'))
        src = bs.Sources({}, {}, {}, (), {})
        html = issue_page.render(109, [(tip.YESTERDAY, old), (tip.TODAY, current)], src, tip.NOW, tip.LANES)
        self.assertIn('New task', html)
        self.assertNotIn('Old task', html)
        self.assertIn('00:05', html)
        self.assertNotIn('00:03', html)
        duplicate = next(line for line in old.splitlines() if line.startswith('| Old task |'))
        ambiguous = current.replace('\n\n## File ownership', '\n' + duplicate + '\n\n## File ownership')
        self.assertIsNone(issue_page.render(109, [(tip.TODAY, ambiguous)], src, tip.NOW, tip.LANES))

class ReviewFindingsTest(unittest.TestCase):
    def test_ambiguous_labels_fail_closed_and_explicit_identities_stay_distinct(self):
        home = bs.backlog.Backlog('https://home', 'team/app')
        # The same spelling could be a foreign host or a nested project on home.
        self.assertIsNone(bs.backlog.issue_ref('foreign/team/app#7', home, any_host=True))
        self.assertIsNone(bs.backlog.issue_ref('group.with.dots/team/app#7', home, any_host=True))
        foreign = bs.backlog.issue_ref('https://foreign/team/app/-/issues/7', home, any_host=True)
        nested = bs.backlog.issue_ref('https://home/group.with.dots/team/app/-/issues/7', home)
        self.assertEqual((foreign.host, foreign.repo), ('foreign', 'team/app'))
        self.assertEqual((nested.host, nested.repo), ('home', 'group.with.dots/team/app'))
        self.assertNotEqual(foreign.label(home), nested.label(home))
        self.assertEqual(task_forge.source_key('gitlab:foreign/team/app#7', home), foreign.label(home))
        self.assertEqual(task_forge.source_key('gitlab:home/group.with.dots/team/app#7', home), nested.label(home))
        self.assertIn(nested.label(home), task_forge.lookup_keys((nested.label(home),), home))
        self.assertEqual(bs.backlog.issue_ref(nested.label(home), home), nested)
        self.assertIsNone(bs.backlog.issue_ref(foreign.label(home), home))
        self.assertIsNone(bs.backlog.issue_ref('https://foreign/team/app/-/issues/7', home))
        self.assertEqual(bs.backlog.issue_ref('team/other#7', home).host, 'github.com')
        self.assertNotIn(nested.label(home), task_forge.lookup_keys(('group.with.dots/team/app#7',), home))

    def test_dotted_nested_project_url_refreshes_without_fetching_foreign_host(self):
        from test_board_sources import gitlab_workspace, refresh_gitlab, FakeGitLab, gl_issue, NOW
        import shutil
        root = gitlab_workspace([('https://labs.example.test/group.with.dots/team/app/-/issues/7', '!42'),
                                 ('https://foreign/team/app/-/issues/8', ''), ('foreign/team/app#9', '')])
        self.addCleanup(shutil.rmtree, root, True)
        issue = {**gl_issue(7), 'webUrl': 'https://labs.example.test/group.with.dots/team/app/-/issues/7'}
        fake = FakeGitLab(issues=[issue], mrs=[{**gl_mr(42), 'description': 'Fixes group.with.dots/team/app#7'}])
        src = refresh_gitlab(root, NOW, fake)
        key = 'gitlab:labs.example.test/group.with.dots/team/app#7'
        self.assertIn(key, src.issues)
        self.assertIn(key, src.changes['!42'].issues)
        self.assertTrue(any('group.with.dots/team/app' in q for q in fake.queries))
        self.assertFalse(any('foreign/team/app' in q or '"8"' in q or '"9"' in q for q in fake.queries))

    def test_sibling_pr_url_is_fetched_by_refresh_in_its_own_repo(self):
        from test_board_sources import workspace, gh_pr, gh_issue, NOW
        import shutil
        root = workspace('GitHub issues; repo o/app', issue='o/sibling#7', mr='https://github.com/o/sibling/pull/42')
        self.addCleanup(shutil.rmtree, root, True)
        calls = []
        def gh(args):
            query = args[-1]
            calls.append(query)
            pr = {**gh_pr(42), 'url': 'https://github.com/o/sibling/pull/42', 'body': 'Fixes #7'}
            return 0, json.dumps({'data': {'r0': {'n118': gh_issue(118), 'n58': gh_pr(58)},
                                             'r1': {'n7': gh_issue(7), 'n42': pr}, 'merged': {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}, 'rateLimit': {'cost': 1, 'remaining': 4900, 'limit': 5000}}}), ''
        src = bs.refresh(root, NOW, gh=gh)
        self.assertTrue(any('n42: issueOrPullRequest(number: 42)' in query for query in calls))
        self.assertEqual(src.changes['o/sibling#42'].issues, ('o/sibling#7',))
        sibling = parse_tracker(tip.TODAY_START.replace('#109', 'o/sibling#7')).tasks[0]
        local = parse_tracker(tip.TODAY_START.replace('#109', '#7')).tasks[0]
        self.assertEqual(task_forge.task_changes(sibling, src), (src.changes['o/sibling#42'],))
        self.assertEqual(task_forge.task_changes(local, src), ())

    def test_move_on_day_after_task_removed_remains_in_history(self):
        old = tip.TODAY_START.replace('- 00:01 opened the day', '- 00:03 stage: Upload size limit → implement')
        row = next(line for line in old.splitlines() if line.startswith('| Upload size limit |'))
        current = tip.TODAY_START.replace(row + '\n', '').replace('- 00:01 opened the day',
                                                                   '- 00:05 stage: Upload size limit → pr')
        src = bs.Sources({}, {}, {}, (), {}, home=bs.backlog.GitHubBacklog('o/app'))
        html = issue_page.render(109, [(tip.YESTERDAY, old), (tip.TODAY, current)], src, tip.NOW, tip.LANES)
        self.assertIn('00:03', html)
        self.assertIn('00:05', html)
        other = row.replace('#109', 'o/other#109')
        reused = current.replace('\n\n## File ownership', '\n' + other + '\n\n## File ownership')
        html = issue_page.render('github:o/app#109', [(tip.YESTERDAY, old), (tip.TODAY, reused)], src, tip.NOW, tip.LANES, configured(src.home))
        self.assertNotIn('00:05', html)
        ambiguous_old = old.replace('\n\n## File ownership', '\n' + other + '\n\n## File ownership')
        html = issue_page.render('github:o/app#109', [(tip.YESTERDAY, ambiguous_old), (tip.TODAY, current)], src, tip.NOW, tip.LANES, configured(src.home))
        self.assertNotIn('00:05', html)


class IndependentReviewRegressions(unittest.TestCase):
    def test_refresh_does_not_fetch_github_shorthand_as_gitlab(self):
        from test_board_sources import gitlab_workspace, refresh_gitlab, FakeGitLab, gl_mr, NOW
        import shutil
        root = gitlab_workspace([('team/other#7', '!42'), ('gitlab:labs.example.test/team/other#8', '')])
        self.addCleanup(shutil.rmtree, root, True)
        fake = FakeGitLab(mrs=[{**gl_mr(42), 'description': 'Fixes team/other#7'}])
        src = refresh_gitlab(root, NOW, fake)
        tracker = (root / 'daily' / f'{NOW.date()}-tracker.md').read_text()
        github_task = parse_tracker(tracker).tasks[0]
        self.assertEqual(task_forge.task_changes(github_task, src), ())
        self.assertFalse(any('"7"' in q and 'team/other' in q for q in fake.queries))
        self.assertTrue(any('"8"' in q and 'team/other' in q for q in fake.queries))
        self.assertEqual(bs.backlog.issue_ref(github_task.issue, src.home).host, 'github.com')

    def test_failed_refresh_does_not_match_old_gitlab_closer_to_github(self):
        from test_board_sources import gitlab_workspace, NOW, gl_mr
        from unittest import mock
        import shutil
        root = gitlab_workspace([('https://github.com/team/other/issues/7', '!42')])
        self.addCleanup(shutil.rmtree, root, True)
        home = bs.backlog.Backlog('https://labs.example.test', 'team/app')
        from dataclasses import replace
        change = replace(bs._gl_change(gl_mr(42), '', home), issues=('team/other#7',))
        bs._write(root / 'pages' / bs.CACHE, bs.Sources({}, {}, {'!42': change}, (), {}, home))
        with mock.patch.object(bs, 'forge', return_value=(None, None, {'merge requests': 'offline'})):
            src = bs.refresh(root, NOW)
        tracker = (root / 'daily' / f'{NOW.date()}-tracker.md').read_text()
        task = parse_tracker(tracker).tasks[0]
        self.assertEqual(task_forge.task_changes(task, src), ())
        for render in (issue_page.render, source_page.render):
            html = render(7, [(NOW.date(), tracker)], src, NOW)
            self.assertNotIn('MR 42', html)
        gitlab_tracker = tracker.replace('https://github.com/team/other/issues/7', 'gitlab:labs.example.test/team/other#7')
        self.assertEqual(task_forge.task_changes(parse_tracker(gitlab_tracker).tasks[0], src), (src.changes["!42"],))
        self.assertIn('MR 42', issue_page.render(7, [(NOW.date(), gitlab_tracker)], src, NOW, cfg=bs.read_config(root)))
        foreign_tracker = tracker.replace('https://github.com/team/other/issues/7', 'gitlab:foreign.example/team/other#7')
        self.assertEqual(task_forge.task_changes(parse_tracker(foreign_tracker).tasks[0], src), ())

    def test_cached_gitlab_reference_without_verifiable_forge_fails_closed(self):
        from test_board_sources import gl_mr
        from dataclasses import asdict, replace
        home = bs.backlog.Backlog('https://labs.example.test', 'team/app')
        old = replace(bs._gl_change(gl_mr(42), '', home), url='', issues=('team/other#7',))
        loaded = bs._change_of(asdict(old), home)
        self.assertNotIn('team/other#7', loaded.issues)

    def test_cached_bare_closer_keeps_mr_forge_when_cache_home_changes(self):
        from test_board_sources import gl_mr
        from dataclasses import asdict, replace
        gl_home = bs.backlog.Backlog('https://labs.example.test', 'team/app')
        old = replace(bs._gl_change(gl_mr(42), '', gl_home), issues=('#7',))
        loaded = bs._change_of(asdict(old), bs.backlog.GitHubBacklog('team/app'))
        self.assertEqual(loaded.issues, ('gitlab:labs.example.test/team/app#7',))
        unknown = bs._change_of(asdict(replace(old, url='')), gl_home)
        self.assertEqual(unknown.issues, ())

    def test_sibling_pr_discovery_to_source_page_loads_its_review_notes(self):
        from test_board_sources import workspace, gh_pr, gh_issue, NOW
        from unittest import mock
        from types import SimpleNamespace
        import shutil
        root = workspace('GitHub issues; repo o/app', issue='o/sibling#7', mr='https://github.com/o/sibling/pull/42')
        self.addCleanup(shutil.rmtree, root, True)
        def gh(args):
            pr = {**gh_pr(42), 'url': 'https://github.com/o/sibling/pull/42', 'body': 'Fixes #7'}
            return 0, json.dumps({'data': {'r0': {'n118': gh_issue(118), 'n58': gh_pr(58)},
                                             'r1': {'n7': gh_issue(7), 'n42': pr}, 'merged': {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}, 'rateLimit': {'cost': 1, 'remaining': 4900, 'limit': 5000}}}), ''
        bs.refresh(root, NOW, gh=gh)
        tracker = (root / 'daily' / f'{NOW.date()}-tracker.md').read_text()
        queries = []
        def notes_gh(args):
            queries.append(args[-1])
            thread = {'path': 'src/a.py', 'line': 1, 'diffSide': 'RIGHT', 'isResolved': False,
                      'comments': {'nodes': [{'author': {'login': 'reviewer'}, 'body': 'Sibling review note'}]}}
            return 0, json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {'nodes': [thread]}}}}}), ''
        real_notes = source_page.notes
        cfg = bs.read_config(root)
        with mock.patch.object(source_page, 'inputs', return_value=(cfg, NOW, [(NOW.date(), tracker)], SimpleNamespace(graph={}))), \
             mock.patch.object(source_page, 'notes', side_effect=lambda change, home: real_notes(change, home, gh=notes_gh)):
            path = source_page.write(root, root / 'pages', 7)
        self.assertIn('Sibling review note', path.read_text())
        self.assertIn('owner: "o", name: "sibling"', queries[0])
        self.assertIn('pullRequest(number: 42)', queries[0])
        home_change = bs._gh_change('#58', gh_pr(58), '', 'o/app')
        self.assertEqual(real_notes(home_change, cfg.backlog, gh=notes_gh)[1], '')
        self.assertIn('owner: "o", name: "app"', queries[-1])
        self.assertIn('pullRequest(number: 58)', queries[-1])
