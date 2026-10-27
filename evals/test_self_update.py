"""The engine on its own repository installs each new origin/main as its release (Zach, 2026-10-27)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
