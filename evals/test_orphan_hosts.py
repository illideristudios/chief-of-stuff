"""#901: a refused start leaves no host behind, and a tree takes no new run while an earlier run's process lives in it."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claude_adapter  # noqa: E402
import process_status  # noqa: E402
import worker  # noqa: E402

# A host that forks a child into its own session, then exits: the shape the Cursor orphans had (ppid 1, still live).
FORKING_HOST = ("import subprocess, sys\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', sys.argv[1]],"
                " start_new_session=True)\n")


def wait_until(test, check, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


class OrphanHostTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.session = str(uuid.uuid4())
        self.addCleanup(lambda: process_status.stop_session(self.session, grace=1))

    def test_a_host_that_forked_into_its_own_session_and_exited_is_fully_stopped(self):
        run_dir = self.dir / "run"
        run_dir.mkdir()
        request = SimpleNamespace(cwd=str(self.dir), session_id=self.session)
        runner = claude_adapter.spawn([sys.executable, "-c", FORKING_HOST, self.session], request, run_dir)
        self.assertTrue(wait_until(self, lambda: (run_dir / "exit.json").exists()), "the host's parent never exited")
        self.assertTrue(wait_until(self, lambda: process_status.carriers(self.session)), "the forked child never started")
        claude_adapter.cancel(runner)  # the runner's group: the child left it
        process_status.stop_session(self.session)
        self.assertEqual(process_status.carriers(self.session), [])

    def live(self, *extra):
        """A process carrying this session id, as a run's host or runner would."""
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", self.session, *extra],
                                   start_new_session=True)
        self.addCleanup(process.kill)
        self.assertTrue(wait_until(self, lambda: process.pid in process_status.carriers(self.session)))
        return process

    def run_record(self, tree, runner_pid, exited=False):
        run = self.dir / ".chief-of-stuff" / "runs" / "7-plan-1"
        run.mkdir(parents=True)
        (run / "command.json").write_text(json.dumps({"argv": [], "cwd": str(tree), "session_id": self.session}))
        (run / "started.json").write_text(json.dumps({"pid": 1, "runner_pid": runner_pid, "session_id": self.session}))
        if exited:
            (run / "exit.json").write_text(json.dumps({"exit_code": 0}))

    def test_a_tree_is_not_reused_while_an_earlier_run_still_runs_in_it(self):
        tree = self.dir / "trees" / "issue-7-plan-1"
        runner = self.live("--worker")
        self.run_record(tree, runner.pid)
        logged = []
        store = SimpleNamespace(add_log=lambda *args: logged.append(args))
        with self.assertRaisesRegex(RuntimeError, "still live"):
            worker._clear_tree(self.dir, tree, store, "k", "now")
        self.assertIn(runner.pid, process_status.carriers(self.session))
        self.assertEqual(logged, [])

    def test_a_leftover_host_of_a_refused_or_ended_run_is_stopped_before_the_tree_is_reused(self):
        tree = self.dir / "trees" / "issue-7-plan-1"
        orphan = self.live()
        self.run_record(tree, runner_pid=2 ** 22 + 7, exited=True)  # the runner is gone
        logged = []
        store = SimpleNamespace(add_log=lambda *args: logged.append(args))
        worker._clear_tree(self.dir, tree, store, "k", "now")
        self.assertTrue(wait_until(self, lambda: orphan.poll() is not None))
        self.assertEqual(process_status.carriers(self.session), [])
        self.assertIn("stopped a leftover host of run 7-plan-1", logged[0][1])

    def test_a_run_in_another_tree_is_left_alone(self):
        self.live()
        self.run_record(self.dir / "trees" / "elsewhere", runner_pid=2 ** 22 + 7, exited=True)
        worker._clear_tree(self.dir, self.dir / "trees" / "issue-7-plan-1", SimpleNamespace(add_log=None), "k", "now")
        self.assertTrue(process_status.carriers(self.session))


if __name__ == "__main__":
    unittest.main()
