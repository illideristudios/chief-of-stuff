"""Board recovery runs independently of coordinator sessions (#260)."""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pages
import supervisor
from evals.test_pages import free_port

ROOT = Path(__file__).resolve().parent.parent
RULE = "The workspace supervisor maintains the board independently; scheduled checks do not run routine pages commands."


class PagesSupervisionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.port = free_port()
        self.directory = self.root / "published"
        self.log = self.root / "pages.log"
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Timezone: America/Chicago\n"
            "- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Board: self-hosted; URL http://127.0.0.1:{self.port}/; dir `published/`\n")

    def stop_server(self):
        pid = self.directory / pages.PID
        if pid.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid.read_text()), signal.SIGTERM)

    def test_concurrent_starts_publish_the_one_live_servers_pid(self):
        self.addCleanup(self.stop_server)
        real = subprocess.Popen
        children = []

        def spawn(*args, **kwargs):
            proc = real(*args, **kwargs)
            children.append(proc)
            self.addCleanup(proc.wait, timeout=5)
            self.addCleanup(proc.terminate)
            return proc

        with mock.patch.object(pages.subprocess, "Popen", side_effect=spawn) as spawned:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: pages.ensure(self.directory, self.port, self.log), range(4)))
        self.assertEqual(results.count("pages: started"), 1)
        self.assertEqual(results.count("pages: serving"), 3)
        self.assertEqual(spawned.call_count, 1)
        self.assertEqual(int((self.directory / pages.PID).read_text()), children[0].pid)

    def test_supervisor_reuses_then_recovers_a_killed_server_without_an_agent(self):
        self.addCleanup(self.stop_server)
        pages.ensure(self.directory, self.port, self.log)
        original = int((self.directory / pages.PID).read_text())
        stop = threading.Event()
        with mock.patch.object(pages, "POLL", 0.05), mock.patch.object(pages, "RETRY_MIN", 0.05):
            thread = threading.Thread(target=pages.supervise, args=(self.root, stop))
            thread.start()
            try:
                time.sleep(0.15)
                self.assertEqual(int((self.directory / pages.PID).read_text()), original)
                os.kill(original, signal.SIGKILL)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    pid = int((self.directory / pages.PID).read_text())
                    if pid != original and (pages._who(self.port) or "").startswith(pages.SERVER):
                        break
                    time.sleep(0.05)
                else:
                    self.fail("supervisor did not recover the board")
            finally:
                stop.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_failures_and_repeated_crashes_have_bounded_backoff_then_reset(self):
        stop = mock.Mock()
        stop.is_set.side_effect = [False] * 9 + [True]
        with mock.patch.object(pages, "ensure", side_effect=[
                pages.PagesError("occupied"), "pages: started", "pages: started", "pages: started",
                "pages: started", "pages: started", "pages: started", "pages: serving", "pages: started"]), \
             contextlib.redirect_stderr(io.StringIO()):
            pages.supervise(self.root, stop)
        delays = [call.args[0] for call in stop.wait.call_args_list]
        self.assertEqual(delays[:7], [1, 2, 4, 8, 16, 32, 60])
        self.assertEqual(delays[7:], [pages.POLL, 1])

    def test_disabled_board_neither_installs_nor_starts_a_server(self):
        path = self.root / "CLAUDE.md"
        path.write_text("\n".join(line for line in path.read_text().splitlines() if "Board:" not in line))
        with mock.patch.object(pages, "load_agent") as load, \
             mock.patch.object(pages, "ensure") as ensure:
            self.assertEqual(pages.ensure_service(self.root), "pages: off")
            stop = mock.Mock()
            stop.is_set.side_effect = [False, True]
            pages.supervise(self.root, stop)
        load.assert_not_called()
        ensure.assert_not_called()

    def test_management_uses_pinned_release_and_workspace_without_a_browser(self):
        with mock.patch.object(pages.sys, "platform", "darwin"), \
             mock.patch.object(pages, "load_agent", return_value="started") as load:
            self.assertEqual(pages.ensure_service(self.root), "pages: started")
        job = load.call_args.args[0]
        self.assertEqual(job["ProgramArguments"],
                         [sys.executable, str(Path(pages.__file__).resolve()), "supervise", "--root", str(self.root)])
        self.assertEqual(job["WorkingDirectory"], str(self.root))
        self.assertTrue(job["RunAtLoad"])
        self.assertTrue(job["KeepAlive"])
        self.assertEqual(job["ThrottleInterval"], 10)
        self.assertEqual(job["EnvironmentVariables"]["CHIEF_OF_STUFF_VERSION"], pages.VERSION)

    def test_duplicate_supervisors_cannot_launch_children(self):
        with supervisor.lock(supervisor.state_dir(self.root) / "pages-supervisor.lock"), \
             mock.patch.object(pages, "ensure") as ensure:
            with self.assertRaises(supervisor.ServiceError):
                pages.supervise(self.root, threading.Event())
        ensure.assert_not_called()

    def test_scheduled_check_eval_quotes_the_supervision_rule(self):
        case = json.loads((ROOT / "evals/cases/silent-check-says-nothing/case.json").read_text())
        grader = next(g for g in case["turns"][0]["graders"] if g["name"] == "board maintenance belongs to the supervisor")
        self.assertEqual(grader["rule"], RULE)
        self.assertEqual(grader["max"], 0)
        self.assertIn(RULE, (ROOT / "agents/chief-of-stuff.md").read_text())
