"""Unit tests for scripts/settings.py: the workspace's chief-of-stuff.toml, named by the Coordinator block.

Zach, 2026-09-22 22:20, on the day open and close times: "Configurable. We're going to need to pull
settings into a config file. Default to 6 and 22". A value that cannot be read is refused, never
guessed at.
"""

import json
import sys
import tempfile
import os
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import settings as st  # noqa: E402

FULL = """[notify]
adapter = "md-notify"
queue = "notes/queue.md"
day_open = "07:30"
day_close = "21:00"
deadline_warnings = ["24h", "90m"]
"""


class SettingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write(self, text: str, name: str = "chief-of-stuff.toml") -> str:
        (self.root / name).write_text(text)
        return name

    def test_no_settings_line_is_off(self):
        self.assertEqual(st.load(self.root, None).notify.adapter, "off")

    def test_stage_profiles_and_skills_are_optional(self):
        text = ('[lanes.code]\nstages = ["implement", "review", "pr"]\n'
                '[stages.review]\nagent = "reviewer"\nskills = ["security-review", "extras:code-review"]\n'
                '[stages.implement]\nagent = "builder"\n[stages.pr]\nskills = ["delivery"]\n')
        for notify in ("", '[notify]\nadapter = "off"\n'):
            with self.subTest(notify=notify):
                got = st.load(self.root, self.write(text + notify)).stages
                self.assertEqual(got["review"].agent, "reviewer")
                self.assertEqual(got["review"].skills, ("security-review", "extras:code-review"))
                self.assertEqual(got["implement"].skills, ())
                self.assertIsNone(got["pr"].agent)
        self.assertEqual(st.load(self.root, None).stages, {})

    def test_stages_accept_kanban_map_and_terminal_names(self):
        text = ('[kanban]\nstages = ["Review"]\nhuman_review_label = "Held"\n'
                'terminal_stages = ["done"]\n[kanban.map]\nreview = 0\n'
                '[stages.review]\nagent = "reviewer"\n[stages.done]\nskills = ["summary"]\n')
        self.assertEqual(set(st.load(self.root, self.write(text)).stages), {"review", "done"})

    def test_invalid_stage_configuration_is_refused(self):
        prefix = '[lanes.code]\nstages = ["review"]\n'
        for text in ('[stages]\nreview = "reviewer"',
                     '[stages.missing]\nagent = "reviewer"',
                     '[stages.review]\nagent = 3', '[stages.review]\nagent = ""',
                     '[stages.review]\nagent = "--reviewer"',
                     '[stages.review]\nskills = "security-review"',
                     '[stages.review]\nskills = [3]', '[stages.review]\nskills = [""]',
                     '[stages.review]\nskills = ["security\\nreview"]',
                     '[stages.review]\nskills = ["security", "security"]',
                     '[stages.review]\nunknown = "ignored"'):
            with self.subTest(text=text), self.assertRaises(st.SettingsError):
                st.load(self.root, self.write(prefix + text))
        with self.assertRaises(st.SettingsError):
            st.load(self.root, self.write('stages = []\n' + prefix))

    def test_a_named_file_that_is_absent_is_off(self):
        self.assertEqual(st.load(self.root, "chief-of-stuff.toml").notify.adapter, "off")

    def test_a_directory_at_the_settings_path_is_refused(self):
        """#347: a Settings path that is a directory is a refusal, never silent defaults."""
        (self.root / "chief-of-stuff.toml").mkdir()
        with self.assertRaises(st.SettingsError) as raised:
            st.load(self.root, "chief-of-stuff.toml")
        self.assertIn("not a file", str(raised.exception))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a mode-0 file")
    def test_an_unreadable_settings_file_is_refused(self):
        """#285: a settings file the loader cannot read is an error, not defaults."""
        path = self.root / "chief-of-stuff.toml"
        path.write_text("[notify]\n")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        with self.assertRaises(st.SettingsError):
            st.load(self.root, "chief-of-stuff.toml")

    def test_no_notify_section_is_off(self):
        self.assertEqual(st.load(self.root, self.write("[other]\nx = 1\n")).notify.adapter, "off")

    def test_defaults_per_missing_key(self):
        got = st.load(self.root, self.write("[notify]\n")).notify
        self.assertEqual((got.adapter, got.queue, got.day_open, got.day_close),
                         ("off", "notifications/NOTIFICATIONS.md", "06:00", "22:00"))
        self.assertEqual(got.warnings, (("24h", timedelta(hours=24)), ("3h", timedelta(hours=3)), ("1h", timedelta(hours=1))))

    def test_values_are_read(self):
        got = st.load(self.root, self.write(FULL)).notify
        self.assertEqual((got.queue, got.day_open, got.day_close), ("notes/queue.md", "07:30", "21:00"))
        self.assertEqual(got.warnings, (("24h", timedelta(hours=24)), ("90m", timedelta(minutes=90))))
        self.assertEqual(got.queue_path(self.root), (self.root / "notes/queue.md").resolve())

    def test_off_is_an_adapter(self):
        self.assertEqual(st.load(self.root, self.write('[notify]\nadapter = "off"\n')).notify.adapter, "off")

    def test_bad_values_are_refused(self):
        for line in ('adapter = "slack"', 'day_open = "25:00"', 'day_close = "6pm"', 'deadline_warnings = ["3x"]',
                     'deadline_warnings = "3h"', 'queue = "../outside.md"', 'queue = "/tmp/outside.md"'):
            with self.subTest(line=line):
                with self.assertRaises(st.SettingsError):
                    st.load(self.root, self.write(f"[notify]\n{line}\n"))

    def test_toml_that_does_not_parse_is_refused(self):
        with self.assertRaises(st.SettingsError):
            st.load(self.root, self.write("[notify\n"))

    def test_a_file_that_is_not_utf8_is_a_settings_error_not_a_decode_error(self):
        # Every caller catches SettingsError; a UnicodeDecodeError (a ValueError) got past them as a traceback.
        (self.root / "chief-of-stuff.toml").write_bytes(b"\xff\xfe[workers]\n")
        with self.assertRaises(st.SettingsError):
            st.load(self.root, "chief-of-stuff.toml")

    def test_architecture_reviewer_is_optional_and_validated(self):
        self.assertIsNone(st.load(self.root, None).workflow.architecture_reviewer)
        settings = st.load(self.root, self.write('[workflow]\narchitecture_reviewer = "architecture-01"\n'))
        self.assertEqual(settings.workflow.architecture_reviewer, "architecture-01")
        with self.assertRaises(st.SettingsError):
            st.load(self.root, self.write('[workflow]\narchitecture_reviewer = "a b"\n'))

    def test_reviewer_session_is_optional_and_validated(self):
        self.assertIsNone(st.load(self.root, None).workflow.reviewer_session)
        settings = st.load(self.root, self.write('[workflow]\nreviewer_session = "reviewer-01"\n'))
        self.assertEqual(settings.workflow.reviewer_session, "reviewer-01")
        with self.assertRaises(st.SettingsError):
            st.load(self.root, self.write('[workflow]\nreviewer_session = "a b"\n'))

    def test_code_delivery_and_merge_owner(self):
        self.assertEqual((st.load(self.root, None).workflow.delivery,
                          st.load(self.root, None).workflow.merge_owner), ("pull-request", "user"))
        workflow = st.load(self.root, self.write('[workflow]\ndelivery = "branch"\n')).workflow
        self.assertEqual(workflow.delivery, "branch")
        workflow = st.load(self.root, self.write('[workflow]\nmerge_owner = "worker"\n')).workflow
        self.assertEqual(workflow.merge_owner, "worker")
        for text in ('delivery = "direct"', 'merge_owner = "bot"',
                     'delivery = "branch"\nmerge_owner = "worker"'):
            with self.subTest(text=text), self.assertRaises(st.SettingsError):
                st.load(self.root, self.write('[workflow]\n' + text + '\n'))

    def test_worker_cap_is_absent_or_a_positive_whole_number(self):
        self.assertIsNone(st.load(self.root, None).workers.max_concurrency)
        self.assertEqual(st.load(self.root, self.write('[workers]\nmax_concurrency = 3\n')).workers.max_concurrency, 3)
        for bad in ("0", "-1", "2.5", '"3"', "true"):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                st.load(self.root, self.write(f"[workers]\nmax_concurrency = {bad}\n"))

    def test_green_test_requirement_is_opt_in_and_strictly_boolean(self):
        self.assertFalse(st.load(self.root, None).workers.require_green_tests)
        for value, expected in (("true", True), ("false", False)):
            self.assertEqual(st.load(self.root, self.write(
                f"[workers]\nrequire_green_tests = {value}\n")).workers.require_green_tests, expected)
        for bad in ('"true"', "1", "0", "[]"):
            with self.subTest(bad=bad), self.assertRaisesRegex(st.SettingsError, "require_green_tests"):
                st.load(self.root, self.write(f"[workers]\nrequire_green_tests = {bad}\n"))

    def test_worker_rules_default_to_empty(self):
        self.assertEqual(st.load(self.root, None).workers.rules, ())
        for text in ("", "[workers]\n", "[workers]\nrules = []\n"):
            with self.subTest(text=text):
                self.assertEqual(st.load(self.root, self.write(text)).workers.rules, ())

    def test_worker_rules_preserve_wording_and_order(self):
        for prefix in ("", '[notify]\nadapter = "off"\n'):
            with self.subTest(prefix=prefix):
                got = st.load(self.root, self.write(prefix + '[workers]\nrules = ["Keep tests.", "  Verify changes.  "]\n'))
                self.assertEqual(got.workers.rules, ("Keep tests.", "  Verify changes.  "))

    def test_invalid_worker_rules_are_refused(self):
        for value in ('"Keep tests."', "true", "17", "{}", '["Keep tests.", 1]', '[""]', '["  "]'):
            with self.subTest(value=value), self.assertRaisesRegex(st.SettingsError, r"\[workers\] rules"):
                st.load(self.root, self.write(f"[workers]\nrules = {value}\n"))

    def test_worker_rules_reject_line_breaks_and_control_characters(self):
        for escape in (r"\n", r"\r", r"\t", r"\u0000", r"\u001b", r"\u007f", r"\u0085", r"\u2028"):
            for suffix in ("Injected", ""):
                with self.subTest(escape=escape, suffix=suffix), self.assertRaisesRegex(st.SettingsError, r"\[workers\] rules"):
                    st.load(self.root, self.write('[workers]\nrules = ["Keep tests.' + escape + suffix + '"]\n'))

    def test_worker_rules_reject_bidirectional_formatting_controls(self):
        for escape in (r"\u061c", r"\u200e", r"\u200f", r"\u202a", r"\u202b", r"\u202c", r"\u202d", r"\u202e",
                       r"\u2066", r"\u2067", r"\u2068", r"\u2069"):
            with self.subTest(escape=escape), self.assertRaisesRegex(st.SettingsError, r"\[workers\] rules"):
                st.load(self.root, self.write('[workers]\nrules = ["approve abc' + escape + 'txt"]\n'))

    def test_worker_rules_reject_invisible_unicode_format_characters(self):
        for escape in (r"\u200b", r"\ufeff", r"\U000e0001", r"\U000e0061", r"\U000e007f"):
            with self.subTest(escape=escape), self.assertRaisesRegex(st.SettingsError, r"\[workers\] rules"):
                st.load(self.root, self.write('[workers]\nrules = ["approve abc' + escape + 'txt"]\n'))

    def test_worker_rules_total_length_is_capped_at_load(self):
        # The raw sum of len(rule) is the settings boundary; the rendered budget belongs to dispatch_prompt.
        def load(rules):
            return st.load(self.root, self.write("[workers]\nrules = " + json.dumps(rules) + "\n"))
        self.assertEqual(load(["x" * 2000] * 3).workers.rules, ("x" * 2000,) * 3)
        with self.assertRaisesRegex(st.SettingsError, r"\[workers\] rules.*6000"):
            load(["x" * 2000] * 3 + ["x"])

    def test_worker_rules_count_is_capped_at_load(self):
        def load(count):
            return st.load(self.root, self.write("[workers]\nrules = " + json.dumps(["x"] * count) + "\n"))
        self.assertEqual(len(load(st.RULES_MAX).workers.rules), st.RULES_MAX)
        with self.assertRaisesRegex(st.SettingsError, r"\[workers\] rules.*at most"):
            load(st.RULES_MAX + 1)

    def test_settings_does_not_render_assignments(self):
        self.assertFalse(hasattr(st, "standing_rules"))

    def test_worker_rules_reject_peer_markers_and_excessive_length_at_load(self):
        for prefix in ("", '[notify]\nadapter = "off"\n'):
            for rule in ("Keep tests. (via peer)", "x" * 2401):
                with self.subTest(prefix=prefix, rule=rule[:30]), self.assertRaisesRegex(st.SettingsError, r"\[workers\] rules"):
                    st.load(self.root, self.write(prefix + '[workers]\nrules = ["' + rule + '"]\n'))
            rule = "x" * 2400
            got = st.load(self.root, self.write(prefix + '[workers]\nrules = ["' + rule + '"]\n'))
            self.assertEqual(got.workers.rules, (rule,))

    def test_claude_as_profile_is_absent_or_a_claude_as_profile_name(self):
        # Zach, 2026-10-08 20:50: Claude subagents "need to use "cl secondary" (aka claude-as)".
        self.assertEqual(st.load(self.root, None).workers.claude_as_profile, "")
        got = st.load(self.root, self.write('[workers]\nclaude_as_profile = "secondary"\n')).workers
        self.assertEqual(got.claude_as_profile, "secondary")
        for bad in ('""', '"-x"', '".x"', '"a/b"', '"a b"', "3"):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                st.load(self.root, self.write(f"[workers]\nclaude_as_profile = {bad}\n"))

    def test_the_old_claude_profile_key_is_refused_by_its_new_name(self):
        # Ledger E12: renamed to claude_as_profile. Ignored, the old key would launch a bare `claude` without a word.
        with self.assertRaisesRegex(st.SettingsError, "claude_as_profile"):
            st.load(self.root, self.write('[workers]\nclaude_profile = "secondary"\n'))

    def test_pages_workers_defaults_to_four_and_is_a_positive_whole_number(self):
        # The user, 2026-09-26: "allow the board server to have up to 4 workers", then "well, N workers. 4 default".
        # Rule: "[pages] workers that is not a positive whole number raises SettingsError naming [pages] workers".
        self.assertEqual(st.load(self.root, None).pages.workers, 4)
        self.assertEqual(st.load(self.root, self.write("[pages]\n")).pages.workers, 4)
        self.assertEqual(st.load(self.root, self.write("[pages]\nworkers = 2\n")).pages.workers, 2)
        for bad in ("0", "-1", "2.5", '"3"', "true"):
            with self.subTest(bad=bad), self.assertRaisesRegex(st.SettingsError, r"\[pages\] workers"):
                st.load(self.root, self.write(f"[pages]\nworkers = {bad}\n"))

    def test_worker_launcher_defaults_to_ghostty_and_accepts_tmux(self):
        self.assertEqual(st.load(self.root, None).workers.launcher, "ghostty")
        self.assertEqual(st.load(self.root, None).workers.mode, "interactive")
        self.assertEqual(st.load(self.root, self.write('[workers]\nlauncher = "tmux"\n')).workers.launcher, "tmux")
        with self.assertRaises(st.SettingsError):
            st.load(self.root, self.write('[workers]\nlauncher = "shell"\n'))

    def test_worker_launcher_accepts_herdr(self):
        # #471: interactive workers use a dedicated named herdr session, separate from the user's.
        for prefix in ("", '[notify]\nadapter = "off"\n'):
            with self.subTest(notify=bool(prefix)):
                got = st.load(self.root, self.write(prefix + '[workers]\nlauncher = "herdr"\n'))
                self.assertEqual(got.workers.launcher, "herdr")
                self.assertEqual(got.workers.mode, "interactive")

    def test_unknown_worker_launcher_is_refused_naming_all_three_choices(self):
        for value in ('"shell"', '"HERDR"', '""', "true", "17"):
            with self.subTest(value=value):
                with self.assertRaises(st.SettingsError) as refused:
                    st.load(self.root, self.write(f'[workers]\nlauncher = {value}\n'))
                message = str(refused.exception)
                self.assertIn("[workers] launcher", message)
                for choice in ("ghostty", "tmux", "herdr"):
                    self.assertIn(choice, message)

    def test_worker_herdr_session_defaults_to_chief_of_stuff(self):
        self.assertEqual(st.load(self.root, None).workers.herdr_session, "chief-of-stuff")
        self.assertEqual(st.load(self.root, "absent.toml").workers.herdr_session, "chief-of-stuff")
        for text in ("", '[notify]\nadapter = "off"\n', '[workers]\n',
                     '[workers]\nlauncher = "tmux"\n', '[workers]\nlauncher = "herdr"\n'):
            with self.subTest(text=text):
                got = st.load(self.root, self.write(text)).workers
                self.assertEqual(got.herdr_session, "chief-of-stuff")

    def test_worker_herdr_session_accepts_valid_names_verbatim(self):
        for name in ("chief-of-stuff", "0", "a", "review_workers-471", "a" * 32):
            # Both settings load paths preserve the session prefix for tab create, pane run and close.
            for prefix in ("", '[notify]\nadapter = "off"\n'):
                for launcher in ("ghostty", "herdr"):
                    with self.subTest(name=name, notify=bool(prefix), launcher=launcher):
                        text = prefix + f'[workers]\nlauncher = "{launcher}"\nherdr_session = "{name}"\n'
                        got = st.load(self.root, self.write(text)).workers
                        self.assertEqual(got.herdr_session, name)
                        self.assertEqual(got.launcher, launcher)
                        self.assertEqual(got.mode, "interactive")

    def test_worker_herdr_session_refuses_invalid_names_and_non_strings(self):
        # No lowercasing, trimming, or title sanitization for the configured session name.
        for value in ('""', '"Chief-of-stuff"', '"two words"', '" workers"', '"workers "',
                      '"-workers"', '"_workers"', '"workers.main"', '"../workers"',
                      '"' + 'a' * 33 + '"', '"workers;touch marker"', '"$(whoami)"',
                      '"workers\\n"', '"workers\\t"', '"w\\u00e9"',
                      "17", "true", "1.5", '["workers"]', '{name = "workers"}'):
            with self.subTest(value=value), self.assertRaisesRegex(st.SettingsError, r"\[workers\] herdr_session"):
                st.load(self.root, self.write(f'[workers]\nherdr_session = {value}\n'))

    def test_worker_mode_accepts_one_shot_and_refuses_unknown_values(self):
        self.assertEqual(st.load(self.root, self.write('[workers]\nmode = "one-shot"\n')).workers.mode,
                         "one-shot")
        for value in ('"background"', 'true', '17'):
            with self.subTest(value=value), self.assertRaises(st.SettingsError):
                st.load(self.root, self.write(f'[workers]\nmode = {value}\n'))

    def test_repos_is_empty_or_the_name_to_path_table_the_file_gives(self):
        # #54: the repositories `chief-of-stuff worktree` cuts from, by name; a relative path is relative to the workspace root.
        self.assertEqual(st.load(self.root, None).repos, {})
        self.assertEqual(st.load(self.root, self.write('[workers]\nmode = "one-shot"\n')).repos, {})
        self.assertEqual(st.load(self.root, self.write("[repos]\n")).repos, {})
        settings = st.load(self.root, self.write('[repos]\nalder = "alder-checkout"\nbirch = "/abs/birch"\n'))
        self.assertEqual(settings.repos, {"alder": "alder-checkout", "birch": "/abs/birch"})

    def test_repos_loads_beside_a_notify_table(self):
        # `load` builds its result in two places: a file with `[notify]` takes the second.
        settings = st.load(self.root, self.write('[notify]\nadapter = "md-notify"\n[repos]\nalder = "alder-checkout"\n'))
        self.assertEqual(settings.repos, {"alder": "alder-checkout"})

    def test_a_repos_value_that_is_blank_or_not_a_string_is_refused_naming_the_entry(self):
        for bad in ('""', '"  "', "3", "true", '["checkout"]', "{}"):
            with self.subTest(bad=bad), self.assertRaisesRegex(st.SettingsError, r"\[repos\] alder must be a path"):
                st.load(self.root, self.write(f"[repos]\nalder = {bad}\n"))

    def test_repos_that_is_not_a_table_is_refused(self):
        for bad in ("3", '"alder"', '["alder"]', "true", "0", '""', "false", "[]"):
            with self.subTest(bad=bad), self.assertRaisesRegex(st.SettingsError, r"\[repos\] must be a table"):
                st.load(self.root, self.write(f"repos = {bad}\n"))

    def test_a_wrong_value_in_a_file_that_parses_is_a_settings_error_naming_its_table_and_key(self):
        # #47, Codex on PR #283: `gates = [["a"]]` raised TypeError from `set()` and `health` printed a traceback.
        # Rule: "the loader answers every wrong-typed value in a settings file with SettingsError naming the
        # table and key; it never raises another exception type for parseable TOML".
        # A fuzz of twelve wrong types over every table, key and list entry found these, all in two functions.
        lane = '[lanes.x]\nstages = ["a"]\ngates = {}\n'
        found = {
            # settings._lane: a gate that cannot go in a set
            "gates, a nested list": (lane.format('[["a"]]'), r"\[lanes\] x: gates"),
            "gates, a list holding an empty list": (lane.format("[[]]"), r"\[lanes\] x: gates"),
            "gates, a list holding a list of numbers": (lane.format("[[1, 2]]"), r"\[lanes\] x: gates"),
            "gates, a list holding a table": (lane.format("[{ a = 1 }]"), r"\[lanes\] x: gates"),
            "gates, a list holding an empty table": (lane.format("[{}]"), r"\[lanes\] x: gates"),
            "gates, a stage and then a list": (lane.format('["a", ["a"]]'), r"\[lanes\] x: gates"),
            # settings._notify: a string, and still not a path the OS takes (ValueError from Path.resolve)
            "queue, a NUL in the path": ('[notify]\nqueue = "a\\u0000b"\n', r"\[notify\] queue"),
        }
        for what, (text, names) in found.items():
            with self.subTest(what), self.assertRaisesRegex(st.SettingsError, names):
                st.load(self.root, self.write(text))


if __name__ == "__main__":
    unittest.main()


LANES = """[lanes]
build = { stages = ["implement", "pr", "review", "triage", "merge"], gates = ["triage", "merge"] }
"""


class LanesTest(unittest.TestCase):
    """#33 (Zach, 2026-09-23): "A lane: the sequence that a task has to move through" — named, in the toml."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def load(self, text: str):
        (self.root / "s.toml").write_text(text)
        return st.load(self.root, "s.toml")

    def test_no_lanes_table_is_no_lanes(self):
        self.assertEqual(self.load("[notify]\n").lanes, {})

    def test_a_lane_is_its_stages_and_gates(self):
        lane = self.load(LANES).lanes["build"]
        self.assertEqual(lane.stages, ("implement", "pr", "review", "triage", "merge"))
        self.assertEqual(lane.gates, ("triage", "merge"))

    def test_lanes_do_not_need_notify(self):
        got = self.load(LANES)
        self.assertEqual(got.notify.adapter, "off")
        self.assertIn("build", got.lanes)

    def test_a_lane_that_cannot_be_read_is_refused(self):
        for bad in ('[lanes]\nx = 1\n',
                    '[lanes]\nx = { stages = [] }\n',
                    '[lanes]\nx = { stages = ["a", "a"] }\n',
                    '[lanes]\nx = { stages = ["a"], gates = ["b"] }\n',
                    '[lanes]\nx = { stages = ["a", 2] }\n'):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                self.load(bad)


class BudgetsTest(unittest.TestCase):
    """#33 stage 3: a running task's time budget by size; XL has none."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def load(self, text: str):
        (self.root / "s.toml").write_text(text)
        return st.load(self.root, "s.toml")

    def test_no_budgets_table_is_no_budgets(self):
        self.assertEqual(self.load("").budgets, {})

    def test_budgets_by_size(self):
        got = self.load('[budgets]\nS = "30m"\nM = "90m"\nL = "3h"\n').budgets
        self.assertEqual(got, {"S": timedelta(minutes=30), "M": timedelta(minutes=90), "L": timedelta(hours=3)})

    def test_a_budget_that_cannot_be_read_is_refused(self):
        for bad in ('budgets = 1\n', '[budgets]\nQ = "1h"\n', '[budgets]\nM = "soon"\n', '[budgets]\nM = 90\n'):
            with self.assertRaises(st.SettingsError, msg=bad):
                self.load(bad)


KANBAN = '''[kanban]
stages = ["00 - PLAN", "01 - PLAN REVIEW", "02 - TEST", "03 - BUILD", "04 - REVIEW", "05 - FIX"]
human_review_label = "!! - HUMAN REVIEW REQUIRED"
hold_stages = ["plan review", "triage", "merge"]
terminal_stages = ["main"]

[kanban.map]
plan = 0
"plan review" = 1
test = 2
implement = 3
review = 4
triage = 4
fix = 5
merge = 4
'''


class KanbanSettingsTest(unittest.TestCase):
    def test_terminal_stages_are_unique_safe_managed_labels(self):
        # Forges compare label names case-insensitively, so a case-only variant collides too.
        for names in ('["main", "main"]', '["!! - HUMAN REVIEW REQUIRED"]', '["main,done"]', '["00 - PLAN"]',
                      '["main", "Main"]', '["!! - human review required"]', '["00 - plan"]'):
            with self.subTest(names=names), self.assertRaisesRegex(st.SettingsError, "terminal_stages"):
                self.load(KANBAN.replace('terminal_stages = ["main"]', f'terminal_stages = {names}'))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def load(self, text: str):
        (self.root / "s.toml").write_text(text)
        return st.load(self.root, "s.toml")

    def test_absent_kanban_is_disabled(self):
        self.assertIsNone(self.load("").kanban)

    def test_unmapped_stages_lists_workflow_stages_missing_from_the_map(self):
        cfg = self.load(KANBAN).kanban
        policies = st.load_policy()
        self.assertEqual(st.unmapped_stages(cfg, policies), [])
        thin = st.Kanban(stages=("only",), human_review_label="hold", stage_map={"implement": 0},
                         terminal_stages=())
        self.assertIn("review", st.unmapped_stages(thin, policies))

    def test_config_maps_fine_stages_and_hold_overlay(self):
        cfg = self.load(KANBAN).kanban
        self.assertEqual(cfg.label_for("implement"), "03 - BUILD")
        self.assertEqual(cfg.label_for("fix"), "05 - FIX")
        self.assertTrue(cfg.holds("triage"))
        self.assertFalse(cfg.holds("review"))
        # Terminal stages leave a forge mark named like the stage so a rebuild can round-trip (#627).
        self.assertEqual(cfg.label_for("main"), "main")

    def test_stage_labels_round_trip_case_insensitively(self):
        cfg = self.load(KANBAN).kanban
        for label, stage in (("Main", "main"), ("03 - build", "implement")):
            with self.subTest(label=label):
                self.assertEqual(cfg.stage_for_label((label, "kind::code")), stage)

    def test_different_stage_labels_are_refused_regardless_of_case(self):
        cfg = self.load(KANBAN).kanban
        with self.assertRaisesRegex(ValueError, "more than one stage"):
            cfg.stage_for_label(("Main", "03 - build"))

    def test_invalid_mapping_is_refused(self):
        for bad in (
            KANBAN.replace('plan = 0', 'plan = 6'),
            KANBAN.replace('plan = 0', 'plan = true'),
            KANBAN.replace('"triage", "merge"', '"unknown", "merge"'),
            KANBAN.replace('"00 - PLAN", ', '"01 - PLAN REVIEW", '),
            KANBAN.replace('terminal_stages = ["main"]', 'terminal_stages = ["review"]'),
        ):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                self.load(bad)

    def test_padded_labels_are_refused_because_provisioning_and_sync_must_agree_on_the_name(self):
        for bad in (KANBAN.replace('"00 - PLAN"', '" 00 - PLAN"'),
                    KANBAN.replace('human_review_label = "', 'human_review_label = " '),
                    KANBAN.replace('terminal_stages = ["main"]', 'terminal_stages = ["main "]')):
            with self.subTest(bad=bad), self.assertRaisesRegex(st.SettingsError, "whitespace"):
                self.load(bad)

    def test_github_project_options_can_use_different_status_names(self):
        extra = '''\n[kanban.github_project]
owner = "example-org"
number = 7
field = "Status"
options = ["Plan", "Plan Review", "Test", "Build", "Review", "Fix"]
'''
        cfg = self.load(KANBAN + extra).kanban.github_project
        self.assertEqual((cfg.owner, cfg.number, cfg.field), ("example-org", 7, "Status"))
        self.assertEqual(cfg.options[4], "Review")

    def test_invalid_github_project_is_refused(self):
        for extra in ('owner = ""\nnumber = 7', 'owner = "org"\nnumber = 0',
                      'owner = "org"\nnumber = 7\noptions = ["one"]',
                      'owner = "org"\nnumber = 7\noptions = ["Plan", "Plan", "Test", "Build", "Review", "Fix"]'):
            with self.subTest(extra=extra), self.assertRaises(st.SettingsError):
                self.load(KANBAN + "\n[kanban.github_project]\n" + extra + "\n")

    def test_project_shorthand_owner_number_is_accepted(self):
        text = KANBAN.replace('terminal_stages = ["main"]',
                              'terminal_stages = ["main"]\nproject = "example-org/7"')
        cfg = self.load(text).kanban.github_project
        self.assertEqual((cfg.owner, cfg.number, cfg.field), ("example-org", 7, "Status"))
        self.assertEqual(cfg.options[0], "00 - PLAN")


MODELS = '''[models.deep]
rotation = ["claude:opus@high", "codex:gpt-test:preview@medium"]

[models.implement]
rotation = ["codex:gpt-test-coder@medium", "claude:sonnet", "claude:haiku@low"]
'''


class ModelsTest(unittest.TestCase):
    """#111: "I want, as part of the toml, the suggested models and model rotations to use." Each entry is
    `runtime:model-id@effort`; effort is part of the entry (Zach: "effort should be part of the model id")."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def load(self, text: str) -> st.Settings:
        (self.root / "s.toml").write_text(text)
        return st.load(self.root, "s.toml")

    def test_absent_models_is_empty(self):
        self.assertEqual(self.load('[workers]\nmode = "one-shot"\n').models, {})

    def test_rotation_is_read_in_order(self):
        got = self.load(MODELS).models
        self.assertEqual([str(e) for e in got["implement"]],
                         ["codex:gpt-test-coder@medium", "claude:sonnet", "claude:haiku@low"])
        first = got["deep"][1]
        # The runtime splits on the first colon and effort on the last @: the ID keeps its own colon.
        self.assertEqual((first.runtime, first.model, first.effort), ("codex", "gpt-test:preview", "medium"))
        self.assertIsNone(got["implement"][1].effort)

    def test_an_effort_on_a_runtime_that_takes_none_is_refused_at_load(self):
        # #52: refused here, naming the entry the user wrote; not at launch about a `--effort` flag never typed.
        with self.assertRaises(st.SettingsError) as raised:
            self.load('[models.deep]\nrotation = ["cursor:gpt-x@high", "claude:opus"]\n')
        for word in ("cursor:gpt-x@high", "cursor", "effort"):
            self.assertIn(word, str(raised.exception))

    def test_an_effort_loads_where_the_runtime_takes_one_and_none_loads_anywhere(self):
        for entry in ("cursor:gpt-x", "codex:gpt-x@high", "agy:gem-x@high"):
            with self.subTest(entry=entry):
                self.assertEqual([str(e) for e in self.load(f'[models.deep]\nrotation = ["{entry}"]\n').models["deep"]],
                                 [entry])

    def test_pick_is_first_by_default_random_on_request_and_nothing_else(self):
        # #877: "can we make it so they randomly select".
        got = self.load('[models.deep]\nrotation = ["claude:opus"]\npick = "random"\n'
                        '[models.review]\nrotation = ["claude:opus"]\n')
        self.assertEqual(got.model_picks, {"deep": "random", "review": "first"})
        for bad in ('[models.deep]\nrotation = ["claude:opus"]\npick = "best"\n',
                    '[models.deep]\npick = "random"\n'):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                self.load(bad)

    def test_an_entry_without_a_runtime_prefix_means_claude(self):
        # #701: 1.0 configs that wrote a bare Claude model keep working.
        rotation = self.load('[models.deep]\nrotation = ["opus@high", "codex:gpt-x"]\n').models["deep"]
        self.assertEqual([str(entry) for entry in rotation], ["claude:opus@high", "codex:gpt-x"])

    def test_bad_tables_are_refused(self):
        for bad in (
            '[models]\ndeep = "claude:opus"\n',
            '[models.deep]\n',
            '[models.deep]\nrotation = []\n',
            '[models.deep]\nrotation = "claude:opus"\n',
            '[models.deep]\nrotation = ["gemini:pro"]\n',
            '[models.deep]\nrotation = ["claude:"]\n',
            '[models.deep]\nrotation = ["claude:@high"]\n',
            '[models.deep]\nrotation = ["claude:opus@max"]\n',
            '[models.deep]\nrotation = ["claude:opus@"]\n',
            '[models.deep]\nrotation = ["claude:opus", "claude:opus"]\n',
            '[models.deep]\nrotation = [1]\n',
            '[models.deep]\nrotation = ["claude:opus"]\neffort = "high"\n',
        ):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                self.load(bad)

    def test_pick_suggests_the_first_entry(self):
        rotation = self.load(MODELS).models["implement"]
        self.assertEqual(str(st.pick(rotation)), "codex:gpt-test-coder@medium")

    def test_after_advances_the_rotation(self):
        rotation = self.load(MODELS).models["implement"]
        self.assertEqual(str(st.pick(rotation, after="codex:gpt-test-coder@medium")), "claude:sonnet")
        # The entry without its effort names the same entry.
        self.assertEqual(str(st.pick(rotation, after="codex:gpt-test-coder")), "claude:sonnet")
        with self.assertRaisesRegex(st.SettingsError, "rotation exhausted"):
            st.pick(rotation, after="claude:haiku@low")
        with self.assertRaisesRegex(st.SettingsError, "not in the rotation"):
            st.pick(rotation, after="claude:opus")

    def test_not_family_skips_that_runtime(self):
        rotation = self.load(MODELS).models["implement"]
        self.assertEqual(str(st.pick(rotation, not_family="codex")), "claude:sonnet")
        with self.assertRaisesRegex(st.SettingsError, "rotation exhausted"):
            st.pick(rotation, after="claude:sonnet", not_family="claude")


class GraphSettingsTest(unittest.TestCase):
    """The decision page's MODULE GRAPH: which clone branch-graph reads, its import root, depth and rules."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def load(self, text: str) -> st.Settings:
        (self.root / "s.toml").write_text(text)
        return st.load(self.root, "s.toml")

    def test_absent_graph_is_not_configured(self):
        self.assertIsNone(st.load(self.root, None).graph)
        self.assertIsNone(self.load('[workers]\nmode = "one-shot"\n').graph)

    def test_clone_and_root_with_the_default_depth_and_no_rules(self):
        got = self.load('[graph]\nclone = "repos/app"\nroot = "agent"\n').graph
        self.assertEqual((got.clone, got.root, got.depth, got.rules), ("repos/app", "agent", 2, None))

    def test_depth_and_rules_are_read(self):
        got = self.load('[graph]\nclone = "repos/app"\nroot = "agent"\ndepth = 3\nrules = "docs/architecture.md"\n').graph
        self.assertEqual((got.depth, got.rules), (3, "docs/architecture.md"))

    def test_a_graph_that_cannot_be_read_is_refused(self):
        base = 'clone = "repos/app"\nroot = "agent"\n'
        for bad in ('graph = 1\n',
                    '[graph]\nroot = "agent"\n',
                    '[graph]\nclone = "repos/app"\n',
                    '[graph]\nclone = ""\nroot = "agent"\n',
                    '[graph]\nclone = 3\nroot = "agent"\n',
                    '[graph]\nclone = "repos/app"\nroot = ""\n',
                    f'[graph]\n{base}depth = 0\n',
                    f'[graph]\n{base}depth = "2"\n',
                    f'[graph]\n{base}depth = 2.5\n',
                    f'[graph]\n{base}depth = true\n',
                    f'[graph]\n{base}rules = 5\n',
                    f'[graph]\n{base}rules = ""\n'):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                self.load(bad)

    # The user: a real MR drew dozens of tests.* modules and was unreadable; hide test modules and cap each view.
    def test_test_modules_are_excluded_and_views_hold_twelve_by_default(self):
        got = self.load('[graph]\nclone = "repos/app"\nroot = "agent"\n').graph
        self.assertEqual((tuple(got.exclude), got.max_nodes), (("tests.*", "evals.*"), 12))

    def test_exclude_and_max_nodes_are_read(self):
        got = self.load('[graph]\nclone = "repos/app"\nroot = "agent"\nexclude = ["legacy.*", "vendor.*"]\nmax_nodes = 3\n').graph
        self.assertEqual((tuple(got.exclude), got.max_nodes), (("legacy.*", "vendor.*"), 3))

    def test_an_empty_exclude_excludes_nothing(self):
        got = self.load('[graph]\nclone = "repos/app"\nroot = "agent"\nexclude = []\n').graph
        self.assertEqual(tuple(got.exclude), ())

    def test_a_bad_exclude_or_max_nodes_is_refused(self):
        base = '[graph]\nclone = "repos/app"\nroot = "agent"\n'
        for bad in ('exclude = "tests.*"\n',
                    'exclude = [1]\n',
                    'exclude = [""]\n',
                    'exclude = ["tests.*", 2]\n',
                    'max_nodes = 2\n',
                    'max_nodes = 0\n',
                    'max_nodes = -5\n',
                    'max_nodes = "12"\n',
                    'max_nodes = 12.5\n',
                    'max_nodes = true\n'):
            with self.subTest(bad=bad), self.assertRaises(st.SettingsError):
                self.load(base + bad)


class WorkflowStagesTest(unittest.TestCase):
    def test_shipped_policy_stages_are_sorted_and_complete(self):
        self.assertEqual(st.workflow_stages(st.load_policy()),
                         ["fix", "implement", "merge", "plan", "review"])
