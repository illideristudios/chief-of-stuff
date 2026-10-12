"""A fresh workspace gets valid configuration without touching existing content."""

import contextlib
import io
import json
import sys
import tempfile
import tomllib
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import init_workspace as init  # noqa: E402
import install_model_guidance as guidance  # noqa: E402
import engine  # noqa: E402
import issue_gate  # noqa: E402
from workspace import parse_coordinator  # noqa: E402
from settings import _models, load as load_settings  # noqa: E402


class InitWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.args = ["--root", str(self.root), "--user", "Robin", "--timezone", "America/Chicago"]
        fake = mock.patch.object(init.backlog, "run_gh", return_value=(0, "[]", ""))
        fake.start()
        self.addCleanup(fake.stop)

    def run_init(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        forge_args = [] if "--gitlab-host" in extra or "--github-repo" in extra else ["--github-repo", "owner/repo"]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = init.main([*self.args, *forge_args, *extra])
        return code, out.getvalue(), err.getvalue()

    def test_no_forge_choice_refuses_with_setup_and_writes_nothing(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = init.main(self.args)
        self.assertNotEqual(code, 0)
        self.assertIn(issue_gate.SETUP, err.getvalue())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_generated_settings_let_the_real_engine_start(self):
        self.assertEqual(self.run_init()[0], 0)
        err = io.StringIO()
        with mock.patch.object(engine, "run_pass") as passed, contextlib.redirect_stderr(err):
            code = engine.main(["--root", str(self.root), "--once"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(passed.call_count, 1)
        self.assertEqual(passed.call_args.kwargs["forge"].home.repo, "owner/repo")
        self.assertEqual(passed.call_args.kwargs["trees"], self.root.resolve() / "worktrees")
        self.assertTrue((self.root / "store.db").is_file())
        self.assertFalse((self.root / "daily").exists(), "init creates no tracker tables")
        settings = load_settings(self.root, "chief-of-stuff.toml")
        self.assertIsNotNone(settings.kanban)
        self.assertEqual(set(settings.kanban.stage_map), {"plan", "implement", "review", "fix", "merge"})

    def test_fresh_workspace_has_parseable_block_and_explicit_off_settings(self):
        code, _, err = self.run_init("--agent", "implementer:task", "--launcher", "tmux", "--board-port", "8765",
                                     "--github-repo", "owner/repo")
        self.assertEqual(code, 0, err)
        config = parse_coordinator((self.root / "CLAUDE.md").read_text(), date.today())
        self.assertEqual((config.user, config.log_dir, config.tracker_path("2026-09-25")),
                         ("Robin", "daily/", "daily/2026-09-25-tracker.md"))
        self.assertEqual(config.backlog.repo, "owner/repo")
        self.assertEqual(config.board_url, "http://127.0.0.1:8765/")
        self.assertIn("- Agent: implementer task", (self.root / "CLAUDE.md").read_text())
        settings = load_settings(self.root, config.settings_path)
        self.assertEqual((settings.notify.adapter, settings.workers.launcher), ("off", "tmux"))
        self.assertEqual(settings.workers.mode, "interactive")
        self.assertIn("Choice order", (self.root / guidance.NAME).read_text())

    def test_initializer_can_select_one_shot_default(self):
        code, _, err = self.run_init("--worker-mode", "one-shot")
        self.assertEqual(code, 0, err)
        self.assertEqual(load_settings(self.root, "chief-of-stuff.toml").workers.mode, "one-shot")
        # #111: the example rotation is commented out, so a fresh workspace keeps the markdown guidance.
        self.assertEqual(load_settings(self.root, "chief-of-stuff.toml").models, {})
        example = "\n".join(line.removeprefix("# ") for line in
                            (self.root / "chief-of-stuff.toml").read_text().splitlines() if line.startswith("# "))
        self.assertIn("implement", _models(tomllib.loads(example)["models"]))

    def test_the_settings_carry_a_commented_repos_table_that_loads_once_uncommented(self):
        # #54: a new workspace is told where the repositories `worktree` cuts from go, and has none until one is named.
        self.assertEqual(self.run_init()[0], 0)
        path = self.root / "chief-of-stuff.toml"
        lines = path.read_text().splitlines()
        self.assertIn("# [repos]", lines)
        self.assertEqual(load_settings(self.root, "chief-of-stuff.toml").repos, {})
        start = lines.index("# [repos]")
        end = start + 1
        while end < len(lines) and lines[end].startswith("# "):
            end += 1
        lines[start:end] = [line.removeprefix("# ") for line in lines[start:end]]
        path.write_text("\n".join(lines) + "\n")
        self.assertEqual(len(load_settings(self.root, "chief-of-stuff.toml").repos), 1)

    def test_existing_claude_content_is_preserved_and_a_second_init_refuses(self):
        claude = self.root / "CLAUDE.md"
        claude.write_text("# Existing rules\n\nKeep this.\n")
        self.assertEqual(self.run_init()[0], 0)
        first = claude.read_text()
        self.assertTrue(first.startswith("# Existing rules\n\nKeep this.\n"))
        code, _, err = self.run_init()
        self.assertEqual(code, 1)
        self.assertIn("already has", err)
        self.assertEqual(claude.read_text(), first)

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.run_init("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would create", out)
        self.assertFalse((self.root / "CLAUDE.md").exists())
        self.assertFalse((self.root / "chief-of-stuff.toml").exists())
        self.assertFalse((self.root / guidance.NAME).exists())

    def test_existing_settings_are_kept(self):
        settings = self.root / "chief-of-stuff.toml"
        settings.write_text('[workers]\nlauncher = "tmux"\n')
        self.assertEqual(self.run_init()[0], 0)
        self.assertEqual(settings.read_text(), '[workers]\nlauncher = "tmux"\n')

    def test_init_provisions_a_configured_hold_label(self):
        # #566: init creates the hold label the settings configure, so no hold dies on a label the forge never saw.
        (self.root / "chief-of-stuff.toml").write_text(
            '[kanban]\nstages = ["00 - PLAN"]\nhuman_review_label = "!! - HUMAN REVIEW REQUIRED"\n'
            '[kanban.map]\nplan = 0\n')
        labels = []

        def gh(args):
            if args[:2] == ["label", "list"]:
                return 0, json.dumps([{"name": x} for x in labels]), ""
            if args[:2] == ["label", "create"]:
                labels.append(args[2])
            return 0, "", ""

        with mock.patch.object(init.backlog, "run_gh", gh):
            code, out, err = self.run_init("--github-repo", "owner/repo")
        self.assertEqual(code, 0, err)
        self.assertEqual(labels, ["!! - HUMAN REVIEW REQUIRED"])
        self.assertIn("hold label !! - HUMAN REVIEW REQUIRED created on owner/repo", out)

    def test_a_hold_label_the_forge_refuses_is_reported_and_init_still_succeeds(self):
        (self.root / "chief-of-stuff.toml").write_text(
            '[kanban]\nstages = ["00 - PLAN"]\nhuman_review_label = "HOLD"\n[kanban.map]\nplan = 0\n')
        code, out, err = self.run_init("--gitlab-host", "https://labs.example.test", "--gitlab-project", "team/app")
        self.assertEqual(code, 0)
        self.assertIn("hold label not created on team/app", err)

    def test_dry_run_prints_the_hold_label_it_would_create(self):
        (self.root / "chief-of-stuff.toml").write_text(
            '[kanban]\nstages = ["00 - PLAN"]\nhuman_review_label = "!! - HUMAN REVIEW REQUIRED"\n'
            '[kanban.map]\nplan = 0\n')
        code, out, _ = self.run_init("--dry-run", "--github-repo", "owner/repo")
        self.assertEqual(code, 0)
        self.assertIn("would create the hold label !! - HUMAN REVIEW REQUIRED on owner/repo", out)

    def test_existing_model_guidance_is_preserved(self):
        path = self.root / guidance.NAME
        path.write_text("Use the model selected by Robin.\n")
        self.assertEqual(self.run_init()[0], 0)
        self.assertEqual(path.read_text(), "Use the model selected by Robin.\n")

    def test_guidance_only_installs_into_an_existing_workspace(self):
        (self.root / "CLAUDE.md").write_text("## Coordinator\n")
        self.assertEqual(guidance.main(["--root", str(self.root)]), 0)
        path = self.root / guidance.NAME
        self.assertIn("Suggested routing", path.read_text())
        path.write_text("My model policy.\n")
        self.assertEqual(guidance.main(["--root", str(self.root)]), 0)
        self.assertEqual(path.read_text(), "My model policy.\n")

    def test_gitlab_backlog_is_configured_without_a_token_value(self):
        code, _, err = self.run_init("--gitlab-host", "https://gitlab.example.com",
                                     "--gitlab-project", "group/repo", "--gitlab-token-env", "MY_GITLAB_TOKEN")
        self.assertEqual(code, 0, err)
        text = (self.root / "CLAUDE.md").read_text()
        self.assertIn("project group/repo; token env MY_GITLAB_TOKEN", text)
        self.assertNotIn("token value", text)

    def test_invalid_values_refuse_without_writing(self):
        for extra in (("--daily-dir", "../outside"), ("--agent", "bad name:task"),
                      ("--github-repo", "bad"), ("--board-port", "70000"),
                      ("--gitlab-host", "https://gitlab.example.com")):
            with self.subTest(extra=extra):
                code, _, _ = self.run_init(*extra)
                self.assertEqual(code, 1)
                self.assertFalse((self.root / "CLAUDE.md").exists())
                self.assertFalse((self.root / "chief-of-stuff.toml").exists())
