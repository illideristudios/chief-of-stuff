"""pages.py: the workspace's pages, served from this Mac (Zach, 2026-09-24 14:07: "we should host our own
webserver and ensure they are set up as part of the agent's boot loop")."""

import collections
import email.utils
import hashlib
import http.client
import json
import http.server
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pages as pg  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port: int, path: str, host: str | None = None, method: str = "GET", headers: dict | None = None) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, headers={"Host": host or f"127.0.0.1:{port}", **(headers or {})})
    resp = conn.getresponse()
    resp.body = resp.read()
    conn.close()
    return resp


def post(port: int, path: str, body: bytes, host: str | None = None, origin: str | None = None) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}", "Content-Type": "application/x-www-form-urlencoded"}
    if origin:
        headers["Origin"] = origin
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    resp.body = resp.read()
    conn.close()
    return resp


def pages_dir() -> Path:
    root = Path(tempfile.mkdtemp())
    for name in ("2026-09-23-board.html", "2026-09-24-board.html", "arch-review.html"):
        (root / name).write_text(f"<p>{name}</p>")
    (root / ".pid").write_text("1")
    return root


class ServeTest(unittest.TestCase):
    def setUp(self):
        self.dir = pages_dir()
        self.server = pg.make_server(self.dir, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_root_serves_the_newest_board(self):
        # The tab stays on `/`, so its reload poll sees the next day's board, not only today's file.
        self.assertEqual(get(self.port, "/").status, 200)
        self.assertTrue(get(self.port, "/").body.startswith(b"<p>2026-09-24-board.html</p>"))
        self.assertEqual(get(self.port, "/", method="HEAD").status, 200)
        (self.dir / "2026-09-25-board.html").write_text("<p>next</p>")
        self.assertTrue(get(self.port, "/").body.startswith(b"<p>next</p>"))

    def test_a_page_is_served_uncached_and_names_the_server(self):
        resp = get(self.port, "/arch-review.html")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.body.startswith(b"<p>arch-review.html</p>"))  # then the shared reload check (#330)
        self.assertEqual(resp.getheader("Cache-Control"), "no-cache")
        self.assertTrue(resp.getheader("Server").startswith(pg.SERVER))

    def test_text_is_served_as_utf8(self):
        # A page with no <meta charset> was read as Windows-1252: "Decision · x" showed as "Decision Â· x".
        self.assertEqual(get(self.port, "/arch-review.html").getheader("Content-Type"), "text/html; charset=utf-8")

    def test_all_lists_every_page(self):
        resp = get(self.port, "/all")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"arch-review.html", resp.body)
        # A rendered page is a route, not a file (the user, 2026-09-26: "get rid of the .html versions entirely").
        for hidden in (b".pid", b"-board.html"):
            self.assertNotIn(hidden, resp.body)

    def test_localhost_is_served(self):
        self.assertEqual(get(self.port, "/arch-review.html", host=f"localhost:{self.port}").status, 200)

    def test_another_host_is_refused(self):
        # A page elsewhere can rebind its own name to 127.0.0.1; the Host it sends is still its own.
        self.assertEqual(get(self.port, "/arch-review.html", host="evil.test").status, 403)
        self.assertEqual(get(self.port, "/arch-review.html", host=f"evil.test:{self.port}").status, 403)

    def test_dotfiles_are_not_served(self):
        self.assertEqual(get(self.port, "/.pid").status, 404)


def snippet(last_modified: str = "Thu, 01 Jan 1970 00:00:00 GMT") -> str:
    """The shared reload check. Imported here, not at the top, so only the tests of it are red while it is missing."""
    import page_reload
    return page_reload.inject(b"", last_modified).decode()


class ReloadSnippetTest(unittest.TestCase):
    """Every page the server serves carries one shared reload check, injected at serve time (#330: "I'd like loaded
    pages to auto-refresh if the underlying page is changed by an agent").
    These pin the snippet's text; test_page_reload_node.py runs its branches in node against a stub page (#337)."""

    ROUTES = {"/": "2026-09-24-board.html", "/workers": "workers.html", "/decisions": "decisions.html",
              "/decisions/cache-ttl": "decision-cache-ttl.html", "/issues/7": "issue-7.html",
              "/issues/7/source": "issue-7-source.html", "/arch-review.html": "arch-review.html"}
    MTIME = 1_800_000_000

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for name in self.ROUTES.values():
            (self.dir / name).write_text(f"<!doctype html><body><p>{name}</p></body>")
            os.utime(self.dir / name, (self.MTIME, self.MTIME))
        (self.dir / "decision-cache-ttl.json").write_text("{}")
        (self.dir / "notes.txt").write_bytes(b"plain notes, no markup\n")
        (self.dir / "data.json").write_bytes(b'{"a": "</body>"}')
        self.server = pg.make_server(self.dir, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_every_page_kind_carries_the_snippet_once_before_the_last_body_end(self):
        for path in self.ROUTES:
            with self.subTest(path=path):
                got = get(self.port, path)
                body = got.body
                snip = snippet(got.getheader("Last-Modified")).encode()
                self.assertEqual(body.count(snip), 1)
                self.assertLess(body.index(snip), body.rindex(b"</body>"))
                self.assertIn(f"<p>{self.ROUTES[path]}</p>".encode(), body)

    def test_head_matches_get_and_keeps_the_files_time(self):
        # The snippet's poll is a HEAD: a different length or time than GET's would make every poll a "change".
        for path in self.ROUTES:
            with self.subTest(path=path):
                got, head = get(self.port, path), get(self.port, path, method="HEAD")
                self.assertEqual(head.getheader("Content-Length"), str(len(got.body)))
                self.assertEqual(head.body, b"")
                self.assertEqual(head.getheader("Last-Modified"), got.getheader("Last-Modified"))
                self.assertEqual(got.getheader("Last-Modified"), email.utils.formatdate(self.MTIME, usegmt=True))
                self.assertIn(f"var seen={json.dumps(got.getheader('Last-Modified'))};".encode(), got.body)

    def test_a_page_without_a_body_end_gets_it_appended_and_the_last_one_is_where_it_goes(self):
        (self.dir / "arch-review.html").write_text("<p>no body</p>")
        got = get(self.port, "/arch-review.html")
        body, snip = got.body, snippet(got.getheader("Last-Modified")).encode()
        self.assertEqual((body.count(snip), body.startswith(b"<p>no body</p>"), body.endswith(snip)), (1, True, True))
        (self.dir / "arch-review.html").write_text('<body><script>var s="</body>"</script></body>')
        got = get(self.port, "/arch-review.html")
        body, snip = got.body, snippet(got.getheader("Last-Modified")).encode()
        self.assertEqual((body.count(snip), body.index(snip) > body.index(b'"</body>"'), body.endswith(b"</body>")), (1, True, True))

    def test_a_file_that_is_not_html_is_served_unchanged(self):
        for name, data in (("notes.txt", b"plain notes, no markup\n"), ("data.json", b'{"a": "</body>"}')):
            with self.subTest(name=name):
                resp = get(self.port, f"/{name}")
                self.assertEqual(resp.body, data)
                self.assertEqual(get(self.port, f"/{name}", method="HEAD").getheader("Content-Length"), str(len(data)))

    def test_the_snippet_polls_with_head_and_asks_before_it_reloads_over_typing(self):
        snip = snippet()
        self.assertTrue(snip.lstrip().startswith("<script") and snip.rstrip().endswith("</script>"))
        for needle in ("HEAD", "no-store", "location.pathname", "visibilitychange",
                       "Last-Modified", "defaultValue", "activeElement", "page-changed", "location.reload"):
            with self.subTest(needle=needle):
                self.assertIn(needle, "".join(snip.replace('"', "'").split()))  # quotes and spacing are the author's
        self.assertNotIn("document.lastModified", snip)  # #332: compare Last-Modified header to header
        for blocking in ("alert(", "confirm(", "prompt("):
            self.assertNotIn(blocking, snip)

    @staticmethod
    def code() -> tuple[str, str]:
        """The snippet's code without its comments, as written and with quotes unified and spacing dropped."""
        code = re.sub(r"//[^\n]*|/\*.*?\*/", "", snippet(), flags=re.S)
        return code, "".join(code.replace('"', "'").split())

    def test_the_snippet_guards_every_kind_of_user_state_a_reload_would_lose(self):
        # A reload drops a picked radio or checkbox, a chosen option and edited rich text, as it drops typed text.
        code, flat = self.code()
        self.assertRegex(flat, r"\.checked!==?\w+\.defaultChecked")
        for word in ("defaultChecked", "defaultSelected", "defaultValue", "tagName", "INPUT", "TEXTAREA", "SELECT"):
            with self.subTest(word=word):
                self.assertRegex(flat, rf"\b{word}\b")  # `SELECTX` is not SELECT
        self.assertIn("selectDirty", code)  # #339: select default is first non-disabled / none if multiple
        self.assertIn(".elements", flat)  # #388: form.elements includes form-attribute controls
        self.assertIn("isContentEditable", flat)  # in code, not only in a comment
        self.assertNotRegex(flat, r"false&&|&&false|\b0&&|&&0\b")  # a guard that can never be true guards nothing

    def test_the_snippet_counts_only_form_controls_as_unsaved_input(self):
        # R13: the source page's Unified/Split radios sit outside any form; picking Split is not input a reload loses.
        # #388: form.elements (not a descendant query) so a control tied by the form attribute still counts.
        _, flat = self.code()
        typing = flat.split("functiontyping()", 1)[1].split("functionnotice()", 1)[0]
        selectors = re.findall(r"some\('([^']*)'", typing)
        self.assertTrue(selectors, "found no `some('selector', ...)` dirty check")
        scoped = all("form" in part for sel in selectors for part in sel.split(","))
        self.assertTrue(scoped or "closest('form')" in flat, f"dirty checks reach controls outside a form: {selectors}")
        self.assertIn(".elements", flat)

    def test_the_source_pages_layout_radios_are_outside_any_form(self):
        # The premise of the test above: no form wraps them, so a form-scoped check never counts them.
        import source_page
        self.assertIn('type="radio"', source_page.LAYOUT)
        self.assertNotIn("<form", source_page.LAYOUT)
        self.assertNotIn("<form", Path(source_page.__file__).read_text())

    def test_the_snippet_compares_last_modified_headers_as_strings(self):
        # #332: Date.parse(document.lastModified) vs the header disagrees in a DST fall-back's repeated hour.
        code, flat = self.code()
        self.assertNotIn("document.lastModified", code)
        self.assertNotIn("Date.parse", code)
        self.assertIn("Last-Modified", flat)
        self.assertIn("t!=null&&t!==seen", flat)

    def test_the_snippet_listens_for_visibilitychange_without_replacing_the_pages_own_handler(self):
        flat = "".join(snippet().replace('"', "'").split())
        self.assertIn("addEventListener('visibilitychange'", flat)

    def test_inject_matches_the_body_end_in_any_case(self):
        import page_reload
        snip = page_reload.ENCODED  # the bytes inject() writes
        self.assertEqual(snip, snippet().encode())
        for end in (b"</BODY>", b"</Body>"):
            with self.subTest(end=end):
                out = page_reload.inject(b"<BODY><p>x</p>" + end + b"</HTML>", "Thu, 01 Jan 1970 00:00:00 GMT")
                self.assertEqual((out.count(snip), out.index(snip) < out.rindex(end), out.endswith(end + b"</HTML>")), (1, True, True))

    def test_inject_escapes_the_baseline_as_a_script_safe_json_string(self):
        stamp = '"\\\n</script>\u2028'
        snip = snippet(stamp)
        value = re.search(r"var seen=(.*);", snip)[1]
        self.assertEqual(json.loads(value), stamp)
        self.assertNotIn("<", value)
        self.assertEqual(snip.count("</script>"), 1)

    def test_an_upper_case_html_suffix_is_injected_like_html(self):
        (self.dir / "LOUD.HTML").write_text("<body><p>loud</p></body>")
        got = get(self.port, "/LOUD.HTML")
        body, snip = got.body, snippet(got.getheader("Last-Modified")).encode()
        self.assertEqual((body.count(snip), b"<p>loud</p>" in body), (1, True))

    def test_an_html_that_does_not_exist_or_is_not_a_file_is_404_for_get_and_head(self):
        (self.dir / "folder.html").mkdir()
        for path in ("/nope.html", "/folder.html"):
            for method in ("GET", "HEAD"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(get(self.port, path, method=method).status, 404)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no mkfifo")
    def test_a_fifo_named_like_a_page_is_404_and_never_blocks_a_handler(self):
        # R5: a FIFO is not a regular file; opening one for read waits for a writer, which a request must never do.
        os.mkfifo(self.dir / "pipe.html")
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                self.assertEqual(get(self.port, "/pipe.html", method=method).status, 404)  # a hang is the client's 5 s timeout

    def test_the_html_suffix_is_judged_on_the_path_the_file_is_served_from(self):
        # R18: a query or fragment ends the path; `.html` after it does not make notes.txt a page.
        for path in ("/notes.txt?.html", "/notes.txt#.html"):
            for method in ("GET", "HEAD"):
                with self.subTest(path=path, method=method):
                    resp = get(self.port, path, method=method)
                    self.assertEqual(resp.status, 200)
                    self.assertTrue(resp.getheader("Content-Type").startswith("text/plain"), resp.getheader("Content-Type"))
                    self.assertEqual(resp.getheader("Content-Length"), str(len(b"plain notes, no markup\n")))
                    if method == "GET":
                        self.assertEqual(resp.body, b"plain notes, no markup\n")

    def test_a_page_that_vanishes_after_the_file_check_is_404_not_a_blank_page_stamped_1970(self):
        real = Path.read_bytes

        def vanish(path):
            if path.name == "arch-review.html":
                raise FileNotFoundError(path)
            return real(path)

        with mock.patch.object(Path, "read_bytes", vanish):
            resp = get(self.port, "/arch-review.html")
        self.assertEqual(resp.status, 404)
        self.assertNotIn("1970", resp.getheader("Last-Modified") or "")

    def test_odd_page_urls_are_404(self):
        # #340: %00 closed with no response; /sub/ listed without the snippet; /dir.html/ was an empty 200.
        (self.dir / "sub").mkdir()
        (self.dir / "dir.html").mkdir()
        for path in ("/%00", "/sub/", "/dir.html/", "/sub/%2F", "/dir.html%2F", "/%2F", "/sub/#x", "/dir.html/#x"):
            for method in ("GET", "HEAD"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(get(self.port, path, method=method).status, 404)

    def test_trailing_slash_tab_routes_match_the_bare_routes(self):
        # #988: a pasted or typed `/workers/` or `/decisions/` must not be a dead end; the query string stays ignored.
        for bare, needle in (("/workers", b"<p>workers.html</p>"), ("/decisions", b"<p>decisions.html</p>")):
            for suffix in ("", "/", "?x=1", "/?x=1"):
                path = bare + suffix
                for method in ("GET", "HEAD"):
                    with self.subTest(path=path, method=method):
                        resp = get(self.port, path, method=method)
                        self.assertEqual(resp.status, 200)
                        if method == "GET":
                            self.assertIn(needle, resp.body)
                        else:
                            self.assertEqual(resp.body, b"")
                            bare_get = get(self.port, bare)
                            self.assertEqual(resp.getheader("Content-Length"), str(len(bare_get.body)))

    def test_unsafe_paths_beside_tab_routes_still_404(self):
        # #988 must not weaken #340: only the exact tab routes gain a trailing slash, not lookalikes or escapes.
        (self.dir / "sub").mkdir()
        for path in ("/workers//", "/decisions//", "/workers/../workers", "/decisions/../decisions",
                     "/workers/extra", "/decisions/extra/", "/Workers/", "/Decisions/",
                     "/workers%2F%2F", "/sub/", "/%00", "/favicon.ico/"):
            for method in ("GET", "HEAD"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(get(self.port, path, method=method).status, 404)

    def test_a_missing_favicon_is_204_and_a_present_one_is_served(self):
        # #988: browsers ask for /favicon.ico; a 404 console noise is worse than an empty 204.
        for method in ("GET", "HEAD"):
            with self.subTest(missing=True, method=method):
                resp = get(self.port, "/favicon.ico", method=method)
                self.assertEqual(resp.status, 204)
                self.assertEqual(resp.body, b"")
                self.assertEqual(resp.getheader("Content-Length"), "0")
            with self.subTest(missing=True, method=method, query=True):
                self.assertEqual(get(self.port, "/favicon.ico?v=1", method=method).status, 204)
        icon = b"\x00\x00\x01\x00fake-ico"
        (self.dir / "favicon.ico").write_bytes(icon)
        for method in ("GET", "HEAD"):
            with self.subTest(missing=False, method=method):
                resp = get(self.port, "/favicon.ico", method=method)
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.getheader("Content-Length"), str(len(icon)))
                if method == "GET":
                    self.assertEqual(resp.body, icon)
                else:
                    self.assertEqual(resp.body, b"")
        self.assertEqual(get(self.port, "/favicon.ico", host="evil.test").status, 403)

    def test_a_case_folded_rendered_name_is_not_served_by_file_name(self):
        # #335: workerſ.html (U+017F) case-folds to workers.html; the route-only rule must use casefold, not lower.
        (self.dir / "workers.html").write_text("<!doctype html><body><p>workers</p></body>")
        self.assertEqual(get(self.port, "/worker%C5%BF.html").status, 404)
        self.assertEqual(get(self.port, "/workers").status, 200)

    def test_head_of_html_does_not_read_the_file_and_a_huge_page_is_refused(self):
        # #333: HEAD matched GET's length without reading; a page over the cap is 413 and never read.
        reads = []
        real = Path.read_bytes

        def spy(path):
            reads.append(path.name)
            return real(path)

        with mock.patch.object(Path, "read_bytes", spy):
            head = get(self.port, "/arch-review.html", method="HEAD")
        self.assertEqual(head.status, 200)
        self.assertNotIn("arch-review.html", reads)
        self.assertEqual(head.getheader("Content-Length"), str(len(get(self.port, "/arch-review.html").body)))
        with mock.patch.object(pg, "MAX_PAGE", 64):
            (self.dir / "big.html").write_bytes(b"x" * 128)
            reads.clear()
            with mock.patch.object(Path, "read_bytes", spy):
                for method in ("GET", "HEAD"):
                    with self.subTest(method=method):
                        self.assertEqual(get(self.port, "/big.html", method=method).status, 413)
            self.assertEqual(reads, [])
        if os.geteuid() == 0:
            return  # root reads a mode-0 file
        locked = self.dir / "locked.html"
        locked.write_text("<!doctype html><body>x</body>")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        for method in ("GET", "HEAD"):
            with self.subTest(unreadable=method):
                self.assertEqual(get(self.port, "/locked.html", method=method).status, 404)


class BannerPerRequestTest(unittest.TestCase):
    """#334: banner is per request, not a side effect left on a reused handler."""

    def test_allowed_clears_banner_before_each_request(self):
        h = pg.Handler.__new__(pg.Handler)
        h.banner = "stale from the request before"
        h.server = mock.Mock(server_address=("127.0.0.1", 9), root=None, refresher=None)
        h.headers = {"Host": "evil.test"}
        h.path, h.command, h.directory = "/", "GET", str(Path(tempfile.mkdtemp()))
        h.send_error = mock.Mock()
        self.assertFalse(h._allowed())
        self.assertEqual(h.banner, "")


class EnsureTest(unittest.TestCase):
    def setUp(self):
        self.dir = pages_dir()
        (self.dir / ".pid").unlink()
        self.port = free_port()
        self.log = self.dir.parent / f"pages-{self.port}.log"

    def stop(self):
        pid = self.dir / ".pid"
        if pid.is_file():
            os.kill(int(pid.read_text()), signal.SIGTERM)

    def test_starts_once_then_reuses(self):
        self.addCleanup(self.stop)
        first = pg.ensure(self.dir, self.port, self.log)
        self.assertEqual(first, "pages: started")
        pid = (self.dir / ".pid").read_text()
        self.assertEqual(get(self.port, "/").status, 200)
        self.assertEqual(pg.ensure(self.dir, self.port, self.log), "pages: serving")
        self.assertEqual((self.dir / ".pid").read_text(), pid)

    def test_a_denied_connection_is_not_a_free_port(self):
        # A sandbox without network refuses the loopback connect; the server on the port may be running fine.
        denied = urllib.error.URLError(PermissionError(1, "Operation not permitted"))
        with mock.patch.object(pg.urllib.request, "urlopen", side_effect=denied), \
                mock.patch.object(pg.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(pg.PagesError, f"{self.port}.*denied"):
                pg.ensure(self.dir, self.port, self.log)
        popen.assert_not_called()

    def test_a_server_that_never_came_up_leaves_no_pid(self):
        # A dead child's pid in .pid made the next ensure refuse to stop the real server (#248).
        dead = mock.Mock(pid=4242, **{"poll.return_value": 1})
        with mock.patch.object(pg.subprocess, "Popen", return_value=dead):
            with self.assertRaisesRegex(pg.PagesError, "did not come up"):
                pg.ensure(self.dir, self.port, self.log)
        self.assertFalse((self.dir / ".pid").exists())

    def test_a_server_that_never_came_up_is_terminated(self):
        """#493: ensure must kill the detached child before raising, or nothing can stop it."""
        started: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def popen(*_a, **_k):
            proc = real_popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              start_new_session=True)
            started.append(proc)
            return proc

        with mock.patch.object(pg.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(pg, "_who", return_value=None), \
                mock.patch.object(pg.time, "sleep"):
            with self.assertRaisesRegex(pg.PagesError, "did not come up"):
                pg.ensure(self.dir, self.port, self.log)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll(), "failed ensure left its detached server running")
        self.assertFalse((self.dir / ".pid").exists())

    def test_a_foreign_server_on_the_port_is_an_error(self):
        other = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        with self.assertRaisesRegex(pg.PagesError, f"{self.port}.*not the pages server"):
            pg.ensure(self.dir, self.port, self.log)

    def test_cancelled_start_reaps_its_detached_child_before_returning(self):
        stop = threading.Event()
        real_popen = subprocess.Popen
        children = []

        def start(*_a, **_k):
            proc = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            children.append(proc)
            self.addCleanup(lambda: proc.poll() is None and proc.kill())
            stop.set()  # shutdown arrived while Popen was starting the child
            return proc

        with mock.patch.object(pg.subprocess, "Popen", side_effect=start), \
             mock.patch.object(pg, "_who", return_value=None):
            self.assertEqual(pg.ensure(self.dir, self.port, self.log, stop=stop), "pages: stopped")
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode, "cancelled startup must reap its child before returning")
        self.assertFalse((self.dir / ".pid").exists())

    def test_cancellation_interrupts_waiting_for_the_ensure_lock(self):
        stop = threading.Event()
        finished = threading.Event()
        attempted = threading.Event()
        errors = []
        real_lock = pg.supervisor.lock

        def lock(*args, **kwargs):
            attempted.set()
            return real_lock(*args, **kwargs)

        def ensure():
            try:
                pg.ensure(self.dir, self.port, self.log, stop=stop)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        with real_lock(self.dir / ".ensure.lock"), \
             mock.patch.object(pg.supervisor, "lock", side_effect=lock), \
             mock.patch.object(pg.subprocess, "Popen") as popen:
            thread = threading.Thread(target=ensure, daemon=True)
            thread.start()
            self.assertTrue(attempted.wait(2), "ensure never tried to acquire the held lock")
            stop.set()
            self.assertTrue(finished.wait(2), "shutdown stayed blocked on the ensure lock")
            thread.join(timeout=2)
        self.assertEqual(errors, [])
        popen.assert_not_called()

    def test_stale_ensure_preserves_newer_server_with_or_without_identity(self):
        root = self.dir.parent / "workspace"
        for identity in ("", " release-" + "a" * 24):
            for selected in (None, root):
                with self.subTest(identity=identity, root=selected), \
                     mock.patch.object(pg, "VERSION", "1.2.3"), \
                     mock.patch.object(pg, "_who", return_value=f"{pg.SERVER}/2.0.0{identity}"), \
                     mock.patch.object(pg, "_stop", side_effect=AssertionError("newer server stopped")), \
                     mock.patch.object(pg.subprocess, "Popen", side_effect=AssertionError("server replaced")):
                    self.assertEqual(pg.ensure(self.dir, self.port, self.log, selected), "pages: serving")

    def test_unselected_caller_preserves_equal_or_unordered_server(self):
        root = self.dir.parent / "workspace"
        for version in ("1.2.3", "1.2.3.0", "unknown"):
            for identity in ("", " release-" + "a" * 24):
                for selected in (None, root):
                    with self.subTest(version=version, identity=identity, root=selected), \
                         mock.patch.object(pg, "VERSION", "1.2.3"), \
                         mock.patch.object(pg, "_who", return_value=f"{pg.SERVER}/{version}{identity}"), \
                         mock.patch.object(pg, "_stop", side_effect=AssertionError("running server stopped")):
                        self.assertEqual(pg.ensure(self.dir, self.port, self.log, selected), "pages: serving")

    def test_selected_same_version_release_replaces_different_or_unidentified_server(self):
        root = self.dir.parent / "workspace"
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / "versions" / "new"
            release.mkdir(parents=True)
            (release / "chief_of_stuff.py").touch()
            (release.parent.parent / "current").symlink_to(release)
            for identity in ("", " release-" + "a" * 24):
                with self.subTest(identity=identity), \
                     mock.patch.object(pg.supervisor, "RELEASE", release), \
                     mock.patch.object(pg, "VERSION", "1.2.3"), \
                     mock.patch.object(pg, "_who", return_value=f"{pg.SERVER}/1.2.3{identity}"), \
                     mock.patch.object(pg, "_stop", side_effect=RuntimeError("selected upgrade")):
                    with self.assertRaisesRegex(RuntimeError, "selected upgrade"):
                        pg.ensure(self.dir, self.port, self.log, root)

    def test_plugin_marker_alone_does_not_authorize_same_version_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            release = Path(tmp).resolve() / "plugins/cache/market/chief/1.2.3"
            (release / "scripts").mkdir(parents=True)
            (release / "scripts/engine.py").touch()
            (release / "requirements.txt").touch()
            (release / ".claude-plugin").mkdir()
            (release / ".claude-plugin/plugin.json").write_text('{"version":"1.2.3"}')
            marker = pg.supervisor.state_dir(root) / "engine-plugin-release"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(release))
            with mock.patch.object(pg.supervisor, "RELEASE", release), \
                 mock.patch.object(pg.supervisor, "current_release", return_value=release), \
                 mock.patch.object(pg, "VERSION", "1.2.3"), \
                 mock.patch.object(pg, "_who", return_value=f"{pg.SERVER}/1.2.3"), \
                 mock.patch.object(pg, "_stop", side_effect=AssertionError("marker is not authority")):
                self.assertEqual(pg.ensure(self.dir, self.port, self.log, root), "pages: serving")

    def test_authoritative_installed_rollback_replaces_newer_server(self):
        root = self.dir.parent / "workspace"
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / "versions" / "old"
            release.mkdir(parents=True)
            (release / "chief_of_stuff.py").touch()
            (release.parent.parent / "current").symlink_to(release)
            with mock.patch.object(pg.supervisor, "RELEASE", release), \
                 mock.patch.object(pg, "VERSION", "1.0.0"), \
                 mock.patch.object(pg, "_who", return_value=f"{pg.SERVER}/2.0.0"), \
                 mock.patch.object(pg, "_stop", side_effect=RuntimeError("rollback selected")):
                with self.assertRaisesRegex(RuntimeError, "rollback selected"):
                    pg.ensure(self.dir, self.port, self.log, root)

    def test_pending_private_intent_does_not_authorize_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "workspace"
            root.mkdir()
            release = Path(tmp).resolve() / "old"
            (release / "scripts").mkdir(parents=True)
            (release / "scripts/engine.py").touch()
            (release / "requirements.txt").touch()
            data = Path(tmp) / "data"
            private = data / "engine-intents"
            private.mkdir(parents=True, mode=0o700)
            label = pg.supervisor.label("engine", root)
            intent = {"operation": "install", "pending": True, "release": str(release),
                      "job": {"Label": label, "WorkingDirectory": str(root)}}
            (private / (label + ".json")).write_text(json.dumps(intent))
            with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(data)}), \
                 mock.patch.object(pg.supervisor, "RELEASE", release), \
                 mock.patch.object(pg, "VERSION", "1.0.0"), \
                 mock.patch.object(pg, "_who", return_value=f"{pg.SERVER}/2.0.0"), \
                 mock.patch.object(pg, "_stop", side_effect=RuntimeError("rollback selected")):
                self.assertEqual(pg.ensure(self.dir, self.port, self.log, root), "pages: serving")
                intent["pending"] = False
                (private / (label + ".json")).write_text(json.dumps(intent))
                with self.assertRaisesRegex(RuntimeError, "rollback selected"):
                    pg.ensure(self.dir, self.port, self.log, root)

    def test_invalid_private_intent_does_not_replace_a_working_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "workspace"
            root.mkdir()
            data = Path(tmp) / "data"
            private = data / "engine-intents"
            private.mkdir(parents=True, mode=0o700)
            (private / (pg.supervisor.label("engine", root) + ".json")).write_text("invalid json")
            with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(data)}), \
                 mock.patch.object(pg, "VERSION", "2.0.0"), \
                 mock.patch.object(pg, "_who", return_value=f"{pg.SERVER}/1.0.0"), \
                 mock.patch.object(pg, "_stop", side_effect=AssertionError("working server stopped")):
                with self.assertRaises(pg.supervisor.ServiceError):
                    pg.ensure(self.dir, self.port, self.log, root)

    def test_stale_newer_caller_preserves_selected_legacy_server_without_identity(self):
        root = self.dir.parent / "workspace"
        with tempfile.TemporaryDirectory() as tmp:
            versions = Path(tmp) / "versions"
            old, new = versions / "old", versions / "new"
            for release in (old, new):
                release.mkdir(parents=True)
                (release / "chief_of_stuff.py").touch()
            (versions.parent / "current").symlink_to(old)
            with mock.patch.object(pg.supervisor, "RELEASE", new), \
                 mock.patch.object(pg, "VERSION", "2.0.0"), \
                 mock.patch.object(pg, "_who", return_value=f"{pg.SERVER}/1.0.0"), \
                 mock.patch.object(pg, "_stop", side_effect=AssertionError("selected server stopped")):
                self.assertEqual(pg.ensure(self.dir, self.port, self.log, root), "pages: serving")

    def test_completed_private_rollback_preserves_actual_legacy_server(self):
        root = self.dir.parent / "workspace"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(pg.__file__).resolve().parent.parent
            legacy = Path(tmp).resolve() / "legacy"
            shutil.copytree(source / "scripts", legacy / "scripts")
            shutil.copyfile(source / "evals/fixtures/legacy_pages.py", legacy / "scripts/pages.py")
            (legacy / ".claude-plugin").mkdir()
            (legacy / ".claude-plugin/plugin.json").write_text('{"version":"1.0.0"}')
            old_server = subprocess.Popen([sys.executable, str(legacy / "scripts/pages.py"),
                                           "serve", "--dir", str(self.dir), "--port", str(self.port)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: old_server.poll() is None and (old_server.kill(), old_server.wait()))
            for _ in range(100):
                if pg._who(self.port):
                    break
                time.sleep(0.05)
            self.assertIsNotNone(pg._who(self.port), "legacy server did not start")
            (self.dir / ".pid").write_text(str(old_server.pid))
            data = Path(tmp).resolve() / "data"
            private = data / "engine-intents"
            private.mkdir(parents=True, mode=0o700)
            selected = legacy
            (selected / "requirements.txt").touch()
            label = pg.supervisor.label("engine", root)
            intent = {"operation": "install", "pending": False, "release": str(selected),
                      "job": {"Label": label, "WorkingDirectory": str(root.resolve())}}
            (private / (label + ".json")).write_text(json.dumps(intent))
            self.assertNotIn("release-", pg._who(self.port))
            with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(data)}), \
                 mock.patch.object(pg.supervisor, "_explicit_install", side_effect=AssertionError("unsafe cached reader"), create=True), \
                 mock.patch.object(pg, "VERSION", "999.0.0"), \
                 mock.patch.object(pg, "_stop", side_effect=AssertionError("selected legacy server stopped")):
                self.assertEqual(pg.ensure(self.dir, self.port, self.log, root), "pages: serving")
            self.assertIsNone(old_server.poll())

    def test_explicit_selection_protects_legacy_server_from_stale_newer_caller(self):
        root = self.dir.parent / "workspace"
        selected = root / "selected-old"
        with mock.patch.object(pg.release_selection, "explicit_intent", return_value={"operation": "install", "pending": False}), \
             mock.patch.object(pg.release_selection, "explicit_release", return_value=selected), \
             mock.patch.object(pg, "VERSION", "2.0.0"), \
             mock.patch.object(pg, "_who", return_value=f"{pg.SERVER}/1.0.0"), \
             mock.patch.object(pg, "_stop", side_effect=AssertionError("selected server stopped")):
            self.assertEqual(pg.ensure(self.dir, self.port, self.log, root), "pages: serving")

    def test_explicit_rollback_replaces_newer_server_only_for_selected_release(self):
        root = self.dir.parent / "workspace"
        for selected, should_stop in ((pg.supervisor.RELEASE, False), (root / "other", False)):
            with self.subTest(selected=selected), \
                 mock.patch.object(pg.release_selection, "explicit_intent", return_value={"operation": "install", "pending": True, "release": str(selected)}), \
                 mock.patch.object(pg.release_selection, "explicit_release", side_effect=lambda root, sup, completed_only=False: None if completed_only else selected), \
                 mock.patch.object(pg, "VERSION", "1.0.0"), \
                 mock.patch.object(pg, "_who", return_value=f"{pg.SERVER}/2.0.0"), \
                 mock.patch.object(pg, "_stop", side_effect=RuntimeError("selected rollback")) as stopped:
                if should_stop:
                    with self.assertRaisesRegex(RuntimeError, "selected rollback"):
                        pg.ensure(self.dir, self.port, self.log, root)
                else:
                    self.assertEqual(pg.ensure(self.dir, self.port, self.log, root), "pages: serving")
                    stopped.assert_not_called()

    def test_completed_explicit_cache_rollback_replaces_newer_server(self):
        root = self.dir.parent / "workspace"
        with mock.patch.object(pg.release_selection, "explicit_intent", return_value={"operation": "install", "release": str(pg.supervisor.RELEASE), "pending": False}), \
             mock.patch.object(pg, "VERSION", "1.0.0"), \
             mock.patch.object(pg, "_who", return_value=f"{pg.SERVER}/2.0.0"), \
             mock.patch.object(pg, "_stop", side_effect=RuntimeError("selected rollback")):
            with self.assertRaisesRegex(RuntimeError, "selected rollback"):
                pg.ensure(self.dir, self.port, self.log, root)

    def test_legacy_supervisor_honors_completed_explicit_cache_rollback(self):
        root = self.dir / "workspace"
        root.mkdir(exist_ok=True)
        state = pg.supervisor.state_dir(root)
        state.mkdir(exist_ok=True)
        release = pg.supervisor.RELEASE
        data = self.dir / "data"
        with mock.patch.object(pg.supervisor, "data_dir", return_value=data):
            pg.supervisor._write_intent(root, {
                "operation": "install", "release": str(release), "pending": False,
                "job": {"Label": pg.supervisor.label("engine", root), "WorkingDirectory": str(root.resolve())}})
        legacy = SimpleNamespace(RELEASE=release, state_dir=pg.supervisor.state_dir,
                                 data_dir=lambda: data, label=pg.supervisor.label, ServiceError=pg.supervisor.ServiceError)
        with mock.patch.object(pg, "supervisor", legacy):
            self.assertTrue(pg._selected_for_rollback(root))

    def test_versions_compare_numerically_and_unordered_versions_are_preserved(self):
        with mock.patch.object(pg, "VERSION", "1.9.0"):
            self.assertTrue(pg._server_not_older(f"{pg.SERVER}/1.10.0"))
            self.assertTrue(pg._server_not_older(f"{pg.SERVER}/1.9"))
            self.assertTrue(pg._server_not_older(f"{pg.SERVER}/1.9.0.0"))
            self.assertTrue(pg._server_not_older(f"{pg.SERVER}/1.10.0-rc1"))
            self.assertFalse(pg._server_not_older(f"{pg.SERVER}/1.8.0"))

    def test_an_older_server_is_restarted(self):
        # A server started before `--root` existed never renders; `--ensure` replaces it, by its pid file.
        self.addCleanup(self.stop)
        old = start_old_server(self.dir, self.port)
        self.addCleanup(old.kill)
        (self.dir / ".pid").write_text(str(old.pid))
        with mock.patch.object(pg, "_selected_for_rollback", return_value=True):
            self.assertEqual(pg.ensure(self.dir, self.port, self.log), "pages: restarted")
        self.assertIsNotNone(old.wait(timeout=5))
        self.assertIn(f"{pg.SERVER}/{pg.VERSION}", pg._who(self.port))

    def test_an_older_server_is_stopped_only_by_its_own_pid(self):
        old = start_old_server(self.dir, self.port)
        self.addCleanup(old.kill)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(other.kill)
        (self.dir / ".pid").write_text(str(other.pid))
        with mock.patch.object(pg, "_selected_for_rollback", return_value=True):
            with self.assertRaisesRegex(pg.PagesError, "not it"):
                pg.ensure(self.dir, self.port, self.log)
        self.assertIsNone(other.poll())
        self.assertIsNone(old.poll())

    def test_concurrent_ensures_start_one_server(self):
        self.addCleanup(self.stop)
        barrier = threading.Barrier(4)
        results: list[str] = []
        errors: list[Exception] = []

        def run():
            barrier.wait(timeout=5)
            try:
                results.append(pg.ensure(self.dir, self.port, self.log))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["pages: serving", "pages: serving", "pages: serving", "pages: started"])
        pid = int((self.dir / ".pid").read_text())
        self.assertTrue(pg.is_pages_serve(pid, self.port))
        self.assertEqual(get(self.port, "/").status, 200)


OLD_SERVER = """import functools, http.server, sys
class H(http.server.SimpleHTTPRequestHandler):
    server_version = "chief-of-stuff-pages"
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[5])), functools.partial(H, directory=sys.argv[3])).serve_forever()
"""


def start_old_server(pages: Path, port: int) -> subprocess.Popen:
    script = Path(tempfile.mkdtemp()) / "pages.py"
    script.write_text(OLD_SERVER)
    proc = subprocess.Popen([sys.executable, str(script), "serve", "--dir", str(pages), "--port", str(port)],
                            stderr=subprocess.DEVNULL)
    for _ in range(50):
        if pg._who(port) is not None:
            return proc
        time.sleep(0.1)
    raise AssertionError("old server did not start")


ZONE = "America/Chicago"
# BUILD draws a task only with a lane and a stage (#350), so the row has both and the settings name the lane.
LANES = '[lanes]\nbuild = { stages = ["implement", "review", "merge"], gates = ["merge"] }\n'
TRACKER = """# Tracker {day}

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| {task} | {task} | Robin | open | 09:00 |  | S | build | implement |  | Checklist: {task} |

## Decisions

| time | item | Robin's words |
|---|---|---|

## Log

- 09:00 opened the day
"""


class RenderOnRequestTest(unittest.TestCase):
    """The agent never renders: a GET re-renders the page its sources outdate (the user, 2026-09-26: "I want to
    make the page render automatically using scripts and stuff instead of the agent pulling in data each time")."""

    def setUp(self):
        # A private parent, removed after the test: FreshRefusesAForeignNameTest snapshots root.parent, which must
        # never be the shared temp dir (#357: reading every file there hung the suite locally).
        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        self.root = Path(parent.name).resolve() / "ws"
        self.root.mkdir()
        self.day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
            "- Settings: `cos.toml`\n")
        (self.root / "daily").mkdir()
        self.tracker = self.root / "daily" / f"{self.day}-tracker.md"
        self.tracker.write_text(TRACKER.format(day=self.day, task="Security audit"))
        self.pages = self.root / "pages"
        self.pages.mkdir()
        (self.root / "cos.toml").write_text(getattr(self, "SETTINGS", LANES))
        self.calls, self.refreshed = [], threading.Event()
        self.server = pg.make_server(self.pages, 0, root=self.root, refresh=self.fake_refresh, every=3600)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        # The first refresh writes .sources.json, a page source; a test racing it sees a re-render it didn't cause.
        self.assertTrue(self.refreshed.wait(10))

    def fake_refresh(self, root, now):
        self.calls.append(now)
        import board_sources
        try:
            return board_sources.refresh(root, now)
        finally:
            self.refreshed.set()

    def board(self) -> Path:
        return self.pages / f"{self.day}-board.html"

    def test_root_renders_todays_board_and_rerenders_after_a_tracker_edit(self):
        self.assertIn(b"Security audit", get(self.port, "/").body)
        self.tracker.write_text(TRACKER.format(day=self.day, task="Draft release notes"))
        self.assertIn(b"Draft release notes", get(self.port, "/").body)
        self.assertEqual(get(self.port, "/decisions").status, 200)
        self.assertTrue((self.pages / "decisions.html").is_file())

    def test_decisions_route_lists_store_decisions_and_matches_board_badge(self):
        import store
        db_path = self.root / "store.db"
        store.migrate(db_path)
        db = store.Store(db_path)
        try:
            now_iso = datetime.now(ZoneInfo(ZONE)).isoformat(timespec="seconds")
            db.upsert_item("github:example/repo#42", title="Engine task", stage="implement", forge_state="open")
            db.add_decision("github:example/repo#42", "scope", "First store ask", asked_at=now_iso)
            db.add_decision("github:example/repo#42", "design", "Second store ask", asked_at=now_iso)
            db.add_decision("github:example/repo#42", "permission", "Answered store ask",
                            answer="allow", decided_by="trio", asked_at=now_iso,
                            record={"status": "answered", "answered_at": now_iso})
        finally:
            db.engine.dispose()
        board = get(self.port, "/").body.decode()
        decisions = get(self.port, "/decisions").body.decode()
        self.assertIn('<span class="badge" title="2 pending">2</span>', board)
        self.assertIn('<span class="badge" title="2 pending">2</span>', decisions)
        self.assertIn("2 pending", decisions)
        self.assertIn("PENDING · 2", decisions)
        self.assertIn("1 completed today", decisions)
        self.assertIn("COMPLETED · 1", decisions)
        for text in ("First store ask", "Second store ask", "Answered store ask", "example/repo#42"):
            with self.subTest(text=text):
                self.assertIn(text, decisions)

    def test_decisions_pages_match_board_navigation_counts(self):
        # #965: Decisions list and a JSON decision page show the same Workers/Dispatch/Decisions badges as `/`.
        import store
        (self.pages / "decision-nav-check.json").write_text(decision("nav-check"))
        db_path = self.root / "store.db"
        store.migrate(db_path)
        db = store.Store(db_path)
        try:
            db.upsert_item("github:example/repo#42", title="Engine task", stage="implement", forge_state="open")
            db.upsert_item("github:example/repo#43", title="Needs a person", human_review=True)
            db.add_decision("github:example/repo#43", "scope", "Store ask on the hold")
        finally:
            db.engine.dispose()
        nav = re.compile(r'<nav class="tabs"[^>]*>.*?</nav>')
        board_nav = nav.search(get(self.port, "/").body.decode()).group(0)
        for route in ("/decisions", "/decisions/nav-check"):
            with self.subTest(route=route):
                page_nav = nav.search(get(self.port, route).body.decode()).group(0)
                for badge in ('title="1 need input">1', 'title="1 ready to dispatch">1',
                              'title="2 pending">2'):
                    self.assertIn(badge, board_nav)
                    self.assertIn(badge, page_nav)

    def test_an_unchanged_page_is_served_as_written(self):
        get(self.port, "/")
        before = self.board().stat().st_mtime_ns
        resp = get(self.port, "/", method="HEAD")
        self.assertEqual((resp.status, self.board().stat().st_mtime_ns), (200, before))

    def test_a_render_error_keeps_the_last_good_page_with_a_banner(self):
        get(self.port, "/")
        (self.root / "cos.toml").write_text("[lanes\n")
        resp = get(self.port, "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"Security audit", resp.body)
        self.assertIn(b"not re-rendered", resp.body)
        self.assertIn(b'role="alert"', resp.body)

    def test_a_rendered_page_carries_the_snippet_once_and_its_file_does_not(self):
        # Injected at serve time, so a page rendered by an older release gets it too; the board's own poll is gone.
        (self.pages / "decision-cache-ttl.json").write_text(
            '{"headline": "Cache TTL", "ask": "Keep 5 minutes?", "options": [{"key": "A", "title": "Keep", '
            '"text": "no change"}, {"key": "B", "title": "Drop", "text": "slower"}], "recommended": "A", "why": "fine", "default": "A at 17:00"}')
        for path, name in (("/", f"{self.day}-board.html"), ("/decisions", "decisions.html"),
                           ("/decisions/cache-ttl", "decision-cache-ttl.html")):
            with self.subTest(path=path):
                got = get(self.port, path)
                page, snip = got.body.decode(), snippet(got.getheader("Last-Modified"))
                self.assertEqual(page.count(snip), 1)
                self.assertEqual(page.count("fetch(location.pathname"), 1)
                on_disk = (self.pages / name).read_text()
                self.assertNotIn(snip, on_disk)
                self.assertNotIn("fetch(location.pathname", on_disk)

    def test_the_banner_variant_carries_the_snippet_once(self):
        get(self.port, "/")
        (self.root / "cos.toml").write_text("[lanes\n")
        got, head = get(self.port, "/"), get(self.port, "/", method="HEAD")
        snip = snippet(got.getheader("Last-Modified"))
        self.assertIn(b"not re-rendered", got.body)
        body = got.body.decode()
        self.assertEqual(body.count(snip), 1)
        self.assertLess(body.index(snip), body.rindex("</body>"))
        self.assertEqual(head.getheader("Content-Length"), str(len(got.body)))
        self.assertEqual(head.getheader("Last-Modified"), got.getheader("Last-Modified"))

    def test_pages_are_routes_and_their_files_are_not_served(self):
        # The user, 2026-09-26: "pages should be: / /decisions /decisions/id actually an app".
        (self.pages / "decision-cache-ttl.json").write_text(
            '{"headline": "Cache TTL", "ask": "Keep 5 minutes?", "options": [{"key": "A", "title": "Keep", '
            '"text": "no change"}, {"key": "B", "title": "Drop", "text": "slower"}], "recommended": "A", "why": "fine", "default": "A at 17:00"}')
        page = get(self.port, "/decisions/cache-ttl")
        self.assertEqual(page.status, 200)
        self.assertIn(b"Cache TTL", page.body)
        self.assertIn(b'href="/decisions#pending-decisions"', page.body)
        self.assertIn(b'href="/decisions/cache-ttl"', get(self.port, "/decisions").body)
        # The user, 2026-09-26: "get rid of the .html versions entirely".
        for missing in ("/decisions/nope", "/decisions/../decisions", "/decisions/cache-ttl.json", "/decisions.html",
                        "/decision-cache-ttl.html", f"/{self.day}-board.html"):
            with self.subTest(missing=missing):
                self.assertEqual(get(self.port, missing).status, 404)

    def test_a_page_rendered_by_an_older_release_is_rendered_again(self):
        # A new release changes the renderer, not the sources: every page written before it is out of date.
        (self.pages / "decision-cache-ttl.json").write_text(
            '{"headline": "Cache TTL", "ask": "Keep 5 minutes?", "options": [{"key": "A", "title": "Keep", '
            '"text": "no change"}, {"key": "B", "title": "Drop", "text": "slower"}], "recommended": "A", "why": "fine", "default": "A at 17:00"}')
        self.assertEqual(get(self.port, "/decisions/cache-ttl").status, 200)
        get(self.port, "/")
        old = 1_000_000_000  # 2001: newer than no source, older than any release
        for f in [*self.pages.glob("*.json"), self.tracker, self.root / "CLAUDE.md"]:
            os.utime(f, (old - 10, old - 10))
        for f in self.pages.glob("*.html"):
            f.write_text("rendered by an older release")
            os.utime(f, (old, old))
        for path in ("/decisions/cache-ttl", "/decisions", "/"):
            with self.subTest(path=path):
                self.assertNotIn(b"rendered by an older release", get(self.port, path).body)

    def test_dotfiles_and_foreign_hosts_are_still_refused(self):
        self.assertEqual(get(self.port, "/.sources.json").status, 404)
        self.assertEqual(get(self.port, "/", host="evil.test").status, 403)

    def test_the_refresher_writes_the_cache_and_a_stale_cache_wakes_it(self):
        cache = self.pages / ".sources.json"
        for _ in range(50):
            if cache.is_file():
                break
            time.sleep(0.05)
        self.assertTrue(cache.is_file())
        self.assertEqual(len(self.calls), 1)
        old = time.time() - 7200
        os.utime(cache, (old, old))
        get(self.port, "/")
        for _ in range(50):
            if len(self.calls) > 1:
                break
            time.sleep(0.05)
        self.assertEqual(len(self.calls), 2)


ISSUE_TRACKER = """# Tracker {day}

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| {task} | {task} | Robin | running 09:00 | {day} |  | M | build | implement | #7 | c |

## Log

- 09:00 opened the day
- 09:00 stage: {task} → implement
"""
DECISION = ('{"headline": "Cache TTL", "ask": "Keep 5 minutes?", "options": [{"key": "A", "title": "Keep", '
            '"text": "no change"}, {"key": "B", "title": "Drop", "text": "slower"}], "recommended": "A", "why": "fine", "default": "A at 17:00"}')
ARCHITECTURAL_HOLD = json.dumps({**json.loads(DECISION), "headline": "Storage architecture",
                                "ask": "Which storage boundary should the service use?",
                                "holds": ["#7"], "asked": "09:00"})


class HoldWiringTest(unittest.TestCase):
    """#228 review: the board's and the task page's write() read the pending decisions and [kanban] themselves, and
    both name the same cause for the task's hold."""

    def workspace(self, decision: str = DECISION, toml: str = "") -> tuple[Path, str]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root, day = Path(tmp.name), datetime.now(ZoneInfo(ZONE)).date().isoformat()
        (root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n- Settings: `cos.toml`\n")
        (root / "cos.toml").write_text(LANES + toml)
        (root / "daily").mkdir()
        (root / "daily" / f"{day}-tracker.md").write_text(ISSUE_TRACKER.format(day=day, task="Security audit"))
        (root / "pages").mkdir()
        (root / "pages" / "decision-cache-ttl.json").write_text(decision)
        return root, day

    def notes(self, root: Path, day: str) -> list[str]:
        import issue_page, render_board
        render_board.write(root, day)
        board = (root / "pages" / f"{day}-board.html").read_text()
        task = issue_page.write(root, root / "pages", 7).read_text()
        return [re.findall(r"needs input · [\w ]+", page) for page in (board, task)]

    def test_a_pending_decision_holding_the_issue_names_decision_on_both_pages(self):
        root, day = self.workspace(DECISION[:-1] + ', "holds": ["#7"]}')
        for got in self.notes(root, day):
            self.assertIn("needs input · decision", got)

    def test_a_legacy_kanban_hold_stage_does_not_hold_either_page(self):
        root, day = self.workspace(toml='[kanban]\nstages = ["impl"]\nhuman_review_label = "needs-human"\nhold_stages = ["implement"]\n[kanban.map]\nimplement = 0\n')
        for got in self.notes(root, day):
            self.assertNotIn("needs input · implement", got)


class LastLookedTest(unittest.TestCase):
    """#229: a person loading the board records the server's time; the Since tab counts the holds that began after
    the look before it, from the start of the day when there is none. A timer reload is not a look."""

    SETTINGS = LANES + '[kanban]\nstages = ["impl"]\nhuman_review_label = "needs-human"\nhold_stages = ["implement"]\n[kanban.map]\nimplement = 0\n'
    # A navigation, as every browser marks one (Safari sends no Sec-Fetch-User: Codex on #579); the reload snippet's
    # own reload carries its cookie, and an agent's GET is no navigation.
    LOOK = {"Sec-Fetch-Mode": "navigate"}
    RELOAD = {"Sec-Fetch-Mode": "navigate", "Cookie": "theme=dark; cos-reload=1"}
    fake_refresh = RenderOnRequestTest.fake_refresh

    def setUp(self):
        RenderOnRequestTest.setUp(self)
        # Since-look behavior uses a real architectural decision, never a retired stage gate.
        (self.pages / "decision-architecture.json").write_text(ARCHITECTURAL_HOLD)

    def since(self, headers=None) -> list[str]:
        return re.findall(r'title="(\d+ went needs input since [\w :]+)"', get(self.port, "/", headers=headers).body.decode())

    def test_a_look_counts_from_the_look_before_it_and_a_timer_reload_is_no_look(self):
        self.tracker.write_text(ISSUE_TRACKER.format(day=self.day, task="Security audit"))  # architectural hold since 09:00
        earlier = datetime.fromisoformat(f"{self.day}T08:00").replace(tzinfo=ZoneInfo(ZONE))
        (self.pages / ".last-looked").write_text(earlier.isoformat())
        self.assertEqual(self.since(), ["1 went needs input since 00:00"])
        self.assertEqual(self.since(self.RELOAD), ["1 went needs input since 00:00"])
        self.assertEqual(get(self.port, "/", method="HEAD", headers=self.LOOK).status, 200)
        self.assertFalse((self.pages / ".looked-before").exists(), "an agent's GET, a timer reload or a HEAD recorded a look")
        before = datetime.now(ZoneInfo(ZONE)).replace(microsecond=0)
        self.assertEqual(self.since(self.LOOK), ["1 went needs input since 08:00"])
        self.assertEqual(datetime.fromisoformat((self.pages / ".looked-before").read_text()), earlier)
        self.assertGreaterEqual(datetime.fromisoformat((self.pages / ".last-looked").read_text()), before)
        for path in ("/.last-looked", "/%2elast-looked", "/%2Elooked-before"):  # #579 review R1: decoded, too
            self.assertEqual(get(self.port, path).status, 404, path)

    def test_a_hold_begun_days_after_an_old_look_counts(self):
        # Codex on #579: the Flow reads 48 hours of trackers; the Since tab reads back to the look before.
        from datetime import timedelta
        today = datetime.fromisoformat(self.day).date()
        old_day = (today - timedelta(days=3)).isoformat()
        (self.root / "daily" / f"{old_day}-tracker.md").write_text(ISSUE_TRACKER.format(day=old_day, task="Security audit"))
        self.tracker.write_text(ISSUE_TRACKER.format(day=self.day, task="Security audit").replace(
            "- 09:00 stage: Security audit → implement\n", ""))
        looked = datetime.fromisoformat(f"{today - timedelta(days=4)}T08:00").replace(tzinfo=ZoneInfo(ZONE))
        (self.pages / ".last-looked").write_text(looked.isoformat())
        self.assertEqual(self.since(self.LOOK), [f"1 went needs input since {looked:%a} 08:00"])  # not today's 08:00

    def test_a_first_look_is_recorded(self):
        # #579 review R2: no `.last-looked` yet.
        get(self.port, "/", headers=self.LOOK)
        self.assertTrue((self.pages / ".last-looked").is_file())
        self.assertFalse((self.pages / ".looked-before").exists())

    def test_a_load_with_no_board_to_serve_is_no_look(self):
        # Codex on #579: a 404 showed the person nothing, so it must not become the look the next board counts from.
        self.tracker.unlink()
        self.assertEqual(get(self.port, "/", headers=self.LOOK).status, 404)
        self.assertFalse((self.pages / ".last-looked").exists())

    def test_a_load_whose_board_cannot_render_is_no_look(self):
        # Codex on #579: the banner showed no board, so the look before it stands.
        earlier = datetime.fromisoformat(f"{self.day}T08:00").replace(tzinfo=ZoneInfo(ZONE)).isoformat()
        (self.pages / ".last-looked").write_text(earlier)
        (self.root / "cos.toml").write_text("[lanes\n")
        self.assertEqual(get(self.port, "/", headers=self.LOOK).status, 200)
        self.assertEqual((self.pages / ".last-looked").read_text(), earlier)
        self.assertFalse((self.pages / ".looked-before").exists())

    def test_a_reload_clears_its_own_marker(self):
        # Codex on #579: the marker is the origin's for 10 s, so another tab's reload must not leave it on the next
        # navigation; the request that carries it clears it, and a person's request carries none to clear.
        self.assertRegex(get(self.port, "/", headers=self.RELOAD).getheader("Set-Cookie") or "", r"^cos-reload=; Max-Age=0; Path=/$")
        self.assertIsNone(get(self.port, "/", headers=self.LOOK).getheader("Set-Cookie"))

    def test_looks_at_once_do_not_trip_over_each_others_files(self):
        # #579 review R5: the page server is threaded; each write goes through its own temporary file.
        import workspace
        errors = []

        def look():
            for _ in range(50):
                try:
                    workspace.record_look(self.pages, datetime.now(ZoneInfo(ZONE)))
                except OSError as e:
                    errors.append(e)
        threads = [threading.Thread(target=look) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        self.assertIsNotNone(workspace.looked_before(self.pages))

    def test_a_look_refreshes_since_on_all_routes(self):
        self.tracker.write_text(ISSUE_TRACKER.format(day=self.day, task="Security audit"))
        (self.pages / "decision-cache-ttl.json").write_text(DECISION)
        earlier = datetime.fromisoformat(f"{self.day}T08:00").replace(tzinfo=ZoneInfo(ZONE))
        later = datetime.fromisoformat(f"{self.day}T10:00").replace(tzinfo=ZoneInfo(ZONE))
        (self.pages / ".looked-before").write_text(earlier.isoformat())
        routes = ("/decisions/cache-ttl", "/issues/7", "/issues/7/source", "/workers", "/decisions", "/")
        for route in routes:
            with self.subTest(route=route, phase="before"):
                response = get(self.port, route)
                self.assertEqual(response.status, 200)
                self.assertIn(b"1 went needs input since 08:00", response.body)
        (self.pages / ".last-looked").write_text(later.isoformat())
        get(self.port, "/", headers=self.LOOK)
        for route in routes:
            with self.subTest(route=route, phase="after"):
                response = get(self.port, route)
                self.assertEqual(response.status, 200)
                self.assertNotIn(b"went needs input since", response.body)


    def test_direct_routes_use_the_tracker_of_the_board_served_at_root(self):
        from datetime import timedelta
        import render_board
        today = datetime.fromisoformat(self.day).date()
        older = (today - timedelta(days=2)).isoformat()
        newer = (today - timedelta(days=1)).isoformat()
        self.tracker.unlink()
        (self.root / "daily" / f"{older}-tracker.md").write_text(ISSUE_TRACKER.format(day=older, task="Security audit") + "\n- 10:00 one-shot audit: HUMAN REVIEW NEEDED\n")
        (self.root / "daily" / f"{newer}-tracker.md").write_text(TRACKER.format(day=newer, task="Other task"))
        (self.pages / "decision-cache-ttl.json").write_text(DECISION)
        render_board.write(self.root, older)
        for page in ("decisions.html", "decision-cache-ttl.html"):
            (self.pages / page).unlink()
        nav = re.compile(r'<nav class="tabs"[^>]*>.*?</nav>')
        board_nav = nav.search(get(self.port, "/").body.decode()).group()
        self.assertIn('title="1 need input">1', board_nav)
        for route in ("/decisions", "/decisions/cache-ttl", "/workers", "/issues/7", "/issues/7/source"):
            with self.subTest(route=route):
                response = get(self.port, route)
                self.assertEqual(response.status, 200)
                self.assertIn('title="1 need input">1', nav.search(response.body.decode()).group())
        render_board.write(self.root, newer, target="board")
        self.assertNotIn('title="1 need input">1', nav.search(get(self.port, "/").body.decode()).group())
        for route in ("/decisions", "/decisions/cache-ttl", "/workers", "/issues/7", "/issues/7/source"):
            with self.subTest(route=route, board="newer"):
                self.assertNotIn('title="1 need input">1', nav.search(get(self.port, route).body.decode()).group())

    def test_concurrent_board_render_cannot_overwrite_a_fresh_decision_badge(self):
        import render_board
        self.tracker.write_text(ISSUE_TRACKER.format(day=self.day, task="Security audit"))
        (self.pages / "decision-cache-ttl.json").write_text(DECISION)
        marker = self.pages / ".looked-before"
        marker.write_text(datetime.fromisoformat(f"{self.day}T08:00").replace(tzinfo=ZoneInfo(ZONE)).isoformat())
        entered, resume = threading.Event(), threading.Event()
        original = render_board.tab_counts
        calls = []

        def paused(*args, **kwargs):
            result = original(*args, **kwargs)
            if threading.current_thread().name == "paused-board" and not calls:
                calls.append(True)
                entered.set()
                if not resume.wait(10):
                    raise TimeoutError("board render did not resume")
            return result

        result = []
        def render_board_request():
            result.append(pg.fresh(self.root, self.pages, f"{self.day}-board.html"))
        with mock.patch.object(render_board, "tab_counts", paused):
            thread = threading.Thread(target=render_board_request, name="paused-board")
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                marker.write_text(datetime.fromisoformat(f"{self.day}T10:00").replace(tzinfo=ZoneInfo(ZONE)).isoformat())
                self.assertNotIn(b"went needs input since", get(self.port, "/decisions/cache-ttl").body)
                self.assertNotIn(b"went needs input since", get(self.port, "/decisions").body)
            finally:
                resume.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [""])
        for route in ("/decisions/cache-ttl", "/decisions"):
            with self.subTest(route=route):
                body = get(self.port, route).body.decode()
                self.assertNotIn("went needs input since", body[body.index('<nav class="tabs"'):body.index('</nav>')])

    def test_decision_cli_output_is_refreshed_by_the_server(self):
        self.tracker.write_text(ISSUE_TRACKER.format(day=self.day, task="Security audit"))
        (self.pages / "decision-cache-ttl.json").write_text(DECISION)
        marker = self.pages / ".looked-before"
        marker.write_text(datetime.fromisoformat(f"{self.day}T08:00").replace(tzinfo=ZoneInfo(ZONE)).isoformat())
        route = "/decisions/cache-ttl"
        self.assertIn(b"1 went needs input since 08:00", get(self.port, route).body)
        script = Path(__file__).resolve().parent.parent / "scripts" / "decision_page.py"
        subprocess.run([sys.executable, str(script), "--root", str(self.root), "--name", "cache-ttl",
                        "--date", self.day], check=True, capture_output=True, text=True)
        self.assertNotIn("went needs input since", (self.pages / "decision-cache-ttl.html").read_text())
        self.assertIn(b"1 went needs input since 08:00", get(self.port, route).body)
        subprocess.run([sys.executable, str(script), "--root", str(self.root), "--name", "cache-ttl",
                        "--date", self.day], check=True, capture_output=True, text=True)
        pg.RENDERED_AT.pop(str(self.pages / "decision-cache-ttl.html"), None)
        self.assertIn(b"1 went needs input since 08:00", get(self.port, route).body)

    def test_board_cli_with_old_navigation_cannot_leave_stale_decision_pages(self):
        self.tracker.write_text(ISSUE_TRACKER.format(day=self.day, task="Security audit"))
        (self.pages / "decision-cache-ttl.json").write_text(DECISION)
        marker = self.pages / ".looked-before"
        marker.write_text(datetime.fromisoformat(f"{self.day}T08:00").replace(tzinfo=ZoneInfo(ZONE)).isoformat())
        self.assertIn(b"1 went needs input since 08:00", get(self.port, "/decisions/cache-ttl").body)
        self.assertIn(b"1 went needs input since 08:00", get(self.port, "/decisions").body)
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        code = """import sys
sys.path.insert(0, sys.argv[1])
import render_board
original = render_board.tab_counts
def paused(*args, **kwargs):
    result = original(*args, **kwargs)
    print('ready', flush=True)
    sys.stdin.readline()
    return result
render_board.tab_counts = paused
render_board.main(['--root', sys.argv[2], '--date', sys.argv[3]])
"""
        proc = subprocess.Popen([sys.executable, "-c", code, str(scripts), str(self.root), self.day],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        marker.write_text(datetime.fromisoformat(f"{self.day}T10:00").replace(tzinfo=ZoneInfo(ZONE)).isoformat())
        attempting, responses = threading.Event(), []
        import decision_page
        real_lock = decision_page.page_render_lock
        def observe_lock(*args, **kwargs):
            if kwargs.get("shared"):
                attempting.set()
            return real_lock(*args, **kwargs)
        def request():
            responses.append(get(self.port, "/decisions/cache-ttl"))
        with mock.patch.object(decision_page, "page_render_lock", side_effect=observe_lock):
            thread = threading.Thread(target=request)
            thread.start()
            try:
                self.assertTrue(attempting.wait(10))
            finally:
                proc.stdin.write("continue\n")
                proc.stdin.flush()
                _, stderr = proc.communicate(timeout=10)
                thread.join(10)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(responses), 1)
        self.assertNotIn(b"went needs input since", responses[0].body)
        for route in ("/decisions/cache-ttl", "/decisions"):
            with self.subTest(route=route):
                self.assertNotIn(b"went needs input since", get(self.port, route).body)

    def test_a_changed_look_refreshes_an_authored_decision_without_a_board_render(self):
        self.tracker.write_text(ISSUE_TRACKER.format(day=self.day, task="Security audit"))
        (self.pages / "decision-cache-ttl.json").write_text(DECISION)
        marker = self.pages / ".looked-before"
        marker.write_text(datetime.fromisoformat(f"{self.day}T08:00").replace(tzinfo=ZoneInfo(ZONE)).isoformat())
        self.assertIn(b"1 went needs input since 08:00", get(self.port, "/decisions/cache-ttl").body)
        marker.write_text(datetime.fromisoformat(f"{self.day}T10:00").replace(tzinfo=ZoneInfo(ZONE)).isoformat())
        self.assertNotIn(b"went needs input since", get(self.port, "/decisions/cache-ttl").body)


class ReloadMarksItselfTest(unittest.TestCase):
    """Codex on #579: the snippet's timer reload marks itself with a short-lived cookie the server reads as no look;
    the notice's Reload button is the person's own and does not."""

    def test_the_timer_reload_sets_the_cookie_and_the_button_does_not(self):
        import page_reload
        snippet = page_reload.SNIPPET
        self.assertIn(f"document.cookie='{page_reload.RELOAD_COOKIE};max-age=10;path=/';location.reload()", snippet)
        self.assertIn("b.onclick=function(){location.reload();}", snippet)
        self.assertEqual(snippet.count("location.reload()"), 2)


class _PagesFixture:
    """A workspace with every kind of page and the real server rendering them. `FREEZE` pins each renderer's clock at
    12:00, so a run of the real renderers with the same inputs gives the same bytes; a test that is about the clock sets it False."""

    FREEZE = True
    OLD = 1_700_000_000
    PAGES = {"/": "{day}-board.html", "/workers": "workers.html", "/decisions": "decisions.html",
             "/decisions/cache-ttl": "decision-cache-ttl.html", "/issues/7": "issue-7.html",
             "/issues/7/source": "issue-7-source.html"}

    def setUp(self):
        # A private parent, removed after the test: FreshRefusesAForeignNameTest snapshots root.parent, which must
        # never be the shared temp dir (#357: reading every file there hung the suite locally).
        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        self.root = Path(parent.name).resolve() / "ws"
        self.root.mkdir()
        self.day = datetime.now(ZoneInfo(ZONE)).date().isoformat()
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n- Settings: `cos.toml`\n")
        (self.root / "daily").mkdir()
        self.tracker = self.root / "daily" / f"{self.day}-tracker.md"
        self.tracker.write_text(ISSUE_TRACKER.format(day=self.day, task="Security audit"))
        self.pages = self.root / "pages"
        self.pages.mkdir()
        (self.pages / "decision-cache-ttl.json").write_text(DECISION)
        if self.FREEZE:
            fixed = datetime.now(ZoneInfo(ZONE)).replace(hour=12, minute=0, second=0, microsecond=0)

            class Frozen(datetime):
                @classmethod
                def now(cls, tz=None):
                    return fixed.astimezone(tz) if tz else fixed

            import decision_page, issue_page, render_board, workers_page
            for module in (decision_page, issue_page, render_board, workers_page):
                patch = mock.patch.object(module, "datetime", Frozen)
                patch.start()
                self.addCleanup(patch.stop)
        self.calls, self.refreshed = [], threading.Event()
        self.server = pg.make_server(self.pages, 0, root=self.root, refresh=self.fake_refresh, every=3600)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.assertTrue(self.refreshed.wait(10))  # the first refresh writes .sources.json: a source a test must not race

    fake_refresh = RenderOnRequestTest.fake_refresh

    def file(self, route: str) -> Path:
        return self.pages / self.PAGES[route].format(day=self.day)

    def count_renders(self) -> collections.Counter:
        """Counts, by renderer module, the calls the server makes to each page renderer from now on (the real one still runs)."""
        import decision_page, issue_page, render_board, source_page, workers_page
        counts: collections.Counter = collections.Counter()
        for module in (decision_page, issue_page, render_board, source_page, workers_page):
            def spy(*args, _real=module.write, _name=module.__name__, **kwargs):
                counts[_name] += 1
                return _real(*args, **kwargs)
            patch = mock.patch.object(module, "write", spy)
            patch.start()
            self.addCleanup(patch.stop)
        return counts

    def request_all(self, method: str = "GET", routes=None) -> dict:
        return {route: get(self.port, route, method=method) for route in (routes or self.PAGES)}


class UnchangedPageKeepsItsTimeTest(_PagesFixture, unittest.TestCase):
    """A served page's Last-Modified is its reload signal (#330): a page that shows the same thing must keep its time,
    or an open tab reloads on every request (workers.html is re-rendered on each one). The renderers write only when
    the bytes differ. Every page is backdated, which makes the next request re-render it (a source is newer) and a
    rewrite move its time to now.
    Known limit: a page that carries a rendered clock (`rendered HH:MM`, the board's header) still changes its bytes,
    so its tab still reloads once a minute; frozen at 12:00 here, the renderers cannot show that."""

    def render_all_then_backdate(self):
        for route in self.PAGES:
            self.assertEqual(get(self.port, route).status, 200, route)
            os.utime(self.file(route), (self.OLD, self.OLD))
        # decision_page writes decision-cache-ttl.html and decisions.html with the board; they were backdated too.

    def assert_still_old(self):
        stamp = email.utils.formatdate(self.OLD, usegmt=True)
        for route in self.PAGES:
            with self.subTest(route=route):
                resp = get(self.port, route)
                self.assertEqual(resp.status, 200)
                self.assertEqual((self.file(route).stat().st_mtime_ns, resp.getheader("Last-Modified")),
                                 (self.OLD * 1_000_000_000, stamp))

    def test_a_cache_rewritten_with_identical_content_moves_no_page(self):
        # What board_sources does every 60 s: the file's time moves, its content does not.
        self.render_all_then_backdate()
        cache = self.pages / ".sources.json"
        cache.write_text(cache.read_text())
        self.assertGreater(cache.stat().st_mtime, self.OLD)
        self.assert_still_old()

    def test_a_changed_tracker_moves_the_pages_it_shows(self):
        self.render_all_then_backdate()
        self.tracker.write_text(ISSUE_TRACKER.format(day=self.day, task="Draft release notes"))
        for route in ("/", "/issues/7"):
            with self.subTest(route=route):
                resp = get(self.port, route)
                self.assertIn(b"Draft release notes", resp.body)
                self.assertNotEqual(resp.getheader("Last-Modified"), email.utils.formatdate(self.OLD, usegmt=True))

    def test_a_new_decision_answer_moves_its_page(self):
        self.render_all_then_backdate()
        resp = post(self.port, "/decisions/cache-ttl", b"key=A")
        self.assertIn(resp.status, (200, 204, 303))
        for route in ("/decisions/cache-ttl", "/decisions"):
            with self.subTest(route=route):
                self.assertNotEqual(get(self.port, route).getheader("Last-Modified"), email.utils.formatdate(self.OLD, usegmt=True))


class RenderStampTest(_PagesFixture, unittest.TestCase):
    """Whether a page needs a render is the server's own record of when it last rendered it (rendered-at, in memory).
    Last-Modified is only when the page's bytes last changed. A source touched without a change, a POST and a render
    error each cost at most one render and one move of Last-Modified, never a render or a new time on every poll.
    A page with no record yet is judged by its file's time, as before."""

    def settle(self):
        time.sleep(0.05)  # a source touched now is newer than a render recorded a moment ago

    def test_a_source_touched_without_a_change_renders_each_page_once_not_on_every_poll(self):
        # R7: .sources.json is rewritten every minute; an editor save or a checkout touches a tracker the same way.
        self.request_all()
        counts = self.count_renders()
        self.settle()
        os.utime(self.pages / ".sources.json", None)
        self.request_all()
        once = dict(counts)
        self.assertEqual((once.get("issue_page"), once.get("source_page")), (1, 1), once)
        self.assertLessEqual(once.get("decision_page", 0), 1, once)
        self.assertIn(once.get("render_board"), (1, 2), once)  # `/` and `/decisions` both come from the board's render
        for _ in range(5):
            self.request_all("HEAD")
            self.request_all()
        self.assertEqual({k: v for k, v in counts.items() if k != "workers_page"},
                         {k: v for k, v in once.items() if k != "workers_page"}, "a render per poll after the first")

    def test_an_answer_re_renders_the_pages_once_and_never_serves_a_1970_time(self):
        # R8: the POST used to set every page's file time to 0; a page whose bytes do not change then kept it for good.
        self.request_all()
        before = {route: int(self.file(route).stat().st_mtime) for route in self.PAGES}
        counts = self.count_renders()
        self.settle()
        self.assertEqual(post(self.port, "/decisions/cache-ttl", b"key=A").status, 303)
        for route, resp in self.request_all().items():
            with self.subTest(route=route):
                stamp = email.utils.parsedate_to_datetime(resp.getheader("Last-Modified")).timestamp()
                self.assertGreaterEqual(stamp, before[route], resp.getheader("Last-Modified"))
        once = dict(counts)
        self.assertEqual((once.get("issue_page"), once.get("source_page")), (1, 1), once)
        self.assertGreaterEqual(once.get("render_board", 0), 1, once)
        for _ in range(3):
            for route, resp in {**self.request_all("HEAD"), **self.request_all()}.items():
                self.assertGreaterEqual(email.utils.parsedate_to_datetime(resp.getheader("Last-Modified")).timestamp(),
                                        before[route], route)
        self.assertEqual({k: v for k, v in counts.items() if k != "workers_page"},
                         {k: v for k, v in once.items() if k != "workers_page"}, "a render per poll after the answer")

    def test_a_failing_render_moves_last_modified_once_going_in_and_once_coming_out(self):
        # R10: the banner was served with the file's own time, so a tab showing it kept it after the render recovered.
        board, cos = self.file("/"), self.root / "cos.toml"
        get(self.port, "/")
        os.utime(board, (self.OLD, self.OLD))
        lm = lambda: get(self.port, "/", method="HEAD").getheader("Last-Modified")
        good = lm()
        self.settle()
        cos.write_text("[lanes\n")
        banner = [lm(), lm(), lm()]
        self.assertIn(b"not re-rendered", get(self.port, "/").body)
        self.assertNotEqual(banner[0], good, "entering the banner state did not move Last-Modified")
        self.assertEqual(len(set(banner)), 1, f"staying in the banner state moved it: {banner}")
        self.settle()
        cos.write_text("# fixed\n")
        back = [lm(), lm(), lm()]
        self.assertNotIn(b"not re-rendered", get(self.port, "/").body)
        self.assertNotEqual(back[0], banner[0], "leaving the banner state did not move Last-Modified")
        self.assertEqual(len(set(back)), 1, f"staying out of it moved it: {back}")

    def test_a_page_that_cannot_render_and_has_no_file_is_200_with_the_banner_and_the_snippet(self):
        # R3: a 404 here would hide the error; the banner is the page.
        import issue_page
        self.assertFalse(self.file("/issues/7").exists())
        with mock.patch.object(issue_page, "write", side_effect=RuntimeError("boom")):
            got, head = get(self.port, "/issues/7"), get(self.port, "/issues/7", method="HEAD")
        self.assertEqual((got.status, head.status), (200, 200))
        self.assertIn(b"not re-rendered: RuntimeError: boom", got.body)
        self.assertEqual(got.body.count(snippet(got.getheader("Last-Modified")).encode()), 1)
        self.assertEqual(head.getheader("Content-Length"), str(len(got.body)))

    def test_a_page_file_that_is_not_utf8_is_rewritten_not_wedged(self):
        # R11: the compare reads bytes; a writer killed mid-character left a file no text read can decode.
        for route, name in (("/issues/7", "issue-7.html"), ("/workers", "workers.html")):
            with self.subTest(route=route):
                (self.pages / name).write_bytes(b"<p>cut mid-char \xc2")
                os.utime(self.pages / name, (self.OLD, self.OLD))
                resp = get(self.port, route)
                self.assertEqual(resp.status, 200)
                self.assertNotIn(b"not re-rendered", resp.body)
                self.assertIn("Security audit" if route == "/issues/7" else "<", (self.pages / name).read_bytes().decode("utf-8"))
                self.assertNotIn(b"cut mid-char", (self.pages / name).read_bytes())

    def test_a_changed_page_is_swapped_in_by_a_rename_so_no_reader_sees_it_half_written(self):
        # R12: decisions.html is written by two renders under two locks; a truncate-then-write shows a reader a partial file.
        import workers_page
        target = self.pages / "workers.html"
        target.write_text("<p>stale</p>")
        before, replaced, real = set(os.listdir(self.pages)), [], os.replace

        def spy(src, dst, *args, **kwargs):
            replaced.append((Path(src), Path(dst)))
            return real(src, dst, *args, **kwargs)

        with mock.patch("os.replace", spy):
            workers_page.write(self.root, self.pages)
        self.assertNotEqual(target.read_text(), "<p>stale</p>")
        self.assertTrue([r for r in replaced if r[1].name == target.name and r[0] != r[1] and r[0].parent == r[1].parent],
                        f"workers.html was not replaced from a temp file beside it: {replaced}")
        self.assertEqual(set(os.listdir(self.pages)), before, "a temp file was left behind")


class UnfrozenBoardTest(_PagesFixture, unittest.TestCase):
    """The renderers' real clocks. A board page that is polled must not change its bytes, or its Last-Modified, with the
    seconds: its tab would reload on every poll. (A clock that changes each minute is the known limit.)"""

    FREEZE = False

    def a_minute_with_room(self):
        """Start no later than :55, so two renders 1.1 s apart share a minute."""
        if (second := datetime.now().second) >= 55:
            time.sleep(61 - second)

    @staticmethod
    def first_difference(a: bytes, b: bytes) -> str:
        i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        return f"first difference at byte {i}: {a[max(0, i - 60):i + 60]!r} vs {b[max(0, i - 60):i + 60]!r}"

    def test_the_board_and_decisions_files_are_the_same_bytes_at_two_different_seconds(self):
        # R6: panels.header printed `now` to the second, so every render of an unchanged board was a changed file.
        import render_board
        self.a_minute_with_room()
        names = (f"{self.day}-board.html", "decisions.html")
        render_board.write(self.root, self.day)
        first = {n: (self.pages / n).read_bytes() for n in names}
        time.sleep(1.1)
        render_board.write(self.root, self.day)
        for n in names:
            with self.subTest(page=n):
                again = (self.pages / n).read_bytes()
                self.assertTrue(first[n] == again, self.first_difference(first[n], again))

    def test_polling_a_stale_decisions_page_renders_the_board_once_and_leaves_its_files_alone(self):
        # R6: a stale decisions.html re-rendered on every HEAD, and each render of the board moved its time.
        get(self.port, "/")
        get(self.port, "/decisions")
        counts = self.count_renders()
        self.a_minute_with_room()
        time.sleep(0.05)
        os.utime(self.tracker, None)

        def state():
            head = get(self.port, "/decisions", method="HEAD")
            files = [self.file("/"), self.file("/decisions")]
            return head.getheader("Last-Modified"), [(f.read_bytes(), f.stat().st_mtime_ns) for f in files]

        seen = [state()]
        for _ in range(2):
            time.sleep(1.1)
            seen.append(state())
        self.assertLessEqual(counts.get("render_board", 0), 1, dict(counts))
        self.assertTrue(seen[1] == seen[0] and seen[2] == seen[0], "a poll moved the board's or decisions' file or time")


class AnswerTest(unittest.TestCase):
    """The user answers on the page; the server saves it beside the decision for the coordinator's next pass (the user,
    2026-09-26: "Selecting / entering an option should save out that decision so the agent can pick it up next pass")."""

    fake_refresh = RenderOnRequestTest.fake_refresh

    def setUp(self):
        RenderOnRequestTest.setUp(self)
        self.json = self.pages / "decision-cache-ttl.json"
        self.json.write_text(json.dumps({"headline": "Cache TTL", "ask": "Keep 5 minutes?", "options": [
            {"key": "A", "title": "Keep", "text": "no change"}, {"key": "B", "title": "Drop", "text": "slower"}],
            "recommended": "A", "why": "fine", "default": "A at 17:00"}))
        old = time.time() - 3600
        os.utime(self.json, (old, old))
        self.origin = f"http://127.0.0.1:{self.port}"

    def answer(self) -> dict | None:
        return json.loads(self.json.read_text()).get("answer")

    def test_a_choice_is_saved_and_the_page_shows_it_answered(self):
        mtime = self.json.stat().st_mtime_ns
        get(self.port, "/decisions/cache-ttl")
        resp = post(self.port, "/decisions/cache-ttl", b"key=B&words=", origin=self.origin)
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/decisions/cache-ttl"))
        saved = self.answer()
        self.assertEqual((saved["key"], saved["words"]), ("B", ""))
        self.assertLess(abs(datetime.fromisoformat(saved["at"]).timestamp() - time.time()), 60)
        self.assertEqual(self.json.stat().st_mtime_ns, mtime)  # the file's time stays when it was asked
        page = get(self.port, "/decisions/cache-ttl").body.decode()
        self.assertIn(">ANSWERED · B<", page)
        self.assertIn("answered, saved", page)
        listing = get(self.port, "/decisions").body.decode()
        self.assertIn('<div class="row pend saved">', listing)

    def test_a_write_in_is_saved_with_the_words_and_goes_back_to_the_list(self):
        resp = post(self.port, "/decisions/cache-ttl", "words=neither — ask the owner&back=/decisions".encode(),
                    host=f"localhost:{self.port}", origin=f"http://localhost:{self.port}")
        self.assertEqual((resp.status, resp.getheader("Location")), (303, "/decisions"))
        self.assertEqual((self.answer()["key"], self.answer()["words"]), (None, "neither — ask the owner"))
        self.assertIn("“neither — ask the owner”", get(self.port, "/decisions").body.decode())

    def test_what_is_not_the_users_answer_is_refused_and_nothing_is_saved(self):
        cases = (("/decisions/cache-ttl", b"key=B", "evil.test", self.origin, 403),
                 ("/decisions/cache-ttl", b"key=B", None, "http://evil.test", 403),
                 ("/decisions/cache-ttl", b"key=B", None, "null", 403),
                 ("/decisions/nope", b"key=B", None, self.origin, 404),
                 ("/decisions/../x", b"key=B", None, self.origin, 404),
                 ("/decisions/cache-ttl", b"key=Z", None, self.origin, 400),
                 ("/decisions/cache-ttl", b"words=+", None, self.origin, 400),
                 ("/decisions/cache-ttl", b"words=" + b"x" * 70000, None, self.origin, 413),
                 ("/decision-cache-ttl.html", b"key=B", None, self.origin, 404))
        for path, body, host, origin, status in cases:
            with self.subTest(path=path, body=body[:20], host=host, origin=origin):
                self.assertEqual(post(self.port, path, body, host=host, origin=origin).status, status)
        self.assertIsNone(self.answer())

    def test_a_decision_linked_out_of_the_pages_dir_is_refused(self):
        """#1009: a path built from the request resolves inside the pages dir or is refused, so a symlink out of it is
        no decision: not served, not read, not answered."""
        outside = self.root / "outside.json"
        outside.write_text(self.json.read_text())
        before = outside.read_bytes()
        link = self.pages / "decision-linked.json"
        link.symlink_to(outside)
        self.assertEqual(get(self.port, "/decisions/linked").status, 404)
        self.assertEqual(post(self.port, "/decisions/linked", b"key=B", origin=self.origin).status, 404)
        self.assertEqual((outside.read_bytes(), link.is_symlink()), (before, True))

    def test_validation_refusal_survives_navigation_read_failures(self):
        for target, error in (("_page_data", OSError("private store error")),
                              ("_tracker_navigation", OSError("private tracker error")),
                              ("_navigation", RuntimeError("private navigation error"))):
            with self.subTest(target=target), mock.patch.object(pg, target, side_effect=error):
                resp = post(self.port, "/decisions/cache-ttl", b"key=Z&words=%3Cdraft%3E", origin=self.origin)
                self.assertEqual(resp.status, 400)
                self.assertIn("&lt;draft&gt;", resp.body.decode())
                self.assertIn("Choose one of the options", resp.body.decode())
                self.assertNotIn("private", resp.body.decode())
                self.assertIn("no-store", resp.getheader("Cache-Control"))
                self.assertIsNone(self.answer())

    def test_an_unreadable_decision_takes_no_answer(self):
        self.json.write_text("{")
        self.assertEqual(post(self.port, "/decisions/cache-ttl", b"key=B", origin=self.origin).status, 409)

    def test_refused_authored_answer_keeps_words_and_hides_parse_details(self):
        resp = post(self.port, "/decisions/cache-ttl", b"key=Z&words=%3Cdraft%3E", origin=self.origin)
        body = resp.body.decode()
        self.assertEqual(resp.status, 400)
        self.assertIn('class="tabs"', body)
        self.assertIn('href="/"', body)
        self.assertIn('&lt;draft&gt;', body)
        self.assertIn('name="words"', body)
        self.assertIn("Cache TTL", body)
        self.assertIn('name="key"', body)
        self.assertIn("no-store", resp.getheader("Cache-Control"))
        self.assertIsNone(self.answer())
        broken = json.loads(self.json.read_text())
        broken["recommended"] = "sensitive-value"
        self.json.write_text(json.dumps(broken))
        resp = post(self.port, "/decisions/cache-ttl", b"words=my+draft", origin=self.origin)
        self.assertEqual(resp.status, 409)
        self.assertNotIn("sensitive-value", resp.body.decode())
        self.assertIn("my draft", resp.body.decode())
        missing = post(self.port, "/decisions/missing", b"words=keep+me", origin=self.origin)
        self.assertEqual(missing.status, 404)
        self.assertIn("keep me", missing.body.decode())
        self.assertIn("no-store", missing.getheader("Cache-Control"))


WAIT = 5.0   # the longest a test waits for something that must happen
QUIET = 0.5  # how long a test watches for something that must not happen


def decision(slug: str) -> str:
    return json.dumps({"headline": slug, "ask": "Which?", "options": [{"key": "A", "title": "Keep", "text": "no change"},
                       {"key": "B", "title": "Drop", "text": "slower"}], "recommended": "A", "why": "fine", "default": "A at 17:00"})


class _Renders:
    """Fake renderers in place of `render_board.write`, `decision_page.write` and `issue_page.write`: each records the page
    it renders on entry and holds until the test releases it, so a test sees how many renders are inside at once.

    The user, 2026-09-26: "allow the board server to have up to 4 workers", then "well, N workers. 4 default"."""

    SETTINGS = ""
    fake_refresh = RenderOnRequestTest.fake_refresh

    def setUp(self):
        RenderOnRequestTest.setUp(self)
        import decision_page
        import issue_page
        import render_board
        self.cond, self.gate = threading.Condition(), threading.Semaphore(0)
        self.inside, self.entered, self.most = [], [], {}
        for target, fake in ((render_board, lambda root, day=None, **inputs: self.render(f"{day}-board.html")),
                             (decision_page, lambda root, pages_dir, slug, day, source=None, navigation=None: self.render(f"decision-{slug}.html")),
                             (issue_page, lambda root, pages_dir, n, **inputs: self.render(f"issue-{n}.html"))):
            patcher = mock.patch.object(target, "write", fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.threads = []
        self.addCleanup(self.finish)

    def render(self, name: str):
        with self.cond:
            self.inside.append(name)
            self.entered.append(name)
            self.most[name] = max(self.most.get(name, 0), self.inside.count(name))
            self.most["*"] = max(self.most.get("*", 0), len(self.inside))
            self.cond.notify_all()
        try:
            self.gate.acquire(timeout=30)
            (self.pages / name).write_text(f"<!doctype html><body><p>{name}</p></body>")
        finally:
            with self.cond:
                self.inside.remove(name)
                self.cond.notify_all()

    def until(self, predicate, timeout: float) -> bool:
        with self.cond:
            return self.cond.wait_for(predicate, timeout)

    def fire(self, call, *args, **kwargs) -> dict:
        box = {}

        def run():
            try:
                box["resp"] = call(self.port, *args, **kwargs)
            except Exception as e:  # a client timeout: the test's own assertions report it
                box["error"] = e
        box["thread"] = threading.Thread(target=run, daemon=True)
        box["thread"].start()
        self.threads.append(box["thread"])
        return box

    def finish(self):
        self.gate.release(1000)
        for t in self.threads:
            t.join(WAIT)

    def assert_up_to(self, n: int):
        """Rule: "The server renders up to N pages at once." Rule: "The N+1th waits. It proceeds when one of the N finishes." """
        first = [self.fire(get, f"/issues/{i}") for i in range(1, n + 1)]
        self.assertTrue(self.until(lambda: len(self.inside) >= n, WAIT),
                        f"{n} different pages were requested; {len(self.inside)} rendered at once")
        last = self.fire(get, f"/issues/{n + 1}")
        self.assertFalse(self.until(lambda: len(self.entered) > n, QUIET), f"page {n + 1} rendered beside {n} others")
        self.gate.release()
        self.assertTrue(self.until(lambda: len(self.entered) > n, WAIT), f"page {n + 1} did not render when one of the {n} finished")
        self.gate.release(n)
        for box in [*first, last]:
            box["thread"].join(WAIT)
            self.assertEqual(box.get("resp") and box["resp"].status, 200, box.get("error"))
        self.assertEqual(self.most["*"], n)


class ConcurrentRenderTest(_Renders, unittest.TestCase):
    """A slow render (the module graph, the source & diff page) no longer holds up every other page."""

    def test_up_to_four_pages_render_at_once_by_default(self):
        # Rule: "N comes from the workspace settings TOML `[pages] workers` ... and defaults to 4." No [pages] here.
        self.assert_up_to(4)

    def test_one_page_renders_once_at_a_time_and_different_pages_overlap(self):
        # Rule: "Two requests for the same page never render it concurrently ... Different pages do overlap."
        a = self.fire(get, "/issues/1")
        self.assertTrue(self.until(lambda: "issue-1.html" in self.inside, WAIT))
        again, other = self.fire(get, "/issues/1"), self.fire(get, "/issues/2")
        self.assertTrue(self.until(lambda: "issue-2.html" in self.inside, WAIT),
                        "a render of another page waited for issue 1's render")
        self.assertFalse(self.until(lambda: self.inside.count("issue-1.html") > 1, QUIET), "issue 1 rendered twice at once")
        self.gate.release(10)
        for box in (a, again, other):
            box["thread"].join(WAIT)
            self.assertEqual(box.get("resp") and box["resp"].status, 200, box.get("error"))
        self.assertEqual(self.most["issue-1.html"], 1)

    def test_an_answer_waits_only_for_a_render_of_its_own_decision(self):
        # Rule: "a decision-answer save never overlaps a render of that same decision page. Different pages do overlap."
        for slug in ("cache-ttl", "log-level"):
            (self.pages / f"decision-{slug}.json").write_text(decision(slug))
        origin = f"http://127.0.0.1:{self.port}"
        page = self.fire(get, "/decisions/cache-ttl")
        self.assertTrue(self.until(lambda: "decision-cache-ttl.html" in self.inside, WAIT))
        same = self.fire(post, "/decisions/cache-ttl", b"key=B", origin=origin)
        other = self.fire(post, "/decisions/log-level", b"key=A", origin=origin)
        other["thread"].join(WAIT)
        self.assertEqual(other.get("resp") and other["resp"].status, 303,
                         f"the answer to another decision waited for this render: {other.get('error')}")
        self.assertEqual(json.loads((self.pages / "decision-log-level.json").read_text())["answer"]["key"], "A")
        same["thread"].join(QUIET)
        self.assertTrue(same["thread"].is_alive(), "the answer was saved while its decision page was rendering")
        self.assertNotIn("answer", json.loads((self.pages / "decision-cache-ttl.json").read_text()))
        self.gate.release(10)
        for box in (page, same):
            box["thread"].join(WAIT)
        self.assertEqual(same.get("resp") and same["resp"].status, 303, same.get("error"))
        self.assertEqual(json.loads((self.pages / "decision-cache-ttl.json").read_text())["answer"]["key"], "B")


class FreshRefusesAForeignNameTest(_PagesFixture, unittest.TestCase):
    """`fresh` takes a name from the URL. CodeQL (#330): a name that is not a plain page file name in pages_dir must
    return "" before any stat, lock or render, whatever its caller checked."""

    NAMES = ("../evil.html", "sub/x.html", "/abs.html", "x/../../evil.html", ".hidden.html", "decision-../../x.html",
             "issue-1/../../evil.html", "workers.html/../../evil.html", "issue-7\0.html")

    def snapshot(self):
        return {str(p): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                for base in (self.root, self.pages, self.root.parent) for p in [base, *sorted(base.glob("*"))] if p.exists()
                and not p.name.startswith(("tmp", "claude"))}

    def spy_on_writers(self):
        import decision_page, issue_page, render_board, source_page, workers_page
        called = []
        for module in (decision_page, issue_page, render_board, source_page, workers_page):
            patch = mock.patch.object(module, "write", lambda *a, _n=module.__name__, **k: called.append(_n))
            patch.start()
            self.addCleanup(patch.stop)
        return called

    def test_a_name_outside_pages_dir_is_refused_before_it_touches_anything(self):
        (self.root / "evil.html").write_text("sentinel")
        before, called = self.snapshot(), self.spy_on_writers()
        for name in self.NAMES:
            with self.subTest(name=name):
                self.assertEqual(pg.fresh(self.root, self.pages, name), "")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(called, [])
                self.assertFalse((self.root.parent / "evil.html").exists())

    def test_a_real_page_name_still_renders(self):
        called = self.spy_on_writers()
        self.assertEqual(pg.fresh(self.root, self.pages, "workers.html"), "")
        self.assertEqual(called, ["workers_page"])


class ConfiguredWorkersTest(_Renders, unittest.TestCase):
    SETTINGS = "[pages]\nworkers = 2\n"

    def test_pages_workers_sets_how_many_render_at_once(self):
        # Rule: "N comes from the workspace settings TOML `[pages] workers`, a positive whole number".
        self.assert_up_to(2)


class SuperviseTest(unittest.TestCase):
    """#735 (folded into #634): the engine's supervisor keeps the board's page server alive (H4), with backoff on
    failed starts, and there is no second LaunchAgent."""

    def setUp(self):
        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        self.root = Path(parent.name).resolve() / "ws"
        self.root.mkdir()
        self.port = free_port()
        self.pages = self.root / "pages"
        self.pages.mkdir()
        (self.pages / f"{datetime.now().date().isoformat()}-board.html").write_text("<p>board</p>")
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: self-hosted; URL http://127.0.0.1:{self.port}/; dir `pages/`\n")

    def stop_server(self):
        pid_file = self.pages / ".pid"
        if pid_file.is_file():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except (OSError, ValueError):
                pass

    def test_restarts_a_killed_page_server_without_an_agent_or_second_launch_agent(self):
        self.addCleanup(self.stop_server)
        stop = threading.Event()
        with mock.patch.object(pg, "POLL", 0.05), mock.patch.object(pg, "RETRY_MIN", 0.05):
            thread = threading.Thread(target=pg.supervise, args=(self.root, stop), daemon=True)
            thread.start()
            self.addCleanup(lambda: thread.join(timeout=5))
            self.addCleanup(stop.set)
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline and pg._who(self.port) is None:
                time.sleep(0.05)
            self.assertEqual(get(self.port, "/").status, 200)
            first_pid = int((self.pages / ".pid").read_text())
            os.kill(first_pid, signal.SIGTERM)
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline and pg.is_pages_serve(first_pid, self.port):
                time.sleep(0.05)
            deadline = time.monotonic() + WAIT
            while time.monotonic() < deadline:
                if (self.pages / ".pid").is_file() and int((self.pages / ".pid").read_text()) != first_pid:
                    if pg._who(self.port) is not None:
                        break
                time.sleep(0.05)
            second_pid = int((self.pages / ".pid").read_text())
            self.assertNotEqual(second_pid, first_pid)
            self.assertEqual(get(self.port, "/").status, 200)
            stop.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_backs_off_failed_and_flapping_starts_up_to_sixty_seconds_then_resets_when_serving(self):
        waits: list[float] = []
        stop = threading.Event()
        outcomes = iter([
            pg.PagesError("port busy"),
            pg.PagesError("port busy"),
            "pages: started",
            "pages: restarted",
            pg.PagesError("port busy"),
            pg.PagesError("port busy"),
            pg.PagesError("port busy"),
            "pages: serving",
            pg.PagesError("port busy"),
        ])

        def fake_ensure(*_a, **_k):
            item = next(outcomes)
            if isinstance(item, Exception):
                raise item
            return item

        def fake_wait(seconds: float) -> bool:
            waits.append(seconds)
            if len(waits) >= 9:
                stop.set()
                return True
            return False

        with mock.patch.object(pg, "ensure", side_effect=fake_ensure), \
             mock.patch.object(stop, "wait", side_effect=fake_wait):
            pg.supervise(self.root, stop)
        self.assertEqual(waits, [1, 2, 4, 8, 16, 32, 60, pg.POLL, 1])

    def test_does_nothing_when_board_is_disabled_or_supervisor_lock_is_held(self):
        import supervisor
        stop = threading.Event()
        with supervisor.lock(supervisor.state_dir(self.root) / "pages-supervisor.lock"), \
             mock.patch.object(pg, "ensure") as ensured:
            pg.supervise(self.root, stop)
        ensured.assert_not_called()

        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: none\n")
        stop2 = threading.Event()
        with mock.patch.object(pg, "ensure") as ensured, \
             mock.patch.object(stop2, "wait", side_effect=lambda _s: (stop2.set(), True)[1]):
            pg.supervise(self.root, stop2)
        ensured.assert_not_called()

    def test_keeps_supervising_through_an_unreadable_config_and_a_board_added_later(self):
        stop, waits = threading.Event(), []
        configs = iter([KeyError("America/Nowhere"), SimpleNamespace(pages_dir=None, board_url=None),
                        SimpleNamespace(pages_dir="pages/", board_url="http://127.0.0.1:1/")])

        def read(_root):
            item = next(configs)
            if isinstance(item, Exception):
                raise item
            return item

        def fake_wait(seconds):
            waits.append(seconds)
            return len(waits) == 3 and (stop.set() or True)

        with mock.patch.object(pg, "read_config", side_effect=read), \
             mock.patch.object(pg, "from_config", return_value=(self.pages, self.port)), \
             mock.patch.object(pg, "ensure", return_value="pages: serving") as ensured, \
             mock.patch.object(stop, "wait", side_effect=fake_wait):
            pg.supervise(self.root, stop)
        self.assertEqual(len(waits), 3)
        ensured.assert_called_once()


class StoreTaskPageEndToEndTest(unittest.TestCase):
    """#919: end-to-end HTTP task pages served from a store-backed workspace via Refresher + board_sources.refresh."""

    def setUp(self) -> None:
        import store

        parent = tempfile.TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        self.root = Path(parent.name) / "ws"
        self.root.mkdir()
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Worktrees: `trees/`\n"
            "- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`\n"
            "- Backlog: GitHub Issues; repo example/repo\n- Settings: `cos.toml`\n"
        )
        (self.root / "cos.toml").write_text(LANES)
        (self.root / "daily").mkdir()
        (self.root / "trees").mkdir()
        self.pages = self.root / "pages"
        self.pages.mkdir()
        store.migrate(self.root / "store.db")
        self.db = store.Store(self.root / "store.db")
        self.addCleanup(self.db.engine.dispose)

    def test_refresher_populates_store_issues_and_serves_distinct_colliding_pages_and_stale_notices(self) -> None:
        import urllib.parse
        import board_sources as bs

        home_key = "github:example/repo#12"
        other_key = "github:other/repo#12"
        self.db.upsert_item(home_key, workflow="build", stage="implement", forge_state="open", title="Home limit")

        fail_next = False

        def fake_gh(args, input_text=None):
            if fail_next:
                return 1, "", "gh: HTTP 503 Service Unavailable"
            query = args[args.index("-f") + 1]
            data = {"rateLimit": {"cost": 1, "remaining": 4900, "limit": 5000}}
            if "inventory: repository" in query:
                data["inventory"] = {"pullRequests": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
            elif "issueOrPullRequest" in query:
                other = 'owner: "other", name: "repo"' in query
                repo = "other/repo" if other else "example/repo"
                body = ("## Context\nOther context paragraph.\n\n## Acceptance\n- [ ] Only other check\n" if other else
                        "## Context\nHome context paragraph.\n\n## Acceptance\n- [x] First home check\n- [ ] Second home check\n")
                data["r0"] = {"n12": {
                    "__typename": "Issue", "number": 12, "url": f"https://github.com/{repo}/issues/12",
                    "title": "Other limit" if other else "Home limit", "state": "OPEN", "labels": {"nodes": []},
                    "body": body, "closedAt": None, "milestone": None,
                }}
            return 0, json.dumps({"data": data}), ""

        done = threading.Event()

        def run_refresh(root, now):
            try:
                return bs.refresh(root, now, gh=fake_gh)
            finally:
                done.set()

        server = pg.make_server(self.pages, 0, root=self.root, refresh=run_refresh, every=3600)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertTrue(done.wait(WAIT))

        for route in ("/issues/12", f"/issues/{urllib.parse.quote(home_key, safe='')}"):
            resp = get(port, route)
            self.assertEqual(resp.status, 200, route)
            body = resp.body.decode()
            self.assertIn("Home context paragraph.", body)
            self.assertIn("Acceptance · 1 of 2 met", body)
            self.assertIn("First home check", body)

        # Add colliding `#12` in `other/repo` and run a refresh cycle.
        self.db.upsert_item(other_key, workflow="build", stage="review", forge_state="open", title="Other limit")
        done.clear()
        server.refresher.wake.set()
        self.assertTrue(done.wait(WAIT))

        self.assertEqual(get(port, "/issues/12").status, 404)
        home_page = get(port, f"/issues/{urllib.parse.quote(home_key, safe='')}").body.decode()
        other_page = get(port, f"/issues/{urllib.parse.quote(other_key, safe='')}").body.decode()
        self.assertIn("Home context paragraph.", home_page)
        self.assertNotIn("Other context paragraph.", home_page)
        self.assertIn("Other context paragraph.", other_page)
        self.assertIn("Only other check", other_page)
        self.assertNotIn("Home context paragraph.", other_page)

        # Fail the next refresh and verify the page keeps cached facts while marking them stale.
        fail_next = True
        time.sleep(0.05)
        done.clear()
        server.refresher.wake.set()
        self.assertTrue(done.wait(WAIT))
        stale_page = get(port, f"/issues/{urllib.parse.quote(home_key, safe='')}").body.decode()
        self.assertIn("Home context paragraph.", stale_page)
        self.assertIn("First home check", stale_page)
        self.assertIn("HTTP 503 Service Unavailable", stale_page)
        self.assertRegex(stale_page, r"(?i)\bstale\b")

def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


class StandaloneBoardTest(unittest.TestCase):
    """#935: a workspace with a Board line and no engine lost its board at a restart, and its .pid named a dead
    process. One board LaunchAgent supervises it; a stale .pid is replaced without signalling anything; the port is
    never substituted; and status tells a missing engine apart from a healthy board."""

    stop_server = SuperviseTest.stop_server

    def setUp(self):
        SuperviseTest.setUp(self)
        self.jobs = self.root.parent / "agents"
        for patch in (mock.patch.object(pg.sys, "platform", "darwin"),
                      mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(self.root.parent / "data")}),
                      mock.patch.object(pg.supervisor, "launchctl", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)

    def wait_serving(self):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and pg._who(self.port) is None:
            time.sleep(0.05)

    def test_pre_handoff_rollback_is_reported_and_current_supervisor_recovers(self):
        self.addCleanup(self.stop_server)
        source = Path(pg.__file__).resolve().parent.parent
        versions = self.root.parent / "install/versions"
        old, new = versions / "old", versions / "new"
        for release, version in ((old, "1.0.0"), (new, "2.0.0")):
            shutil.copytree(source / "scripts", release / "scripts")
            (release / ".claude-plugin").mkdir()
            (release / ".claude-plugin/plugin.json").write_text(json.dumps({"version": version}))
            (release / "chief_of_stuff.py").touch()
            (release / "requirements.txt").touch()
        # Both files are the unmodified 62b1a067 pre-readiness release, not simulated current code.
        for name in ("pages", "supervisor"):
            shutil.copyfile(source / f"evals/fixtures/legacy_{name}.py", old / f"scripts/{name}.py")
        legacy = subprocess.run([sys.executable, str(old / "scripts/pages.py"), "--root", str(self.root),
                                 "--handoff-ready-fd=4", "supervise"], capture_output=True, text=True, timeout=10)
        self.assertEqual(legacy.returncode, 2)
        self.assertIn("unrecognized arguments: --handoff-ready-fd=4", legacy.stderr)
        current = versions.parent / "current"
        current.symlink_to(new)
        code = ("import sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(new / 'scripts')!r}); "
                "import pages; pages.POLL = pages.RETRY_MIN = pages.RETRY_MAX = 0.1; "
                "pages.supervisor.ensure_venv = lambda *a, **k: Path(sys.executable); "
                f"sys.exit(pages.main(['--root', {str(self.root)!r}, 'supervise']))")
        log = self.root / "supervisor-test.log"
        with log.open("w") as output:
            proc = subprocess.Popen([sys.executable, "-c", code], stdout=output, stderr=output)
        def cleanup():
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        self.addCleanup(cleanup)
        def wait_for(predicate):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if predicate():
                        return True
                except (OSError, ValueError):
                    pass
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            return False
        self.assertTrue(wait_for(lambda: (self.pages / ".pid").exists() and pg._who(self.port)))
        current.unlink()
        current.symlink_to(old)
        self.assertTrue(wait_for(lambda: "does not declare board handoff support" in log.read_text()), log.read_text())
        self.assertIsNone(proc.poll())
        pid = int((self.pages / ".pid").read_text())
        os.kill(pid, signal.SIGTERM)
        self.assertTrue(wait_for(lambda: (self.pages / ".pid").exists() and
                                 int((self.pages / ".pid").read_text()) != pid and pg._who(self.port)),
                        "current supervisor did not recover the board")
        time.sleep(0.5)
        self.assertEqual(log.read_text().count("does not declare board handoff support"), 1)
        self.assertNotIn("release " + str(old) + " exited", log.read_text())

    def test_after_a_restart_the_board_agent_serves_the_configured_port_over_a_dead_pid(self):
        self.addCleanup(self.stop_server)
        stale = dead_pid()
        (self.pages / ".pid").write_text(str(stale))
        stop = threading.Event()
        with mock.patch.object(pg, "POLL", 0.05):
            thread = threading.Thread(target=pg.supervise_with_retry, args=(self.root, stop), daemon=True)
            thread.start()
            self.addCleanup(lambda: thread.join(timeout=5))
            self.addCleanup(stop.set)
            self.wait_serving()
            self.assertEqual(get(self.port, "/").status, 200)
            pid = int((self.pages / ".pid").read_text())
            self.assertNotEqual(pid, stale)
            self.assertTrue(pg.is_pages_serve(pid, self.port))

    def test_standalone_follows_an_upgrade_and_restarts_the_server_from_the_new_release(self):
        self.addCleanup(self.stop_server)
        install = self.root.parent / "install"
        versions = install / "versions"
        source = Path(pg.__file__).resolve().parent.parent
        for name, version in (("old", "1.0.0"), ("new", "1.0.0")):
            release = versions / name
            shutil.copytree(source / "scripts", release / "scripts")
            (release / ".claude-plugin").mkdir()
            (release / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
            (release / "chief_of_stuff.py").touch()
            (release / "requirements.txt").write_text("")
        # An earlier installed draft does not advertise a release identity.
        legacy = versions / "old/scripts/pages.py"
        legacy.write_text(legacy.read_text().replace('server_version = f"{SERVER}/{VERSION} release-{RELEASE_ID}"',
                                                    'server_version = f"{SERVER}/{VERSION}"').replace(
            'if identity and identity[1] == RELEASE_ID:', 'if not identity or identity[1] == RELEASE_ID:'))
        current = install / "current"
        current.symlink_to(versions / "old")
        # Exercise a real supervisor process and real servers; stub only venv preparation to avoid pip.
        code = (
            "import sys; from pathlib import Path; "
            f"sys.path.insert(0, {str(versions / 'old' / 'scripts')!r}); "
            "import pages; pages.POLL = pages.RETRY_MIN = pages.RETRY_MAX = 0.1; "
            "pages.supervisor.ensure_venv = lambda *a, **k: Path(sys.executable); "
            f"sys.exit(pages.main(['--root', {str(self.root)!r}, 'supervise']))"
        )
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        self.addCleanup(cleanup)

        def wait_for(predicate):
            deadline = time.monotonic() + WAIT + 10
            while time.monotonic() < deadline:
                if predicate():
                    return True
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            return False

        self.assertTrue(wait_for(lambda: (self.pages / ".pid").exists() and
                                 (pg._who(self.port) or "").startswith(pg.SERVER + "/1.0.0")))
        old_pid = int((self.pages / ".pid").read_text())
        current.unlink()
        current.symlink_to(versions / "new")
        new_id = hashlib.sha256(os.fsencode((versions / "new").resolve())).hexdigest()[:24]
        self.assertTrue(wait_for(lambda: (self.pages / ".pid").exists() and
                                 int((self.pages / ".pid").read_text()) != old_pid and
                                 f"release-{new_id}" in (pg._who(self.port) or "")),
                        "standalone supervisor did not upgrade the server")
        def server_pid():
            try:
                return int((self.pages / ".pid").read_text())
            except (OSError, ValueError):
                return None
        self.assertTrue(wait_for(lambda: server_pid() is not None and pg.is_pages_serve(server_pid(), self.port)))
        first_pid = server_pid()
        # A stale caller with no selection of its own must leave the new, same-version server running.
        code_ensure = (f"import sys; sys.path.insert(0, {str(versions / 'old' / 'scripts')!r}); "
                       "import pages; from pathlib import Path; "
                       f"pages.ensure(Path({str(self.pages)!r}), {self.port}, "
                       f"Path({str(self.root / 'pages.log')!r}), Path({str(self.root)!r}))")
        subprocess.run([sys.executable, "-c", code_ensure], check=True, timeout=10)
        self.assertEqual(server_pid(), first_pid)
        os.kill(first_pid, signal.SIGTERM)
        self.assertTrue(wait_for(lambda: server_pid() is not None and server_pid() != first_pid and
                                 (pg._who(self.port) or "").startswith(pg.SERVER + "/1.0.0")),
                        "the upgraded supervisor did not restart the upgraded server")
        self.assertIsNone(proc.poll())
        upgraded_pid = server_pid()
        current.unlink()
        current.symlink_to(versions / "old")
        self.assertTrue(wait_for(lambda: server_pid() is not None and server_pid() != upgraded_pid and
                                 (pg._who(self.port) or "").startswith(pg.SERVER + "/1.0.0") and
                                 "release-" not in (pg._who(self.port) or "")),
                        "rollback did not restore the earlier release")

    def test_broken_new_release_keeps_the_old_supervisor_alive_to_restore_a_crashed_board(self):
        self.addCleanup(self.stop_server)
        install = self.root.parent / "install"
        versions = install / "versions"
        source = Path(pg.__file__).resolve().parent.parent
        old, broken = versions / "old", versions / "broken"
        shutil.copytree(source / "scripts", old / "scripts")
        (old / ".claude-plugin").mkdir()
        (old / ".claude-plugin/plugin.json").write_text('{"version":"1.0.0"}')
        (old / "chief_of_stuff.py").touch()
        (old / "requirements.txt").touch()
        shutil.copytree(old / "scripts", broken / "scripts")
        shutil.copytree(old / ".claude-plugin", broken / ".claude-plugin")
        (broken / "chief_of_stuff.py").touch()
        (broken / "requirements.txt").touch()
        current = install / "current"
        current.symlink_to(old)
        code = ("import sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(old / 'scripts')!r}); "
                "import pages; pages.POLL = pages.RETRY_MIN = pages.RETRY_MAX = 0.1; "
                "pages.supervisor.ensure_venv = lambda *a, **k: Path(sys.executable); "
                f"sys.exit(pages.main(['--root', {str(self.root)!r}, 'supervise']))")
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        self.addCleanup(cleanup)
        def wait_for(predicate):
            deadline = time.monotonic() + WAIT + 5
            while time.monotonic() < deadline:
                if predicate():
                    return True
                if proc.poll() is not None:
                    return False
                time.sleep(0.05)
            return False
        self.assertTrue(wait_for(lambda: (self.pages / ".pid").exists() and pg._who(self.port)))
        marker = self.root / "candidate-attempted"
        startup_failure = ((old / "scripts/pages.py").read_text().replace(
            'if __name__ == "__main__":',
            f'def ensure(*args, **kwargs):\n    Path({str(marker)!r}).touch()\n'
            '    os.kill(int((args[0] / PID).read_text()), signal.SIGTERM)\n'
            '    raise RuntimeError("server startup failed")\n\nif __name__ == "__main__":'))
        mark = f"from pathlib import Path; Path({str(marker)!r}).touch()\n"
        for failure in (mark + "import missing_release_dependency\n", mark + "raise RuntimeError('startup failed')\n",
                        startup_failure,
                        mark + "import os, sys, time\nfd = int(sys.argv[sys.argv.index('--handoff-ready-fd')+1])\n"
                        "os.write(fd, b'1'); os.close(fd); time.sleep(0.2); sys.exit(1)\n"):
            with self.subTest(failure=failure):
                (broken / "scripts/pages.py").write_text(failure)
                current.unlink()
                current.symlink_to(broken)
                self.assertTrue(wait_for(marker.exists), "candidate was not attempted")
                time.sleep(1)
                self.assertIsNone(proc.poll(), "old supervisor was discarded after exec succeeded")
                self.assertTrue(wait_for(lambda: pg._who(self.port) is not None), "old supervisor did not restore startup failure")
                pid = int((self.pages / ".pid").read_text())
                os.kill(pid, signal.SIGTERM)
                self.assertTrue(wait_for(lambda: (self.pages / ".pid").exists() and
                                         int((self.pages / ".pid").read_text()) != pid and pg._who(self.port)))
                current.unlink()
                current.symlink_to(old)
                marker.unlink()
                time.sleep(0.3)

    def test_live_waiting_successor_that_cannot_recover_falls_back(self):
        self.addCleanup(self.stop_server)
        versions = self.root.parent / "install/versions"
        old, broken = versions / "old", versions / "broken"
        source = Path(pg.__file__).resolve().parent.parent
        for release in (old, broken):
            shutil.copytree(source / "scripts", release / "scripts")
            (release / ".claude-plugin").mkdir()
            (release / ".claude-plugin/plugin.json").write_text('{"version":"1.0.0"}')
            (release / "chief_of_stuff.py").touch()
            (release / "requirements.txt").touch()
        candidate = broken / "scripts/pages.py"
        marker = self.root / "candidate-failed"
        launched = self.root / "candidate-running"
        candidate.write_text(candidate.read_text().replace(
            'if __name__ == "__main__":',
            f'def ensure(*args, **kwargs):\n    Path({str(marker)!r}).touch()\n'
            f'    raise RuntimeError("cannot recover board")\n\nif __name__ == "__main__":\n    Path({str(launched)!r}).touch()').replace(
            'POLL = 5  # seconds between healthy server checks',
            'POLL = 0.1  # seconds between healthy server checks'))
        current = versions.parent / "current"
        current.symlink_to(old)
        code = ("import sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(old / 'scripts')!r}); "
                "import pages; pages.POLL = pages.RETRY_MIN = pages.RETRY_MAX = 0.1; "
                "pages.supervisor.ensure_venv = lambda *a, **k: Path(sys.executable); "
                f"sys.exit(pages.main(['--root', {str(self.root)!r}, 'supervise']))")
        def wait_for(predicate, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return True
                time.sleep(0.05)
            return False
        self.assertEqual(pg.ensure(self.pages, self.port, self.root / "pages.log", self.root), "pages: started")
        old_pid = int((self.pages / ".pid").read_text())
        lock_path = pg.supervisor.state_dir(self.root) / "pages-supervisor.lock"
        with pg.supervisor.lock(lock_path):
            proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            def cleanup():
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)
            self.addCleanup(cleanup)
            current.unlink()
            current.symlink_to(broken)
            # The candidate is alive but cannot run ensure until this owner releases the lock.
            self.assertTrue(wait_for(launched.exists), "candidate was not launched")
            self.assertFalse(marker.exists())
            self.assertIsNone(proc.poll())
            os.kill(old_pid, signal.SIGTERM)
            self.assertTrue(wait_for(lambda: pg._who(self.port) is None))
        self.assertTrue(wait_for(marker.exists), "candidate never tried to recover")
        self.assertTrue(wait_for(lambda: pg._who(self.port) is not None), "parent did not restore the board")
        self.assertIsNone(proc.poll())

    def test_waiting_successor_reports_failure_after_lock_owner_exits(self):
        stop = threading.Event()
        reports = []
        attempted, failed = threading.Event(), threading.Event()
        def report(value):
            reports.append(value)
            if value is False:
                failed.set()
        lock_path = pg.supervisor.state_dir(self.root) / "pages-supervisor.lock"

        def fail(*args, **kwargs):
            attempted.set()
            raise pg.PagesError("candidate cannot recover board")

        with mock.patch.object(pg, "POLL", 0.05), mock.patch.object(pg, "RETRY_MAX", 0.05), \
             mock.patch.object(pg, "ensure", side_effect=fail):
            with pg.supervisor.lock(lock_path):
                thread = threading.Thread(target=pg.supervise_with_retry, args=(self.root, stop, report))
                thread.start()
                self.assertTrue(threading.Event().wait(0.15) is False)
                self.assertNotIn(True, reports, "lock contention is not readiness")
            try:
                self.assertTrue(attempted.wait(2))
                self.assertTrue(failed.wait(2), "failed recovery must reach the parent")
            finally:
                stop.set()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_plugin_selected_update_survives_old_supervisor_and_server_failover(self):
        self.addCleanup(self.stop_server)
        cache = self.root.parent / "plugins/cache/market/chief"
        old, new = cache / "1.0.0", cache / "2.0.0"
        source = Path(pg.__file__).resolve().parent.parent
        for release, version in ((old, "1.0.0"), (new, "2.0.0")):
            shutil.copytree(source / "scripts", release / "scripts")
            (release / ".claude-plugin").mkdir()
            (release / ".claude-plugin/plugin.json").write_text(json.dumps({"version": version}))
            (release / "requirements.txt").touch()
            if release == old:
                # This is the actual pre-selector module, not a stubbed selector.
                shutil.copyfile(source / "evals/fixtures/legacy_supervisor.py", release / "scripts/supervisor.py")
                self.assertNotIn("def selected_release(", (release / "scripts/supervisor.py").read_text())
            page_file = release / "scripts/pages.py"
            page_file.write_text(page_file.read_text().replace(
                'POLL = 5  # seconds between healthy server checks',
                'POLL = 0.1  # seconds between healthy server checks'))
        code = ("import sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(old / 'scripts')!r}); "
                "import pages; pages.POLL = pages.RETRY_MIN = pages.RETRY_MAX = 0.1; "
                "pages.supervisor.ensure_venv = lambda *a, **k: Path(sys.executable); "
                f"sys.exit(pages.main(['--root', {str(self.root)!r}, 'supervise']))")
        log = self.root / "supervisor-test.log"
        with log.open("w") as output:
            proc = subprocess.Popen([sys.executable, "-c", code], stdout=output, stderr=output)
        def cleanup():
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        self.addCleanup(cleanup)
        def wait_for(predicate):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if predicate():
                        return True
                except (OSError, ValueError):
                    pass
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            return False
        def server_id(release):
            return f"release-{hashlib.sha256(os.fsencode(release.resolve())).hexdigest()[:24]}"
        self.assertTrue(wait_for(lambda: (self.pages / ".pid").exists() and server_id(old) in (pg._who(self.port) or "")))
        initial_pid = int((self.pages / ".pid").read_text())
        marker = pg.supervisor.state_dir(self.root) / "engine-plugin-release"
        marker.write_text(str(new))
        def new_server_recorded():
            try:
                pid = int((self.pages / ".pid").read_text())
                return (pid != initial_pid and server_id(new) in (pg._who(self.port) or "")
                        and pg.is_pages_serve(pid, self.port))
            except (OSError, ValueError):
                return False
        self.assertTrue(wait_for(new_server_recorded), f"selected release did not start: {log.read_text()}")
        upgraded_pid = int((self.pages / ".pid").read_text())
        # The old engine's board thread must not replace the selected server.
        code_ensure = (f"import sys; sys.path.insert(0, {str(old / 'scripts')!r}); "
                       "import pages; from pathlib import Path; "
                       f"pages.ensure(Path({str(self.pages)!r}), {self.port}, Path({str(self.root / 'pages.log')!r}), Path({str(self.root)!r}))")
        subprocess.run([sys.executable, "-c", code_ensure], check=True, timeout=10)
        self.assertEqual(int((self.pages / ".pid").read_text()), upgraded_pid)
        os.kill(upgraded_pid, signal.SIGTERM)
        self.assertTrue(wait_for(lambda: server_id(new) in (pg._who(self.port) or "") and
                                 int((self.pages / ".pid").read_text()) != upgraded_pid),
                        "selected release did not restore the server")
        self.assertIsNone(proc.poll())

    def test_old_supervisor_keeps_the_selected_newer_server(self):
        selected = self.root.parent.resolve() / "newer"
        selected.mkdir()
        selected_id = hashlib.sha256(os.fsencode(selected.resolve())).hexdigest()[:24]
        with mock.patch.object(pg, "_selected_release", return_value=selected), \
             mock.patch.object(pg, "_who", return_value=f"{pg.SERVER}/2.0.0 release-{selected_id}"), \
             mock.patch.object(pg, "_stop") as stopped, \
             mock.patch.object(pg.subprocess, "Popen", side_effect=AssertionError("unexpected server start")):
            self.assertEqual(pg.ensure(self.pages, self.port, self.root / "pages.log", self.root), "pages: serving")
        stopped.assert_not_called()

    def test_standalone_follows_plugin_selected_release(self):
        release = self.root.parent.resolve() / "new"
        (release / "scripts").mkdir(parents=True)
        (release / "scripts/pages.py").touch()
        stop, attempted = threading.Event(), threading.Event()
        def prepare(*args, **kwargs):
            attempted.set()
            stop.set()
            return Path(sys.executable)
        with mock.patch.object(pg, "_selected_release", return_value=release), \
             mock.patch.object(pg.supervisor, "current_release", return_value=pg.supervisor.RELEASE), \
             mock.patch.object(pg.supervisor, "ensure_venv", side_effect=prepare), \
             mock.patch.object(pg, "ensure", return_value="pages: serving"), \
             mock.patch.object(pg, "POLL", 0.05):
            thread = threading.Thread(target=pg.supervise_standalone, args=(self.root, stop))
            thread.start()
            try:
                self.assertTrue(attempted.wait(2), "plugin-selected upgrade was not prepared")
            finally:
                stop.set()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_handoff_rejects_workspace_inputs_before_prepare_and_execution(self):
        for change_after_prepare, link_name in ((after, name) for after in (False, True)
                                                for name in ("scripts/pages.py", "scripts/indirect.py",
                                                             "pip-inputs/constraints.txt")):
            with self.subTest(change_after_prepare=change_after_prepare, link_name=link_name):
                release = self.root.parent / (("candidate-after-" if change_after_prepare else "candidate-before-")
                                              + link_name.replace("/", "-"))
                (release / "scripts").mkdir(parents=True)
                entry = release / "scripts/pages.py"
                entry.write_text("# candidate\n")
                (release / "scripts/engine.py").touch()
                (release / "requirements.txt").touch()
                malicious = self.root / "pages-attack.py"
                malicious.write_text("# workspace code\n")
                link = release / link_name
                link.parent.mkdir(parents=True, exist_ok=True)
                def add_link():
                    link.unlink(missing_ok=True)
                    link.symlink_to(malicious)
                if not change_after_prepare:
                    add_link()
                stop = threading.Event()
                prepared = threading.Event()
                rejected = threading.Event()
                real_validate = pg.release_selection.validate_handoff
                def validate(*args):
                    try:
                        real_validate(*args)
                    except pg.supervisor.ServiceError:
                        rejected.set()
                        stop.set()
                        raise
                def prepare(*args, **kwargs):
                    prepared.set()
                    add_link()
                    return Path(sys.executable)
                with mock.patch.object(pg, "_selected_release", return_value=release), \
                     mock.patch.object(pg.release_selection, "validate_handoff", side_effect=validate), \
                     mock.patch.object(pg.supervisor, "ensure_venv", side_effect=prepare), \
                     mock.patch.object(pg, "ensure", return_value="pages: serving"), \
                     mock.patch.object(pg, "POLL", 0.05), \
                     mock.patch.object(pg.subprocess, "Popen") as popen:
                    thread = threading.Thread(target=pg.supervise_standalone, args=(self.root, stop))
                    thread.start()
                    try:
                        self.assertTrue(rejected.wait(3), "unsafe entry was not rejected")
                    finally:
                        stop.set()
                        thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(prepared.is_set(), change_after_prepare)
                    popen.assert_not_called()

    def test_invalid_private_intent_keeps_existing_supervisor_running(self):
        import contextlib
        import io
        data = pg.supervisor.data_dir()
        private = data / "engine-intents"
        private.mkdir(parents=True, mode=0o700)
        intent = private / (pg.supervisor.label("engine", self.root) + ".json")
        intent.write_text("invalid json")
        stop, served, retried = threading.Event(), threading.Event(), threading.Event()
        errors = io.StringIO()
        original_select = pg._selected_release
        def select(root):
            result = original_select(root)
            retried.set()
            return result
        def ensure(*args, **kwargs):
            served.set()
            return "pages: serving"
        with mock.patch.object(pg, "_selected_release", side_effect=select), \
             mock.patch.object(pg, "ensure", side_effect=ensure), \
             mock.patch.object(pg, "POLL", 0.05), \
             mock.patch.object(pg.subprocess, "Popen") as popen, contextlib.redirect_stderr(errors):
            thread = threading.Thread(target=pg.supervise_standalone, args=(self.root, stop))
            thread.start()
            try:
                self.assertTrue(served.wait(2))
                deadline = time.monotonic() + 2
                while "cannot read explicit intent" not in errors.getvalue() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIn("cannot read explicit intent", errors.getvalue())
                self.assertTrue(thread.is_alive())
                popen.assert_not_called()
                intent.unlink()
                self.assertTrue(retried.wait(2), "selection was not retried after repair")
                self.assertTrue(thread.is_alive())
            finally:
                stop.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_failed_upgrade_keeps_supervising_and_shutdown_releases_the_lock(self):
        release = self.root.parent.resolve() / "new"
        (release / "scripts").mkdir(parents=True)
        (release / "scripts" / "pages.py").touch()
        stop, failed, serving = threading.Event(), threading.Event(), threading.Event()
        def prepare(*args, **kwargs):
            failed.set()
            raise pg.supervisor.ServiceError("pip offline")
        def ensure(*args, **kwargs):
            serving.set()
            return "pages: serving"
        with mock.patch.object(pg.supervisor, "current_release", return_value=release), \
             mock.patch.object(pg.supervisor, "ensure_venv", side_effect=prepare), \
             mock.patch.object(pg, "ensure", side_effect=ensure), \
             mock.patch.object(pg, "POLL", 0.05), mock.patch.object(pg.subprocess, "Popen") as popen:
            thread = threading.Thread(target=pg.supervise_standalone, args=(self.root, stop))
            thread.start()
            try:
                self.assertTrue(failed.wait(2))
                self.assertTrue(serving.wait(2))
                self.assertTrue(thread.is_alive())
                popen.assert_not_called()
            finally:
                stop.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            with pg.supervisor.lock(pg.supervisor.state_dir(self.root) / "pages-supervisor.lock"):
                pass

    def test_failed_child_launch_resumes_supervision(self):
        release = self.root.parent.resolve() / "new"
        (release / "scripts").mkdir(parents=True)
        (release / "scripts" / "trusted_module.py").write_text('VALUE = 42\n')
        (release / "scripts" / "pages.py").write_text('import json, os, trusted_module\nassert trusted_module.VALUE == 42\nassert os.getcwd() == "/"\nassert not any(k.upper().startswith("PYTHON") for k in os.environ)\nprint(json.dumps({"ready": True}))\n')
        marker = self.root / "workspace-code-executed"
        for name in ("json", "sitecustomize"):
            (self.root / (name + ".py")).write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError("workspace code")\n')
        real_popen = subprocess.Popen
        launched = []
        stop, attempted, resumed = threading.Event(), threading.Event(), threading.Event()
        def ensure(*args, **kwargs):
            if attempted.is_set():
                resumed.set()
            return "pages: serving"
        def fail_start(argv, **kwargs):
            attempted.set()
            child = real_popen(argv, **kwargs, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out, err = child.communicate(timeout=10)
            launched.append((argv, kwargs, child.returncode, out, err))
            raise OSError("exec failed")
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(self.root)}), \
             mock.patch.object(pg.supervisor, "current_release", return_value=release), \
             mock.patch.object(pg.supervisor, "ensure_venv", return_value=Path(sys.executable)), \
             mock.patch.object(pg, "ensure", side_effect=ensure), \
             mock.patch.object(pg, "POLL", 0.05), mock.patch.object(pg.subprocess, "Popen", side_effect=fail_start):
            thread = threading.Thread(target=pg.supervise_standalone, args=(self.root, stop))
            thread.start()
            try:
                self.assertTrue(attempted.wait(2))
                self.assertTrue(resumed.wait(2))
            finally:
                stop.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(launched)
            argv, kwargs, returncode, out, err = launched[0]
            self.assertEqual(argv[1:3], ["-I", "-c"])
            self.assertEqual(kwargs["cwd"], "/")
            self.assertFalse(any(key.upper().startswith("PYTHON") for key in kwargs["env"]))
            self.assertEqual(returncode, 0, err)
            self.assertIn('"ready": true', out)
            self.assertFalse(marker.exists())

    def test_a_stale_pid_goes_even_when_the_start_fails(self):
        (self.pages / ".pid").write_text(str(dead_pid()))
        dead = mock.Mock(pid=4242, **{"poll.return_value": 1})
        real_popen = subprocess.Popen
        # Only the server start fails; `ps`, which checks the pid's identity, still runs.
        start = lambda argv, **kw: dead if "serve" in argv else real_popen(argv, **kw)  # noqa: E731
        with mock.patch.object(pg.subprocess, "Popen", side_effect=start):
            with self.assertRaisesRegex(pg.PagesError, "did not come up"):
                pg.ensure(self.pages, self.port, self.root / "pages.log")
        self.assertFalse((self.pages / ".pid").exists())

    def test_a_pid_reused_by_another_process_is_never_signalled(self):
        self.addCleanup(self.stop_server)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(other.kill)
        (self.pages / ".pid").write_text(str(other.pid))
        with mock.patch.object(pg.os, "kill", wraps=os.kill) as kill:
            self.assertEqual(pg.ensure(self.pages, self.port, self.root / "pages.log"), "pages: started")
        kill.assert_not_called()
        self.assertIsNone(other.poll())
        self.assertNotEqual(int((self.pages / ".pid").read_text()), other.pid)

    def test_a_silent_server_the_pid_still_names_is_reported_not_killed_or_moved(self):
        (self.pages / ".pid").write_text("4242")
        with mock.patch.object(pg, "_who", return_value=None), \
             mock.patch.object(pg, "is_pages_serve", return_value=True), \
             mock.patch.object(pg.os, "kill") as kill, \
             mock.patch.object(pg.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(pg.PagesError, f"pid 4242 .* port {self.port}"):
                pg.ensure(self.pages, self.port, self.root / "pages.log")
        kill.assert_not_called()
        popen.assert_not_called()
        self.assertEqual((self.pages / ".pid").read_text(), "4242")

    def test_a_foreign_server_on_the_port_never_moves_the_board_elsewhere(self):
        with mock.patch.object(pg, "_who", return_value="nginx"), \
             mock.patch.object(pg.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(pg.PagesError, f"port {self.port} answers as nginx"):
                pg.ensure(self.pages, self.port, self.root / "pages.log")
        popen.assert_not_called()

    def test_the_board_agent_waits_while_another_supervisor_holds_the_lock(self):
        stop, waits = threading.Event(), []
        with mock.patch.object(pg, "supervise") as supervised, \
             mock.patch.object(stop, "wait", side_effect=lambda s: (waits.append(s), len(waits) == 2 and stop.set())[1]):
            pg.supervise_with_retry(self.root, stop)
        self.assertEqual(supervised.call_count, 2)  # it does not exit for launchd to relaunch it every ten seconds

    def test_install_writes_one_board_agent_for_the_configured_workspace(self):
        self.assertEqual(pg.install(self.root, self.jobs), "pages: started")
        [path] = self.jobs.glob("*.plist")
        import plistlib
        job = plistlib.loads(path.read_bytes())
        self.assertEqual(job["Label"], pg.supervisor.label("pages", self.root))
        self.assertEqual(job["ProgramArguments"][1:], [str(Path(pg.__file__).resolve()), "--root",
                                                       str(self.root.resolve()), "supervise"])
        self.assertTrue(job["RunAtLoad"] and job["KeepAlive"])
        self.assertEqual(job["WorkingDirectory"], str(self.root.resolve()))

    def test_install_with_an_engine_adds_a_fallback_and_refuses_without_a_board(self):
        engine = pg.supervisor.job_path(pg.supervisor.label("engine", self.root), self.jobs)
        engine.parent.mkdir(parents=True)
        engine.write_bytes(b"")
        self.assertEqual(pg.install(self.root, self.jobs), "pages: started")
        board = pg.supervisor.job_path(pg.supervisor.label("pages", self.root), self.jobs)
        self.assertTrue(board.exists())
        (self.root / "CLAUDE.md").write_text(
            "## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n- Tracker: `daily/<date>-tracker.md`\n"
            f"- Timezone: {ZONE}\n- Board: none\n")
        engine.unlink()
        board.unlink()
        with self.assertRaises(pg.PagesError):
            pg.install(self.root, self.jobs)
        self.assertEqual(list(self.jobs.glob("*.plist")), [])

    def test_waiter_takes_over_after_the_active_supervisor_stops(self):
        stop = threading.Event()
        entered = threading.Event()
        lock_path = pg.supervisor.state_dir(self.root) / "pages-supervisor.lock"

        def ensure(*args, **kwargs):
            entered.set()
            return "pages: serving"
        with mock.patch.object(pg, "RETRY_MAX", 0.05), mock.patch.object(pg, "ensure", side_effect=ensure):
            thread = threading.Thread(target=pg.supervise_with_retry, args=(self.root, stop), daemon=True)
            try:
                with pg.supervisor.lock(lock_path):
                    thread.start()
                    self.assertFalse(entered.wait(0.15))
                self.assertTrue(entered.wait(2), "waiting supervisor did not acquire the released lock")
            finally:
                stop.set()
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_status_prefers_the_independent_board_agent_when_both_are_installed(self):
        self.assertEqual(pg.install(self.root, self.jobs), "pages: started")
        engine = pg.supervisor.job_path(pg.supervisor.label("engine", self.root), self.jobs)
        engine.write_bytes(b"")
        self.assertEqual(pg.status(self.root, self.jobs)["agent"], "board")

    def test_status_tells_a_missing_engine_from_a_healthy_board(self):
        self.addCleanup(self.stop_server)
        pg.ensure(self.pages, self.port, self.root / "pages.log")
        report = pg.status(self.root, self.jobs)
        self.assertEqual((report["board"], report["engine"], report["agent"], report["port"]),
                         ("serving", "missing", "none", self.port))
        self.assertTrue(report["pid_live"])
        import supervisor
        with supervisor.lock(supervisor.state_dir(self.root) / "engine.lock"):
            self.assertEqual(pg.status(self.root, self.jobs)["engine"], "running")

    def test_status_names_a_down_board_and_its_stale_pid(self):
        (self.pages / ".pid").write_text(str(dead_pid()))
        report = pg.status(self.root, self.jobs)
        self.assertEqual((report["board"], report["engine"], report["supervised"], report["pid_live"]),
                         ("down", "missing", False, False))


class CrossProcessPublicationTest(unittest.TestCase):
    setUp = RenderOnRequestTest.setUp
    fake_refresh = RenderOnRequestTest.fake_refresh

    def test_two_servers_serialize_same_page_publications(self):
        import select
        scripts = Path(__file__).resolve().parent.parent / 'scripts'
        code = '''import sys, threading, contextlib
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import pages, render_board
root = Path(sys.argv[2]); directory = root / "pages"
original_lock = pages.publication_lock if hasattr(pages, "publication_lock") else None
if original_lock:
    @contextlib.contextmanager
    def observed_lock(root, path):
        print("attempting", flush=True)
        with original_lock(root, path):
            yield
    pages.publication_lock = observed_lock
def write(root, day, **kw):
    print("entered", flush=True)
    sys.stdin.readline()
    (directory / (day + "-board.html")).write_text("<body>" + sys.argv[3] + "</body>")
render_board.write = write
server = pages.make_server(directory, 0, root=root, refresh=lambda *a: None)
print(server.server_address[1], flush=True)
server.serve_forever()
'''
        processes = []
        for label in ('first', 'second'):
            proc = subprocess.Popen([sys.executable, '-u', '-c', code, str(scripts), str(self.root), label],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            processes.append(proc)
            def cleanup(p=proc):
                if p.poll() is None:
                    p.kill()
                p.communicate(timeout=10)
            self.addCleanup(cleanup)
        def line(proc):
            self.assertTrue(select.select([proc.stdout], [], [], 10)[0], 'server did not signal in time')
            return proc.stdout.readline().decode().strip()
        ports = [int(line(p)) for p in processes]
        results = []
        threads = [threading.Thread(target=lambda port=p: results.append(get(port, '/')), daemon=True) for p in ports]
        threads[0].start()
        self.assertEqual(line(processes[0]), 'attempting')
        self.assertEqual(line(processes[0]), 'entered')
        threads[1].start()
        self.assertEqual(line(processes[1]), 'attempting')
        try:
            self.assertFalse(select.select([processes[1].stdout], [], [], 0.6)[0], 'second server rendered concurrently')
        finally:
            processes[0].stdin.write(b'go\n'); processes[0].stdin.flush()
        self.assertEqual(line(processes[1]), 'entered')
        processes[1].stdin.write(b'go\n'); processes[1].stdin.flush()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual([r.status for r in results], [200, 200])
        again = threading.Thread(target=lambda: results.append(get(ports[0], '/')), daemon=True)
        again.start()
        self.assertEqual(line(processes[0]), 'attempting')
        self.assertEqual(line(processes[0]), 'entered')
        processes[0].stdin.write(b'go\n'); processes[0].stdin.flush()
        again.join(10)
        self.assertFalse(again.is_alive())
        self.assertIn(b'first', results[-1].body)

    def test_stalled_static_download_does_not_block_board_cli(self):
        (self.pages / 'large.bin').write_bytes(b'x' * (20 * 1024 * 1024))
        client = socket.socket()
        self.addCleanup(client.close)
        self.server.handle_error = lambda *args: None
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.settimeout(10)
        client.connect(('127.0.0.1', self.port))
        client.sendall(f'GET /large.bin HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n'.encode())
        self.assertIn(b'200', client.recv(1024))
        script = Path(__file__).resolve().parent.parent / 'scripts' / 'render_board.py'
        proc = subprocess.run([sys.executable, str(script), '--root', str(self.root), '--date', self.day],
                              capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_authored_decision_uses_served_board_day_for_references(self):
        from datetime import timedelta
        import decision_page
        today = datetime.fromisoformat(self.day).date()
        board_day = (today - timedelta(days=2)).isoformat()
        newer_day = (today - timedelta(days=1)).isoformat()
        self.tracker.unlink()
        (self.root / 'daily' / f'{board_day}-tracker.md').write_text(ISSUE_TRACKER.format(day=board_day, task='Board task'))
        (self.root / 'daily' / f'{newer_day}-tracker.md').write_text(ISSUE_TRACKER.format(day=newer_day, task='Newer task').replace('implement', 'review'))
        (self.pages / f'{board_day}-board.html').write_text('<body>board</body>')
        decision = json.loads(DECISION)
        decision['references'] = [{'kind': 'issue', 'value': '#7'}]
        (self.pages / 'decision-cache-ttl.json').write_text(json.dumps(decision))
        original = decision_page.write
        seen = []
        def observe(root, directory, name, day, **kw):
            ctx = decision_page.context(root, day)
            seen.append((day, [(task.item, task.stage) for task in ctx.tasks]))
            return original(root, directory, name, day, **kw)
        with mock.patch.object(decision_page, 'write', side_effect=observe):
            response = get(self.port, '/decisions/cache-ttl')
            self.assertEqual(response.status, 200)
        self.assertEqual(seen, [(board_day, [('Board task', 'implement')])])
        self.assertIn(b'<span class="st">implement</span>', response.body)

    def test_large_static_downloads_and_head(self):
        payload = b"PDF-video-data\x00" * (2 * 1024 * 1024 + 1)
        for name in ("large.pdf", "large.mp4"):
            (self.pages / name).write_bytes(payload)
            for method in ("GET", "HEAD"):
                with self.subTest(name=name, method=method):
                    response = get(self.port, "/" + name, method=method)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Length"), str(len(payload)))
                    self.assertEqual(response.body, payload if method == "GET" else b"")

    def test_head_never_allocates_a_snapshot(self):
        (self.pages / "asset.bin").write_bytes(b"asset")
        with mock.patch.object(pg.tempfile, "TemporaryFile", side_effect=AssertionError("HEAD copied file")):
            response = get(self.port, "/asset.bin", method="HEAD")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Length"), "5")

    def test_snapshot_storage_failure_is_not_a_missing_file(self):
        (self.pages / "asset.bin").write_bytes(b"asset")
        with mock.patch.object(pg.tempfile, "TemporaryFile", side_effect=OSError(28, "no space")):
            response = get(self.port, "/asset.bin")
        self.assertEqual(response.status, 503)
        self.assertEqual(pg.STATIC_SNAPSHOT_SLOTS, 0)
        real_temp = pg.tempfile.TemporaryFile
        opened = []
        class FailingWrite:
            def __init__(self):
                self.file = real_temp()
                opened.append(self.file)
            def fileno(self):
                return self.file.fileno()
            def write(self, data):
                raise OSError(28, "disk full")
            def close(self):
                self.file.close()
        with mock.patch.object(pg.tempfile, "TemporaryFile", side_effect=FailingWrite):
            self.assertEqual(get(self.port, "/asset.bin").status, 503)
        self.assertTrue(opened[0].closed)
        self.assertEqual(pg.STATIC_SNAPSHOT_SLOTS, 0)
        with mock.patch.object(pg.Handler, "_open_static", side_effect=OSError(24, "too many files")):
            self.assertEqual(get(self.port, "/asset.bin").status, 503)
        self.assertEqual(get(self.port, "/asset.bin").status, 200)

    def test_concurrent_snapshots_reserve_storage_until_client_disconnects(self):
        (self.pages / "asset.bin").write_bytes(b"x" * (4 * 1024 * 1024))
        client = socket.socket()
        self.addCleanup(client.close)
        self.server.handle_error = lambda *args: None
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.settimeout(10)
        with mock.patch.object(pg, "MAX_STATIC_SNAPSHOT_SLOTS", 1):
            client.connect(("127.0.0.1", self.port))
            client.sendall(f"GET /asset.bin HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
            self.assertIn(b"200", client.recv(1024))
            self.assertEqual(get(self.port, "/asset.bin").status, 503)
            self.assertEqual(get(self.port, "/asset.bin", method="HEAD").status, 200)
            client.close()
            for _ in range(100):
                if pg.STATIC_SNAPSHOT_SLOTS == 0:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(pg.STATIC_SNAPSHOT_SLOTS, 0)
            self.assertEqual(get(self.port, "/asset.bin").status, 200)

    def test_cli_can_publish_while_static_copy_is_paused(self):
        (self.pages / "asset.bin").write_bytes(b"x" * 100_000)
        entered, release = threading.Event(), threading.Event()
        real_open = pg.Handler._open_static
        class PausedFile:
            def __init__(self, stream):
                self.stream = stream
            def fileno(self):
                return self.stream.fileno()
            def read(self, size):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("copy not released")
                return self.stream.read(size)
            def close(self):
                self.stream.close()
        results = []
        with mock.patch.object(pg.Handler, "_open_static", lambda handler, name: PausedFile(real_open(handler, name))), \
             mock.patch.object(pg, "MAX_STATIC_SNAPSHOT_SLOTS", 1):
            thread = threading.Thread(target=lambda: results.append(get(self.port, "/asset.bin")), daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(get(self.port, "/asset.bin").status, 503)
                script = Path(__file__).resolve().parent.parent / "scripts" / "render_board.py"
                proc = subprocess.run([sys.executable, str(script), "--root", str(self.root), "--date", self.day],
                                      capture_output=True, text=True, timeout=5)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            finally:
                release.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status, 200)

    def test_cli_can_publish_while_static_open_is_paused(self):
        (self.pages / "asset.bin").write_bytes(b"asset")
        entered, release = threading.Event(), threading.Event()
        real_open = pg.Handler._open_static
        def paused_open(handler, name):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("open not released")
            return real_open(handler, name)
        results = []
        with mock.patch.object(pg.Handler, "_open_static", paused_open):
            thread = threading.Thread(target=lambda: results.append(get(self.port, "/asset.bin")), daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                script = Path(__file__).resolve().parent.parent / "scripts" / "render_board.py"
                proc = subprocess.run([sys.executable, str(script), "--root", str(self.root), "--date", self.day],
                                      capture_output=True, text=True, timeout=5)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            finally:
                release.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status, 200)

    def test_snapshot_capacity_allows_large_files_and_reserves_concurrent_sizes(self):
        large = 2 * 1024**3
        with mock.patch.object(pg, "_static_free_bytes", return_value=3 * large + pg.STATIC_FREE_RESERVE - 1):
            first = pg.StaticSnapshot(large)
            try:
                with self.assertRaises(OSError):
                    pg.StaticSnapshot(2 * large)
                self.assertEqual(pg.STATIC_SNAPSHOT_SLOTS, 1)
            finally:
                first.close()
            second = pg.StaticSnapshot(2 * large)
            second.close()
        self.assertEqual(pg.STATIC_SNAPSHOT_SLOTS, 0)
        self.assertEqual(pg.STATIC_RESERVED_BYTES, 0)

    def test_insufficient_temporary_space_returns_503_and_releases_slot(self):
        (self.pages / "asset.bin").write_bytes(b"asset")
        with mock.patch.object(pg, "_static_free_bytes", return_value=pg.STATIC_FREE_RESERVE + 4):
            self.assertEqual(get(self.port, "/asset.bin").status, 503)
        self.assertEqual(pg.STATIC_SNAPSHOT_SLOTS, 0)
        self.assertEqual(pg.STATIC_RESERVED_BYTES, 0)
        self.assertEqual(get(self.port, "/asset.bin").status, 200)

    def test_static_paths_cannot_escape_or_follow_external_symlinks(self):
        outside = self.root / "secret.bin"
        outside.write_bytes(b"secret")
        (self.pages / "escape.bin").symlink_to(outside)
        (self.pages / "escape-dir").symlink_to(self.root, target_is_directory=True)
        (self.pages / ".hidden").write_bytes(b"hidden")
        (self.pages / "hidden-link.bin").symlink_to(self.pages / ".hidden")
        (self.pages / "visible.bin").write_bytes(b"visible")
        (self.pages / "inside-link.bin").symlink_to(self.pages / "visible.bin")
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.pages / "pipe.bin")
        for method in ("GET", "HEAD"):
            for path in ("/escape.bin", "/escape-dir/secret.bin", "/%2e%2e/secret.bin", "/.hidden", "/hidden-link.bin", "/pipe.bin"):
                with self.subTest(method=method, path=path):
                    self.assertEqual(get(self.port, path, method=method).status, 404)
            self.assertEqual(get(self.port, "/escape.bin", method=method, host="evil.test").status, 403)
            self.assertEqual(get(self.port, "/inside-link.bin", method=method).status, 200)

    def test_static_directory_swap_cannot_escape_during_open(self):
        safe = self.pages / "safe"
        safe.mkdir()
        (safe / "asset.bin").write_bytes(b"safe")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "asset.bin").write_bytes(b"secret")
        real_open = os.open
        swapped = threading.Event()
        def swap_before_open(path, flags, *args, **kwargs):
            if path == "safe" and kwargs.get("dir_fd") is not None and not swapped.is_set():
                safe.rename(self.pages / "moved")
                safe.symlink_to(outside, target_is_directory=True)
                swapped.set()
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(pg.os, "open", side_effect=swap_before_open):
            response = get(self.port, "/safe/asset.bin")
        self.assertTrue(swapped.is_set())
        self.assertEqual(response.status, 404)
        self.assertNotIn(b"secret", response.body)

    def test_static_root_swap_cannot_redirect_open(self):
        (self.pages / "asset.bin").write_bytes(b"safe")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "asset.bin").write_bytes(b"secret")
        moved = self.root / "moved-pages"
        real_open = os.open
        swapped = threading.Event()
        def swap_before_open(path, flags, *args, **kwargs):
            if os.fspath(path) == str(self.pages.resolve()) and not swapped.is_set():
                self.pages.rename(moved)
                self.pages.symlink_to(outside, target_is_directory=True)
                swapped.set()
            return real_open(path, flags, *args, **kwargs)
        try:
            with mock.patch.object(pg.os, "open", side_effect=swap_before_open):
                response = get(self.port, "/asset.bin")
            self.assertTrue(swapped.is_set())
            self.assertEqual(response.status, 404)
            self.assertNotIn(b"secret", response.body)
        finally:
            if swapped.is_set():
                self.pages.unlink()
                moved.rename(self.pages)

    def test_replaced_static_root_is_refused_for_get_and_head(self):
        (self.pages / "asset.bin").write_bytes(b"safe")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "asset.bin").write_bytes(b"secret")
        moved = self.root / "moved-pages"
        self.pages.rename(moved)
        self.pages.symlink_to(outside, target_is_directory=True)
        try:
            for method in ("GET", "HEAD"):
                response = get(self.port, "/asset.bin", method=method)
                self.assertEqual(response.status, 404)
                self.assertNotIn(b"secret", response.body)
        finally:
            self.pages.unlink()
            moved.rename(self.pages)

    def test_static_in_place_truncate_while_transmitting_keeps_body_and_metadata(self):
        path = self.pages / "asset.bin"
        original = b"original data" * 100_000
        path.write_bytes(original)
        head = get(self.port, "/asset.bin", method="HEAD")
        real_send = pg.Handler._send_static
        class TruncateDuringTransmission:
            def __init__(self, stream):
                self.stream = stream
                self.reads = 0
            def read(self, size):
                self.reads += 1
                if self.reads == 2:
                    path.write_bytes(b"changed")
                return self.stream.read(size)
            def close(self):
                self.stream.close()
        def wrap_stream(handler, body):
            finish = real_send(handler, body)
            def wrapped():
                result = finish()
                return (TruncateDuringTransmission(result[0]), result[1])
            return wrapped
        with mock.patch.object(pg.Handler, "_send_static", wrap_stream):
            response = get(self.port, "/asset.bin")
        self.assertEqual(path.read_bytes(), b"changed")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, original)
        self.assertEqual(head.body, b"")
        for header in ("Content-Length", "Last-Modified", "Content-Type"):
            self.assertEqual(head.getheader(header), response.getheader(header))
        self.assertEqual(response.getheader("Content-Length"), str(len(original)))

    def test_static_change_during_snapshot_is_refused(self):
        path = self.pages / "asset.bin"
        path.write_bytes(b"original" * 100_000)
        real_open = pg.Handler._open_static
        class ChangingFile:
            def __init__(self, stream):
                self.stream = stream
            def fileno(self):
                return self.stream.fileno()
            def read(self, size):
                data = self.stream.read(size)
                path.write_bytes(b"changed")
                return data
            def close(self):
                self.stream.close()
        with mock.patch.object(pg.Handler, "_open_static", lambda handler, name: ChangingFile(real_open(handler, name))):
            response = get(self.port, "/asset.bin")
        self.assertEqual(response.status, 503)
        self.assertNotIn(b"original", response.body)

    def test_static_metadata_change_during_snapshot_is_retryable(self):
        path = self.pages / "asset.bin"
        path.write_bytes(b"original")
        real_open = pg.Handler._open_static
        class ChangingFile:
            def __init__(self, stream):
                self.stream = stream
            def fileno(self):
                return self.stream.fileno()
            def read(self, size):
                data = self.stream.read(size)
                os.utime(path, ns=(1_000_000_000, 1_000_000_000))
                return data
            def close(self):
                self.stream.close()
        with mock.patch.object(pg.Handler, "_open_static", lambda handler, name: ChangingFile(real_open(handler, name))):
            response = get(self.port, "/asset.bin")
        self.assertEqual(response.status, 503)
        self.assertNotIn(b"original", response.body)

    def test_static_conditional_request_uses_open_file_timestamp(self):
        (self.pages / "asset.bin").write_bytes(b"asset")
        response = get(self.port, "/asset.bin")
        for method in ("GET", "HEAD"):
            cached = get(self.port, "/asset.bin", method=method, headers={"If-Modified-Since": response.getheader("Last-Modified")})
            self.assertEqual(cached.status, 304)
            self.assertEqual(cached.body, b"")

    def test_static_open_file_survives_atomic_replacement(self):
        path = self.pages / "asset.bin"
        original = b"old contents"
        path.write_bytes(original)
        real_open = pg.Handler._open_static
        def replace_after_open(handler, name):
            stream = real_open(handler, name)
            replacement = path.with_suffix(".new")
            replacement.write_bytes(b"replacement has a different length")
            replacement.replace(path)
            return stream
        with mock.patch.object(pg.Handler, "_open_static", replace_after_open):
            response = get(self.port, "/asset.bin")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Length"), str(len(original)))
        self.assertEqual(response.body, original)



if __name__ == "__main__":
    unittest.main()
