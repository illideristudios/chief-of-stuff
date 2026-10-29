"""Regression coverage for task and forge identity across projects and hosts."""
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import test_issue_page as tip
import board_sources as bs
import issue_page
import source_page
import task_forge
from tracker import parse_tracker
from test_board_sources import gl_mr


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.home = bs.backlog.Backlog("https://labs.example.test", "team/app")

    def test_gitlab_closing_references_are_host_safe_and_shorthand_matches(self):
        description = ("Fixes #7, team/app#8, team/other#9, team/nested/other#10 and "
                       "https://foreign.example/team/app/-/issues/11")
        change = bs._gl_change({**gl_mr(42), "description": description}, "", self.home)
        src = bs.Sources({}, {}, {change.ref: change}, (), {}, home=self.home)
        def matches(ref):
            row = tip.TODAY_START.replace("#109", ref)
            task = parse_tracker(row).tasks[0]
            return task_forge.task_changes(task, src)
        for ref in ("#7", "team/app#8", "labs.example.test/team/app#8", "team/other#9", "team/nested/other#10"):
            with self.subTest(ref=ref):
                self.assertEqual(matches(ref), (change,))
        for ref in ("#9", "team/another#9", "https://foreign.example/team/app/-/issues/11", "#11"):
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
            self.assertEqual(task_forge.source_key(key, home), "foreign.example:8443/team/app#7")

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
                html = render(109, trackers, src, tip.NOW)
                self.assertIn("Home", html)
                self.assertNotIn("Other issue", html)
                self.assertNotIn("Other change", html)
                self.assertNotIn("Old task", html)
        other_row = next(line for line in old.splitlines() if line.startswith("| Old task |"))
        ambiguous = [(tip.TODAY, current.replace("\n\n## File ownership", "\n" + other_row + "\n\n## File ownership"))]
        for render in (issue_page.render, source_page.render):
            self.assertIsNone(render(109, ambiguous, src, tip.NOW))
