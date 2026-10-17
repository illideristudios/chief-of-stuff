#!/usr/bin/env python3
"""Serve the workspace's pages locally: the board and any page a session writes beside it.

The pages are routes: `/` the board, `/workers` the running workers and today's ends, `/decisions` the list,
`/decisions/<slug>` a decision page, `/issues/<n>` a tracker issue's page, `/issues/<n>/source` its change's source
and diff. Each is rendered into its file (`<day>-board.html`, `workers.html`, `decisions.html`, `decision-<slug>.html`,
`issue-<n>.html`, `issue-<n>-source.html`), which only the routes serve: a request for the file's own name is a 404.
A GET re-renders a page when one of its sources (the trackers, CLAUDE.md, the settings TOML, the decision JSON,
`.sources.json`) is newer than its last render, or differs in bytes from what that render read, and `/workers` on every GET (it reads the session logs too), and a thread refreshes `.sources.json` from the forge every minute. An open tab
reloads itself when the file changes.

    python3 pages.py --ensure [--root R]              # start it unless it is already serving; print status only
    python3 pages.py serve --dir D --port P --root R  # what --ensure starts, detached

A decision page posts the user's answer to `POST /decisions/<slug>` (`key` and/or `words`, form-encoded).
The server saves it as the decision JSON's `answer` ({key, words, at}) for the coordinator's next pass, and
redirects back to the page, which then shows the decision answered.

The `## Coordinator` block's `Board:` line names both halves: `URL http://127.0.0.1:<port>/` and
``dir `<pages dir>` ``. It listens on 127.0.0.1 only, and it answers only requests whose Host is
127.0.0.1 or localhost. `/` is today's board once today's tracker exists, else the newest one; `/all` lists every other file a session
wrote there. Binding to loopback does not stop a page elsewhere from rebinding its own
name to 127.0.0.1, but that page still sends its own name as the Host.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import http.server
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from stat import S_ISREG
from html import escape
from urllib.parse import parse_qs, quote, unquote, urlsplit

import forge
import page_reload
from workspace import LAST_LOOKED, LOOKED_BEFORE, ConfigError, daily_trackers, pages_address, read_config, record_look, unrecord_look

SERVER = "chief-of-stuff-pages"
PID = ".pid"
CACHE = ".sources.json"
REFRESH = 60  # seconds between forge reads
RENDERED = re.compile(r"\d{4}-\d{2}-\d{2}-board\.html|workers\.html|decisions\.html|decision-[a-z0-9]+(?:-[a-z0-9]+)*\.html|issue-(?:\d+|k[0-9a-f]+)(?:-source)?\.html")
ROUTE = re.compile(r"/decisions/([a-z0-9]+(?:-[a-z0-9]+)*)")
ISSUE = re.compile(r"/issues/([a-z0-9_.-]+:[A-Za-z0-9_./-]+#\d+|\d+)(/source)?")
MAX_BODY = 16 * 1024  # a write-in is a sentence or a paragraph
MAX_PAGE = 1024 * 1024  # rendered pages are a few hundred KB; refuse larger rather than load them
# ponytail: a lock per page (never freed; a workspace has tens of pages) under the server's `[pages] workers` slots.
# An answer saved mid-render of another page (the board) can leave that page stale until the next forge refresh.
PAGE_LOCKS: dict[str, threading.Lock] = {}
PAGE_LOCKS_GUARD = threading.Lock()
# Each page's last render by this server: (when it started, ns; the file's time after it; the sha256 of the
# sources' bytes it consumed). Apart from the file's time, which is only when its bytes last changed (the
# Last-Modified a tab reloads on). A page with no record, or whose file changed since (an older release's, an
# editor's), is judged by its file's time; a None file time marks it stale. The digest decides freshness where
# the record is this server's own render of this file: this filesystem's mtime clock reads behind the wall
# clock by more than a render takes under load, so no mtime comparison can order a source saved just after a
# render began against that render — the bytes can.
RENDERED_AT: dict[str, tuple[int, int | None, str]] = {}


def page_lock(path: Path | str) -> threading.Lock:
    """The lock one page's renders and its answer saves take, so neither overlaps another of the same page."""
    with PAGE_LOCKS_GUARD:
        return PAGE_LOCKS.setdefault(str(path), threading.Lock())


def _plugin_version() -> str:
    try:
        return json.loads((Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return "0"


VERSION = _plugin_version()
# The renderer's own code counts as a source: a release upgrade changes it, not the sources.
import fragment  # light (html, re, pathlib); the renderers stay lazy for --ensure

RENDERER_MTIME_NS = max(p.stat().st_mtime_ns for p in (*Path(__file__).resolve().parent.glob("*.py"), fragment.THEME_PATH) if p.is_file())


def _version(server_header: str) -> tuple[int, ...]:
    """The version a Server header names; a server from before versions were sent is (0,)."""
    m = re.match(rf"{SERVER}/(\d+(?:\.\d+)*)", server_header)
    return tuple(int(x) for x in m[1].split(".")) if m else (0,)


class PagesError(RuntimeError):
    """The pages cannot be served: no Board line, or the port is someone else's."""


def _sources(root: Path, cfg, pages_dir: Path, day: str) -> list[Path]:
    return [root / "CLAUDE.md", *([root / cfg.settings_path] if cfg.settings_path else []), root / cfg.log_path(day),
            root / "store.db", root / "store.db-wal", *(path for _, path in daily_trackers(root, cfg)), *pages_dir.glob("decision-*.json"),
            pages_dir / CACHE]


def _digest(sources: list[Path]) -> str:
    """sha256 of the sources' bytes, for a render record: content, not times, says whether a render's inputs changed."""
    h = hashlib.sha256()
    for p in sources:
        if p.is_file():
            h.update(str(p).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def today_board(root: Path) -> str | None:
    """Today's board name once today's tracker or store exists, else None."""
    cfg = read_config(root)
    day = datetime.now(cfg.zone).date().isoformat()
    return f"{day}-board.html" if (root / cfg.tracker_path(day)).is_file() or (root / "store.db").is_file() else None


def _page_data(root: Path):
    """The driver opens under the migrate lock; use cases hand detached rows to presenters."""
    import store_access
    import task_forge
    if not (root / "store.db").is_file():
        return None
    db = store_access.migrated_store(root)
    try:
        with db.transaction(write=False):
            return task_forge.page_data(db)
    finally:
        db.engine.dispose()


def write_board(root: Path, day: str | None = None, *, now: datetime | None = None):
    """The board command's driver, shared with request rendering."""
    import render_board
    return render_board.write(root, day, now=now, db=_page_data(root))


def _navigation(root: Path, pages_dir: Path, data):
    import decision_actions
    if data is None:
        return {}
    return decision_actions.tab_counts(data.items(), data.decisions(), data.segments())


def _issue_identity(name: str, source: bool = False):
    token = name[len("issue-"):-len("-source.html") if source else -5]
    return bytes.fromhex(token[1:]).decode() if token.startswith("k") else int(token)


def fresh(root: Path, pages_dir: Path, name: str, slots=contextlib.nullcontext()) -> str:
    """Re-render the page `name` when a source is newer than its last render, or differs in bytes from what that
    render consumed, inside one of `slots`. Returns the render error, or "". On an error the last good file stays
    where it is."""
    # The renderers are imported here, not at the top, to keep `pages.py --ensure` light.
    import decision_page
    import issue_page
    import render_board
    import source_page
    import workers_page
    # Defence in depth, shaped for CodeQL's py/path-injection: `name` is a plain page file name whose resolved path sits
    # directly in pages_dir, and everything after uses the checked path (and the name taken from it), not the raw one.
    if not RENDERED.fullmatch(name):
        return ""
    base = os.path.realpath(pages_dir)
    checked = os.path.realpath(os.path.join(base, name))
    if not checked.startswith(base + os.sep):
        return ""
    target = Path(checked)
    name = target.name
    key = str(pages_dir / name)  # what the handler's page_lock and RENDERED_AT use, so the locks and records agree
    try:
        cfg = read_config(root)
        today = datetime.now(cfg.zone).date().isoformat()
        if name.startswith("decision-"):
            if not (pages_dir / f"{name[:-5]}.json").is_file():
                return ""
            day = today
        elif name.startswith("issue-") or name == "workers.html":
            day = today
        elif name == "decisions.html":
            days = [d.isoformat() for d, _ in daily_trackers(root, cfg)]
            day = today if today in days or not days else days[-1]
        else:
            day = name[:10]
            if not (root / cfg.tracker_path(day)).is_file() and not (root / "store.db").is_file():
                return ""
        with page_lock(key):  # checked under the lock: a request that waited finds the page already rendered
            sources = [*_sources(root, cfg, pages_dir, day),
                       *([pages_dir / LOOKED_BEFORE] if name.endswith("-board.html") else []), fragment.THEME_PATH]
            newest = max(RENDERER_MTIME_NS, max((p.stat().st_mtime_ns for p in sources if p.is_file()), default=0))
            mtime = target.stat().st_mtime_ns if target.is_file() else 0
            digest = _digest(sources)
            record = RENDERED_AT.get(key)
            rendered = record[0] if record and record[1] in (mtime, None) else mtime
            if rendered >= newest and name != "workers.html":
                # The filesystem's mtime clock reads behind the wall clock by more than a render takes under load
                # here, so a source saved after that render began can carry an mtime from before it. Where the
                # record is this server's render of this file, the sources' bytes settle it: fresh only if they
                # are still exactly what that render consumed. A source touched without a change keeps the mtime
                # path's one confirming render — it re-renders, records the same digest, and the polls after it
                # are fresh.
                if not (record and record[1] == mtime and record[2] != digest):
                    return ""
            started = time.time_ns()
            with slots:
                if name.startswith("decision-"):
                    decision_page.write(root, pages_dir, name[len("decision-"):-5], day)
                else:
                    data = _page_data(root)
                    if name == "workers.html" or name.startswith("issue-"):
                        navigation = _navigation(root, pages_dir, data)
                        if name == "workers.html":
                            workers_page.write(root, pages_dir, db=data, navigation=navigation)
                        elif name.endswith("-source.html"):
                            source_page.write(root, pages_dir, _issue_identity(name, source=True),
                                              db=data, navigation=navigation)
                        else:
                            issue_page.write(root, pages_dir, _issue_identity(name), db=data, navigation=navigation)
                    else:
                        render_board.write(root, day, db=data)
            RENDERED_AT[key] = started, target.stat().st_mtime_ns if target.is_file() else 0, digest  # only after a render that did not raise
        return ""
    except Exception as e:  # any renderer failure: the server keeps serving the last good page
        return f"{type(e).__name__}: {e}"


class Refresher(threading.Thread):
    """Calls `refresh(root, now)` every `every` seconds, and at once when `wake` is set."""

    def __init__(self, root: Path, refresh, every: float):
        super().__init__(daemon=True)
        self.root, self.refresh, self.every, self.wake = root, refresh, every, threading.Event()

    def run(self):
        while True:
            try:
                self.refresh(self.root, datetime.now().astimezone())
            except Exception as e:  # refresh never raises by contract; a bug in it must not end the thread
                print(f"pages: sources refresh failed: {e}", file=sys.stderr, flush=True)
            self.wake.wait(self.every)
            self.wake.clear()


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = f"{SERVER}/{VERSION}"
    banner = ""

    def _allowed(self) -> bool:
        """Refuse a foreign Host or a dotfile; map `/` to today's (else the newest) board, `/all` to the listing and
        `/workers`, `/decisions[/<slug>]` and `/issues/<n>` to its file; refuse a rendered page's file name; re-render a page its sources outdate."""
        self.banner = ""  # per request: keep-alive reuses the handler (#334)
        port = self.server.server_address[1]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.send_error(403, "Host is not this machine")
            return False
        path = re.split(r"[?#]", self.path, maxsplit=1)[0]
        decoded = unquote(path)
        if "\0" in decoded or (decoded != "/" and decoded.endswith("/")):  # #340: null or a directory URL, `%2F` too
            self.send_error(404)
            return False
        if any(part.startswith(".") for part in decoded.split("/")):  # `%2e` is a dot too
            self.send_error(404)
            return False
        root = getattr(self.server, "root", None)
        self._poke()
        look = False
        if path == "/":
            # Served in place, not redirected: the tab stays on `/`, so its reload poll finds the next day's board.
            name = None
            if root is not None:
                try:
                    name = today_board(root)
                except Exception:  # an unreadable CLAUDE.md: fall back to the newest board on disk
                    pass
            boards = sorted(Path(self.directory).glob("*-board.html"))
            if name is None and not boards:
                self.send_error(404, "no board rendered yet")
                return False
            # A person's load (#229): a browser navigation (an agent's GET is none) that is not the reload snippet's
            # own (its cookie), with a board to serve.
            look = (self.command == "GET" and self.headers.get("Sec-Fetch-Mode") == "navigate"
                    and page_reload.RELOAD_COOKIE not in self.headers.get("Cookie", ""))
            self.path = "/" + (name or boards[-1].name)
        elif path == "/all":
            self.path = "/"
        elif path in ("/workers", "/decisions"):
            self.path = f"{path}.html"
        elif path.startswith("/decisions/"):
            m = ROUTE.fullmatch(path)
            if not m or not (Path(self.directory) / f"decision-{m[1]}.json").is_file():
                self.send_error(404, "no such decision")
                return False
            self.path = f"/decision-{m[1]}.html"
        elif decoded.startswith("/issues/"):
            m = ISSUE.fullmatch(decoded)
            if not m:
                self.send_error(404, "no such issue")
                return False
            self.path = "/" + fragment.issue_page_name(m[1], bool(m[2]))
        elif RENDERED.fullmatch(Path(self.translate_path(path)).name.casefold()):
            # A rendered page is a route only. casefold: long s and a case-insensitive disk reach it too (#335).
            self.send_error(404)
            return False
        name = re.split(r"[?#]", self.path, maxsplit=1)[0].lstrip("/")
        if root is not None and RENDERED.fullmatch(name):
            # A look is recorded before `fresh`, so the board counts from the look before, and taken back when the
            # board cannot render; one lock around it all, so two tabs loading at once cannot interleave it.
            pages = Path(self.directory)
            with page_lock(pages / LAST_LOOKED) if look else contextlib.nullcontext():
                prior = None
                if look:
                    with contextlib.suppress(OSError):
                        prior = record_look(pages, datetime.now().astimezone())
                self.banner = fresh(root, pages, name, self.server.slots)
                if self.banner and prior is not None:
                    with contextlib.suppress(OSError):
                        unrecord_look(pages, prior)
        if name.startswith("issue-") and not self.banner and not (Path(self.directory) / name).is_file():
            self.send_error(404, "no task names this issue")  # issue_page.write removes the page then
            return False
        return True

    def _poke(self):
        """Wake the refresher when the cache is older than its interval; the GET never waits for it."""
        refresher = getattr(self.server, "refresher", None)
        if refresher is None:
            return
        try:
            stale = time.time() - (Path(self.directory) / CACHE).stat().st_mtime > refresher.every
        except OSError:
            stale = True
        if stale:
            refresher.wake.set()

    def _send_html(self, body: bool):
        """An HTML page with the shared reload check; with the banner set, the last good page plus one line saying the
        render failed (never a blank 500). HEAD's length is GET's, and Last-Modified stays the file's own.
        A page over MAX_PAGE is refused without reading; HEAD without a banner never reads (#333)."""
        path = Path(self.translate_path(self.path))
        try:
            stat = path.stat()
            if not S_ISREG(stat.st_mode):
                raise OSError  # a directory, FIFO or device is not a page
            if stat.st_size > MAX_PAGE:
                return self.send_error(413, f"a page is at most {MAX_PAGE} bytes")
            stamp = stat.st_mtime
            if not body and not self.banner:
                with path.open("rb"):  # GET would read it: an unreadable page is GET's 404, not a 200
                    pass
                # The injected HTTP date is fixed length; count the snippet without reading the file.
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(stat.st_size + len(page_reload.ENCODED)))
                self.send_header("Last-Modified", self.date_time_string(stamp))
                self.end_headers()
                return
            page = path.read_bytes()
        except OSError:
            if not self.banner:  # no page, or it vanished: a 404, never a blank 200 stamped 1970
                return self.send_error(404)
            page, stamp = b"<!doctype html>\n<meta charset=\"utf-8\">\n<body>\n</body>\n", 0
        if self.banner:
            stamp = max(stamp - 1, 0)  # its own stable time: entering and leaving the banner each move it once, staying never does
            note = ('<p role="alert" style="margin:0;padding:6px 16px;background:#c9533a;color:#fff;'
                    f'font:14px/1.4 system-ui,sans-serif">not re-rendered: {escape(self.banner)}</p>').encode()
            page = re.sub(rb"<body[^>]*>", lambda m: m[0] + note, page, count=1) if b"<body" in page else note + page
        last_modified = self.date_time_string(stamp)
        page = page_reload.inject(page, last_modified)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Last-Modified", last_modified)
        self.end_headers()
        if body:
            self.wfile.write(page)

    def _is_html(self) -> bool:
        return bool(self.banner) or unquote(urlsplit(self.path).path).lower().endswith(".html")

    def do_GET(self):
        if self._allowed():
            self._send_html(True) if self._is_html() else super().do_GET()

    def _post_store_decision(self, form: dict[str, list[str]]):
        import decision_actions
        import forge
        import store_access

        root = getattr(self.server, "root", None)
        dec_id = form.get("decision_id", [""])[0].strip()
        if root is None or not dec_id.isdigit():
            return self.send_error(404, "no such decision")
        action = form.get("action", ["answer"])[0].strip()
        answer_val = form.get("answer", [""])[0].strip()
        target_stage = form.get("target_stage", [""])[0].strip()
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
        import sqlite3
        from dataclasses import asdict
        from sqlalchemy.exc import OperationalError
        from supervisor import ServiceError
        from settings import load_policy

        db = None
        try:
            db = store_access.migrated_store(root)
            # Reserve before checking state: a competing POST or engine write cannot interleave.
            def apply():
                dec = db.decision(int(dec_id))
                if dec is None:
                    return (404, "no such decision")
                item = db.item(dec.issue_key)
                if action == "answer" and not answer_val:
                    return (400, "an answer must contain text")
                policies = load_policy()
                history = db.segments(dec.issue_key)
                stage = target_stage if action in ("reroute", "re-route") else (item.stage if item else dec.gate)
                if action == "reverse":
                    affected = next((s.stage for s in reversed(history) if s.stage not in {"review", "merge"}), None)
                    stage = dec.gate if dec.gate in policies.stages and dec.gate != "merge" else affected or stage
                    if stage == "merge":
                        stage = policies.workflow_for(None).start
                if action != "answer" and stage not in policies.stages:
                    return (400, "unknown target stage")
                persona = policies.stages[stage].persona or "" if stage in policies.stages else ""
                attempt = max((s.attempt for s in history if s.stage == stage), default=0) + 1
                try:
                    if action == "answer":
                        tr = decision_actions.answer(dec, answer_val, decided_by="user", answered_at=now_iso)
                    elif action == "reverse":
                        tr = decision_actions.reverse(dec, answer_val, answered_at=now_iso,
                                                      stage=stage, persona=persona, attempt=attempt)
                    elif action in ("reroute", "re-route"):
                        tr = decision_actions.reroute(dec, target_stage, answer_text=answer_val,
                                                      answered_at=now_iso, current_stage=item.stage if item else "",
                                                      persona=persona, attempt=attempt)
                    else:
                        return (400, "unknown decision action")
                except ValueError as exc:
                    return (409, str(exc))
                fg = forge.for_workspace(root)
                if fg is None:
                    return (409, "no forge configured")
                for fw in tr.actions:
                    if isinstance(fw, decision_actions.ForgeStageWrite):
                        number = int(fw.issue_key.rpartition("#")[2])
                        try:
                            result = fg.write_stage(number, fw.stage, expected=fw.previous_stage or None, commit=True)
                        except Exception as exc:
                            db.add_log(dec.issue_key, f"decision stage write failed: {exc}", now_iso)
                            return (409, str(exc))
                        if not result.ok or not result.done:
                            error = result.error or "stage write was not committed"
                            db.answer_decision(dec.id, tr.decision.answer, status="blocked", blocked_reason=error)
                            return (409, error)
                        db.upsert_item(fw.issue_key, stage=fw.stage)
                data = db.resume(dec.issue_key) or {}
                if tr.redispatch is not None:
                    data["redispatch"] = asdict(tr.redispatch)
                comments = list(data.get("decision_comments", []))
                for fw in tr.actions:
                    if isinstance(fw, decision_actions.ForgeComment):
                        try:
                            error = fg.comment(int(fw.issue_key.rpartition("#")[2]), fw.body, commit=True)
                        except Exception as exc:
                            error = str(exc)
                        if error:
                            comments.append(fw.body)
                            db.add_log(dec.issue_key, f"decision comment failed: {error}", now_iso)
                if comments:
                    data["decision_comments"] = comments
                db.set_resume(dec.issue_key, data)
                db.update_decision(tr.store_write)
            with db.transaction():
                error = apply()
            if error is not None:
                return self.send_error(*error)
        except (ServiceError, sqlite3.OperationalError, OperationalError) as exc:
            return self.send_error(503, f"store unavailable: {exc}")
        finally:
            if db is not None:
                db.engine.dispose()
        for page in Path(self.directory).iterdir():
            if RENDERED.fullmatch(page.name):
                RENDERED_AT[str(page)] = 0, None, ""
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        """Save the user's answer beside the decision. Only this machine's own pages may post: the Host and, when a
        browser sends one, the Origin must be this server's."""
        port = self.server.server_address[1]
        own = (f"127.0.0.1:{port}", f"localhost:{port}")
        origin = self.headers.get("Origin")
        if self.headers.get("Host", "") not in own or (origin is not None and origin not in [f"http://{h}" for h in own]):
            return self.send_error(403, "not a page of this server")
        route_path = re.split(r"[?#]", self.path, maxsplit=1)[0]
        if route_path == "/":
            try:
                size = int(self.headers["Content-Length"])
            except (TypeError, ValueError):
                return self.send_error(411)
            if size > MAX_BODY:
                self.close_connection = True
                return self.send_error(413, f"an answer is at most {MAX_BODY} bytes")
            form = parse_qs(self.rfile.read(size).decode("utf-8", "replace"))
            return self._post_store_decision(form)
        m = ROUTE.fullmatch(route_path)
        source = Path(self.directory) / f"decision-{m[1]}.json" if m else None
        if source is None or not source.is_file():
            return self.send_error(404, "no such decision")
        try:
            size = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            return self.send_error(411)
        if size > MAX_BODY:
            self.close_connection = True
            return self.send_error(413, f"an answer is at most {MAX_BODY} bytes")
        form = parse_qs(self.rfile.read(size).decode("utf-8", "replace"))
        key, words = form.get("key", [""])[0], form.get("words", [""])[0].strip()
        import decision_page
        with page_lock(Path(self.directory) / f"decision-{m[1]}.html"):
            try:
                d = decision_page.parse(json.loads(source.read_text()))
            except decision_page.BAD as e:
                return self.send_error(409, f"the decision does not read: {e}")
            if (key and key not in [o["key"] for o in d["options"]]) or not (key or words):
                return self.send_error(400, "an answer is one of the options' keys, or words")
            d["answer"] = {"key": key or None, "words": words, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
            stat = source.stat()
            tmp = source.with_name(f".{source.name}.tmp")
            tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
            os.replace(tmp, source)
            # The file's time is when it was asked (decision_page.Context.asked), so it keeps it; the pages it
            # outdates are marked stale instead (their render record cleared), and the next GET renders them.
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            for page in Path(self.directory).iterdir():
                if RENDERED.fullmatch(page.name):
                    RENDERED_AT[str(page)] = 0, None, ""
        self.send_response(303)
        self.send_header("Location", "/decisions" if form.get("back") == ["/decisions"] else f"/decisions/{m[1]}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        if self._allowed():
            self._send_html(False) if self._is_html() else super().do_HEAD()

    def list_directory(self, path):
        # The stdlib listing, without dotfiles (the pid file is the server's) or rendered pages (routes, not files).
        names = sorted(p.name for p in Path(path).iterdir() if not p.name.startswith(".") and not RENDERED.fullmatch(p.name))
        body = "".join(f'<li><a href="/{quote(n)}">{escape(n)}</a></li>' for n in names).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    def guess_type(self, path):
        # Every page here is written as UTF-8; without a charset a browser falls back to Windows-1252.
        kind = super().guess_type(path)
        return f"{kind}; charset=utf-8" if kind.startswith("text/") else kind

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        if page_reload.RELOAD_COOKIE in ((getattr(self, "headers", None) or {}).get("Cookie") or ""):
            # Consumed by the request that carries it, so it cannot mark another tab's navigation (Codex on #579).
            self.send_header("Set-Cookie", f"{page_reload.RELOAD_COOKIE.split('=')[0]}=; Max-Age=0; Path=/")
        super().end_headers()

    def log_message(self, *_args):
        pass


def _workers(root: Path | None) -> int:
    """`[pages] workers` from the workspace settings; the default when they cannot be read (a render shows why)."""
    from settings import Pages, load
    try:
        cfg = read_config(root)
        return load(root, cfg.settings_path).pages.workers
    except Exception:  # no root, no CLAUDE.md, or a settings file that does not read
        return Pages.workers


def make_server(pages_dir: Path, port: int, root: Path | None = None, refresh=None,
                every: float = REFRESH) -> http.server.ThreadingHTTPServer:
    """With `root`, pages render on request and a Refresher keeps `.sources.json` current."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                             functools.partial(Handler, directory=str(pages_dir)))
    server.root, server.refresher, server.slots = root, None, threading.BoundedSemaphore(_workers(root))
    if root is not None:
        if refresh is None:
            from board_sources import refresh
        server.refresher = Refresher(root, refresh, every)
        server.refresher.start()
    return server


def _who(port: int) -> str | None:
    """The Server header on the port, "" for a server that sends none, None when nothing answers.
    Raises PagesError when the connect is denied: a sandbox says nothing about what runs on the port."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/all", method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=1) as resp:
            return resp.headers.get("Server", "")
    except urllib.error.HTTPError as e:
        return e.headers.get("Server", "")
    except OSError as e:
        if isinstance(e, PermissionError) or isinstance(getattr(e, "reason", None), PermissionError):
            raise PagesError(f"cannot reach port {port}: the connection was denied (a sandboxed session?); "
                             "check it outside the sandbox") from e
        return None


def is_pages_serve(pid: int, port: int | None = None) -> bool:
    """True when `pid`'s command line is `pages.py serve`, and on `port` when given. A reused pid is someone else's."""
    try:
        command = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except OSError:
        return False  # cannot verify identity: do not signal
    if "pages.py serve" not in command:
        return False
    return port is None or bool(re.search(rf"--port {port}(\s|$)", command))


def _stop(pages_dir: Path, port: int) -> None:
    """Stop the older pages server on the port. Only the pid file's process, and only when it runs
    `pages.py serve` on this port: a reused pid is someone else's."""
    try:
        pid = int((pages_dir / PID).read_text())
    except (OSError, ValueError):
        raise PagesError(f"an older pages server answers on port {port}, and no pid file names it; stop it by hand") from None
    if not is_pages_serve(pid, port):
        raise PagesError(f"an older pages server answers on port {port}, but pid {pid} is not it; stop it by hand")
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if _who(port) is None:
            return
        time.sleep(0.1)
    raise PagesError(f"the older pages server (pid {pid}) is still answering on port {port}")


def ensure(pages_dir: Path, port: int, log: Path, root: Path | None = None) -> str:
    """Start the server unless ours already answers on the port; replace one older than this code. Never
    moves to another port: the URL is the board's address, and a board that wanders is a board nobody has open."""
    who = _who(port)
    restart = False
    if who is not None:
        if not who.startswith(SERVER):
            raise PagesError(f"port {port} answers as {who or 'an unnamed server'}, not the pages server")
        if _version(who) >= _version(f"{SERVER}/{VERSION}"):
            return "pages: serving"
        _stop(pages_dir, port)
        restart = True
    pages_dir.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, str(Path(__file__).resolve()), "serve", "--dir", str(pages_dir), "--port", str(port)]
    with log.open("a") as out:
        proc = subprocess.Popen(argv + (["--root", str(root.resolve())] if root is not None else []),
                                stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        if (_who(port) or "").startswith(SERVER):
            (pages_dir / PID).write_text(str(proc.pid))  # .pid names only a server that answered (#248)
            return "pages: restarted" if restart else "pages: started"
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:  # failed start must not leave a detached server with no pid file (#493)
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
    raise PagesError(f"the pages server did not come up on port {port}; see {log}")


def from_config(root: Path) -> tuple[Path, int]:
    """(pages dir, port) from the block's Board line."""
    try:
        return pages_address(root)
    except (OSError, ConfigError) as e:
        raise PagesError(str(e)) from None


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["--board"]:
        import render_board
        render_board.main(args_list[1:], writer=write_board)
        return 0
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ensure", action="store_true", help="start the server unless it is serving; print status only")
    ap.add_argument("--root", help="workspace root holding CLAUDE.md (default: the current directory)")
    ap.add_argument("command", nargs="?", choices=("serve",))
    ap.add_argument("--dir")
    ap.add_argument("--port", type=int)
    args = ap.parse_args(args_list)
    if args.command == "serve":
        make_server(Path(args.dir), args.port, Path(args.root) if args.root else None).serve_forever()
        return 0
    root = Path(args.root or ".")
    try:
        pages_dir, port = from_config(root)
        print(ensure(pages_dir, port, root / ".chief-of-stuff" / "pages.log", root))
    except PagesError as e:
        print(f"pages: not served — {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
