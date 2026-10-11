"""Engine-owned worktree cleanup (1.0 plan M3 item 5, ledger E19) past every finding-27 guard.

Each test builds a real origin, clone and linked worktrees. A tree is removed only when it is a finished store item's,
no live worker claims it, `git status --ignored` lists nothing but build output, no container binds it, and nothing on
it is unpushed or unmerged. A guard that cannot be verified keeps the tree.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "evals"))

import cleanup  # noqa: E402
import make_repo  # noqa: E402
import store  # noqa: E402
from tree_state import unsaved  # noqa: E402

KEY = "github:example/repo#7"
CLAUDE = "# Workspace\n\n## Coordinator\n\n- Worktrees: `trees/`\n"


class CleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "CLAUDE.md").write_text(CLAUDE)
        self.clone = make_repo.build(self.root, {"files": {".gitignore": "*.db\nnode_modules/\n"}, "worktrees": [
            {"name": "done", "branch": "done"},
            {"name": "unpushed", "branch": "unpushed", "merged": False},
            {"name": "dirty", "branch": "dirty", "dirty": True},
        ]})
        self.trees = self.root / "trees"
        self.db = self.root / "store.db"
        store.migrate(self.db)
        self.store = store.Store(self.db)
        self.addCleanup(self.store.engine.dispose)
        for name in ("done", "unpushed", "dirty"):
            self.finish(name)
        self.bin = self.root / "bin"  # no container CLI unless a test plants one
        self.bin.mkdir()
        path = mock.patch.dict(os.environ, {"PATH": f"{self.bin}:/usr/bin:/bin"})
        path.start()
        self.addCleanup(path.stop)

    def finish(self, name: str, state: str = "closed") -> None:
        key = f"{KEY}-{name}"
        self.store.upsert_item(key, forge_state=state)
        self.store.set_ownership(key, str(self.trees / name))

    def run_cleanup(self, *flags: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cleanup.main(["--root", str(self.root), "--store", str(self.db), *flags])
        return code, out.getvalue()

    def kept(self, out: str) -> dict[str, str]:
        return {line.split()[1]: line for line in out.splitlines() if line.startswith("keep ")}

    def removable(self, out: str) -> list[str]:
        return [line.split()[1] for line in out.splitlines() if line.startswith(("remove ", "removed "))]

    def worktrees(self) -> str:
        return make_repo.git(["worktree", "list", "--porcelain"], self.clone)

    def plant(self, name: str, body: str) -> None:
        exe = self.bin / name
        exe.write_text(f"#!{sys.executable}\n{body}")
        exe.chmod(0o755)

    # the preview

    def test_the_preview_lists_the_clean_finished_tree_and_why_each_other_was_kept_and_removes_nothing(self) -> None:
        code, out = self.run_cleanup()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.removable(out), ["done"])
        kept = self.kept(out)
        self.assertIn("not on origin/main", kept["unpushed"])
        self.assertIn("uncommitted", kept["dirty"])
        self.assertTrue((self.trees / "done").is_dir())
        self.assertIn(str(self.trees / "done"), self.worktrees())
        self.assertEqual(self.store.log(f"{KEY}-done"), [])

    # removal

    def test_remove_deletes_the_clean_finished_tree_and_its_build_output_and_logs_it(self) -> None:
        (self.trees / "done" / "node_modules" / "pkg").mkdir(parents=True)
        (self.trees / "done" / "node_modules" / "pkg" / "index.js").write_text("x\n")
        code, out = self.run_cleanup("--remove")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.removable(out), ["done"])
        self.assertFalse((self.trees / "done").exists())
        self.assertNotIn(str(self.trees / "done"), self.worktrees())
        self.assertTrue((self.trees / "unpushed").is_dir() and (self.trees / "dirty").is_dir())
        self.assertIn("removed", self.store.log(f"{KEY}-done")[0].message)
        self.assertTrue(self.store.log(f"{KEY}-unpushed")[0].message.startswith("keep unpushed"))

    # each guard keeps the tree

    def test_unpushed_commits_keep_the_tree(self) -> None:
        _, out = self.run_cleanup("--remove")
        self.assertIn("1 not on origin/main; no upstream", self.kept(out)["unpushed"])
        self.assertTrue((self.trees / "unpushed" / "unpushed.txt").is_file())

    def test_uncommitted_files_keep_the_tree(self) -> None:
        _, out = self.run_cleanup("--remove")
        self.assertIn("1 uncommitted", self.kept(out)["dirty"])
        self.assertTrue((self.trees / "dirty" / "scratch.txt").is_file())

    def test_an_ignored_file_that_is_not_build_output_keeps_the_tree(self) -> None:
        (self.trees / "done" / "snapshot.db").write_text("only copy\n")
        _, out = self.run_cleanup("--remove")
        self.assertIn("snapshot.db", self.kept(out)["done"])
        self.assertTrue((self.trees / "done" / "snapshot.db").is_file())

    def test_a_live_worker_keeps_the_tree(self) -> None:
        tree = self.trees / "done"
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "claude", str(tree)])
        self.addCleanup(worker.wait)
        self.addCleanup(worker.kill)
        self.register(worker.pid, tree)
        _, out = self.run_cleanup("--remove")
        self.assertIn("live worker", self.kept(out)["done"])
        self.assertTrue(tree.is_dir())

    def test_a_reused_pid_is_not_a_live_worker(self) -> None:
        self.register(os.getpid(), self.trees / "done")  # alive, but its command is this test, not the worker
        _, out = self.run_cleanup("--remove")
        self.assertEqual(self.removable(out), ["done"])
        self.assertFalse((self.trees / "done").exists())

    def test_a_worker_that_cannot_be_verified_keeps_the_tree(self) -> None:
        self.register(os.getpid(), self.trees / "done")
        with mock.patch.object(cleanup, "check", return_value=[{"pid": os.getpid(), "status": "unverified"}]):
            _, out = self.run_cleanup("--remove")
        self.assertIn("cannot be verified", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_an_unreadable_registration_keeps_every_tree(self) -> None:
        sessions = self.root / ".chief-of-stuff" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "broken.json").write_text("{not json")
        _, out = self.run_cleanup("--remove")
        self.assertIn("registry", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_a_live_one_shot_keeps_the_tree(self) -> None:
        pidfile = self.trees / "done" / ".chief-of-stuff" / "one-shot.pid"
        pidfile.parent.mkdir()
        pidfile.write_text(f"{os.getpid()} {KEY}\n")
        _, out = self.run_cleanup("--remove")
        self.assertIn("one-shot", self.kept(out)["done"])
        self.assertTrue(pidfile.is_file())

    def test_an_open_item_keeps_the_tree(self) -> None:
        self.finish("done", state="open")
        _, out = self.run_cleanup("--remove")
        self.assertIn("not finished", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_a_tree_no_store_item_owns_is_kept(self) -> None:
        make_repo.git(["worktree", "add", "-b", "stray", str(self.trees / "stray"), "main"], self.clone)
        _, out = self.run_cleanup("--remove")
        self.assertIn("no store item", self.kept(out)["stray"])
        self.assertTrue((self.trees / "stray").is_dir())

    def test_a_container_bound_inside_the_tree_keeps_it(self) -> None:
        mount = self.trees / "done" / "data"
        self.plant("docker", "import sys\nprint('c1') if sys.argv[1] == 'ps' else print(%r)\n" % str(mount))
        _, out = self.run_cleanup("--remove")
        self.assertIn("container", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_a_container_runtime_that_cannot_answer_keeps_every_tree(self) -> None:
        self.plant("docker", "import sys\nsys.exit('Cannot connect to the Docker daemon')\n")
        _, out = self.run_cleanup("--remove")
        self.assertIn("containers cannot be checked", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_a_tree_git_cannot_read_is_kept(self) -> None:
        (self.trees / "done" / ".git").write_text("gitdir: /nowhere\n")
        _, out = self.run_cleanup("--remove")
        self.assertIn("git cannot read", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_a_locked_tree_is_kept(self) -> None:
        make_repo.git(["worktree", "lock", str(self.trees / "done")], self.clone)
        _, out = self.run_cleanup("--remove")
        self.assertIn("locked", self.kept(out)["done"])
        self.assertTrue((self.trees / "done").is_dir())

    def test_the_main_clone_is_never_removed(self) -> None:
        make_repo.git(["clone", "-q", str(self.root / "origin.git"), str(self.trees / "mirror")], self.root)
        self.finish("mirror")
        _, out = self.run_cleanup("--remove")
        self.assertIn("not a linked worktree", self.kept(out)["mirror"])
        self.assertTrue((self.trees / "mirror").is_dir())

    def test_a_missing_store_removes_nothing(self) -> None:
        self.db.unlink()
        code, out = self.run_cleanup("--remove")
        self.assertNotEqual(code, 0)
        self.assertFalse(self.db.exists())
        self.assertTrue((self.trees / "done").is_dir())

    def register(self, pid: int, tree: Path) -> None:
        sessions = self.root / ".chief-of-stuff" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "name": "worker-1", "runtime": "claude", "worktree": str(tree)}))


class UnsavedTest(unittest.TestCase):
    def test_only_an_ignored_build_output_directory_is_not_unsaved(self) -> None:
        status = "!! node_modules/\n!! web/node_modules/\n!! scripts/__pycache__/\n!! plan.db\n?? notes.md\nM a.py\n" \
                 "!! node_modules\n!! \"odd\\tname/node_modules/\"\n"
        self.assertEqual(unsaved(status), ("plan.db", "notes.md", "a.py", "node_modules", '"odd\\tname/node_modules/"'))


if __name__ == "__main__":
    unittest.main()
