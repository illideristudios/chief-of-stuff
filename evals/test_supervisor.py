"""Engine supervisor: the dependency venv and the launchd LaunchAgent, with pip, launchctl and the login shell mocked."""
import contextlib
import io
import json
import plistlib
import re
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
        base = Path(tmp.name).resolve()
        self.release, self.data, self.jobs, self.root = (base / n for n in ("release", "data", "agents", "ws"))
        (self.release / ".claude-plugin").mkdir(parents=True)
        self.root.mkdir()
        self.publish("1.0.0", "SQLAlchemy==2.0.54\n")
        self.calls = []
        self.pip_fails = False
        self.python_version, self.cache_tag = "3.13.0", "cpython-313"
        self.identity_call = ["/login/bin/python3", "-c",
                              "import json, sys; print(json.dumps([sys.version, sys.implementation.cache_tag]))"]
        for patch in (mock.patch.object(supervisor, "RELEASE", self.release),
                      mock.patch.object(supervisor.sys, "platform", "darwin"),
                      mock.patch.dict(supervisor.os.environ, {
                          "CLAUDE_PLUGIN_DATA": str(self.data),
                          "CLAUDE_CONFIG_DIR": "",
                          "CLAUDE_AS_PROFILES_DIR": "",
                          "XDG_STATE_HOME": "",
                      }),
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
        if argv == self.identity_call:
            return subprocess.CompletedProcess(argv, 0, json.dumps([self.python_version, self.cache_tag]), "")
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


class CurrentReleaseTest(SupervisorTest):
    def test_the_installed_pointer_names_the_release_to_run(self):
        install = self.release.parent / "install"
        old, new = install / "versions/1.0.0-aaa", install / "versions/1.0.1-bbb"
        old.mkdir(parents=True), new.mkdir(parents=True)
        (old / "chief_of_stuff.py").touch()
        (new / "chief_of_stuff.py").touch()
        (install / "current").symlink_to(old.relative_to(install))
        with mock.patch.object(supervisor, "RELEASE", old.resolve()):
            self.assertEqual(supervisor.current_release(), old.resolve())
            (install / "current").unlink()
            (install / "current").symlink_to(new.relative_to(install))
            self.assertEqual(supervisor.current_release(), new.resolve())

    def test_a_release_run_from_a_checkout_is_its_own_current(self):
        self.assertEqual(supervisor.current_release(), supervisor.RELEASE)

    def test_invalid_install_pointers_keep_the_working_release_and_report_the_problem(self):
        install = self.release.parent / "install"
        old = install / "versions/old"
        old.mkdir(parents=True)
        pointer = install / "current"
        for target in (install / "missing", install / "incomplete", pointer):
            with self.subTest(target=target):
                if target.name == "incomplete":
                    target.mkdir()
                pointer.symlink_to(target)
                self.addCleanup(pointer.unlink, missing_ok=True)
                errors = io.StringIO()
                with mock.patch.object(supervisor, "RELEASE", old), contextlib.redirect_stderr(errors):
                    actual = supervisor.current_release()
                pointer.unlink()
                self.assertEqual(actual, old)
                if target.name == "incomplete":
                    self.assertIn(f"missing entry file: {target / 'chief_of_stuff.py'}", errors.getvalue())
                elif target.name == "missing":
                    self.assertIn(f"No such file or directory: '{target}'", errors.getvalue())
                else:
                    self.assertRegex(errors.getvalue(), rf"(?:[Ll]oop|Too many levels of symbolic links).*{re.escape(str(pointer))}")


class VenvTest(SupervisorTest):
    def test_prepares_selected_release_pins_without_changing_the_running_release(self):
        old_python = supervisor.ensure_venv(self.data)
        current = self.release.parent / "next"
        current.mkdir()
        (current / "requirements.txt").write_text("SQLAlchemy==2.0.55\n")
        self.pip_fails = True
        with self.assertRaisesRegex(supervisor.ServiceError, "No matching distribution"):
            supervisor.ensure_venv(self.data, release=current)
        self.assertTrue(old_python.is_file())
        self.assertEqual(supervisor.RELEASE, self.release)
        self.pip_fails = False
        new_python = supervisor.ensure_venv(self.data, release=current)
        self.assertNotEqual(old_python, new_python)
        self.assertIn([str(new_python.parent.parent.resolve() / "bin/python"), "-m", "pip", "install", "--disable-pip-version-check",
                       "-r", str(current / "requirements.txt")], self.calls)
        self.assertEqual(supervisor.ensure_venv(self.data, release=current), new_python)
        self.assertEqual(supervisor.ensure_venv(self.data), old_python)

    def test_repairing_the_running_interpreters_venv_publishes_a_new_generation_beside_it(self):
        python = supervisor.ensure_venv(self.data)
        running = python.parent.parent.resolve()
        for failure, executable, prefix in (("probe timeout", str(python), str(running)),
                                             ("prefix alias", "/unrelated/python", str(running))):
            with self.subTest(failure=failure):
                with mock.patch.object(supervisor.sys, "executable", executable), \
                     mock.patch.object(supervisor.sys, "prefix", prefix), \
                     mock.patch.object(supervisor, "_venv_python_runs", return_value=False):
                    repaired = supervisor.ensure_venv(self.data)
                self.assertEqual(repaired, python)
                self.assertTrue((running / "bin/python").is_file(), "the running interpreter must survive repair")
                self.assertNotEqual(repaired.parent.parent.resolve(), running)
                running = repaired.parent.parent.resolve()

    def test_a_legacy_directory_is_published_behind_the_stable_link(self):
        python = supervisor.ensure_venv(self.data)
        pointer = python.parent.parent
        generation = pointer.resolve()
        legacy_name = pointer.name.removesuffix(".current")
        legacy = self.data / legacy_name
        pointer.unlink()
        generation.rename(legacy)
        adopted = supervisor.ensure_venv(self.data)
        self.assertEqual(adopted, pointer / "bin/python")
        self.assertEqual(pointer.readlink(), Path(legacy_name))
        supervisor.install(self.root, ENGINE, self.jobs)
        self.assertEqual(self.job()["ProgramArguments"][0], str(adopted))
        (legacy / "bin/python").unlink()
        supervisor.ensure_venv(self.data)
        self.assertTrue(Path(self.job()["ProgramArguments"][0]).is_file())

    def test_a_repair_keeps_the_launch_path_a_launchagent_already_records(self):
        supervisor.install(self.root, ENGINE, self.jobs)
        launch = self.job()["ProgramArguments"][0]
        generation = Path(launch).parent.parent.resolve()
        (generation / "bin/python").unlink()
        supervisor.ensure_venv(self.data)
        self.assertNotEqual(Path(launch).parent.parent.resolve(), generation)
        self.assertTrue(Path(launch).is_file(), "the recorded path must resolve to the repaired interpreter")
        self.calls.clear()
        supervisor.install(self.root, ENGINE, self.jobs)
        self.assertEqual(self.job()["ProgramArguments"][0], launch)
        self.assertNotIn("bootout", self.verbs(), "an unchanged launch path is not a changed job")

    def test_a_corrupt_generation_pointer_is_a_cache_miss(self):
        python = supervisor.ensure_venv(self.data)
        pointer = python.parent.parent
        pointer.unlink()
        pointer.write_text("../../outside")
        repaired = supervisor.ensure_venv(self.data)
        self.assertEqual(repaired, python)
        self.assertTrue(pointer.is_symlink() and repaired.is_file())

    def test_failed_repair_preserves_another_workspaces_interpreter_and_marker(self):
        python = supervisor.ensure_venv(self.data)
        marker = python.parent.parent / ".installed"
        self.pip_fails = True
        with mock.patch.object(supervisor, "_venv_python_runs", return_value=False), \
             self.assertRaises(supervisor.ServiceError):
            supervisor.ensure_venv(self.data)
        self.assertTrue(python.is_file(), "another workspace still executes this interpreter")
        self.assertTrue(marker.is_file(), "failed preparation must not modify the published venv")
        self.pip_fails = False
        self.assertEqual(supervisor.ensure_venv(self.data), python)

    def test_preflight_shares_one_sixty_second_budget_across_build_steps(self):
        elapsed = [0]
        run = self.fake_run

        def slow(argv, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 60 - elapsed[0])
            if elapsed[0] + 25 > 60:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            elapsed[0] += 25
            return run(argv, **kwargs)

        with mock.patch.object(supervisor.time, "monotonic", side_effect=lambda: elapsed[0]), \
             mock.patch.object(supervisor.subprocess, "run", side_effect=slow), \
             self.assertRaises(subprocess.TimeoutExpired):
            supervisor.ensure_venv(self.data, timeout=60)
        self.assertLessEqual(elapsed[0], 60)

    def test_changed_base_python_identity_builds_a_new_venv_beside_the_old(self):
        old = supervisor.ensure_venv(self.data)
        for version, tag in (("3.13.1", "cpython-313"), ("3.13.1", "cpython-314")):
            with self.subTest(version=version, cache_tag=tag):
                self.python_version, self.cache_tag = version, tag
                new = supervisor.ensure_venv(self.data)
                self.assertNotEqual(new, old)
                self.assertTrue(old.is_file(), "the running engine's venv must stay intact")
                self.assertTrue((old.parent.parent / ".installed").exists())
                self.assertIn(["/login/bin/python3", "-m", "venv", str(new.parent.parent.resolve())], self.calls)
                self.assertEqual(supervisor.ensure_venv(self.data), new)
                old = new

    def test_builds_once_from_the_login_shell_python_and_reuses_it(self):
        python = supervisor.ensure_venv(self.data)
        venv = python.parent.parent.resolve()
        self.assertEqual(python.parent.parent.name, venv.name.rsplit("-", 1)[0] + ".current")
        self.assertEqual(self.calls, [self.identity_call, ["/login/bin/python3", "-m", "venv", str(venv)],
                                      [str(venv / "bin/python"), "-m", "pip", "install", "--disable-pip-version-check",
                                       "-r", str(self.release / "requirements.txt")]])
        self.assertEqual(supervisor.ensure_venv(self.data), python)
        # Reuse checks the base identity and probes the cached interpreter; it must not rebuild.
        self.assertEqual(self.calls[3:], [self.identity_call, [str(python), "-c", ""]])

    def test_changed_requirements_build_a_new_venv_beside_the_old(self):
        old = supervisor.ensure_venv(self.data)
        self.publish("1.0.0", "SQLAlchemy==2.0.55\n")
        new = supervisor.ensure_venv(self.data)
        self.assertNotEqual(new, old)
        self.assertTrue(old.exists(), "the running engine's venv must stay intact")
        self.assertEqual(len(self.calls), 6)

    def test_a_failed_install_is_reported_and_never_reused(self):
        self.pip_fails = True
        with self.assertRaisesRegex(supervisor.ServiceError, "No matching distribution"):
            supervisor.ensure_venv(self.data)
        self.pip_fails = False
        supervisor.ensure_venv(self.data)
        self.assertEqual(len(self.calls), 6, "the half-built venv is rebuilt, not reused")

    def test_no_login_shell_python_is_reported(self):
        with mock.patch.object(supervisor.shell_setup, "resolve", return_value=None), \
             self.assertRaisesRegex(supervisor.ServiceError, "python3"):
            supervisor.ensure_venv(self.data)

    def test_a_missing_or_dangling_venv_python_gets_a_new_published_generation(self):
        python = supervisor.ensure_venv(self.data)
        for failure in ("missing", "dangling"):
            with self.subTest(failure=failure):
                generation = python.parent.parent.resolve()
                python.unlink()
                if failure == "dangling":
                    python.symlink_to("/nonexistent/chief-of-stuff-venv-python")
                repaired = supervisor.ensure_venv(self.data)
                self.assertEqual(repaired, python)
                self.assertNotEqual(repaired.parent.parent.resolve(), generation)
                self.assertTrue(repaired.is_file() and not repaired.is_symlink())
                self.assertTrue((repaired.parent.parent / ".installed").exists())
                self.assertEqual(supervisor.ensure_venv(self.data), repaired)

    def test_successful_repair_keeps_the_previous_generation_at_its_original_path(self):
        old = supervisor.ensure_venv(self.data)
        generation = old.parent.parent.resolve()
        with mock.patch.object(supervisor, "_venv_python_runs", return_value=False):
            new = supervisor.ensure_venv(self.data)
        self.assertEqual(old, new, "launch path is stable across repairs")
        self.assertNotEqual(new.parent.parent.resolve(), generation)
        self.assertTrue((generation / "bin/python").is_file())
        self.assertTrue((generation / ".installed").is_file())
        self.assertEqual(supervisor.ensure_venv(self.data), new)

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

    def test_an_engine_install_takes_the_board_over_from_a_standalone_board_agent(self):
        # #935: a workspace keeps one LaunchAgent; the engine supervises the board itself.
        board = supervisor.job_path(supervisor.label("pages", self.root), self.jobs)
        board.parent.mkdir(parents=True)
        board.write_bytes(plistlib.dumps({"Label": board.stem, "WorkingDirectory": str(self.root.resolve())}))
        supervisor.install(self.root, ENGINE, self.jobs)
        self.assertEqual([p.stem for p in self.jobs.glob("*.plist")], [supervisor.label("engine", self.root)])

    def test_install_keeps_limit_paths_from_the_installing_shell(self):
        paths = {
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CLAUDE_AS_PROFILES_DIR": str(self.root / "profiles"),
            "XDG_STATE_HOME": str(self.root / "state"),
        }
        with mock.patch.dict(supervisor.os.environ, paths):
            supervisor.install(self.root, ENGINE, self.jobs)
        env = self.job()["EnvironmentVariables"]
        for name, value in paths.items():
            self.assertEqual(env[name], value)

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

    def test_install_defaults_to_the_release_engine_and_restarts_on_version_path_or_pins_update(self):
        self.assertEqual(supervisor.install(self.root, jobs_dir=self.jobs), "engine: started")
        first_venv = supervisor.ensure_venv(self.data)
        self.assertEqual(self.job()["ProgramArguments"],
                         [str(first_venv), str(self.release / "scripts/engine.py")])
        self.assertEqual(supervisor.install(self.root, jobs_dir=self.jobs), "engine: serving")

        # 1. Plugin version update restarts the engine LaunchAgent.
        self.publish("1.0.1", "SQLAlchemy==2.0.54\n")
        self.assertEqual(supervisor.install(self.root, jobs_dir=self.jobs), "engine: restarted")
        self.assertEqual(self.job()["EnvironmentVariables"]["CHIEF_OF_STUFF_VERSION"], "1.0.1")

        # 2. Dependency pins update builds a new venv and restarts the engine LaunchAgent.
        self.publish("1.0.1", "SQLAlchemy==2.0.55\n")
        self.assertEqual(supervisor.install(self.root, jobs_dir=self.jobs), "engine: restarted")
        second_venv = supervisor.ensure_venv(self.data)
        self.assertNotEqual(second_venv, first_venv)
        self.assertEqual(self.job()["ProgramArguments"],
                         [str(second_venv), str(self.release / "scripts/engine.py")])

        # 3. Release path update restarts the engine LaunchAgent on the new release's engine.py.
        next_release = self.release.parent / "release-1.0.2"
        (next_release / ".claude-plugin").mkdir(parents=True)
        (next_release / ".claude-plugin/plugin.json").write_text(json.dumps({"version": "1.0.2"}))
        (next_release / "requirements.txt").write_text("SQLAlchemy==2.0.55\n")
        with mock.patch.object(supervisor, "RELEASE", next_release):
            self.assertEqual(supervisor.install(self.root, jobs_dir=self.jobs), "engine: restarted")
            self.assertEqual(self.job()["ProgramArguments"],
                             [str(second_venv), str(next_release / "scripts/engine.py")])
            self.assertEqual(self.job()["EnvironmentVariables"]["CHIEF_OF_STUFF_VERSION"], "1.0.2")

    def test_cli_install_status_and_uninstall_manage_the_default_engine(self):
        with mock.patch.object(supervisor, "job_path",
                               side_effect=lambda name, jobs_dir=None: (jobs_dir or self.jobs) / f"{name}.plist"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(supervisor.main(["--root", str(self.root), "install"]), 0)
                self.assertEqual(supervisor.main(["--root", str(self.root), "status"]), 0)
                self.assertEqual(supervisor.main(["--root", str(self.root), "uninstall"]), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "engine: started")
        self.assertIn("installed: true", out.getvalue())
        self.assertIn("loaded: true", out.getvalue())
        self.assertEqual(lines[-1], "engine: stopped")

    def test_cli_reports_errors_without_a_traceback(self):
        errors = io.StringIO()
        with mock.patch.object(supervisor, "install", side_effect=supervisor.ServiceError("cannot install engine")), \
             contextlib.redirect_stderr(errors):
            self.assertEqual(supervisor.main(["--root", str(self.root), "install"]), 2)
        self.assertIn("cannot install engine", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


class BasePythonTest(unittest.TestCase):
    def test_supervisor_and_notify_service_import_without_the_engine_venv_packages(self):
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        code = ("import sys; sys.modules.update(sqlalchemy=None, alembic=None); sys.path.insert(0, sys.argv[1]); "
                "import supervisor, notify_service")
        result = subprocess.run([sys.executable, "-c", code, str(scripts)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_workspace_forge_lives_on_startup_not_supervisor(self):
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import startup
        import supervisor
        self.assertTrue(hasattr(startup, "workspace_forge"))
        self.assertFalse(hasattr(supervisor, "workspace_forge"))
        self.assertNotIn("forge", supervisor.__dict__)
        self.assertNotIn("settings", supervisor.__dict__)


class ManifestTest(unittest.TestCase):
    def test_marketplace_version_matches_the_plugin_manifest(self):
        plugin = Path(__file__).resolve().parent.parent / ".claude-plugin"
        marketplace = json.loads((plugin / "marketplace.json").read_text())["plugins"][0]["version"]
        self.assertEqual(marketplace, json.loads((plugin / "plugin.json").read_text())["version"])


if __name__ == "__main__":
    unittest.main()
