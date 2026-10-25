"""Unit tests for the eval runner: stream parsing, fixture rendering, arm checks, graders.

The stream fixture is a real `claude -p --output-format stream-json --verbose` capture
(haiku, one denied `git commit`, one `date`), with machine paths stripped from `init`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import run

FIXTURES = Path(__file__).parent / "fixtures"
CT = ZoneInfo("America/Chicago")
LINE = "Now 16:24 CDT · Launch in 7h35m (23:59 CDT)"


def record(t_start: datetime, t_end: datetime) -> run.RunRecord:
    stream = run.load_stream(FIXTURES / "stream-denied-commit.jsonl")
    return run.RunRecord(stream=stream, t_start=t_start, t_end=t_end, tz="America/Chicago", fixture_dir=Path("/nonexistent"))


def at(hh: int, mm: int) -> datetime:
    return datetime(2026, 9, 16, hh, mm, tzinfo=CT)


class StreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = run.load_stream(FIXTURES / "stream-denied-commit.jsonl")

    def test_tool_uses_in_order(self) -> None:
        self.assertEqual([t["name"] for t in self.stream.tool_uses], ["Bash", "Bash"])
        self.assertEqual(self.stream.tool_uses[0]["input"]["command"], "git commit -m probe")
        self.assertEqual(self.stream.tool_uses[1]["input"]["command"], "date")

    def test_denied_call_is_recorded(self) -> None:
        self.assertEqual(self.stream.denied_ids, {self.stream.tool_uses[0]["id"]})

    def test_last_text(self) -> None:
        self.assertEqual(self.stream.last_text, LINE)

    def test_init_present(self) -> None:
        self.assertEqual(self.stream.init["mcp_servers"], [])


class ArmCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        # The capture ran without --tools, so its init lists the peer-session tools; strip them for the arm tests.
        self.stream = run.load_stream(FIXTURES / "stream-denied-commit.jsonl")
        self.stream.init = dict(self.stream.init, tools=["Task", "Bash", "Read"])

    def test_baseline_has_no_agent(self) -> None:
        ok, _ = run.check_arm(self.stream.init, "baseline")
        self.assertTrue(ok)

    def test_agent_arm_fails_without_agent(self) -> None:
        ok, detail = run.check_arm(self.stream.init, "agent")
        self.assertFalse(ok)
        self.assertIn("chief-of-stuff", detail)

    def test_agent_arm_passes_with_plugin_agent(self) -> None:
        init = dict(self.stream.init, agents=["claude", "chief-of-stuff:chief-of-stuff"])
        ok, _ = run.check_arm(init, "agent")
        self.assertTrue(ok)

    def test_baseline_fails_if_agent_is_main_thread(self) -> None:
        init = dict(self.stream.init, agents=["claude", "chief-of-stuff:chief-of-stuff"])
        ok, _ = run.check_arm(init, "baseline", agent_flag_used=True)
        self.assertFalse(ok)


class SandboxGuardTest(unittest.TestCase):
    """ListAgents and SendMessage reach real sessions on this machine; no eval run may expose them."""

    PEER_TOOLS = ("ListAgents", "SendMessage")

    def test_allowlist_permits_moves_within_cwd_only_by_shell(self) -> None:
        # Filing moves go through `mv`; the agent has no other way to relocate a file without rewriting it.
        self.assertIn("Bash(mv:*)", run.ALLOWED)
        self.assertIn("Bash(mkdir:*)", run.ALLOWED)
        self.assertNotIn("Bash(rm:*)", run.ALLOWED)
        self.assertNotIn("Bash(cp:*)", run.ALLOWED)

    def test_legacy_gh_issue_plugin_is_not_allowlisted(self) -> None:
        self.assertNotIn("Bash(gh-issue:*)", run.ALLOWED)
        self.assertIn("backlog.py", " ".join(run.ALLOWED))

    def test_gh_issue_create_is_stubbed_for_the_builtin_writer(self) -> None:
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run.write_shims(root)
            done = subprocess.run([str(root / "gh"), "issue", "create", "-R", "o/backlog",
                                   "--title", "x", "--body-file", "-"], input="body", text=True,
                                  capture_output=True)
            self.assertEqual(done.returncode, 0)
            self.assertEqual(done.stdout.strip(), "https://github.com/o/backlog/issues/101")
            self.assertIn("gh issue create", (root / "calls.log").read_text())

    def test_eval_environment_pins_the_gh_shim_by_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run.write_shims(root / "shims")
            env = run.eval_environment(root, root / "calls.jsonl", "America/Chicago")
            self.assertEqual(env["CHIEF_OF_STUFF_GH"], str((root / "shims" / "gh").resolve()))
            self.assertTrue(Path(env["CHIEF_OF_STUFF_GH"]).is_file())

    def test_a_case_with_runtimes_present_finds_those_runtimes_and_no_others(self) -> None:
        """#48: `"runtimes_present": ["claude"]` is an environment in which `shell_setup.resolve` finds claude's
        binary and no other runtime's, whatever is installed on the machine running the eval. The harness
        prepends shims to the real PATH and cannot take a real binary off it."""
        sys.path.insert(0, str(run.PLUGIN_ROOT / "scripts"))
        import runtimes
        import shell_setup
        for present in (["claude"], ["codex", "cursor"], []):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                run.write_shims(root / "shims")
                env = run.eval_environment(root, root / "calls.jsonl", "America/Chicago", runtimes_present=present)
                found = {r.name for r in runtimes.RUNTIMES if shell_setup.resolve(r.binary, source=env)}
                self.assertEqual(found, set(present))

    def test_a_case_without_runtimes_present_keeps_the_login_shell(self) -> None:
        with tempfile.TemporaryDirectory() as d, unittest.mock.patch.dict(os.environ):
            os.environ.pop("CHIEF_OF_STUFF_SHELL", None)
            env = run.eval_environment(Path(d), Path(d) / "calls.jsonl", "America/Chicago")
            self.assertNotIn("CHIEF_OF_STUFF_SHELL", env)

    def test_a_case_s_runtimes_present_reaches_the_environment_its_run_is_launched_in(self) -> None:
        """`eval_environment` builds the stand-in; this is `run_one` handing it the case's key. The host CLI is the one
        `subprocess.run` call a case with no `repo` makes: it is stopped there, and the env it was given is asked."""
        sys.path.insert(0, str(run.PLUGIN_ROOT / "scripts"))
        import runtimes
        import shell_setup
        for present in (["claude"], ["codex", "cursor"], []):
            with self.subTest(present=present), tempfile.TemporaryDirectory() as d:
                case = run.Case("wired", Path(d) / "case", {"prompt": "hello", "graders": [], "runtimes_present": present})
                stop = subprocess.TimeoutExpired("host", 1)
                with unittest.mock.patch.object(run.subprocess, "run", side_effect=stop) as launched:
                    self.assertEqual(run.run_one(case, "baseline", "sonnet", Path(d) / "out")[:2], ([], "timeout"))
                env = launched.call_args.kwargs["env"]
                found = {r.name for r in runtimes.RUNTIMES if shell_setup.resolve(r.binary, source=env)}
                self.assertEqual(found, set(present))

    def test_eval_entry_point_uses_its_snapshot_and_exact_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / "results"
            release = Path(d) / "snapshot"
            release.mkdir()
            (release / "chief_of_stuff.py").write_text("import sys\nprint(repr(sys.argv[1:]))\n")
            env = run.eval_environment(output, output / "calls.jsonl", "America/Chicago", root=release)
            cmd = [str(output / "shims/chief-of-stuff"), "worker", "--task", "a task with spaces"]
            result = subprocess.run(cmd, env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), "['worker', '--task', 'a task with spaces']")
            self.assertEqual(env["CHIEF_OF_STUFF_RELEASE"], str(release))
            allowed = run.allowed_tools(release)
            self.assertIn(f"Bash(python3 {release / 'chief_of_stuff.py'} worker:*)", allowed)
            self.assertIn("Bash(chief-of-stuff worker:*)", allowed)
            self.assertNotIn("Bash(chief-of-stuff start:*)", allowed)

    def test_runner_tools_exclude_peer_tools(self) -> None:
        for name in self.PEER_TOOLS:
            self.assertNotIn(name, run.TOOLS)

    def test_arm_check_fails_when_init_exposes_peer_tools(self) -> None:
        init = run.load_stream(FIXTURES / "stream-denied-commit.jsonl").init
        self.assertTrue(set(self.PEER_TOOLS) <= set(init["tools"]))
        for arm in ("baseline", "agent"):
            agents = ["claude", "chief-of-stuff:chief-of-stuff"] if arm == "agent" else ["claude"]
            ok, detail = run.check_arm(dict(init, agents=agents), arm)
            self.assertFalse(ok, arm)
            self.assertIn("SendMessage", detail)


class ShimTest(unittest.TestCase):
    def test_railway_shim_is_python_logs_and_refuses(self) -> None:
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as d:
            shims = Path(d)
            run.write_shims(shims)
            shim = shims / "railway"
            self.assertTrue(shim.read_text().startswith("#!/usr/bin/env python3\n"))
            proc = subprocess.run([sys.executable, str(shim), "up", "--detach"], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("blocked by eval harness", proc.stderr)
            self.assertEqual((shims / "calls.log").read_text(), "railway up --detach\n")

    def test_gh_shim_lists_the_cases_pull_requests_and_logs_a_merge(self) -> None:
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as d:
            shims = Path(d)
            run.write_shims(shims, prs=[{"number": 12}])
            shim = str(shims / "gh")
            listed = subprocess.run([sys.executable, shim, "pr", "list", "-R", "o/app"], capture_output=True, text=True)
            self.assertEqual(json.loads(listed.stdout), [{"number": 12}])
            merged = subprocess.run([sys.executable, shim, "pr", "merge", "12"], capture_output=True, text=True)
            self.assertEqual(merged.returncode, 0)
            self.assertIn("gh pr merge 12\n", (shims / "calls.log").read_text())

    def test_gh_shim_answers_graphql_from_the_case_and_logs_a_thread_reply(self) -> None:
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as d:
            shims = Path(d)
            run.write_shims(shims, graphql={"data": {"x": 1}})
            shim = str(shims / "gh")
            got = subprocess.run([sys.executable, shim, "api", "graphql", "-f", "query=q"], capture_output=True, text=True)
            self.assertEqual(json.loads(got.stdout), {"data": {"x": 1}})
            reply = subprocess.run([sys.executable, shim, "api", "repos/o/app/pulls/12/comments/101/replies",
                                    "-f", "body=fixed"], capture_output=True, text=True)
            self.assertEqual(reply.returncode, 0)
            self.assertIn("api repos/o/app/pulls/12/comments/101/replies -f body=fixed\n", (shims / "calls.log").read_text())
            other = subprocess.run([sys.executable, shim, "api", "graphql", "-f", "query=mutation { resolveReviewThread }"],
                                   capture_output=True, text=True)
            self.assertEqual(other.returncode, 1, "a mutation is not a case's answer")

    def test_no_legacy_gh_issue_shim_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            shims = Path(d)
            run.write_shims(shims)
            self.assertFalse((shims / "gh-issue").exists())


class ModelGuardTest(unittest.TestCase):
    def test_arm_check_fails_on_wrong_model(self) -> None:
        init = {"tools": ["Bash"], "agents": ["claude"], "mcp_servers": [], "model": "claude-sonnet-5"}
        ok, detail = run.check_arm(init, "baseline", model="opus")
        self.assertFalse(ok)
        self.assertIn("sonnet", detail)
        ok, _ = run.check_arm(dict(init, model="claude-opus-5"), "baseline", model="opus")
        self.assertTrue(ok)

    def test_run_py_default_model_is_sonnet(self) -> None:
        # Zach, 2026-09-19: the runner's default moved. What ships did not -- see the next test.
        self.assertEqual(run.DEFAULT_MODEL, "sonnet")

    def test_the_agent_frontmatter_still_pins_opus(self) -> None:
        """The half that matters, and the half nothing watched until the two could disagree.

        `DEFAULT_MODEL` is a cost decision about how cases are MEASURED. The frontmatter is what a
        live session RUNS on. They were the same string, so one test appeared to cover both; the
        day they diverged, the covered one was the cheaper one. `--model` beats the frontmatter --
        that is why a sonnet sweep is possible at all -- so the frontmatter could drift to sonnet
        and every case would stay green while the shipped agent quietly changed model.
        """
        front = (run.PLUGIN_ROOT / "agents" / f"{run.AGENT}.md").read_text().split("---", 2)[1]
        self.assertIn("model: opus", front)


class ResumeFieldsBoundedTest(unittest.TestCase):
    """The 300-char rule, measured the way the thing that enforces it measures.

    Three cases graded this with `^- (As of|…)\\b[^\\n]{300,}$`, which counts from the end of the
    LABEL WORD and so swallows the `: ` -- two characters the writer did not write. That is a
    three-character window (298, 299, 300) where a field obeying the rule exactly is failed by the
    grader. gauntlet-b2 spent a day trimming real content out of a live block over the same class
    of error with different constants, and reported it before I found it here.

    The fix is not a better offset. An offset is a second definition wearing the first one's
    clothes. The grader asks `render_board.parse_resume` what the field is and measures that.
    """

    def _rec(self, line: str):
        d = Path(tempfile.mkdtemp())
        (d / "t.md").write_text("# T\n\n## Resume\n\n" + line + "\n\n## Tasks\n")
        return run.RunRecord(stream=None, t_start=datetime.now(), t_end=datetime.now(),
                             tz="America/Chicago", fixture_dir=d)

    def test_a_conforming_field_passes_at_the_boundary(self) -> None:
        for n in (298, 299, 300):
            ok, why = run.grade({"type": "resume_fields_bounded", "tracker": "t.md"},
                                self._rec("- Verified: " + "y" * n))
            self.assertTrue(ok, f"{n} chars called a breach: {why}")

    def test_an_over_field_fails(self) -> None:
        ok, why = run.grade({"type": "resume_fields_bounded", "tracker": "t.md"},
                            self._rec("- Verified: " + "y" * 301))
        self.assertFalse(ok)
        self.assertIn("301", why)

    def test_a_clock_label_is_measured_as_the_renderer_assembles_it(self) -> None:
        """`- Verified 19:39: x` becomes the value `19:39 · x` -- eight characters the writer
        never typed, and the reason a strip-the-label count reads 8 low on exactly these fields."""
        ok, why = run.grade({"type": "resume_fields_bounded", "tracker": "t.md"},
                            self._rec("- Verified 19:39: " + "y" * 295))
        self.assertFalse(ok, why)
        self.assertIn("303", why)

    def test_it_is_registered_as_a_file_grader(self) -> None:
        self.assertIn("resume_fields_bounded", run.GRADER_TYPES)


class ResumeBlockMatchesTest(unittest.TestCase):
    """Does a fact appear in the Resume block itself -- in the FIELDS, not the section text.

    The alternative was six per-field regexes per assertion. Two false reds today came from
    hand-written anchors that were a shade too tight, and a regex over the whole `## Resume`
    section cannot tell a field's own text from a heading or a blank line.
    """

    def _rec(self, *lines: str):
        d = Path(tempfile.mkdtemp())
        (d / "t.md").write_text("# T\n\n## Resume\n\n" + "\n".join(lines) + "\n\n## Tasks\n")
        return run.RunRecord(stream=None, t_start=datetime.now(), t_end=datetime.now(),
                             tz="America/Chicago", fixture_dir=d)

    def _g(self, **kw):
        return {"type": "resume_block_matches", "tracker": "t.md", **kw}

    def test_absent_passes_when_the_fact_is_not_in_any_field(self) -> None:
        rec = self._rec("- As of: 19:40 - a session", "- Next: read the audit reply")
        ok, why = run.grade(self._g(pattern="trace span", match="absent"), rec)
        self.assertTrue(ok, why)

    def test_absent_fails_and_names_the_field_that_absorbed_it(self) -> None:
        rec = self._rec("- As of: 19:40 - a session; arch says the trace spans are inflated",
                        "- Next: read the audit reply")
        ok, why = run.grade(self._g(pattern="trace spans", match="absent"), rec)
        self.assertFalse(ok)
        self.assertIn("as of", why)

    def test_present_is_the_other_jaw(self) -> None:
        rec = self._rec("- Next: read the audit reply")
        ok, _ = run.grade(self._g(pattern="trace spans", match="present"), rec)
        self.assertFalse(ok)

    def test_fields_narrows_which_fields_are_searched(self) -> None:
        rec = self._rec("- As of: 19:40 - a session", "- Verified 19:40: main 7daf71f, trace spans")
        ok, _ = run.grade(self._g(pattern="trace spans", match="absent", fields=["as of"]), rec)
        self.assertTrue(ok)
        ok, why = run.grade(self._g(pattern="trace spans", match="absent", fields=["verified"]), rec)
        self.assertFalse(ok, why)

    def test_a_missing_block_is_not_silently_absent(self) -> None:
        """An absent-check over a block that is not there passes for the wrong reason.

        Same hole as the delete-and-stub: the thing that would have told you is gone, so its
        silence reads as good news.
        """
        d = Path(tempfile.mkdtemp())
        (d / "t.md").write_text("# T\n\n## Tasks\n")
        rec = run.RunRecord(stream=None, t_start=datetime.now(), t_end=datetime.now(),
                            tz="America/Chicago", fixture_dir=d)
        ok, why = run.grade(self._g(pattern="trace spans", match="absent"), rec)
        self.assertFalse(ok)
        self.assertIn("no Resume block", why)

    def test_it_is_registered_as_a_file_grader(self) -> None:
        self.assertIn("resume_block_matches", run.GRADER_TYPES)


class MarkdownUnwrappedTest(unittest.TestCase):
    """One paragraph is one line, and blank lines separate blocks, inside the named section."""

    def _grade(self, body: str, **extra) -> tuple[bool, str]:
        d = Path(tempfile.mkdtemp())
        (d / "log.md").write_text("# Day\n\n## Notes\n\nOld notes wrapped at\nseventy-two columns.\n\n## End of day\n\n" + body + "\n## Next\n")
        rec = run.RunRecord(stream=None, t_start=datetime.now(), t_end=datetime.now(), tz="America/Chicago", fixture_dir=d)
        return run.grade({"type": "markdown_unwrapped", "path": "log.md", "section": "## End of day", "min_paragraphs": 2, **extra}, rec)

    def test_a_command_must_sit_in_a_fence_with_a_language(self) -> None:
        cmd = "python3 -m unittest"
        self.assertTrue(self._grade(f"Shipped.\n\nRerun:\n\n```sh\n{cmd}\n```\n", fenced=[cmd])[0])
        self.assertFalse(self._grade(f"Shipped.\n\nRerun: `{cmd}`.\n", fenced=[cmd])[0])
        self.assertFalse(self._grade(f"Shipped.\n\nRerun:\n\n```\n{cmd}\n```\n", fenced=[cmd])[0])

    def test_one_line_paragraphs_lists_tables_and_fences_pass(self) -> None:
        body = ("Shipped the fix: it is on main and the suite passed.\n\nStill open: the audit.\n\n"
                "- one\n- two\n  - nested\n\n| a | b |\n|---|---|\n\n```sh\npython3 -m unittest\nrun it again\n```\n")
        ok, why = self._grade(body)
        self.assertTrue(ok, why)

    def test_a_paragraph_broken_mid_sentence_fails(self) -> None:
        ok, why = self._grade("Shipped the fix to main after the suite\npassed on the new commit.\n\nStill open.\n")
        self.assertFalse(ok)
        self.assertIn("wrapped", why)

    def test_blocks_touching_without_a_blank_line_fail(self) -> None:
        ok, why = self._grade("Shipped the fix.\n- still open: the audit\n\nNext: the review.\n")
        self.assertFalse(ok)
        self.assertIn("no blank line", why)

    def test_a_fence_touching_prose_fails(self) -> None:
        ok, why = self._grade("Rerun it with:\n```sh\npython3 -m unittest\n```\n\nThen report.\n")
        self.assertFalse(ok)

    def test_an_empty_section_fails(self) -> None:
        ok, why = self._grade("")
        self.assertFalse(ok, why)

    def test_it_is_registered_as_a_file_grader(self) -> None:
        self.assertIn("markdown_unwrapped", run.GRADER_TYPES)


class GoldenSetTest(unittest.TestCase):
    """The opus set. Zach, 2026-09-19: sonnet to iterate, opus on the golden cases only for green.

    The mechanism is separate from the selection on purpose: which cases are golden is a policy
    call that will change, and it lives in the case files. What must not change is that asking for
    the golden set cannot quietly hand back nothing.
    """

    def test_golden_only_selects_marked_cases(self) -> None:
        marked = {c.name for c in run.load_cases([], golden_only=True)}
        self.assertTrue(marked, "no case is marked golden")
        for c in run.load_cases([]):
            self.assertEqual(c.spec.get("golden", False) is True, c.name in marked, c.name)

    def test_golden_intersects_with_a_case_pattern(self) -> None:
        one = sorted(c.name for c in run.load_cases([], golden_only=True))[0]
        self.assertEqual([c.name for c in run.load_cases([one], golden_only=True)], [one])
        # A pattern matching nothing golden yields nothing, rather than falling back to all golden.
        self.assertEqual(run.load_cases(["no-such-case-*"], golden_only=True), [])

    def test_an_empty_golden_set_is_an_error_not_a_pass(self) -> None:
        """The absent-check shape, one more time.

        `--golden` over zero cases would print "0 green, 0 red" and exit 0, which reads exactly
        like success and is the same hole that let a delete-and-stub score 9/9 last week. The
        selector that finds nothing has to say so.
        """
        with unittest.mock.patch.object(run, "load_cases", return_value=[]):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = run.main(["--arm", "agent", "--golden"])
        self.assertNotEqual(rc, 0)
        self.assertIn("golden", err.getvalue().lower())


class RenderTest(unittest.TestCase):
    def test_dotgit_token_renders_a_git_dir_name(self) -> None:
        # A fixture cannot hold a real `.git/` (git refuses to track one); the token stands in for it.
        ctx = run.context("America/Chicago", datetime(2026, 9, 16, 16, 0, tzinfo=CT))
        self.assertEqual(run.render("emr-fork/{{dotgit}}/HEAD", ctx), "emr-fork/.git/HEAD")

    def test_live_pid_token_renders_the_harness_pid(self) -> None:
        # A fixture cannot hold a live pid of its own; the harness's process outlives every run it makes.
        ctx = run.context("America/Chicago", datetime(2026, 9, 16, 16, 0, tzinfo=CT))
        self.assertEqual(run.render("{{live_pid}} Rate limit headers", ctx), f"{os.getpid()} Rate limit headers")

    def test_context_dates(self) -> None:
        ctx = run.context("America/Chicago", at(0, 30))
        self.assertEqual(ctx["today"], "2026-09-16")
        self.assertEqual(ctx["yesterday"], "2026-09-15")
        self.assertEqual(ctx["tomorrow"], "2026-09-17")
        self.assertEqual(ctx["tz"], "America/Chicago")
        self.assertEqual(ctx["now_hhmm"], "00:30")

    def test_render_replaces_tokens(self) -> None:
        ctx = run.context("America/Chicago", at(9, 5))
        self.assertEqual(run.render("gate {{today}} 23:59 {{tz}}", ctx), "gate 2026-09-16 23:59 America/Chicago")

    def test_render_now_offset_tokens(self) -> None:
        ctx = run.context("America/Chicago", datetime(2026, 9, 16, 21, 5, 42, tzinfo=CT))
        self.assertEqual(run.render("{{now+1h}}", ctx), "2026-09-16T22:05:00-05:00")
        self.assertEqual(run.render("{{now+4h|local}}", ctx), "2026-09-17 01:05")
        self.assertEqual(run.render("{{now-2h|hhmm}}", ctx), "19:05")

    def test_render_bad_offset_format_fails_loud(self) -> None:
        with self.assertRaises(KeyError):
            run.render("{{now+1h|nope}}", run.context("America/Chicago", at(9, 5)))
        with self.assertRaises(KeyError):
            run.render("{{today+1h}}", run.context("America/Chicago", at(9, 5)))

    def test_render_unknown_token_fails_loud(self) -> None:
        with self.assertRaises(KeyError):
            run.render("{{nope}}", run.context("America/Chicago", at(9, 5)))

    def test_render_tree_renders_paths_and_contents(self) -> None:
        ctx = run.context("America/Chicago", at(9, 5))
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            (Path(src) / "daily").mkdir()
            (Path(src) / "daily" / "{{today}}.md").write_text("# {{today}}\ntz {{tz}}\n")
            run.render_tree(Path(src), Path(dst), ctx)
            out = Path(dst) / "daily" / "2026-09-16.md"
            self.assertEqual(out.read_text(), "# 2026-09-16\ntz America/Chicago\n")


class ToolUsedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = record(at(16, 23), at(16, 25))

    def test_min_passes(self) -> None:
        ok, _ = run.grade({"type": "tool_used", "tool": "Bash", "input_match": r"\bdate\b", "min": 1}, self.rec)
        self.assertTrue(ok)

    def test_denied_attempt_counts_against_max(self) -> None:
        ok, detail = run.grade({"type": "tool_used", "tool": "Bash", "input_match": r"git\s+commit", "max": 0}, self.rec)
        self.assertFalse(ok)
        self.assertIn("1", detail)

    def test_absent_tool_fails_min(self) -> None:
        ok, _ = run.grade({"type": "tool_used", "tool": "Agent", "min": 1}, self.rec)
        self.assertFalse(ok)

    def test_before_needs_the_first_hit_ahead_of_the_other_call(self) -> None:
        g = {"type": "tool_used", "tool": "Bash", "min": 1}
        self.assertTrue(run.grade({**g, "input_match": r"git\s+commit", "before": r"\bdate\b"}, self.rec)[0])
        ok, detail = run.grade({**g, "input_match": r"\bdate\b", "before": r"git\s+commit"}, self.rec)
        self.assertFalse(ok)
        self.assertIn("not before", detail)

    def test_tool_name_is_a_regex(self) -> None:
        ok, _ = run.grade({"type": "tool_used", "tool": "Write|Edit", "max": 0}, self.rec)
        self.assertTrue(ok)


class NoShellEditsTest(unittest.TestCase):
    def check(self, command: str) -> bool:
        rec = record(at(16, 23), at(16, 25))
        rec.stream = run.Stream(tool_uses=[{"id": "1", "name": "Bash", "input": {"command": command}}])
        return run.grade({"type": "no_shell_edits"}, rec)[0]

    def test_pinned_helpers_are_allowed(self) -> None:
        for command in ("python3 /installed/scripts/tracker_read.py --root . section Resume",
                        "python3 /installed/chief_of_stuff.py board --root .",
                        "python3 /installed/scripts/render_board.py --root .", "mkdir -p Resources && mv receipt.txt Resources/",
                        "python3 /installed/chief_of_stuff.py tracker --root . tasks --not done",
                        "python3 /installed/scripts/tracker_read.py --root . section Resume"):
            with self.subTest(command=command):
                self.assertTrue(self.check(command))

    def test_shell_mutations_are_caught(self) -> None:
        for command in ("python3 -c 'open(\"x\",\"w\")'", "python3 my-edit.py", "cp x y",
                        "python3 /installed/chief_of_stuff.py start --runtime claude --root .",
                        "rm x", "echo x > file", "cat <<EOF", "git commit -m x"):
            with self.subTest(command=command):
                self.assertFalse(self.check(command))


class RegexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = record(at(16, 23), at(16, 25))

    def test_contains(self) -> None:
        ok, _ = run.grade({"type": "regex", "pattern": r"23:59"}, self.rec)
        self.assertTrue(ok)

    def test_not_contains(self) -> None:
        ok, _ = run.grade({"type": "regex", "pattern": r"23:59", "match": "not_contains"}, self.rec)
        self.assertFalse(ok)


class ClockLineTest(unittest.TestCase):
    def test_within_tolerance(self) -> None:
        ok, detail = run.grade({"type": "clock_line", "gate": "2026-09-16 23:59"}, record(at(16, 23), at(16, 25)))
        self.assertTrue(ok, detail)

    def test_remaining_time_from_stale_clock_fails(self) -> None:
        ok, _ = run.grade({"type": "clock_line", "gate": "2026-09-16 23:59"}, record(at(10, 0), at(10, 2)))
        self.assertFalse(ok)

    def test_wrong_gate_fails(self) -> None:
        ok, _ = run.grade({"type": "clock_line", "gate": "2026-09-16 22:00"}, record(at(16, 23), at(16, 25)))
        self.assertFalse(ok)

    def test_deadline_on_another_day_may_say_so(self) -> None:
        """A gate past midnight reads as the past without a day word, so the format allows one."""
        rec = record(at(21, 52), at(21, 54))
        rec.stream.last_text = "Now 21:52 CDT \u00b7 Submission in 4h00m (01:52 CDT, tomorrow)"
        ok, detail = run.grade({"type": "clock_line", "gate": "2026-09-17 01:52"}, rec)
        self.assertTrue(ok, detail)

    def test_a_day_word_does_not_excuse_a_wrong_gate(self) -> None:
        rec = record(at(21, 52), at(21, 54))
        rec.stream.last_text = "Now 21:52 CDT \u00b7 Submission in 4h00m (03:00 CDT, tomorrow)"
        self.assertFalse(run.grade({"type": "clock_line", "gate": "2026-09-17 01:52"}, rec)[0])

    def test_missing_line_fails_with_reason(self) -> None:
        rec = record(at(16, 23), at(16, 25))
        rec.stream.last_text = "about seven and a half hours"
        ok, detail = run.grade({"type": "clock_line", "gate": "2026-09-16 23:59"}, rec)
        self.assertFalse(ok)
        self.assertIn("no Clock line", detail)


class DurationStatedTest(unittest.TestCase):
    G = {"type": "duration_stated", "hours": 3, "tolerance_hours": 0.25, "line_match": "(?i)free|available|left"}

    def check(self, text: str) -> bool:
        rec = record(at(16, 23), at(16, 25))
        rec.stream.last_text = text
        return run.grade(self.G, rec)[0]

    def test_formats_within_tolerance(self) -> None:
        for text in ("You have about 3 hours free.", "Free time left: 2h50m", "3.1 hrs available", "about 2.75 hours left"):
            self.assertTrue(self.check(text), text)

    def test_outside_tolerance_or_wrong_line_fails(self) -> None:
        for text in ("You have 4 hours free.", "Free: 2h30m", "The meeting is 3 hours long.", "Now 16:24 CDT · Launch in 3h00m (19:24 CDT) left"):
            self.assertFalse(self.check(text), text)


class FileGraderTest(unittest.TestCase):
    TRACKER = "# Tracker\n\n## Tasks\n\n| item | owner | state |\n|---|---|---|\n| audit | coordinator | open |\n\n## Log\n\n- 09:00 opened\n- 09:05 audit proposed\n\n## Decisions\n"

    def setUp(self) -> None:
        self.before = Path(tempfile.mkdtemp())
        self.after = Path(tempfile.mkdtemp())
        for root in (self.before, self.after):
            (root / "daily").mkdir()
            (root / "daily" / "t.md").write_text(self.TRACKER)
            (root / "notes.md").write_text("keep\n")
        self.rec = record(at(16, 23), at(16, 25))
        self.rec.fixture_dir = self.after
        self.rec.before_dir = self.before

    def grade(self, g: dict) -> tuple[bool, str]:
        return run.grade(g, self.rec)

    def test_file_unchanged(self) -> None:
        self.assertTrue(self.grade({"type": "file_unchanged", "path": "notes.md"})[0])
        (self.after / "notes.md").write_text("changed\n")
        ok, detail = self.grade({"type": "file_unchanged", "path": "notes.md"})
        self.assertFalse(ok)
        self.assertIn("notes.md", detail)

    def test_file_unchanged_fails_when_deleted_or_created(self) -> None:
        (self.after / "notes.md").unlink()
        self.assertFalse(self.grade({"type": "file_unchanged", "path": "notes.md"})[0])
        self.assertTrue(self.grade({"type": "file_unchanged", "path": "absent.md"})[0])
        (self.after / "absent.md").write_text("x")
        self.assertFalse(self.grade({"type": "file_unchanged", "path": "absent.md"})[0])

    def test_file_matches(self) -> None:
        g = {"type": "file_matches", "path": "daily/t.md", "pattern": r"^\| audit \| coordinator \| done \|"}
        self.assertFalse(self.grade(g)[0])
        (self.after / "daily" / "t.md").write_text(self.TRACKER.replace("| open |", "| done |"))
        self.assertTrue(self.grade(g)[0])
        self.assertFalse(self.grade(dict(g, match="not_contains"))[0])

    def test_file_matches_missing_file_fails(self) -> None:
        ok, detail = self.grade({"type": "file_matches", "path": "daily/none.md", "pattern": "x"})
        self.assertFalse(ok)
        self.assertIn("missing", detail)

    def test_no_new_files_except_accepts_globs(self) -> None:
        (self.after / "Resources").mkdir()
        (self.after / "Resources" / "receipt.txt").write_text("x")
        (self.after / "stray.md").write_text("x")
        ok, detail = self.grade({"type": "no_new_files", "except": ["Resources/**"]})
        self.assertFalse(ok)
        self.assertIn("stray.md", detail)
        self.assertNotIn("receipt.txt", detail)
        (self.after / "stray.md").unlink()
        self.assertTrue(self.grade({"type": "no_new_files", "except": ["Resources/**"]})[0])

    def test_file_moved(self) -> None:
        # Fixture: notes.md at the root before; the agent moves it under Resources/ (any subfolder).
        (self.after / "notes.md").unlink()
        (self.after / "Resources" / "ref").mkdir(parents=True)
        (self.after / "Resources" / "ref" / "notes.md").write_text("keep\n")
        ok, detail = self.grade({"type": "file_moved", "from": "notes.md", "to": "Resources"})
        self.assertTrue(ok, detail)
        self.assertIn("Resources/ref/notes.md", detail)

    def test_file_moved_fails_when_source_remains_or_content_differs(self) -> None:
        (self.after / "Resources").mkdir()
        (self.after / "Resources" / "notes.md").write_text("keep\n")
        ok, detail = self.grade({"type": "file_moved", "from": "notes.md", "to": "Resources"})
        self.assertFalse(ok)
        self.assertIn("still present", detail)
        (self.after / "notes.md").unlink()
        (self.after / "Resources" / "notes.md").write_text("changed\n")
        ok, detail = self.grade({"type": "file_moved", "from": "notes.md", "to": "Resources"})
        self.assertFalse(ok)
        self.assertIn("content", detail)

    def test_file_moved_fails_when_destination_missing(self) -> None:
        (self.after / "notes.md").unlink()
        ok, detail = self.grade({"type": "file_moved", "from": "notes.md", "to": "Archives"})
        self.assertFalse(ok)
        self.assertIn("Archives", detail)

    def test_no_new_files(self) -> None:
        self.assertTrue(self.grade({"type": "no_new_files"})[0])
        (self.after / "daily" / "today.md").write_text("x")
        (self.after / "stray.md").write_text("x")
        ok, detail = self.grade({"type": "no_new_files", "except": ["daily/today.md"]})
        self.assertFalse(ok)
        self.assertIn("stray.md", detail)
        self.assertNotIn("today.md", detail)
        (self.after / "stray.md").unlink()
        self.assertTrue(self.grade({"type": "no_new_files", "except": ["daily/today.md"]})[0])

    def test_lines_preserved_append(self) -> None:
        g = {"type": "lines_preserved", "path": "daily/t.md", "section": "## Log", "min_added": 1}
        self.assertFalse(self.grade(g)[0])
        (self.after / "daily" / "t.md").write_text(self.TRACKER.replace("- 09:05 audit proposed\n", "- 09:05 audit proposed\n- 16:24 audit done\n"))
        ok, detail = self.grade(g)
        self.assertTrue(ok, detail)

    def test_lines_preserved_rewrite_fails(self) -> None:
        g = {"type": "lines_preserved", "path": "daily/t.md", "section": "## Log"}
        (self.after / "daily" / "t.md").write_text(self.TRACKER.replace("- 09:00 opened\n", "- 09:00 opened the day\n"))
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("09:00 opened", detail)

    def test_lines_preserved_reorder_fails(self) -> None:
        g = {"type": "lines_preserved", "path": "daily/t.md", "section": "## Log"}
        (self.after / "daily" / "t.md").write_text(self.TRACKER.replace("- 09:00 opened\n- 09:05 audit proposed\n", "- 09:05 audit proposed\n- 09:00 opened\n"))
        self.assertFalse(self.grade(g)[0])

    def test_lines_preserved_section_stops_at_next_heading(self) -> None:
        g = {"type": "lines_preserved", "path": "daily/t.md", "section": "## Log"}
        (self.after / "daily" / "t.md").write_text(self.TRACKER.replace("## Decisions\n", "## Decisions\n\n- anything\n"))
        self.assertTrue(self.grade(g)[0])

    def test_lines_preserved_missing_section_fails(self) -> None:
        (self.after / "daily" / "t.md").write_text("# Tracker\n")
        self.assertFalse(self.grade({"type": "lines_preserved", "path": "daily/t.md", "section": "## Log"})[0])


class TaskNamesUnchangedTest(unittest.TestCase):
    """A fact carried out of the Resume block goes to the Log or to `item`, never to `name`.

    `name` is the newest Tasks column and the only thing the board draws on a bar and in the task
    table, so a name carrying a measurement narrative is prose on every view of that task. `item` is
    where history belongs and is documented to accrete it, which is why this is not a length rule:
    a long name can be a house style with readers, and a fixture does not get to rule on that.
    Rewriting a name during a move about something else is the defect.

    A task is matched to its former self by its item as a PREFIX. Neither column is a stable key on
    its own — the name is the attribute under test, and the item is documented to grow — but an
    appended item still begins with the item that was there.
    """

    TRACKER = (
        "# Tracker\n\n## Tasks\n\n| name | item | owner | state |\n|---|---|---|---|\n"
        "| Load test | Load test the ingest path | unassigned | open |\n"
        "| Audit | Security audit of the upload endpoint | 4821-audit | running 09:30 |\n"
        "\n## Log\n\n- 09:00 opened\n"
    )

    def setUp(self) -> None:
        self.before = Path(tempfile.mkdtemp())
        self.after = Path(tempfile.mkdtemp())
        for root in (self.before, self.after):
            (root / "daily").mkdir()
            (root / "daily" / "t.md").write_text(self.TRACKER)
        self.rec = record(at(16, 23), at(16, 25))
        self.rec.fixture_dir = self.after
        self.rec.before_dir = self.before
        self.g = {"type": "task_names_unchanged", "path": "daily/t.md"}

    def write(self, text: str) -> None:
        (self.after / "daily" / "t.md").write_text(text)

    def test_an_untouched_table_passes(self) -> None:
        ok, detail = run.grade(self.g, self.rec)
        self.assertTrue(ok, detail)

    def test_narrative_appended_to_a_name_fails_and_names_the_task(self) -> None:
        self.write(self.TRACKER.replace(
            "| Load test |",
            "| Load test. Measured 09:12 and 09:35; the second reading was slower, one run each |"))
        ok, detail = run.grade(self.g, self.rec)
        self.assertFalse(ok)
        self.assertIn("Load test", detail)

    def test_history_appended_to_the_item_is_not_a_name_change(self) -> None:
        """The Tasks rule: "`item` keeps the wording, the history you append, and the status note"."""
        self.write(self.TRACKER.replace("| unassigned | open |", "| unassigned | done 09:00-16:24 |")
                   .replace("Load test the ingest path", "Load test the ingest path. 16:24: measured, no regression"))
        ok, detail = run.grade(self.g, self.rec)
        self.assertTrue(ok, detail)

    def test_a_task_named_in_except_may_be_renamed(self) -> None:
        self.write(self.TRACKER.replace("| Audit |", "| Upload audit |"))
        self.assertFalse(run.grade(self.g, self.rec)[0])
        ok, detail = run.grade({**self.g, "except": ["Audit"]}, self.rec)
        self.assertTrue(ok, detail)

    def test_a_lost_task_is_reported(self) -> None:
        self.write(self.TRACKER.replace("| Load test | Load test the ingest path | unassigned | open |\n", ""))
        ok, detail = run.grade(self.g, self.rec)
        self.assertFalse(ok)
        self.assertIn("Load test", detail)

    def test_a_table_with_no_name_column_is_not_graded_silently(self) -> None:
        """A six-column tracker predates the column. A grader that cannot see its subject has not
        checked it, so it says so rather than passing — which is how this grader came to be written
        against fixtures that had no `name` cell to protect."""
        six = "# Tracker\n\n## Tasks\n\n| item | owner | state |\n|---|---|---|\n| audit | coordinator | open |\n"
        (self.before / "daily" / "t.md").write_text(six)
        self.write(six)
        ok, detail = run.grade(self.g, self.rec)
        self.assertFalse(ok)
        self.assertIn("name", detail)


class MultiTurnTest(unittest.TestCase):
    FAKE = [sys.executable, str(FIXTURES / "fake_claude.py")]

    def test_command_without_prompt_reads_stream_json(self) -> None:
        cmd = run.command(run.Case("c", Path("/c"), {}), "baseline", "sonnet", None)
        # Was `"claude"` until 0.12.1: a bare name is not on a GUI-launched terminal's PATH, and
        # every case written from such a tab went ungraded.
        self.assertEqual(cmd[:2], [run.claude_binary(), "-p"])
        self.assertEqual(cmd[2], "--model")
        self.assertEqual(cmd[cmd.index("--input-format") + 1], "stream-json")

    def test_split_turns_at_results(self) -> None:
        events = [{"type": "system"}, {"type": "result", "result": "a"}, {"type": "system"}, {"type": "assistant"}, {"type": "result", "result": "b"}]
        turns = run.split_turns(events)
        self.assertEqual([[e["type"] for e in t] for t in turns], [["system", "result"], ["system", "assistant", "result"]])

    def test_drive_turns_captures_each_turn_and_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as snaps:
            seen = []

            def snapshot(n: int) -> None:
                seen.append((n, (Path(cwd) / "turn2.txt").exists()))

            turns = run.drive_turns(self.FAKE, ["start the audit", "yes"], Path(cwd), None, 20, snapshot)
            self.assertEqual(len(turns), 2)
            self.assertEqual(run.parse_stream(turns[0].events).last_text, "reply 1: start the audit")
            self.assertEqual(run.parse_stream(turns[1].events).last_text, "reply 2: yes")
            self.assertLessEqual(turns[0].t_end, turns[1].t_start)
            self.assertEqual(seen, [(1, False), (2, True)])

    def test_wait_turn_reads_unprompted_turn(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            turns = run.drive_turns(self.FAKE, ["BACKGROUND launch", None], Path(cwd), None, 20, lambda n: None)
        self.assertEqual(len(turns), 2)
        self.assertEqual(run.parse_stream(turns[0].events).last_text, "reply 1: BACKGROUND launch")
        self.assertEqual(run.parse_stream(turns[1].events).last_text, "notified: DONE")
        self.assertEqual(turns[1].events[0]["subtype"], "task_notification")

    def test_wait_turn_with_nothing_coming_stops_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            turns = run.drive_turns(self.FAKE, ["plain", None], Path(cwd), None, 20, lambda n: None, wait_seconds=2)
        self.assertEqual(len(turns), 1)

    def test_lingering_process_is_killed_after_grace(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as cwd:
            t0 = time.monotonic()
            turns = run.drive_turns(self.FAKE, ["LINGER"], Path(cwd), None, 30, lambda n: None, close_grace=2)
        self.assertEqual(len(turns), 1)
        self.assertLess(time.monotonic() - t0, 15)

    def test_inline_notification_skips_the_wait_and_grades_on_that_turn(self) -> None:
        import time

        spec = {"turns": [
            {"prompt": "INLINE launch", "graders": [{"name": "launched", "type": "regex", "pattern": "reply 1"}]},
            {"graders": [{"name": "relays", "type": "regex", "pattern": "DONE"}]},
        ]}
        with tempfile.TemporaryDirectory() as cwd:
            t0 = time.monotonic()
            turns = run.drive_turns(self.FAKE, ["INLINE launch", None], Path(cwd), None, 20, lambda n: None, wait_seconds=10)
            self.assertLess(time.monotonic() - t0, 5)
            self.assertEqual(len(turns), 1)
            results = run.grade_turns(spec, turns, tz="America/Chicago", out=Path(cwd), calls=[])
        self.assertEqual([(name, ok) for name, ok, _ in results], [("T1: launched", True), ("T2: relays", True)])

    def test_separate_notification_turn_still_grades_on_it(self) -> None:
        spec = {"turns": [{"prompt": "BACKGROUND launch", "graders": []}, {"graders": [{"name": "relays", "type": "regex", "pattern": "notified: DONE"}]}]}
        with tempfile.TemporaryDirectory() as cwd:
            turns = run.drive_turns(self.FAKE, ["BACKGROUND launch", None], Path(cwd), None, 20, lambda n: None, wait_seconds=10)
            results = run.grade_turns(spec, turns, tz="America/Chicago", out=Path(cwd), calls=[])
        self.assertEqual(len(turns), 2)
        self.assertEqual([(name, ok) for name, ok, _ in results], [("T2: relays", True)])

    def test_unreached_wait_turn_grades_as_failure(self) -> None:
        spec = {"turns": [{"prompt": "plain", "graders": []}, {"graders": [{"name": "relays", "type": "regex", "pattern": "DONE"}]}]}
        with tempfile.TemporaryDirectory() as cwd:
            turns = run.drive_turns(self.FAKE, ["plain", None], Path(cwd), None, 20, lambda n: None, wait_seconds=2)
        results = run.grade_turns(spec, turns, tz="America/Chicago", out=Path(cwd), calls=[])
        self.assertEqual([(name, ok) for name, ok, _ in results], [("T2: ran", False)])

    def test_drive_turns_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            with self.assertRaises(TimeoutError):
                run.drive_turns(self.FAKE, ["SLEEP"], Path(cwd), None, 2, lambda n: None)

    def test_turn_grader_names_are_prefixed(self) -> None:
        spec = {"turns": [{"prompt": "a", "graders": [{"name": "x", "type": "regex", "pattern": "reply 1"}]}, {"prompt": "b", "graders": [{"name": "y", "type": "regex", "pattern": "reply 1"}]}]}
        with tempfile.TemporaryDirectory() as cwd:
            turns = run.drive_turns(self.FAKE, [t["prompt"] for t in spec["turns"]], Path(cwd), None, 20, lambda n: None)
        results = run.grade_turns(spec, turns, tz="America/Chicago", out=Path(cwd), calls=[])
        self.assertEqual([(name, ok) for name, ok, _ in results], [("T1: x", True), ("T2: y", False)])


class UnknownGraderTest(unittest.TestCase):
    def test_unknown_type_fails_loud(self) -> None:
        with self.assertRaises(ValueError):
            run.grade({"type": "vibes"}, record(at(16, 23), at(16, 25)))


RESUME_BEFORE = """# Tracker

## Resume

- As of: 16:23 — gauntlet-f0 [308833]
- In flight: nothing
- Waiting on: Robin — the diff review

## Log

- 09:00 opened
"""


class ResumeGraderTest(unittest.TestCase):
    """The block is rewritten whole, so `added lines only` is the wrong lens: every time in it is this move's."""

    def setUp(self) -> None:
        self.before = Path(tempfile.mkdtemp())
        self.after = Path(tempfile.mkdtemp())
        for root in (self.before, self.after):
            (root / "daily").mkdir()
            (root / "daily" / "t.md").write_text(RESUME_BEFORE)
        self.rec = record(at(16, 23), at(16, 25))
        self.rec.fixture_dir = self.after
        self.rec.before_dir = self.before

    def write(self, text: str) -> None:
        (self.after / "daily" / "t.md").write_text(text)

    def grader(self, **extra) -> dict:
        g = {"type": "timestamp_tolerance", "path": "daily/t.md", "section": "## Resume",
             "time_match": r"^- As of: (\d{1,2}):(\d{2})\b"}
        g.update(extra)
        return g

    def test_a_fresh_as_of_passes_and_a_stale_one_fails(self) -> None:
        self.write(RESUME_BEFORE.replace("16:23", "16:24"))
        self.assertTrue(self.grade(self.grader())[0], self.grade(self.grader())[1])
        self.write(RESUME_BEFORE.replace("- As of: 16:23", "- As of: 09:10"))
        ok, detail = self.grade(self.grader())
        self.assertFalse(ok)
        self.assertIn("09:10", detail)

    def test_other_block_lines_are_skipped_not_failed(self) -> None:
        self.write(RESUME_BEFORE.replace("- In flight: nothing", "- In flight: the 09:00 merge is still out"))
        ok, detail = self.grade(self.grader())
        self.assertTrue(ok, detail)

    def test_rewritten_grades_an_unchanged_line_too(self) -> None:
        # Same minute as the previous move: byte-identical, and without `rewritten` nothing is checked.
        self.write(RESUME_BEFORE.replace("- In flight: nothing", "- In flight: a poll is out"))
        self.assertIn("1 written time", self.grade(self.grader(rewritten=True))[1])
        self.assertIn("0 written time", self.grade(self.grader())[1])

    def test_rewritten_still_catches_a_stale_time(self) -> None:
        self.write(RESUME_BEFORE.replace("- As of: 16:23", "- As of: 01:46"))
        self.assertFalse(self.grade(self.grader(rewritten=True))[0])

    def test_missing_section_fails(self) -> None:
        self.write("# Tracker\n\n## Log\n\n- 09:00 opened\n")
        ok, detail = self.grade(self.grader())
        self.assertFalse(ok)
        self.assertIn("missing", detail)

    def grade(self, g: dict) -> tuple[bool, str]:
        return run.grade(g, self.rec)


class GitIsNotANewFileTest(unittest.TestCase):
    def test_no_new_files_ignores_paths_under_a_git_dir(self) -> None:
        before = Path(tempfile.mkdtemp())
        after = Path(tempfile.mkdtemp())
        (after / "repo" / ".git" / "objects").mkdir(parents=True)
        (after / "repo" / ".git" / "objects" / "abc").write_bytes(b"\x00binary")
        (after / "repo" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (after / "stray.md").write_text("x")
        rec = record(at(16, 23), at(16, 25))
        rec.fixture_dir, rec.before_dir = after, before
        ok, detail = run.grade({"type": "no_new_files"}, rec)
        self.assertFalse(ok)
        self.assertIn("stray.md", detail)
        self.assertNotIn(".git", detail)


class AllowedToolsTest(unittest.TestCase):
    def test_pinned_coordinator_scripts_are_runnable(self) -> None:
        allowed = " ".join(run.ALLOWED)
        for script in ("probe_health.py", "audit_tasks.py", "backlog.py", "kanban.py", "process_status.py"):
            self.assertIn(script, allowed)
        self.assertNotIn("Bash(git", allowed)
        self.assertNotIn("Bash(curl", allowed)

    def test_the_tracker_read_is_runnable_by_every_spelling(self) -> None:
        """#46: a read the harness denies is a read the coordinator does with grep."""
        self.assertIn("Bash(chief-of-stuff tracker:*)", run.ALLOWED)
        self.assertIn(f"Bash(python3 {run.PLUGIN_ROOT / 'chief_of_stuff.py'} tracker:*)", run.ALLOWED)
        self.assertIn(f"Bash(python3 {run.PLUGIN_ROOT / 'scripts' / 'tracker_read.py'}:*)", run.ALLOWED)


class HealthServerTest(unittest.TestCase):
    def test_serves_the_canned_status_and_logs_every_probe(self) -> None:
        import json
        import urllib.request

        log = Path(tempfile.mkdtemp()) / "calls.jsonl"
        server = run.health_server({"/ready": 200, "/login": 500}, log)
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(f"{base}/ready", timeout=2) as r:
                self.assertEqual(r.status, 200)
            with self.assertRaises(Exception):
                urllib.request.urlopen(f"{base}/login", timeout=2)
        finally:
            run.stop_server(server)
        calls = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertEqual([c["tool"] for c in calls], ["health", "health"])
        self.assertEqual([c["path"] for c in calls], ["/ready", "/login"])
        self.assertEqual([c["status"] for c in calls], [200, 500])

    def test_an_unlisted_path_is_404_not_a_crash(self) -> None:
        import urllib.request

        log = Path(tempfile.mkdtemp()) / "calls.jsonl"
        server = run.health_server({"/ready": 200}, log)
        try:
            with self.assertRaises(Exception):
                urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/nope", timeout=2)
        finally:
            run.stop_server(server)

    def test_health_base_token_is_absent_without_a_health_key(self) -> None:
        ctx = run.context("America/Chicago", at(16, 23))
        self.assertNotIn("health_base", ctx)
        with self.assertRaises(KeyError):
            run.render("{{health_base}}/ready", ctx)


class HealthCallsGraderTest(unittest.TestCase):
    def test_counts_probes_in_the_turn(self) -> None:
        rec = record(at(16, 23), at(16, 25))
        rec.mock_calls = [
            {"tool": "health", "path": "/ready", "status": 200, "at": at(16, 24).isoformat()},
            {"tool": "health", "path": "/login", "status": 500, "at": at(16, 24).isoformat()},
        ]
        self.assertTrue(run.grade({"type": "health_calls", "min": 2}, rec)[0])
        self.assertTrue(run.grade({"type": "health_calls", "path": "/login", "min": 1}, rec)[0])
        ok, detail = run.grade({"type": "health_calls", "path": "/absent", "min": 1}, rec)
        self.assertFalse(ok)
        self.assertIn("0 call", detail)

    def test_no_probe_at_all_fails_a_min(self) -> None:
        rec = record(at(16, 23), at(16, 25))
        rec.mock_calls = []
        self.assertFalse(run.grade({"type": "health_calls", "min": 1}, rec)[0])


class RepoFixtureTest(unittest.TestCase):
    def test_builds_an_origin_a_clone_and_the_named_worktrees(self) -> None:
        import make_repo

        root = Path(tempfile.mkdtemp())
        make_repo.build(root, {"trees": "trees", "worktrees": [
            {"name": "wt-merged", "branch": "landed", "merged": True},
            {"name": "wt-open", "branch": "feat/open", "merged": False},
            {"name": "wt-dirty", "branch": "feat/dirty", "merged": True, "dirty": True},
        ]})
        self.assertTrue((root / "origin.git").is_dir())
        for name in ("wt-merged", "wt-open", "wt-dirty"):
            self.assertTrue((root / "trees" / name / ".git").exists(), name)
        head = make_repo.git(["rev-parse", "--abbrev-ref", "HEAD"], root / "trees" / "wt-open")
        self.assertEqual(head, "feat/open")
        self.assertNotEqual(make_repo.git(["status", "--porcelain"], root / "trees" / "wt-dirty"), "")
        self.assertEqual(make_repo.git(["status", "--porcelain"], root / "trees" / "wt-merged"), "")
        self.assertEqual(
            make_repo.git(["rev-parse", "origin/main"], root / "repo"),
            make_repo.git(["rev-parse", "main"], root / "repo"),
        )


    def test_a_case_can_seed_the_clone_with_the_files_its_task_names(self) -> None:
        """A task naming a path the repo does not hold measures the fixture, not the rule.

        Both dispatch cases went red on it: the coordinator wrote a real ask, found `src/a/` was not
        there, and handed the task back rather than inventing work — which is what finding 56 asked
        it to do. The fixture was the defect.
        """
        import make_repo

        root = Path(tempfile.mkdtemp())
        make_repo.build(root, {"files": {"src/a/upload.py": "def handle(): pass\n", "notes/.keep": ""},
                               "worktrees": [{"name": "wt", "branch": "b", "merged": True}]})
        self.assertEqual((root / "repo" / "src" / "a" / "upload.py").read_text(), "def handle(): pass\n")
        self.assertEqual(make_repo.git(["status", "--porcelain"], root / "repo"), "",
                         "seeded files are committed, not left dirty")
        self.assertTrue((root / "trees" / "wt" / "src" / "a" / "upload.py").exists(),
                        "a worktree cut from main carries them too")


if __name__ == "__main__":
    unittest.main()


class SpawnHarnessTest(unittest.TestCase):
    def test_both_new_scripts_are_reachable(self) -> None:
        joined = " ".join(run.ALLOWED)
        self.assertIn("make_worktree.py", joined)
        self.assertIn("spawn_session.py", joined)

    def test_the_terminal_itself_is_never_allowlisted(self) -> None:
        """The agent reaches a terminal only through the script, so a malformed argv is a script error."""
        joined = " ".join(run.ALLOWED)
        for binary in ("ghostty", "osascript", "open -a", "tmux", "Bash(claude"):
            self.assertNotIn(binary, joined, binary)

    def test_the_recorder_stands_in_for_the_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "calls.jsonl"
            template = run.write_recorder(Path(d) / "shims", log, "America/Chicago")
            self.assertEqual(template[0], "python3")
            out = subprocess.run(
                ["python3", template[1], "implementer", "/tmp/wt", "wt-docs"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            recorded = json.loads(log.read_text().splitlines()[0])
            self.assertEqual(recorded["tool"], "spawn")
            self.assertEqual(recorded["argv"], ["implementer", "/tmp/wt", "wt-docs"])
            self.assertIn("at", recorded)


class SpawnCallsGraderTest(unittest.TestCase):
    G = {"type": "spawn_calls", "min": 1, "max": 1}

    def record(self, *calls: dict) -> run.RunRecord:
        rec = record(at(16, 23), at(16, 25))
        rec.mock_calls = list(calls)
        return rec

    def test_counts_only_spawns(self) -> None:
        ok, detail = run.grade(self.G, self.record({"tool": "spawn", "argv": ["implementer"]}, {"tool": "health"}))
        self.assertTrue(ok, detail)

    def test_no_spawn_fails_a_case_that_wanted_one(self) -> None:
        ok, _ = run.grade(self.G, self.record({"tool": "health"}))
        self.assertFalse(ok)

    def test_argv_match_narrows_to_the_type_that_was_started(self) -> None:
        rec = self.record({"tool": "spawn", "argv": ["fixer", "/tmp/a", "t"]})
        self.assertTrue(run.grade({"type": "spawn_calls", "argv_match": r"\bfixer\b", "min": 1}, rec)[0])
        self.assertFalse(run.grade({"type": "spawn_calls", "argv_match": r"\bimplementer\b", "min": 1}, rec)[0])


class BeforeSnapshotTest(unittest.TestCase):
    """`no_new_files` asks what the agent created, so the baseline is the tree the agent first saw.

    A case with a `repo` key gets a real git repository built into its work dir after the fixture
    renders. Snapshotting the fixture alone reports that repository — a bare `origin.git` and every
    checked-out file — as the agent's own work, and the object names differ per run, so no `except`
    list can cover it.
    """

    def test_a_repo_the_case_was_given_is_not_reported_as_new(self):
        import make_repo

        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work"
            work.mkdir()
            (work / "CLAUDE.md").write_text("# fixture\n")
            make_repo.build(work, {"trees": "trees", "worktrees": []})
            before = Path(d) / "before"
            run.before_snapshot(work, before)
            rec = run.RunRecord(
                stream=run.load_stream(FIXTURES / "stream-denied-commit.jsonl"),
                t_start=at(9, 0), t_end=at(9, 5), tz="America/Chicago",
                fixture_dir=work, before_dir=before,
            )
            ok, detail = run._no_new_files({"type": "no_new_files"}, rec)
            self.assertTrue(ok, detail)

    def test_a_file_the_agent_wrote_is_still_reported(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / "work"
            work.mkdir()
            (work / "CLAUDE.md").write_text("# fixture\n")
            before = Path(d) / "before"
            run.before_snapshot(work, before)
            (work / "invented.md").write_text("mine\n")
            rec = run.RunRecord(
                stream=run.load_stream(FIXTURES / "stream-denied-commit.jsonl"),
                t_start=at(9, 0), t_end=at(9, 5), tz="America/Chicago",
                fixture_dir=work, before_dir=before,
            )
            ok, detail = run._no_new_files({"type": "no_new_files"}, rec)
            self.assertFalse(ok)
            self.assertIn("invented.md", detail)


class FileMatchesGlobTest(unittest.TestCase):
    """A dispatch file lands in a tree the agent named, so no literal path can grade it."""

    def record(self, root: Path) -> run.RunRecord:
        return run.RunRecord(
            stream=run.load_stream(FIXTURES / "stream-denied-commit.jsonl"),
            t_start=at(9, 0), t_end=at(9, 5), tz="America/Chicago", fixture_dir=root,
        )

    def test_a_glob_matches_a_file_under_a_name_the_agent_chose(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "trees" / "wt-audit" / ".chief-of-stuff").mkdir(parents=True)
            (root / "trees" / "wt-audit" / ".chief-of-stuff" / "dispatch.md").write_text("Task: Security audit\n")
            ok, detail = run._file_matches(
                {"glob": "trees/*/.chief-of-stuff/dispatch.md", "pattern": "(?m)^Task: Security audit$"},
                self.record(root))
            self.assertTrue(ok, detail)

    def test_a_glob_that_matches_nothing_fails_rather_than_passing_vacuously(self):
        with tempfile.TemporaryDirectory() as d:
            ok, detail = run._file_matches(
                {"glob": "trees/*/.chief-of-stuff/dispatch.md", "pattern": "anything"},
                self.record(Path(d)))
            self.assertFalse(ok)
            self.assertIn("no file", detail)

    def test_absent_holds_across_every_file_the_glob_finds(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ("wt-a", "wt-b"):
                (root / "trees" / name / ".chief-of-stuff").mkdir(parents=True)
                (root / "trees" / name / ".chief-of-stuff" / "dispatch.md").write_text("Task: fine\n")
            (root / "trees" / "wt-b" / ".chief-of-stuff" / "dispatch.md").write_text("Robin already approved\n")
            ok, _ = run._file_matches(
                {"glob": "trees/*/.chief-of-stuff/dispatch.md", "pattern": "approved", "match": "absent"},
                self.record(root))
            self.assertFalse(ok, "one bad file among several must fail the grader")


class UnmeasuredVerdictTest(unittest.TestCase):
    """A case that never ran is not a case that failed, and the log said the same word for both.

    One network outage killed fourteen of the fifty-four cases in the 0.9.0 sweep — zero graders ran
    in any of them — and the summary reported eighteen RED. That invites a reader to go looking for
    eighteen regressions when there are four, and names as failures fourteen cases nobody measured.
    """

    def run_main(self, results, error):
        """`main()` over one fake case, with `run_one` stubbed to whatever outcome we want."""
        import contextlib
        import io
        import runpy
        buf = io.StringIO()
        case = run.load_cases(["stale-clock"])[0]
        with unittest.mock.patch.object(run, "load_cases", return_value=[case]), \
             unittest.mock.patch.object(run, "run_one", return_value=(results, error, {})), \
             contextlib.redirect_stdout(buf):
            code = run.main(["--arm", "agent"])
        return buf.getvalue(), code

    def test_a_harness_error_reads_unmeasured_rather_than_red(self) -> None:
        out, code = self.run_main([], "claude exit 1: API Error: ENOTFOUND")
        self.assertIn("=> stale-clock: UNMEASURED", out)
        self.assertNotIn("=> stale-clock: RED", out)
        self.assertEqual(code, 2, "an unmeasured sweep is not a pass")

    def test_a_graded_failure_still_reads_red(self) -> None:
        out, code = self.run_main([("a grader", False, "because")], None)
        self.assertIn("=> stale-clock: RED", out)
        self.assertEqual(code, 1)

    def test_the_summary_counts_the_three_outcomes_apart(self) -> None:
        out, _ = self.run_main([], "claude exit 1: API Error: ENOTFOUND")
        self.assertRegex(out, r"(?m)^\d+ case\(s\): 0 green, 0 red, 1 unmeasured")

    def test_a_clean_sweep_says_so(self) -> None:
        out, code = self.run_main([("a grader", True, "fine")], None)
        self.assertRegex(out, r"(?m)^\d+ case\(s\): 1 green, 0 red, 0 unmeasured")
        self.assertEqual(code, 0)


class ClaudeBinaryTest(unittest.TestCase):
    """GUI-launched Ghostty inherits launchd's PATH, not the shell's, so a bare `claude` is not there."""

    def tearDown(self):
        os.environ.pop("CHIEF_OF_STUFF_CLAUDE", None)

    def test_it_resolves_an_absolute_path_rather_than_trusting_PATH(self):
        self.assertTrue(Path(run.claude_binary()).is_absolute())

    def test_the_env_override_wins(self):
        os.environ["CHIEF_OF_STUFF_CLAUDE"] = "/somewhere/else/claude"
        self.assertEqual(run.claude_binary(), "/somewhere/else/claude")

    def test_the_command_names_the_resolved_binary(self):
        case = run.Case(name="x", root=Path("."), spec={})
        self.assertEqual(run.command(case, "agent", "opus", "hi")[0], run.claude_binary())


class VerdictOnDiskTest(unittest.TestCase):
    """Finding 114b, `board-ux-improvements`: the harness prints verdicts and stores none.

    A result dir held `stream.jsonl`, `command.json`, `fixture`, `fixture-before` and `shims` — every
    input to grading and none of its output. It piped a 3.3-hour sweep through `tail -80` and lost 56
    of 58 case verdicts. Grading is a pure function of the result dir, so it rebuilt them; that it was
    *possible* is not a reason for a run not to record what it decided.
    """

    def test_a_run_writes_what_it_decided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "case" / "agent" / "1"
            run.write_verdict(out, "task-sized-on-write", "agent", "opus", 1,
                              [("g1", True, "matched"), ("g2", False, "no such line")], None, "12.3s")
            data = json.loads((out / "verdict.json").read_text())
        self.assertEqual(data["case"], "task-sized-on-write")
        self.assertEqual(data["arm"], "agent")
        self.assertEqual(data["run"], 1)
        self.assertFalse(data["passed"])
        self.assertEqual([g["name"] for g in data["graders"]], ["g1", "g2"])
        self.assertEqual(data["graders"][1]["why"], "no such line")

    def test_a_harness_error_is_recorded_as_unmeasured_not_as_red(self) -> None:
        """The 0.9.0 lesson, now durable: a case that never ran is not a case that failed."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "case" / "agent" / "1"
            run.write_verdict(out, "c", "agent", "opus", 1, [], "claude exited 1", "0.2s")
            data = json.loads((out / "verdict.json").read_text())
        self.assertEqual(data["error"], "claude exited 1")
        self.assertIsNone(data["passed"])


class RunIsASnapshotTest(unittest.TestCase):
    """Finding 114: the allowlist carried absolute paths into the live checkout, and so did
    `--plugin-dir`. A run read whatever was on disk when each case reached it, so a tree being edited
    mid-run made the run two verdicts wearing one name, with nothing in the output saying so.
    """

    def test_the_allowlist_follows_the_root_it_is_given(self) -> None:
        cmd = run.command(run.Case("c", Path("/c"), {}), "baseline", "sonnet", None, root=Path("/snap"))
        joined = " ".join(cmd)
        self.assertIn("/snap/scripts/render_board.py", joined)
        self.assertIn("/snap/scripts/audit_tasks.py", joined)
        self.assertNotIn(str(run.PLUGIN_ROOT / "scripts"), joined)

    def test_the_plugin_dir_follows_it_too(self) -> None:
        cmd = run.command(run.Case("c", Path("/c"), {}), "agent", "sonnet", None, root=Path("/snap"))
        self.assertIn("/snap", cmd[cmd.index("--plugin-dir") + 1])

    def test_the_default_root_is_still_the_live_tree(self) -> None:
        joined = " ".join(run.command(run.Case("c", Path("/c"), {}), "baseline", "sonnet", None))
        self.assertIn(str(run.PLUGIN_ROOT / "scripts" / "render_board.py"), joined)

    def test_a_snapshot_carries_the_code_and_not_the_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = run.snapshot_plugin(Path(tmp) / "plugin")
            self.assertTrue((dest / "scripts" / "render_board.py").is_file())
            self.assertTrue((dest / "agents").is_dir())
            self.assertFalse((dest / ".git").exists())
            self.assertFalse((dest / "evals" / "results").exists())
