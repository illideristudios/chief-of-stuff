"""Least-conflict proposals use complete forge facts and never merge the checkout."""

import dataclasses
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import chief_of_stuff as cli
import merge_order as mo


def change(n, files, base="main", **kw):
    return mo.Change(n, f"Change {n}", f"https://forge/{n}", f"{n:040x}", base, frozenset(files), **kw)


class OrderingTest(unittest.TestCase):
    def test_disjoint_changes_need_no_trials(self):
        changes = [change(8, ["a"]), change(2, ["b"])]
        self.assertEqual(mo.overlapping(changes), [])
        self.assertEqual(mo.order(changes, {}), [2, 8])

    def test_clean_chain_precedes_conflict_only_change(self):
        changes = [change(1, ["b"]), change(4, ["a", "b"]), change(3, ["a"]), change(9, ["c"])]
        trials = {(1, 4): mo.Trial("conflict", ("b",)), (3, 4): mo.Trial("clean")}
        self.assertEqual(mo.overlapping(changes), [(1, 4), (3, 4)])
        self.assertEqual(mo.order(changes, trials), [9, 3, 4, 1])

    def test_walk_is_deterministic_for_branching_components(self):
        changes = [change(n, ["a"]) for n in (4, 3, 2, 1)]
        trials = {pair: mo.Trial("conflict") for pair in mo.overlapping(changes)}
        trials.update({(1, 3): mo.Trial("clean"), (2, 3): mo.Trial("clean"), (3, 4): mo.Trial("clean")})
        self.assertEqual(mo.order(changes, trials), [1, 3, 2, 4])

    def test_truncated_and_unverified_changes_are_not_ordered(self):
        changes = [change(1, ["a"]), change(2, ["a"]), change(3, ["c"], complete=False), change(4, ["d"])]
        self.assertEqual(mo.order(changes, {(1, 2): mo.Trial("unverified", why="timeout")}), [4])

    def test_bases_are_never_compared(self):
        self.assertEqual(mo.overlapping([change(1, ["a"]), change(2, ["a"], base="release")]), [])


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.github = True
        self.changes = [change(2, ["new", "old"]), change(1, ["other"])]
        self.calls = []
        self.git_calls = []
        self.merge_code = 0
        self.fetch_failure = ""
        self.wrong_head = False
        self.full_files = False
        self.full_list = False
        self.list_size = 0
        self.origin = "git@github.com:acme/repo.git"
        self.checkout = True
        self.write_config()

    def write_config(self):
        spec = "github; repo acme/repo" if self.github else "gitlab; host https://lab.example; project acme/repo"
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Eval\n- Timezone: America/Chicago\n"
            "- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Backlog: {spec}\n")

    def rows(self, rest=False):
        if self.full_list or self.list_size:
            source = [change(n + 1, []) for n in range(self.list_size or mo.backlog.PER_PAGE)]
        else:
            source = self.changes
        if not self.github:
            return [dict(iid=c.number, title=c.title, web_url=c.url, sha=c.head, target_branch=c.base) for c in source]
        if rest:
            return [dict(number=c.number, title=c.title, html_url=c.url, head={"sha": c.head}, base={"ref": c.base}) for c in source]
        return [dict(number=c.number, title=c.title, url=c.url, headRefOid=c.head, baseRefName=c.base) for c in source]

    def page(self, url):
        query = parse_qs(urlsplit(url).query)
        page = int(query["page"][0])
        is_files = "/files?" in url or "/diffs?" in url
        if is_files:
            n = int(url.split("/")[-2])
            c = next(c for c in self.changes if c.number == n)
            rows = ([dict(filename="new", previous_filename="old")] if n == 2 and self.github
                    else [dict(filename=p) if self.github else dict(old_path=p, new_path=p) for p in c.files])
            if self.full_files and n == 2:
                return [rows[0]] * mo.backlog.PER_PAGE
            return rows if page == 1 else []
        return self.rows(rest=True) if page == 1 or self.full_list else []

    def gh(self, args):
        self.calls.append(args)
        return 0, json.dumps(self.rows() if args[:2] == ["pr", "list"] else self.page(args[1])), ""

    def call(self, method, url, token, timeout):
        self.calls.append((method, url))
        self.assertEqual(method, "GET")
        return self.page(url), {}, ""

    def git(self, argv, **kw):
        self.git_calls.append((argv, kw))
        args = argv[argv.index("-C") + 2:]
        out, err, code = "", "", 0
        if args[0] == "rev-parse":
            if args[-1] == "--is-inside-work-tree":
                out = "true\n" if self.checkout else "false\n"
            else:
                n = int(args[-1].split("/")[-1])
                out = ("f" * 40 if self.wrong_head else f"{n:040x}") + "\n"
        elif args[0] == "config":
            out = self.origin + "\n"
        elif args[0] == "fetch":
            if self.fetch_failure:
                code, err = 128, self.fetch_failure
        elif args[0] == "merge-tree":
            code = self.merge_code
            out = "a" * 40 + "\0" + ("path with spaces\0" if code == 1 else "")
        else:
            self.fail(f"unexpected git operation {args}")
        return subprocess.CompletedProcess(argv, code, out, err)

    def run_main(self, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(mo.backlog, "token", return_value="test"):
            code = mo.main(["--root", str(self.root)], gh=self.gh, call=self.call, git=self.git, **kw)
        return code, out.getvalue(), err.getvalue()

    def operations(self, name):
        return [(a, k) for a, k in self.git_calls if a[a.index("-C") + 2] == name]

    def test_github_lists_sorted_unique_paths_and_sanitizes_title(self):
        self.changes[0] = dataclasses.replace(self.changes[0], title="Add\nreader")
        code, out, err = self.run_main()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("#2 Addreader\n  new\n  old\n", out)
        self.assertIn("order: #1 #2\n", out)
        self.assertEqual(self.operations("fetch"), [])
        self.assertEqual(self.operations("merge-tree"), [])

    def test_gitlab_lists_both_rename_paths(self):
        self.github = False
        self.write_config()
        original = self.call
        def renamed(method, url, token, timeout):
            if "/2/diffs?" in url:
                return [dict(old_path="old", new_path="new")], {}, ""
            return original(method, url, token, timeout)
        self.call = renamed
        code, out, err = self.run_main()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("!2 Change 2\n  new\n  old\n", out)
        self.assertIn("order: !1 !2", out)

    def overlap(self):
        self.changes = [change(2, ["new"]), change(1, ["new"]), change(3, ["disjoint"])]

    def test_fetches_only_overlapping_heads_once_then_trials_once(self):
        for github in (True, False):
            with self.subTest(github=github):
                self.github = github
                self.write_config()
                self.overlap()
                self.git_calls.clear()
                code, out, err = self.run_main()
                self.assertEqual((code, err), (0, ""))
                mark = "#" if github else "!"
                self.assertIn(f"order: {mark}3 {mark}1 {mark}2", out)
                fetches = self.operations("fetch")
                self.assertEqual(len(fetches), 2)
                for n, (args, kw) in enumerate(fetches, 1):
                    source = "pull" if github else "merge-requests"
                    dest = "pr" if github else "mr"
                    self.assertEqual(args[-1], f"+refs/{source}/{n}/head:refs/remotes/origin/{dest}/{n}")
                    self.assertIn("--no-tags", args)
                    self.assertEqual(args[1:1 + len(mo.git_trees.GIT_WRITE_PINS)], mo.git_trees.GIT_WRITE_PINS)
                    self.assertEqual(kw["env"], mo.git_trees.write_env())
                    self.assertEqual(kw["timeout"], 60)
                trials = self.operations("merge-tree")
                self.assertEqual(len(trials), 1)
                self.assertEqual(trials[0][0][-8:], ["merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", "--end-of-options", f"{1:040x}", f"{2:040x}"])
                self.assertEqual(trials[0][1]["timeout"], 30)

    def test_conflict_reports_real_path_without_prose(self):
        self.overlap()
        self.merge_code = 1
        code, out, err = self.run_main()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("conflict #1 #2\n  path with spaces\n", out)

    def test_truncated_files_are_listed_but_excluded_from_order_and_trials(self):
        self.overlap()
        self.full_files = True
        with mock.patch.object(mo.backlog, "MAX_PAGES", 2), mock.patch.object(mo.backlog, "PER_PAGE", 2):
            code, out, err = self.run_main()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("unverified #2 files truncated", out)
        self.assertIn("order: #1 #3\n", out)
        self.assertEqual(self.operations("fetch"), [])

    def test_full_open_list_fails_closed_on_both_forges(self):
        self.full_list = True
        for github in (True, False):
            self.github = github
            self.write_config()
            with self.subTest(github=github), mock.patch.object(mo.backlog, "MAX_PAGES", 2):
                code, out, err = self.run_main()
                self.assertEqual(code, 1, err)
                self.assertEqual(out, "")
                self.assertIn("truncated", err)

    def test_exactly_one_full_open_page_can_be_complete(self):
        self.list_size = 2
        self.changes = [change(1, ["one"]), change(2, ["new"])]
        for github in (True, False):
            self.github = github
            self.write_config()
            with self.subTest(github=github), mock.patch.object(mo.backlog, "PER_PAGE", 2):
                self.assertEqual(self.run_main()[0], 0)

    def test_trial_errors_and_moved_heads_are_unverified_and_not_ordered(self):
        self.overlap()
        for attr, value in (("merge_code", 128), ("wrong_head", True), ("fetch_failure", "fatal: unavailable")):
            with self.subTest(attr=attr), mock.patch.object(self, attr, value):
                code, out, err = self.run_main()
                self.assertEqual((code, err), (0, ""))
                self.assertIn("unverified #1 #2", out)
                self.assertIn("order: #3\n", out)

    def test_timeout_is_unverified(self):
        self.overlap()
        original = self.git
        def timeout(args, **kw):
            if "merge-tree" in args:
                raise subprocess.TimeoutExpired(args, 30)
            return original(args, **kw)
        self.git = timeout
        code, out, err = self.run_main()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("unverified #1 #2", out)

    def test_lock_ref_failure_retries_once(self):
        self.overlap()
        self.fetch_failure = "cannot lock ref"
        self.run_main()
        self.assertEqual(len(self.operations("fetch")), 4)

    def test_separate_base_blocks_never_trial_cross_base_pairs(self):
        self.changes = [change(2, ["new"], base="release"), change(1, ["new"])]
        code, out, err = self.run_main()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("base main\n#1", out)
        self.assertIn("\n\nbase release\n#2", out)
        self.assertEqual(self.operations("fetch"), [])

    def test_bad_workspace_or_origin_is_refused_before_forge_reads(self):
        for origin in ("git@github.com:other/repo.git", "https://github.com/acme/repo-other.git"):
            with self.subTest(origin=origin), mock.patch.object(self, "origin", origin):
                self.assertEqual(self.run_main()[0], 2)
        self.checkout = False
        self.assertEqual(self.run_main()[0], 2)
        self.checkout = True
        (self.root / "CLAUDE.md").write_text("## Coordinator\n")
        self.assertEqual(self.run_main()[0], 2)
        self.assertEqual(self.calls, [])

    def test_no_changes_is_empty_success(self):
        self.changes = []
        self.assertEqual(self.run_main(), (0, "", ""))

    def test_failed_forge_reads_print_no_partial_order(self):
        self.gh = lambda args: (1, "", "unavailable")
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("unavailable", err)

    def test_command_registration(self):
        self.assertEqual(cli.COMMANDS["merge-order"], "scripts/merge_order.py")


class RealGitTest(unittest.TestCase):
    def test_fetch_ignores_configured_ref_updates_and_pruning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, checkout = root / "source", root / "checkout"
            source.mkdir()
            checkout.mkdir()
            def git(where, *args):
                return subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, check=True).stdout.strip()
            git(source, "init", "-q")
            git(source, "config", "user.name", "Eval")
            git(source, "config", "user.email", "eval@example.test")
            (source / "file").write_text("base\n")
            git(source, "add", ".")
            git(source, "commit", "-qm", "base")
            head = git(source, "rev-parse", "HEAD")
            git(source, "update-ref", "refs/pull/1/head", head)
            git(source, "update-ref", "refs/merge-requests/2/head", head)
            git(checkout, "init", "-q")
            git(checkout, "remote", "add", "origin", str(source))
            # A broad mapping could otherwise update a ref outside the command's namespace.
            git(checkout, "config", "remote.origin.fetch", "+refs/*:refs/remotes/surprise/*")
            git(checkout, "config", "fetch.prune", "true")
            git(checkout, "config", "fetch.pruneTags", "true")
            git(checkout, "config", "fetch.writeCommitGraph", "true")
            for github, n, dest in ((True, 1, "pr"), (False, 2, "mr")):
                c = dataclasses.replace(change(n, ["file"]), head=head)
                self.assertEqual(mo.fetch(checkout, c, github), "")
                self.assertEqual(git(checkout, "rev-parse", f"refs/remotes/origin/{dest}/{n}"), head)
            refs = git(checkout, "for-each-ref", "--format=%(refname)").splitlines()
            self.assertEqual(refs, ["refs/remotes/origin/mr/2", "refs/remotes/origin/pr/1"])
            self.assertFalse((checkout / ".git/index").exists())
            self.assertFalse((checkout / ".git/objects/info/commit-graph").exists())

    def test_conflicting_and_clean_heads_preserve_head_index_and_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()
            git("init", "-q")
            git("config", "user.name", "Eval")
            git("config", "user.email", "eval@example.test")
            (root / "file with spaces").write_text("base\n")
            (root / "other").write_text("base\n")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            heads = []
            for name, path in (("left", "file with spaces"), ("right", "file with spaces"), ("clean", "other")):
                git("checkout", "-qb", name, base)
                (root / path).write_text(name + "\n")
                git("commit", "-qam", name)
                heads.append(git("rev-parse", "HEAD"))
            (root / "dirty").write_text("untracked\n")
            before = (git("rev-parse", "HEAD"), git("status", "--porcelain"), (root / ".git/index").read_bytes())
            changes = [dataclasses.replace(change(i + 1, ["file with spaces"]), head=h) for i, h in enumerate(heads)]
            conflict = mo.trial(root, changes[0], changes[1])
            clean = mo.trial(root, changes[0], changes[2])
            self.assertEqual(conflict, mo.Trial("conflict", ("file with spaces",)))
            self.assertEqual(clean, mo.Trial("clean"))
            self.assertEqual(before, (git("rev-parse", "HEAD"), git("status", "--porcelain"), (root / ".git/index").read_bytes()))


if __name__ == "__main__":
    unittest.main()
