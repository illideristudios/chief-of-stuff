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

    def follow(self, root):
        return self_update.follow_main(root, self.install_dir, self.seen)

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

    def test_a_workspace_that_is_not_chief_of_stuff_installs_nothing(self):
        root = make_repo.build(self.base, {"files": {"src/a.py": "x = 1\n"}})
        self.assertIsNone(self.follow(root))
        self.assertFalse((self.install_dir / "current").exists())


class EngineFollowsMainTest(unittest.TestCase):
    def setUp(self):
        import engine  # deferred: the store's dependencies load only for the loop's wiring
        self.engine = engine
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def run_step(self, release, failure=None):
        with mock.patch.object(self.engine.supervisor, "RELEASE", release), \
                mock.patch.object(self.engine.self_update, "follow_main", side_effect=failure) as follow, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.engine._follow_main(self.base / "ws")
        return follow, err.getvalue()

    def test_a_failed_install_is_logged_and_the_loop_goes_on(self):
        follow, err = self.run_step(self.base / "share/versions/1.0.0-abc", subprocess.CalledProcessError(128, "git"))
        follow.assert_called_once_with(self.base / "ws", self.base / "share", mock.ANY)
        self.assertIn("engine: cannot install origin/main", err)

    def test_an_engine_run_from_a_checkout_installs_nothing(self):
        follow, _ = self.run_step(self.base / "checkout")
        follow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
