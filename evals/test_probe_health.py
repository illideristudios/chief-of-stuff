"""Unit tests for scripts/probe_health.py: health facts come from a probe, never from the agent's memory of one.

A test that probes a target drives a real loopback server on port 0. No network, no mocked urllib:
the script's interesting code is the timeout, the status comparison and the unreachable host, and a
fake would skip all three. The parsing tests and the settings tests with no `Health:` line open no server.
"""

import contextlib
import io
import json
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import probe_health as ph  # noqa: E402
import runtimes  # noqa: E402
import test_shell_setup  # noqa: E402
from shell_setup import ShellError  # noqa: E402
import workspace  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    """Serves the status its server was told to serve, after an optional delay."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's name)
        status, delay = self.server.plan.get(self.path, (404, 0.0))
        if delay:
            time.sleep(delay)
        body = json.dumps({"path": self.path}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a) -> None:
        return


def serve(plan: dict[str, tuple[int, float]]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.plan = plan
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TargetParsingTest(unittest.TestCase):
    def test_parses_a_health_line_from_the_coordinator_block(self):
        text = (
            "# Workspace\n\n## Coordinator\n\n"
            "- Timezone: America/Chicago\n"
            "- Health: agent https://example.test/ready 200\n"
            "- Health: login https://example.test/login 200\n"
            "- Board: none\n\n"
            "## Other\n\n- Health: not-a-target https://example.test/x 200\n"
        )
        self.assertEqual(
            [(t.name, t.url, t.expect) for t in ph._targets(text)],
            [("agent", "https://example.test/ready", 200), ("login", "https://example.test/login", 200)],
        )

    def test_missing_block_is_no_targets_not_an_error(self):
        self.assertEqual(ph._targets("# Workspace\n\nNothing here.\n"), [])

    def test_expected_status_defaults_to_200(self):
        self.assertEqual(ph._targets("## Coordinator\n\n- Health: agent https://example.test/ready\n")[0].expect, 200)

    def test_refuses_a_non_http_scheme(self):
        with self.assertRaises(ph.TargetError):
            ph.parse_target("secrets file:///etc/passwd 200")


class ProbeTest(unittest.TestCase):
    def test_matching_status_is_ok(self):
        server, base = serve({"/ready": (200, 0.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        result = ph.probe(ph.Target("agent", f"{base}/ready", 200))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, 200)

    def test_wrong_status_is_not_ok_and_keeps_the_code(self):
        server, base = serve({"/login": (500, 0.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        result = ph.probe(ph.Target("login", f"{base}/login", 200))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 500)

    def test_a_404_is_a_status_not_an_exception(self):
        server, base = serve({})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        result = ph.probe(ph.Target("gone", f"{base}/nope", 200))
        self.assertEqual(result.status, 404)
        self.assertFalse(result.ok)

    def test_unreachable_host_reports_and_does_not_raise(self):
        result = ph.probe(ph.Target("dead", f"http://127.0.0.1:{free_port()}/ready", 200), timeout=1.0)
        self.assertFalse(result.ok)
        self.assertIsNone(result.status)
        self.assertTrue(result.detail)

    def test_a_slow_target_times_out_within_the_budget(self):
        server, base = serve({"/slow": (200, 2.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        started = time.monotonic()
        result = ph.probe(ph.Target("slow", f"{base}/slow", 200), timeout=0.3)
        self.assertLess(time.monotonic() - started, 1.5, "a hung probe must not stall the move")
        self.assertFalse(result.ok)
        self.assertIsNone(result.status)


class OutputTest(unittest.TestCase):
    def test_one_line_per_target_including_failures(self):
        server, base = serve({"/ready": (200, 0.0), "/login": (500, 0.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        targets = [
            ph.Target("agent", f"{base}/ready", 200),
            ph.Target("login", f"{base}/login", 200),
            ph.Target("dead", f"http://127.0.0.1:{free_port()}/x", 200),
        ]
        lines, bad = ph.report(targets, timeout=1.0)
        self.assertEqual(len(lines), 3)
        self.assertEqual(bad, 2)
        self.assertRegex(lines[0], r"^agent \S+/ready 200 ok$")
        self.assertIn("login 500", lines[1])
        self.assertIn("expected 200", lines[1])
        self.assertIn("dead", lines[2])

    def test_lines_carry_no_url_query_or_credentials(self):
        server, base = serve({"/ready": (200, 0.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        lines, _ = ph.report([ph.Target("agent", f"{base}/ready?token=secret", 200)], timeout=1.0)
        self.assertNotIn("secret", lines[0])

    def test_exit_code_is_the_number_of_failures(self):
        server, base = serve({"/ready": (200, 0.0), "/login": (500, 0.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "CLAUDE.md"
            p.write_text(
                "## Coordinator\n\n"
                f"- Health: agent {base}/ready 200\n"
                f"- Health: login {base}/login 200\n"
            )
            code = ph.main(["--config", str(p), "--timeout", "1"])
        self.assertEqual(code, 1)

    def test_no_targets_exits_zero_and_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "CLAUDE.md"
            p.write_text("## Coordinator\n\n- Board: none\n")
            code = ph.main(["--config", str(p)])
        self.assertEqual(code, 0)


TOML = "chief-of-stuff.toml"
BAD_MODE = '[workers]\nmode = "sometimes"\n'
GOOD = '[workers]\nmode = "one-shot"\n'
# The four lines `workspace.parse_coordinator` refuses a block without.
FULL = "- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n- Timezone: UTC\n"
# Ways a block can spell a `Settings:` line, and the file each one names (None: no settings line). The rule is
# `workspace._bullets` over `md.BULLET`: "The block's top-level `- key: value` bullets, a repeated key's last line
# winning", read by `settings_path` under the key `Settings` after `md.unquote`.
FORMS = {
    "one line": ("- Settings: `a.toml`\n", "a.toml"),
    # a repeated key's last line wins, whichever file that is
    "two lines": ("- Settings: `a.toml`\n- Settings: `b.toml`\n", "b.toml"),
    "two lines, the other order": ("- Settings: `b.toml`\n- Settings: `a.toml`\n", "a.toml"),
    # top-level only: an indented bullet belongs to the bullet above it
    "nested under another bullet": ("- Board: none\n  - Settings: `a.toml`\n", None),
    "a bullet indented by one space": (" - Settings: `a.toml`\n", None),
    # md.BULLET is a `-` bullet
    "a star bullet": ("* Settings: `a.toml`\n", None),
    "no bullet": ("Settings: `a.toml`\n", None),
    # the key is unquoted: backticks and the spaces around it go
    "a backticked label": ("- `Settings`: `a.toml`\n", "a.toml"),
    "a space before the colon": ("- Settings : a.toml\n", "a.toml"),
    # the key is `Settings` exactly, as main's parse_coordinator reads it (`top.get("Settings", "")`)
    "a lower-case label": ("- settings: a.toml\n", None),
    "a longer key": ("- Settings file: `a.toml`\n", None),
    # workspace._first_path: "its first backtick span, else its first word"
    "prose before the span": ("- Settings: see `a.toml`\n", "a.toml"),
    "two spans": ("- Settings: `a.toml` (not `b.toml`)\n", "a.toml"),
    # a line that names no path names no file
    "an empty value, then a Health: line": ("- Settings:\n- Health: agent http://127.0.0.1:{port}/ready 200\n", None),
}

class SettingsCheckTest(unittest.TestCase):
    """#47: `health` loads the file the `Settings:` line names through the settings loader.

    The blocks stay as small as the ones above: health reads `Settings:`, `Harness defects:` and
    `Health:`. An invalid optional Harness defects destination prints a failure and adds one to the exit code.
    """

    def run_health(self, block: str, toml: str | bytes | None = None, *, text: str | None = None, setup=None) -> tuple[int, list[str], str]:
        """Exit code, stdout lines and stderr of one `health` run over a workspace holding `block`.

        `text` replaces the whole CLAUDE.md; `setup(root)` runs once the files are written, before health does.
        What `setup` returns, if anything, is called once health is done, so the temp dir can be removed.
        """
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "CLAUDE.md"
            config.write_text("## Coordinator\n\n" + block if text is None else text)
            if toml is not None:
                (Path(d) / TOML).write_bytes(toml.encode() if isinstance(toml, str) else toml)
            undo = setup(Path(d)) if setup else None
            out, err = io.StringIO(), io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = ph.main(["--config", str(config), "--timeout", "1"])
            finally:
                if undo:
                    undo()
        return code, out.getvalue().splitlines(), err.getvalue()

    def targets(self) -> str:
        """Two `Health:` lines, one target up and one answering 500."""
        server, base = serve({"/ready": (200, 0.0), "/login": (500, 0.0)})
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"- Health: agent {base}/ready 200\n- Health: login {base}/login 200\n"

    def settings_line(self, lines: list[str]) -> str:
        found = [x for x in lines if x.startswith("settings")]
        self.assertEqual(len(found), 1, f"one settings line on stdout, got {lines!r}")
        self.assertIn(TOML, found[0])
        return found[0]

    def test_invalid_workers_mode_is_named_on_the_settings_line_and_exits_1(self):
        for spelling in (f"`{TOML}`", TOML, f"`{TOML}` (lanes and workers)"):
            with self.subTest(spelling=spelling):
                code, lines, _ = self.run_health(f"- Settings: {spelling}\n", BAD_MODE)
                self.assertIn("[workers] mode", self.settings_line(lines))
                self.assertEqual(code, 1)

    def test_valid_settings_print_a_settings_ok_line_and_exit_0(self):
        code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n", GOOD)
        self.assertRegex(self.settings_line(lines), r"\bok\b")
        self.assertEqual(code, 0)

    def test_a_settings_fault_counts_as_one_more_failed_check(self):
        code, _, _ = self.run_health(f"- Settings: `{TOML}`\n" + self.targets(), BAD_MODE)
        self.assertEqual(code, 2, "one failing target plus the settings fault")

    def test_a_settings_fault_does_not_hide_or_change_the_target_lines(self):
        targets = self.targets()
        _, alone, _ = self.run_health(targets)
        _, lines, _ = self.run_health(f"- Settings: `{TOML}`\n" + targets, BAD_MODE)
        self.settings_line(lines)
        self.assertEqual([x for x in lines if not x.startswith("settings")], alone)

    def test_valid_settings_leave_target_lines_and_exit_code_unchanged(self):
        targets = self.targets()
        alone_code, alone, _ = self.run_health(targets)
        code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n" + targets, GOOD)
        self.settings_line(lines)
        self.assertEqual([x for x in lines if not x.startswith("settings")], alone)
        self.assertEqual(code, alone_code)

    def test_the_settings_line_comes_before_the_target_lines(self):
        for toml in (GOOD, BAD_MODE):
            with self.subTest(toml=toml):
                # The Settings: line is last in the block: the order is the output's, not the file's.
                _, lines, _ = self.run_health(self.targets() + f"- Settings: `{TOML}`\n", toml)
                self.assertEqual(lines.index(self.settings_line(lines)), 0)

    def test_no_settings_line_prints_no_settings_line(self):
        # The file is there and broken: without a `Settings:` line nothing names it, so nothing reads it.
        code, lines, _ = self.run_health(self.targets(), BAD_MODE)
        self.assertEqual([x for x in lines if x.startswith("settings")], [])
        self.assertEqual(code, 1)

    def test_a_missing_settings_file_is_not_a_fault_and_is_said_to_be_absent(self):
        # settings.load: a `Settings:` line whose path is absent is the defaults. Nothing was validated,
        # so the line says the file is absent and never says ok.
        code, lines, err = self.run_health(f"- Settings: `{TOML}`\n")
        found = self.settings_line(lines)
        self.assertIn("absent", found)
        self.assertNotRegex(found, r"\bok\b")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_a_directory_at_the_settings_path_is_a_settings_fault_not_absent(self):
        """#347: a Settings path that is a directory is a refusal, not `absent, defaults`."""
        code, lines, err = self.run_health(f"- Settings: `{TOML}`\n", setup=lambda root: (root / TOML).mkdir())
        found = self.settings_line(lines)
        self.assertIn("invalid", found)
        self.assertNotIn("absent", found)
        self.assertEqual(code, 1)
        self.assertEqual(err, "")

    def test_a_non_utf8_config_is_one_line_on_stderr_and_exit_2(self):
        """#285: CLAUDE.md that is not UTF-8 is exit 2 on stderr, not a traceback."""
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "CLAUDE.md"
            config.write_bytes(b"\xff\xfe## Coordinator\n")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ph.main(["--config", str(config), "--timeout", "1"])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("UTF-8", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_an_empty_settings_file_is_ok_not_absent(self):
        # probe_health: "a settings file that is not there is `absent, defaults`". A zero-byte file is there.
        code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n", "")
        found = self.settings_line(lines)
        self.assertRegex(found, r"\bok\b")
        self.assertNotIn("absent", found)
        self.assertEqual(code, 0)

    def test_health_reports_on_the_file_every_other_command_loads(self):
        """Every other command takes the path from `workspace.read_config(root).settings_path`: health names
        that file and no other, and prints no settings line when there is none. `FORMS` says which file."""
        for form, (lines_, want) in FORMS.items():
            with self.subTest(form):
                loaded = []

                def setup(root: Path) -> None:
                    for name in ("a.toml", "b.toml"):
                        (root / name).write_text(GOOD)
                    loaded.append(workspace.read_config(root).settings_path)

                text = "# Workspace\n\n## Coordinator\n\n" + FULL + lines_.format(port=free_port())
                self.assertEqual(workspace.settings_path(text), want)
                _, lines, _ = self.run_health("", text=text, setup=setup)
                self.assertEqual(loaded, [want], "read_config on the same block")
                found = [x for x in lines if x.startswith("settings")]
                self.assertEqual([re.findall(r"\b[ab]\.toml\b", x) for x in found], [[want]] if want else [])

    def test_a_settings_line_outside_the_coordinator_block_is_not_read(self):
        stray = f"- Settings: `{TOML}`\n"
        block = "## Coordinator\n\n" + FULL
        for where, text in (("under a heading before the block", f"# Workspace\n\n## Notes\n\n{stray}\n{block}"),
                            ("under a heading after the block", f"# Workspace\n\n{block}\n## Notes\n\n{stray}"),
                            ("above every heading", f"{stray}\n{block}")):
            with self.subTest(where):
                code, lines, _ = self.run_health("", BAD_MODE, text=text)
                self.assertEqual([x for x in lines if x.startswith("settings")], [])
                self.assertEqual(code, 0)

    def test_an_unreadable_settings_file_is_a_settings_fault_line_not_a_traceback(self):
        code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n", GOOD, setup=lambda root: (root / TOML).chmod(0))
        self.assertNotRegex(self.settings_line(lines), r"\bok\b")
        self.assertEqual(code, 1)

    def test_an_unreadable_settings_file_is_named_once_with_no_absolute_path(self):
        """The coordinator quotes the line into a log: it names the file as the block does, once, says why,
        and carries no path of the machine it ran on."""
        roots = []

        def setup(root: Path) -> None:
            (root / TOML).chmod(0)
            roots.extend({str(root), str(root.resolve())})

        code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n", GOOD, setup=setup)
        found = self.settings_line(lines)
        self.assertRegex(found, r"(?i)permission denied")
        self.assertEqual(found.count(TOML), 1, found)
        for root in roots:
            self.assertNotIn(root, found)
        self.assertEqual(code, 1)

    def test_a_settings_file_in_an_unsearchable_directory_is_a_settings_fault_not_absent(self):
        """Whether the file is there cannot be told: that is a fault, not `absent` and not `ok`.

        `Path.is_file()` answers this with False on some Pythons and PermissionError on others; neither
        is "the file is absent", and neither may reach the user as a traceback."""
        name, roots = f"locked/{TOML}", []

        def setup(root: Path):
            (root / "locked").mkdir()
            (root / name).write_text(GOOD)
            (root / "locked").chmod(0)
            roots.extend({str(root), str(root.resolve())})
            return lambda: (root / "locked").chmod(0o700)

        _, alone, _ = self.run_health("- Board: none\n")
        code, lines, _ = self.run_health(f"- Settings: `{name}`\n- Board: none\n", setup=setup)
        found = self.settings_line(lines)
        self.assertIn(f"settings {name} invalid", found)
        self.assertNotIn("absent", found)
        self.assertNotRegex(found, r"\bok\b")
        for root in roots:
            self.assertNotIn(root, found)
        self.assertEqual(lines[1:], alone, "the no-targets line still prints")
        self.assertEqual(code, 1)

    def test_the_no_targets_line_still_prints_after_a_settings_line(self):
        _, alone, _ = self.run_health("- Board: none\n")
        self.assertTrue(alone, "a block with no targets says so")
        for toml in (GOOD, BAD_MODE):
            with self.subTest(toml=toml):
                _, lines, _ = self.run_health(f"- Settings: `{TOML}`\n- Board: none\n", toml)
                self.assertEqual(lines.index(self.settings_line(lines)), 0)
                self.assertEqual(lines[1:], alone)

    def test_a_loader_message_with_a_newline_is_still_one_line(self):
        """One line per check: a key the user wrote reaches the loader's message, and a newline in it must
        not start a second stdout line that reads as a target's."""
        forged = "agent http://127.0.0.1/ready 200 ok"
        _, alone, _ = self.run_health("- Board: none\n")
        for toml in (f'[models."x\\n{forged}"]\nrotation = []\n', f'[lanes."x\\n{forged}"]\nstages = []\n',
                     f'[models."x\\r{forged}"]\nrotation = []\n'):
            with self.subTest(toml=toml):
                code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n- Board: none\n", toml)
                self.settings_line(lines)
                self.assertEqual([x for x in lines if not x.startswith("settings")], alone)
                self.assertEqual(code, 1)

    def test_a_fault_line_names_the_file_once(self):
        # settings.load prefixes these four messages with the path it was given.
        faults = {"invalid TOML": "[workers\nmode = \n", "[lanes] not a table": "lanes = 1\n",
                  "[budgets] not a table of sizes": "budgets = 1\n", "[notify] not a table": "notify = 1\n"}
        for what, toml in faults.items():
            with self.subTest(what):
                code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n", toml)
                self.assertEqual(self.settings_line(lines).count(TOML), 1)
                self.assertEqual(code, 1)

    def test_an_undecodable_settings_file_is_a_settings_fault_line_not_a_traceback(self):
        code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n", b"[workers]\nmode = \"\xff\xfe\"\n")
        self.settings_line(lines)
        self.assertEqual(code, 1)

    def test_a_wrong_typed_value_the_loader_trips_on_is_a_settings_fault_line_not_a_traceback(self):
        # Codex on PR #283: valid TOML whose `gates` holds a list made the loader raise TypeError, and
        # health printed a traceback where its one `settings <file> invalid: ...` line goes.
        _, alone, _ = self.run_health("- Board: none\n")
        code, lines, _ = self.run_health(f"- Settings: `{TOML}`\n- Board: none\n",
                                         '[lanes.x]\nstages = ["a"]\ngates = [["a"]]\n')
        found = self.settings_line(lines)
        self.assertIn("invalid", found)
        self.assertIn("[lanes]", found)
        self.assertEqual(lines[1:], alone)
        self.assertEqual(code, 1)

    def test_a_settings_name_with_a_nul_is_a_settings_fault_line_not_a_traceback(self):
        # Codex on PR #283: a NUL in the `Settings:` value made `Path.stat()` raise ValueError, which is no
        # OSError, and health printed a traceback where its one `settings <file> invalid: ...` line goes.
        name = f"a\x00{TOML}"
        _, alone, _ = self.run_health("- Board: none\n")
        for spelling in (f"`{name}`", name):
            with self.subTest(spelling=spelling):
                block = f"- Settings: {spelling}\n- Board: none\n"
                self.assertEqual(workspace.settings_path("## Coordinator\n\n" + block), name)
                code, lines, err = self.run_health(block)
                found = self.settings_line(lines)
                self.assertIn("invalid", found)
                self.assertNotIn("absent", found)
                self.assertNotRegex(found, r"\bok\b")
                self.assertEqual(lines[1:], alone, "the no-targets line still prints")
                self.assertEqual(code, 1)
                self.assertEqual(err, "")

    def test_a_malformed_health_target_still_exits_2_on_stderr(self):
        code, _, err = self.run_health("- Health: secrets file:///etc/passwd 200\n")
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())

    def test_a_missing_config_still_exits_2_on_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = ph.main(["--config", str(Path(d) / "CLAUDE.md")])
        self.assertEqual(code, 2)
        self.assertTrue(err.getvalue().strip())

    def test_a_malformed_health_target_does_not_hide_the_settings_fault(self):
        block = f"- Settings: `{TOML}`\n- Health: secrets file:///etc/passwd 200\n"
        code, lines, err = self.run_health(block, BAD_MODE)
        self.assertIn("[workers] mode", self.settings_line(lines))
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())


# Three classes, three runtimes, claude named three times; agy is a runtime no rotation names.
MODELS = (GOOD + '\n[models.deep]\nrotation = ["claude:opus@high", "claude:sonnet@high"]\n'
          '\n[models.implement]\nrotation = ["claude:haiku@high", "codex:gpt-x"]\n'
          '\n[models.review]\nrotation = ["cursor:some-model"]\n')
NAMED = ("claude", "codex", "cursor")
SETTINGS = f"- Settings: `{TOML}`\n"
# Where the stand-in `resolve` says a present binary lives: a home path, which carries a username.
HOME = "/Users/somebody"


class RuntimeCheckTest(unittest.TestCase):
    """#48: `health` says which of the runtimes the `[models]` rotations name are installed.

    `probe_health.resolve` is patched in every test, as test_runtime_launch.py and test_one_shot.py patch
    theirs: no test asks a real login shell for a real binary. The stand-in is keyed by binary name, the
    thing `shell_setup.resolve` is asked for (`agent` for cursor), and answers a path under `HOME` or None.
    """

    run_health, targets, settings_line = (SettingsCheckTest.run_health, SettingsCheckTest.targets,
                                          SettingsCheckTest.settings_line)

    def health(self, block: str, toml: str | None, present=(), raises: Exception | None = None):
        """Exit code, stdout lines, stderr, and the binaries `resolve` was asked for, in order."""
        def found(program=None, **_kw):
            if raises:
                raise raises
            return f"{HOME}/.local/bin/{program}" if program in present else None

        with mock.patch.object(ph, "resolve", side_effect=found) as resolve:
            code, lines, err = self.run_health(block, toml)
        return code, lines, err, [c.args[0] if c.args else c.kwargs["program"] for c in resolve.call_args_list]

    def runtimes_line(self, lines: list[str]) -> str:
        found = [x for x in lines if x.startswith("runtimes")]
        self.assertEqual(len(found), 1, f"one runtimes line on stdout, got {lines!r}")
        return found[0]

    def said(self, line: str, name: str) -> str:
        """What the line says about runtime `name`: the text from its name up to the next runtime's name.

        No separator is pinned. The status word comes after the name it belongs to, with no other runtime's
        name between the two, so `claude ok, codex missing` cannot be read as claude missing."""
        m = re.search(rf"\b{name}\b(.*?)(?=\b(?:{'|'.join(runtimes.NAMES)})\b|$)", line)
        self.assertIsNotNone(m, f"{name} is named on {line!r}")
        return m.group(1)

    def assert_status(self, line: str, name: str, word: str) -> None:
        other = {"ok": "missing", "missing": "ok"}[word]
        about = self.said(line, name)
        self.assertRegex(about, rf"\b{word}\b", f"{name} is {word} on {line!r}")
        self.assertNotRegex(about, rf"\b{other}\b", f"{name} is not also {other} on {line!r}")

    def test_one_runtimes_line_names_each_runtime_the_rotations_name(self):
        # Rules 1 and 8 of the contract: one line, each distinct runtime, each said present.
        code, lines, err, _ = self.health(SETTINGS, MODELS, present={"claude", "codex", "agent"})
        line = self.runtimes_line(lines)
        for name in NAMED:
            self.assert_status(line, name, "ok")
        self.assertNotRegex(line, r"\bagy\b", "no rotation names agy")
        self.assertRegex(self.settings_line(lines), r"\bok\b")
        self.assertEqual((code, err), (0, ""))

    def test_each_runtime_is_looked_up_once_by_its_binary(self):
        _, _, _, asked = self.health(SETTINGS, MODELS, present={"claude", "codex", "agent"})
        self.assertEqual(Counter(asked), Counter(runtimes.binary(name) for name in NAMED))

    def test_the_runtimes_line_comes_after_the_settings_line_and_before_the_targets(self):
        targets = self.targets()
        alone_code, alone, _ = self.run_health(targets)
        # The Settings: line is last in the block: the order is the output's, not the file's.
        code, lines, _, _ = self.health(targets + SETTINGS, MODELS, present={"claude", "codex", "agent"})
        self.assertEqual(lines.index(self.settings_line(lines)), 0)
        self.assertEqual(lines.index(self.runtimes_line(lines)), 1)
        self.assertEqual(lines[2:], alone)
        self.assertEqual(code, alone_code, "present runtimes add nothing to the exit code")

    def test_each_missing_runtime_is_said_missing_and_adds_one_to_the_exit_code(self):
        # The launcher refuses a missing runtime rather than skipping it: a rotation naming one is a dispatch that fails.
        targets = self.targets()
        alone_code, alone, _ = self.run_health(targets)
        for present, missing in (({"claude", "agent"}, ("codex",)), ({"claude"}, ("codex", "cursor")), (set(), NAMED)):
            with self.subTest(missing=missing):
                code, lines, err, _ = self.health(SETTINGS + targets, MODELS, present=present)
                line = self.runtimes_line(lines)
                for name in NAMED:
                    self.assert_status(line, name, "missing" if name in missing else "ok")
                self.assertEqual(code, alone_code + len(missing))
                self.assertEqual(lines[2:], alone, "the targets are still probed and said as before")
                self.assertEqual(err, "")

    def test_the_lookup_stand_in_in_the_process_environment_says_which_runtimes_exist(self):
        """End to end, `resolve` unpatched: `CHIEF_OF_STUFF_SHELL` in the process environment, as the eval harness sets
        it, answers the lookups. `$SHELL` cannot run, so a claude said ok was found by the stand-in."""
        def setup(root: Path):
            claude = root / "claude"
            claude.write_text(f"#!{sys.executable}\n")
            claude.chmod(0o755)
            standin = test_shell_setup.ShellSetupTest.standin(self, root / "standin.py", claude)
            env = mock.patch.dict("os.environ", {"SHELL": "/missing/shell", "CHIEF_OF_STUFF_SHELL": str(standin)}, clear=True)
            env.start()
            return env.stop

        toml = GOOD + '\n[models.deep]\nrotation = ["claude:opus@high", "codex:gpt-x"]\n'
        code, lines, err = self.run_health(SETTINGS, toml, setup=setup)
        line = self.runtimes_line(lines)
        self.assert_status(line, "claude", "ok")
        self.assert_status(line, "codex", "missing")
        self.assertEqual((code, err), (1, ""))

    def test_the_runtimes_are_said_in_the_runtime_table_s_order_not_the_file_s(self):
        # One order whatever the settings file's is: the line reads the same on every machine that names the same runtimes.
        backwards = (GOOD + '\n[models.deep]\nrotation = ["cursor:some-model", "codex:gpt-x"]\n'
                     '\n[models.implement]\nrotation = ["codex:gpt-x", "claude:haiku@high"]\n')
        for present in ({"claude", "codex", "agent"}, {"codex"}):
            with self.subTest(present=present):
                _, lines, _, asked = self.health(SETTINGS, backwards, present=present)
                line = self.runtimes_line(lines)
                said = sorted(NAMED, key=lambda name: re.search(rf"\b{name}\b", line).start())
                self.assertEqual(said, [name for name in runtimes.NAMES if name in NAMED])
                self.assertEqual(Counter(asked), Counter(runtimes.binary(name) for name in NAMED))

    def test_a_malformed_health_target_does_not_hide_the_runtimes_line(self):
        # As for the settings fault in #47: the usage fault is exit 2 on stderr, and what was already checked is still said.
        block = SETTINGS + "- Health: secrets file:///etc/passwd 200\n"
        code, lines, err, _ = self.health(block, MODELS, present={"claude", "agent"})
        self.assertRegex(self.settings_line(lines), r"\bok\b")
        line = self.runtimes_line(lines)
        self.assert_status(line, "claude", "ok")
        self.assert_status(line, "codex", "missing")
        self.assertEqual(lines.index(line), 1)
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())

    def test_a_missing_runtime_with_another_binary_name_says_the_binary(self):
        # cursor installs `agent`: "cursor missing" alone does not say what to install.
        self.assertEqual(runtimes.binary("cursor"), "agent")
        _, lines, _, _ = self.health(SETTINGS, MODELS, present={"claude", "codex"})
        line = self.runtimes_line(lines)
        self.assert_status(line, "cursor", "missing")
        self.assertRegex(self.said(line, "cursor"), r"\bagent\b")

    def test_the_runtimes_line_carries_no_absolute_path(self):
        # A home path carries the username, and the coordinator quotes this line into the tracker.
        for present in ({"claude", "codex", "agent"}, {"claude"}, set()):
            with self.subTest(present=present):
                _, lines, _, _ = self.health(SETTINGS, MODELS, present=present)
                line = self.runtimes_line(lines)
                self.assertNotIn("somebody", line)
                self.assertNotRegex(line, r"(?:^|[\s(\[:='\"`])[/~][\w.]", "no token on the line starts a path")

    def test_settings_that_name_no_runtime_print_no_runtimes_line_and_ask_no_shell(self):
        cases = {"no [models] table": (SETTINGS, GOOD), "an empty settings file": (SETTINGS, ""),
                 "an absent settings file": (SETTINGS, None), "no Settings: line": ("- Board: none\n", MODELS)}
        for what, (block, toml) in cases.items():
            with self.subTest(what):
                code, lines, err, asked = self.health(block, toml, present={"claude"})
                self.assertEqual([x for x in lines if x.startswith("runtimes")], [])
                self.assertEqual(asked, [])
                self.assertEqual((code, err), (0, ""))

    def test_invalid_settings_print_no_runtimes_line_and_ask_no_shell(self):
        # The file did not load, so nothing says which runtimes it names; its fault line and count are #47's.
        faults = {"a bad key beside valid rotations": MODELS.replace("one-shot", "sometimes"),
                  "a rotation naming no runtime": GOOD + '\n[models.implement]\nrotation = ["nope:gpt-x"]\n'}
        for what, toml in faults.items():
            with self.subTest(what):
                code, lines, _, asked = self.health(SETTINGS, toml)
                self.assertIn("invalid", self.settings_line(lines))
                self.assertEqual([x for x in lines if x.startswith("runtimes")], [])
                self.assertEqual(asked, [])
                self.assertEqual(code, 1)

    def test_a_login_shell_that_cannot_be_asked_is_one_failed_check_not_a_traceback(self):
        targets = self.targets()
        alone_code, alone, _ = self.run_health(targets)
        why = ShellError("could not inspect login shell for claude: timed out after 15 seconds")
        code, lines, err, _ = self.health(SETTINGS + targets, MODELS, raises=why)
        line = self.runtimes_line(lines)
        self.assertIn("timed out", line, "the line says why")
        self.assertNotRegex(line, r"\b(?:ok|missing)\b", "a check that was not made says nothing is present or missing")
        self.assertEqual(lines.index(line), 1)
        self.assertEqual(lines[2:], alone, "the targets are still probed")
        self.assertEqual(code, alone_code + 1, "one failed check, however many runtimes were named")
        self.assertEqual(err, "")

    def test_a_login_shell_fault_line_carries_no_absolute_path(self):
        # A shell fault names the shell's path, which can sit under a home directory, and a home directory can hold a
        # space. The path is quoted by `login_shell`'s own message and inside the OSError and TimeoutExpired texts
        # `resolve` passes on, and bare where a message does not quote it. Only its last name is kept. Not pinned: a
        # bare path with a space in it, which nothing marks the end of.
        home = "/Users/zach smith"
        faults = {
            "quoted": (f"configured login shell '{HOME}/bin/fish' is not executable", "not executable"),
            "quoted, a space in the home directory": (f"configured login shell '{home}/bin/fish' is not executable", "not executable"),
            "quoted inside a list": (f"could not inspect login shell for claude: Command '['{home}/bin/zsh', '-lic', "
                                     "'command -v claude']' timed out after 15 seconds", "timed out"),
            "quoted after an errno": (f"could not inspect login shell for claude: [Errno 8] Exec format error: '{home}/bin/zsh'",
                                      "Exec format error"),
            "bare": (f"could not inspect login shell for claude: {HOME}/bin/fish: bad interpreter", "bad interpreter"),
            "bare, then quoted": (f"shell {HOME}/bin/fish could not read '{home}/.config/fish/config.fish'", "could not read"),
        }
        for what, (message, why) in faults.items():
            with self.subTest(what):
                code, lines, _, _ = self.health(SETTINGS, MODELS, raises=ShellError(message))
                line = self.runtimes_line(lines)
                self.assertIn(why, line, "the line says why")
                for part in ("Users", "somebody", "zach", "smith", "/bin", ".config"):
                    self.assertNotIn(part, line)
                self.assertRegex(line, r"\b(?:fish|zsh)\b", "the shell is still named")
                self.assertEqual(code, 1)


class HarnessDefectsConfigTest(unittest.TestCase):
    BLOCK = "## Coordinator\n\n- User: Robin\n- Daily log dir: daily/\n- Tracker: daily/<date>-tracker.md\n- Timezone: America/Chicago\n"

    def test_optional_session_is_parsed(self):
        from datetime import date
        self.assertIsNone(workspace.parse_coordinator(self.BLOCK, date.today()).harness_defects)
        self.assertEqual(workspace.parse_coordinator(self.BLOCK + "- Harness defects: session `7720-harness`\n",
                                                    date.today()).harness_defects, "7720-harness")

    def test_invalid_and_unsupported_destinations_fail_config_and_health(self):
        from datetime import date
        for value in ("repo org/repo", "session", "", "worker 7720-harness", "session `unterminated", "session `repo org/repo`", "session `a; b, task x`",
                      "session a;b", "session a,b", "session a/b", "session user", "session `USER`",
                      "session coordinator", "session `Coordinator`", "session `only-one-quote", "session only-one-quote`",
                      "session a..b", "session `a..b`"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as d:
                text = self.BLOCK + f"- Harness defects: {value}\n"
                with self.assertRaisesRegex(workspace.ConfigError, "Harness defects"):
                    workspace.parse_coordinator(text, date.today())
                path = Path(d) / "CLAUDE.md"
                path.write_text(text)
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = ph.main(["--config", str(path)])
                self.assertNotEqual(code, 0)
                self.assertIn("Harness defects", out.getvalue())


if __name__ == "__main__":
    unittest.main()
