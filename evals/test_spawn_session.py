"""Unit tests for scripts/spawn_session.py: the only file in the plugin that starts a process.

The launcher template never comes from the workspace. A working session can edit `CLAUDE.md`
freely — the coordinator's limits bind the coordinator and nobody else — so a launch template read
from there would be arbitrary argv, executed on the user's machine, with a config file as the
carrier. It lives in the plugin, and the eval overrides it through the environment.

Nothing here starts a real terminal: every test drives `--dry-run` or a recorder.
"""

import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import one_shot  # noqa: E402
import spawn_session as ss  # noqa: E402

CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Worktrees: `trees/`
- Timezone: America/Chicago
"""

TRACKER = """# Tracker 2026-09-18

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Security audit | unassigned | open | 09:00 |  | Checklist: Security audit of the upload handler |

## File ownership

| context | paths |
|---|---|
| Security audit | `src/a/` |

## Log

- 09:00 opened the day
"""



class ArgvTest(unittest.TestCase):
    def test_the_default_launcher_opens_a_terminal_and_runs_claude_in_the_tree(self):
        argv = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd="/tmp/wt", title="wt-docs")
        self.assertEqual(argv[0], "ghostty")
        self.assertIn("--working-directory=/tmp/wt", argv)
        self.assertIn("claude", argv)
        self.assertIn("--agent", argv)
        self.assertIn("implementer", argv)

    def test_a_dispatched_session_starts_in_plan_mode(self):
        """The plan gate is enforced at launch, not asked for in the assignment text."""
        argv = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd="/tmp/wt", title="t")
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")

    def test_substitution_is_per_token_so_a_space_cannot_add_an_argument(self):
        argv = ss.argv(["run", "--title={title}", "--", "claude"], agent_type="x", cwd="/tmp/a b", title="two words")
        self.assertIn("--title=two words", argv)
        self.assertEqual(len(argv), 4)

    def test_a_value_that_looks_like_a_flag_stays_one_token(self):
        argv = ss.argv(["run", "{title}"], agent_type="x", cwd="/tmp", title="--dangerous")
        self.assertEqual(argv, ["run", "--dangerous"])
        self.assertEqual(len(argv), 2, "a value never splits into more argv entries than it occupies")

    def test_an_unset_field_drops_its_flag_only_when_the_flag_and_value_are_separate_tokens(self):
        # A `-c VALUE` pair goes together; a placeholder that is its own flag (`--model={model}`) goes alone,
        # and the flag before it stays.
        for template, want in (
            (["wrap", "-C", "{cwd}", "-x", "--effort={effort}", "run"], ["wrap", "-C", "/tmp/wt", "-x", "run"]),
            (["wrap", "-c", "model_reasoning_effort={effort}", "run"], ["wrap", "run"]),
            (["wrap", "--bool", "--model={model}", "go"], ["wrap", "--bool", "go"]),
        ):
            with self.subTest(template=template):
                self.assertEqual(ss.argv(template, agent_type=None, cwd="/tmp/wt", title="t"), want)

    def test_an_unknown_placeholder_is_refused_rather_than_left_in_the_argv(self):
        with self.assertRaises(ss.RefusedError):
            ss.argv(["run", "{whatever}"], agent_type="x", cwd="/tmp", title="t")


class LauncherSourceTest(unittest.TestCase):
    def test_a_shell_as_argv0_is_refused(self):
        for shell in ("sh", "bash", "zsh", "env", "eval", "/bin/sh"):
            with self.assertRaises(ss.RefusedError, msg=shell):
                ss.launcher([shell, "-c", "claude"])

    def test_a_metacharacter_anywhere_in_the_template_is_refused(self):
        for bad in (["ghostty", "-e", "claude; rm -rf /"], ["ghostty", "-e", "claude && x"], ["ghostty", "$(x)"], ["ghostty", "a|b"]):
            with self.assertRaises(ss.RefusedError, msg=str(bad)):
                ss.launcher(bad)

    def test_an_empty_template_is_refused(self):
        with self.assertRaises(ss.RefusedError):
            ss.launcher([])

    def test_the_environment_overrides_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Path(d) / "rec.py"
            rec.write_text("#!/usr/bin/env python3\n")
            env = json.dumps(["python3", str(rec), "{cwd}", "{title}"])
            with unittest.mock.patch.dict(os.environ, {ss.ENV: env}):
                self.assertEqual(ss.launcher()[0], "python3")

    def test_a_malformed_override_is_refused_not_ignored(self):
        with unittest.mock.patch.dict(os.environ, {ss.ENV: "ghostty -e claude"}):
            with self.assertRaises(ss.RefusedError):
                ss.launcher()


class RunTest(unittest.TestCase):
    def test_dry_run_prints_the_argv_and_starts_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "implementer", "--cwd", "/tmp", "--title", "t",
                 "--root", str(root), "--date", "2026-09-18", "--task", "Security audit", "--dry-run"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("new tab in front window", out.stdout)
            self.assertIn("would run", out.stdout)
            self.assertIn("would write", out.stdout)

    def test_it_runs_the_launcher_and_reports_what_it_started(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            tree = root / "trees" / "wt-x"
            tree.mkdir(parents=True)
            log = root / "calls.jsonl"
            rec = root / "rec.py"
            rec.write_text(
                "#!/usr/bin/env python3\nimport json,sys\n"
                f"open({str(log)!r},'a').write(json.dumps({{'argv': sys.argv[1:]}})+chr(10))\n"
            )
            env = dict(os.environ, **{ss.ENV: json.dumps(["python3", str(rec), "{cwd}", "{title}", "{type}"])})
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "fixer", "--cwd", str(tree), "--title", "wt-x",
                 "--root", str(root), "--date", "2026-09-18", "--task", "Security audit"],
                capture_output=True, text=True, timeout=30, env=env,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            recorded = json.loads(log.read_text().splitlines()[0])
            self.assertEqual(recorded["argv"], [str(tree), "wt-x", "fixer"])

    def test_the_dry_run_line_cannot_be_read_as_a_shell_line(self):
        """The argv is right, but a human reads the printed line — and may paste it into a shell.

        A path carrying a space and a semicolon must print with its boundaries visible, or the
        preview reads as `... ; rm -rf ~ ...` and is true when pasted. This used a hostile title until
        the title became the session's name, which is refused unless it is a bare session name.
        """
        cwd = "/tmp/--dangerous ; rm -rf ~"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "implementer", "--cwd", cwd, "--name", "t",
                 "--root", str(root), "--date", "2026-09-18", "--task", "Security audit", "--dry-run"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("; rm -rf ~ -e", out.stdout, "the path's tokens run into the rest of the argv")
        self.assertIn(shlex.quote(cwd), out.stdout, "one path must print as one quoted word")

    def test_a_hostile_name_starts_nothing(self):
        """The name reaches `claude --name`, so it is a bare session name or it is refused."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "implementer", "--cwd", "/tmp",
                 "--name=--dangerous ; rm -rf ~", "--root", str(root), "--date", "2026-09-18",
                 "--task", "Security audit", "--dry-run"],
                capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(out.returncode, 1)
        self.assertIn("refused", out.stderr)
        self.assertNotIn("would run", out.stdout)

    def test_the_override_path_still_quotes_every_argv_entry(self):
        """The eval harness builds an argv, so the per-token invariant still has to hold there."""
        cwd = "/tmp/--dangerous ; rm -rf ~"
        env = dict(os.environ, **{ss.ENV: json.dumps(["ghostty", "--working-directory={cwd}", "-e", "claude"])})
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "implementer", "--cwd", cwd, "--name", "t",
                 "--root", str(root), "--date", "2026-09-18", "--task", "Security audit", "--dry-run"],
                capture_output=True, text=True, timeout=30, env=env,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("'--working-directory=/tmp/--dangerous ; rm -rf ~'", out.stdout,
                      "one argv entry must print as one quoted word")

    def test_a_refusal_exits_nonzero_and_says_why(self):
        env = dict(os.environ, **{ss.ENV: json.dumps(["sh", "-c", "claude"])})
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            out = subprocess.run(
                [sys.executable, str(Path(ss.__file__)), "--type", "x", "--cwd", "/tmp", "--title", "t",
                 "--root", str(root), "--date", "2026-09-18", "--task", "Security audit", "--dry-run"],
                capture_output=True, text=True, timeout=30, env=env,
            )
            self.assertEqual(out.returncode, 1)
            self.assertIn("refused", out.stderr)


class NoShellTest(unittest.TestCase):
    def test_the_source_never_reaches_for_a_shell(self):
        source = Path(ss.__file__).read_text()
        for forbidden in ("shell=True", "os.system", "shlex.split"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_it_never_closes_anything(self):
        """Spawning is the coordinator's; ending a session, removing a tree, deleting a branch is not.

        Call shapes, not substrings: `skills` contains `kill`, and a check that cannot tell those
        apart fails on a word rather than on a behaviour.
        """
        source = Path(ss.__file__).read_text()
        # The dispatch this run wrote, taken back when its launch failed, is not a session, tree or branch
        # (Zach, 2026-09-29: narrow the guard to exactly this line).
        source = source.replace("written.unlink(missing_ok=True)", "")
        for forbidden in (r"\bos\.kill\b", r"\.kill\(", r"\.terminate\(", r"\brmtree\b",
                          r"worktree\s+remove", r"branch\s+-[dD]\b", r"\.unlink\(", r"os\.remove\b"):
            self.assertIsNone(re.search(forbidden, source), forbidden)


class BootstrapTest(unittest.TestCase):
    """The argv carries a constant. Nothing the coordinator composed travels on the command line."""

    def test_the_last_argv_token_is_the_bootstrap_and_points_at_the_dispatch_file(self):
        """`BOOTSTRAP` became a template when the paths went absolute, so the token is the filled
        form rather than the constant. The invariant it guards is unchanged: one token, naming the
        assignment, and nothing the coordinator composed."""
        argv = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd="/tmp/wt", title="wt-docs")
        self.assertIn(ss.PROMPT_FILE, argv[-1])
        self.assertIn("/tmp/wt", argv[-1])

    def test_the_argv_is_the_same_whatever_the_task_is(self):
        """There is no per-dispatch text on the command line, so there is nothing to expand."""
        a = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd="/tmp/a", title="t")
        b = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd="/tmp/a", title="t")
        self.assertEqual(a, b)
        self.assertNotIn("Task:", " ".join(a))

    def test_no_agent_type_drops_the_flag_and_its_value_together(self):
        """Removed as a pair, never substituted empty: a token must not expand to zero or two."""
        argv = ss.argv(ss.DEFAULT_LAUNCHER, agent_type=None, cwd="/tmp/wt", title="t")
        self.assertNotIn("--agent", argv)
        self.assertNotIn("", argv)
        self.assertIn("claude", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")

    def test_an_agent_type_is_passed_when_the_block_names_one(self):
        argv = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="fixer", cwd="/tmp/wt", title="t")
        self.assertEqual(argv[argv.index("--agent") + 1], "fixer")


class GhosttyTabTest(unittest.TestCase):
    """The real launcher: a tab in the window the user already has, not a window of its own.

    Design-input 44 said "a Ghostty tab running `claude`" in Zach's own words and 0.7.0 shipped a
    window. Ghostty's scripting dictionary has `new tab ... with configuration`, so the tab is asked
    for rather than simulated with keystrokes.
    """

    CLAUDE_BIN = ss.Path("/opt/homebrew/bin/claude")

    def script(self, agent_type="implementer", cwd="/tmp/trees/wt-audit", **kw):
        kw.setdefault("claude", self.CLAUDE_BIN)
        return ss.ghostty_script(cwd=cwd, agent_type=agent_type, **kw)

    def test_it_asks_for_a_tab(self):
        self.assertIn("new tab in front window with configuration", self.script())

    def test_it_makes_a_window_when_there_is_none_to_put_a_tab_in(self):
        s = self.script()
        self.assertIn("new window with configuration", s)
        self.assertIn("count of windows", s)

    def test_the_tab_takes_the_title_it_was_given(self):
        """A surface configuration has no title field; `set_tab_title:` is how a tab gets a name."""
        self.assertIn('set_tab_title:wt-audit', self.script(title="wt-audit"))

    def test_the_session_starts_in_its_worktree(self):
        self.assertIn('set initial working directory of cfg to "/tmp/trees/wt-audit"', self.script())

    def test_the_tab_outlives_a_command_that_dies(self):
        """Ghostty calls any sub-second exit a launch failure; without this the evidence closes itself."""
        self.assertIn("set wait after command of cfg to true", self.script())

    def test_the_command_is_quoted_per_token(self):
        """Ghostty parses `command` shell-style, so the bootstrap stays one argument or it is nonsense."""
        s = self.script()
        # The filled bootstrap, since it carries paths now; still exactly one shell token.
        filled = [t for t in shlex.split(s.split("set command of cfg to ")[1].split("\n")[0].strip('"').replace('\\"', '"'))
                  if t.startswith("Read the file ")]
        self.assertEqual(len(filled), 1, filled)

    def test_no_agent_type_drops_the_flag_and_its_value_together(self):
        self.assertNotIn("--agent", self.script(agent_type=None))
        self.assertIn("--agent implementer", self.script())

    def test_it_passes_no_environment_of_its_own(self):
        """The tab inherits Ghostty's environment, which is the user's login session and not a coordinator's.

        That is what the KEEP whitelist was approximating; asking Ghostty for a tab gets it exactly.
        """
        s = self.script()
        self.assertNotIn("environment variables", s)
        self.assertNotIn("CLAUDE", s)

    def test_the_tab_gets_the_launching_path(self):
        """A `command` tab skips the login shell, so Ghostty's PATH is launchd's: no /opt/homebrew/bin.

        Sessions spawned that way ran without rtk, gh or glab — every Bash call failed the rtk hook with
        `rtk: command not found`, and `gh pr create` would have too. The launching shell's PATH is
        what a terminal the user opened has.
        """
        with unittest.mock.patch.dict(os.environ, {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"}):
            s = self.script()
        self.assertIn(" PATH=/opt/homebrew/bin:/usr/bin:/bin ", s)

    def test_a_quote_cannot_end_the_applescript_string(self):
        s = self.script(cwd='/tmp/a"b', agent_type=None)
        self.assertIn('\\"', s)
        self.assertNotIn('"/tmp/a"b"', s)

    def test_a_backslash_is_escaped_before_anything_else(self):
        s = self.script(cwd="/tmp/a\\b", agent_type=None)
        self.assertIn("\\\\", s)

    def test_a_control_character_is_refused_rather_than_escaped(self):
        with self.assertRaises(ss.RefusedError):
            self.script(cwd="/tmp/a\x1b]0;x\x07", agent_type=None)

    def test_the_binary_is_resolved_absolutely(self):
        """Ghostty is launched from the GUI, so its children get launchd's PATH, not a shell's.

        The first real tab died in 38ms with `exec: claude: not found`, while `which claude` in the
        coordinator answered `/opt/homebrew/bin/claude`. Restoring a PATH would put the environment
        surface back; resolving the binary here does not.
        """
        s = self.script(cwd="/tmp", agent_type=None)
        self.assertIn("/opt/homebrew/bin/claude", s)
        self.assertNotIn("command:\"claude ", s)

    def test_a_claude_that_cannot_be_found_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(ss.RefusedError) as e:
            ss.ghostty_script(cwd="/tmp", agent_type=None, claude=None)
        self.assertIn("claude", str(e.exception))

    def test_it_never_reaches_a_shell(self):
        """The one file that starts processes gained a second quoting layer; it did not gain a shell."""
        s = self.script()
        for shellish in ("do shell script", "/bin/sh", "bash -c", "osascript -e"):
            self.assertNotIn(shellish, s)


class DispatchFileTest(unittest.TestCase):
    """The assignment lands in the tree before the session that reads it exists."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / "CLAUDE.md").write_text(CLAUDE)
        (self.root / "daily").mkdir()
        (self.root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
        self.tree = self.root / "trees" / "wt-audit"
        self.tree.mkdir(parents=True)

    def spawn(self, task="Security audit", **kw):
        log = self.root / "calls.jsonl"
        rec = self.root / "rec.py"
        rec.write_text(
            "#!/usr/bin/env python3\nimport json,os,sys\n"
            f"open({str(log)!r},'a').write(json.dumps({{'argv': sys.argv[1:], "
            f"'file_there': os.path.exists({str(self.tree / ss.PROMPT_FILE)!r})}})+chr(10))\n"
        )
        env = dict(os.environ, **{ss.ENV: json.dumps(["python3", str(rec), "{cwd}", "{title}"])})
        args = ["--cwd", str(self.tree), "--title", "wt-audit", "--root", str(self.root),
                "--date", "2026-09-18", "--task", task]
        out = subprocess.run([sys.executable, str(Path(ss.__file__)), *args, *kw.get("extra", [])],
                             capture_output=True, text=True, timeout=30, env=env)
        calls = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return out, calls

    def test_the_task_lands_in_the_dispatch_file(self):
        out, _ = self.spawn()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Task: Security audit", (self.tree / ss.PROMPT_FILE).read_text())

    def test_the_directory_ignores_itself_so_the_tree_stays_clean(self):
        """An untracked file here would read as uncommitted work in every spawned tree."""
        self.spawn()
        self.assertEqual((self.tree / ".chief-of-stuff" / ".gitignore").read_text().strip(), "*")

    def test_the_file_is_there_before_the_process_starts(self):
        _, calls = self.spawn()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["file_there"], "the session could read a file that is not there yet")

    def test_a_task_that_is_not_a_row_writes_nothing_and_starts_nothing(self):
        out, calls = self.spawn(task="Upload handler fix")
        self.assertEqual(out.returncode, 1)
        self.assertIn("refused", out.stderr)
        self.assertFalse((self.tree / ss.PROMPT_FILE).exists())
        self.assertEqual(calls, [])

    def test_an_expansion_that_reached_the_script_is_refused_as_an_unknown_row(self):
        out, calls = self.spawn(task="locriani")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(calls, [])

    def test_a_tree_that_already_holds_a_dispatch_is_refused(self):
        (self.tree / ".chief-of-stuff").mkdir()
        (self.tree / ss.PROMPT_FILE).write_text("someone else's assignment\n")
        out, calls = self.spawn()
        self.assertEqual(out.returncode, 1)
        self.assertIn("already", out.stderr)
        self.assertTrue(out.stderr.startswith("refused: "), out.stderr)
        self.assertEqual((self.tree / ss.PROMPT_FILE).read_text(), "someone else's assignment\n")
        self.assertEqual(calls, [])

    def test_the_coordinator_it_registers_with_reaches_the_assignment(self):
        """The ref arrives from the session rather than from the coordinator noticing it in a listing."""
        out, calls = self.spawn(extra=["--coordinator", "gauntlet-d3 [0d6cf4]"])
        body = (self.tree / ss.PROMPT_FILE).read_text()
        self.assertIn("gauntlet-d3 [0d6cf4]", body)
        self.assertIn("Register first", body)

    def test_it_still_says_to_register_when_no_coordinator_was_named(self):
        self.spawn()
        self.assertIn("chief-of-stuff coordinator", (self.tree / ss.PROMPT_FILE).read_text())

    def test_a_coordinator_that_is_not_a_session_name_writes_nothing_and_starts_nothing(self):
        out, calls = self.spawn(extra=["--coordinator", "$(whoami)"])
        self.assertEqual(out.returncode, 1)
        self.assertIn("refused", out.stderr)
        self.assertFalse((self.tree / ss.PROMPT_FILE).exists())
        self.assertEqual(calls, [])

    def test_the_assignment_carries_the_paths_the_task_owns(self):
        """Finding 56: a task name is not something a session can act on."""
        self.spawn()
        self.assertIn("Owns: `src/a/`", (self.tree / ss.PROMPT_FILE).read_text())

    def test_a_task_with_no_ownership_row_writes_nothing_and_starts_nothing(self):
        (self.root / "daily" / "2026-09-18-tracker.md").write_text(
            TRACKER.replace("| Security audit | `src/a/` |", ""))
        out, calls = self.spawn()
        self.assertEqual(out.returncode, 1)
        self.assertIn("File ownership", out.stderr)
        self.assertFalse((self.tree / ss.PROMPT_FILE).exists())
        self.assertEqual(calls, [])

    def test_the_started_line_names_the_file_it_wrote(self):
        out, _ = self.spawn()
        self.assertIn(ss.PROMPT_FILE, out.stdout)


class LaunchEnvironmentTest(unittest.TestCase):
    """A spawned session is a new session, not a child of the coordinator's.

    The coordinator is itself a Claude Code session, so its environment carries the markers that say
    which session this is, which socket it speaks on, and that it is somebody's child. Inheriting
    those gave a spawned session the coordinator's messaging credentials, bound it to the
    coordinator's project rather than its own worktree, and turned its transcript off — one cause,
    three symptoms, found by watching a real spawn.
    """

    def test_no_claude_variable_reaches_the_new_session(self):
        parent = dict(os.environ, CLAUDE_CODE_CHILD_SESSION="1", CLAUDE_CODE_SESSION_ID="abc",
                      CLAUDE_CODE_MESSAGING_SOCKET="/tmp/cc-socks/1.sock",
                      CLAUDE_CODE_MESSAGING_TOKEN="secret", CLAUDECODE="1", CLAUDE_PID="99")
        env = ss.launch_env(parent)
        leaked = [k for k in env if k.upper().startswith("CLAUDE")]
        self.assertEqual(leaked, [], f"the new session would speak as the coordinator: {leaked}")

    def test_the_messaging_token_is_never_passed_on(self):
        env = ss.launch_env(dict(os.environ, CLAUDE_CODE_MESSAGING_TOKEN="secret"))
        self.assertNotIn("secret", "".join(env.values()))

    def test_what_a_terminal_actually_needs_survives(self):
        parent = dict(os.environ, PATH="/usr/bin:/bin", HOME="/Users/robin", TERM="xterm-256color")
        env = ss.launch_env(parent)
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["HOME"], "/Users/robin")
        self.assertEqual(env["TERM"], "xterm-256color")

    def test_the_launcher_override_does_not_ride_along(self):
        """It is read here; a session that inherited it could relaunch itself."""
        env = ss.launch_env({ss.ENV: '["ghostty"]', "PATH": "/usr/bin"})
        self.assertNotIn(ss.ENV, env)

    def test_the_new_session_keeps_its_own_transcript(self):
        """Dropping the child marker is the fix; the task audit covers a branch, not the reasoning."""
        env = ss.launch_env(dict(os.environ, CLAUDE_CODE_CHILD_SESSION="1"))
        self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", env)

    def test_herdr_variables_follow_the_existing_launch_env_whitelist(self):
        # #471: the socket has no access control; even an unknown HERDR_* name must be scrubbed.
        parent = {"PATH": "/usr/bin:/bin", "HOME": "/tmp/worker", "HERDR_SOCKET_PATH": "/tmp/parent.sock",
                  "HERDR_BIN_PATH": "/tmp/herdr", "HERDR_PANE_ID": "parent-pane", "HERDR_ENV": "parent",
                  "HERDR_WORKSPACE_ID": "parent-workspace", "HERDR_TAB_ID": "parent-tab",
                  "HERDR_FUTURE_CAPABILITY": "parent-only"}
        expected = {key: value for key, value in parent.items() if key in ss.KEEP}
        self.assertEqual(ss.launch_env(parent), expected)

    def test_a_herdr_variable_explicitly_approved_by_keep_still_survives(self):
        parent = {"PATH": "/usr/bin", "HERDR_SOCKET_PATH": "/tmp/approved.sock", "HERDR_PANE_ID": "parent-pane"}
        with unittest.mock.patch.object(ss, "KEEP", (*ss.KEEP, "HERDR_SOCKET_PATH")):
            self.assertEqual(ss.launch_env(parent), {"PATH": "/usr/bin", "HERDR_SOCKET_PATH": "/tmp/approved.sock"})


if __name__ == "__main__":
    unittest.main()


class BootstrapNamesItsPathsTest(unittest.TestCase):
    """Zach, 23:33-23:34: the bootstrap said "in this directory" and Ghostty does not reliably give
    the tab the directory it was asked for, so "this directory" was sometimes not the worktree and
    the session could not find its own assignment.

    Two fixes, because one of them is the cause and the other is the belt. `env -C` pins the working
    directory in the command itself rather than trusting a surface configuration field — shell-free,
    and it exits 125 loudly on a bad path instead of starting a session somewhere arbitrary. And the
    bootstrap names both paths absolutely, so a session that lands in the wrong place can still read
    its assignment and knows where it was supposed to be.
    """

    def argv(self, cwd="/tmp/wt"):
        return ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd=cwd, title="wt-docs")

    def test_the_bootstrap_names_the_dispatch_file_absolutely(self):
        self.assertIn(f"/tmp/wt/{ss.PROMPT_FILE}", self.argv()[-1])

    def test_the_bootstrap_names_the_working_directory_absolutely(self):
        self.assertIn("/tmp/wt", self.argv()[-1])

    def test_it_no_longer_says_in_this_directory(self):
        """The phrase that made the assignment unfindable whenever the cwd was not the worktree."""
        self.assertNotIn("in this directory", self.argv()[-1])

    def test_a_relative_cwd_is_resolved_before_it_reaches_the_session(self):
        """A session cannot resolve a relative path against a directory it may not be standing in."""
        token = self.argv(cwd=".")[-1]
        self.assertNotIn(" ./", token)
        self.assertIn(str(Path(".").resolve()), token)

    def test_the_bootstrap_carries_no_shell_metacharacter(self):
        """It is a launcher token like any other and the guard runs on the template."""
        self.assertIsNone(ss.METACHARACTERS.search(ss.BOOTSTRAP))

    def test_the_ghostty_command_pins_the_directory_with_env(self):
        s = ss.ghostty_script(cwd="/tmp/wt", agent_type="implementer", claude=Path("/opt/homebrew/bin/claude"))
        self.assertRegex(s, r"/usr/bin/env -C /tmp/wt (?:'PATH=[^']*'|PATH=\S*) .*session_exec.py exec-login -- /opt/homebrew/bin/claude")

    def test_the_ghostty_bootstrap_names_the_paths_too(self):
        s = ss.ghostty_script(cwd="/tmp/wt", agent_type="implementer", claude=Path("/opt/homebrew/bin/claude"))
        self.assertIn(shlex.quote(f"/tmp/wt/{ss.PROMPT_FILE}"), s.replace("\\", ""))


class SessionNameTest(unittest.TestCase):
    """Zach, 2026-09-22 16:18: "the NUMERIC NAMES I ASSIGN ARE THE ONLY NAMES THEY ARE ALLOWED TO KEEP".

    The tab title was all `--title` ever set. `claude` itself got no name, so the harness named the
    session after its directory and then retitled it from its first task: impl07 was listed as
    `lab-write-patient-check-merge` by its second turn. A name given at launch registers as the
    user's, and the harness never retitles one of those.
    """

    setUp = DispatchFileTest.setUp
    spawn = DispatchFileTest.spawn

    def test_the_launch_names_the_session(self):
        argv = ss.argv(ss.DEFAULT_LAUNCHER, agent_type="implementer", cwd="/tmp/wt", title="impl07")
        self.assertEqual(argv[argv.index("--name") + 1], "impl07")

    def test_the_ghostty_command_names_the_session(self):
        s = ss.ghostty_script(cwd="/tmp/wt", agent_type="implementer", claude=Path("/opt/homebrew/bin/claude"),
                              title="impl07")
        self.assertIn(" --name impl07 ", s)

    def test_no_name_drops_the_flag_and_its_value_together(self):
        """Removed as a pair, never substituted empty: `--name ''` would be a session named nothing."""
        s = ss.ghostty_script(cwd="/tmp/wt", agent_type="implementer", claude=Path("/opt/homebrew/bin/claude"))
        self.assertNotIn("--name", s)

    def test_name_is_the_flag_and_title_still_means_it(self):
        out, calls = self.spawn(extra=["--name", "impl07"])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(calls[0]["argv"][1], "impl07")

    def test_the_assignment_carries_the_name_the_launch_gave(self):
        self.spawn(extra=["--name", "impl07"])
        self.assertIn("You are `impl07`.", (self.tree / ss.PROMPT_FILE).read_text())

    def test_a_name_that_is_not_a_session_name_writes_nothing_and_starts_nothing(self):
        for bad in ("impl 07", "-x", "a;b", "impl07 [abc123]", "$(whoami)"):
            with self.subTest(bad=bad):
                out, calls = self.spawn(extra=[f"--name={bad}"])
                self.assertEqual(out.returncode, 1, bad)
                self.assertIn("refused", out.stderr)
                self.assertFalse((self.tree / ss.PROMPT_FILE).exists())
                self.assertEqual(calls, [])


def run_main(*extra):
    """A dry-run launch against a throwaway workspace; the process result."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "CLAUDE.md").write_text(CLAUDE)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
        return subprocess.run(
            [sys.executable, str(Path(ss.__file__)), "--cwd", "/tmp", "--name", "implementer-GFlash38H-01",
             "--root", str(root), "--date", "2026-09-18", "--task", "Security audit", "--dry-run", *extra],
            capture_output=True, text=True, timeout=30)


class HelpTest(unittest.TestCase):
    def test_help_lists_each_allowed_effort(self):
        """#61 second review: `--help` names every value `--effort` accepts (`ss.EFFORTS`, its non-empty ones)."""
        out = subprocess.run([sys.executable, str(Path(ss.__file__)), "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0)
        text = " ".join(out.stdout.split())
        start = text.rindex("--effort EFFORT")  # the option's own entry, after the usage line
        block = text[start:text.index("--dry-run", start)]
        for effort in filter(None, ss.EFFORTS):
            self.assertRegex(block, rf"\b{effort}\b", effort)


class TaskFlagTest(unittest.TestCase):
    """#33, "everything, code included": the flag is `--task`, and `--lane` is gone."""

    def test_lane_is_refused(self):
        out = run_main("--lane", "Security audit")
        self.assertEqual(out.returncode, 2)
        self.assertIn("--lane", out.stderr)


class AgyTest(unittest.TestCase):
    """Zach, 2026-09-23 18:42: "within the next hour I need agy instances going"; registration and reporting
    through the engine store when IPC is unavailable. agy has no `--agent` and no `--name`; `--mode plan` is its plan-first gate."""

    AGY = Path("/opt/homebrew/bin/agy")

    def script(self, **kw):
        return ss.ghostty_script(cwd="/tmp/wt", agent_type="implementer", claude=self.AGY, title="implementer-GFlash38H-01",
                                 runtime="agy", model="gemini-3.8-flash-high", workspace="/tmp/ws", **kw)

    def test_the_tab_runs_agy_in_plan_mode_on_the_named_model(self):
        self.assertRegex(self.script(), r"session_exec.py --registry .* --runtime agy .* -- /opt/homebrew/bin/agy "
                                        r"--model gemini-3.8-flash-high --mode plan -i ")

    def test_it_carries_no_claude_flags(self):
        s = self.script()
        for flag in ("--agent", "--permission-mode"):
            self.assertNotIn(flag, s)
        self.assertIn("--name implementer-GFlash38H-01", s, "the wrapper registers the assigned name")

    def test_the_bootstrap_names_the_dispatch_and_the_workspace_rules(self):
        s = self.script().replace("\\", "")
        self.assertIn(f"/tmp/wt/{ss.PROMPT_FILE}", s)
        self.assertIn("/tmp/ws/CLAUDE.md", s)

    def test_the_tab_is_still_titled_with_the_name(self):
        self.assertIn("set_tab_title:implementer-GFlash38H-01", self.script())


    def test_agy_without_a_model_is_refused(self):
        out = run_main("--runtime", "agy")
        self.assertEqual(out.returncode, 1)
        self.assertIn("--model", out.stderr)

    def test_a_hostile_model_is_refused(self):
        out = run_main("--runtime", "agy", "--model", "x;rm -rf ~")
        self.assertEqual(out.returncode, 1)

    def test_effort_reaches_the_agy_tab(self):
        out = run_main("--runtime", "agy", "--model", "gemini-3.8-flash-high", "--effort", "high")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--effort high", out.stdout)

    def test_the_effort_comes_before_the_bootstrap_prompt(self):
        # `-i` takes the prompt as its value: the effort pair must not sit between them.
        tokens = ss.runtime_tokens(cwd="/tmp/wt", agent_type=None, binary=self.AGY, title="agy-01", runtime="agy",
                                   model="gemini-3.8-flash-high", effort="high", workspace="/tmp/ws")
        agy = tokens[tokens.index(str(self.AGY)):]
        self.assertTrue(agy[agy.index("-i") + 1].startswith("Read the file"), agy)
        effort = agy.index("--effort")
        self.assertEqual(agy[effort + 1], "high")
        self.assertLess(effort + 1, agy.index("-i"))

    def test_the_dry_run_shows_the_agy_tab(self):
        out = run_main("--runtime", "agy", "--model", "gemini-3.8-flash-high")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--mode plan", out.stdout)


class ClaudeModelTest(unittest.TestCase):
    """The class in a name (`COpusM`) is the model and effort a Claude session launches on."""

    CLAUDE_BIN = Path("/opt/homebrew/bin/claude")

    def script(self, **kw):
        return ss.ghostty_script(cwd="/tmp/wt", agent_type="implementer", claude=self.CLAUDE_BIN,
                                 title="implementer-COpusM-01", **kw)

    def test_model_and_effort_reach_the_claude_command(self):
        self.assertRegex(self.script(model="opus", effort="medium"),
                         r"/opt/homebrew/bin/claude --plugin-dir \S+ --agent implementer --name implementer-COpusM-01 "
                         r"--model opus --effort medium --permission-mode plan ")

    def test_without_them_the_command_is_unchanged(self):
        s = self.script()
        self.assertNotIn("--model", s)
        self.assertNotIn("--effort", s)

    def test_the_override_argv_carries_them_too(self):
        out = ss.argv(ss.CLAUDE_ARGV, agent_type="implementer", cwd="/tmp/wt", title="t", model="sonnet", effort="high")
        self.assertEqual(out[out.index("--model"):out.index("--model") + 4], ["--model", "sonnet", "--effort", "high"])

    def test_a_bad_effort_or_model_is_refused(self):
        for extra in (["--effort", "extreme"], ["--model", "x;rm -rf ~"]):
            with self.subTest(extra=extra):
                self.assertEqual(run_main(*extra).returncode, 1 if "--model" in extra else 2)


class EffortPerRuntimeTest(unittest.TestCase):
    """#52: each runtime says how it takes an effort, or why it cannot; none silently inherits the user's config."""

    def codex_tokens(self, **kw):
        tokens = ss.runtime_tokens(cwd="/tmp/wt", agent_type=None, binary=Path("/bin/codex"), title="codex-01",
                                   runtime="codex", workspace="/tmp/ws", **kw)
        return tokens[tokens.index("/bin/codex"):]  # codex's own argv, past the env and wrapper prefix

    def test_an_interactive_codex_launch_carries_its_effort(self):
        tokens = self.codex_tokens(effort="low")
        self.assertIn(("-c", "model_reasoning_effort=low"), list(zip(tokens, tokens[1:])))
        self.assertTrue(tokens[-1].startswith("Read the file"), tokens[-1])  # the bootstrap prompt stays last

    def test_without_an_effort_a_codex_launch_leaves_the_users_config_alone(self):
        tokens = self.codex_tokens()
        self.assertNotIn("-c", tokens)
        self.assertFalse([t for t in tokens if "model_reasoning_effort" in t])

    def test_a_runtime_that_cannot_express_effort_is_refused_by_name(self):
        out = run_main("--runtime", "cursor", "--effort", "medium")
        self.assertEqual(out.returncode, 1)
        self.assertTrue(out.stderr.startswith("refused:"), out.stderr)
        self.assertIn("--effort", out.stderr)
        self.assertIn("cursor", out.stderr)


class TmuxLauncherTest(unittest.TestCase):
    def test_tmux_executes_worker_argv_without_a_shell(self):
        command = ss.tmux_command(tmux=Path("/bin/tmux"), binary=Path("/bin/codex"),
                                  cwd="/tmp/a b", agent_type=None, title="codex-01",
                                  runtime="codex", workspace="/tmp")
        self.assertEqual(command[:7], ["/bin/tmux", "new-window", "-d", "-c", "/tmp/a b", "-n", "codex-01"])
        self.assertEqual(command[7:10], [ss.ENV_BIN, "-C", "/tmp/a b"])
        self.assertIn("/bin/codex", command)
        self.assertFalse({"sh", "bash", "zsh"} & set(command[7:]))

    def test_workspace_launcher_can_be_overridden_per_invocation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE + "- Settings: `chief-of-stuff.toml`\n")
            (root / "chief-of-stuff.toml").write_text('[workers]\nlauncher = "tmux"\n')
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            tree = root / "trees" / "wt-x"
            tree.mkdir(parents=True)
            args = ["--cwd", str(tree), "--name", "codex-01", "--task", "Security audit",
                    "--root", str(root), "--date", "2026-09-18", "--dry-run"]
            with unittest.mock.patch.object(ss, "resolve", side_effect=lambda name: "/bin/" + name):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(ss.main(args), 0)
                self.assertIn("tmux new-window", output.getvalue())
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(ss.main(args + ["--launcher", "ghostty"]), 0)
                self.assertIn("would ask Ghostty for a tab", output.getvalue())

    def test_missing_tmux_refuses_before_writing_assignment(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE)
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            tree = root / "trees" / "wt-x"
            tree.mkdir(parents=True)
            with unittest.mock.patch.object(ss, "resolve", return_value=None), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(ss.main(["--cwd", str(tree), "--name", "codex-01", "--task", "Security audit",
                                          "--root", str(root), "--date", "2026-09-18", "--launcher", "tmux"]), 1)
            self.assertFalse((tree / ss.PROMPT_FILE).exists())

    def test_a_failed_launch_takes_its_dispatch_back_so_a_retry_can_use_the_tree(self):
        # A Ghostty tab that never opened left its dispatch behind, and the retry was refused the tree.
        failures = {"exit 1": subprocess.CompletedProcess([], 1, "", "no tab"),
                    "no binary": FileNotFoundError("osascript")}
        for launcher in ("ghostty", "tmux"):
            for why, failure in failures.items():
                with self.subTest(launcher=launcher, why=why), tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    (root / "CLAUDE.md").write_text(CLAUDE)
                    (root / "daily").mkdir()
                    (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
                    tree = root / "trees" / "wt-x"
                    tree.mkdir(parents=True)
                    args = ["--cwd", str(tree), "--name", "codex-01", "--task", "Security audit",
                            "--root", str(root), "--date", "2026-09-18", "--launcher", launcher]
                    with unittest.mock.patch.object(ss, "resolve", return_value="/bin/fake"), \
                            contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                        with unittest.mock.patch.object(ss.subprocess, "run", side_effect=[failure]):
                            self.assertEqual(ss.main(args), 1)
                        self.assertFalse((tree / ss.PROMPT_FILE).exists())
                        with unittest.mock.patch.object(ss.subprocess, "run",
                                                        return_value=subprocess.CompletedProcess([], 0, "", "")):
                            self.assertEqual(ss.main(args), 0)
                    self.assertIn("Task: Security audit", (tree / ss.PROMPT_FILE).read_text())


class HerdrLauncherTest(unittest.TestCase):
    """#471: exact argv, JSON pane handoff, and refusal cleanup; no real herdr or agent runs.

    Every CLI call selects a dedicated named session before its subcommand. Dry runs use
    <root-pane>, since tab creation has not happened. The pane shell receives the tmux wrapper with
    each word shell-quoted, minus the `env -C <root> PATH=... CHIEF_OF_STUFF_WORKSPACE=...` prefix: the tab is
    created in the worktree (`--cwd`), the pane shell already has the user's PATH, and the workspace travels as
    `tab create --env CHIEF_OF_STUFF_WORKSPACE=<root>`. A typed line past ~1024 bytes is cut by the tty, so a
    line over HERDR_LINE_LIMIT (900) bytes is refused before any herdr call. Assignment, registry and label
    keep one name.
    The fake tab-created response follows the installed herdr v0.9.3 API schema.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.day = date.today().isoformat()
        (self.root / "CLAUDE.md").write_text(CLAUDE + '- Settings: `chief-of-stuff.toml`\n')
        (self.root / "daily").mkdir()
        (self.root / "daily" / f"{self.day}-tracker.md").write_text(re.sub(r"\d{4}-\d{2}-\d{2}", self.day, TRACKER))
        self.tree = self.root / "trees" / "audit tree"
        self.tree.mkdir(parents=True)
        self.dispatch = self.tree / ss.PROMPT_FILE
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.jsonl"
        self.runtime_log = self.root / "agent-was-executed"
        self.response = self.root / "herdr-response.json"
        self.workspace_id, self.tab_id, self.pane_id = "w53", "w53:t29", "w53:p97"
        self.configure_fake()
        # Embedded paths survive launch_env's whitelist; no special fake-only environment is needed.
        self.herdr = self.executable("herdr", f"""import json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path
args = sys.argv[1:]
with open({str(self.log)!r}, 'a') as log:
    log.write(json.dumps({{'argv': args, 'env': dict(os.environ),
                          'dispatch_exists': Path({str(self.dispatch)!r}).is_file()}}) + '\\n')
if len(args) < 4 or args[0] != '--session' or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{{0,31}}', args[1]):
    print('missing or invalid dedicated herdr session', file=sys.stderr)
    sys.exit(92)
session, args = args[1], args[2:]
config = json.loads(Path({str(self.response)!r}).read_text())
if args[:2] == ['session', 'list']:
    if config['failure'] == 'session list':
        print('no running herdr server', file=sys.stderr)
        sys.exit(7)
    # None: any requested session is running. A list: the whole roster (v0.9.3 reply shape).
    roster = config['sessions'] if config['sessions'] is not None else [session]
    print(json.dumps({{'id': 'eval-session-list', 'result': {{'type': 'session_list',
                       'sessions': [{{'name': name}} for name in roster]}}}}))
elif args[:2] == ['tab', 'create']:
    if config['failure'] == 'tab':
        print('no running herdr server', file=sys.stderr)
        sys.exit(7)
    print(json.dumps(config['reply']))
elif args[:2] == ['pane', 'run']:
    if config['failure'] == 'pane':
        print('pane command was rejected', file=sys.stderr)
        sys.exit(8)
    # The real wrapper registers before exec. failure 'register' is #551's cut-off line: the pane
    # took the command and nothing ran.
    if config['failure'] != 'register':
        words = args[3:]
        tick, dquote = chr(39), chr(34)
        escaped = tick + dquote + tick + dquote + tick
        for at, word in enumerate(words[:-1]):
            if word == '--registry':
                raw = words[at + 1]
                if len(raw) >= 2 and raw[0] == tick and raw[-1] == tick:
                    raw = raw[1:-1].replace(escaped, tick)
                path = Path(raw)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({{'runtime': 'claude', 'name': path.stem, 'worktree': 'eval',
                                             'pid': os.getpid(),
                                             'started': datetime.now(timezone.utc).isoformat()}}) + '\\n')
                break
    print(json.dumps({{'result': {{'started': True}}}}))
elif args[:2] == ['tab', 'close']:
    tab = config['reply'].get('result', {{}}).get('tab', {{}})
    if not isinstance(tab, dict) or args != ['tab', 'close', tab.get('tab_id')]:
        print('cleanup did not use the returned tab id', file=sys.stderr)
        sys.exit(91)
    if config['cleanup_failure']:
        print('tab is still open: cleanup denied', file=sys.stderr)
        sys.exit(9)
    print(json.dumps({{'result': {{'closed': True}}}}))
else:
    print('unexpected herdr argv', file=sys.stderr)
    sys.exit(91)
""")
        for name in ("claude", "codex", "agent", "agy"):
            self.executable(name, f"from pathlib import Path\nPath({str(self.runtime_log)!r}).touch()\n")
        # As in test_shell_setup, only lookups use this Python stand-in. Its PATH has only fakes.
        lookup = self.executable("lookup", """import shutil, sys
args = sys.argv[1:]
if len(args) != 2 or args[0] != '-lic' or not args[1].startswith('command -v '):
    sys.exit(90)
found = shutil.which(args[1].removeprefix('command -v '))
if not found:
    sys.exit(1)
print(found)
""")
        self.env = {"PATH": str(self.bin), "HOME": str(self.root), "SHELL": "/bin/zsh",
                    "CHIEF_OF_STUFF_SHELL": str(lookup)}

    def executable(self, name, source):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o755)
        return path

    def tab_reply(self):
        # Required fields of SuccessResponse / tab_created / TabInfo / PaneInfo.
        return {"id": "eval-tab-create", "result": {
            "type": "tab_created",
            "tab": {"tab_id": self.tab_id, "workspace_id": self.workspace_id, "number": 29,
                    "label": "audit-01", "focused": False, "pane_count": 1, "agent_status": "idle"},
            "root_pane": {"pane_id": self.pane_id, "terminal_id": "eval-terminal-97",
                          "workspace_id": self.workspace_id, "tab_id": self.tab_id,
                          "focused": False, "agent_status": "idle", "revision": 0}}}

    def configure_fake(self, *, failure="", reply=None, cleanup_failure=False, sessions=None):
        if reply is None:
            reply = self.tab_reply()
        self.response.write_text(json.dumps({"failure": failure, "reply": reply,
                                             "cleanup_failure": cleanup_failure, "sessions": sessions}))

    def args(self, *, title="audit-01", runtime="claude", launcher="herdr", extra=()):
        # Relative input must become absolute in both the tab argv and runtime_tokens.
        args = ["--cwd", os.path.relpath(self.tree), f"--title={title}", "--root", str(self.root),
                "--date", self.day, "--task", "Security audit", "--runtime", runtime]
        if launcher is not None:
            args += ["--launcher", launcher]
        return args + list(extra)

    def invoke(self, limit=None, *, wait=None, **kw):
        """A subprocess run. `limit` or `wait` instead runs main in-process with HERDR_LINE_LIMIT raised
        to it (the non-claude runtimes' lines are over the real limit, and their exact shape still needs
        pinning) or with the registration window shortened to `wait` seconds: the real window is 30 s."""
        if limit is None and wait is None:
            return subprocess.run([sys.executable, str(Path(ss.__file__)), *self.args(**kw)],
                                  capture_output=True, text=True, timeout=30, env=self.env)
        with unittest.mock.patch.dict(os.environ, self.env, clear=True), \
                contextlib.ExitStack() as stack:
            if limit is not None:
                stack.enter_context(unittest.mock.patch.object(ss, "HERDR_LINE_LIMIT", limit))
            if wait is not None:
                stack.enter_context(unittest.mock.patch.object(ss, "HERDR_REGISTER_WAIT", wait))
            output, errors = io.StringIO(), io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            code = ss.main(self.args(**kw))
        return subprocess.CompletedProcess([], code, output.getvalue(), errors.getvalue())

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def worker_tokens(self, *, title="audit-01", runtime="claude", model="", effort="", herdr=False):
        """The shared wrapper (tmux, Ghostty). herdr=True is the typed-line form: the leading
        `env -C <root> PATH=... CHIEF_OF_STUFF_WORKSPACE=...` is dropped and the rest is unchanged."""
        binary = self.bin / {"claude": "claude", "codex": "codex", "cursor": "agent", "agy": "agy"}[runtime]
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            tokens = ss.runtime_tokens(cwd=str(self.tree), agent_type=None, binary=binary, title=title,
                                       runtime=runtime, model=model, effort=effort, workspace=str(self.root))
        if not herdr:
            return tokens
        self.assertEqual(tokens[:2], [ss.ENV_BIN, "-C"])
        return tokens[5:]  # env, -C, <root>, PATH=..., CHIEF_OF_STUFF_WORKSPACE=...

    def tab_argv(self, title="audit-01", *, session="chief-of-stuff"):
        return ["--session", session, "tab", "create", "--cwd", str(self.tree), "--label", title, "--no-focus",
                "--env", f"CHIEF_OF_STUFF_WORKSPACE={self.root}"]

    def pane_tokens(self, *, title="audit-01", runtime="claude", model="", effort=""):
        """The typed-line tokens herdr_commands builds: the wrapper's herdr form, with the claude
        registration splice (the launcher verifies the start through the same wrapper, #551)."""
        tokens = self.worker_tokens(title=title, runtime=runtime, model=model, effort=effort, herdr=True)
        if runtime == "claude":
            at = tokens.index("exec-login") + 1
            tokens = tokens[:at] + ["--registry", str(self.root / ss.PROMPT_DIR / "sessions" / f"{title}.json"),
                                    "--name", title] + tokens[at:]
        return tokens

    def pane_argv(self, *, title="audit-01", runtime="claude", pane=None, model="", effort="",
                  session="chief-of-stuff"):
        # herdr joins these words for the pane's shell; quote every wrapper token independently.
        return ["--session", session, "pane", "run", pane or self.pane_id,
                *[shlex.quote(token) for token in self.pane_tokens(title=title, runtime=runtime,
                                                                  model=model, effort=effort)]]

    def commands(self, *, title="audit-01", runtime="claude", session="chief-of-stuff", model="", effort="",
                 limit=None):
        binary = self.bin / {"claude": "claude", "codex": "codex", "cursor": "agent", "agy": "agy"}[runtime]
        with unittest.mock.patch.dict(os.environ, self.env, clear=True), contextlib.ExitStack() as stack:
            if limit is not None:
                stack.enter_context(unittest.mock.patch.object(ss, "HERDR_LINE_LIMIT", limit))
            return ss.herdr_commands(herdr=self.herdr, session=session, cwd=str(self.tree),
                                     agent_type=None, binary=binary, title=title, runtime=runtime,
                                     model=model, effort=effort, workspace=str(self.root))

    def close_argv(self, *, session="chief-of-stuff"):
        return ["--session", session, "tab", "close", self.tab_id]

    def session_list_argv(self, *, session="chief-of-stuff"):
        return ["--session", session, "session", "list", "--json"]

    def assert_refused(self, out, *details):
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertRegex(out.stderr, r"(?m)^refused: .+")
        self.assertIn("herdr", out.stderr.lower())
        for detail in details:
            self.assertIn(detail, out.stderr.lower())
        self.assertFalse(self.dispatch.exists(), "a refused launch must leave the tree retryable")
        self.assertFalse(self.runtime_log.exists(), "a fake must never execute the interactive runtime")

    def test_dry_run_prints_exact_two_herdr_argv_and_runs_nothing_for_every_runtime(self):
        for runtime, model, effort in (("claude", "sonnet", "high"), ("codex", "gpt-test", "low"),
                                       ("cursor", "cursor-test", ""), ("agy", "agy-test", "medium")):
            with self.subTest(runtime=runtime):
                extra = ["--dry-run", "--model", model] + (["--effort", effort] if effort else [])
                out = self.invoke(limit=10_000, runtime=runtime, extra=extra)
                self.assertEqual(out.returncode, 0, out.stderr)
                lines = out.stdout.splitlines()
                self.assertEqual(len(lines), 3, out.stdout)
                create = [str(self.herdr), *self.tab_argv()]
                self.assertEqual(lines[0], "would run: " + shlex.join(create))
                expected = [str(self.herdr), *self.pane_argv(runtime=runtime,
                                                           pane="<root-pane>", model=model, effort=effort)]
                self.assertEqual(lines[1], "would run: " + shlex.join(expected))
                self.assertEqual(lines[2], f"would write: {self.dispatch}")
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.dispatch.exists())
                self.assertFalse(self.runtime_log.exists())

    def test_real_run_hands_json_root_pane_to_pane_run_and_keeps_dispatch(self):
        for pane in ("w53:p97", "w208:p413"):
            with self.subTest(pane=pane):
                self.pane_id = pane
                self.configure_fake()
                self.log.unlink(missing_ok=True)
                self.dispatch.unlink(missing_ok=True)
                out = self.invoke()
                self.assertEqual(out.returncode, 0, out.stderr)
                calls = self.calls()
                self.assertEqual([call["argv"] for call in calls],
                                 [self.session_list_argv(), self.tab_argv(), self.pane_argv()])
                self.assertTrue(all(call["dispatch_exists"] for call in calls))
                self.assertIn("Task: Security audit", self.dispatch.read_text())
                self.assertIn("started", out.stdout)
                self.assertIn("herdr", out.stdout.lower())
                self.assertFalse(self.runtime_log.exists())

    def test_workspace_herdr_setting_selects_the_launcher_without_a_flag(self):
        (self.root / "chief-of-stuff.toml").write_text('[workers]\nlauncher = "herdr"\n')
        out = self.invoke(launcher=None)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([call["argv"] for call in self.calls()],
                         [self.session_list_argv(), self.tab_argv(), self.pane_argv()])

    def test_configured_herdr_session_is_used_by_tab_create_and_pane_run(self):
        session = "review_workers-471"
        # The setting also applies when herdr is selected by --launcher rather than TOML.
        (self.root / "chief-of-stuff.toml").write_text(f'[workers]\nherdr_session = "{session}"\n')
        out = self.invoke()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([call["argv"] for call in self.calls()],
                         [self.session_list_argv(session=session), self.tab_argv(session=session),
                          self.pane_argv(session=session)])
        self.assertTrue(self.dispatch.is_file())

    def test_configured_herdr_session_is_printed_in_both_dry_run_argv(self):
        session = "review_workers-471"
        (self.root / "chief-of-stuff.toml").write_text(
            f'[workers]\nlauncher = "herdr"\nherdr_session = "{session}"\n')
        out = self.invoke(launcher=None, extra=["--dry-run"])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(),
                         ["would run: " + shlex.join([str(self.herdr), *self.tab_argv(session=session)]),
                          "would run: " + shlex.join([str(self.herdr), *self.pane_argv(
                              session=session, pane="<root-pane>")]),
                          f"would write: {self.dispatch}"])
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.dispatch.exists())

    def test_claude_profile_launches_claude_workers_through_claude_as(self):
        # Zach, 2026-10-08 20:50: Claude workers launch through `claude-as run <profile>`, not a bare `claude`.
        claude_as = self.executable("claude-as", "")
        (self.root / "chief-of-stuff.toml").write_text('[workers]\nlauncher = "herdr"\nclaude_profile = "secondary"\n')
        out = self.invoke(launcher=None, extra=["--dry-run"])
        self.assertEqual(out.returncode, 0, out.stderr)
        pane = shlex.split(out.stdout.splitlines()[1].removeprefix("would run: "))
        self.assertNotIn(str(self.bin / "claude"), pane, "a bare claude still launches")
        at = pane.index(str(claude_as))
        self.assertEqual(pane[at + 1:at + 3], ["run", "secondary"])
        self.assertIn("--plugin-dir", pane[at + 3:])
        # Other runtimes are not Claude Code and keep their own binary.
        out = self.invoke(limit=10_000, runtime="codex", launcher=None, extra=["--dry-run", "--model", "gpt-test"])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn(str(claude_as), out.stdout)
        self.assertIn(str(self.bin / "codex"), out.stdout)

    def test_failed_pane_run_closes_tab_in_configured_herdr_session(self):
        session = "review_workers-471"
        (self.root / "chief-of-stuff.toml").write_text(
            f'[workers]\nlauncher = "herdr"\nherdr_session = "{session}"\n')
        self.configure_fake(failure="pane")
        out = self.invoke(launcher=None)
        self.assert_refused(out, "pane run", "pane command was rejected")
        self.assertEqual([call["argv"] for call in self.calls()],
                         [self.session_list_argv(session=session), self.tab_argv(session=session),
                          self.pane_argv(session=session), self.close_argv(session=session)])

    def test_invalid_herdr_session_refuses_before_dispatch_or_cli_calls(self):
        for value in ('""', '"Chief-of-stuff"', '"two words"', '"-workers"', '"_workers"',
                      '"workers.main"', '"../workers"', '"' + 'a' * 33 + '"',
                      '"workers;touch marker"', '"workers\\n"', "17", "true", '["workers"]'):
            with self.subTest(value=value):
                (self.root / "chief-of-stuff.toml").write_text(f'[workers]\nherdr_session = {value}\n')
                out = self.invoke()
                self.assert_refused(out, "[workers] herdr_session")
                self.assertEqual(self.calls(), [])
                self.assertNotIn("would run:", out.stdout)

    def add_herdr_environment_bait(self):
        self.env.update({"HERDR_SOCKET_PATH": str(self.root / "parent.sock"),
                         "HERDR_BIN_PATH": str(self.herdr), "HERDR_PANE_ID": "parent-pane",
                         "HERDR_ENV": "parent", "HERDR_WORKSPACE_ID": "parent-workspace",
                         "HERDR_TAB_ID": "parent-tab", "HERDR_FUTURE_CAPABILITY": "parent-only"})

    def assert_cli_environment_matches_launch_env(self, calls):
        # Workers run in herdr's own environment. Observe the CLI process the launcher actually
        # creates, just as for tmux; executing a worker from the fake would test the wrong boundary.
        self.assertTrue(calls, "the fake CLI must run to prove its environment was scrubbed")
        for call in calls:
            with self.subTest(argv=call["argv"]):
                inherited = {key: value for key, value in call["env"].items() if key.startswith("HERDR_")}
                self.assertEqual(inherited, {})
                for key in ("PATH", "HOME", "SHELL"):
                    self.assertEqual(call["env"][key], self.env[key])

    def test_herdr_tab_create_and_pane_run_scrub_parent_herdr_environment(self):
        self.add_herdr_environment_bait()
        out = self.invoke()
        self.assertEqual(out.returncode, 0, out.stderr)
        calls = self.calls()
        self.assertEqual([call["argv"] for call in calls],
                         [self.session_list_argv(), self.tab_argv(), self.pane_argv()])
        self.assert_cli_environment_matches_launch_env(calls)
        self.assertFalse(self.runtime_log.exists())

    def test_herdr_failure_cleanup_also_scrubs_parent_herdr_environment(self):
        self.add_herdr_environment_bait()
        self.configure_fake(failure="pane")
        out = self.invoke()
        self.assert_refused(out, "pane run", "pane command was rejected")
        calls = self.calls()
        self.assertEqual([call["argv"] for call in calls],
                         [self.session_list_argv(), self.tab_argv(), self.pane_argv(), self.close_argv()])
        self.assert_cli_environment_matches_launch_env(calls)

    def test_control_tmux_scrubs_parent_herdr_environment_using_launch_env(self):
        self.add_herdr_environment_bait()
        self.executable("tmux", f"""import json, os, sys
with open({str(self.log)!r}, 'a') as log:
    log.write(json.dumps({{'argv': sys.argv[1:], 'env': dict(os.environ)}}) + '\\n')
""")
        out = self.invoke(launcher="tmux")
        self.assertEqual(out.returncode, 0, out.stderr)
        calls = self.calls()
        self.assertEqual([call["argv"] for call in calls],
                         [["new-window", "-d", "-c", str(self.tree), "-n", "audit-01", *self.worker_tokens()]])
        self.assert_cli_environment_matches_launch_env(calls)
        self.assertFalse(self.runtime_log.exists())

    def test_missing_herdr_on_isolated_path_refuses_without_dispatch_or_calls(self):
        self.herdr.unlink()
        out = self.invoke()
        self.assert_refused(out, "path")
        self.assertEqual(self.calls(), [])

    def test_failed_tab_create_refuses_and_removes_dispatch(self):
        self.configure_fake(failure="tab")
        out = self.invoke()
        self.assert_refused(out, "tab", "no running herdr server")
        self.assertEqual([call["argv"] for call in self.calls()],
                         [self.session_list_argv(), self.tab_argv()])
        self.assertTrue(self.calls()[1]["dispatch_exists"])

    def test_json_without_root_pane_closes_known_tab_and_never_runs_a_pane(self):
        self.tab_id = "w53:t113"  # Cleanup must use the received id, not an assumed first tab.
        reply = self.tab_reply()
        del reply["result"]["root_pane"]
        self.configure_fake(reply=reply)
        out = self.invoke()
        self.assert_refused(out, "pane")
        self.assertEqual([call["argv"] for call in self.calls()],
                         [self.session_list_argv(), self.tab_argv(), self.close_argv()])

    def test_failed_pane_run_closes_created_tab_and_removes_dispatch(self):
        self.configure_fake(failure="pane")
        out = self.invoke()
        self.assert_refused(out, "pane run", "pane command was rejected")
        self.assertEqual([call["argv"] for call in self.calls()],
                         [self.session_list_argv(), self.tab_argv(), self.pane_argv(), self.close_argv()])

    def test_missing_or_malformed_pane_id_refuses_and_closes_the_created_tab(self):
        for pane in ({}, {"pane_id": None}, {"pane_id": ""}, {"pane_id": "--x"},
                     {"pane_id": "w1:p1 extra"}, {"pane_id": "w1:p1\n"},
                     {"pane_id": "p" * 65}, {"pane_id": 97}, {"pane_id": True},
                     {"pane_id": {"pane_id": self.pane_id}}, {"pane_id": [self.pane_id]},
                     None, [], self.pane_id):
            with self.subTest(root_pane=pane):
                self.log.unlink(missing_ok=True)
                reply = self.tab_reply()
                reply["result"]["root_pane"] = pane
                self.configure_fake(reply=reply)
                out = self.invoke()
                self.assert_refused(out, "pane")
                self.assertEqual([call["argv"] for call in self.calls()],
                                 [self.session_list_argv(), self.tab_argv(), self.close_argv()])

    def test_missing_or_malformed_tab_id_refuses_without_starting_or_closing(self):
        for tab in ({}, {"tab_id": None}, {"tab_id": ""}, {"tab_id": "--x"},
                    {"tab_id": "w1:t1 extra"}, {"tab_id": "w1:t1\n"},
                    {"tab_id": "t" * 65}, {"tab_id": 29}, {"tab_id": True},
                    {"tab_id": {"tab_id": self.tab_id}}, {"tab_id": [self.tab_id]},
                    None, [], self.tab_id):
            with self.subTest(tab=tab):
                self.log.unlink(missing_ok=True)
                reply = self.tab_reply()
                reply["result"]["tab"] = tab
                self.configure_fake(reply=reply)
                out = self.invoke()
                self.assert_refused(out, "tab id")
                self.assertEqual([call["argv"] for call in self.calls()],
                                 [self.session_list_argv(), self.tab_argv()])
        self.log.unlink(missing_ok=True)
        reply = self.tab_reply()
        del reply["result"]["tab"]
        self.configure_fake(reply=reply)
        self.assert_refused(self.invoke(), "tab id")
        self.assertEqual([call["argv"] for call in self.calls()],
                         [self.session_list_argv(), self.tab_argv()])

    def test_pane_failure_reports_cleanup_failure_without_hiding_original_error(self):
        self.configure_fake(failure="pane", cleanup_failure=True)
        out = self.invoke()
        self.assert_refused(out, "pane command was rejected")
        self.assertRegex(out.stderr, r"(?m)^cleanup: .*herdr tab close.*cleanup denied")
        self.assertEqual([call["argv"] for call in self.calls()],
                         [self.session_list_argv(), self.tab_argv(), self.pane_argv(), self.close_argv()])

    # #551: `pane run` confirms delivery, not execution. The start is real only when the worker's own
    # registration changes on disk; a line the pane's shell cut never registers, and a registration
    # left by an earlier worker does not vouch for this one.
    def test_the_registry_path_unquotes_exactly_the_module_quoting(self):
        # _registry_from reads the module's own shlex.quote output; it must invert it exactly.
        for value in ("two words", "single'quote", "$(touch marker)", "plain-path", ""):
            with self.subTest(value=value):
                self.assertEqual(ss._unquote(shlex.quote(value)), value)

    def test_a_herdr_start_that_never_registered_reports_failure_and_leaves_the_tree_reusable(self):
        self.configure_fake(failure="register")
        out = self.invoke(limit=10_000, wait=0.5)
        self.assert_refused(out, "registered")
        self.assertEqual([call["argv"] for call in self.calls()],
                         [self.session_list_argv(), self.tab_argv(), self.pane_argv(), self.close_argv()])
        self.assertNotIn("started", out.stdout)
        self.assertFalse((self.root / ss.PROMPT_DIR / "sessions" / "audit-01.json").exists())

    def test_a_stale_registration_from_an_earlier_worker_does_not_vouch_for_a_new_start(self):
        registry = self.root / ss.PROMPT_DIR / "sessions" / "audit-01.json"
        registry.parent.mkdir(parents=True)
        registry.write_text('{"runtime": "claude", "name": "audit-01", "worktree": "gone", "pid": 419,'
                            ' "started": "2026-10-01T09:00:00+00:00"}\n')
        self.configure_fake(failure="register")
        out = self.invoke(limit=10_000, wait=0.5)
        self.assert_refused(out, "registered")
        self.assertNotIn("started", out.stdout)

    # #552: herdr has no `session create` (v0.9.3 CLI reference); a session exists only once
    # `herdr --session <name>` launched it. The launcher checks the roster before tab create, so a
    # start against a session that was never begun names the setting and the way back.
    def test_a_missing_herdr_session_refuses_naming_the_setting_and_the_way_back(self):
        (self.root / "chief-of-stuff.toml").write_text('[workers]\nherdr_session = "away-team-7"\n')
        self.configure_fake(sessions=["chief-of-stuff"])
        out = self.invoke()
        self.assert_refused(out, "[workers] herdr_session", "away-team-7", "session create")
        self.assertEqual([call["argv"] for call in self.calls()],
                         [self.session_list_argv(session="away-team-7")])
        self.assertNotIn("started", out.stdout)

    def test_a_failed_session_list_probe_is_never_a_no(self):
        # A server that cannot answer the roster is tab create's to report: the launcher must not
        # refuse a start it could not rule out.
        self.configure_fake(failure="session list")
        out = self.invoke()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("started", out.stdout)
        self.assertEqual([call["argv"] for call in self.calls()],
                         [self.session_list_argv(), self.tab_argv(), self.pane_argv()])
        self.assertTrue(self.dispatch.is_file())

    def test_commands_refuse_invalid_dedicated_session_even_without_settings_loader(self):
        # Exercise the builder's own check; settings validation cannot kill its mutant.
        for session in ("", "Chief", "two words", "-workers", "_workers", "a" * 33,
                        "workers\n", "../workers", "workers;touch marker"):
            with self.subTest(session=session), self.assertRaisesRegex(ss.RefusedError, "herdr_session"):
                self.commands(session=session)
        self.assertEqual(self.calls(), [])

    def invoke_with_cli_probe(self, *, hang=None):
        """Intercept only our fake herdr; never wait 30 seconds or resolve a real server.

        A simulated hung subprocess raises immediately, but only after asserting the caller
        supplied a deadline. Removing timeout= must fail instead of making this eval hang.
        """
        real_run = subprocess.run
        cli_calls = []

        def run(command, **kw):
            if command[0] != str(self.herdr):
                return real_run(command, **kw)
            action = " ".join(command[3:5])
            cli_calls.append(command[1:])
            timeout = kw.get("timeout")
            self.assertIsInstance(timeout, (int, float), "every herdr CLI call needs a subprocess timeout")
            self.assertEqual(timeout, 30, "every CLI operation keeps the 30 s subprocess timeout")
            if action == hang:
                raise subprocess.TimeoutExpired(command, timeout)
            return real_run(command, **kw)

        with unittest.mock.patch.dict(os.environ, self.env, clear=True), \
                unittest.mock.patch.object(ss.subprocess, "run", side_effect=run), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = ss.main(self.args())
        return subprocess.CompletedProcess([], code, output.getvalue(), errors.getvalue()), cli_calls

    def test_hung_tab_create_is_refused_with_30_second_timeout_and_no_close(self):
        out, calls = self.invoke_with_cli_probe(hang="tab create")
        self.assert_refused(out, "tab create", "timed out")
        self.assertEqual(calls, [self.session_list_argv(), self.tab_argv()])

    def test_hung_pane_run_is_refused_and_closes_created_tab(self):
        out, calls = self.invoke_with_cli_probe(hang="pane run")
        self.assert_refused(out, "pane run", "timed out")
        self.assertEqual(calls, [self.session_list_argv(), self.tab_argv(), self.pane_argv(), self.close_argv()])

    def test_hung_cleanup_is_reported_and_original_pane_error_is_preserved(self):
        self.configure_fake(failure="pane")
        out, calls = self.invoke_with_cli_probe(hang="tab close")
        self.assert_refused(out, "pane command was rejected")
        self.assertRegex(out.stderr, r"(?m)^cleanup: .*herdr tab close.*timed out")
        self.assertEqual(calls, [self.session_list_argv(), self.tab_argv(), self.pane_argv(), self.close_argv()])

    def test_every_cli_call_filters_even_explicitly_kept_herdr_variables(self):
        self.add_herdr_environment_bait()
        self.env["herdr_future_token"] = "parent-only"
        # Ordinary whitelist tests cannot catch removal of the HERDR_* filter. An approved
        # variable must still be stripped from create, run and close (case-insensitively).
        approved = (*ss.KEEP, *[key for key in self.env if key.upper().startswith("HERDR_")])
        self.configure_fake(failure="pane")
        real_run = subprocess.run
        calls = []

        def run(command, **kw):
            if command[0] == str(self.herdr):
                calls.append(command[1:])
                inherited = {k: v for k, v in kw["env"].items() if k.upper().startswith("HERDR_")}
                self.assertEqual(inherited, {}, f"parent identity reached {command[3:5]}")
                self.assertEqual(kw["env"]["PATH"], self.env["PATH"])
            return real_run(command, **kw)

        with unittest.mock.patch.dict(os.environ, self.env, clear=True), \
                unittest.mock.patch.object(ss, "KEEP", approved), \
                unittest.mock.patch.object(ss.subprocess, "run", side_effect=run), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            # Green control: the injected whitelist really does keep the bait.
            self.assertEqual(ss.launch_env()["HERDR_SOCKET_PATH"], self.env["HERDR_SOCKET_PATH"])
            code = ss.main(self.args())
        out = subprocess.CompletedProcess([], code, output.getvalue(), errors.getvalue())
        self.assert_refused(out, "pane command was rejected")
        self.assertEqual(calls, [self.session_list_argv(), self.tab_argv(), self.pane_argv(), self.close_argv()])

    def test_pane_run_uses_quoted_tmux_wrapper_without_agent_start_options(self):
        create, run = self.commands(runtime="codex", model="gpt-test", effort="high", limit=10_000)
        self.assertEqual(create, [str(self.herdr), *self.tab_argv()])
        self.assertEqual(run, [str(self.herdr), *self.pane_argv(
            runtime="codex", pane="<root-pane>", model="gpt-test", effort="high")])
        self.assertEqual(run[3:6], ["pane", "run", "<root-pane>"])
        self.assertNotIn("--timeout", run)
        self.assertNotIn("--kind", run)
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            tmux = ss.tmux_command(tmux=self.bin / "tmux", binary=self.bin / "codex",
                                   cwd=str(self.tree), agent_type=None, title="audit-01",
                                   runtime="codex", model="gpt-test", effort="high", workspace=str(self.root))
        # The same runtime argv as tmux; only the env prefix is gone (the tab's --env carries the workspace).
        self.assertEqual(shlex.split(" ".join(run[6:])), tmux[7 + 5:])
        self.assertEqual(tmux[7:12][:2], [ss.ENV_BIN, "-C"])
        self.assertEqual(create[-2:], ["--env", f"CHIEF_OF_STUFF_WORKSPACE={self.root}"])

    def long_path(self):
        """About 20 KB of PATH, the launching bin dir first so the fakes still resolve."""
        value = f"{self.bin}:" + ":".join(f"/opt/seg{i:04d}/bin" for i in range(1300))
        self.assertGreater(len(value), 20_000)
        return value

    @staticmethod
    def typed(argv, *, built=False):
        """The line herdr types into the pane's shell: the words after the pane id, joined.
        A recorded call is [--session, s, pane, run, PANE, ...]; herdr_commands output starts with the binary."""
        return " ".join(argv[6 if built else 5:])

    def assert_short_line_without_env(self, line):
        self.assertNotIn("/opt/seg", line)
        self.assertNotIn("/usr/bin/env", line)
        words = shlex.split(line)
        self.assertEqual([w for w in words if w.startswith(("PATH=", "CHIEF_OF_STUFF_WORKSPACE="))], [])
        self.assertNotIn("-C", words)
        self.assertNotIn(self.env["PATH"], line)
        self.assertEqual(words[0], sys.executable)

    def test_the_typed_line_is_short_and_carries_no_env_or_path_under_a_20kb_path(self):
        self.env["PATH"] = self.long_path()
        _, run = self.commands()
        self.assert_short_line_without_env(self.typed(run, built=True))
        self.env["PATH"] = str(self.bin)

    def test_the_typed_line_does_not_grow_with_the_path(self):
        for runtime in ("claude", "codex", "cursor", "agy"):
            with self.subTest(runtime=runtime):
                model = "" if runtime == "claude" else "m"
                short = self.typed(self.commands(runtime=runtime, model=model, limit=10_000)[1], built=True)
                self.env["PATH"] = self.long_path()
                long = self.typed(self.commands(runtime=runtime, model=model, limit=10_000)[1], built=True)
                self.env["PATH"] = str(self.bin)
                self.assertEqual(long, short)

    def test_a_real_launch_under_a_20kb_path_types_the_exact_runtime_tail_and_nothing_else(self):
        self.env["PATH"] = self.long_path()
        out = self.invoke()
        self.assertEqual(out.returncode, 0, out.stderr)
        pane_run = self.calls()[2]["argv"]
        self.assertEqual(pane_run, self.pane_argv())
        self.assert_short_line_without_env(self.typed(pane_run))
        # The tail after the wrapper is exactly the tmux/Ghostty tail: binary, args, plugin-dir.
        tail = lambda t: t[t.index(str(self.bin / "claude")):]
        self.assertEqual(tail(shlex.split(self.typed(pane_run))), tail(self.worker_tokens()))
        self.env["PATH"] = str(self.bin)

    def test_tab_create_carries_the_workspace_as_its_environment_last_after_no_focus(self):
        out = self.invoke()
        self.assertEqual(out.returncode, 0, out.stderr)
        create = self.calls()[1]["argv"]
        self.assertEqual(create[-3:], ["--no-focus", "--env", f"CHIEF_OF_STUFF_WORKSPACE={self.root}"])
        self.assertEqual(create, self.tab_argv())

    def test_herdr_commands_builds_the_short_line_without_a_filesystem(self):
        gone = self.root / "no" / "such tree"
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            create, run = ss.herdr_commands(herdr=self.herdr, session="chief-of-stuff", cwd=str(gone),
                                            agent_type=None, binary=self.bin / "claude", title="audit-01",
                                            runtime="claude", workspace=str(self.root))
        words = shlex.split(self.typed(run, built=True))
        self.assertEqual(words[0], sys.executable)
        self.assertNotIn(ss.ENV_BIN, words)
        self.assertEqual(create[-2:], ["--env", f"CHIEF_OF_STUFF_WORKSPACE={self.root}"])
        self.assertFalse(gone.exists())

    def test_the_workspace_environment_is_absolute_even_for_a_relative_root(self):
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            create, _ = ss.herdr_commands(herdr=self.herdr, session="chief-of-stuff", cwd=str(self.tree),
                                          agent_type=None, binary=self.bin / "claude", title="audit-01",
                                          runtime="claude", workspace=os.path.relpath(self.root))
        self.assertEqual(create[-1], f"CHIEF_OF_STUFF_WORKSPACE={os.path.abspath(self.root)}")

    # The length guard: a launch never claims "started" with a line the pane shell would cut.
    def line_bytes(self, **kw):
        # Measure the line as herdr_commands builds it: herdr's claude line carries the registration
        # tokens the launcher verifies after pane run.
        return len(" ".join(shlex.quote(t) for t in self.pane_tokens(**kw)).encode())

    def test_the_limit_is_900_bytes(self):
        self.assertEqual(ss.HERDR_LINE_LIMIT, 900)

    # No test below depends on how long the real lines are (that varies with the checkout and temp paths): a refusal
    # sets the limit just under the measured line, a pass sets it to the line or above.
    def test_a_claude_line_with_short_paths_passes_the_real_limit(self):
        # Only the paths of session_exec.py and the plugin dir come from the checkout; the rest is fixed and short.
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            _, run = ss.herdr_commands(herdr=self.herdr, session="chief-of-stuff", cwd="/t/w",
                                       agent_type=None, binary=Path("/b/claude"), title="w1",
                                       runtime="claude", workspace="/t")
        self.assertLess(len(self.typed(run, built=True).encode()), ss.HERDR_LINE_LIMIT)

    def test_the_guard_counts_utf8_bytes_and_refuses_only_past_the_limit(self):
        limit = self.line_bytes(title="x") + 60
        for char in ("a", "\u00e9"):
            with self.subTest(char=char), unittest.mock.patch.object(ss, "HERDR_LINE_LIMIT", limit):
                n = next(n for n in range(1, 400) if self.line_bytes(title=char * n) > limit)
                self.assertLessEqual(self.line_bytes(title=char * (n - 1)), limit)
                self.commands(title=char * (n - 1))  # at or under the limit: built
                with self.assertRaisesRegex(ss.RefusedError, str(limit)):
                    self.commands(title=char * n)
                if char != "a":
                    chars = len(" ".join(shlex.quote(t) for t in self.worker_tokens(herdr=True, title=char * n)))
                    self.assertLessEqual(chars, limit, "bytes, not characters, are counted")

    RUNTIMES = (("claude", ""), ("codex", "gpt-test"), ("cursor", "cursor-test"), ("agy", "agy-test"))

    def launch(self, runtime, model, limit, extra=()):
        self.log.unlink(missing_ok=True)
        self.dispatch.unlink(missing_ok=True)
        return self.invoke(limit=limit, runtime=runtime, extra=[*(["--model", model] if model else []), *extra])

    def test_a_line_over_the_limit_is_refused_before_any_herdr_call(self):
        for runtime, model in self.RUNTIMES:
            with self.subTest(runtime=runtime):
                size = self.line_bytes(runtime=runtime, model=model)
                out = self.launch(runtime, model, size - 1)
                self.assertEqual(out.returncode, 1, out.stdout)
                self.assertRegex(out.stderr, r"(?m)^refused: ")
                self.assertIn(str(size), out.stderr)
                self.assertIn(str(size - 1), out.stderr)
                self.assertRegex(out.stderr, r"pane shell cannot take a line that long")
                self.assertNotIn(str(self.root), out.stderr)
                self.assertNotIn(str(self.tree), out.stderr)
                self.assertEqual(self.calls(), [], "no tab may be created for a line that cannot be typed")
                self.assertFalse(self.dispatch.exists(), "a refused launch leaves the tree reusable")
                self.assertFalse(self.runtime_log.exists())
                self.assertNotIn("started", out.stdout)

    def test_a_line_exactly_at_the_limit_is_launched(self):
        for runtime, model in self.RUNTIMES:
            with self.subTest(runtime=runtime):
                out = self.launch(runtime, model, self.line_bytes(runtime=runtime, model=model))
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual([call["argv"][2:4] for call in self.calls()],
                                 [["session", "list"], ["tab", "create"], ["pane", "run"]])

    def test_the_guard_also_refuses_the_dry_run_it_would_have_printed(self):
        out = self.launch("codex", "gpt-test", 50, extra=["--dry-run"])
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("would run:", out.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_refused_launch_leaves_the_tree_reusable_for_a_relaunch(self):
        refused = self.launch("codex", "gpt-test", 50)
        self.assertEqual(refused.returncode, 1)
        again = self.invoke(limit=10_000)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual([call["argv"] for call in self.calls()],
                         [self.session_list_argv(), self.tab_argv(), self.pane_argv()])
        self.assertTrue(self.dispatch.is_file())

    def test_the_guard_never_applies_to_tmux_or_ghostty(self):
        self.executable("tmux", f"""import json, sys
with open({str(self.log)!r}, 'a') as log:
    log.write(json.dumps({{'argv': sys.argv[1:]}}) + '\\n')
""")
        out = self.invoke(limit=50, launcher="tmux", runtime="codex", extra=["--model", "m"])
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.invoke(limit=50, launcher="ghostty", runtime="codex", extra=["--dry-run", "--model", "m"])
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_control_tmux_and_ghostty_keep_env_cwd_and_the_launching_path(self):
        self.executable("tmux", f"""import json, sys
with open({str(self.log)!r}, 'a') as log:
    log.write(json.dumps({{'argv': sys.argv[1:]}}) + '\\n')
""")
        self.env["PATH"] = launching = self.long_path()
        out = self.invoke(launcher="tmux")
        self.assertEqual(out.returncode, 0, out.stderr)
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[6:11], [ss.ENV_BIN, "-C", str(self.tree), f"PATH={launching}",
                                      f"CHIEF_OF_STUFF_WORKSPACE={self.root}"])
        self.assertNotIn("--workspace", argv)
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            script = ss.ghostty_script(cwd=str(self.tree), agent_type=None, claude=self.bin / "claude",
                                       title="audit-01", workspace=str(self.root))
        self.assertIn(shlex.quote(f"PATH={launching}"), script)
        self.assertIn("/usr/bin/env -C", script)
        self.assertNotIn("--workspace", script)
        self.env["PATH"] = str(self.bin)

    def test_hostile_titles_and_paths_round_trip_through_the_pane_shell(self):
        # The command builder must quote even inputs rejected by the assignment's name policy.
        # No shell is executed: shlex.split checks exactly the literal words herdr's shell sees.
        original_root, original_tree, original_bin = self.root, self.tree, self.bin
        original_path = self.env["PATH"]
        hostile = ("two words", "single'quote", 'double"quote', "$(touch marker)",
                   "`touch marker`", "semi; touch marker", "first\nsecond")
        for runtime, model, effort in (("claude", "sonnet", "high"), ("codex", "gpt-test", "low"),
                                       ("cursor", "cursor-test", ""), ("agy", "agy-test", "medium")):
            for value in hostile:
                with self.subTest(runtime=runtime, value=value):
                    self.root = original_root / value
                    self.tree = original_tree / value
                    self.bin = original_bin / value
                    self.env["PATH"] = str(self.bin)
                    create, run = self.commands(title=value, runtime=runtime, model=model, effort=effort,
                                                limit=100_000)
                    tokens = self.pane_tokens(title=value, runtime=runtime, model=model, effort=effort)
                    self.assertEqual(create, [str(self.herdr), *self.tab_argv(value)])
                    self.assertEqual(run[:6], [str(self.herdr), "--session", "chief-of-stuff",
                                              "pane", "run", "<root-pane>"])
                    self.assertEqual(run[6:], [shlex.quote(token) for token in tokens])
                    self.assertEqual(shlex.split(" ".join(run[6:])), tokens)
                    self.assertTrue(any(str(self.tree) in t for t in tokens), "the prompt token names the tree")
                    self.assertEqual(create[create.index("--cwd") + 1], str(self.tree))
                    script = next(i for i, t in enumerate(tokens) if t.endswith("session_exec.py"))
                    self.assertNotIn("--workspace", tokens[script:tokens.index("--", script)])
                    self.assertNotIn(ss.ENV_BIN, tokens)
                    self.assertFalse([t for t in tokens if t.startswith(("PATH=", "CHIEF_OF_STUFF_WORKSPACE="))])
        self.root, self.tree, self.bin = original_root, original_tree, original_bin
        self.env["PATH"] = original_path
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.runtime_log.exists())

    def test_valid_names_are_verbatim_in_assignment_registry_and_pane_run(self):
        for title in ("a", "audit-team", "audit_team-01", "AUDIT-01", "audit.team", "0", "7-day", "a" * 64):
            with self.subTest(title=title):
                self.log.unlink(missing_ok=True)
                self.dispatch.unlink(missing_ok=True)
                out = self.invoke(limit=10_000, title=title, runtime="codex")
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual([call["argv"] for call in self.calls()],
                                 [self.session_list_argv(), self.tab_argv(title),
                                  self.pane_argv(title=title, runtime="codex")])
                self.assertIn(f"name `{title}`;", self.dispatch.read_text())
                start = shlex.split(" ".join(self.calls()[2]["argv"][5:]))
                # session_exec registers this --name; do not execute the worker in the fake.
                registry = start.index("--registry")
                self.assertEqual(start[registry + 1],
                                 str(self.root / ss.PROMPT_DIR / "sessions" / f"{title}.json"))
                name_flag = start.index("--name", registry)
                self.assertEqual(start[name_flag + 1], title)

    def test_control_valid_session_names_stay_verbatim_in_dry_run(self):
        for title in ("a", "audit-team", "audit_team-01", "AUDIT-01", "audit.team", "0", "7-day", "a" * 64):
            with self.subTest(title=title):
                out = self.invoke(limit=10_000, title=title, runtime="codex", extra=["--dry-run"])
                self.assertEqual(out.returncode, 0, out.stderr)
                lines = out.stdout.splitlines()
                self.assertEqual(lines[0], "would run: " + shlex.join([str(self.herdr), *self.tab_argv(title)]))
                start = shlex.split(lines[1].removeprefix("would run: "))
                self.assertEqual(start[:6], [str(self.herdr), "--session", "chief-of-stuff",
                                              "pane", "run", "<root-pane>"])
                start = shlex.split(" ".join(start[6:]))
                registry = start.index("--registry")
                self.assertEqual(start[registry + 1],
                                 str(self.root / ss.PROMPT_DIR / "sessions" / f"{title}.json"))
                self.assertEqual(start[start.index("--name", registry) + 1], title)
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.dispatch.exists())

    def test_eval_argv_override_wins_over_explicit_herdr_without_resolving_it(self):
        self.assert_override_wins("herdr")

    def test_control_eval_argv_override_still_wins_over_ghostty_and_tmux(self):
        for launcher in ("ghostty", "tmux"):
            with self.subTest(launcher=launcher):
                self.assert_override_wins(launcher)

    def assert_override_wins(self, launcher):
        override = [sys.executable, "fixture.py", "{cwd}", "{title}"]
        with unittest.mock.patch.dict(os.environ, {**self.env, ss.ENV: json.dumps(override)}, clear=True), \
                unittest.mock.patch.object(ss, "resolve", side_effect=AssertionError("override resolved a terminal")), \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            try:
                code = ss.main(self.args(launcher=launcher, extra=["--dry-run"]))
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 0, errors.getvalue())
        self.assertEqual(output.getvalue().splitlines(),
                         ["would run: " + shlex.join([sys.executable, "fixture.py", str(self.tree), "audit-01"]),
                          f"would write: {self.dispatch}"])
        self.assertFalse(self.dispatch.exists())

    def test_control_tmux_fake_still_receives_exact_runtime_tokens(self):
        self.executable("tmux", f"""import json, sys
with open({str(self.log)!r}, 'a') as log:
    log.write(json.dumps({{'argv': sys.argv[1:]}}) + '\\n')
""")
        out = self.invoke(launcher="tmux")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([call["argv"] for call in self.calls()],
                         [["new-window", "-d", "-c", str(self.tree), "-n", "audit-01", *self.worker_tokens()]])
        self.assertIn("tmux", out.stdout)
        self.assertTrue(self.dispatch.is_file())
        self.assertFalse(self.runtime_log.exists())

    def test_control_ghostty_still_prints_its_tab_script_without_running_it(self):
        out = self.invoke(launcher="ghostty", extra=["--dry-run"])
        self.assertEqual(out.returncode, 0, out.stderr)
        with unittest.mock.patch.dict(os.environ, self.env, clear=True):
            script = ss.ghostty_script(cwd=str(self.tree), agent_type=None, claude=self.bin / "claude",
                                       title="audit-01", workspace=str(self.root))
        self.assertEqual(out.stdout, f"would run: {ss.OSASCRIPT} -\nwould ask Ghostty for a tab:\n"
                                    f"{script.rstrip()}\nwould write: {self.dispatch}\n")
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.dispatch.exists())

    def test_control_one_shot_still_delegates_without_a_terminal(self):
        for launcher in ("ghostty", "tmux"):
            with self.subTest(launcher=launcher):
                (self.root / "chief-of-stuff.toml").write_text(f'[workers]\nlauncher = "{launcher}"\n')
                with unittest.mock.patch.dict(os.environ, self.env, clear=True), \
                        unittest.mock.patch.object(one_shot, "run", return_value=0) as runner, \
                        unittest.mock.patch.object(ss, "resolve", side_effect=AssertionError("one-shot resolved a terminal")):
                    self.assertEqual(ss.main(self.args(launcher=None, extra=["--one-shot", "--dry-run"])), 0)
                self.assertEqual(runner.call_args.kwargs["cwd"], self.tree)
                self.assertEqual(runner.call_args.kwargs["name"], "audit-01")
                self.assertTrue(runner.call_args.kwargs["dry_run"])
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.dispatch.exists())

    def test_one_shot_ignores_configured_herdr_and_uses_the_existing_runner(self):
        (self.root / "chief-of-stuff.toml").write_text('[workers]\nlauncher = "herdr"\nmode = "one-shot"\n')
        with unittest.mock.patch.dict(os.environ, self.env, clear=True), \
                unittest.mock.patch.object(one_shot, "run", return_value=0) as runner, \
                unittest.mock.patch.object(ss, "resolve", side_effect=AssertionError("one-shot resolved herdr")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = ss.main(self.args(launcher=None, extra=["--dry-run"]))
        self.assertEqual(code, 0, errors.getvalue())
        runner.assert_called_once()
        self.assertEqual(runner.call_args.kwargs["cwd"], self.tree)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.dispatch.exists())


class WorkerModeTest(unittest.TestCase):
    def test_toml_requires_one_shot_for_every_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE + "- Settings: `chief-of-stuff.toml`\n")
            (root / "chief-of-stuff.toml").write_text('[workers]\nmode = "one-shot"\n')
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            tree = root / "trees" / "wt-x"
            tree.mkdir(parents=True)
            args = ["--cwd", str(tree), "--name", "worker01", "--task", "Security audit",
                    "--root", str(root), "--date", "2026-09-18", "--dry-run"]
            with unittest.mock.patch("one_shot.run", return_value=0) as one_shot:
                self.assertEqual(ss.main(args), 0)
                self.assertEqual(one_shot.call_args.kwargs["task"], "Security audit")
                self.assertTrue(one_shot.call_args.kwargs["dry_run"])

    def one_shot_workspace(self) -> tuple[Path, list[str]]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "CLAUDE.md").write_text(CLAUDE + "- Settings: `chief-of-stuff.toml`\n")
        (root / "chief-of-stuff.toml").write_text('[workers]\nmode = "one-shot"\n')
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
        tree = root / "trees" / "wt-x"
        tree.mkdir(parents=True)
        return tree, ["--cwd", str(tree), "--name", "worker01", "--task", "Security audit",
                      "--root", str(root), "--date", "2026-09-18", "--dry-run"]

    def test_interactive_overrides_a_one_shot_workspace(self):
        """#188: a direct request for an interactive session launches one in a one-shot workspace."""
        tree, args = self.one_shot_workspace()
        env = {ss.ENV: json.dumps(["python3", "/tmp/rec.py", "{cwd}", "{title}"])}
        with unittest.mock.patch("one_shot.run", return_value=0) as one_shot, \
             unittest.mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ss.main(args + ["--interactive"]), 0)
        one_shot.assert_not_called()
        self.assertIn("would run: python3 /tmp/rec.py", out.getvalue())
        self.assertIn(f"would write: {Path(os.path.abspath(tree)) / ss.PROMPT_FILE}", out.getvalue())

    def test_interactive_and_one_shot_together_are_refused(self):
        """#188: one request per launch; asking for both is refused, not resolved."""
        _, args = self.one_shot_workspace()
        with unittest.mock.patch("one_shot.run", return_value=0) as one_shot, \
             contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            try:
                code = ss.main(args + ["--interactive", "--one-shot"])
            except SystemExit as refused:
                code = refused.code
        self.assertNotEqual(code, 0)
        one_shot.assert_not_called()

    def test_the_launch_is_handed_the_item_for_a_task_name(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE + "- Settings: `chief-of-stuff.toml`\n")
            (root / "chief-of-stuff.toml").write_text('[workers]\nmode = "one-shot"\n')
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(
                TRACKER.replace("| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|",
                                "| name | item | owner | state | since | due | size | checklist |\n"
                                "|---|---|---|---|---|---|---|---|", 1)
                .replace("| Security audit | unassigned | open | 09:00 |  |",
                         "| audit | Security audit | unassigned | open | 09:00 |  | M |", 1))
            tree = root / "trees" / "wt-x"
            tree.mkdir(parents=True)
            with unittest.mock.patch("one_shot.run", return_value=0) as one_shot:
                self.assertEqual(ss.main(["--cwd", str(tree), "--name", "worker01", "--task", "audit",
                                          "--root", str(root), "--date", "2026-09-18", "--dry-run"]), 0)
            self.assertEqual(one_shot.call_args.kwargs["task"], "Security audit")

    def test_class_takes_the_suggested_entry_and_explicit_flags_win(self):
        """#111: --class with no --model or --runtime launches the rotation's first entry, effort and all."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE + "- Settings: `chief-of-stuff.toml`\n")
            (root / "chief-of-stuff.toml").write_text(
                '[workers]\nmode = "one-shot"\n\n[models.implement]\nrotation = ["claude:haiku@high", "codex:gpt-test"]\n')
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            tree = root / "trees" / "wt-x"
            tree.mkdir(parents=True)
            args = ["--cwd", str(tree), "--name", "worker01", "--task", "Security audit",
                    "--root", str(root), "--date", "2026-09-18", "--dry-run", "--class", "implement"]
            with unittest.mock.patch("one_shot.run", return_value=0) as one_shot:
                self.assertEqual(ss.main(args), 0)
                got = one_shot.call_args.kwargs
                self.assertEqual((got["runtime"], got["model"], got["effort"]), ("claude", "haiku", "high"))
                self.assertEqual(ss.main(args + ["--runtime", "codex", "--model", "gpt-test"]), 0)
                got = one_shot.call_args.kwargs
                self.assertEqual((got["runtime"], got["model"], got["effort"]), ("codex", "gpt-test", ""))
            (root / "chief-of-stuff.toml").write_text(
                '[workers]\nmode = "one-shot"\n\n[models.implement]\nrotation = ["codex:gpt-test@medium"]\n')
            with unittest.mock.patch("one_shot.run", return_value=0) as one_shot:
                self.assertEqual(ss.main(args), 0)
                got = one_shot.call_args.kwargs
                self.assertEqual((got["runtime"], got["model"], got["effort"]), ("codex", "gpt-test", "medium"))
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(ss.main(args[:-1] + ["review"]), 1)
            self.assertIn("[models.review]", err.getvalue())

    def dry_run_argv(self, args: list[str]) -> list[str]:
        """The argv a one-shot dry run prints, with the CLI found at a fixed path."""
        with unittest.mock.patch("one_shot.resolve", return_value="/bin/fake"), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ss.main(args), 0)
        (line,) = [l for l in out.getvalue().splitlines() if l.startswith("would run one-shot: ")]
        return shlex.split(line.removeprefix("would run one-shot: "))

    def test_codex_takes_its_effort_as_a_config_override(self):
        """#52: codex has no --effort flag; `-c model_reasoning_effort=<level>` is how a launch sets it."""
        _, args = self.one_shot_workspace()
        argv = self.dry_run_argv(args + ["--runtime", "codex", "--effort", "medium"])
        self.assertIn(("-c", "model_reasoning_effort=medium"), list(zip(argv, argv[1:])))

    def test_agy_takes_its_effort_in_a_one_shot_before_the_print_value(self):
        """#52: agy lists `--effort`; `--print` takes the prompt, so the pair comes before it."""
        _, args = self.one_shot_workspace()
        argv = self.dry_run_argv(args + ["--runtime", "agy", "--model", "m", "--effort", "medium"])
        self.assertIn(("--effort", "medium"), list(zip(argv, argv[1:])))
        self.assertEqual(argv[-2], "--print")

    def test_a_class_entry_whose_runtime_cannot_express_effort_is_refused_not_dropped(self):
        """#52: `cursor:<model>@high` silently ran with no effort; with none named it still launches."""
        tree, args = self.one_shot_workspace()
        toml = tree.parent.parent / "chief-of-stuff.toml"
        toml.write_text('[workers]\nmode = "one-shot"\n\n[models.implement]\nrotation = ["cursor:gpt-x@high"]\n')
        with unittest.mock.patch("one_shot.resolve", return_value="/bin/fake"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ss.main(args + ["--class", "implement"]), 1)
        self.assertTrue(err.getvalue().startswith("refused:"), err.getvalue())
        self.assertIn("cursor:gpt-x@high", err.getvalue())  # the entry the user wrote; no `--effort` flag was typed
        toml.write_text('[workers]\nmode = "one-shot"\n\n[models.implement]\nrotation = ["cursor:gpt-x"]\n')
        self.assertIn("--model", self.dry_run_argv(args + ["--class", "implement"]))

    def test_a_class_entrys_codex_effort_reaches_the_launch_and_an_explicit_effort_wins(self):
        """#52: `codex:<model>@medium` was parsed and then dropped, so codex ran on the user's own config effort."""
        tree, args = self.one_shot_workspace()
        (tree.parent.parent / "chief-of-stuff.toml").write_text(
            '[workers]\nmode = "one-shot"\n\n[models.implement]\nrotation = ["codex:gpt-test@medium"]\n')
        for extra, level in (([], "medium"), (["--effort", "high"], "high")):
            with self.subTest(extra=extra):
                argv = self.dry_run_argv(args + ["--class", "implement", *extra])
                self.assertIn(("-c", f"model_reasoning_effort={level}"), list(zip(argv, argv[1:])))

    def test_cli_one_shot_selects_one_task_in_interactive_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "CLAUDE.md").write_text(CLAUDE + "- Settings: `chief-of-stuff.toml`\n")
            (root / "chief-of-stuff.toml").write_text('[workers]\nmode = "interactive"\n')
            (root / "daily").mkdir()
            (root / "daily" / "2026-09-18-tracker.md").write_text(TRACKER)
            tree = root / "trees" / "wt-x"
            tree.mkdir(parents=True)
            with unittest.mock.patch("one_shot.run", return_value=0) as one_shot:
                self.assertEqual(ss.main(["--cwd", str(tree), "--name", "worker01",
                                          "--task", "Security audit", "--root", str(root),
                                          "--date", "2026-09-18", "--one-shot", "--dry-run"]), 0)
                one_shot.assert_called_once()


class CheckTest(unittest.TestCase):
    """#61: `worker --check --task T` validates a row with no tree, writes nothing, starts nothing.

    The coordinator checks a new Tasks and File ownership row before proposing it. Without --check,
    `worker` needs --cwd and --name, so the row was checked against an unrelated tree.
    """

    def workspace(self, tracker: str = TRACKER, toml: str | None = None, claude: str = CLAUDE) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "CLAUDE.md").write_text(claude + ("- Settings: `chief-of-stuff.toml`\n" if toml else ""))
        if toml:
            (root / "chief-of-stuff.toml").write_text(toml)
        (root / "daily").mkdir()
        (root / "daily" / "2026-09-18-tracker.md").write_text(tracker)
        return root

    @staticmethod
    def snapshot(root: Path) -> dict[str, bytes | None]:
        return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}

    def check(self, root: Path, *extra: str, task: str = "Security audit") -> tuple[int, str, str]:
        """Run `--check` in process; any process start or dispatch write fails the test."""
        def boom(*a, **k):
            raise AssertionError("--check started a process or wrote a file")
        before = self.snapshot(root)
        with unittest.mock.patch.object(ss.subprocess, "Popen", boom), \
             unittest.mock.patch.object(ss.subprocess, "run", boom), \
             unittest.mock.patch.object(ss.dispatch_prompt, "write_dispatch", boom), \
             unittest.mock.patch("one_shot.run", boom), \
             contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                code = ss.main(["--check", "--root", str(root), "--date", "2026-09-18", "--task", task, *extra])
            except SystemExit as refused:
                code = refused.code
        self.assertEqual(self.snapshot(root), before, "--check changed the workspace")
        return code, out.getvalue(), err.getvalue()

    def composed(self, root: Path, task: str = "Security audit", **kw) -> str:
        return ss.dispatch_prompt.compose(root, "2026-09-18", task, **kw)

    def refusal(self, root: Path, task: str = "Security audit", **kw) -> str:
        """What `check` must print for a row the real dispatch refuses: compose's own text, never a copy of it."""
        with self.assertRaises(ss.dispatch_prompt.RefusedError) as expected:
            self.composed(root, task, **kw)
        return f"refused: {expected.exception}"

    def test_a_complete_row_prints_the_assignment_with_no_cwd_and_no_name(self):
        root = self.workspace()
        code, out, err = self.check(root)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), self.composed(root).strip())
        self.assertIn("src/a/", out)

    def test_a_task_name_resolves_to_its_row(self):
        named = TRACKER.replace("| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|",
                                "| name | item | owner | state | since | due | size | checklist |\n|---|---|---|---|---|---|---|---|", 1
                                ).replace("| Security audit | unassigned | open | 09:00 |  |",
                                          "| audit | Security audit | unassigned | open | 09:00 |  | S |", 1)
        root = self.workspace(named)
        code, out, err = self.check(root, task="audit")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), self.composed(root, "Security audit").strip())

    def test_a_missing_file_ownership_row_is_refused_with_composes_text(self):
        root = self.workspace(TRACKER.replace("| Security audit | `src/a/` |\n", ""))
        with self.assertRaises(ss.dispatch_prompt.RefusedError) as expected:
            self.composed(root)
        code, out, err = self.check(root)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.strip(), f"refused: {expected.exception}")
        self.assertIn("File ownership", err)

    def test_an_unknown_task_is_refused(self):
        root = self.workspace()
        code, out, err = self.check(root, task="No such task")
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("refused: "), err)
        self.assertIn("No such task", err)

    def test_check_never_needs_a_tree_in_a_one_shot_workspace(self):
        """Superseded: it pinned the interactive assignment for a one-shot workspace. A one-shot workspace dispatches
        one-shot, so `--check` prints the one-shot assignment and still needs no tree."""
        root = self.workspace(toml='[workers]\nmode = "one-shot"\n')
        code, out, err = self.check(root)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("# One-shot assignment"), out[:80])
        self.assertIn("src/a/", out)

    def test_check_ignores_interactive_and_dry_run_in_an_interactive_workspace(self):
        """Pinned: --check prints the assignment; it does not preview ("would run") or launch. Replaces
        test_check_wins_over_one_shot_interactive_and_dry_run, whose --one-shot case pinned a false pass (see below)."""
        root = self.workspace(toml='[workers]\nmode = "interactive"\n')
        want = self.composed(root).strip()
        for flags in (["--interactive"], ["--dry-run"], ["--interactive", "--launcher", "tmux"]):
            with self.subTest(flags=flags):
                code, out, err = self.check(root, *flags)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(out.strip(), want)
                self.assertNotIn("would run", out)

    def test_one_shot_check_refuses_what_the_one_shot_dispatch_refuses(self):
        """#61 review: `emit(root, date, task)` always composed the interactive form, so a row the one-shot dispatch
        refuses (here: not open) passed. The same row passes without --one-shot, as the interactive dispatch accepts it."""
        root = self.workspace(TRACKER.replace("| unassigned | open |", "| unassigned | waiting |"))
        want = self.refusal(root, one_shot=True, worktree=Path("/x"), name="w1")
        for flags in (["--one-shot"], ["--one-shot", "--dry-run"]):
            with self.subTest(flags=flags):
                code, out, err = self.check(root, *flags)
                self.assertEqual((code, out, err.strip()), (1, "", want))
        code, out, err = self.check(root)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), self.composed(root).strip())

    def test_a_one_shot_workspace_check_refuses_that_row_unless_interactive(self):
        root = self.workspace(TRACKER.replace("| unassigned | open |", "| unassigned | waiting |"), toml='[workers]\nmode = "one-shot"\n')
        want = self.refusal(root, one_shot=True, worktree=Path("/x"), name="w1")
        code, out, err = self.check(root)
        self.assertEqual((code, out, err.strip()), (1, "", want))
        code, out, err = self.check(root, "--interactive")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), self.composed(root).strip())

    def test_one_shot_check_of_a_complete_row_prints_the_one_shot_assignment(self):
        root = self.workspace()
        code, out, err = self.check(root, "--one-shot", "--dry-run")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("# One-shot assignment"), out[:80])
        self.assertNotIn("would run", out)

    STANDING = "impl02: standing implementer. Wait idle for the next item."
    BACKLOG = CLAUDE + "- Backlog: GitHub issues; repo https://github.com/o/backlog (private)\n"

    def standing_workspace(self) -> Path:
        tracker = TRACKER.replace("Security audit | unassigned", f"{self.STANDING} | unassigned"
                                  ).replace("| Security audit | `src/a/` |", f"| {self.STANDING} | `src/d/` |")
        return self.workspace(tracker, claude=self.BACKLOG)

    def overlapping(self, other_state: str = "running 08:30", other_paths: str = "`src/a/upload.py`; worktree `trees/export` (feat/export)") -> str:
        return TRACKER.replace("| Security audit | unassigned | open | 09:00 |  | Checklist: Security audit of the upload handler |\n",
                               "| Security audit | unassigned | open | 09:00 |  | Checklist: Security audit of the upload handler |\n"
                               f"| Export header | worker07 | {other_state} | 08:00 |  | Export |\n"
                               ).replace("| Security audit | `src/a/` |\n", f"| Security audit | `src/a/` |\n| Export header | {other_paths} |\n")

    def test_one_shot_check_reports_the_launchers_overlap_refusal_and_starts_nothing(self):
        # #365 review R10: `--check --one-shot` refuses a row whose paths overlap a running row, with the text the launcher's
        # own refusal carries (one rule, read once), so the coordinator learns before it launches. Nothing is written.
        root = self.workspace(self.overlapping())
        copy = root / "copy-tracker.md"
        copy.write_text((root / "daily/2026-09-18-tracker.md").read_text())
        with self.assertRaises(ValueError) as launcher:
            one_shot.record_launch(copy, "Security audit", "worker01", "worktree `t` (b)", "claude", "", "10:00")
        self.assertIn("Export header", str(launcher.exception))
        self.assertIn("overlap", str(launcher.exception))
        code, out, err = self.check(root, "--one-shot")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.strip(), f"refused: {launcher.exception}")

    def test_one_shot_check_names_the_running_task_by_its_name_not_its_whole_item(self):
        # #365 review: the same refusal as the launcher's, naming the running row by its `name` cell.
        prompt = "Cap uploads at 25 MB across the form and the API. " * 8
        tracker = TRACKER.replace(
            "| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|\n"
            "| Security audit | unassigned | open | 09:00 |  | Checklist: Security audit of the upload handler |\n",
            "| name | item | owner | state | since | due | size | checklist |\n|---|---|---|---|---|---|---|---|\n"
            f"| upload | {prompt} | worker07 | running 08:30 | 08:00 |  | M | Cap |\n"
            "| audit | Security audit | unassigned | open | 09:00 |  | S | Inspect |\n"
        ).replace("| Security audit | `src/a/` |\n", "| audit | `src/a/` |\n| upload | `src/a/upload.py`; worktree `trees/up` (feat/up) |\n")
        root = self.workspace(tracker)
        code, out, err = self.check(root, "--one-shot")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("'upload'", err)
        self.assertNotIn("Cap uploads", err)

    def test_one_shot_check_passes_when_nothing_running_overlaps(self):
        for label, state, paths in (("another path", "running 08:30", "`src/b/`"), ("read-only", "running 08:30", "none; read-only review of `src/a/`"),
                                    ("ready only", "open", "`src/a/`"), ("not running", "waiting", "`src/a/`")):
            with self.subTest(label):
                root = self.workspace(self.overlapping(state, paths))
                code, out, err = self.check(root, "--one-shot")
                self.assertEqual((code, err), (0, ""))
                self.assertTrue(out.startswith("# One-shot assignment"), out[:80])

    def test_a_standing_row_is_valid_only_when_check_is_given_its_name(self):
        """#61 review: `name` never reached compose, so a standing placeholder in a workspace with a backlog was refused
        for naming no issue, and the agent rule then forbade proposing it."""
        root = self.standing_workspace()
        code, out, err = self.check(root, "--name", "impl02", task=self.STANDING)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), self.composed(root, self.STANDING, name="impl02").strip())
        want = self.refusal(root, self.STANDING)
        self.assertIn("names no issue", want)
        code, out, err = self.check(root, task=self.STANDING)
        self.assertEqual((code, out, err.strip()), (1, "", want))
        want = self.refusal(root, self.STANDING, name="impl03")
        code, out, err = self.check(root, "--name", "impl03", task=self.STANDING)
        self.assertEqual((code, out, err.strip()), (1, "", want))

    def test_check_passes_a_workflow_row_in_a_workspace_with_a_backlog(self):
        """#362: an `issue` cell of exactly `workflow` marks a step the pipeline performs; `--check` agrees with compose
        that it names no issue and is not refused for it."""
        tracker = TRACKER.replace(
            "| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|\n"
            "| Security audit | unassigned | open | 09:00 |  | Checklist: Security audit of the upload handler |",
            "| name | item | owner | state | since | due | size | issue | checklist |\n|---|---|---|---|---|---|---|---|---|\n"
            "| Audit | Security audit | unassigned | open | 09:00 |  | S | workflow | Checklist: Security audit of the upload handler |")
        self.assertIn("| workflow |", tracker)
        root = self.workspace(tracker, claude=self.BACKLOG)
        code, out, err = self.check(root)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), self.composed(root).strip())

    def test_check_takes_the_token_exactly_workflow_once_its_whitespace_is_stripped(self):
        """#366 review R1: `--check` agrees with compose on every spelling: ` workflow ` passes, the others are refused."""
        base = TRACKER.replace(
            "| item | owner | state | since | due | checklist |\n|---|---|---|---|---|---|\n"
            "| Security audit | unassigned | open | 09:00 |  | Checklist: Security audit of the upload handler |",
            "| name | item | owner | state | since | due | size | issue | checklist |\n|---|---|---|---|---|---|---|---|---|\n"
            "| Audit | Security audit | unassigned | open | 09:00 |  | S | {} | Checklist: Security audit of the upload handler |")
        root = self.workspace(base.format("  workflow  "), claude=self.BACKLOG)
        code, out, err = self.check(root)
        self.assertEqual((code, err), (0, ""))
        for cell in ("Workflow", "WORKFLOW", "workflow.", "workflows", "my workflow"):
            with self.subTest(cell=cell):
                root = self.workspace(base.format(cell), claude=self.BACKLOG)
                want = self.refusal(root)
                self.assertIn("not an issue reference", want)
                code, out, err = self.check(root)
                self.assertEqual((code, out, err.strip()), (1, "", want))

    def test_check_refuses_a_name_that_is_not_a_session_name(self):
        root = self.workspace()
        want = self.refusal(root, name="bad name!")
        code, out, err = self.check(root, "--name", "bad name!")
        self.assertEqual((code, out, err.strip()), (1, "", want))

    def test_launch_only_flags_neither_refuse_nor_change_a_check(self):
        """#61 review: --model/--effort/--class validation ran before the check branch, so `--check --runtime agy` said
        "--model needs a model id". --check composes the Tasks and File ownership rows; these flags are no part of that."""
        root = self.workspace()
        want = self.composed(root).strip()
        for flags in (["--runtime", "agy"], ["--runtime", "codex", "--model", "not a model!"], ["--model", "bad model!"],
                      ["--runtime", "agy", "--effort", "high"], ["--effort", "bogus"], ["--class", "nope"],
                      ["--coordinator", "x y z"], ["--timeout-minutes", "0"]):
            with self.subTest(flags=flags):
                code, out, err = self.check(root, *flags)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(out.strip(), want)

    def test_the_check_help_says_it_checks_the_rows_only(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            ss.main(["--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("checks the Tasks and File ownership rows only", text)
        self.assertNotIn("wins over every launch flag", text)

    def test_without_check_cwd_and_name_are_still_required(self):
        for args in (["--task", "Security audit"], ["--task", "Security audit", "--cwd", "/tmp"],
                     ["--task", "Security audit", "--name", "w1"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as stop:
                    ss.main(args)
                self.assertEqual(stop.exception.code, 2)
                self.assertTrue("--cwd" in err.getvalue() or "--name" in err.getvalue(), err.getvalue())

    def test_the_command_runs_from_an_unrelated_directory(self):
        """The real CLI: `chief-of-stuff worker --check` from a directory that is not the workspace."""
        root = self.workspace(TRACKER.replace("| Security audit | `src/a/` |\n", ""))
        entry = Path(ss.__file__).resolve().parent.parent / "chief_of_stuff.py"
        with tempfile.TemporaryDirectory() as elsewhere:
            run = subprocess.run([sys.executable, str(entry), "worker", "--check", "--root", str(root),
                                  "--date", "2026-09-18", "--task", "Security audit"],
                                 capture_output=True, text=True, timeout=30, cwd=elsewhere)
            self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertTrue(run.stderr.startswith("refused: no File ownership row"), run.stderr)
