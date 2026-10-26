"""Unit tests for scripts/backlog.py: the backlog lives in GitLab, and a missing answer is never a clean one.

Every test drives a real loopback server on port 0, the way test_probe_health.py does. The
interesting code is the header, the pagination and the four ways a fetch can fail, and a mocked
urllib would skip all four. The suite never touches the network and never reads the real Keychain:
the Keychain lookup is injected, because a test that passes only on Zach's laptop proves nothing.
"""

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import backlog as bl  # noqa: E402
import backlog_ref  # noqa: E402

TOKEN = "glpat-test-not-a-real-token"


class Ask(NamedTuple):
    method: str
    path: str
    headers: object
    body: dict


class Handler(BaseHTTPRequestHandler):
    """Serves what its server was told to serve for (method, path, state, page), and records the ask."""

    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        key = (method, parts.path, query.get("state", [""])[0], query.get("page", ["1"])[0])
        length = int(self.headers.get("Content-Length") or 0)
        raw_in = self.rfile.read(length) if length else b""
        try:
            sent = json.loads(raw_in) if raw_in else {}
        except ValueError:
            sent = {"_raw": raw_in.decode("utf-8", "replace")}
        # The Message itself, not a dict of it: header names are case-insensitive and urllib
        # sends `Private-Token`. A dict lookup would fail a request GitLab accepts.
        self.server.seen.append(Ask(method, self.path, self.headers, sent))
        status, body, headers = self.server.plan.get(key, (404, '{"message":"404 Not found"}', {}))
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's names)
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def log_message(self, *_a) -> None:
        return


def serve(plan: dict) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.plan = plan
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def issue(iid: int, title: str, state: str = "opened", labels=(), closed_at: str = "") -> dict:
    return {
        "iid": iid,
        "title": title,
        "state": state,
        "labels": list(labels),
        "web_url": f"https://labs.gauntletai.com/zachgardner/openemr/-/issues/{iid}",
        "updated_at": "2026-09-20T05:11:02.000Z",
        "closed_at": closed_at,
    }


ISSUES = "/api/v4/projects/zachgardner%2Fopenemr/issues"


def config_text(line: str) -> str:
    return (
        "# Workspace\n\n## Coordinator\n\n"
        "- User: Zach\n"
        "- Timezone: America/Chicago\n"
        f"{line}"
        "- Board: Artifact tool; URL https://example.test/b\n\n"
        "## After\n"
    )


def written(text: str) -> Path:
    path = Path(tempfile.mkdtemp()) / "CLAUDE.md"
    path.write_text(text)
    return path


def cfg(base: str) -> bl.Backlog:
    return bl.Backlog(host=base, project="zachgardner/openemr", env="CHIEF_OF_STUFF_GITLAB_TOKEN")


class ConfigTest(unittest.TestCase):
    def test_reads_host_project_and_env_name_from_the_coordinator_block(self):
        path = written(config_text(
            "- Backlog: GitLab; host https://labs.gauntletai.com; project zachgardner/openemr;"
            " token env CHIEF_OF_STUFF_GITLAB_TOKEN\n"
        ))
        found = bl.backlog_from_config(path)
        self.assertEqual(found.host, "https://labs.gauntletai.com")
        self.assertEqual(found.project, "zachgardner/openemr")
        self.assertEqual(found.env, "CHIEF_OF_STUFF_GITLAB_TOKEN")

    def test_no_backlog_line_is_absence_not_an_error(self):
        """A workspace with no backlog is a workspace with no backlog. Absent is a state, not a crash."""
        self.assertIsNone(bl.backlog_from_config(written(config_text(""))))

    def test_a_host_that_is_not_http_is_refused(self):
        path = written(config_text("- Backlog: GitLab; host ssh://labs.gauntletai.com; project a/b\n"))
        with self.assertRaises(bl.BacklogError):
            bl.backlog_from_config(path)

    def test_a_literal_token_in_the_config_is_refused(self):
        """House rule 6: a token never lives in a file that is committed. The config carries its NAME."""
        path = written(config_text(
            f"- Backlog: GitLab; host https://labs.gauntletai.com; project a/b; token {TOKEN}\n"
        ))
        with self.assertRaises(bl.BacklogError) as caught:
            bl.backlog_from_config(path)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_the_env_name_ends_at_the_first_space(self):
        """The live config line carries a parenthetical after the name, and an env var has no spaces."""
        path = written(config_text(
            "- Backlog: GitLab; host https://labs.gauntletai.com; project a/b;"
            " token env CHIEF_OF_STUFF_GITLAB_TOKEN (in the Keychain, never in this file)\n"
        ))
        self.assertEqual(bl.backlog_from_config(path).env, "CHIEF_OF_STUFF_GITLAB_TOKEN")

    def test_keychain_service_is_derived_from_the_host(self):
        self.assertEqual(cfg("https://labs.gauntletai.com").service, "gitlab-labs.gauntletai.com")

    def test_a_missing_token_names_the_variable_and_the_keychain_service(self):
        self.assertEqual(bl.Backlog("https://h.test", "g/p", "TEAM_TOKEN").missing_token,
                         "no token: set $TEAM_TOKEN or add it to the Keychain as gitlab-h.test")

    def test_a_sibling_is_another_project_read_with_the_same_credential(self):
        home = bl.Backlog("https://h.test", "g/p", "TEAM_TOKEN", "acct")
        self.assertEqual(home.sibling("g/other"), bl.Backlog("https://h.test", "g/other", "TEAM_TOKEN", "acct"))


class TokenTest(unittest.TestCase):
    def test_environment_wins_over_the_keychain(self):
        got = bl.token(cfg("https://h.test"), environ={"CHIEF_OF_STUFF_GITLAB_TOKEN": "from-env"},
                       keychain=lambda *_a: "from-keychain")
        self.assertEqual(got, "from-env")

    def test_keychain_is_not_consulted_when_the_environment_has_it(self):
        asked = []
        bl.token(cfg("https://h.test"), environ={"CHIEF_OF_STUFF_GITLAB_TOKEN": "from-env"},
                 keychain=lambda *a: asked.append(a) or "")
        self.assertEqual(asked, [])

    def test_keychain_answers_when_the_environment_does_not(self):
        got = bl.token(cfg("https://h.test"), environ={}, keychain=lambda account, service: "from-keychain")
        self.assertEqual(got, "from-keychain")

    def test_absent_everywhere_is_the_empty_string_not_an_exception(self):
        self.assertEqual(bl.token(cfg("https://h.test"), environ={}, keychain=lambda *_a: "", glab=lambda _h: ""), "")

    def test_glab_answers_when_the_environment_and_keychain_do_not(self):
        asked = []
        got = bl.token(cfg("https://h.test"), environ={}, keychain=lambda *_a: "",
                       glab=lambda host: asked.append(host) or "from-glab")
        self.assertEqual((got, asked), ("from-glab", ["h.test"]))

    def test_the_keychain_wins_over_glab(self):
        got = bl.token(cfg("https://h.test"), environ={}, keychain=lambda *_a: "from-keychain",
                       glab=lambda _h: "from-glab")
        self.assertEqual(got, "from-keychain")

    def test_a_keychain_that_fails_is_absence_not_a_crash(self):
        def boom(*_a):
            raise OSError("security: not found")
        self.assertEqual(bl.token(cfg("https://h.test"), environ={}, keychain=boom, glab=boom), "")


class IssuesTest(unittest.TestCase):
    def test_reads_one_page_and_parses_the_fields(self):
        body = json.dumps([issue(3, "PHQ-9-teen wired dead", labels=["defect"])])
        server, base = serve({("GET", ISSUES, "opened", "1"): (200, body, {})})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertEqual(got.error, "")
        self.assertEqual(len(got.issues), 1)
        one = got.issues[0]
        self.assertEqual((one.iid, one.title, one.state), (3, "PHQ-9-teen wired dead", "opened"))
        self.assertEqual(one.labels, ("defect",))

    def test_reads_closed_at(self):
        """#221: the audit needs to know *when* an issue closed, mirrored the way `updated_at` already is."""
        body = json.dumps([issue(3, "closed one", state="closed", closed_at="2026-09-17T19:05:00.000Z")])
        server, base = serve({("GET", ISSUES, "opened", "1"): (200, body, {})})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertEqual(got.issues[0].closed_at, "2026-09-17T19:05:00.000Z")

    def test_closed_at_defaults_to_empty(self):
        """A GitLab issue with no `closed_at` (an open one) reports an empty string, never None."""
        body = json.dumps([{"iid": 1, "title": "open", "state": "opened", "labels": [],
                            "web_url": "https://labs.gauntletai.com/zachgardner/openemr/-/issues/1"}])
        server, base = serve({("GET", ISSUES, "opened", "1"): (200, body, {})})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertEqual(got.issues[0].closed_at, "")

    def test_sends_the_token_as_a_private_token_header(self):
        server, base = serve({("GET", ISSUES, "opened", "1"): (200, "[]", {})})
        self.addCleanup(stop, server)
        bl.issues(cfg(base), token=TOKEN)
        self.assertEqual(server.seen[0].headers.get("PRIVATE-TOKEN"), TOKEN)

    def test_follows_x_next_page_to_the_end(self):
        """A page cap that silently truncates would read as a shorter backlog, which is 116(a) again."""
        plan = {
            ("GET", ISSUES, "opened", "1"): (200, json.dumps([issue(1, "one")]), {"X-Next-Page": "2"}),
            ("GET", ISSUES, "opened", "2"): (200, json.dumps([issue(2, "two")]), {"X-Next-Page": ""}),
        }
        server, base = serve(plan)
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertEqual([i.iid for i in got.issues], [1, 2])

    def test_no_token_never_makes_a_request(self):
        server, base = serve({})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token="")
        self.assertIn("no token", got.error)
        self.assertEqual(got.issues, ())
        self.assertEqual(server.seen, [])

    def test_an_http_error_is_a_value_not_an_exception(self):
        server, base = serve({("GET", ISSUES, "opened", "1"): (401, '{"message":"401 Unauthorized"}', {})})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertIn("401", got.error)
        self.assertEqual(got.issues, ())

    def test_malformed_json_is_a_value_not_an_exception(self):
        server, base = serve({("GET", ISSUES, "opened", "1"): (200, "not json", {})})
        self.addCleanup(stop, server)
        got = bl.issues(cfg(base), token=TOKEN)
        self.assertTrue(got.error)
        self.assertEqual(got.issues, ())

    def test_an_unreachable_host_is_a_value_not_an_exception(self):
        got = bl.issues(cfg(f"http://127.0.0.1:{free_port()}"), token=TOKEN, timeout=0.5)
        self.assertTrue(got.error)
        self.assertEqual(got.issues, ())

    def test_labels_narrow_the_ask(self):
        server, base = serve({("GET", ISSUES, "opened", "1"): (200, "[]", {})})
        self.addCleanup(stop, server)
        bl.issues(cfg(base), token=TOKEN, labels=("agent-layer", "defect"))
        self.assertIn("labels=agent-layer%2Cdefect", server.seen[0].path)


class CountsTest(unittest.TestCase):
    def test_counts_open_and_closed(self):
        plan = {
            ("GET", ISSUES, "opened", "1"): (200, "[]", {"X-Total": "93"}),
            ("GET", ISSUES, "closed", "1"): (200, "[]", {"X-Total": "7"}),
        }
        server, base = serve(plan)
        self.addCleanup(stop, server)
        got = bl.counts(cfg(base), token=TOKEN)
        self.assertEqual((got.open, got.closed, got.error), (93, 7, ""))
        self.assertEqual(bl.line(got), "backlog: 93 open, 7 closed")

    def test_a_missing_answer_never_renders_as_a_clean_zero(self):
        """116(a)'s rule reaches here: `0 open` and `we could not ask` must not read the same."""
        got = bl.counts(cfg(f"http://127.0.0.1:{free_port()}"), token=TOKEN, timeout=0.5)
        self.assertTrue(got.error)
        self.assertIsNone(got.open)
        rendered = bl.line(got)
        self.assertIn("unknown", rendered)
        self.assertNotIn("0 open", rendered)

    def test_no_token_is_unknown_not_empty(self):
        server, base = serve({})
        self.addCleanup(stop, server)
        rendered = bl.line(bl.counts(cfg(base), token=""))
        self.assertIn("unknown", rendered)
        self.assertNotIn("0 open", rendered)


LABELS = "/api/v4/projects/zachgardner%2Fopenemr/labels"


def key(method: str, path: str) -> tuple:
    """A write carries no `state` or `page`, so its plan key is the defaults the handler reads."""
    return (method, path, "", "1")


def made(iid: int, title: str) -> str:
    return json.dumps(issue(iid, title))


class WriteTest(unittest.TestCase):
    def test_dry_run_is_the_default_and_touches_nothing(self):
        """A write is outward-facing. The default has to be the one that cannot do damage."""
        server, base = serve({})
        self.addCleanup(stop, server)
        got = bl.create(cfg(base), "seed data has no cohort", token=TOKEN)
        self.assertEqual(server.seen, [])
        self.assertFalse(got.done)
        self.assertTrue(got.ok)
        self.assertIn("would create", bl.write_line(got))

    def test_commit_posts_the_issue_and_reads_back_what_gitlab_made(self):
        plan = {("GET", LABELS, "", "1"): (200, json.dumps([{"name": "defect"}]), {}),
                key("POST", ISSUES): (201, made(12, "seed data has no cohort"), {})}
        server, base = serve(plan)
        self.addCleanup(stop, server)
        got = bl.create(cfg(base), "seed data has no cohort", body="three rules cannot fire",
                        labels=("defect",), token=TOKEN, commit=True)
        self.assertTrue(got.done, got.error)
        self.assertEqual(got.iid, 12)
        self.assertIn("/-/issues/12", got.url)
        sent = server.seen[-1]
        self.assertEqual(sent.method, "POST")
        self.assertEqual(sent.body["title"], "seed data has no cohort")
        self.assertEqual(sent.body["description"], "three rules cannot fire")
        self.assertEqual(sent.body["labels"], "defect")
        self.assertEqual(sent.headers.get("PRIVATE-TOKEN"), TOKEN)
        self.assertIn("created #12", bl.write_line(got))

    def test_a_create_without_a_token_never_reaches_the_network(self):
        server, base = serve({})
        self.addCleanup(stop, server)
        got = bl.create(cfg(base), "t", token="", commit=True)
        self.assertEqual(server.seen, [])
        self.assertIn("no token", got.error)
        self.assertFalse(got.done)

    def test_a_refused_write_never_reads_as_a_write_that_happened(self):
        """116(a) for the write side: `failed` and `created` must not be the same sentence."""
        server, base = serve({key("POST", ISSUES): (403, '{"message":"403 Forbidden"}', {})})
        self.addCleanup(stop, server)
        got = bl.create(cfg(base), "t", token=TOKEN, commit=True)
        self.assertFalse(got.done)
        self.assertIn("403", got.error)
        rendered = bl.write_line(got)
        self.assertIn("failed", rendered)
        self.assertNotIn("created #", rendered)

    def test_close_sends_the_state_event(self):
        server, base = serve({key("PUT", f"{ISSUES}/12"): (200, made(12, "t"), {})})
        self.addCleanup(stop, server)
        got = bl.close(cfg(base), 12, token=TOKEN, commit=True)
        self.assertTrue(got.done, got.error)
        sent = server.seen[-1]
        self.assertEqual(sent.method, "PUT")
        self.assertEqual(sent.body["state_event"], "close")

    def test_close_dry_run_touches_nothing(self):
        server, base = serve({})
        self.addCleanup(stop, server)
        got = bl.close(cfg(base), 12, token=TOKEN)
        self.assertEqual(server.seen, [])
        self.assertIn("would close", bl.write_line(got))

    def test_a_closed_issue_is_named_once(self):
        """`closed #1: #1` is what naming the issue in both halves of the line looks like."""
        server, base = serve({key("PUT", f"{ISSUES}/12"): (200, made(12, "t"), {})})
        self.addCleanup(stop, server)
        self.assertEqual(bl.write_line(bl.close(cfg(base), 12, token=TOKEN, commit=True)), "closed #12")

    def test_comment_posts_a_note_on_the_issue(self):
        server, base = serve({key("POST", f"{ISSUES}/12/notes"): (201, '{"id": 99}', {})})
        self.addCleanup(stop, server)
        got = bl.comment(cfg(base), 12, "landed on main as 3c8476e", token=TOKEN, commit=True)
        self.assertTrue(got.done, got.error)
        sent = server.seen[-1]
        self.assertEqual((sent.method, sent.body["body"]), ("POST", "landed on main as 3c8476e"))


class LabelTest(unittest.TestCase):
    """Labels are created on demand from what a write asks for. No vocabulary is baked in:
    Zach, 2026-09-19 22:47 — "lanes are also going to be changeable over time"."""

    def test_creates_only_the_labels_that_are_missing(self):
        plan = {
            ("GET", LABELS, "", "1"): (200, json.dumps([{"name": "defect"}]), {}),
            key("POST", LABELS): (201, '{"name": "agent-layer"}', {}),
        }
        server, base = serve(plan)
        self.addCleanup(stop, server)
        got = bl.ensure_labels(cfg(base), ("defect", "agent-layer"), token=TOKEN, commit=True)
        posted = [a for a in server.seen if a.method == "POST"]
        self.assertEqual([a.body["name"] for a in posted], ["agent-layer"])
        self.assertEqual([w.what for w in got if w.done], ["agent-layer"])

    def test_a_label_that_already_exists_is_not_created_again(self):
        plan = {("GET", LABELS, "", "1"): (200, json.dumps([{"name": "defect"}]), {})}
        server, base = serve(plan)
        self.addCleanup(stop, server)
        got = bl.ensure_labels(cfg(base), ("defect",), token=TOKEN, commit=True)
        self.assertEqual([a for a in server.seen if a.method == "POST"], [])
        self.assertEqual(got, ())

    def test_dry_run_creates_no_label(self):
        plan = {("GET", LABELS, "", "1"): (200, "[]", {})}
        server, base = serve(plan)
        self.addCleanup(stop, server)
        got = bl.ensure_labels(cfg(base), ("agent-layer",), token=TOKEN)
        self.assertEqual([a for a in server.seen if a.method == "POST"], [])
        self.assertIn("would create label", bl.write_line(got[0]))

    def test_a_label_list_that_fails_is_a_value_not_an_exception(self):
        server, base = serve({("GET", LABELS, "", "1"): (500, '{"message":"500"}', {})})
        self.addCleanup(stop, server)
        got = bl.ensure_labels(cfg(base), ("agent-layer",), token=TOKEN, commit=True)
        self.assertTrue(got[0].error)
        self.assertFalse(got[0].done)

    def test_create_makes_its_labels_before_it_makes_the_issue(self):
        """An issue POSTed with an unknown label silently drops it on some GitLab versions."""
        plan = {
            ("GET", LABELS, "", "1"): (200, "[]", {}),
            key("POST", LABELS): (201, '{"name": "seed-data"}', {}),
            key("POST", ISSUES): (201, made(13, "t"), {}),
        }
        server, base = serve(plan)
        self.addCleanup(stop, server)
        bl.create(cfg(base), "t", labels=("seed-data",), token=TOKEN, commit=True)
        posts = [a.path.split("?")[0] for a in server.seen if a.method == "POST"]
        self.assertEqual(posts, [LABELS, ISSUES])


class CliWriteTest(unittest.TestCase):
    def config_at(self, base: str) -> str:
        return str(written(config_text(
            f"- Backlog: GitLab; host {base}; project zachgardner/openemr;"
            " token env CHIEF_OF_STUFF_GITLAB_TOKEN\n"
        )))

    def test_create_without_commit_writes_nothing(self):
        """There is deliberately no `--token` flag: argv is readable by `ps`, so the CLI reads the
        environment and the Keychain and nothing else."""
        server, base = serve({})
        self.addCleanup(stop, server)
        with patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": TOKEN}):
            code = bl.main(["--config", self.config_at(base), "--create", "a title"])
        self.assertEqual(code, 0)
        self.assertEqual(server.seen, [])

    def test_create_with_commit_writes(self):
        plan = {("GET", LABELS, "", "1"): (200, "[]", {}),
                key("POST", ISSUES): (201, made(14, "a title"), {})}
        server, base = serve(plan)
        self.addCleanup(stop, server)
        with patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": TOKEN}):
            code = bl.main(["--config", self.config_at(base), "--create", "a title", "--commit"])
        self.assertEqual(code, 0)
        self.assertEqual([a.method for a in server.seen if a.method == "POST"], ["POST"])

    def test_close_takes_a_bare_number_or_a_hash_number(self):
        """#44: `audit` prints `--close 226`, the tracker writes `#226`; both close issue 226."""
        server, base = serve({})
        self.addCleanup(stop, server)
        for arg in ("226", "#226"):
            with self.subTest(arg=arg):
                done = bl.Written(bl.CLOSE, "#226", done=True, iid=226)
                with patch.object(bl, "close", return_value=done) as closer, \
                        patch("sys.stdout", new=io.StringIO()):
                    code = bl.main(["--config", self.config_at(base), "--close", arg])
                self.assertEqual(code, 0)
                closer.assert_called_once()
                self.assertEqual(closer.call_args.args[1], 226)


# ---- GitHub -------------------------------------------------------------------------------------
#
# Zach, 2026-09-22 13:40: "for all future issue tracking, I want it to be stored in
# https://github.com/locriani/GauntletIssues as github issues." `gh` carries the auth; the runner is
# injected, so none of these touch the network or a socket.

LIVE_LINE = (
    "- Backlog: GitHub issues; repo https://github.com/locriani/GauntletIssues (private); read and write "
    "with `gh issue` (Zach, 2026-09-22 13:40: \"for all future issue tracking, I want it to be stored in "
    "https://github.com/locriani/GauntletIssues as github issues.\")\n"
)
REPO = "locriani/GauntletIssues"
GH = bl.GitHubBacklog(repo=REPO)


def gh_issue(number: int, title: str, state: str = "OPEN", labels=(), closed: str | None = None) -> dict:
    """`closed=None` is `gh`'s own answer for an open issue: a JSON null, never the string 'null'."""
    return {"number": number, "title": title, "state": state, "labels": [{"name": n} for n in labels],
            "url": f"https://github.com/{REPO}/issues/{number}", "updatedAt": "2026-09-22T19:24:55Z",
            "closedAt": closed}


class FakeGh:
    """Answers `gh issue list` per --state from `rows`, `gh issue close` with `close_rc`. Records argv."""

    def __init__(self, rows: dict | None = None, fail: dict | None = None, close_rc: int = 0,
                 view: dict | None = None, comments: list | None = None):
        self.rows = rows or {}
        self.fail = fail or {}
        self.close_rc = close_rc
        self.view = view
        self.comments = comments or []
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []

    def __call__(self, args: list[str], input_text: str | None = None) -> tuple[int, str, str]:
        self.calls.append(list(args))
        self.inputs.append(input_text)
        if args[:2] == ["issue", "list"]:
            state = args[args.index("--state") + 1]
            if state in self.fail:
                return self.fail[state]
            return 0, json.dumps(self.rows.get(state, [])), ""
        if args[:2] == ["issue", "close"]:
            return (self.close_rc, "", "could not close") if self.close_rc else (0, "", "")
        if args[:2] == ["issue", "create"]:
            return 0, f"https://github.com/{REPO}/issues/101\n", ""
        if args[:2] == ["issue", "comment"]:
            return 0, "", ""
        if args[:2] == ["issue", "view"]:
            if not self.view:
                return 1, "", "could not resolve the issue number"
            return 0, json.dumps(self.view), ""
        if args[0] == "api":
            return 0, json.dumps(self.comments), ""
        raise AssertionError(f"unexpected gh call: {args}")


class GitHubConfigTest(unittest.TestCase):
    def test_live_line_parses_as_written(self):
        self.assertEqual(bl.backlog_from_config(written(config_text(LIVE_LINE))), GH)

    def test_stage_five_suffix_is_ignored(self):
        line = LIVE_LINE.rstrip("\n") + "; file with `gh-issue new`\n"
        self.assertEqual(bl.backlog_from_config(written(config_text(line))), GH)

    def test_repo_forms(self):
        for spec in ("GitHub; repo o/n", "github; repo https://github.com/o/n.git", "GitHub issues; repo `o/n`"):
            with self.subTest(spec=spec):
                self.assertEqual(bl.parse_backlog(spec), bl.GitHubBacklog(repo="o/n"))

    def test_repo_is_required_and_shaped(self):
        for spec in ("GitHub issues", "GitHub; repo", "GitHub; repo just-a-name", "GitHub; repo a/b/c"):
            with self.subTest(spec=spec):
                with self.assertRaises(bl.BacklogError):
                    bl.parse_backlog(spec)

class GitHubReadTest(unittest.TestCase):
    def test_list_argv_and_mapping(self):
        gh = FakeGh(rows={"open": [gh_issue(3, "inbox.py", labels=("bug",), closed="2026-09-17T19:05:00Z")]})
        got = bl.issues(GH, gh=gh)
        self.assertTrue(got.ok, got.error)
        self.assertEqual(gh.calls, [["issue", "list", "-R", REPO, "--state", "open", "--json",
                                     "number,title,state,labels,url,updatedAt,closedAt,body", "--limit", "5000"]])
        self.assertEqual(got.issues, (bl.Issue(iid=3, title="inbox.py", state=bl.OPEN, labels=("bug",),
                                                web_url=f"https://github.com/{REPO}/issues/3",
                                                updated_at="2026-09-22T19:24:55Z",
                                                closed_at="2026-09-17T19:05:00Z"),))

    def test_closed_at_defaults_to_empty(self):
        """An open GitHub issue's `closedAt` comes back null; that must never render as the string 'None'."""
        gh = FakeGh(rows={"open": [gh_issue(3, "inbox.py")]})
        got = bl.issues(GH, gh=gh)
        self.assertEqual(got.issues[0].closed_at, "")

    def test_closed_and_all(self):
        gh = FakeGh(rows={"closed": [gh_issue(4, "p", "CLOSED")], "all": [gh_issue(1, "a"), gh_issue(4, "p", "CLOSED")]})
        self.assertEqual([i.state for i in bl.issues(GH, state=bl.CLOSED, gh=gh).issues], [bl.CLOSED])
        self.assertEqual([i.state for i in bl.issues(GH, state="all", gh=gh).issues], [bl.OPEN, bl.CLOSED])

    def test_labels_narrow(self):
        gh = FakeGh()
        bl.issues(GH, labels=("bug", "agent"), gh=gh)
        self.assertEqual(gh.calls[0][-4:], ["--label", "bug", "--label", "agent"])

    def test_limit_reached_is_not_a_whole_list(self):
        gh = FakeGh(rows={"open": [gh_issue(n, "t") for n in range(bl.GH_LIMIT)]})
        got = bl.issues(GH, gh=gh)
        self.assertFalse(got.ok)
        self.assertIn("partial", got.error)

    def test_failures_are_unknown_never_empty(self):
        for fail, needle in (((1, "", "HTTP 404: Not Found"), "HTTP 404"), ((0, "not json", ""), "JSON"),
                             ((127, "", "gh not found"), "gh not found")):
            with self.subTest(needle=needle):
                got = bl.issues(GH, gh=FakeGh(fail={"open": fail}))
                self.assertFalse(got.ok)
                self.assertIn(needle, got.error)
                self.assertEqual(got.issues, ())

    def test_run_gh_without_gh_installed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("gh")):
            self.assertEqual(bl.run_gh(["issue", "list"]), (127, "", "gh not found"))

    def test_counts(self):
        gh = FakeGh(rows={"open": [gh_issue(n, "t") for n in (1, 2, 3)], "closed": [gh_issue(4, "t", "CLOSED")]})
        self.assertEqual(bl.counts(GH, gh=gh), bl.Counts(open=3, closed=1))
        self.assertEqual([c[c.index("--json") + 1] for c in gh.calls], ["number", "number"])

    def test_counts_half_known_is_unknown(self):
        gh = FakeGh(rows={"open": [gh_issue(1, "t")]}, fail={"closed": (1, "", "rate limited")})
        got = bl.counts(GH, gh=gh)
        self.assertEqual((got.open, got.closed), (None, None))
        self.assertIn("rate limited", got.error)


class IssueRefTest(unittest.TestCase):
    """A tracker's `issue` cell. Zach, 2026-09-22 22:20: each task "is actually backed by an entry in github"."""

    def test_forms(self):
        for cell, want in (("#9", (REPO, 9)), (" #12 ", (REPO, 12)), ("o/n#4", ("o/n", 4)),
                           ("https://github.com/o/n/issues/7", ("o/n", 7)),
                           (f"[#9](https://github.com/{REPO}/issues/9)", (REPO, 9))):
            with self.subTest(cell=cell):
                self.assertEqual(bl.issue_ref(cell, GH), bl.IssueRef(*want))

    def test_rejects(self):
        for cell in ("", "TBD", "9", "#", "#x", "issue 9", "https://github.com/o/n/pull/7", "#9 #10",
                     "https://gl.example/g/p/-/merge_requests/7"):
            with self.subTest(cell=cell):
                self.assertIsNone(bl.issue_ref(cell, GH))

    def test_bare_number_needs_a_repo(self):
        self.assertIsNone(bl.issue_ref("#9", None))
        self.assertEqual(bl.issue_ref("o/n#9", None), bl.IssueRef("o/n", 9))

    def test_url_and_label(self):
        ref = bl.IssueRef(REPO, 9)
        self.assertEqual(ref.url, f"https://github.com/{REPO}/issues/9")
        self.assertEqual(ref.label(GH), "#9")
        self.assertEqual(ref.label(bl.GitHubBacklog("o/n")), f"{REPO}#9")

    def test_store_label_reads_a_home_key_as_a_number(self):
        # #903: a store key labels like IssueRef.label. Home is `#N`; another project names itself; no home drops only the forge prefix.
        gl = bl.Backlog(host="https://gl.example", project="g/p")
        self.assertEqual(backlog_ref.store_label(f"github:{REPO}#9", GH), "#9")
        self.assertEqual(backlog_ref.store_label("github:o/n#9", GH), "o/n#9")
        self.assertEqual(backlog_ref.store_label("gitlab:gl.example/g/p#5", gl), "#5")
        self.assertEqual(backlog_ref.store_label("gitlab:gl.example/acme/web#5", gl), "gl.example/acme/web#5")
        self.assertEqual(backlog_ref.store_label("gitlab:gl2.example/g/p#5", gl), "gl2.example/g/p#5")
        self.assertEqual(backlog_ref.store_label(f"github:{REPO}#9", None), f"{REPO}#9")
        self.assertEqual(backlog_ref.store_label("gitlab:gl.example/acme/web#5", None), "gl.example/acme/web#5")
        for cell in ("workflow", "#7", "https://github.com/o/n/issues/7"):
            with self.subTest(cell=cell):
                self.assertEqual(backlog_ref.store_label(cell, GH), cell)


class StoreRefTest(unittest.TestCase):
    def test_keys_preserve_their_namespace_and_label_relative_to_home(self):
        cases = (
            ("gitlab:gl2.example/g/p#5", GL, bl.IssueRef("g/p", 5, "gl2.example"), "gl2.example/g/p#5"),
            ("gitlab:gl2.example/only#5", GL, bl.IssueRef("only", 5, "gl2.example"), "gl2.example/only#5"),
            ("gitlab:gl2.example/team/nested/app#5", GL,
             bl.IssueRef("team/nested/app", 5, "gl2.example"), "gl2.example/team/nested/app#5"),
            ("gitlab:gl.example/g/p#5", GL, bl.IssueRef("g/p", 5, "gl.example"), "#5"),
            ("gitlab:gl.example/acme/web#5", GL, bl.IssueRef("acme/web", 5, "gl.example"), "gl.example/acme/web#5"),
            (f"github:{REPO}#9", GH, bl.IssueRef(REPO, 9), "#9"),
            ("github:o/n#9", GH, bl.IssueRef("o/n", 9), "o/n#9"),
            (f"github:{REPO}#9", None, bl.IssueRef(REPO, 9), f"{REPO}#9"),
            ("gitlab:gl2.example/g/p#5", None, bl.IssueRef("g/p", 5, "gl2.example"), "gl2.example/g/p#5"),
        )
        for key, home, expected, label in cases:
            with self.subTest(key=key, home=home):
                ref = backlog_ref.store_ref(key)
                self.assertEqual(ref, expected)
                self.assertEqual(ref.label(home), label)
                self.assertEqual(backlog_ref.store_label(key, home), label)

    def test_non_keys_are_not_parsed_and_keep_their_label(self):
        for key in ("", "workflow", "#5", "https://gl2.example/g/p/-/issues/5",
                    "gitlab:gl2.example/g/p#5/extra", "gitlab:gl2.example/g/p#x", " github:o/n#5"):
            with self.subTest(key=key):
                self.assertIsNone(backlog_ref.store_ref(key))
                self.assertEqual(backlog_ref.store_label(key, GL), key)

    def test_naming_a_foreign_store_key_does_not_allow_fetching_its_url(self):
        self.assertIsNone(bl.issue_ref("https://gl2.example/g/p/-/issues/5", GL))


class ParseIssueArgTest(unittest.TestCase):
    """#44: a CLI argument may be a bare `N`; a tracker cell may not. Two parsers, one responsibility each."""

    def test_issue_ref_stays_strict(self):
        self.assertIsNone(bl.issue_ref("226", GH))

    def test_forms(self):
        for arg, want in (("226", (REPO, 226)), ("#226", (REPO, 226)), (" 226 ", (REPO, 226)),
                          ("o/n#226", ("o/n", 226)),
                          ("https://github.com/o/n/issues/226", ("o/n", 226))):
            with self.subTest(arg=arg):
                self.assertEqual(bl.parse_issue_arg(arg, GH), bl.IssueRef(*want))

    def test_bare_number_resolves_against_a_gitlab_backlog(self):
        self.assertEqual(bl.parse_issue_arg("226", GL), bl.parse_issue_arg("#226", GL))
        self.assertEqual(bl.parse_issue_arg("226", GL), bl.IssueRef("g/p", 226, "gl.example"))

    def test_rejects(self):
        for arg in ("abc", "", "#", "22 6", "-5"):
            with self.subTest(arg=arg):
                self.assertIsNone(bl.parse_issue_arg(arg, GH))


# Zach, 2026-09-23 22:40: "remove the github issue remote and make everything use gitlab now that we have that going."
GL = bl.Backlog(host="https://gl.example", project="g/p")


class GitLabIssueRefTest(unittest.TestCase):
    def test_a_bare_number_is_in_the_gitlab_project(self):
        ref = bl.issue_ref("#9", GL)
        self.assertEqual(ref, bl.IssueRef("g/p", 9, "gl.example"))
        self.assertEqual(ref.url, "https://gl.example/g/p/-/issues/9")
        self.assertEqual(ref.label(GL), "#9")

    def test_a_ref_outside_the_backlogs_project_is_labelled_with_its_host_and_project(self):
        # the key an issue is filed under: another project on the home host, and the same path on another host, are two keys
        other_project = bl.IssueRef("acme/web", 8, "gl.example")
        other_host = bl.IssueRef("g/p", 8, "gl2.example")
        self.assertEqual(other_project.label(GL), "gl.example/acme/web#8")
        self.assertEqual(other_host.label(GL), "gl2.example/g/p#8")
        self.assertEqual(bl.IssueRef("g/p", 8, "gl.example").label(GL), "#8")
        # a GitHub repo keeps `owner/repo#N`; with no home, a GitLab ref is keyed by its host and project
        self.assertEqual(bl.IssueRef("o/n", 8).label(GL), "o/n#8")
        self.assertEqual(other_project.label(None), "gl.example/acme/web#8")

    def test_gitlab_urls(self):
        for cell, want in (("https://gl.example/g/p/-/issues/7", ("g/p", 7, "gl.example")),
                           ("https://gl.example/g/sub/q/-/work_items/3/", ("g/sub/q", 3, "gl.example")),
                           ("[#7](https://gl.example/g/p/-/issues/7)", ("g/p", 7, "gl.example"))):
            with self.subTest(cell=cell):
                self.assertEqual(bl.issue_ref(cell, GL), bl.IssueRef(*want))

    def test_a_gitlab_url_on_another_host_is_no_reference(self):
        # R1 on PR 14: a cell naming any host sent the Backlog's token there. Only the Backlog's own host is read.
        for cell in ("https://evil.example.com/g/p/-/issues/1", "https://gl.example.evil.com/g/p/-/issues/1"):
            with self.subTest(cell=cell):
                self.assertIsNone(bl.issue_ref(cell, GL))
        self.assertIsNone(bl.issue_ref("https://gl.example/g/p/-/issues/1", GH))
        self.assertIsNone(bl.issue_ref("https://gl.example/g/p/-/issues/1", None))

    def test_owner_repo_is_still_github(self):
        ref = bl.issue_ref("locriani/GauntletIssues#33", GL)
        self.assertEqual(ref, bl.IssueRef(REPO, 33))
        self.assertEqual(ref.label(GL), f"{REPO}#33")

    def test_the_same_path_on_another_host_is_not_home(self):
        self.assertEqual(bl.IssueRef("g/p", 9).label(GL), "g/p#9")

    def test_issue_states_reads_the_gitlab_project(self):
        seen = []

        def fake(cfg, state="opened", **_kw):
            seen.append((cfg, state))
            return bl.Fetch(issues=(bl.Issue(9, "a", bl.OPEN), bl.Issue(4, "b", bl.CLOSED)))
        with patch.object(bl, "issues", fake):
            got = bl.issue_states(GL)
        self.assertEqual({n: i.state for n, i in got.items()}, {9: bl.OPEN, 4: bl.CLOSED})
        self.assertEqual(seen, [(GL, "all")])

    def test_file_with_names_the_backend_writer(self):
        self.assertIn("chief-of-stuff backlog --create", bl.file_with(GH))
        self.assertIn("chief-of-stuff backlog --create", bl.file_with(GL))


class IssueStatesTest(unittest.TestCase):
    def test_one_list_call_all_states(self):
        gh = FakeGh(rows={"all": [gh_issue(9, "a"), gh_issue(4, "b", "CLOSED")]})
        got = bl.issue_states(GH, gh=gh)
        self.assertEqual({n: i.state for n, i in got.items()}, {9: bl.OPEN, 4: bl.CLOSED})
        self.assertEqual(len(gh.calls), 1)
        self.assertEqual(gh.calls[0][gh.calls[0].index("--state") + 1], "all")

    def test_failure_raises_never_empty(self):
        with self.assertRaisesRegex(bl.BacklogError, "rate limited"):
            bl.issue_states(GH, gh=FakeGh(fail={"all": (1, "", "rate limited")}))

    def test_partial_raises(self):
        gh = FakeGh(rows={"all": [gh_issue(n, "t") for n in range(bl.GH_LIMIT)]})
        with self.assertRaisesRegex(bl.BacklogError, "partial"):
            bl.issue_states(GH, gh=gh)


class GitHubWriteTest(unittest.TestCase):
    def test_run_gh_gives_up_on_a_stalled_gh(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 1)) as run:
            code, out, err = bl.run_gh(["issue", "list"])
        self.assertEqual((code, out), (124, ""))
        self.assertIn("timed out", err)
        self.assertEqual(run.call_args.kwargs["timeout"], bl.GH_TIMEOUT)

    def test_eval_binary_override_is_absolute_and_does_not_depend_on_path(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "fake-gh"
            fake.write_text(f"#!{sys.executable}\nimport sys\nprint('fake:' + ' '.join(sys.argv[1:]))\n")
            fake.chmod(0o755)
            with patch.dict(os.environ, {"CHIEF_OF_STUFF_GH": str(fake), "PATH": "/usr/bin:/bin"}):
                code, out, err = bl.run_gh(["issue", "list"])
            self.assertEqual((code, out.strip(), err), (0, "fake:issue list", ""))
        with patch.dict(os.environ, {"CHIEF_OF_STUFF_GH": "./gh"}):
            self.assertIn("absolute", bl.run_gh(["issue", "list"])[2])

    def test_create_preview_and_commit_use_generic_writer(self):
        gh = FakeGh()
        self.assertEqual(bl.write_line(bl.create(GH, "a title", body="b", gh=gh)), "would create: a title")
        self.assertEqual(gh.calls, [])
        got = bl.create(GH, "a title", body="private text", labels=("bug",), parent=7,
                        blocked_by=(8, 9), commit=True, gh=gh)
        self.assertTrue(got.done)
        self.assertEqual((got.iid, got.url), (101, f"https://github.com/{REPO}/issues/101"))
        self.assertEqual(gh.calls, [["issue", "create", "-R", REPO, "--title", "a title", "--body-file", "-",
                                     "--label", "bug", "--parent", "7", "--blocked-by", "8,9"]])
        self.assertEqual(gh.inputs, ["private text"])
        self.assertNotIn("private text", " ".join(gh.calls[0]))

    def test_create_failure_and_unrecognized_url_do_not_report_success(self):
        def failed(_args, _body):
            return 1, "", "permission denied"
        def malformed(_args, _body):
            return 0, "created without a URL", ""
        self.assertIn("permission denied", bl.create(GH, "x", commit=True, gh=failed).error)
        got = bl.create(GH, "x", commit=True, gh=malformed)
        self.assertFalse(got.done)
        self.assertIn("no recognizable issue URL", got.error)

    def test_comment_uses_stdin_and_previews(self):
        gh = FakeGh()
        self.assertEqual(bl.write_line(bl.comment(GH, 3, "b", gh=gh)), "would comment: #3")
        got = bl.comment(GH, 3, "b", commit=True, gh=gh)
        self.assertTrue(got.done)
        self.assertEqual(gh.calls, [["issue", "comment", "3", "-R", REPO, "--body-file", "-"]])
        self.assertEqual(gh.inputs, ["b"])

    def test_close_dry_run(self):
        gh = FakeGh()
        got = bl.close(GH, 5, gh=gh)
        self.assertEqual(bl.write_line(got), "would close: #5")
        self.assertEqual(gh.calls, [])

    def test_close_commit(self):
        gh = FakeGh()
        got = bl.close(GH, 5, commit=True, gh=gh)
        self.assertEqual(gh.calls, [["issue", "close", "5", "-R", REPO]])
        self.assertEqual(bl.write_line(got), "closed #5")

    def test_close_failure(self):
        got = bl.close(GH, 5, commit=True, gh=FakeGh(close_rc=1))
        self.assertIn("could not close", got.error)


class GitHubCliTest(unittest.TestCase):
    def setUp(self):
        self.config = str(written(config_text(LIVE_LINE)))
        self.gh = FakeGh(rows={"open": [gh_issue(n, f"t{n}", labels=("bug",)) for n in (1, 2, 3)],
                               "closed": [gh_issue(n, f"t{n}", "CLOSED") for n in (4, 5)]})
        patcher = patch.object(bl, "run_gh", self.gh)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, *argv: str) -> tuple[int, str]:
        from contextlib import redirect_stdout
        import io
        out = io.StringIO()
        with redirect_stdout(out):
            code = bl.main(["--config", self.config, *argv])
        return code, out.getvalue()

    def test_count(self):
        self.assertEqual(self.run_main(), (0, "backlog: 3 open, 2 closed\n"))

    def test_list(self):
        code, out = self.run_main("--list")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["#1 t1 [bug]", "#2 t2 [bug]", "#3 t3 [bug]"])

    def test_create_uses_builtin_writer(self):
        code, out = self.run_main("--create", "a title", "--commit")
        self.assertEqual(code, 0)
        self.assertIn("created #101: a title", out)
        self.assertIn(["issue", "create", "-R", REPO, "--title", "a title", "--body-file", "-"], self.gh.calls)

    def test_close_takes_a_bare_number_or_a_hash_number(self):
        for arg in ("226", "#226"):
            with self.subTest(arg=arg):
                self.gh.calls.clear()
                code, out = self.run_main("--close", arg, "--commit")
                self.assertEqual((code, out), (0, "closed #226\n"))
                self.assertEqual(self.gh.calls, [["issue", "close", "226", "-R", REPO]])

    def test_close_refuses_an_issue_outside_the_backlog(self):
        """`other/repo#5` is not the configured Backlog: an error, and `close` is never reached."""
        err = io.StringIO()
        try:
            with patch("sys.stderr", new=err):
                code, _out = self.run_main("--close", "other/repo#5", "--commit")
        except SystemExit:
            self.fail("argparse rejected the value; main() should return its own error")
        self.assertNotEqual(code, 0)
        self.assertIn("other/repo#5", err.getvalue())
        self.assertEqual(self.gh.calls, [])

    def test_close_refuses_garbage(self):
        err = io.StringIO()
        try:
            with patch("sys.stderr", new=err):
                code, _out = self.run_main("--close", "abc", "--commit")
        except SystemExit:
            self.fail("argparse rejected the value; main() should return its own error")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.gh.calls, [])


class ReadOneTest(unittest.TestCase):
    """#63: `--view` reads one issue's title, state, body and labels through the same port as the list."""

    def test_view_reads_title_state_body_and_labels(self):
        row = {**issue(12, "the design gate"), "description": "gate the design before build"}
        row["labels"] = ["design-gate", "board"]
        server, base = serve({("GET", f"{ISSUES}/12", "", "1"): (200, json.dumps(row), {})})
        try:
            got = bl.read_one(cfg(base), 12, token=TOKEN)
        finally:
            stop(server)
        self.assertTrue(got.ok)
        self.assertEqual(got.issue.iid, 12)
        self.assertEqual(got.issue.title, "the design gate")
        self.assertEqual(got.issue.state, "opened")
        self.assertEqual(got.issue.body, "gate the design before build")
        self.assertEqual(got.issue.labels, ("design-gate", "board"))

    def test_a_missing_issue_is_an_error_not_an_exception(self):
        server, base = serve({})  # every path 404s
        try:
            got = bl.read_one(cfg(base), 99, token=TOKEN)
        finally:
            stop(server)
        self.assertFalse(got.ok)
        self.assertTrue(got.error)
        self.assertIsNone(got.issue)

    def test_a_view_without_a_token_reports_the_absence(self):
        got = bl.read_one(cfg("http://127.0.0.1:1"), 12, token="")
        self.assertFalse(got.ok)
        self.assertIn("token", got.error)


class CommentsTest(unittest.TestCase):
    """#63: `--comments` reads an issue's notes in order, each with author and time. GitLab's system
    notes (label and state changes) are not a person speaking; they are not comments."""

    def test_notes_read_in_order_with_author_time_and_body(self):
        plan = {("GET", f"{ISSUES}/12/notes", "", "1"): (200, json.dumps([
            {"body": "closed the duplicate", "author": {"username": "gitlab-bot"},
             "created_at": "2026-09-25T10:00:00Z", "system": True},
            {"body": "the design gate passes on the new layout", "author": {"username": "zach"},
             "created_at": "2026-09-25T21:48:00Z", "system": False},
            {"body": "one more question", "author": {"username": "robin"},
             "created_at": "2026-09-26T08:15:00Z", "system": False},
        ]), {})}
        server, base = serve(plan)
        try:
            got = bl.comments(cfg(base), 12, token=TOKEN)
        finally:
            stop(server)
        self.assertTrue(got.ok)
        self.assertEqual([(n.author, n.time, n.body) for n in got.notes], [
            ("zach", "2026-09-25T21:48:00Z", "the design gate passes on the new layout"),
            ("robin", "2026-09-26T08:15:00Z", "one more question"),
        ])

    def test_notes_read_every_page_before_the_answer(self):
        page_one = [{"body": f"note {i}", "author": {"username": "zach"},
                     "created_at": f"2026-09-25T10:{i // 60:02d}:{i % 60:02d}Z", "system": False}
                    for i in range(100)]
        last = {"body": "the last note", "author": {"username": "robin"},
                "created_at": "2026-09-26T08:15:00Z", "system": False}
        plan = {
            ("GET", f"{ISSUES}/12/notes", "", "1"): (200, json.dumps(page_one), {"X-Next-Page": "2"}),
            ("GET", f"{ISSUES}/12/notes", "", "2"): (200, json.dumps([last]), {}),
        }
        server, base = serve(plan)
        try:
            got = bl.comments(cfg(base), 12, token=TOKEN)
        finally:
            stop(server)
        self.assertTrue(got.ok)
        self.assertEqual(len(got.notes), 101)
        self.assertEqual(got.notes[-1].body, "the last note")

    def test_a_failed_notes_read_is_an_error_not_an_exception(self):
        server, base = serve({})
        try:
            got = bl.comments(cfg(base), 99, token=TOKEN)
        finally:
            stop(server)
        self.assertFalse(got.ok)
        self.assertTrue(got.error)


class GitHubReadTest(unittest.TestCase):
    """#63: the same read through GitHub's gh port, same shapes."""

    def test_view_reads_the_body_through_gh(self):
        fake = FakeGh(view={"number": 12, "title": "the design gate", "state": "OPEN",
                            "labels": [{"name": "design-gate"}],
                            "url": f"https://github.com/{REPO}/issues/12",
                            "updatedAt": "2026-09-22T19:24:55Z", "closedAt": None,
                            "body": "gate the design before build"})
        got = bl.read_one(GH, 12, gh=fake)
        self.assertTrue(got.ok)
        self.assertEqual(got.issue.body, "gate the design before build")
        self.assertEqual(got.issue.labels, ("design-gate",))
        self.assertEqual(got.issue.state, "opened")
        self.assertIn("view", fake.calls[0])

    def test_comments_read_in_order_through_gh_api(self):
        fake = FakeGh(comments=[{"user": {"login": "zach"}, "created_at": "2026-09-25T21:48:00Z",
                                 "body": "the gate passes"},
                                {"user": {"login": "robin"}, "created_at": "2026-09-26T08:15:00Z",
                                 "body": "one more"}])
        got = bl.comments(GH, 12, gh=fake)
        self.assertTrue(got.ok)
        self.assertEqual([(n.author, n.time, n.body) for n in got.notes], [
            ("zach", "2026-09-25T21:48:00Z", "the gate passes"),
            ("robin", "2026-09-26T08:15:00Z", "one more"),
        ])

    def test_a_failed_view_is_an_error_not_an_exception(self):
        got = bl.read_one(GH, 404, gh=FakeGh())  # no view row: gh answers rc 1
        self.assertFalse(got.ok)
        self.assertTrue(got.error)


class ViewTextTest(unittest.TestCase):
    """#63: the text `--view` prints — the issue's own line, its state, body, then its comments in order."""

    def test_view_text_names_the_issue_its_state_and_labels(self):
        one = bl.Issue(iid=12, title="the design gate", state="opened", labels=("design-gate",), body="gate it")
        text = bl.view_text(one)
        self.assertIn("#12 the design gate [design-gate]", text)
        self.assertIn("state: opened", text)
        self.assertIn("gate it", text)

    def test_comments_follow_in_order_with_author_and_time(self):
        one = bl.Issue(iid=12, title="t", state="opened", body="")
        text = bl.view_text(one, notes=(bl.Note("zach", "2026-09-25T21:48:00Z", "the gate passes"),
                                        bl.Note("robin", "2026-09-26T08:15:00Z", "one more")))
        self.assertEqual(text.splitlines()[-2:], [
            "2026-09-25T21:48:00Z zach: the gate passes",
            "2026-09-26T08:15:00Z robin: one more",
        ])


class ViewMainTest(unittest.TestCase):
    """#63: the coordinator reads a design-gate comment through the entry point, not glab or gh by hand."""

    def run_view(self, *extra: str, plan: dict | None = None) -> tuple[int, str, str, ThreadingHTTPServer]:
        planned = plan if plan is not None else {
            ("GET", f"{ISSUES}/12", "", "1"):
                (200, json.dumps({**issue(12, "the design gate"), "description": "gate it"}), {}),
        }
        server, base = serve(planned)
        self.addCleanup(stop, server)
        path = written(config_text(
            f"- Backlog: GitLab; host {base}; project zachgardner/openemr; token env CHIEF_OF_STUFF_GITLAB_TOKEN\n"
        ))
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"CHIEF_OF_STUFF_GITLAB_TOKEN": TOKEN}):  # noqa: S607
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = bl.main(["--config", str(path), "--view", "12", *extra])
        return code, out.getvalue(), err.getvalue(), server

    def test_view_prints_the_issue_through_the_entry_point(self):
        code, out, err, _server = self.run_view()
        self.assertEqual(code, 0, err)
        self.assertIn("the design gate", out)
        self.assertIn("gate it", out)

    def test_view_with_comments_prints_the_notes_in_order(self):
        plan = {
            ("GET", f"{ISSUES}/12", "", "1"):
                (200, json.dumps({**issue(12, "the design gate"), "description": "gate it"}), {}),
            ("GET", f"{ISSUES}/12/notes", "", "1"): (200, json.dumps([
                {"body": "the design gate passes", "author": {"username": "zach"},
                 "created_at": "2026-09-25T21:48:00Z", "system": False},
            ]), {}),
        }
        code, out, err, _server = self.run_view("--comments", plan=plan)
        self.assertEqual(code, 0, err)
        self.assertIn("2026-09-25T21:48:00Z zach: the design gate passes", out)

    def test_a_missing_issue_exits_1_saying_unknown(self):
        code, out, err, _server = self.run_view(plan={})
        self.assertEqual(code, 1)
        self.assertIn("unknown", err)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
