"""PR 478 round 1: Pages lifecycle and the quiet Check contract.

All harness signals are mocked. The positive pid control owns a live child and reaps it
outside the mock; no test can signal a process group or an unrelated process.
"""

import contextlib
import io
import os
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import run


class PagesLifecycleTest(unittest.TestCase):
    def setUp(self):
        # These lifecycle tests never serve HTTP; avoid binding a sandbox socket.
        port = patch.object(run, "_free_port", return_value=41000)
        port.start()
        self.addCleanup(port.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / "work"
        self.pid = self.work / "pages" / ".pid"
        self.pid.parent.mkdir(parents=True)
        case_root = self.root / "case"
        fixture_pid = case_root / "fixture" / "pages" / ".pid"
        fixture_pid.parent.mkdir(parents=True)
        fixture_pid.write_text("{{live_pid}}\n")
        self.case = run.Case("pages", case_root, {
            "turns": [{"prompt": "check", "graders": []}], "graders": [],
        })

    def test_stop_pages_rejects_unsafe_and_malformed_pids(self):
        """R1: every unsafe numeric value must remain outside the kill guard."""
        for content in ("0", "1", "-1", "abc", ""):
            with self.subTest(pid=content), patch.object(run.os, "kill") as kill:
                self.pid.write_text(content)
                run.stop_pages(self.work)
                kill.assert_not_called()

    def _pages_serve_child(self) -> subprocess.Popen:
        """A live process whose cmdline looks like `pages.py serve` (the harness identity check)."""
        script = self.root / "pages.py"
        script.write_text("import time; time.sleep(60)\n")
        return subprocess.Popen(
            [sys.executable, str(script), "serve", "--port", "41000"],
            start_new_session=True,
        )

    def test_stop_pages_signals_a_live_pages_serve_pid(self):
        """#482: only a pid whose command line is pages.py serve is signalled."""
        child = self._pages_serve_child()
        try:
            self.assertIsNone(child.poll())
            self.assertGreater(child.pid, 1)
            self.assertNotEqual(child.pid, os.getpid())
            self.pid.write_text(str(child.pid))
            sys.path.insert(0, str(run.PLUGIN_ROOT / "scripts"))
            import pages as pg  # noqa: E402
            with patch.object(run.os, "kill") as kill, \
                 patch.object(pg, "is_pages_serve", return_value=True) as identified:
                run.stop_pages(self.work)
                identified.assert_called_once_with(child.pid)
                kill.assert_called_once_with(child.pid, signal.SIGTERM)
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_stop_pages_spares_an_unrelated_positive_pid(self):
        """#482: a positive pid that is not pages.py serve must not be signalled."""
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        try:
            self.pid.write_text(str(child.pid))
            sys.path.insert(0, str(run.PLUGIN_ROOT / "scripts"))
            import pages as pg  # noqa: E402
            with patch.object(run.os, "kill") as kill, \
                 patch.object(pg, "is_pages_serve", return_value=False) as identified:
                run.stop_pages(self.work)
                identified.assert_called_once_with(child.pid)
                kill.assert_not_called()
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_stop_pages_spares_the_harness(self):
        self.pid.write_text(str(os.getpid()))
        with patch.object(run.os, "kill") as kill:
            run.stop_pages(self.work)
            kill.assert_not_called()

    def test_stop_pages_without_a_pid_is_harmless(self):
        with patch.object(run.os, "kill") as kill:
            run.stop_pages(self.work)
            kill.assert_not_called()

    def test_stop_pages_handles_a_pid_too_large_for_os_kill(self):
        """R9: emulate the platform's conversion failure without sending a signal."""
        self.pid.write_text(str(10**30))
        with patch.object(run.os, "kill", side_effect=OverflowError("pid out of range")):
            run.stop_pages(self.work)

    def test_case_starts_pages_before_first_turn_and_stops_via_finally(self):
        """R2: exercise run_one -> run_turns -> first turn, including real stop_pages."""
        events = []
        server_pid = os.getpid() + 1000
        real_stop_pages = run.stop_pages

        def start(work, env, root):
            events.append("start")
            (work / "pages" / ".pid").write_text(str(server_pid))

        def first_turn(*args, **kwargs):
            events.append("turn")
            raise RuntimeError("first turn failed")

        def stop(work):
            events.append("stop")
            real_stop_pages(work)

        def kill(pid, sig):
            events.append("kill")

        with patch.object(run.tempfile, "mkdtemp", return_value=str(self.work)), \
             patch.object(run, "start_fixture_pages", side_effect=start) as started, \
             patch.object(run, "stop_pages", side_effect=stop) as stopped, \
             patch.object(run.os, "kill", side_effect=kill) as signalled, \
             patch.object(run, "command", return_value=["mock-runtime"]), \
             patch.object(run, "drive_turns", side_effect=first_turn) as turns:
            sys.path.insert(0, str(run.PLUGIN_ROOT / "scripts"))
            import pages as pg  # noqa: E402
            with patch.object(pg, "is_pages_serve", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "first turn failed"):
                    run.run_one(self.case, "agent", "sonnet", self.root / "out")
            self.assertEqual(events, ["start", "turn", "stop", "kill"])
            started.assert_called_once()
            self.assertEqual(started.call_args.args[0], self.work)
            turns.assert_called_once()
            stopped.assert_called_once_with(self.work)
            signalled.assert_called_once_with(server_pid, signal.SIGTERM)
        self.assertFalse(self.work.exists())

    def test_multi_turn_case_reads_the_snapshot_plugin_root(self):
        """#233 review: a multi-turn case runs against the plugin snapshot it was given, not the live checkout."""
        snapshot = self.root / "snapshot"
        with patch.object(run.tempfile, "mkdtemp", return_value=str(self.work)), \
             patch.object(run, "start_fixture_pages"), \
             patch.object(run, "command", return_value=["mock-runtime"]) as built, \
             patch.object(run, "drive_turns", side_effect=RuntimeError("stop after the command")):
            with self.assertRaisesRegex(RuntimeError, "stop after the command"):
                run.run_one(self.case, "agent", "sonnet", self.root / "out", root=snapshot, effort="high")
        self.assertEqual((built.call_args.kwargs["root"], built.call_args.kwargs["effort"]), (snapshot, "high"))

    def test_huge_pid_does_not_skip_the_rest_of_case_cleanup(self):
        """R9: stop_pages must not prevent rmtree or either stop_server call."""
        def start(work, env, root):
            (work / "pages" / ".pid").write_text(str(10**30))

        overflow = None
        with patch.object(run.tempfile, "mkdtemp", return_value=str(self.work)), \
             patch.object(run, "start_fixture_pages", side_effect=start), \
             patch.object(run, "run_turns", return_value=([], None, {})), \
             patch.object(run.os, "kill", side_effect=OverflowError("pid out of range")), \
             patch.object(run.shutil, "rmtree", wraps=run.shutil.rmtree) as removed, \
             patch.object(run, "stop_server") as stopped:
            try:
                result = run.run_one(self.case, "agent", "sonnet", self.root / "out")
            except OverflowError as exc:
                overflow = exc
            with self.subTest(cleanup="overflow contained"):
                self.assertIsNone(overflow, "stop_pages let OverflowError escape run_one")
            with self.subTest(cleanup="work removed"):
                removed.assert_called_once_with(self.work, ignore_errors=True)
                self.assertFalse(self.work.exists())
            with self.subTest(cleanup="both HTTP servers stopped"):
                self.assertEqual(stopped.call_args_list, [call(None), call(None)])
            if overflow is None:
                self.assertEqual(result, ([], None, {}))

    def _assert_failed_ensure_cleans_pid(self, error):
        # R6: failure before ensure replaces {{live_pid}} leaves a stale sentinel.
        # A removed pid file is the cleanup alternative specified by this review.
        self.pid.write_text(str(os.getpid()))
        with patch.object(run.subprocess, "run", side_effect=error) as ensure, \
             patch.object(run.os, "kill") as kill:
            try:
                run.start_fixture_pages(self.work, {}, run.PLUGIN_ROOT)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass  # run_one owns conversion to a per-case harness error.
            ensure.assert_called_once()
            self.assertIn("--ensure", ensure.call_args.args[0])
            kill.assert_not_called()  # the sentinel must never signal the harness.
            self.assertFalse(self.pid.exists(), "failed Pages ensure left its pid file behind")

    def test_failed_ensure_exit_cleans_its_pid(self):
        self._assert_failed_ensure_cleans_pid(subprocess.CalledProcessError(
            1, ["pages.py", "--ensure"], stderr="pages setup failed",
        ))

    def test_failed_ensure_timeout_cleans_its_pid(self):
        self._assert_failed_ensure_cleans_pid(subprocess.TimeoutExpired(
            ["pages.py", "--ensure"], 15, stderr="pages setup timed out",
        ))

    def test_successful_ensure_keeps_the_server_pid_for_cleanup(self):
        self.pid.write_text(str(os.getpid()))
        server_pid = os.getpid() + 1000

        def ensure(*args, **kwargs):
            self.pid.write_text(str(server_pid))
            return subprocess.CompletedProcess(args[0], 0, "pages: serving\n", "")

        sys.path.insert(0, str(run.PLUGIN_ROOT / "scripts"))
        import pages as pg  # noqa: E402
        with patch.object(run.subprocess, "run", side_effect=ensure) as started, \
             patch.object(run.os, "kill") as kill, \
             patch.object(pg, "is_pages_serve", return_value=True):
            run.start_fixture_pages(self.work, {}, run.PLUGIN_ROOT)
            started.assert_called_once()
            self.assertEqual(int(self.pid.read_text()), server_pid)
            kill.assert_not_called()
            run.stop_pages(self.work)
            kill.assert_called_once_with(server_pid, signal.SIGTERM)

    def _assert_failed_setup_is_a_case_error_in_a_continuing_sweep(self, error):
        """R6: main and run_one stay real; only ensure and runtime execution are mocked."""
        failed = run.Case("failed-pages", self.case.root, self.case.spec)
        healthy = run.Case("next-pages", self.case.root, self.case.spec)
        output = io.StringIO()
        attempts = []

        real_run = subprocess.run

        def ensure(argv, **kwargs):
            if "--ensure" not in argv:  # stop_pages may ask ps via the same subprocess.run
                return real_run(argv, **kwargs)
            attempts.append(kwargs["cwd"])
            if len(attempts) == 1:
                raise error
            (kwargs["cwd"] / "pages" / ".pid").write_text(str(os.getpid() + 1000))
            return subprocess.CompletedProcess(argv, 0, "pages: serving\n", "")

        sys.path.insert(0, str(run.PLUGIN_ROOT / "scripts"))
        import pages as pg  # noqa: E402
        with patch.object(run, "EVALS", self.root / "sweep"), \
             patch.object(run, "load_cases", return_value=[failed, healthy]), \
             patch.object(run, "snapshot_plugin", return_value=run.PLUGIN_ROOT), \
             patch.object(run.subprocess, "run", side_effect=ensure), \
             patch.object(run.os, "kill"), \
             patch.object(pg, "is_pages_serve", return_value=True), \
             patch.object(run, "run_turns", return_value=(
                 [("next case ran", True, "control")], None, {},
             )) as turns, \
             patch.object(run, "write_verdict", wraps=run.write_verdict) as verdict, \
             contextlib.redirect_stdout(output):
            code = run.main(["--arm", "agent", "--model", "sonnet"])
        self.assertEqual(code, 2)
        self.assertEqual(len(attempts), 2, "a Pages setup failure aborted the sweep")
        turns.assert_called_once()  # the failed setup must never reach a turn.
        self.assertEqual([c.args[1] for c in verdict.call_args_list], ["failed-pages", "next-pages"])
        self.assertEqual(verdict.call_args_list[0].args[5], [])
        self.assertIn("pages", verdict.call_args_list[0].args[6].lower())
        self.assertIsNone(verdict.call_args_list[1].args[6])
        self.assertIn("HARNESS ERROR", output.getvalue())
        self.assertIn("=> failed-pages: UNMEASURED", output.getvalue())
        self.assertIn("=> next-pages: GREEN", output.getvalue())
        self.assertIn("1 green, 0 red, 1 unmeasured", output.getvalue())
        self.assertTrue(all(not work.exists() for work in attempts))

    def test_failed_ensure_exit_is_a_case_error_and_sweep_continues(self):
        self._assert_failed_setup_is_a_case_error_in_a_continuing_sweep(
            subprocess.CalledProcessError(1, ["pages.py", "--ensure"], stderr="setup failed"),
        )

    def test_failed_ensure_timeout_is_a_case_error_and_sweep_continues(self):
        self._assert_failed_setup_is_a_case_error_in_a_continuing_sweep(
            subprocess.TimeoutExpired(["pages.py", "--ensure"], 15, stderr="setup timed out"),
        )


def status_line_conflicts(text):
    """Find ending rules lacking a named Check exception, without pinning its wording."""
    conflicts = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if not (re.search(r"\b(?:move|check)s?\b.*\bends?\b", sentence, re.I | re.S)
                and re.search(r"\bstatus\s+line\b", sentence, re.I)):
            continue
        exception = re.search(
            r"\b(?:except|unless|excluding|outside|apart from|other than|save for)\b[^.!?]*\bchecks?\b"
            r"|\bchecks?\b[^.!?]*\b(?:follow\w*|quiet|silent|instead)\b"
            r"|\b(?:see|per|according to|use|obey)\s+(?:the\s+)?Check\b"
            r"|\bCheck\s+(?:section|exception|rule)\b",
            sentence, re.I,
        )
        if not exception:
            conflicts.append(sentence.strip())
    return conflicts


class QuietCheckContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (Path(__file__).resolve().parent.parent / "agents" / "chief-of-stuff.md").read_text()

    def test_quiet_check_and_changed_check_rules_are_present(self):
        """Green control: both intended Check endings already exist."""
        check = self.text.split("\n## Check\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn("A check that finds nothing ends with the single line `.` and nothing else.", check)
        self.assertIn("A check that finds something says only what changed, in one short line.", check)

    def test_check_exception_accepts_different_wording(self):
        for sentence in (
            "Every move ends with a status line, except a check (see Check).",
            "Except for checks, every move ends with a status line.",
            "Otherwise a move ends with a status line; checks follow the Check section.",
            "A move ends with a status line unless it is a check, which stays quiet.",
            "Every move ends with a status line (for checks, see Check).",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(status_line_conflicts(sentence), [])
        for sentence in (
            "Every move ends with a stop: a status line, or one question when needed (see Asks).",
            "Otherwise a move ends with a status line.",
            "A check ends on one status line.",
            "Every move ends with a status line, including a check (see Asks).",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(status_line_conflicts(sentence), [sentence])

    def test_intro_move_ending_names_the_check_exception(self):
        """R7: the overarching move rule must defer to the quiet Check rule."""
        intro = self.text.split("\n## Role\n", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(status_line_conflicts(intro), [], "intro status-line rule needs a Check exception")

    def test_asks_move_ending_names_the_check_exception(self):
        """R7: the otherwise/status-line sentence must also exempt Check."""
        asks = self.text.split("\n## Asks\n", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(status_line_conflicts(asks), [], "Asks status-line rule needs a Check exception")

    def test_check_user_quote_keeps_its_attribution_date_and_time(self):
        """R13: provenance from origin/main belongs in the sentence carrying the quote."""
        quote = "a 15 minute timer loop that kicks you to check, evaluate state each time and hand off things again if needed"
        check = self.text.split("\n## Check\n", 1)[1].split("\n## ", 1)[0]
        sentences = re.split(r"(?<=[.!?])\s+", check)
        quoted = [sentence for sentence in sentences if quote in sentence]
        self.assertEqual(len(quoted), 1, "restore the user's original timer-loop quote in Check")
        self.assertRegex(quoted[0], r"\bthe user\b")
        self.assertIn("2026-09-24 01:35", quoted[0])


if __name__ == "__main__":
    unittest.main()
