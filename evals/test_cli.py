"""The one installed command routes fixed operations and preserves release pinning."""

import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import chief_of_stuff as cli  # noqa: E402
import claude_adapter  # noqa: E402
import performance_report  # noqa: E402
import probe_health  # noqa: E402
import shell_setup  # noqa: E402
import supervisor  # noqa: E402

def _help_sections(help_text: str) -> dict[str, str]:
    blocks = help_text.split("\n\n", 1)
    body = blocks[1] if len(blocks) > 1 else ""
    headings = list(re.finditer(r"^([\w ][\w -]*):\s*$", body, re.MULTILINE))
    sections = {"purpose": body[:headings[0].start()].strip() if headings else body.strip()}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        sections[heading.group(1).casefold()] = body[heading.end():end].strip()
    return sections


# A per-command help issue opts in here after bringing its parser up to the standard.
ENFORCED = frozenset({"docs", "health", "init", "pages", "performance"})


def missing_sections(help_text: str, name: str) -> list[str]:
    """Check the shared help contract; Purpose is argparse's description."""
    problems = []
    if not re.match(rf"usage: chief-of-stuff {re.escape(name)}(?:\s|$)", help_text):
        problems.append("usage must name the installed command")
    # Usage may wrap across several indented lines. Description follows a blank line.
    blocks = help_text.split("\n\n", 1)
    body = blocks[1] if len(blocks) > 1 else ""
    headings = list(re.finditer(r"^([\w ][\w -]*):\s*$", body, re.MULTILINE))
    purpose = body[:headings[0].start()].strip() if headings else body.strip()
    if not purpose:
        problems.append("Purpose")
    sections = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        sections[heading.group(1).casefold()] = body[heading.end():end].strip()
    for section in cli.HELP_SECTIONS[1:]:
        if not sections.get(section.casefold()):
            problems.append(section)
    if not re.search(rf"^\s*chief-of-stuff {re.escape(name)}(?:\s|$)",
                     sections.get("examples", ""), re.MULTILINE):
        problems.append("Examples must invoke the installed command")
    return problems


class CommandHelpTest(unittest.TestCase):
    def run_cli(self, *args, **kwargs):
        return subprocess.run([sys.executable, str(ROOT / "chief_of_stuff.py"), *args],
                              capture_output=True, text=True, **kwargs)

    def test_top_level_help_groups_each_visible_command_with_its_own_purpose(self):
        done = self.run_cli("--help", check=True)
        group = None
        seen = []
        for line in done.stdout.splitlines():
            if line.rstrip(":") in cli.COMMAND_GROUPS:
                group = line.rstrip(":")
            elif line.startswith("  "):
                name, purpose = line.strip().split("  ", 1)
                self.assertEqual(cli.COMMAND_REGISTRY[name][1], group)
                self.assertEqual(purpose.strip(), cli.command_purpose(name))
                seen.append(name)
        expected = [name for name, (_, group) in cli.COMMAND_REGISTRY.items() if group]
        self.assertCountEqual(seen, expected)
        self.assertEqual(len(seen), len(set(seen)))
        positions = [done.stdout.index(f"{group}:") for group in cli.COMMAND_GROUPS]
        self.assertEqual(positions, sorted(positions))

    def test_every_routed_command_is_grouped_or_hidden_and_store_backed_ones_exist(self):
        visible = {name for name, (_, group) in cli.COMMAND_REGISTRY.items() if group}
        self.assertEqual(visible, {"init", "work", "engine", "supervisor", "worktree", "backlog", "kanban",
                               "pipeline", "merge-order", "health", "pages", "decisions", "performance", "limits",
                               "architect-signals", "docs"})
        self.assertLessEqual(cli.STORE_BACKED, set(cli.COMMANDS))

    def test_retiring_commands_are_routable_but_not_discoverable(self):
        retired = {"install", "start", "models", "worker", "result", "adopt", "processes",
                   "inbox", "log", "tracker", "tick", "audit", "notify", "decision", "sources",
                   "merge-approved", "merge-ready", "review-threads"}
        self.assertTrue(all(cli.COMMAND_REGISTRY[name][1] is None
                            for name in retired & cli.COMMAND_REGISTRY.keys()))
        help_text = self.run_cli("--help", check=True).stdout
        docs = self.run_cli("docs", check=True).stdout
        for name in retired:
            self.assertNotRegex(help_text, rf"(?m)^  {re.escape(name)}  ")
            self.assertNotIn(f"### `{name}`", docs)
        registry = {"docs": (None, "observability"), "retiring-fixture": ("chief_of_stuff.py", None)}
        with patch.object(cli, "COMMAND_REGISTRY", registry), \
                patch.object(cli, "COMMANDS", {"retiring-fixture": "chief_of_stuff.py"}), \
                patch.object(cli.os, "execv") as execute:
            self.assertNotIn("retiring-fixture", cli.top_level_help())
            self.assertNotIn("retiring-fixture", cli.command_reference())
            self.assertEqual(cli.main(["retiring-fixture", "--help"]), 0)
        self.assertEqual(execute.call_args.args[1][-2:],
                         [str(ROOT / "chief_of_stuff.py"), "--help"])

    def test_failed_help_names_the_command(self):
        with patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 2, "", "parser unavailable")):
            with self.assertRaisesRegex(ValueError, "help failed for worktree.*parser unavailable"):
                cli.command_help("worktree")
        with patch.object(cli.subprocess, "run", side_effect=subprocess.TimeoutExpired("help", 30)):
            with self.assertRaisesRegex(ValueError, "help failed for worktree"):
                cli.command_help("worktree")

    def test_reference_is_fresh_and_independent_of_terminal_environment(self):
        done = self.run_cli("docs", check=True,
                            env=dict(os.environ, COLUMNS="40", NO_COLOR="", PYTHONHASHSEED="123"))
        self.assertEqual((ROOT / "docs/commands.md").read_text(), done.stdout,
                         "Regenerate with: python3 chief_of_stuff.py docs > docs/commands.md")

    def test_generator_includes_each_visible_commands_actual_help(self):
        reference = cli.command_reference()
        for name, (_, group) in cli.COMMAND_REGISTRY.items():
            if group:
                with self.subTest(command=name):
                    self.assertIn(f"### `{name}`\n\n```text\n{cli.command_help(name).rstrip()}\n```", reference)

    def test_generator_failure_does_not_print_partial_reference(self):
        with patch.object(cli, "command_help", side_effect=ValueError("help failed for worktree")):
            with patch("sys.stdout") as output, patch("sys.stderr") as error:
                self.assertEqual(cli.main(["docs"]), 1)
        output.write.assert_not_called()
        self.assertIn("help failed for worktree", error.write.call_args_list[0].args[0])

    def test_standard_accepts_complete_help(self):
        self.assertEqual(missing_sections(self.complete_help(), "sample"), [])

    @staticmethod
    def complete_help():
        return ("usage: chief-of-stuff sample [-h]\n\nDo the sample operation.\n\n"
                "options:\n  -h, --help  show this help message and exit\n\n"
                "When not to use:\n  Use another operation for other work.\n\n"
                "Output:\n  A sample on stdout.\n\nExit codes:\n  0 success; 2 usage error.\n\n"
                "Side effects:\n  None.\n\nExamples:\n  chief-of-stuff sample --help\n")

    def test_standard_rejects_each_missing_or_empty_section(self):
        complete = self.complete_help()
        for section in cli.HELP_SECTIONS[1:]:
            with self.subTest(section=section):
                heading = section.lower() if section == "Options" else section
                deficient = re.sub(rf"(?m)^{heading}:\n(?:  .+\n)+", "", complete)
                self.assertIn(section, missing_sections(deficient, "sample"))
                empty = re.sub(rf"(?m)(^{heading}:\n)(?:  .+\n)+", r"\1", complete)
                self.assertIn(section, missing_sections(empty, "sample"))
        self.assertIn("Purpose", missing_sections(complete.replace("Do the sample operation.", ""), "sample"))
        self.assertIn("usage must name the installed command",
                      missing_sections(complete.replace("usage: chief-of-stuff sample", "usage: sample.py"), "sample"))
        self.assertIn("Examples must invoke the installed command",
                      missing_sections(complete.replace("  chief-of-stuff sample --help", "  sample.py --help"), "sample"))

    def test_enforced_commands_meet_shared_standard_and_purpose(self):
        live = {name for name, (_, group) in cli.COMMAND_REGISTRY.items() if group}
        self.assertLessEqual(ENFORCED, live)
        for name in sorted(ENFORCED):
            with self.subTest(command=name):
                help_text = cli.command_help(name)
                self.assertEqual(missing_sections(help_text, name), [])
                purpose = cli.command_purpose(name)
                self.assertLessEqual(len(purpose), 100)
                self.assertEqual(help_text.split("\n\n")[1].splitlines()[0], purpose)


class PagesCommandTest(unittest.TestCase):
    def pages(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "chief_of_stuff.py"), "pages", *args],
                              capture_output=True, text=True)

    def test_help_is_the_agent_command_reference(self):
        done = self.pages("--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        self.assertEqual(missing_sections(done.stdout, "pages"), [], done.stdout)
        for text in ("--ensure", "/workers", "re-render", "nothing here ever opens it", "supervise", "install",
                     "uninstall", "status", "--board", "/decisions/<id>", "POST / ", "pages.log"):
            self.assertIn(text, done.stdout)
        self.assertNotIn("pages: started", done.stdout.splitlines())

    def test_serve_requires_directory_and_port(self):
        for args in (("serve",), ("serve", "--dir", "."), ("serve", "--port", "8787")):
            with self.subTest(args=args):
                done = self.pages(*args)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual(done.stdout, "")
                self.assertIn("usage:", done.stderr)
                self.assertNotIn("Traceback", done.stderr)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.share = self.root / "share"
        # No test runs the real claude: the install's plugin step answers from here.
        patcher = mock.patch.object(claude_adapter, "update_plugin", return_value={"plugin": "reported"})
        self.plugin = patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, **kwargs):
        return cli.install(source=ROOT, install_dir=self.share, bin_dir=self.bin, **kwargs)

    def test_an_install_brings_the_plugin_to_the_release_and_reports_it(self):
        # §14: I → Pl ∨ F. The plugin step reports where the plugin stands; the install passes that on.
        result = self.install()
        version = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"]
        self.plugin.assert_called_once_with(self.share / "current", version)
        self.assertEqual(result["plugin"], "reported")

    def test_dry_run_does_not_write(self):
        result = self.install(dry_run=True)
        self.plugin.assert_not_called()
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
        self.assertIn("worktree", help_text)
        self.assertIn("init", help_text)
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
                cli.main(["performance", "--since", "2026-10-01"])
        ensure.assert_called_once_with(data)
        execv.assert_called_once_with(
            str(venv_python),
            [str(venv_python), str(ROOT / "scripts/performance_report.py"), "--since", "2026-10-01"],
        )

    def test_help_of_every_store_backed_command_never_bootstraps_the_engine_venv(self):
        for name in sorted(cli.STORE_BACKED):
            for flag in ("--help", "-h"):
                with self.subTest(command=name, flag=flag), \
                     mock.patch.object(supervisor, "lock", side_effect=AssertionError("bootstrap")), \
                     mock.patch.object(supervisor, "ensure_venv", side_effect=AssertionError("bootstrap")), \
                     mock.patch.object(sys, "stdout", io.StringIO()) as out:
                    code = cli.main([name, flag])
                    self.assertEqual(code, 0)
                    self.assertTrue(out.getvalue().startswith("usage:"), out.getvalue())

    def test_bad_arguments_of_store_backed_commands_exit_2_without_bootstrapping(self):
        bad = {"pages": ["serve"], "performance": [], "limits": ["--root"], "work": [],
               "engine": ["--interval", "soon"], "decision": [], "adopt": ["--bogus"]}
        self.assertLessEqual(set(bad), cli.STORE_BACKED)
        for name, argv in bad.items():
            with self.subTest(command=name), \
                 mock.patch.object(supervisor, "lock", side_effect=AssertionError("bootstrap")), \
                 mock.patch.object(supervisor, "ensure_venv", side_effect=AssertionError("bootstrap")), \
                 mock.patch.object(sys, "stderr", io.StringIO()) as err:
                self.assertEqual(cli.main([name, *argv]), 2)
                self.assertIn("usage:", err.getvalue())

    def test_help_bootstraps_only_when_this_interpreter_lacks_the_dependencies(self):
        missing = subprocess.CompletedProcess([], 1, "", "ModuleNotFoundError: No module named 'sqlalchemy'")
        with mock.patch.object(cli.subprocess, "run", return_value=missing), \
             mock.patch.object(supervisor, "lock", self._passthrough_lock), \
             mock.patch.object(supervisor, "ensure_venv", return_value=Path("/pin/venv/bin/python")) as ensure, \
             mock.patch.object(cli.os, "execv", side_effect=SystemExit(0)) as execv:
            with self.assertRaises(SystemExit):
                cli.main(["limits", "--help"])
        ensure.assert_called_once()
        execv.assert_called_once_with("/pin/venv/bin/python",
                                      ["/pin/venv/bin/python", str(ROOT / "scripts/limits_cli.py"), "--help"])

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
            code = cli.main(["performance", "--since", "2026-10-01"])
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
            code = cli.main(["performance", "--since", "2026-10-01"])
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
            code = cli.main(["performance", "--since", "2026-10-01"])
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
                cli.main(["performance", "--since", "2026-10-01"])
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
        self.assertEqual(reaching, set(cli.STORE_BACKED))


def _help_section(help_text: str, heading: str) -> str:
    body = re.split(rf"(?m)^{re.escape(heading)}:\n", help_text, maxsplit=1)[1]
    return re.split(r"(?m)^[A-Z][\w ]*:$", body, maxsplit=1)[0]


class InitHelpTest(unittest.TestCase):
    """#839: `chief-of-stuff init --help` is the reference an agent discovers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)

    def init(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "chief_of_stuff.py"), "init", *args],
                              cwd=self.cwd, capture_output=True, text=True)

    def test_public_help_meets_the_shared_standard_and_writes_nothing(self):
        self.assertIn("init", ENFORCED)
        done = self.init("--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        self.assertEqual(list(self.cwd.iterdir()), [])
        self.assertEqual(missing_sections(done.stdout, "init"), [], done.stdout)
        self.assertNotIn("init_workspace.py", done.stdout.splitlines()[0])
        text = done.stdout
        options = " ".join(text.split("When not to use", 1)[0].split())
        for flag in ("-h, --help", "--root", "--user", "--timezone", "--daily-dir", "--worktrees", "--agent",
                     "--launcher", "--worker-mode", "--board-port", "--github-repo", "--gitlab-host",
                     "--gitlab-project", "--gitlab-token-env", "--dry-run"):
            self.assertIn(flag, options, flag)
        for fact in ("default false", "required, no default", "default daily/", "default worktrees/",
                     "default ghostty", "default interactive", "default unset",
                     "default CHIEF_OF_STUFF_GITLAB_TOKEN", "1-65535", "mutually exclusive",
                     "NAME:task or NAME:standing"):
            self.assertIn(fact, options, fact)
        avoided = " ".join(_help_section(text, "When not to use").split())
        self.assertIn("chief-of-stuff models --root PATH", avoided)
        self.assertIn("not an update", avoided)
        self.assertIn("#825", avoided)
        self.assertIn("not implemented", avoided)
        self.assertIn("[workspaces]", avoided)
        output = " ".join(_help_section(text, "Output").split())
        for fact in ("Not TOON and not JSON", "stdout", "stderr", "configured ROOT", "would append to",
                     "would create", "would keep existing", "label not created"):
            self.assertIn(fact, output, fact)
        codes = " ".join(_help_section(text, "Exit codes").split())
        for fact in ("0", "1", "2", "usage fault", "still exit 0"):
            self.assertIn(fact, codes, fact)
        effects = " ".join(_help_section(text, "Side effects").split())
        for fact in ("Appends one ## Coordinator block", "never replaced", "only when absent",
                     "needs human", "--dry-run", "no --commit", "immediately", "gh", "token value is never written",
                     "does not create a store", "no rollback"):
            self.assertIn(fact, effects, fact)
        examples = [line.strip() for line in _help_section(text, "Examples").splitlines()
                    if line.strip().startswith("chief-of-stuff init")]
        self.assertEqual(len(examples), 3, examples)
        self.assertIn("Robin", examples[0])
        self.assertIn("/tmp/alder", examples[0])
        self.assertIn("--dry-run", examples[0])
        self.assertIn("America/Chicago", examples[0])
        self.assertIn("Morgan", examples[1])
        self.assertIn("/tmp/birch", examples[1])
        self.assertIn("example-team/birch", examples[1])
        self.assertIn("implementer:task", examples[1])
        self.assertIn("--launcher tmux", examples[1])
        self.assertIn("Casey", examples[2])
        self.assertIn("/tmp/cedar", examples[2])
        self.assertIn("https://gitlab.example.test", examples[2])
        self.assertIn("example-team/cedar", examples[2])
        self.assertIn("CEDAR_GITLAB_TOKEN", examples[2])
        roots = ("/tmp/alder", "/tmp/birch", "/tmp/cedar")
        for example, root in zip(examples, roots):
            self.assertIn(root, example)
            self.assertIn("chief-of-stuff init", example)
            for other in roots:
                if other != root:
                    self.assertNotIn(other, example)

    def test_usage_faults_exit_2_and_write_nothing(self):
        missing = self.init()
        self.assertEqual(missing.returncode, 2, missing.stderr)
        self.assertIn("usage:", missing.stderr)
        self.assertEqual(list(self.cwd.iterdir()), [])
        workspace = self.cwd / "workspace"
        workspace.mkdir()
        clash = self.init("--root", str(workspace), "--user", "Morgan", "--timezone", "America/Chicago",
                          "--github-repo", "example-team/birch", "--gitlab-host", "https://gitlab.example.test")
        self.assertEqual(clash.returncode, 2, clash.stderr)
        self.assertIn("not allowed with", clash.stderr)
        self.assertEqual(list(workspace.iterdir()), [])

    def test_unpaired_gitlab_flags_exit_1_and_dry_run_writes_nothing(self):
        workspace = self.cwd / "cedar"
        workspace.mkdir()
        unpaired = self.init("--root", str(workspace), "--user", "Casey", "--timezone", "America/Chicago",
                             "--gitlab-host", "https://gitlab.example.test")
        self.assertEqual(unpaired.returncode, 1, unpaired.stderr)
        self.assertIn("refused:", unpaired.stderr)
        self.assertIn("requires --gitlab-project", unpaired.stderr)
        self.assertEqual(list(workspace.iterdir()), [])
        preview = self.init("--dry-run", "--root", str(workspace), "--user", "Robin",
                            "--timezone", "America/Chicago", "--github-repo", "example-team/alder")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("would create", preview.stdout)
        self.assertEqual(list(workspace.iterdir()), [])


class PerformanceHelpTest(unittest.TestCase):
    """#850: `chief-of-stuff performance --help` is the reference an agent discovers and trusts."""

    def run_performance(self, *args):
        env = dict(os.environ, COLUMNS="100", NO_COLOR="1", PYTHON_COLORS="0", PYTHONHASHSEED="0")
        return subprocess.run(
            [sys.executable, str(ROOT / "chief_of_stuff.py"), "performance", *args],
            env=env, capture_output=True, text=True,
        )

    def test_performance_help_meets_shared_standard_and_covers_performance_contract(self):
        self.assertIn("performance", ENFORCED)
        done = self.run_performance("--help")
        short = self.run_performance("-h")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        self.assertEqual((short.returncode, short.stdout, short.stderr), (0, done.stdout, ""))
        self.assertEqual(missing_sections(done.stdout, "performance"), [], done.stdout)

        sections = _help_sections(done.stdout)
        purpose_first_line = sections["purpose"].splitlines()[0]
        expected_purpose = performance_report.__doc__.strip().splitlines()[0]
        self.assertEqual(purpose_first_line, expected_purpose)
        self.assertLessEqual(len(purpose_first_line), 100)

        parser = performance_report.build_parser()
        options_text = " ".join(sections["options"].split())
        for action in parser._actions:
            for flag in action.option_strings:
                self.assertIn(flag, options_text, flag)
            if action.option_strings:
                self.assertIn("default:", action.help, f"{action.option_strings} help must state its default")

        help_text = " ".join(done.stdout.split())
        for phrase in (
            "chief-of-stuff board",
            "chief-of-stuff pages",
            "chief-of-stuff tracker",
            "chief-of-stuff health",
            "chief-of-stuff limits",
            "stage segments",
            "store.db",
            "interactive sessions",
            "one-shot",
            "not TOON",
            "not JSON",
            "period",
            "runtime",
            "model",
            "effort",
            "agent_type",
            "runs",
            "completed",
            "human_review",
            "relaunched",
            "median_minutes",
            "Monday",
            "lower middle",
            "performance:",
            "migrate.lock",
            "engine.lock",
            "No preview",
            "--dry-run",
            "--commit",
            "maple-workspace",
            "--raw",
            "attempt records",
        ):
            self.assertIn(phrase, help_text, phrase)

        examples = [line.strip() for line in sections["examples"].splitlines()
                    if line.strip().startswith("chief-of-stuff performance")]
        self.assertEqual(len(examples), 4, examples)
        for example in examples:
            tokens = shlex.split(example)
            self.assertEqual(tokens[:2], ["chief-of-stuff", "performance"])
            parser.parse_args(tokens[2:])

    def test_help_does_not_read_config_or_open_store(self):
        with mock.patch("performance_report.read_config") as read_cfg, \
                mock.patch("performance_report.store_access.migrated_store") as migrated, \
                mock.patch("performance_report.table") as tbl, \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                performance_report.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        read_cfg.assert_not_called()
        migrated.assert_not_called()
        tbl.assert_not_called()

    def test_parser_fault_exits_two_without_store_access(self):
        for bad_argv in ([], ["--since"], ["--by", "month"], ["--unknown"]):
            with self.subTest(argv=bad_argv):
                err = io.StringIO()
                with mock.patch("performance_report.read_config") as read_cfg, \
                        mock.patch("performance_report.store_access.migrated_store") as migrated, \
                        mock.patch("performance_report.table") as tbl, \
                        redirect_stderr(err):
                    with self.assertRaises(SystemExit) as raised:
                        performance_report.main(bad_argv)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("usage: chief-of-stuff performance", err.getvalue())
                read_cfg.assert_not_called()
                migrated.assert_not_called()
                tbl.assert_not_called()

    def test_invalid_date_and_missing_store_exit_two_on_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text(
                "# Workspace\n\n## Coordinator\n\n- User: Robin\n- Daily log dir: `daily/`\n"
                "- Tracker: `daily/<date>-tracker.md`\n- Timezone: UTC\n")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = performance_report.main(["--root", str(root), "--since", "not-a-date"])
            self.assertEqual(code, 2)
            self.assertEqual(out.getvalue(), "")
            self.assertTrue(err.getvalue().startswith("performance: "), err.getvalue())

            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = performance_report.main(["--root", str(root), "--since", "2026-10-01"])
            self.assertEqual(code, 2)
            self.assertEqual(out.getvalue(), "")
            self.assertIn(f"performance: no store at {root.resolve() / 'store.db'}", err.getvalue())

    def test_printed_table_columns_and_empty_key_dash_match_documented_output(self):
        import store

        help_out = io.StringIO()
        with redirect_stdout(help_out):
            with self.assertRaises(SystemExit):
                performance_report.main(["--help"])
        output_section = _help_sections(help_out.getvalue())["output"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text(
                "# Workspace\n\n## Coordinator\n\n- User: Robin\n- Daily log dir: `daily/`\n"
                "- Tracker: `daily/<date>-tracker.md`\n- Timezone: UTC\n")
            store.migrate(root / "store.db")
            db = store.Store(root / "store.db")
            try:
                db.upsert_item("github:example/repo#1", title="sample")
                db.record_segment(
                    "github:example/repo#1", "implement", 1,
                    started_at="2026-10-08T09:00:00+00:00",
                    ended_at="2026-10-08T09:15:00+00:00",
                    outcome="done", runtime="claude", model="sonnet", effort="", agent_type="",
                )
            finally:
                db.engine.dispose()

            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = performance_report.main([
                    "--root", str(root), "--since", "2026-10-08", "--until", "2026-10-08", "--by", "week",
                ])
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        lines = [line.split() for line in out.getvalue().strip().splitlines()]
        self.assertEqual(len(lines), 2, lines)
        headers, row = lines
        for col in headers:
            self.assertIn(f"- {col}:", output_section, col)
        # Thursday 2026-10-08 buckets to Monday 2026-10-05; empty effort, agent_type and workflow print as "-".
        self.assertEqual(row, ["2026-10-05", "claude", "sonnet", "-", "-", "-", "1", "1", "0", "0", "15"])


class HealthHelpTest(unittest.TestCase):
    """#846: `chief-of-stuff health --help` is the reference an agent discovers and trusts."""

    def run_health(self, *args):
        env = dict(os.environ, COLUMNS="100", NO_COLOR="1", PYTHON_COLORS="0", PYTHONHASHSEED="0")
        return subprocess.run(
            [sys.executable, str(ROOT / "chief_of_stuff.py"), "health", *args],
            env=env, capture_output=True, text=True,
        )

    def test_health_help_meets_shared_standard_and_covers_health_contract(self):
        self.assertIn("health", ENFORCED)
        done = self.run_health("--help")
        short = self.run_health("-h")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        self.assertEqual((short.returncode, short.stdout, short.stderr), (0, done.stdout, ""))
        self.assertEqual(missing_sections(done.stdout, "health"), [], done.stdout)

        sections = _help_sections(done.stdout)
        purpose_first_line = sections["purpose"].splitlines()[0]
        expected_purpose = probe_health.__doc__.strip().splitlines()[0]
        self.assertEqual(purpose_first_line, expected_purpose)
        self.assertLessEqual(len(purpose_first_line), 100)

        parser = probe_health.build_parser()
        options_text = " ".join(sections["options"].split())
        for action in parser._actions:
            for flag in action.option_strings:
                self.assertIn(flag, options_text, flag)
            if action.option_strings:
                self.assertIn("default:", action.help, f"{action.option_strings} help must state its default")

        help_text = " ".join(done.stdout.split())
        for phrase in (
            "(advice)",
            "absent, defaults",
            "invalid",
            "runtimes claude",
            "runtimes not checked",
            "no Health: targets",
            "processes",
            "init",
            "audit",
            "--config",
            "--timeout",
            "does not change the exit code",
            "codex, cursor, and agy are not probed",
            "not TOON",
            "not JSON",
            "Reads only",
            "no --commit",
            "cedar-desk",
        ):
            self.assertIn(phrase, help_text, phrase)
        for other in ("runtimes codex", "runtimes cursor", "runtimes agy"):
            self.assertNotIn(other, help_text)

        examples = [line.strip() for line in sections["examples"].splitlines()
                    if line.strip().startswith("chief-of-stuff health")]
        self.assertEqual(len(examples), 3, examples)
        for example in examples:
            tokens = shlex.split(example)
            self.assertEqual(tokens[:2], ["chief-of-stuff", "health"])
            parser.parse_args(tokens[2:])


if __name__ == "__main__":
    unittest.main()
