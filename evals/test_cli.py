"""The one installed command routes fixed operations and preserves release pinning."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import chief_of_stuff as cli  # noqa: E402
import shell_setup  # noqa: E402
import supervisor  # noqa: E402


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.share = self.root / "share"

    def install(self, **kwargs):
        return cli.install(source=ROOT, install_dir=self.share, bin_dir=self.bin, **kwargs)

    def test_dry_run_does_not_write(self):
        result = self.install(dry_run=True)
        self.assertFalse(result["installed"])
        self.assertTrue(result["wrapper_write"])
        self.assertFalse(self.share.exists())
        self.assertFalse((self.bin / "chief-of-stuff").exists())

    def test_installed_release_includes_requirements(self):
        release = Path(self.install()["release"])
        self.assertTrue((release / "requirements.txt").is_file())
        self.assertEqual((release / "requirements.txt").read_bytes(), (ROOT / "requirements.txt").read_bytes())

    def test_one_wrapper_routes_init_and_models_from_versioned_release(self):
        first = self.install()
        command = self.bin / "chief-of-stuff"
        self.assertTrue(command.is_file())
        self.assertTrue(os.access(command, os.X_OK))
        self.assertTrue((self.share / "current").is_symlink())
        self.assertTrue((Path(first["release"]) / "chief_of_stuff.py").is_file())
        help_text = subprocess.run([str(command), "--help"], capture_output=True, text=True, check=True).stdout
        self.assertIn("worker", help_text)
        self.assertIn("start", help_text)
        workspace = self.root / "workspace"
        workspace.mkdir()
        gh = self.bin / "gh"
        gh.write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps([]))\n")
        gh.chmod(0o755)
        env = dict(os.environ, CHIEF_OF_STUFF_GH=str(gh))
        done = subprocess.run([str(command), "init", "--root", str(workspace), "--user", "Robin",
                               "--timezone", "America/Chicago", "--worker-mode", "one-shot",
                               "--github-repo", "owner/repo"], env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('mode = "one-shot"', (workspace / "chief-of-stuff.toml").read_text())
        guidance = workspace / "chief-of-stuff-models.md"
        self.assertTrue(guidance.is_file())
        guidance.unlink()
        done = subprocess.run([str(command), "models", "--root", str(workspace)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(guidance.is_file())
        second = self.install()
        self.assertFalse(second["wrapper_write"])
        self.assertEqual(first["release"], second["release"])

    def test_wrapper_uses_pinned_release_and_refuses_other_paths(self):
        self.install()
        command = self.bin / "chief-of-stuff"
        pinned = self.share / "versions" / "fixture-pin"
        pinned.mkdir()
        (pinned / "chief_of_stuff.py").write_text("print('pinned release')\n")
        env = dict(os.environ, CHIEF_OF_STUFF_RELEASE=str(pinned))
        out = subprocess.run([str(command), "--help"], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "pinned release")
        env["CHIEF_OF_STUFF_RELEASE"] = str(self.root)
        denied = subprocess.run([str(command), "--help"], env=env, capture_output=True, text=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("invalid pinned", denied.stderr)

    def test_existing_different_wrapper_requires_explicit_replace(self):
        command = self.bin / "chief-of-stuff"
        command.write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.install()
        self.assertFalse(self.share.exists())
        result = self.install(replace_wrapper=True)
        self.assertTrue(result["wrapper_write"])
        self.assertIn("os.execv", command.read_text())


class ModelsCommandTest(unittest.TestCase):
    """#111: `chief-of-stuff models --class C` reads the rotation so nobody parses the TOML by hand."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "CLAUDE.md").write_text(
            "# Workspace\n\n## Coordinator\n\n- User: Robin\n- Daily log dir: `daily/`\n"
            "- Tracker: `daily/<date>-tracker.md`\n- Timezone: America/Chicago\n- Settings: `chief-of-stuff.toml`\n")
        (self.root / "chief-of-stuff.toml").write_text(
            '[models.implement]\nrotation = ["codex:gpt-test-coder@medium", "claude:sonnet", "claude:haiku@low"]\n')

    def models(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "chief_of_stuff.py"), "models", "--root", str(self.root), *args],
                              capture_output=True, text=True)

    def test_prints_the_suggested_entry(self):
        done = self.models("--class", "implement")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "codex:gpt-test-coder@medium"), done.stderr)

    def test_after_and_not_family(self):
        self.assertEqual(self.models("--class", "implement", "--after", "codex:gpt-test-coder@medium").stdout.strip(),
                         "claude:sonnet")
        self.assertEqual(self.models("--class", "implement", "--not-family", "codex").stdout.strip(), "claude:sonnet")

    def test_exhausted_rotation_exits_nonzero(self):
        done = self.models("--class", "implement", "--after", "claude:haiku@low")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("rotation exhausted", done.stderr)

    def test_unknown_class_or_no_models_exits_nonzero(self):
        self.assertIn("no [models.review]", self.models("--class", "review").stderr)
        (self.root / "chief-of-stuff.toml").write_text('[workers]\nmode = "one-shot"\n')
        done = self.models("--class", "implement")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("chief-of-stuff-models.md", done.stderr)
        self.assertFalse((self.root / "chief-of-stuff-models.md").exists())


class TrackerCommandTest(unittest.TestCase):
    """#46: `chief-of-stuff tracker` routes to the read-only tracker presenter."""

    def test_tracker_routes_to_the_tracker_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text(
                "# Workspace\n\n## Coordinator\n\n- User: Robin\n- Daily log dir: `daily/`\n"
                "- Tracker: `daily/<date>-tracker.md`\n- Timezone: America/Chicago\n")
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-25-tracker.md").write_text("# Tracker\n\n## Resume\n\n- Next: read the reply\n\n## Log\n")
            done = subprocess.run([sys.executable, str(ROOT / "chief_of_stuff.py"), "tracker", "--root", str(root),
                                   "--date", "2026-09-25", "section", "Resume"], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("- Next: read the reply", done.stdout)


class StoreBackedVenvRoutingTest(unittest.TestCase):
    """#622: store-backed commands exec the pinned venv; plain commands keep sys.executable."""

    @staticmethod
    @contextmanager
    def _passthrough_lock(path, wait=False):
        yield

    def test_store_backed_command_execs_venv_python(self):
        venv_python = Path("/pin/venv/bin/python")
        data = Path("/pin/data")
        with mock.patch.object(supervisor, "lock", self._passthrough_lock), \
             mock.patch.object(supervisor, "ensure_venv", return_value=venv_python) as ensure, \
             mock.patch.object(supervisor, "data_dir", return_value=data), \
             mock.patch.object(cli.os, "execv", side_effect=SystemExit(0)) as execv:
            with self.assertRaises(SystemExit):
                cli.main(["performance", "--help"])
        ensure.assert_called_once_with(data)
        execv.assert_called_once_with(
            str(venv_python),
            [str(venv_python), str(ROOT / "scripts/performance_report.py"), "--help"],
        )

    def test_plain_command_keeps_sys_executable(self):
        with mock.patch.object(supervisor, "ensure_venv") as ensure, \
             mock.patch.object(cli.os, "execv", side_effect=SystemExit(0)) as execv:
            with self.assertRaises(SystemExit):
                cli.main(["models", "--help"])
        ensure.assert_not_called()
        execv.assert_called_once_with(
            sys.executable,
            [sys.executable, str(ROOT / "scripts/install_model_guidance.py"), "--help"],
        )

    def test_decision_page_is_plain_but_decision_graph_uses_the_pinned_venv(self):
        venv_python = Path("/pin/venv/bin/python")
        for command, args, backed in (
            ("decision", ["--root", "/workspace", "--name", "release-cut"], False),
            ("decisions", ["--root", "/workspace", "leaves"], True),
        ):
            with self.subTest(command=command), \
                 mock.patch.object(supervisor, "lock", self._passthrough_lock), \
                 mock.patch.object(supervisor, "data_dir", return_value=Path("/pin/data")), \
                 mock.patch.object(supervisor, "ensure_venv", return_value=venv_python) as ensure, \
                 mock.patch.object(cli.os, "execv", side_effect=SystemExit(0)) as execv:
                with self.assertRaises(SystemExit):
                    cli.main([command, *args])
                if backed:
                    ensure.assert_called_once_with(Path("/pin/data"))
                else:
                    ensure.assert_not_called()
                python = str(venv_python) if backed else sys.executable
                execv.assert_called_once_with(python, [python, str(ROOT / cli.COMMANDS[command]), *args])

    def test_work_routes_to_the_store_backed_driver_and_forwards_intake_options(self):
        venv_python = Path("/pin/venv/bin/python")
        args = ["--root", "/workspace", "Add a flag", "--body", "why", "--issue", "3", "--commit"]
        with mock.patch.object(supervisor, "lock", self._passthrough_lock), \
             mock.patch.object(supervisor, "ensure_venv", return_value=venv_python), \
             mock.patch.object(cli.os, "execv", side_effect=SystemExit(0)) as execv:
            with self.assertRaises(SystemExit):
                cli.main(["work", *args])
        execv.assert_called_once_with(str(venv_python),
            [str(venv_python), str(ROOT / "scripts/work.py"), *args])

    def test_engine_and_supervisor_route_through_the_cli(self):
        venv_python = Path("/pin/venv/bin/python")
        with mock.patch.object(supervisor, "lock", self._passthrough_lock), \
             mock.patch.object(supervisor, "ensure_venv", return_value=venv_python) as ensure, \
             mock.patch.object(cli.os, "execv", side_effect=SystemExit(0)) as execv:
            with self.assertRaises(SystemExit):
                cli.main(["engine", "--root", "/workspace", "--once"])
        ensure.assert_called_once()
        execv.assert_called_once_with(
            str(venv_python),
            [str(venv_python), str(ROOT / "scripts/engine.py"), "--root", "/workspace", "--once"],
        )
        with mock.patch.object(supervisor, "ensure_venv") as ensure, \
             mock.patch.object(cli.os, "execv", side_effect=SystemExit(0)) as execv:
            with self.assertRaises(SystemExit):
                cli.main(["supervisor", "--root", "/workspace", "status"])
        ensure.assert_not_called()
        execv.assert_called_once_with(
            sys.executable,
            [sys.executable, str(ROOT / "scripts/supervisor.py"), "--root", "/workspace", "status"],
        )

    def test_work_cli_files_an_issue_and_writes_the_engine_store(self):
        import forge
        import store
        import work
        from evals import test_forge as tf

        gh = tf.FakeGh()
        factory = forge.for_workspace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text("## Coordinator\n\n- Backlog: GitHub issues; repo example/app\n")

            def exec_driver(python, argv):
                self.assertEqual(argv[1], str(ROOT / "scripts/work.py"))
                raise SystemExit(work.main(argv[2:]))

            with mock.patch.object(supervisor, "lock", self._passthrough_lock), \
                 mock.patch.object(supervisor, "ensure_venv", return_value=Path(sys.executable)), \
                 mock.patch.object(cli.os, "execv", side_effect=exec_driver), \
                 mock.patch.object(forge, "for_workspace", side_effect=lambda root, **opts: factory(root, gh=gh, **opts)), \
                 mock.patch.object(sys, "stdout", io.StringIO()) as out:
                with self.assertRaises(SystemExit) as done:
                    cli.main(["work", "--root", str(root), "Add a flag",
                              "--body", "## File ownership\n- write: `src/`", "--commit"])
            self.assertEqual(done.exception.code, 0)
            self.assertIn("github:example/app#101", out.getvalue())
            db = store.Store(root / "store.db")
            try:
                self.assertEqual([(i.issue_key, i.title) for i in db.items()],
                                 [("github:example/app#101", "Add a flag")])
            finally:
                db.engine.dispose()
        self.assertEqual([c[:2] for c in gh.all_calls], [["issue", "create"]])

    def test_venv_failure_refuses_without_falling_back(self):
        buf = io.StringIO()
        with mock.patch.object(supervisor, "lock", self._passthrough_lock), \
             mock.patch.object(supervisor, "ensure_venv",
                               side_effect=supervisor.ServiceError("no python3 on the login shell's PATH")), \
             mock.patch.object(cli.os, "execv") as execv, \
             mock.patch.object(sys, "stderr", buf):
            code = cli.main(["performance"])
        self.assertEqual(code, 1)
        self.assertIn("refused:", buf.getvalue())
        self.assertIn("no python3", buf.getvalue())
        execv.assert_not_called()

    def test_shell_error_from_setup_refuses(self):
        buf = io.StringIO()
        with mock.patch.object(supervisor, "lock", self._passthrough_lock), \
             mock.patch.object(supervisor, "ensure_venv",
                               side_effect=shell_setup.ShellError("configured login shell '/missing' is not executable")), \
             mock.patch.object(cli.os, "execv") as execv, \
             mock.patch.object(sys, "stderr", buf):
            code = cli.main(["performance"])
        self.assertEqual(code, 1)
        self.assertIn("refused:", buf.getvalue())
        self.assertIn("not executable", buf.getvalue())
        execv.assert_not_called()

    def test_os_error_from_setup_refuses(self):
        buf = io.StringIO()
        with mock.patch.object(supervisor, "lock", self._passthrough_lock), \
             mock.patch.object(supervisor, "ensure_venv", side_effect=OSError("permission denied")), \
             mock.patch.object(cli.os, "execv") as execv, \
             mock.patch.object(sys, "stderr", buf):
            code = cli.main(["performance"])
        self.assertEqual(code, 1)
        self.assertIn("refused:", buf.getvalue())
        self.assertIn("permission denied", buf.getvalue())
        execv.assert_not_called()

    def test_venv_setup_runs_under_supervisor_lock(self):
        data = Path("/pin/data")
        order = []

        @contextmanager
        def fake_lock(path, wait=False):
            order.append(("lock", path, wait))
            yield
            order.append(("unlock", path))

        def fake_ensure(d):
            order.append(("ensure", d))
            return Path("/pin/venv/bin/python")

        with mock.patch.object(supervisor, "lock", fake_lock), \
             mock.patch.object(supervisor, "ensure_venv", side_effect=fake_ensure), \
             mock.patch.object(supervisor, "data_dir", return_value=data), \
             mock.patch.object(cli.os, "execv", side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit):
                cli.main(["performance"])
        self.assertEqual(order, [
            ("lock", data / "supervisor.lock", True),
            ("ensure", data),
            ("unlock", data / "supervisor.lock"),
        ])

    def test_every_command_reaching_sqlalchemy_is_store_backed(self):
        from evals.test_architecture import forbidden_reach
        scripts = ROOT / "scripts"
        reaching = {name for name, target in cli.COMMANDS.items()
                    if target.startswith("scripts/") and target.count("/") == 1
                    and forbidden_reach(scripts, Path(target).stem, set(), frozenset({"sqlalchemy", "alembic"}))}
        self.assertEqual(reaching, set(cli.STORE_BACKED),
                         f"Computed store-backed commands: {sorted(reaching)}; "
                         f"declared STORE_BACKED commands: {sorted(cli.STORE_BACKED)}")


if __name__ == "__main__":
    unittest.main()
