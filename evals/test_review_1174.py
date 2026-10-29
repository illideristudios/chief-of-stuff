"""Regressions for coordinator startup, pass health, and safe automatic handoff."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts'), str(ROOT / 'evals')]
import workspace
import supervisor
import start_coordinator
from test_supervisor import SupervisorTest

real_run_preparation = supervisor.run_preparation


class StartupRegression(unittest.TestCase):
    def test_direct_claude_plugin_session_has_deterministic_engine_startup(self):
        hooks = json.loads((ROOT / 'hooks/hooks.json').read_text())['hooks']
        commands = [h['command'] for entry in hooks['SessionStart'] for h in entry['hooks']]
        self.assertTrue(any('supervisor.py' in c and 'ensure-background' in c for c in commands))

    def test_background_ensure_is_detached_and_skips_unconfigured_workspaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(supervisor.subprocess, 'Popen') as child:
                self.assertEqual(supervisor.ensure_background(root), 'engine: not configured')
                child.assert_not_called()
                (root / 'store.db').touch()
                self.assertEqual(supervisor.ensure_background(root), 'engine: ensuring')
                self.assertTrue(child.call_args.kwargs['start_new_session'])
                self.assertEqual(child.call_args.args[0][-1], 'ensure')


class HealthRegression(unittest.TestCase):
    def test_long_in_progress_pass_is_not_stale_but_idle_engine_is(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with supervisor.lock(supervisor.state_dir(root) / 'engine.lock'):
                workspace.write_engine_status(root, started_at=now-timedelta(minutes=20),
                    last_pass_at=now-timedelta(minutes=10), last_attempt_at=now-timedelta(minutes=10),
                    stopped_at=None, pass_started_at=now-timedelta(minutes=8))
                health = workspace.engine_health(workspace.read_engine_status(root), now, timezone.utc)
                self.assertNotEqual(health.state, 'stale')
                self.assertIsNone(health.warning)
                workspace.write_engine_status(root, started_at=now-timedelta(minutes=20),
                    last_pass_at=now-timedelta(minutes=10), last_attempt_at=now-timedelta(minutes=10),
                    stopped_at=None)
                self.assertEqual(workspace.engine_health(workspace.read_engine_status(root), now, timezone.utc).state, 'stale')


class HandoffRegression(SupervisorTest):
    def test_auto_ensure_does_not_boot_out_engine_during_a_pass(self):
        (self.root / 'store.db').touch()
        self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: started')
        before = self.job()
        self.publish('2.0.0', 'SQLAlchemy==2.0.55\n')
        self.launchctl_mock.reset_mock()
        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
            self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: serving')
            self.assertEqual(self.job(), before)
            self.assertNotIn('bootout', self.verbs())


class SlowInstallRegression(SupervisorTest):
    def test_first_install_can_complete_after_sixty_seconds(self):
        elapsed = [0]
        def slow(argv, **kwargs):
            elapsed[0] += 40
            self.assertGreater(kwargs['timeout'], 60)
            return self.fake_run(argv, **kwargs)
        with mock.patch.object(supervisor.time, 'monotonic', side_effect=lambda: elapsed[0]), \
             mock.patch.object(supervisor, 'run_preparation', side_effect=slow):
            python = supervisor.ensure_venv(self.data)
        self.assertEqual(elapsed[0], 80)
        self.assertTrue(python.is_file())

    def test_preparation_timeout_kills_and_reaps_the_process_group(self):
        import subprocess
        child = mock.MagicMock(pid=12345)
        child.__enter__.return_value = child
        child.communicate.side_effect = [subprocess.TimeoutExpired('pip', 1), ('', '')]
        with mock.patch.object(supervisor.subprocess, 'Popen', return_value=child), \
             mock.patch.object(supervisor.os, 'killpg') as kill, \
             self.assertRaises(subprocess.TimeoutExpired):
            real_run_preparation(['pip'], timeout=1)
        kill.assert_called_once_with(12345, supervisor.signal.SIGKILL)
        self.assertEqual(child.communicate.call_count, 2)


class BlockedPassRegression(SupervisorTest):
    def test_release_change_while_pass_is_blocked_does_not_interrupt_it(self):
        import threading
        import engine
        import store
        from test_engine_health import CLAUDE_MD, COS_TOML, _FakeForge
        (self.root / 'CLAUDE.md').write_text(CLAUDE_MD)
        (self.root / 'cos.toml').write_text(COS_TOML)
        store.migrate(self.root / 'store.db')
        self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: started')
        entered, finish = threading.Event(), threading.Event()
        results = []
        def blocked(**kwargs):
            entered.set()
            if not finish.wait(10):
                raise RuntimeError('test timed out')
        def stop_after_pass(_):
            raise KeyboardInterrupt
        with mock.patch.object(engine.forge, 'for_workspace', return_value=_FakeForge()), \
             mock.patch.object(engine, 'run_pass', side_effect=blocked), \
             mock.patch('pages.supervise'), \
             mock.patch.object(engine.time, 'sleep', side_effect=stop_after_pass):
            thread = threading.Thread(target=lambda: results.append(engine.main(['--root', str(self.root)])))
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                status = workspace.read_engine_status(self.root)
                self.assertIsNotNone(status.pass_started_at)
                self.publish('2.0.0', 'SQLAlchemy==2.0.55\n')
                self.launchctl_mock.reset_mock()
                self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: serving')
                self.assertNotIn('bootout', self.verbs())
                self.assertTrue(thread.is_alive())
            finally:
                finish.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [0])
            self.assertIsNone(workspace.read_engine_status(self.root).pass_started_at)
