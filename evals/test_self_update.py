"""The engine on its own repository installs each new origin/main as its release (Zach, 2026-10-27)."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import self_update
from evals import make_repo

PLUGIN = {".claude-plugin/plugin.json": json.dumps({"name": "chief-of-stuff", "version": "1.0.0"}),
          "scripts/engine.py": "print('one')\n", "start_coordinator.py": "", "chief_of_stuff.py": ""}


class FollowMainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.install_dir = self.base / "share"
        (self.install_dir / "versions").mkdir(parents=True)
        self.seen = self.base / "state" / "main-release"
        self.upstream = str(self.base / "origin.git")

    def follow(self, root, upstream=None):
        return self_update.follow_main(root, self.install_dir, self.seen, self.upstream if upstream is None else upstream)

    def test_a_new_main_commit_is_installed_once_and_becomes_current(self):
        root = make_repo.build(self.base, {"files": PLUGIN})
        first = self.follow(root)
        self.assertEqual((self.install_dir / "current").resolve(), first)
        self.assertEqual((first / "scripts/engine.py").read_text(), "print('one')\n")
        self.assertIsNone(self.follow(root))  # nothing new on main: no install
        (root / "scripts/engine.py").write_text("print('two')\n")
        make_repo.git(["commit", "-am", "two"], root)
        make_repo.git(["push", "origin", "main"], root)
        make_repo.git(["reset", "--hard", "HEAD~1"], root)  # the workspace checkout is not what is installed
        second = self.follow(root)
        self.assertNotEqual(second, first)
        self.assertEqual((self.install_dir / "current").resolve(), second)
        self.assertEqual((second / "scripts/engine.py").read_text(), "print('two')\n")

    def test_an_origin_that_is_not_the_running_releases_upstream_installs_nothing(self):
        # A fork or any other clone carrying chief-of-stuff's files must not replace the release every workspace runs.
        root = make_repo.build(self.base, {"files": PLUGIN})
        self.assertIsNone(self.follow(root, upstream="https://github.com/example/other"))
        self.assertIsNone(self.follow(root, upstream=""))  # a release that names no upstream never self-updates
        self.assertFalse((self.install_dir / "current").exists())

    def test_a_workspace_with_no_origin_installs_nothing(self):
        (self.base / "plain").mkdir()
        self.assertIsNone(self.follow(self.base / "plain"))

    def test_ssh_and_https_urls_name_the_same_repository(self):
        self.assertTrue(self_update.same_repo("git@github.com:example/tool.git", "https://github.com/Example/tool"))
        self.assertFalse(self_update.same_repo("git@github.com:fork/tool.git", "https://github.com/example/tool"))


class EngineFollowsMainTest(unittest.TestCase):
    def setUp(self):
        import engine  # deferred: the store's dependencies load only for the loop's wiring
        self.engine = engine
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def run_step(self, release, failure=None, lock=contextlib.nullcontext):
        (release / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (release / ".claude-plugin/plugin.json").write_text(json.dumps({"repository": "https://github.com/example/tool"}))
        with mock.patch.object(self.engine.supervisor, "RELEASE", release), \
                mock.patch.object(self.engine.supervisor, "lock", side_effect=lambda path: lock()) as locked, \
                mock.patch.object(self.engine.self_update, "follow_main", side_effect=failure) as follow, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.engine._follow_main(self.base / "ws")
        return follow, locked, err.getvalue()

    def test_a_failed_install_is_logged_and_the_loop_goes_on(self):
        follow, locked, err = self.run_step(self.base / "share/versions/1.0.0-abc", subprocess.CalledProcessError(128, "git"))
        follow.assert_called_once_with(self.base / "ws", self.base / "share", mock.ANY, "https://github.com/example/tool")
        self.assertTrue(str(locked.call_args.args[0]).endswith("supervisor.lock"))  # installs never race another workspace
        self.assertIn("engine: cannot install origin/main", err)

    def test_a_busy_supervisor_lock_waits_for_a_later_pass_quietly(self):
        def busy():
            raise self.engine.supervisor.ServiceError("busy")
        follow, _, err = self.run_step(self.base / "share/versions/1.0.0-abc", lock=busy)
        follow.assert_not_called()
        self.assertEqual(err, "")

    def test_an_engine_run_from_a_checkout_installs_nothing(self):
        follow, _, _ = self.run_step(self.base / "checkout")
        follow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
