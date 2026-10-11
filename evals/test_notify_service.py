"""Notification service scheduling and lifecycle, with controlled clocks and launchd."""
import contextlib
import fcntl
import io
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import notify
import notify_service as service
import supervisor
from evals.test_notify import Workspace, NOW, TOML, tracker


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.tmp.cleanup)
        self.root = self.ws.root
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)


class ServiceTest(WorkspaceTest):

    def test_startup_changes_fallback_and_restart(self):
        sync = mock.Mock(wraps=notify.sync_workspace)
        worker = service.Service(self.root, reconcile=sync)
        worker.tick(NOW, 0)
        original = self.ws.queue.read_bytes()
        worker.tick(NOW + timedelta(seconds=5), 5)
        self.assertEqual(sync.call_count, 1)
        worker.tick(NOW + timedelta(minutes=1), 60)
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(self.ws.queue.read_bytes(), original)
        path = self.root / "CLAUDE.md"
        path.write_text(path.read_text().replace("23:59", "22:59"))
        worker.tick(NOW + timedelta(seconds=65), 65)
        self.assertEqual(sync.call_count, 3)
        self.assertIn("22:59", self.ws.queue.read_text())
        service.Service(self.root, reconcile=sync).tick(NOW, 70)
        self.assertEqual(sync.call_count, 4)

    def test_historical_tracker_creation_removal_and_settings_path_change(self):
        worker = service.Service(self.root)
        worker.tick(NOW, 0)
        daily = self.root / "daily"
        daily.mkdir()
        past = daily / "2026-09-22-tracker.md"
        past.write_text(tracker(("13:00", "deadline: Extra 2026-09-24 18:00")))
        worker.tick(NOW, 5)
        self.assertIn("Extra", self.ws.queue.read_text())
        past.unlink()
        worker.tick(NOW, 10)
        self.assertNotIn("Extra", self.ws.queue.read_text())
        (self.root / "other.toml").write_text(TOML + 'day_open = "08:00"\n')
        cfg = self.root / "CLAUDE.md"
        cfg.write_text(cfg.read_text().replace("chief-of-stuff.toml", "other.toml"))
        worker.tick(NOW, 15)
        self.assertIn("daily 08:00", self.ws.queue.read_text())

    def test_failure_preserves_queue_retries_and_logs_only_changes(self):
        worker = service.Service(self.root)
        worker.tick(NOW, 0)
        before = self.ws.queue.read_bytes()
        settings = self.root / "chief-of-stuff.toml"
        settings.write_text('[notify]\nadapter = "invalid"\n')
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            worker.tick(NOW, 5)
            worker.tick(NOW, 10)
        self.assertEqual(self.ws.queue.read_bytes(), before)
        self.assertEqual(output.getvalue().count("notify service:"), 1)
        self.assertTrue(service.status(self.root)["error"])
        settings.write_text(TOML)
        worker.tick(NOW, 15)
        self.assertIsNone(service.status(self.root)["error"])

    def test_off_never_creates_queue_and_can_be_reenabled(self):
        (self.root / "chief-of-stuff.toml").write_text('[notify]\nadapter = "off"\n')
        worker = service.Service(self.root)
        worker.tick(NOW, 0)
        self.assertFalse(self.ws.queue.exists())
        (self.root / "chief-of-stuff.toml").write_text(TOML)
        worker.tick(NOW, 5)
        self.assertTrue(self.ws.queue.exists())

    def test_day_rollover_uses_workspace_date_and_clock_jump_syncs(self):
        late = NOW.replace(hour=23, minute=59)
        worker = service.Service(self.root)
        worker.tick(late, 0)
        (self.root / "daily").mkdir()
        (self.root / "daily/2026-09-24-tracker.md").write_text(
            tracker(("00:00", "deadline: Tomorrow 18:00")))
        worker.tick(late + timedelta(minutes=1), 5)
        self.assertIn("Tomorrow", self.ws.queue.read_text())
        sync = mock.Mock(wraps=notify.sync_workspace)
        worker = service.Service(self.root, reconcile=sync)
        worker.tick(NOW, 0)
        worker.tick(NOW + timedelta(hours=8), 5)
        self.assertEqual(sync.call_count, 2)

    def test_duplicate_service_is_refused_and_status_uses_lock(self):
        with service.instance_lock(self.root):
            self.assertTrue(service.status(self.root)["running"])
            with self.assertRaisesRegex(service.ServiceError, "already running"):
                with service.instance_lock(self.root):
                    pass
        self.assertFalse(service.status(self.root)["running"])

    def test_dst_fold_uses_workspace_wall_time_without_duplicate_rows(self):
        from zoneinfo import ZoneInfo
        cfg = self.root / "CLAUDE.md"
        cfg.write_text(cfg.read_text().replace("2026-09-23 23:59", "2026-11-01 05:00"))
        first = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
        second = datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc)
        self.assertEqual(first.astimezone(ZoneInfo("America/Chicago")).hour, 1)
        self.assertEqual(second.astimezone(ZoneInfo("America/Chicago")).hour, 1)
        worker = service.Service(self.root)
        worker.tick(first, 0)
        before = self.ws.queue.read_text()
        worker.tick(second, 3600)
        self.assertEqual(self.ws.queue.read_text(), before)

    def test_unchanged_sync_does_not_replace_queue_or_change_mode(self):
        worker = service.Service(self.root)
        worker.tick(NOW, 0)
        self.ws.queue.chmod(0o640)
        before = self.ws.queue.stat()
        worker.tick(NOW + timedelta(minutes=1), 60)
        after = self.ws.queue.stat()
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(after.st_mode & 0o777, 0o640)


class LifecycleTest(WorkspaceTest):
    def test_add_with_service_command_in_what_writes_notification(self):
        code = service.main(["--root", str(self.root), "add", "--kind", "awaiting",
                             "--what", "status"])
        self.assertEqual(code, 0)
        self.assertIn("🙋 Awaiting you — status (", self.ws.queue.read_text())
        self.assertIn(") | status |", self.ws.queue.read_text())

    def test_add_with_service_command_in_message_writes_notification(self):
        code = service.main(["--root", str(self.root), "add", "--kind", "awaiting",
                             "--what", "Review", "--message", "stop"])
        self.assertEqual(code, 0)
        self.assertIn("🙋 Awaiting you — Review (", self.ws.queue.read_text())
        self.assertIn(") | stop |", self.ws.queue.read_text())

    def test_relative_root_named_serve_routes_add_and_sync_to_notify(self):
        root = self.root / "serve"
        root.mkdir()
        for name in ("CLAUDE.md", "chief-of-stuff.toml"):
            (root / name).write_text((self.root / name).read_text())
        with contextlib.chdir(self.root):
            for root_args in (["--root", "serve"], ["--root=serve"]):
                for command in (["add", "--kind", "awaiting", "--what", "Review"], ["sync"]):
                    with self.subTest(root_args=root_args, command=command), \
                         mock.patch.object(notify, "main", wraps=notify.main) as dispatch:
                        argv = [*root_args, *command]
                        self.assertEqual(service.main(argv), 0)
                        dispatch.assert_called_once_with(argv)
                        self.assertTrue((root / "notes/NOTIFICATIONS.md").exists())

    def test_service_subcommands_reach_service(self):
        with contextlib.chdir(self.root):
            for root_args in ([], ["--root", str(self.root)], [f"--root={self.root}"]):
                for command in ("serve", "ensure", "status", "stop"):
                    with self.subTest(root_args=root_args, command=command), \
                         mock.patch.object(service, command) as handler, \
                         mock.patch.object(notify, "main") as dispatch:
                        handler.return_value = {} if command == "status" else "notify: serving"
                        self.assertEqual(service.main([*root_args, command]), 0)
                        handler.assert_called_once_with(self.root.resolve())
                        dispatch.assert_not_called()

    def test_invalid_workspace_cli_reports_error_without_traceback(self):
        (self.root / "CLAUDE.md").write_text("# Missing coordinator\n")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = service.main(["--root", str(self.root), "ensure"])
        self.assertEqual(code, 2)
        self.assertIn("no `## Coordinator`", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_ensure_reuses_job_and_replaces_release_then_stop_removes_it(self):
        jobs = self.root / "agents"
        with mock.patch.object(service.sys, "platform", "darwin"), \
             mock.patch.object(supervisor, "launchctl", side_effect=[False, True, True, True,
                                                                  True, True, True, True, True,
                                                                  True, True]) as control:
            self.assertEqual(service.ensure(self.root, jobs_dir=jobs), "notify: started")
            self.assertEqual(service.ensure(self.root, jobs_dir=jobs), "notify: serving")
            self.assertEqual(control.call_count, 4)  # print/enable/bootstrap, then print
            plist = next(jobs.glob("*.plist"))
            import plistlib
            job = plistlib.loads(plist.read_bytes())
            self.assertEqual(job["ProgramArguments"][-1], "serve")
            self.assertEqual(job["WorkingDirectory"], str(self.root.resolve()))
            self.assertTrue(Path(job["ProgramArguments"][0]).is_absolute())
            job["ProgramArguments"][1] = "/old/release/scripts/notify.py"
            plist.write_bytes(plistlib.dumps(job))
            self.assertEqual(service.ensure(self.root, jobs_dir=jobs), "notify: restarted")
            service.stop(self.root, jobs_dir=jobs)
            self.assertFalse(plist.exists())

    def test_off_ensure_has_no_service_side_effects(self):
        (self.root / "chief-of-stuff.toml").write_text('[notify]\nadapter = "off"\n')
        with mock.patch.object(supervisor, "launchctl") as control:
            self.assertEqual(service.ensure(self.root), "notify: off")
        control.assert_not_called()
        self.assertFalse((self.root / ".chief-of-stuff").exists())


class QueueLockTest(unittest.TestCase):
    def test_simultaneous_add_and_sync_do_not_lose_or_duplicate_rows(self):
        ws = Workspace()
        self.addCleanup(ws.tmp.cleanup)
        def edit(index):
            if index % 2:
                notify.sync_workspace(ws.root, NOW)
            else:
                notify.MdNotifyQueue(ws.queue).add(notify.Row("2026-09-23 14:00", "One event"))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(edit, range(12)))
        text = ws.queue.read_text()
        self.assertEqual(text.count("| One event |"), 1)
        self.assertEqual(text.count("⏰ Early in 3h"), 1)

    def test_cooperating_writer_is_read_after_lock_and_acknowledgment_survives(self):
        ws = Workspace()
        self.addCleanup(ws.tmp.cleanup)
        notify.sync_workspace(ws.root, NOW)
        queue = notify.MdNotifyQueue(ws.queue)
        with queue.lock():
            original = ws.queue.read_text()
            first = next(line for line in original.splitlines() if "⏰" in line)
            ws.queue.write_text(original.replace(first + "\n", "") + "\n## Completed\n" + first + "\n")
        notify.sync_workspace(ws.root, NOW)
        self.assertEqual(ws.queue.read_text().count(first), 1)
        self.assertIn("## Completed", ws.queue.read_text())

    def test_lock_contention_times_out_without_touching_queue(self):
        ws = Workspace()
        self.addCleanup(ws.tmp.cleanup)
        notify.sync_workspace(ws.root, NOW)
        queue = notify.MdNotifyQueue(ws.queue)
        before = ws.queue.read_bytes()
        with queue.lock():
            with self.assertRaises(TimeoutError):
                with queue.lock(timeout=0):
                    pass
        self.assertEqual(before, ws.queue.read_bytes())


@unittest.skipUnless(sys.platform == "darwin", "launchd exists only on macOS")
class LaunchdSmokeTest(WorkspaceTest):
    def test_crash_recovery_and_updates_with_no_coordinator(self):
        # Registers only a temporary queue and job; never opens md-notify or a browser.
        due = datetime.now(timezone.utc) + timedelta(days=2)
        from zoneinfo import ZoneInfo
        due = due.astimezone(ZoneInfo("America/Chicago"))
        cfg = self.root / "CLAUDE.md"
        cfg.write_text(cfg.read_text().replace("2026-09-23 23:59", due.strftime("%Y-%m-%d %H:%M")))
        jobs = self.root / "agents"
        def wait_for(predicate, timeout=25):
            until = time.monotonic() + timeout
            while time.monotonic() < until:
                if predicate():
                    return
                time.sleep(0.1)
            self.fail(f"service did not converge: {service.status(self.root)}")
        try:
            service.ensure(self.root, jobs_dir=jobs)
            wait_for(lambda: service.status(self.root)["running"] and self.ws.queue.exists())
            pid = service.status(self.root)["pid"]
            self.assertEqual(service.ensure(self.root, jobs_dir=jobs), "notify: serving")
            changed = due + timedelta(hours=1)
            cfg.write_text(cfg.read_text().replace(due.strftime("%Y-%m-%d %H:%M"),
                                                   changed.strftime("%Y-%m-%d %H:%M")))
            wait_for(lambda: f"Due {changed:%Y-%m-%d %H:%M}" in self.ws.queue.read_text())
            os.kill(pid, signal.SIGKILL)
            wait_for(lambda: service.status(self.root)["running"] and service.status(self.root)["pid"] != pid)
            (self.root / "chief-of-stuff.toml").write_text('[notify]\nadapter = "off"\n')
            wait_for(lambda: service.status(self.root).get("disabled"))
        finally:
            service.stop(self.root, jobs_dir=jobs)
            wait_for(lambda: not service.status(self.root)["running"])
        self.assertFalse(list(jobs.glob("*.plist")))
