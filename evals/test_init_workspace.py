"""A fresh workspace gets valid configuration without touching existing content."""

import contextlib
import io
import json
import os
import shlex
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
import forge  # noqa: E402
from workspace import WORKSPACE_FILES, WorkspaceFile, parse_coordinator  # noqa: E402
from settings import _models, load as load_settings, load_policy, workflow_stages  # noqa: E402


def _engine_ready_kanban(stages: list[str], hold: str, *, project: bool | list[str] = False,
                         terminal: list[str] | None = None) -> str:
    """Existing settings the engine can start: every shipped workflow stage is mapped (or terminal)."""
    mapping = "\n".join(f"{s} = {min(i, len(stages) - 1)}" for i, s in enumerate(workflow_stages(load_policy())))
    text = (f'[kanban]\nstages = {json.dumps(stages)}\nhuman_review_label = "{hold}"\n'
            + (f'terminal_stages = {json.dumps(terminal)}\n' if terminal else "")
            + f'[kanban.map]\n{mapping}\n')
    if project:
        options = f"options = {json.dumps(project)}\n" if isinstance(project, list) else ""
        text += f'[kanban.github_project]\nowner = "owner"\nnumber = 12\n{options}'
    return text


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

    def test_config_and_settings_names_come_from_the_file_inventory(self):
        with mock.patch.dict(WORKSPACE_FILES, {
            "config": WorkspaceFile("workspace.md", "chief-of-stuff init --help"),
            "settings": WorkspaceFile("engine.toml", "chief-of-stuff init --help"),
        }):
            self.assertEqual(self.run_init()[0], 0)
            self.assertEqual({p.name for p in self.root.iterdir()}, {"workspace.md", "engine.toml", guidance.NAME})
            config = parse_coordinator((self.root / "workspace.md").read_text(), date.today())
            self.assertEqual(config.settings_path, "engine.toml")

    def test_fresh_repo_can_write_its_first_stage_after_init(self):
        labels, issue_labels = [], []

        def gh(args):
            if args[:2] == ["label", "list"]:
                return 0, json.dumps([{"name": x} for x in labels]), ""
            if args[:2] == ["label", "create"]:
                labels.append(args[2])
            if args[:2] == ["issue", "view"]:
                return 0, json.dumps({"labels": [{"name": x} for x in issue_labels]}), ""
            if args[:2] == ["issue", "edit"]:
                issue_labels.extend(args[args.index("--add-label") + 1].split(","))
            return 0, "", ""

        with mock.patch.object(init.backlog, "run_gh", gh):
            code, _, err = self.run_init()
            self.assertEqual(code, 0, err)
            config = load_settings(self.root, "chief-of-stuff.toml").kanban
            self.assertEqual(labels, [config.human_review_label, *config.stages])
            result = forge._sync_github(init.backlog.IssueRef("owner/repo", 1),
                                        config, "plan", gh=gh, initialize=True, commit=True)
        self.assertTrue(result.done, result.error)
        self.assertEqual(issue_labels, ["stage: plan"])

    def test_readme_quick_start_and_option_description_require_a_forge(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        command = next(line for line in readme.splitlines() if line.startswith("chief-of-stuff init "))
        args = shlex.split(command)[2:]
        args[args.index("--root") + 1] = str(self.root)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(init.main(args), 0, command)
        self.assertIn("Choose a forge", readme)
        optional = readme.split("Optional flags include", 1)[1].split(". ", 1)[0]
        self.assertNotIn("--github-repo", optional)
        self.assertNotIn("--gitlab-host", optional)

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
        body = '[workers]\nlauncher = "tmux"\n' + _engine_ready_kanban(
            ["stage: plan", "stage: implement", "stage: review", "stage: fix", "stage: merge"], "needs human")
        settings.write_text(body)
        self.assertEqual(self.run_init()[0], 0)
        self.assertEqual(settings.read_text(), body)

    def test_existing_settings_without_kanban_refuse(self):
        (self.root / "chief-of-stuff.toml").write_text('[workers]\nlauncher = "tmux"\n')
        code, _, err = self.run_init()
        self.assertEqual(code, 1)
        self.assertIn("[kanban]", err)
        self.assertFalse((self.root / "CLAUDE.md").exists())

    def test_existing_settings_with_incomplete_workflow_map_refuse(self):
        (self.root / "chief-of-stuff.toml").write_text(
            '[kanban]\nstages = ["stage: plan"]\nhuman_review_label = "needs human"\n[kanban.map]\nplan = 0\n')
        code, _, err = self.run_init()
        self.assertEqual(code, 1)
        self.assertIn("[kanban.map]", err)
        self.assertFalse((self.root / "CLAUDE.md").exists())

    def test_init_provisions_a_configured_hold_label(self):
        # #566: init creates the hold label the settings configure, so no hold dies on a label the forge never saw.
        (self.root / "chief-of-stuff.toml").write_text(
            _engine_ready_kanban(["00 - PLAN"], "!! - HUMAN REVIEW REQUIRED"))
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
        self.assertEqual(labels, ["!! - HUMAN REVIEW REQUIRED", "00 - PLAN"])
        self.assertIn("hold label !! - HUMAN REVIEW REQUIRED created on owner/repo", out)

        self.assertEqual(labels, ["!! - HUMAN REVIEW REQUIRED", "00 - PLAN"])
        self.assertIn("hold label !! - HUMAN REVIEW REQUIRED created on owner/repo", out)

    def test_init_provisions_stage_and_terminal_labels_on_both_forges(self):
        (self.root / "chief-of-stuff.toml").write_text(
            _engine_ready_kanban(["00 - PLAN"], "HOLD", terminal=["main"]))
        for args in (("--github-repo", "owner/repo"),
                     ("--gitlab-host", "https://labs.example.test", "--gitlab-project", "team/app")):
            with self.subTest(args=args), mock.patch.object(init.backlog, "ensure_labels", return_value=[]) as ensure:
                code, _, err = self.run_init(*args)
                (self.root / "CLAUDE.md").unlink()
                self.assertEqual(code, 0, err)
                names = [name for c in ensure.call_args_list for name in c.args[1]]
                self.assertEqual(names, ["HOLD", "00 - PLAN", "main"])

    def test_init_checks_project_options_including_terminal_and_reports_missing_main(self):
        (self.root / "chief-of-stuff.toml").write_text(
            _engine_ready_kanban(["00 - PLAN"], "HOLD", project=["Planning"], terminal=["main"]))
        calls = []

        def gh(args):
            calls.append(args)
            if args[:2] == ["project", "field-list"]:
                return 0, json.dumps({"fields": [{"id": "F", "name": "Status", "options": [
                    {"id": "P", "name": "Planning"}]}]}), ""
            raise AssertionError(args)

        with mock.patch.object(init.backlog, "ensure_labels", return_value=[]) as ensure, \
             mock.patch.object(init.backlog, "run_gh", gh):
            code, _, err = self.run_init("--github-repo", "owner/repo")
        self.assertEqual(code, 0, err)
        self.assertEqual([name for c in ensure.call_args_list for name in c.args[1]], ["HOLD"])
        self.assertTrue(calls)
        self.assertIn("main", err)
        self.assertIn("create", err)

    def test_dry_run_names_terminal_marks_in_label_and_project_modes(self):
        path = self.root / "chief-of-stuff.toml"
        for project in (False, True):
            path.write_text(_engine_ready_kanban(["00 - PLAN"], "HOLD", project=["Planning"] if project else None,
                                                 terminal=["main"]))
            with self.subTest(project=project), mock.patch.object(init.backlog, "run_gh") as gh:
                code, out, err = self.run_init("--dry-run", "--github-repo", "owner/repo")
            self.assertEqual(code, 0, err)
            self.assertIn("main", out)
            gh.assert_not_called()

    def test_gitlab_provisions_the_configured_token_env_labels_in_order(self):
        (self.root / "chief-of-stuff.toml").write_text(
            _engine_ready_kanban(["PLAN", "BUILD"], "HOLD", terminal=["main"]))
        labels, created = ["BUILD"], []

        def call(method, url, token, timeout, payload=None):
            if method == "GET":
                return [{"name": x} for x in labels], {}, ""
            self.assertEqual(method, "POST")
            created.append(payload["name"])
            labels.append(payload["name"])
            return {}, {}, ""

        with mock.patch.dict("os.environ", {"TEAM_GITLAB_TOKEN": "fixture-token"}), \
             mock.patch.object(init.backlog, "_call", call):  # the configured token env, not the default name
            os.environ.pop("CHIEF_OF_STUFF_GITLAB_TOKEN", None)  # restored by patch.dict
            code, _, err = self.run_init("--gitlab-host", "https://labs.example.test", "--gitlab-project", "team/app",
                                         "--gitlab-token-env", "TEAM_GITLAB_TOKEN")
        self.assertEqual(code, 0, err)
        self.assertEqual(created, ["HOLD", "PLAN", "main"])

    def test_existing_project_mode_settings_are_kept_and_init_succeeds(self):
        # The engine reads Project Status snapshots now, so a preserved Project-mode file is engine-ready.
        path = self.root / "chief-of-stuff.toml"
        path.write_text(_engine_ready_kanban(["PLAN"], "HOLD", project=True, terminal=["main"]))
        before = path.read_text()
        code, _, err = self.run_init()
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_text(), before)
        self.assertTrue((self.root / "CLAUDE.md").exists())

    def test_preserved_settings_with_a_padded_stage_label_refuse_before_provisioning(self):
        (self.root / "chief-of-stuff.toml").write_text(_engine_ready_kanban([" stage: plan "], "HOLD"))
        with mock.patch.object(init.backlog, "ensure_labels") as ensure:
            code, _, err = self.run_init()
        self.assertEqual(code, 1)
        self.assertIn("whitespace", err)
        ensure.assert_not_called()
        self.assertFalse((self.root / "CLAUDE.md").exists())

    def test_a_hold_label_the_forge_refuses_is_reported_and_init_still_succeeds(self):
        (self.root / "chief-of-stuff.toml").write_text(_engine_ready_kanban(["00 - PLAN"], "HOLD"))
        with mock.patch.object(init.backlog, "token", return_value=""):
            code, out, err = self.run_init("--gitlab-host", "https://labs.example.test", "--gitlab-project", "team/app")
        self.assertEqual(code, 0)
        self.assertIn("hold label not created on team/app", err)
        self.assertIn("stage label not created on team/app", err)

    def test_dry_run_prints_the_hold_label_it_would_create(self):
        (self.root / "chief-of-stuff.toml").write_text(
            _engine_ready_kanban(["00 - PLAN"], "!! - HUMAN REVIEW REQUIRED"))
        with mock.patch.object(init.backlog, "ensure_labels") as ensure:
            code, out, _ = self.run_init("--dry-run", "--github-repo", "owner/repo")
        ensure.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("would create the stage label 00 - PLAN on owner/repo", out)
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
        with mock.patch.object(init.backlog, "token", return_value=""):
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
