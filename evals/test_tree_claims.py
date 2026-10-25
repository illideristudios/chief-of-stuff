"""Unit tests for scripts/tree_claims.py: the claim sources that know a tree's writer by process (#43).

`process_status` could say a PID was gone and never said whose tree that left behind.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import process_status  # noqa: E402
import tree_claims  # noqa: E402


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


class Workspace(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tree = self.root / "trees" / "wt-x"
        self.tree.mkdir(parents=True)

    def register(self, name: str, pid: int, worktree: Path) -> None:
        folder = self.root / ".chief-of-stuff" / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.json").write_text(json.dumps(
            {"pid": pid, "name": name, "runtime": "codex", "worktree": str(worktree)}))


class RegistryClaimsTest(Workspace):
    def test_a_dead_worker_claims_its_tree_as_gone(self) -> None:
        pid = dead_pid()
        self.register("impl-7", pid, self.tree)
        [claim] = tree_claims.registry_claims(self.root, process_status.registrations, process_status.process_exists)[self.tree.resolve()]
        self.assertEqual((claim.owner, claim.live), ("impl-7", False))
        self.assertIn(str(pid), claim.absence)

    def test_a_live_worker_holds_its_tree(self) -> None:
        self.register("impl-7", os.getpid(), self.tree)
        [claim] = tree_claims.registry_claims(self.root, process_status.registrations, process_status.process_exists)[self.tree.resolve()]
        self.assertTrue(claim.live)

    def test_a_relative_worktree_is_read_against_the_workspace(self) -> None:
        """Not against wherever the audit happened to be started from."""
        self.register("impl-7", dead_pid(), Path("trees/wt-x"))
        self.assertIn(self.tree.resolve(), tree_claims.registry_claims(self.root, process_status.registrations, process_status.process_exists))

    def test_no_registry_claims_nothing(self) -> None:
        self.assertEqual(tree_claims.registry_claims(self.root, process_status.registrations, process_status.process_exists), {})


class OneShotClaimsTest(Workspace):
    def launcher(self, pid: int) -> None:
        (self.tree / ".chief-of-stuff").mkdir()
        (self.tree / ".chief-of-stuff" / "one-shot.pid").write_text(f"{pid} Unsaved work\n")

    def test_a_running_launcher_holds_its_tree(self) -> None:
        self.launcher(os.getpid())
        [claim] = tree_claims.one_shot_claims(self.root / "trees", process_status.running_trees)[self.tree.resolve()]
        self.assertEqual((claim.owner, claim.live), ("Unsaved work", True))

    def test_a_dead_launcher_claims_nothing(self) -> None:
        """Its File ownership row, keyed on the task, is what still speaks for the tree."""
        self.launcher(dead_pid())
        self.assertEqual(tree_claims.one_shot_claims(self.root / "trees", process_status.running_trees), {})


if __name__ == "__main__":
    unittest.main()
