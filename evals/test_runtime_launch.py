"""Focused checks for versioned coordinator launches and non-Claude workers."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import start_coordinator as start  # noqa: E402
import dispatch_prompt as dispatch  # noqa: E402
import session_exec  # noqa: E402
import process_status  # noqa: E402
import runtimes  # noqa: E402
import spawn_session as spawn  # noqa: E402
from evals.test_spawn_session import CLAUDE, TRACKER  # noqa: E402
import evals.test_one_shot as one_shot_tests  # noqa: E402 (module-qualified: its tests are not re-collected here)
from evals.test_dispatch_prompt import SILENT_CHECK_RULE, sections  # noqa: E402
from evals.rules_text import skill_body  # noqa: E402
import git_trees  # noqa: E402
import one_shot  # noqa: E402


class CoordinatorLaunchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        (self.root / "CLAUDE.md").write_text(CLAUDE + "\n- Calendars: none\n")
        self.install_dir = Path(self.tmp.name) / "install"

    def test_install_is_content_addressed_and_keeps_prior_release(self):
        first = start.install(ROOT, self.install_dir)
        again = start.install(ROOT, self.install_dir)
        self.assertEqual(first, again)
        self.assertTrue((first / "scripts" / "spawn_session.py").is_file())
        self.assertTrue((first / "scripts" / "init_workspace.py").is_file())
        self.assertTrue((first / "scripts" / "install_model_guidance.py").is_file())
        self.assertTrue((first / "assets" / "worker-model-guidance.md").is_file())
        self.assertTrue((first / "scripts" / "process_status.py").is_file())
        self.assertTrue((first / "scripts" / "one_shot.py").is_file())
        self.assertTrue((first / "hooks" / "hooks.json").is_file())
        self.assertTrue((first / "scripts" / "board_guard.py").is_file())
        self.assertTrue((first / "scripts" / "_vendor" / "toon_format" / "decoder.py").is_file())
        self.assertTrue((first / "agents" / "chief-of-stuff.md").is_file())
        version = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
        self.assertTrue(first.name.startswith(f"{version}-"))

    def test_concurrent_installs_share_one_complete_release(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            paths = list(pool.map(lambda _: start.install(ROOT, self.install_dir), range(3)))
        self.assertEqual(paths, [paths[0]] * 3)
        self.assertTrue((paths[0] / "scripts" / "dispatch_prompt.py").is_file())

    def test_commands_pin_rules_and_use_host_flags(self):
        release = start.install(ROOT, self.install_dir)
        for runtime, flag in (("claude", "--plugin-dir"), ("codex", "-C"),
                              ("cursor", "--workspace"), ("agy", "-i")):
            with self.subTest(runtime=runtime):
                cmd = start.command(runtime, "/bin/fake", release, self.root)
                self.assertIn(flag, cmd)
                self.assertIn(str(release), " ".join(cmd))
        codex = start.command("codex", "/bin/fake", release, self.root)
        self.assertIn("session-scoped watcher", codex[-1])
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", codex[-1])

    def test_codex_coordinator_has_network_and_names_its_edit_tool(self):
        # Without network the health probe could not resolve DNS and the pages check could not reach loopback;
        # without a named edit tool, the coordinator generated its patches with a script.
        release = start.install(ROOT, self.install_dir)
        cmd = start.command("codex", "/bin/fake", release, self.root)
        self.assertEqual(cmd[1:7], ["-C", str(self.root), "--sandbox", "workspace-write",
                                    "-c", "sandbox_workspace_write.network_access=true"])
        self.assertIn("apply_patch", cmd[-1])

    def test_first_message_does_not_presume_a_new_day(self):
        # A launch on a day that already has a log is a Resume; "Open the day." told it otherwise.
        release = start.install(ROOT, self.install_dir)
        for runtime in runtimes.NAMES:
            with self.subTest(runtime=runtime):
                first = start.command(runtime, "/bin/fake", release, self.root)[-1]
                self.assertTrue(first.endswith(start.START), first[-120:])
                self.assertNotIn("Open the day.", first)
        # A bare `claude --agent chief-of-stuff` sends the frontmatter's initialPrompt instead.
        front = (ROOT / "agents" / "chief-of-stuff.md").read_text().split("---", 2)[1]
        self.assertIn(f'initialPrompt: "{start.START}"', front)

    def test_coordinator_prompts_use_host_reconciliation_checks(self):
        release = start.install(ROOT, self.install_dir)
        expected = {"claude": "[Scheduled check] chief-of-stuff",
                    "agy": "Antigravity `Schedule` at `*/5 * * * *`",
                    "cursor": "Cursor in-session `/loop 5m`",
                    "codex": "Codex CLI has no in-session schedule"}
        for runtime, marker in expected.items():
            with self.subTest(runtime=runtime):
                rendered = start.prompt(runtime, release, self.root)
                self.assertIn(marker, rendered)
                self.assertNotIn("chief-of-stuff inbox", rendered)
                self.assertIn("chief-of-stuff processes --root", rendered)
                self.assertIn(f"Read {release}/skills/review-pipeline/SKILL.md", rendered)
                self.assertIn(f"Read {release}/skills/coordinator-sessions/SKILL.md", rendered)
                self.assertIn(f"Read {release}/skills/dispatch/SKILL.md", rendered)
        # Kanban sync is read on demand when reconciliation advances a stage.
        pipeline = skill_body((release / "skills/review-pipeline/SKILL.md").read_text())
        self.assertIn(
            'After recording a permitted stage transition in the tracker, sync its one issue with '
            '`chief-of-stuff kanban --root <workspace> --date <today> --issue <issue> '
            '--from-stage "<previous stage>" --commit`.', pipeline)
        self.assertEqual(start.WATCH_INTERVAL, 5 * 60)

    def test_every_hosts_prompt_tells_a_quiet_check_to_emit_a_dot(self):
        # #454: start_coordinator.prompt() replaces the Check section on every host but Claude, so the rule must reach them there.
        release = start.install(ROOT, self.install_dir)
        for runtime in runtimes.NAMES:
            check = sections(start.prompt(runtime, release, self.root))["Check"]
            for sentence in SILENT_CHECK_RULE:
                with self.subTest(runtime=runtime, sentence=sentence):
                    self.assertIn(sentence, check)

    def test_host_adapters_preserve_automatic_one_shot_dispatch(self):
        release = start.install(ROOT, self.install_dir)
        for runtime in runtimes.NAMES:
            with self.subTest(runtime=runtime):
                rendered = start.prompt(runtime, release, self.root)
                self.assertIn("automatically assign and launch ready tasks", rendered)
                self.assertIn("never ask for approval of routine one-shot assignments or launches", rendered)
                self.assertIn("Never automatically open the board URL", rendered)
                self.assertNotIn("Never launch a new session without", rendered)
                self.assertNotIn("Do not launch a new session without", rendered)

    def test_claude_worker_loads_the_pinned_hook_and_workspace(self):
        cmd = spawn.runtime_tokens(cwd=str(self.root / "tree"), agent_type=None,
                                   binary=Path("/bin/fake"), workspace=str(self.root))
        self.assertEqual(cmd[cmd.index("--plugin-dir") + 1], str(ROOT))
        self.assertIn(f"CHIEF_OF_STUFF_WORKSPACE={self.root}", cmd)

    def test_missing_calendar_server_does_not_block_launch(self):
        (self.root / "CLAUDE.md").write_text(CLAUDE +
            "\n- Calendar tool: `mcp__claude_ai_Google_Calendar__list_events`\n"
            "- Calendars:\n  - Work: work-id\n")
        output = io.StringIO()
        with mock.patch.object(start, "resolve", return_value="/bin/fake"), \
             mock.patch.object(start.subprocess, "run") as probe, \
             contextlib.redirect_stdout(output):
            code = start.main(["--runtime", "codex", "--root", str(self.root),
                               "--install-dir", str(self.install_dir), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["runtime"], "codex")
        probe.assert_not_called()
        self.assertTrue((self.install_dir / "versions").exists())

    def test_missing_runtime_binary_refuses_before_install(self):
        with mock.patch.object(start, "resolve", return_value=None):
            code = start.main(["--runtime", "cursor", "--root", str(self.root),
                               "--install-dir", str(self.install_dir), "--dry-run"])
        self.assertEqual(code, 1)
        self.assertFalse(self.install_dir.exists())

    def test_all_runtimes_ensure_notifications_before_cli_and_continue_on_failure(self):
        for runtime in start.runtimes.NAMES:
            with self.subTest(runtime=runtime), \
                 mock.patch.object(start, "resolve", return_value="/bin/fake"), \
                 mock.patch.object(start, "login_argv", side_effect=lambda cmd: cmd), \
                 mock.patch.object(start.subprocess, "run", return_value=mock.Mock(
                     returncode=2, stderr="notify: test failure")) as ensure, \
                 mock.patch.object(start.subprocess, "call", return_value=0) as launch, \
                 mock.patch.object(start.threading, "Thread"), \
                 contextlib.redirect_stderr(io.StringIO()) as errors:
                code = start.main(["--runtime", runtime, "--root", str(self.root),
                                   "--install-dir", str(self.install_dir)])
                self.assertEqual(code, 0)
                argv = ensure.call_args.args[0]
                self.assertEqual(argv[-3:], ["--root", str(self.root.resolve()), "ensure"])
                self.assertIn("versions", argv[1])
                launch.assert_called_once()
                self.assertIn("test failure", errors.getvalue())

    def test_watcher_stops_with_its_session_and_notifies_once_per_change(self):
        release = start.install(ROOT, self.install_dir)
        stop = threading.Event()
        calls = []
        with mock.patch.object(start, "check_once", return_value="one finding"), \
             mock.patch.object(start.subprocess, "run", side_effect=lambda *a, **k: calls.append(a[0])):
            worker = threading.Thread(target=start.watcher, args=(self.root, release, stop, 0.01))
            worker.start()
            time.sleep(0.06)
            stop.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls), 1)

    def test_watcher_ignores_clean_audit_summary_and_reports_findings(self):
        release = start.install(ROOT, self.install_dir)
        clean = [mock.Mock(returncode=0, stdout="tasks=1 trees=0 orphaned=0 stopped=0\n"),
                 mock.Mock(returncode=0, stdout="[]")]
        with mock.patch.object(start.subprocess, "run", side_effect=clean):
            self.assertEqual(start.check_once(self.root, release), "")
        finding = [mock.Mock(returncode=0, stdout="orphaned work: task Build it — owner gone\n"
                                                   "tasks=1 trees=0 orphaned=1 stopped=0\n"),
                   mock.Mock(returncode=0, stdout="[]")]
        with mock.patch.object(start.subprocess, "run", side_effect=finding):
            self.assertIn("orphaned work: task Build it", start.check_once(self.root, release))

    def test_watcher_notifies_on_kanban_drift(self):
        release = start.install(ROOT, self.install_dir)
        outputs = [mock.Mock(returncode=1, stdout="kanban: Build it — expected 03, found 00\n"
                                                   "tasks=1 trees=0 kanban=1\n"),
                   mock.Mock(returncode=0, stdout="[]")]
        with mock.patch.object(start.subprocess, "run", side_effect=outputs):
            self.assertIn("kanban: Build it", start.check_once(self.root, release))

    def test_watcher_audits_the_workspace_day_not_the_hosts(self):
        # Between the host's midnight and the workspace's, the host's date names the wrong tracker.
        release = start.install(ROOT, self.install_dir)
        clean = [mock.Mock(returncode=0, stdout="tasks=1 trees=0 orphaned=0 stopped=0\n"),
                 mock.Mock(returncode=0, stdout="[]")]
        host = mock.Mock(wraps=start.date, **{"today.return_value": start.date(2000, 1, 1)})
        with mock.patch.object(start.subprocess, "run", side_effect=clean) as run, \
                mock.patch.object(start, "date", host):
            start.check_once(self.root, release)
        self.assertNotIn("2000-01-01", run.call_args_list[0].args[0])

    def test_watcher_uses_batch_process_helper_and_reports_reused_pid(self):
        release = start.install(ROOT, self.install_dir)
        rows = [{"pid": 123, "name": "impl01", "runtime": "cursor", "status": "pid_reused"}]
        outputs = [mock.Mock(returncode=0, stdout="tasks=1 trees=0 orphaned=0 stopped=0\n"),
                   mock.Mock(returncode=0, stdout=process_status.toon_encode(rows))]
        with mock.patch.object(start.subprocess, "run", side_effect=outputs) as run:
            self.assertIn("worker impl01 process is pid_reused", start.check_once(self.root, release))
        self.assertEqual(Path(run.call_args_list[1].args[0][1]).name, "process_status.py")

    def test_watcher_reads_what_processes_prints_for_a_live_one_shot_run_with_and_without_a_registered_worker(self):
        # The real `processes` output, not a canned one: the watcher decodes it as one list of rows.
        release = start.install(ROOT, self.install_dir)
        pidfile = self.root / "trees" / "rate-limit" / ".chief-of-stuff" / "one-shot.pid"
        pidfile.parent.mkdir(parents=True)
        pidfile.write_text(f"{os.getpid()} Rate limit headers\n")  # this process: alive for the whole test
        for what, register in (("one-shot alone", False), ("with a registered worker", True)):
            with self.subTest(what):
                if register:
                    session_exec.register(self.root / ".chief-of-stuff" / "sessions" / "worker.json", runtime="cursor",
                                          name="impl01", worktree="/tmp/impl01-tree", pid=os.getpid())
                out = io.StringIO()
                with contextlib.redirect_stdout(out), mock.patch.object(
                        process_status.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=f" {os.getpid()} /bin/agent\n")):
                    self.assertEqual(process_status.main(["--root", str(self.root)]), 0)
                outputs = [mock.Mock(returncode=0, stdout="tasks=1 trees=0 orphaned=0 stopped=0\n"),
                           mock.Mock(returncode=0, stdout=out.getvalue())]
                with mock.patch.object(start.subprocess, "run", side_effect=outputs):
                    self.assertNotIn("could not be parsed", start.check_once(self.root, release))

    def test_cursor_launch_with_fake_cli(self):
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "agent"
        record = Path(self.tmp.name) / "cursor-argv.json"
        fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                        f"open({str(record)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], "
                        "'pinned': os.environ.get('CHIEF_OF_STUFF_RELEASE')}))\n")
        fake.chmod(0o755)
        home = Path(self.tmp.name) / "home"
        home.mkdir()
        (home / ".zshrc").write_text(f'export PATH="{bin_dir}:$PATH"\n')
        env = dict(os.environ, HOME=str(home), SHELL="/bin/zsh")
        proc = subprocess.run([sys.executable, str(ROOT / "start_coordinator.py"), "--runtime", "cursor",
                               "--root", str(self.root), "--install-dir", str(self.install_dir)],
                              env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        launched = json.loads(record.read_text())
        argv = launched["argv"]
        self.assertEqual(argv[:2], ["--workspace", str(self.root.resolve())])
        self.assertIn("## Host adapter", argv[-1])
        self.assertEqual(Path(launched["pinned"]).parent, (self.install_dir / "versions").resolve())


class WorkerRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "CLAUDE.md").write_text(CLAUDE)
        (self.root / "daily").mkdir()
        (self.root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)

    def test_codex_and_cursor_launch_in_planning_state(self):
        codex = spawn.ghostty_script(cwd="/tmp/wt", agent_type=None, claude=Path("/bin/codex"),
            title="impl01", runtime="codex", workspace=str(self.root))
        cursor = spawn.ghostty_script(cwd="/tmp/wt", agent_type=None, claude=Path("/bin/agent"),
            title="impl02", runtime="cursor", workspace=str(self.root))
        self.assertIn("/bin/codex -C /tmp/wt --sandbox read-only", codex)
        self.assertIn("/bin/agent --workspace /tmp/wt --mode plan", cursor)
        for text in (codex, cursor):
            self.assertIn("session_exec.py", text)
            self.assertIn("--registry", text)
            self.assertIn("--login-shell", text)

    def test_assignment_names_engine_tools_and_plan_gates(self):
        for runtime in runtimes.NAMES:
            body = dispatch.compose(self.root, "2026-09-18", "Security audit",
                                    worktree=Path("/tmp/wt"), name="impl01", runtime=runtime)
            self.assertIn("engine MCP tool `report`", body)
            self.assertNotIn("chief-of-stuff inbox", body)
            if runtime in ("codex", "cursor"):
                self.assertIn("List your sessions", body)
                self.assertIn("/plan", body)
            if runtime == "codex":
                self.assertIn("resume-codex", body)
                self.assertIn("--session-id <session-id>", body)

    def test_registration_and_liveness_use_the_assigned_identity(self):
        path = self.root / ".chief-of-stuff" / "sessions" / "impl01.json"
        session_exec.register(path, runtime="cursor", name="impl01", worktree="/tmp/wt", pid=123)
        record = json.loads(path.read_text())
        self.assertEqual((record["name"], record["runtime"], record["worktree"]),
                         ("impl01", "cursor", "/tmp/wt"))
        with mock.patch.object(session_exec.os, "kill") as kill, \
             mock.patch.object(session_exec.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="/bin/agent --workspace /tmp/wt")):
            self.assertTrue(session_exec.alive(path))
            kill.assert_called_once_with(123, 0)
        with mock.patch.object(session_exec.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(session_exec.alive(path))

    def test_wrapper_registers_before_exec_of_fake_cursor(self):
        fake = self.root / "fake-agent"
        marker = self.root / "seen.json"
        fake.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                        f"open({str(marker)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
        fake.chmod(0o755)
        registry = self.root / ".chief-of-stuff" / "sessions" / "impl01.json"
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "session_exec.py"),
            "--registry", str(registry), "--runtime", "cursor", "--name", "impl01",
            "--worktree", str(self.root), "--", str(fake), "--mode", "plan"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(marker.read_text()), ["--mode", "plan"])
        self.assertEqual(json.loads(registry.read_text())["name"], "impl01")
        self.assertFalse(session_exec.alive(registry))

    def test_wrapper_loads_login_zsh_setup_when_requested(self):
        home = self.root / "home"
        home.mkdir()
        (home / ".zshrc").write_text("export CHIEF_SHELL_MARKER=from-zshrc\n")
        fake = self.root / "fake-agent"
        marker = self.root / "shell-marker.txt"
        fake.write_text("#!/usr/bin/env python3\nimport os,sys\n"
                        f"open({str(marker)!r}, 'w').write(os.environ.get('CHIEF_SHELL_MARKER', 'missing'))\n")
        fake.chmod(0o755)
        registry = self.root / ".chief-of-stuff" / "sessions" / "impl02.json"
        env = dict(os.environ, HOME=str(home))
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "session_exec.py"),
            "--registry", str(registry), "--runtime", "cursor", "--name", "impl02",
            "--worktree", str(self.root), "--login-shell", "--", str(fake)],
            env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(marker.read_text(), "from-zshrc")


class ResumeCodexGrantTest(unittest.TestCase):
    """#428: `resume-codex` grants the same validated git directories as the one-shot path, after the existing args."""
    _repo_with_linked_tree = one_shot_tests.CommandTest._repo_with_linked_tree
    _other_repo_with_worktree = one_shot_tests.CommandTest._other_repo_with_worktree
    _git = one_shot_tests.CommandTest._git

    class Exec(Exception):
        pass

    def resume(self, tmp, worktree):
        """The argv `resume-codex` hands to exec_login, with the exec itself mocked."""
        root = Path(tmp) / "workspace"
        root.mkdir(exist_ok=True)
        with mock.patch.object(session_exec, "resolve", return_value="/bin/fake-codex"), \
             mock.patch.object(session_exec, "exec_login", side_effect=self.Exec) as login:
            with self.assertRaises(self.Exec):
                session_exec.main(["resume-codex", "--root", str(root), "--name", "impl01",
                                   "--worktree", str(worktree), "--session-id", "sid-1"])
        return login.call_args.args[0]

    @staticmethod
    def old(worktree):
        return ["/bin/fake-codex", "resume", "sid-1", "-C", str(worktree), "-s", "workspace-write"]

    def test_a_linked_worktree_resumes_with_its_common_dir_then_its_own_gitdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, tree = self._repo_with_linked_tree(tmp)
            self.assertEqual(self.resume(tmp, tree), [
                *self.old(tree), "--add-dir", str((base / ".git").resolve()),
                "--add-dir", str((base / ".git/worktrees/tree").resolve())])

    def test_a_normal_clone_resumes_with_its_dot_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            subprocess.run(["git", "init", "-q", str(base)], check=True)
            self.assertEqual(self.resume(tmp, base), [*self.old(base), "--add-dir", str((base / ".git").resolve())])

    def test_a_plain_dir_and_a_clone_subdirectory_resume_with_the_six_old_elements(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, plain = Path(tmp) / "base", Path(tmp) / "plain"
            subprocess.run(["git", "init", "-q", str(base)], check=True)
            (base / "src").mkdir()
            plain.mkdir()
            for cwd in (plain, base / "src"):
                with self.subTest(cwd=str(cwd)):
                    self.assertEqual(self.resume(tmp, cwd), self.old(cwd))

    def test_the_resume_grant_equals_the_one_shot_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, tree = self._repo_with_linked_tree(tmp)
            shot = one_shot.command("codex", "/bin/fake", tree, tree / "d.md", agent_type=None, model="", effort="")
            resume = self.resume(tmp, tree)
            self.assertIn("--add-dir", resume)
            self.assertEqual(shot[shot.index("--add-dir"):][:4], resume[resume.index("--add-dir"):])
            self.assertEqual(resume[len(self.old(tree)):], git_trees.codex_add_dir_args(tree))

    def test_a_subdirectory_with_a_forged_back_pointer_resumes_with_the_six_old_elements(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, tree = self._repo_with_linked_tree(tmp)
            sub = tree / "sub"
            sub.mkdir()
            (base / ".git/worktrees/tree/gitdir").write_text(f"{sub / '.git'}\n")
            self.assertEqual(self.resume(tmp, sub), self.old(sub))

    def test_the_resume_grant_spawns_no_git_process(self):
        """#443: the grant is read from the tree's pointer files (`git_view.locate`), so a git that hangs or is missing cannot touch it."""
        with tempfile.TemporaryDirectory() as tmp:
            base, tree = self._repo_with_linked_tree(tmp)
            with mock.patch.object(git_trees.subprocess, "run", side_effect=AssertionError("git spawned")), \
                 mock.patch.object(git_trees.subprocess, "Popen", side_effect=AssertionError("git spawned")):
                self.assertEqual(self.resume(tmp, tree), [
                    *self.old(tree), "--add-dir", str((base / ".git").resolve()),
                    "--add-dir", str((base / ".git/worktrees/tree").resolve())])

    def test_an_unviewable_tree_resumes_with_the_six_old_elements_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, tree = self._repo_with_linked_tree(tmp)
            (tree / ".git").write_text("gitdir: " + str(Path(tmp) / "no-such-gitdir") + "\n")
            self.assertEqual(self.resume(tmp, tree), self.old(tree))

    def test_a_damaged_or_hostile_gitfile_resumes_with_the_six_old_elements_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, tree = self._repo_with_linked_tree(tmp)
            other, _ = self._other_repo_with_worktree(tmp)
            victim, twin = Path(tmp) / "victim", Path(tmp) / "twin"
            subprocess.run(["git", "init", "-q", str(victim)], check=True)
            self._git("-C", str(base), "worktree", "add", "-q", str(twin))
            (twin / ".git").write_text(f"gitdir: {other / '.git/worktrees/other-tree'}\n")
            self.assertEqual(self.resume(tmp, twin), self.old(twin))
            (tree / ".git").write_text(f"gitdir: {victim / '.git'}\n")
            self.assertEqual(self.resume(tmp, tree), self.old(tree))
            (tree / ".git").write_text("\0 not a gitfile")
            self.assertEqual(self.resume(tmp, tree), self.old(tree))
            (tree / ".git").unlink()
            self.assertEqual(self.resume(tmp, tree), self.old(tree))


if __name__ == "__main__":
    unittest.main()
