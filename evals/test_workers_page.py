"""Issue #195: a `/workers` page lists every running worker and today's ended launches.

Every test drives the real page server (pages.py) over a fixture workspace, so the only seams are the route and HOME:

- `GET /workers` is the page. pages.py renders it on request, as it renders the board and the task pages.
- The running workers are the sources cache's (`.sources.json`, `board_sources.Sources.workers`); the fixture writes
  the cache directly and the server's refresh does nothing, so no process or registry is read.
- The ended launches, the started lines and each task's ref and stage are today's tracker's (written by
  `one_shot.record_launch` / `one_shot.update_tracker`, the launcher's own writers).
- A worker's Claude session log is `Path.home() / ".claude" / "projects" / <dir> / <session>.jsonl`, where `<dir>`
  is the worker tree's absolute path (a one-shot's tree is `<root>/<Worktrees dir>/<Worker.tree>`) with every
  character other than a letter or digit replaced by `-` (so `/` and `.` become `-`, and so does a temp dir's `_`).
  The tests set HOME to a temp dir; the page must find the log through `Path.home()`, never a fixed path.
  Tokens are the sum of `input_tokens`, `output_tokens`, `cache_creation_input_tokens` and
  `cache_read_input_tokens` over every line's `message.usage`; last activity is the latest line `timestamp`,
  shown as HH:MM in the workspace's timezone.

Markup contract these tests read (keep to it; everything else is free):

- The page opens with the section tab bar (test_tab_bar's contract), the Workers tab `aria-current="page"`.
- RUNNING and ENDED TODAY are each a heading (`<h1>`–`<h6>`) whose text starts with that name, followed by a
  `<table>`: its first `<tr>` holds `<th>` column heads, and each later `<tr>` is one row. On RUNNING, the column for
  last activity has a head containing "activity" and the one for tokens a head containing "token"; a row's cells line
  up with the heads one to one.
- Header boxes sit in the page's `<header>`: each is an element whose text is its label (`need you`, `running`,
  `slots free`) and its value, in either order.
- A task link is `<a href="/issues/<n>">`.
"""

import functools
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path[:0] = [str(Path(__file__).resolve().parent.parent / "scripts"), str(Path(__file__).resolve().parent)]
import board_sources as bs  # noqa: E402
import one_shot  # noqa: E402
import pages as pg  # noqa: E402
from test_pages import get  # noqa: E402
from test_tab_bar import TABS, VOID, Node, name, parse  # noqa: E402

ZONE = ZoneInfo("America/Chicago")
CAP = 4
HEAD = "| name | item | owner | state | since | due | size | lane | stage | issue | checklist |\n" + "|---" * 11 + "|\n"
TRACKER = """# Tracker

## Tasks

{head}| Upload size limit | Cap uploads | unassigned | open | {day} |  | M |  | implement | #109 | c |
| Cache warmup | Warm the pool | unassigned | open | {day} |  | S |  | review | #111 | c |
| Tidy config | Tidy | Robin | open | {day} |  | S |  |  | #112 | c |
| Review round 1 | Review r1 | unassigned | open | {day} |  | S |  |  | #120 | c |
| Verify limits | Verify r1 | unassigned | open | {day} |  | S |  |  | #121 | c |
| Fix round 1 | Fix r1 | unassigned | open | {day} |  | S |  |  | #122 | c |
| Docs pass | Docs p1 | unassigned | open | {day} |  | S |  |  | #123 | c |

## File ownership

| context | paths |
|---|---|
| Cap uploads | `src/a/` |
| Warm the pool | `src/b/` |
| Review r1 | `src/c/` |
| Verify r1 | `src/d/` |
| Fix r1 | `src/e/` |
| Docs p1 | `src/f/` |

## Log

- 00:01 opened the day
"""
# Yesterday's human-review end: neither a row of ENDED TODAY nor a count of need you.
YESTERDAY_TRACKER = "# Tracker\n\n## Log\n\n- 23:10 one-shot impl-z9: HUMAN REVIEW NEEDED — stuck. Changes: none.\n"
# The worker with a session log (impl-a1): its usage, one dict per assistant line. Tokens are the four fields' sum.
USAGE = ({"input_tokens": 100, "output_tokens": 200, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 40000},
         {"input_tokens": 20, "output_tokens": 180, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 12000})
TOKENS = 56000
# Today's launcher end lines: worker -> (Log time, what the line says, how it ended as the page words it, task ref).
ENDED = {"impl-c3": ("00:20", "done", "completed", "120"),
         "impl-d4": ("00:30", "human_review", "needs input", "121"),
         "impl-e5": ("00:40", "relaunch", "relaunch", "122")}
TASK = {"impl-c3": "Review r1", "impl-d4": "Verify r1", "impl-e5": "Fix r1"}


def claude_dir(tree: Path) -> str:
    """The `~/.claude/projects/` dir name Claude gives a session run in `tree`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(tree))


def stamp(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def transcript_line(kind: str, at: datetime, usage: dict | None = None) -> str:
    message = {"role": kind, "content": "…"} | ({"usage": usage} if usage else {})
    return json.dumps({"type": kind, "timestamp": stamp(at), "message": message}) + "\n"


def token_forms(n: int) -> set[str]:
    """The ways a page may write a token count: 56000, 56,000, 56.0k, 56k."""
    return {str(n), f"{n:,}", f"{n / 1000:.1f}k", f"{round(n / 1000)}k"}


def elapsed(start: datetime, now: datetime) -> set[str]:
    """Time running as the board writes it (`2h05m`), allowing the minute to tick between fixture and render."""
    return {f"{m // 60}h{m % 60:02d}m" for m in (int((now - start).total_seconds() // 60) + d for d in (0, 1))}


class Workspace:
    """A workspace on disk: today's tracker with two running one-shots (impl-a1 with a session log, impl-b2
    without), a session (planner), today's three end lines, yesterday's tracker, and the sources cache."""

    def __init__(self, cap: int | None):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.home = self.root / "home"
        self.now = datetime.now(ZONE)
        day = self.now.date().isoformat()
        yesterday = (self.now.date() - timedelta(days=1)).isoformat()
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Worktrees: `trees/`\n- Timezone: {ZONE.key}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
            + ("- Settings: `cos.toml`\n" if cap else ""))
        if cap:
            (self.root / "cos.toml").write_text(f"[workers]\nmax_concurrency = {cap}\n")
        (self.root / "daily").mkdir()
        (self.root / "daily" / f"{yesterday}-tracker.md").write_text(YESTERDAY_TRACKER)
        self.tracker = self.root / "daily" / f"{day}-tracker.md"
        self.tracker.write_text(TRACKER.format(head=HEAD, day=day))
        self.started = {"impl-a1": self.now - timedelta(hours=2, minutes=5), "impl-b2": self.now - timedelta(hours=1, minutes=10),
                        "planner": self.now - timedelta(minutes=30)}
        for worker, task, runtime, model in (("impl-a1", "Cap uploads", "claude", "opus"), ("impl-b2", "Warm the pool", "codex", "gpt-5.5")):
            (self.root / "trees" / worker).mkdir(parents=True)
            self.launch(task, worker, runtime, model, f"{self.started[worker]:%H:%M}")
        for worker, (at, status, _, _) in ENDED.items():
            self.launch(TASK[worker], worker, "claude", "sonnet", "00:10")
            one_shot.update_tracker(self.tracker, TASK[worker], worker, "Robin", status, "reason", "x.py", at)
        self.pages = self.root / "pages"
        self.pages.mkdir()
        self.cache([bs.Worker("impl-a1", "one-shot", "claude", "opus", "Upload size limit", "impl-a1", self.started["impl-a1"], None),
                    bs.Worker("impl-b2", "one-shot", "codex", "gpt-5.5", "Cache warmup", "impl-b2", self.started["impl-b2"], None),
                    bs.Worker("planner", "session", "claude", "", "Tidy config", "", self.started["planner"], None)])
        self.log = self.home / ".claude" / "projects" / claude_dir(self.root / "trees" / "impl-a1") / "0a1b2c3d.jsonl"
        self.log.parent.mkdir(parents=True)
        self.last = self.now - timedelta(minutes=2)
        self.log.write_text(transcript_line("user", self.now - timedelta(minutes=10))
                            + transcript_line("assistant", self.now - timedelta(minutes=8), USAGE[0])
                            + transcript_line("assistant", self.now - timedelta(minutes=5), USAGE[1])
                            + transcript_line("user", self.last))

    def launch(self, task: str, worker: str, runtime: str, model: str, at: str):
        one_shot.record_launch(self.tracker, task, worker, f"worktree `trees/{worker}` ({worker})", runtime, model, at)

    def cache(self, workers: list[bs.Worker]):
        bs._write(self.pages / bs.CACHE, bs.Sources({"workers": self.now}, {}, {}, tuple(workers), {}))

    def touch(self, path: Path):
        """Make `path` newer than any page already rendered from it."""
        later = time.time() + 5
        os.utime(path, (later, later))

    def serve(self):
        """Start the server with HOME at the fixture's home; returns the port. Undo with `close`."""
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        self.server = pg.make_server(self.pages, 0, root=self.root, refresh=lambda root, now: None, every=3600)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()
        shutil.rmtree(self.root, True)


@functools.cache
def served(cap: int | None) -> tuple[Workspace, dict[str, tuple[int, str]]]:
    """The fixture workspace and its `/workers` and `/` pages as the server answers them: (status, body)."""
    ws = Workspace(cap)
    port = ws.serve()
    try:
        out = {}
        for path in ("/workers", "/"):
            resp = get(port, path)
            out[path] = (resp.status, resp.body.decode())
        return ws, out
    finally:
        ws.close()


def page(cap: int | None = CAP, path: str = "/workers") -> str:
    status, body = served(cap)[1][path]
    assert status == 200, f"GET {path} answered {status}, not the page"
    return body


class Tree(HTMLParser):
    """The whole document as Nodes."""

    def __init__(self):
        super().__init__()
        self.root = Node("#document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        text = Node("#text", {})
        text.text.append(data)
        self.stack[-1].children.append(text)


def doc(html: str) -> Node:
    t = Tree()
    t.feed(html)
    return t.root


def words(node: Node) -> str:
    """The node's text in document order, one space between pieces (so `<td>a</td><td>b</td>` reads `a b`)."""
    return " ".join(unescape(" ".join(t for n in node.walk() if n.tag == "#text" for t in n.text)).split())


def table(html: str, heading: str) -> Node:
    """The first `<table>` after the heading whose text starts with `heading`."""
    seen = False
    for n in doc(html).walk():
        if not seen and re.fullmatch(r"h[1-6]", n.tag) and words(n).casefold().startswith(heading.casefold()):
            seen = True
        elif seen and n.tag == "table":
            return n
    raise AssertionError(f"no {heading} heading followed by a <table>")


def cells(tr: Node) -> list[Node]:
    return [c for c in tr.children if c.tag in ("td", "th")]


def rows(t: Node) -> list[Node]:
    """The table's rows below its head row."""
    trs = [n for n in t.walk() if n.tag == "tr"]
    return [tr for tr in trs if any(c.tag == "td" for c in cells(tr))]


def row(t: Node, worker: str) -> Node:
    hits = [tr for tr in rows(t) if re.search(rf"(?<![\w-]){re.escape(worker)}(?![\w-])", words(tr))]
    if len(hits) != 1:
        raise AssertionError(f"{len(hits)} rows name {worker}, not one")
    return hits[0]


def column(t: Node, tr: Node, head: str) -> str:
    """The text of `tr`'s cell under the column head containing `head`."""
    heads = next((cells(r) for r in (n for n in t.walk() if n.tag == "tr") if all(c.tag == "th" for c in cells(r)) and cells(r)), [])
    at = [i for i, c in enumerate(heads) if head in words(c).casefold()]
    if len(at) != 1:
        raise AssertionError(f"{len(at)} column heads contain {head!r}: {[words(c) for c in heads]}")
    return words(cells(tr)[at[0]])


def links(node: Node) -> list[str]:
    return [n.attrs.get("href", "") for n in node.walk() if n.tag == "a"]


def box(html: str, label: str) -> str | None:
    """The value of the `<header>` box labelled `label`, or None when the header has none."""
    header = next((n for n in doc(html).walk() if n.tag == "header"), None)
    if header is None:
        raise AssertionError("the page has no <header>")
    found = sorted((len(t), t) for n in header.walk() if label in (t := words(n).casefold())
                   and re.search(r"\d", t.replace(label, "", 1)))
    return found[0][1].replace(label, "", 1).strip() if found else None


class TabsTest(unittest.TestCase):
    """(#195) `/workers` opens with the tab bar, Workers current; the Workers tab and the board panel title link to it."""

    def test_the_page_opens_with_the_bar_and_workers_is_current(self):
        navs = parse(page()).navs
        self.assertEqual(len(navs), 1, f"/workers has {len(navs)} Board sections navs, not one")
        self.assertEqual(tuple(name(t) for t in navs[0].children), TABS)
        current = [name(t) for t in navs[0].children if any(n.attrs.get("aria-current") == "page" for n in t.walk())]
        self.assertEqual(current, ["Workers"], f"aria-current=\"page\" is on {current}, not only Workers")

    def test_the_workers_tab_links_to_the_page(self):
        tab = {name(t): t for t in parse(page()).navs[0].children}["Workers"]
        self.assertEqual([n.attrs["href"] for n in tab.walk() if "href" in n.attrs], ["/workers"])

    def test_the_board_workers_panel_title_links_to_the_page(self):
        panel = next((n for n in doc(page(path="/")).walk() if n.tag == "section" and n.attrs.get("id") == "workers"), None)
        self.assertIsNotNone(panel, "the board has no WORKERS panel")
        titled = [n for n in panel.walk() if n.tag == "a" and n.attrs.get("href") == "/workers"
                  and words(n).casefold() == "workers"]
        self.assertTrue(titled, "the board's WORKERS panel title is not a link to /workers")


class RunningTest(unittest.TestCase):
    """(#195) RUNNING: a row per running worker: name, runtime and model, one-shot or session, task ref linked to its
    task page, task name, stage, time running."""

    def test_a_row_per_running_worker(self):
        t = table(page(), "running")
        for worker in ("impl-a1", "impl-b2", "planner"):
            with self.subTest(worker=worker):
                row(t, worker)
        self.assertEqual(len(rows(t)), 3, [words(tr) for tr in rows(t)])

    def test_a_one_shot_row_names_its_runtime_task_stage_and_time(self):
        ws = served(CAP)[0]
        for worker, runtime, model, ref, task, stage in (("impl-a1", "claude", "opus", "109", "Upload size limit", "implement"),
                                                         ("impl-b2", "codex", "gpt-5.5", "111", "Cache warmup", "review")):
            with self.subTest(worker=worker):
                tr = row(table(page(), "running"), worker)
                text = words(tr)
                for want in (runtime, model, "one-shot", task, stage):
                    self.assertIn(want, text)
                self.assertIn(f"/issues/{ref}", links(tr))
                self.assertTrue(elapsed(ws.started[worker], ws.now) & set(text.split()),
                                f"{worker}'s row shows no time running of {sorted(elapsed(ws.started[worker], ws.now))}: {text}")

    def test_a_session_row_names_its_runtime_task_and_time(self):
        ws = served(CAP)[0]
        text = words(row(table(page(), "running"), "planner"))
        for want in ("claude", "session", "Tidy config"):
            self.assertIn(want, text)
        self.assertTrue(elapsed(ws.started["planner"], ws.now) & set(text.split()), f"planner's row shows no time running: {text}")


class ActivityTest(unittest.TestCase):
    """(#195) A row shows last activity and tokens from the worker's Claude session log when it has one, and leaves
    both blank otherwise."""

    def test_a_worker_with_a_session_log_shows_its_last_activity_and_tokens(self):
        ws = served(CAP)[0]
        t = table(page(), "running")
        tr = row(t, "impl-a1")
        self.assertEqual(column(t, tr, "activity"), f"{ws.last.astimezone(ZONE):%H:%M}")
        self.assertIn(column(t, tr, "token"), token_forms(TOKENS))

    def test_a_worker_without_one_leaves_both_blank(self):
        t = table(page(), "running")
        for worker in ("impl-b2", "planner"):
            with self.subTest(worker=worker):
                tr = row(t, worker)
                self.assertEqual((column(t, tr, "activity"), column(t, tr, "token")), ("", ""))


class EndedTodayTest(unittest.TestCase):
    """(#195) ENDED TODAY: a row per launcher end line today: time, how it ended, worker, task, linked to the task page."""

    def test_a_row_per_end_line_today_and_none_from_yesterday(self):
        t = table(page(), "ended today")
        self.assertEqual(len(rows(t)), len(ENDED), [words(tr) for tr in rows(t)])
        self.assertNotIn("impl-z9", words(t))

    def test_each_row_names_its_time_how_it_ended_and_links_its_task(self):
        t = table(page(), "ended today")
        for worker, (at, _, how, ref) in ENDED.items():
            with self.subTest(worker=worker):
                tr = row(t, worker)
                text = words(tr)
                self.assertIn(at, text.split())
                self.assertIn(how, text.casefold())
                self.assertIn(f"/issues/{ref}", links(tr))


class HeaderTest(unittest.TestCase):
    """(#195) Header boxes: need you (today's human-review ends), running, and slots free `k of N` when the workspace
    caps one-shots."""

    def test_no_header_prints_a_rendered_clock(self) -> None:
        # #215: the eyebrow keeps the board link and the sources' errors, not a render clock.
        self.assertNotRegex(page(), r"\brendered \d{1,2}:\d{2}")

    def test_need_you_counts_todays_human_review_ends(self):
        self.assertEqual(box(page(), "needs input"), "1")

    def test_running_counts_the_running_workers(self):
        self.assertEqual(box(page(), "running"), "3")

    def test_slots_free_is_the_cap_less_the_running_one_shots(self):
        self.assertEqual(box(page(), "slots free"), f"{CAP - 2} of {CAP}")

    def test_an_uncapped_workspace_has_no_slots_box(self):
        self.assertIsNone(box(page(None), "slots free"))


class OnRequestTest(unittest.TestCase):
    """(#195) The page renders from the sources cache and logs on request; no agent writes it."""

    def setUp(self):
        self.ws = Workspace(CAP)
        self.port = self.ws.serve()
        self.addCleanup(self.ws.close)

    def fetch(self) -> str:
        resp = get(self.port, "/workers")
        self.assertEqual(resp.status, 200, f"GET /workers answered {resp.status}")
        return resp.body.decode()

    def test_the_next_request_shows_what_the_cache_and_logs_say_now(self):
        self.fetch()
        ws = self.ws
        ws.cache([*bs.load(ws.pages).workers,
                  bs.Worker("impl-g7", "one-shot", "claude", "opus", "Docs pass", "impl-g7", ws.now - timedelta(minutes=5), None)])
        ws.touch(ws.pages / bs.CACHE)
        ws.launch("Docs p1", "impl-f6", "claude", "sonnet", "00:50")
        one_shot.update_tracker(ws.tracker, "Docs p1", "impl-f6", "Robin", "done", "reason", "y.py", "01:00")
        ws.touch(ws.tracker)
        with ws.log.open("a") as f:
            f.write(transcript_line("assistant", ws.now - timedelta(minutes=1), {"input_tokens": 1000}))
        ws.touch(ws.log)
        html = self.fetch()
        running, ended = table(html, "running"), table(html, "ended today")
        self.assertIn("Docs pass", words(row(running, "impl-g7")))
        self.assertIn("01:00", words(row(ended, "impl-f6")).split())
        self.assertIn(column(running, row(running, "impl-a1"), "token"), token_forms(TOKENS + 1000))


if __name__ == "__main__":
    unittest.main()
