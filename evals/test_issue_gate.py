"""No work without an issue (design decision 3, plan M2 item 2). Work with no forge issue files the issue first, then writes
exactly one store row keyed by it; a workspace with no `Backlog:` line is refused, and the setup is offered."""

from __future__ import annotations

import sys
import contextlib
import importlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))

import backlog  # noqa: E402
import forge  # noqa: E402
import issue_gate  # noqa: E402
import store  # noqa: E402
import supervisor  # noqa: E402
import test_forge as tf  # noqa: E402  (module-qualified: don't re-collect its TestCases)

SCOPE = "## File ownership\n- write: `src/`"

BLOCK = "# Workspace\n\n## Coordinator\n\n- User: Robin\n- Timezone: UTC\n{backlog}\n## After\n"


class GateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        store.migrate(self.root / "store.db")
        self.store = store.Store(self.root / "store.db")
        self.addCleanup(self.store.engine.dispose)

    def workspace(self, backlog_line: str) -> Path:
        (self.root / "CLAUDE.md").write_text(BLOCK.format(backlog=backlog_line))
        return self.root

    def test_work_without_an_issue_files_the_issue_first_then_writes_exactly_one_row(self):
        gh = tf.FakeGh()
        f = forge.for_workspace(self.workspace("- Backlog: GitHub issues; repo example/app"), gh=gh)
        got = issue_gate.start(f, self.store, "Add a flag", body=SCOPE, commit=True)
        self.assertEqual(got, issue_gate.Started(issue_key="github:example/app#101"))
        self.assertEqual([c[:2] for c in gh.all_calls], [["issue", "create"]])
        self.assertEqual([(i.issue_key, i.title) for i in self.store.items()], [("github:example/app#101", "Add a flag")])
        # Starting the same issue again is the same row.
        issue_gate.start(f, self.store, "Add a flag", issue=101, commit=True)
        self.assertEqual(len(self.store.items()), 1)
        self.assertEqual([c[:2] for c in gh.all_calls][1:], [["issue", "view"]], "work on an issue that exists files nothing")

    def test_a_supplied_issue_is_confirmed_on_the_forge_and_its_title_is_the_forges(self):
        gh = tf.FakeGh(issues=[tf.gh_issue(3, "Fix the parser")])
        got = issue_gate.start(forge.Forge(tf.GH, gh=gh), self.store, "a caller's name for it", issue=3, commit=True)
        self.assertEqual(got.issue_key, "github:example/app#3")
        self.assertEqual([(i.issue_key, i.title) for i in self.store.items()], [("github:example/app#3", "Fix the parser")])

    def test_new_and_existing_issues_supply_ownership_from_their_body(self):
        body = "## File ownership\n- read-only: `src/`"
        f = forge.Forge(tf.GH, gh=tf.FakeGh(issues=[{**tf.gh_issue(3, "Parser"), "body": body}]))
        for number in (None, 3):
            with self.subTest(number=number):
                got = issue_gate.start(f, self.store, "Parser", body=body if number is None else "ignored", issue=number, commit=True)
                self.assertEqual([(r.path, r.mode) for r in self.store.ownership(got.issue_key)], [("src", "read")])

    def test_new_issue_without_valid_scope_is_refused_before_any_forge_or_store_write(self):
        for body in ("", "## File ownership\n", "## File ownership\n- typo: `src/`"):
            for commit in (False, True):
                with self.subTest(body=body, commit=commit):
                    gh = tf.FakeGh()
                    got = issue_gate.start(forge.Forge(tf.GH, gh=gh), self.store, "Parser", body=body, commit=commit)
                    self.assertIn("needs file ownership", got.refused)
                    self.assertEqual(gh.all_calls, [])
                    self.assertEqual(self.store.items(), [])

    def test_an_existing_undeclared_issue_is_cached_without_inventing_scope(self):
        f = forge.Forge(tf.GH, gh=tf.FakeGh(issues=[tf.gh_issue(3, "Parser")]))
        self.store.upsert_item("github:example/app#3")
        self.store.replace_file_ownership("github:example/app#3", [(".", "write")])
        got = issue_gate.start(f, self.store, "Parser", issue=3, commit=True)
        self.assertEqual(self.store.ownership(got.issue_key), [])

    def test_a_supplied_issue_the_forge_does_not_confirm_is_refused_and_writes_no_row(self):
        got = issue_gate.start(forge.Forge(tf.GH, gh=tf.FakeGh()), self.store, "Add a flag", issue=999999, commit=True)
        self.assertIn("#999999", got.refused)
        self.assertEqual(self.store.items(), [])

    def test_closed_gitlab_issue_is_refused_without_a_row(self):
        row = {**tf.gl_issue(3, "Fix the parser"), "state": "closed"}
        with mock.patch.object(backlog, "_call", return_value=(row, {}, "")):
            got = issue_gate.start(forge.Forge(tf.GL, token="fixture-token"), self.store,
                                   "Fix the parser", issue=3, commit=True)
        self.assertIn("issue #3 is closed", got.refused)
        self.assertEqual(self.store.items(), [])

    def test_a_preview_of_work_on_an_existing_issue_writes_nothing(self):
        gh = tf.FakeGh(issues=[tf.gh_issue(3, "Fix the parser")])
        got = issue_gate.start(forge.Forge(tf.GH, gh=gh), self.store, "Fix the parser", issue=3)
        self.assertEqual(got, issue_gate.Started(preview="would start: github:example/app#3 Fix the parser"))
        self.assertEqual(self.store.items(), [])

    def test_on_gitlab_too(self):
        def call(method, url, token, timeout, payload=None):
            return {"iid": 7, "web_url": "https://gitlab.example.test/team/app/-/issues/7"}, {}, ""

        f = forge.for_workspace(self.workspace(
            "- Backlog: GitLab; host https://gitlab.example.test; project team/app; token env SOME_TOKEN"), token="t")
        with mock.patch.object(backlog, "_call", call):
            got = issue_gate.start(f, self.store, "Add a flag", body=SCOPE, commit=True)
        self.assertEqual(got.issue_key, "gitlab:gitlab.example.test/team/app#7")
        self.assertEqual([i.issue_key for i in self.store.items()], ["gitlab:gitlab.example.test/team/app#7"])

    def test_a_create_that_fails_writes_no_row(self):
        gh = tf.FakeGh(fail={("issue", "create")})
        got = issue_gate.start(forge.Forge(tf.GH, gh=gh), self.store, "Add a flag", body=SCOPE, commit=True)
        self.assertIn("rate limited", got.refused)
        self.assertEqual(self.store.items(), [])

    def test_a_create_that_names_no_number_writes_no_row(self):
        f = forge.Forge(backlog.Backlog("https://gitlab.example.test", "team/app"), token="t")
        with mock.patch.object(backlog, "_call", lambda *a, **k: ({}, {}, "")):
            got = issue_gate.start(f, self.store, "Add a flag", body=SCOPE, commit=True)
        self.assertIn("no issue number", got.refused)
        self.assertEqual(self.store.items(), [])

    def test_a_preview_files_nothing_and_writes_no_row(self):
        gh = tf.FakeGh()
        got = issue_gate.start(forge.Forge(tf.GH, gh=gh), self.store, "Add a flag", body=SCOPE)
        self.assertEqual(got.preview, "would create: Add a flag")
        self.assertEqual((gh.all_calls, self.store.items()), ([], []))

    def test_no_backlog_is_refused_and_setup_is_offered(self):
        for root in (self.workspace(""), self.root / "no-workspace-here"):
            with self.subTest(root=root):
                got = issue_gate.start(forge.for_workspace(root), self.store, "Add a flag", body=SCOPE, commit=True)
                self.assertIn("no `Backlog:` line", got.refused)
                self.assertIn("- Backlog: GitHub issues; repo OWNER/NAME", got.setup)
                self.assertIn("chief-of-stuff init", got.setup)
                self.assertEqual(self.store.items(), [])


class WorkMainTest(unittest.TestCase):
    """The registered work driver's real main builds the forge and the engine store."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "CLAUDE.md").write_text(BLOCK.format(backlog="- Backlog: GitHub issues; repo example/app") +
                                             "\n")
        self.gh = tf.FakeGh()

    def run_work(self, *args):
        work = importlib.import_module("work")
        real_factory = forge.for_workspace
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(forge, "for_workspace", side_effect=lambda root, **opts:
                               real_factory(root, gh=self.gh, **opts)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = work.main(["--root", str(self.root), "Add a flag", "--body", SCOPE, *args])
        return code, out.getvalue(), err.getvalue()

    def items(self):
        if not (self.root / "store.db").exists():
            return []
        db = store.Store(self.root / "store.db")
        try:
            return [(i.issue_key, i.title) for i in db.items()]
        finally:
            db.engine.dispose()

    def test_commit_files_before_writing_one_row_and_reuses_the_engine_store(self):
        create = self.gh
        def gh(args, input_text=None):
            if args[:2] == ["issue", "create"]:
                self.assertEqual(self.items(), [], "the store row must follow issue creation")
                self.assertEqual(input_text, SCOPE)
            return create(args, input_text)
        self.gh = gh
        code, out, err = self.run_work("--body", SCOPE, "--commit")
        self.assertEqual(code, 0, err)
        self.assertIn("github:example/app#101", out)
        self.assertEqual(self.items(), [("github:example/app#101", "Add a flag")])
        self.assertIn("--body-file", create.all_calls[0])
        self.assertEqual(self.run_work("--issue", "101", "--commit")[0], 0)
        self.assertEqual(len(self.items()), 1)
        self.assertEqual([c[:2] for c in create.all_calls], [["issue", "create"], ["issue", "view"]])

    def test_default_preview_files_nothing_and_writes_no_row(self):
        code, out, err = self.run_work("--body", SCOPE)
        self.assertEqual(code, 0, err)
        self.assertIn("would create: Add a flag", out)
        self.assertEqual((self.gh.all_calls, self.items()), ([], []))
        state = supervisor.state_dir(self.root)
        self.assertEqual((self.root / "store.db").exists() or (state / "migrate.lock").exists(), False,
                         "a preview must not create, migrate or lock the store")

    def test_work_records_an_answer_for_the_named_issues_ask(self):
        self.gh = tf.FakeGh(issues=[{**tf.gh_issue(3, "Parser"), "body": SCOPE}, tf.gh_issue(4, "Other")])
        store.migrate(self.root / "store.db")
        db = store.Store(self.root / "store.db")
        self.addCleanup(db.engine.dispose)
        key = "github:example/app#3"
        db.upsert_item(key)
        ask = db.add_ask(key, "implement", 1, "session", "Continue?", ["yes"], "")
        db.add_report(key, "implement", 1, "session", "blocked", "waiting", [])
        self.assertEqual(self.run_work("--issue", "3", "--answer-ask", str(ask), "--answer", "yes")[0], 0)
        self.assertIsNone(db.asks(key)[0].answer, "preview writes nothing")
        self.assertEqual(self.run_work("--issue", "4", "--answer-ask", str(ask), "--answer", "yes", "--commit")[0], 1)
        self.assertIsNone(db.asks(key)[0].answer, "another issue cannot answer this ask")
        code, _, err = self.run_work("--issue", "3", "--answer-ask", str(ask), "--answer", "yes", "--commit")
        self.assertEqual(code, 0, err)
        self.assertEqual(db.asks(key)[0].answer, "yes")
        import engine_facts
        import readiness
        from datetime import datetime, timezone
        from types import SimpleNamespace
        facts = engine_facts.gather(forge=SimpleNamespace(blocked_by=lambda: {}), store=db,
                                    root=self.root, trees=self.root / "trees", now=datetime.now(timezone.utc))
        self.assertIsNone(readiness.why_not(key, claims=facts.claims, running=(), blockers=(),
                                          asks=facts.asks, reports=facts.reports))

    def test_existing_issue_preview_does_not_open_the_store(self):
        self.gh = tf.FakeGh(issues=[tf.gh_issue(3, "Fix the parser")])
        with mock.patch.object(store, "migrate") as migrated:
            self.assertEqual(self.run_work("--issue", "3")[0], 0)
        migrated.assert_not_called()
        self.assertFalse((self.root / "store.db").exists())

    def test_a_running_engine_does_not_block_intake(self):
        store.migrate(self.root / "store.db")  # a live engine has already migrated it
        with supervisor.lock(supervisor.state_dir(self.root) / "engine.lock"):
            code, out, err = self.run_work("--commit")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.items()), 1)

    def test_migration_holds_the_migrate_lock_and_releases_it_afterward(self):
        path = supervisor.state_dir(self.root) / "migrate.lock"

        def migrate(db_path):
            with self.assertRaises(supervisor.ServiceError), supervisor.lock(path):
                pass
            real_migrate(db_path)

        real_migrate = store.migrate
        with mock.patch.object(store, "migrate", side_effect=migrate) as migrated:
            code, _, err = self.run_work("--commit")
        self.assertEqual(code, 0, err)
        migrated.assert_called_once_with(self.root.resolve() / "store.db")
        with supervisor.lock(path):
            pass

    def test_a_current_store_is_not_migrated_again_even_with_a_live_engine(self):
        store.migrate(self.root / "store.db")
        with supervisor.lock(supervisor.state_dir(self.root) / "engine.lock"), \
             mock.patch.object(store, "migrate") as migrated:
            code, _, err = self.run_work("--commit")
        self.assertEqual(code, 0, err)
        migrated.assert_not_called()
        self.assertEqual(len(self.items()), 1)

    def test_a_stale_store_is_not_migrated_under_a_live_engine(self):
        (self.root / "store.db").write_bytes(b"")  # no alembic_version: behind head
        with supervisor.lock(supervisor.state_dir(self.root) / "engine.lock"), \
             mock.patch.object(store, "migrate") as migrated:
            code, _, err = self.run_work("--commit")
        self.assertEqual(code, 1)
        self.assertIn("engine is running", err)
        migrated.assert_not_called()
        self.assertEqual(self.gh.all_calls, [], "nothing is filed when the store cannot open")

    def test_closed_issue_refuses_preview_and_commit_without_a_row(self):
        self.gh = tf.FakeGh(issues=[{**tf.gh_issue(3, "Fix the parser"), "state": "CLOSED"}])
        for args in ((), ("--commit",)):
            with self.subTest(args=args):
                code, out, err = self.run_work("--issue", "3", *args)
                self.assertNotEqual(code, 0)
                self.assertIn("issue #3 is closed", err)
                self.assertEqual(out, "")
                self.assertEqual(self.items(), [])

    def test_existing_issue_is_confirmed_and_uses_the_forge_title(self):
        self.gh = tf.FakeGh(issues=[tf.gh_issue(3, "Fix the parser")])
        code, out, err = self.run_work("--issue", "3")
        self.assertEqual(code, 0, err)
        self.assertIn("would start: github:example/app#3 Fix the parser", out)
        self.assertEqual(self.items(), [])
        self.assertEqual(self.run_work("--issue", "3", "--commit")[0], 0)
        self.assertEqual(self.items(), [("github:example/app#3", "Fix the parser")])
        self.assertEqual([c[:2] for c in self.gh.all_calls], [["issue", "view"], ["issue", "view"]])

    def test_missing_issue_and_failed_create_refuse_without_a_row(self):
        for args in (("--issue", "999", "--commit"), ("--commit",)):
            with self.subTest(args=args):
                self.gh = tf.FakeGh(fail={("issue", "create")})
                code, _, err = self.run_work(*args)
                self.assertNotEqual(code, 0)
                self.assertIn("refused:", err)
                self.assertEqual(self.items(), [])

    def test_no_backlog_offers_setup_and_creates_no_store(self):
        (self.root / "CLAUDE.md").write_text(BLOCK.format(backlog=""))
        for args in ((), ("--commit",)):
            with self.subTest(args=args):
                code, _, err = self.run_work(*args)
                self.assertNotEqual(code, 0)
                self.assertIn("no `Backlog:` line", err)
                self.assertIn(issue_gate.SETUP, err)
                self.assertFalse((self.root / "store.db").exists())
                self.assertEqual(self.gh.all_calls, [])

    def test_workspace_without_claude_md_offers_setup(self):
        (self.root / "CLAUDE.md").unlink()
        code, _, err = self.run_work("--commit")
        self.assertNotEqual(code, 0)
        self.assertIn(issue_gate.SETUP, err)
        self.assertFalse((self.root / "store.db").exists())

    def test_gitlab_main_uses_the_configured_forge(self):
        (self.root / "CLAUDE.md").write_text(BLOCK.format(backlog=
            "- Backlog: GitLab; host https://gitlab.example.test; project team/app; token env SOME_TOKEN"))
        with mock.patch.dict("os.environ", {"SOME_TOKEN": "fixture-token"}), \
             mock.patch.object(backlog, "_call", return_value=({"iid": 7}, {}, "")) as call:
            code, out, err = self.run_work("--commit")
        self.assertEqual(code, 0, err)
        self.assertIn("gitlab:gitlab.example.test/team/app#7", out)
        self.assertEqual(self.items(), [("gitlab:gitlab.example.test/team/app#7", "Add a flag")])
        self.assertEqual(call.call_args.args[0], "POST")


if __name__ == "__main__":
    unittest.main()
