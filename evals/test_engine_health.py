"""#936: reliable engine pass status/liveness, board & task page health warnings, and server freshness transitions."""

from __future__ import annotations

import contextlib
import http.client
import io
import os
import signal
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "evals")]

import board_sources as bs  # noqa: E402
import engine  # noqa: E402
import issue_page  # noqa: E402
import pages as pg  # noqa: E402
import store  # noqa: E402
import supervisor  # noqa: E402
import task_forge  # noqa: E402
import workspace as ws  # noqa: E402

ZONE = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 9, 14, 0, tzinfo=ZONE)
KEY = "github:example/repo#12"

CLAUDE_MD = """# Workspace

## Coordinator
- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Timezone: America/Chicago
- Board: self-hosted; URL http://127.0.0.1:8765/; dir `pages/`
- Backlog: GitHub issues; repo example/repo
- Settings: `cos.toml`
"""

COS_TOML = """[lanes]
build = { stages = ["plan", "implement", "review", "fix", "merge"] }

[kanban]
stages = ["Planning", "In progress", "In review", "Fixing", "Merging"]
human_review_label = "Needs input"

[kanban.map]
plan = 0
implement = 1
review = 2
fix = 3
merge = 4

[workers]
max_concurrency = 4
"""


class _FakeForge:
    github = True

    class home:
        repo = "example/repo"


def make_workspace(*, with_store: bool = True) -> tuple[tempfile.TemporaryDirectory[str], Path, store.Store | None]:
    parent = tempfile.TemporaryDirectory()
    root = (Path(parent.name) / "ws").resolve()
    root.mkdir()
    (root / "CLAUDE.md").write_text(CLAUDE_MD, encoding="utf-8")
    (root / "cos.toml").write_text(COS_TOML, encoding="utf-8")
    (root / "pages").mkdir()
    (root / "trees").mkdir()
    if not with_store:
        return parent, root, None
    db_path = root / "store.db"
    store.migrate(db_path)
    db = store.Store(db_path, clock=lambda: NOW)
    return parent, root, db


class EngineStatusLifecycleTest(unittest.TestCase):
    """Engine loop and --once record pass timestamps, startup/pass errors, and stop timestamps under engine.lock."""

    def setUp(self) -> None:
        parent, self.root, self.db = make_workspace(with_store=False)
        self.addCleanup(parent.cleanup)

    def test_running_loop_records_live_status_and_sets_stopped_at_on_exit(self) -> None:
        observed_while_running: list[ws.EngineStatus | None] = []

        def check_during_sleep(_seconds: float) -> None:
            observed_while_running.append(ws.read_engine_status(self.root))
            raise KeyboardInterrupt

        with mock.patch.object(engine.forge, "for_workspace", return_value=_FakeForge()), \
             mock.patch.object(engine.supervisor, "current_release", return_value=engine.supervisor.RELEASE), \
             mock.patch("pages.supervise"), \
             mock.patch.object(engine, "run_pass"), \
             mock.patch.object(engine.time, "sleep", side_effect=check_during_sleep):
            self.assertEqual(engine.main(["--root", str(self.root), "--interval", "45"]), 0)

        self.assertEqual(len(observed_while_running), 1)
        live = observed_while_running[0]
        self.assertIsNotNone(live)
        assert live is not None
        self.assertTrue(live.running)
        self.assertTrue(live.present)
        self.assertEqual(live.interval, 45.0)
        self.assertIsNotNone(live.started_at)
        self.assertIsNotNone(live.last_pass_at)
        self.assertIsNone(live.stopped_at)
        self.assertIsNone(live.last_error)

        after = ws.read_engine_status(self.root)
        self.assertIsNotNone(after)
        assert after is not None
        self.assertFalse(after.running)
        self.assertIsNotNone(after.last_pass_at)
        self.assertIsNotNone(after.stopped_at)

    def test_sigterm_during_sleep_records_stopped_at_cleanly(self) -> None:
        def terminate_during_sleep(_seconds: float) -> None:
            os.kill(os.getpid(), signal.SIGTERM)

        with mock.patch.object(engine.forge, "for_workspace", return_value=_FakeForge()), \
             mock.patch.object(engine.supervisor, "current_release", return_value=engine.supervisor.RELEASE), \
             mock.patch("pages.supervise"), \
             mock.patch.object(engine, "run_pass"), \
             mock.patch.object(engine.time, "sleep", side_effect=terminate_during_sleep):
            self.assertEqual(engine.main(["--root", str(self.root)]), 0)

        after = ws.read_engine_status(self.root)
        assert after is not None
        self.assertFalse(after.running)
        self.assertIsNotNone(after.stopped_at)

    def test_failed_pass_preserves_prior_last_pass_at_and_next_success_clears_error(self) -> None:
        snapshots: list[ws.EngineStatus | None] = []
        calls = 0

        def sleep_probe(_seconds: float) -> None:
            nonlocal calls
            calls += 1
            snapshots.append(ws.read_engine_status(self.root))
            if calls >= 3:
                raise KeyboardInterrupt

        with mock.patch.object(engine.forge, "for_workspace", return_value=_FakeForge()), \
             mock.patch.object(engine.supervisor, "current_release", return_value=engine.supervisor.RELEASE), \
             mock.patch("pages.supervise"), \
             mock.patch.object(engine, "run_pass", side_effect=[None, RuntimeError("forge timeout"), None]), \
             mock.patch.object(engine.time, "sleep", side_effect=sleep_probe), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(engine.main(["--root", str(self.root)]), 0)

        first, failed, recovered = snapshots
        assert first is not None and failed is not None and recovered is not None
        self.assertIsNone(first.last_error)
        self.assertIsNotNone(first.last_pass_at)

        self.assertEqual(failed.last_pass_at, first.last_pass_at)
        self.assertEqual(failed.error_kind, "pass")
        self.assertIn("forge timeout", failed.last_error or "")

        self.assertIsNone(recovered.last_error)
        self.assertIsNone(recovered.error_kind)
        assert recovered.last_pass_at is not None and first.last_pass_at is not None
        self.assertGreaterEqual(recovered.last_pass_at, first.last_pass_at)

    def test_startup_settings_error_records_startup_failure_and_preserves_prior_last_pass(self) -> None:
        with mock.patch.object(engine.forge, "for_workspace", return_value=_FakeForge()), \
             mock.patch.object(engine, "run_pass"):
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 0)
        before = ws.read_engine_status(self.root)
        assert before is not None and before.last_pass_at is not None

        (self.root / "cos.toml").write_text("[models.deep]\nruntime = 'codex'\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 2)

        after = ws.read_engine_status(self.root)
        assert after is not None
        self.assertFalse(after.running)
        self.assertEqual(after.last_pass_at, before.last_pass_at)
        self.assertEqual(after.error_kind, "startup")
        self.assertIn("models.deep", after.last_error or "")
        self.assertIsNotNone(after.stopped_at)

    def test_held_lock_refusal_does_not_overwrite_live_engine_status(self) -> None:
        t_pass = NOW - timedelta(minutes=2)
        ws.write_engine_status(
            self.root,
            started_at=t_pass - timedelta(minutes=10),
            last_pass_at=t_pass,
            last_attempt_at=t_pass,
            stopped_at=None,
            interval=60.0,
            mode="service",
        )
        raw_before = (supervisor.state_dir(self.root) / ws.ENGINE_STATUS_FILE).read_text(encoding="utf-8")
        with supervisor.lock(supervisor.state_dir(self.root) / ws.ENGINE_LOCK_FILE), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(engine.main(["--root", str(self.root), "--once"]), 2)
        raw_after = (supervisor.state_dir(self.root) / ws.ENGINE_STATUS_FILE).read_text(encoding="utf-8")
        self.assertEqual(raw_after, raw_before)


class EngineBoardAndTaskHealthTest(unittest.TestCase):
    """Board and task pages surface running, stopped, failed, stale, missing, and corrupt engine status honestly."""

    def setUp(self) -> None:
        parent, self.root, db = make_workspace(with_store=True)
        assert db is not None
        self.db = db
        self.addCleanup(parent.cleanup)
        self.addCleanup(self.db.engine.dispose)
        self.db.upsert_item(KEY, workflow="build", stage="review", forge_state="open",
                            title="Upload size limit", human_review=True)
        # Simulate fresh page-server forge cache timestamps (`now`), including a PR that merged after the engine stopped!
        self.sources = bs.Sources(
            fetched={"kanban": NOW, "merge requests": NOW, "workers": NOW},
            issues={
                KEY: bs.Issue("#12", "https://github.com/example/repo/issues/12",
                              "Upload size limit", "open", ("Needs input",)),
                "#12": bs.Issue("#12", "https://github.com/example/repo/issues/12",
                                "Upload size limit", "open", ("Needs input",)),
            },
            changes={
                "#21": bs.Change("#21", "https://github.com/example/repo/pull/21", "Upload size limit",
                                 "merged", False, "passed", True, 1, NOW - timedelta(minutes=15),
                                 "main", ("src/upload.py",), (KEY, "#12")),
            },
            workers=(),
            errors={},
        )
        bs._write(self.root / "pages" / bs.CACHE, self.sources)

    def _render_board_and_issue(self) -> tuple[str, str]:
        board_html = pg.write_board(self.root, now=NOW)[0].read_text(encoding="utf-8")
        data = task_forge.page_data(self.db)
        with mock.patch.object(issue_page, "datetime") as fake_dt:
            fake_dt.now.return_value = NOW
            issue_path = issue_page.write(self.root, self.root / "pages", KEY, db=data)
        assert issue_path is not None
        return board_html, issue_path.read_text(encoding="utf-8")

    def test_concurrent_shared_lock_probes_do_not_false_positive_each_other(self) -> None:
        lock_path = supervisor.state_dir(self.root) / ws.ENGINE_LOCK_FILE
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch()
        results: list[bool] = []
        barrier = threading.Barrier(4)

        def probe() -> None:
            barrier.wait(5)
            for _ in range(25):
                results.append(ws.lock_held(lock_path))

        threads = [threading.Thread(target=probe) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(5)
        self.assertEqual(len(results), 100)
        self.assertFalse(any(results), "concurrent read probes must never report an unlocked engine as running")
        with supervisor.lock(lock_path):
            self.assertTrue(ws.lock_held(lock_path))

    def test_running_engine_shows_last_pass_and_omits_fake_tracker_and_kanban_stamps(self) -> None:
        last_pass = NOW - timedelta(minutes=1)
        ws.write_engine_status(
            self.root,
            started_at=NOW - timedelta(hours=2),
            last_pass_at=last_pass,
            last_attempt_at=last_pass,
            stopped_at=None,
            interval=60.0,
            mode="service",
        )
        with supervisor.lock(supervisor.state_dir(self.root) / ws.ENGINE_LOCK_FILE):
            board_html, task_html = self._render_board_and_issue()

        self.assertIn("engine running \u00b7 last pass 13:59", board_html)
        self.assertNotIn('class="panels-warn"', board_html)
        self.assertNotIn("tracker 14:00", board_html)
        self.assertNotIn("kanban 14:00", board_html)
        self.assertNotIn("workers 14:00", board_html)
        self.assertIn("merge requests 14:00", board_html)
        self.assertIn("last pass 13:59", task_html)
        self.assertNotIn('role="alert"', task_html)

    def test_stopped_engine_after_crash_or_reboot_warns_despite_fresh_sources_refresh(self) -> None:
        # Exact #936 failure mode: engine crashed/rebooted yesterday at 02:27 (stopped_at=None in status file),
        # while pages.py kept refreshing .sources.json at 14:00 today and PR #21 merged 15 minutes ago.
        yesterday_pass = (NOW - timedelta(days=1)).replace(hour=2, minute=27)
        ws.write_engine_status(
            self.root,
            started_at=yesterday_pass - timedelta(hours=3),
            last_pass_at=yesterday_pass,
            last_attempt_at=yesterday_pass,
            stopped_at=None,
            interval=60.0,
            mode="service",
        )
        board_html, task_html = self._render_board_and_issue()

        self.assertIn("engine stopped \u00b7 last pass Thu 02:27", board_html)
        self.assertIn('class="panels-warn"', board_html)
        self.assertIn("engine not running (last pass Thu 02:27)", board_html)
        self.assertNotIn("tracker 14:00", board_html)
        # Task page shows merged PR #21 alongside store NEEDS INPUT, with explicit alert explaining engine is stopped:
        self.assertIn("MERGED 13:45", task_html)
        self.assertIn("NEEDS INPUT", task_html)
        self.assertIn('role="alert"', task_html)
        self.assertIn("engine not running (last pass Thu 02:27)", task_html)

    def test_cleanly_stopped_engine_names_stop_time_and_last_pass(self) -> None:
        last_pass = NOW - timedelta(minutes=12)
        stopped_at = NOW - timedelta(minutes=10)
        ws.write_engine_status(
            self.root,
            started_at=NOW - timedelta(hours=1),
            last_pass_at=last_pass,
            last_attempt_at=last_pass,
            stopped_at=stopped_at,
            interval=60.0,
            mode="service",
        )
        board_html, task_html = self._render_board_and_issue()
        self.assertIn("engine not running since 13:50 (last pass 13:48)", board_html)
        self.assertIn("engine not running since 13:50 (last pass 13:48)", task_html)

    def test_failed_pass_while_running_and_failed_startup_while_stopped_surface_errors(self) -> None:
        last_pass = NOW - timedelta(minutes=5)
        attempt = NOW - timedelta(minutes=1)
        ws.write_engine_status(
            self.root,
            started_at=NOW - timedelta(hours=1),
            last_pass_at=last_pass,
            last_attempt_at=attempt,
            stopped_at=None,
            interval=60.0,
            mode="service",
            last_error="GitHub GraphQL 502",
            error_kind="pass",
        )
        with supervisor.lock(supervisor.state_dir(self.root) / ws.ENGINE_LOCK_FILE):
            board_running_fail, task_running_fail = self._render_board_and_issue()

        self.assertIn("engine error \u00b7 last pass 13:55", board_running_fail)
        self.assertIn('class="panels-warn"', board_running_fail)
        self.assertIn("GitHub GraphQL 502", board_running_fail)
        self.assertIn("last pass 13:55", board_running_fail)
        self.assertIn("GitHub GraphQL 502", task_running_fail)

        # Startup/settings error where engine exited on startup:
        ws.write_engine_status(
            self.root,
            started_at=attempt,
            last_pass_at=last_pass,
            last_attempt_at=attempt,
            stopped_at=attempt,
            interval=60.0,
            mode="service",
            last_error="unknown settings section [models.deep]",
            error_kind="startup",
        )
        board_startup_fail, task_startup_fail = self._render_board_and_issue()
        self.assertIn("engine stopped \u00b7 last pass 13:55", board_startup_fail)
        self.assertIn("engine failed to start", board_startup_fail)
        self.assertIn("unknown settings section [models.deep]", board_startup_fail)
        self.assertIn("unknown settings section [models.deep]", task_startup_fail)

    def test_stale_running_engine_warns_when_no_pass_completed_within_window(self) -> None:
        stale_pass = NOW - timedelta(minutes=10)
        ws.write_engine_status(
            self.root,
            started_at=NOW - timedelta(hours=2),
            last_pass_at=stale_pass,
            last_attempt_at=stale_pass,
            stopped_at=None,
            interval=60.0,
            mode="service",
        )
        with supervisor.lock(supervisor.state_dir(self.root) / ws.ENGINE_LOCK_FILE):
            board_html, task_html = self._render_board_and_issue()

        self.assertIn("engine stale \u00b7 last pass 13:50", board_html)
        self.assertIn('class="panels-warn"', board_html)
        self.assertIn("engine stale: no completed pass since 13:50", board_html)
        self.assertIn("engine stale: no completed pass since 13:50", task_html)

    def test_overlong_in_progress_pass_warns_on_board_and_task_page(self) -> None:
        ws.write_engine_status(self.root, started_at=NOW - timedelta(days=2),
                               pass_started_at=NOW - timedelta(days=1, hours=1),
                               last_pass_at=NOW - timedelta(days=2), last_attempt_at=NOW - timedelta(days=2), stopped_at=None)
        with supervisor.lock(supervisor.state_dir(self.root) / ws.ENGINE_LOCK_FILE):
            board_html, task_html = self._render_board_and_issue()
        self.assertIn('pass in progress', board_html)
        self.assertIn('may be stuck', board_html)
        self.assertIn('may be stuck', task_html)
        self.assertNotIn('engine stale', board_html)

    def test_missing_and_corrupt_engine_status_warn_with_unknown_last_pass(self) -> None:
        # 1. Missing engine-status.json on a store-backed workspace:
        board_missing, task_missing = self._render_board_and_issue()
        self.assertIn("engine stopped \u00b7 last pass unknown", board_missing)
        self.assertIn('class="panels-warn"', board_missing)
        self.assertIn("engine not running (last pass unknown)", board_missing)
        self.assertIn("engine not running (last pass unknown)", task_missing)

        # 2. Corrupt engine-status.json:
        status_path = supervisor.state_dir(self.root) / ws.ENGINE_STATUS_FILE
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text("{corrupt json", encoding="utf-8")
        board_corrupt, task_corrupt = self._render_board_and_issue()
        self.assertIn("engine stopped \u00b7 last pass unknown", board_corrupt)
        self.assertIn("unreadable", board_corrupt)
        self.assertIn("unreadable", task_corrupt)

    def test_pages_server_rerenders_once_when_engine_lock_drops_without_file_writes(self) -> None:
        pg.RENDERED_AT.clear()
        now_live = datetime.now(ZONE).replace(second=0, microsecond=0)
        ws.write_engine_status(
            self.root,
            started_at=now_live - timedelta(minutes=5),
            last_pass_at=now_live,
            last_attempt_at=now_live,
            stopped_at=None,
            interval=60.0,
            mode="service",
        )
        server = pg.make_server(self.root / "pages", 0, root=self.root, refresh=lambda r, n: None, every=3600)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]

        def fetch(path: str = "/", method: str = "GET") -> tuple[int, str, str]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request(method, path)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            lm = resp.getheader("Last-Modified") or ""
            conn.close()
            return resp.status, lm, body

        lock_path = supervisor.state_dir(self.root) / ws.ENGINE_LOCK_FILE
        with supervisor.lock(lock_path):
            status, lm_live, body_live = fetch("/")
            self.assertEqual(status, 200)
            self.assertIn("engine running", body_live)
            self.assertNotIn('class="panels-warn"', body_live)

        # Lock dropped without writing any file (simulating SIGKILL / power loss).
        board_file = next((self.root / "pages").glob("*-board.html"))
        os.utime(board_file, (1_700_000_000, 1_700_000_000))
        # Adjust RENDERED_AT's recorded file mtime so we test pure digest invalidation on lock loss:
        key = str(board_file)
        if key in pg.RENDERED_AT:
            started_ns, _, old_digest = pg.RENDERED_AT[key]
            pg.RENDERED_AT[key] = (started_ns, board_file.stat().st_mtime_ns, old_digest)

        status, lm_stopped, body_stopped = fetch("/")
        self.assertEqual(status, 200)
        self.assertIn("engine not running", body_stopped)
        self.assertIn('class="panels-warn"', body_stopped)
        mtime_after_stop = board_file.stat().st_mtime_ns
        self.assertNotEqual(mtime_after_stop, 1_700_000_000 * 1_000_000_000)

        # Subsequent poll with no further change keeps the rendered file untouched:
        status2, lm_stopped_again, _ = fetch("/", method="HEAD")
        self.assertEqual((status2, lm_stopped_again, board_file.stat().st_mtime_ns),
                         (200, lm_stopped, mtime_after_stop))


if __name__ == "__main__":
    unittest.main()
