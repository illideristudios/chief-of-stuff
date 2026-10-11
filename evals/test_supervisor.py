"""Engine supervisor: the dependency venv and the launchd LaunchAgent, with pip, launchctl and the login shell mocked."""
import contextlib
import io
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import supervisor

ENGINE = ("engine.py", "serve")


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.release, self.data, self.jobs, self.root = (base / n for n in ("release", "data", "agents", "ws"))
        (self.release / ".claude-plugin").mkdir(parents=True)
        self.root.mkdir()
        self.publish("1.0.0", "SQLAlchemy==2.0.54\n")
        self.calls = []
        self.pip_fails = False
        for patch in (mock.patch.object(supervisor, "RELEASE", self.release),
                      mock.patch.object(supervisor.sys, "platform", "darwin"),
                      mock.patch.dict(supervisor.os.environ, {"CLAUDE_PLUGIN_DATA": str(self.data)}),
                      mock.patch.object(supervisor.shell_setup, "resolve", return_value="/login/bin/python3"),
                      mock.patch.object(supervisor.subprocess, "run", side_effect=self.fake_run)):
            patch.start()
            self.addCleanup(patch.stop)
        self.loaded = False
        control = mock.patch.object(supervisor, "launchctl", side_effect=self.launchctl)
        self.launchctl_mock = control.start()
        self.addCleanup(control.stop)

    def publish(self, version, requirements):
        (self.release / ".claude-plugin/plugin.json").write_text(json.dumps({"version": version}))
        (self.release / "requirements.txt").write_text(requirements)

    def fake_run(self, argv, **_):
        self.calls.append(argv)
        if argv[1:3] == ["-m", "venv"]:
            (Path(argv[-1]) / "bin").mkdir(parents=True)
            (Path(argv[-1]) / "bin/python").touch()
        if "pip" in argv and self.pip_fails:
            return subprocess.CompletedProcess(argv, 1, "", "ERROR: No matching distribution found for SQLAlchemy==2.0.54")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def launchctl(self, *args, missing_ok=False):
        if args[0] == "print":
            return self.loaded
        if args[0] == "bootstrap":
            self.loaded = True
        if args[0] == "bootout":
            self.loaded = False
        return True

    def job(self):
        return plistlib.loads(next(self.jobs.glob("*.plist")).read_bytes())

    def verbs(self):
        return [call.args[0] for call in self.launchctl_mock.call_args_list]


class VenvTest(SupervisorTest):
    def test_builds_once_from_the_login_shell_python_and_reuses_it(self):
        python = supervisor.ensure_venv(self.data)
        venv = python.parent.parent
        self.assertEqual(python, venv / "bin/python")
        self.assertEqual(self.calls, [["/login/bin/python3", "-m", "venv", str(venv)],
                                      [str(python), "-m", "pip", "install", "--disable-pip-version-check",
                                       "-r", str(self.release / "requirements.txt")]])
        self.assertEqual(supervisor.ensure_venv(self.data), python)
        # Reuse probes the cached interpreter once; it must not rebuild.
        self.assertEqual(self.calls[2:], [[str(python), "-c", ""]])

    def test_changed_requirements_build_a_new_venv_beside_the_old(self):
        old = supervisor.ensure_venv(self.data)
        self.publish("1.0.0", "SQLAlchemy==2.0.55\n")
        new = supervisor.ensure_venv(self.data)
        self.assertNotEqual(new, old)
        self.assertTrue(old.exists(), "the running engine's venv must stay intact")
        self.assertEqual(len(self.calls), 4)

    def test_a_failed_install_is_reported_and_never_reused(self):
        self.pip_fails = True
        with self.assertRaisesRegex(supervisor.ServiceError, "No matching distribution"):
            supervisor.ensure_venv(self.data)
        self.pip_fails = False
        supervisor.ensure_venv(self.data)
        self.assertEqual(len(self.calls), 4, "the half-built venv is rebuilt, not reused")

    def test_no_login_shell_python_is_reported(self):
        with mock.patch.object(supervisor.shell_setup, "resolve", return_value=None), \
             self.assertRaisesRegex(supervisor.ServiceError, "python3"):
            supervisor.ensure_venv(self.data)

    def test_a_missing_or_dangling_venv_python_is_rebuilt(self):
        python = supervisor.ensure_venv(self.data)
        python.unlink()
        self.assertEqual(supervisor.ensure_venv(self.data), python)
        self.assertTrue(python.is_file())
        self.assertEqual(len(self.calls), 4, "marker alone must not reuse a missing interpreter")
        python.unlink()
        python.symlink_to("/nonexistent/chief-of-stuff-venv-python")
        self.assertEqual(supervisor.ensure_venv(self.data), python)
        self.assertTrue(python.is_file() and not python.is_symlink())
        self.assertEqual(len(self.calls), 6)

    def test_data_dir_falls_back_to_the_plugin_data_dir_when_unset(self):
        with mock.patch.dict(supervisor.os.environ, {"CLAUDE_PLUGIN_DATA": ""}):
            self.assertEqual(supervisor.data_dir(),
                             Path.home() / ".claude/plugins/data/chief-of-stuff-chief-of-stuff")
        self.assertEqual(supervisor.data_dir(), self.data)


class AgentTest(SupervisorTest):
    def test_install_runs_the_engine_under_the_venv_python_and_keeps_it_alive(self):
        self.assertEqual(supervisor.install(self.root, ENGINE, self.jobs), "engine: started")
        job = self.job()
        venv_python = supervisor.ensure_venv(self.data)
        self.assertEqual(job["ProgramArguments"], [str(venv_python), *ENGINE])
        self.assertEqual(job["WorkingDirectory"], str(self.root.resolve()))
        self.assertTrue(job["KeepAlive"])
        self.assertTrue(job["RunAtLoad"])
        self.assertEqual(job["EnvironmentVariables"],
                         {"CLAUDE_PLUGIN_DATA": str(self.data), "CHIEF_OF_STUFF_VERSION": "1.0.0"})
        self.assertEqual(self.verbs(), ["print", "enable", "bootstrap"])
        self.assertEqual(supervisor.install(self.root, ENGINE, self.jobs), "engine: serving")

    def test_install_creates_the_engine_log_directory(self):
        self.assertFalse((self.root / ".chief-of-stuff").exists())
        supervisor.install(self.root, ENGINE, self.jobs)
        log = self.root.resolve() / ".chief-of-stuff" / "engine.log"
        self.assertTrue(log.parent.is_dir())
        self.assertEqual(self.job()["StandardOutPath"], str(log))
        self.assertEqual(self.job()["StandardErrorPath"], str(log))

    def test_a_plugin_update_restarts_the_engine(self):
        supervisor.install(self.root, ENGINE, self.jobs)
        self.publish("1.0.1", "SQLAlchemy==2.0.54\n")
        self.assertEqual(supervisor.install(self.root, ENGINE, self.jobs), "engine: restarted")
        self.assertEqual(self.job()["EnvironmentVariables"]["CHIEF_OF_STUFF_VERSION"], "1.0.1")
        self.assertIn("bootout", self.verbs())
        self.assertTrue(self.loaded)

    def test_a_failed_install_leaves_the_running_engine_alone(self):
        supervisor.install(self.root, ENGINE, self.jobs)
        before = self.job()
        self.publish("1.0.1", "SQLAlchemy==9.9.9\n")
        self.pip_fails = True
        self.launchctl_mock.reset_mock()
        with self.assertRaises(supervisor.ServiceError):
            supervisor.install(self.root, ENGINE, self.jobs)
        self.assertEqual(self.job(), before)
        self.assertEqual(self.verbs(), [])

    def test_no_engine_command_installs_nothing(self):
        with self.assertRaisesRegex(supervisor.ServiceError, "entry command"):
            supervisor.install(self.root, (), self.jobs)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.jobs.exists())

    def test_uninstall_removes_the_job_and_status_reports_it(self):
        self.assertEqual(supervisor.status(self.root, self.jobs),
                         {"installed": False, "loaded": False, "version": None, "current": "1.0.0"})
        supervisor.install(self.root, ENGINE, self.jobs)
        self.assertEqual(supervisor.status(self.root, self.jobs),
                         {"installed": True, "loaded": True, "version": "1.0.0", "current": "1.0.0"})
        self.assertEqual(supervisor.uninstall(self.root, self.jobs), "engine: stopped")
        self.assertFalse(list(self.jobs.glob("*.plist")))
        self.assertFalse(self.loaded)

    def test_refuses_off_macos(self):
        with mock.patch.object(supervisor.sys, "platform", "linux"), \
             self.assertRaisesRegex(supervisor.ServiceError, "macOS"):
            supervisor.install(self.root, ENGINE, self.jobs)

    def test_cli_reports_errors_without_a_traceback(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(supervisor.main(["--root", str(self.root), "install"]), 2)
        self.assertIn("entry command", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
