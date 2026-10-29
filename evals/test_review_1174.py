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
                self.assertEqual(child.call_args.args[0][-1], 'ensure-retry')


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
                self.assertIn('pass in progress', health.meta)
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

class FollowupRegression(SupervisorTest):
    def test_contending_workspaces_retry_and_opt_out_wins_while_waiting(self):
        import threading
        import time
        other = self.root.parent / 'other'
        other.mkdir()
        for root in (self.root, other):
            (root / 'store.db').touch()
        results = {}
        with supervisor.lock(self.data / 'supervisor.lock'):
            threads = [threading.Thread(target=lambda r=root: results.setdefault(r, supervisor.ensure(r, jobs_dir=self.jobs)))
                       for root in (self.root, other)]
            for thread in threads:
                thread.start()
            time.sleep(0.2)
            supervisor.atomic_write(supervisor.state_dir(other) / supervisor.DISABLED_MARKER, b'')
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results[self.root], 'engine: started')
        self.assertEqual(results[other], 'engine: disabled')
        self.assertTrue(supervisor.job_path(supervisor.label('engine', self.root), self.jobs).exists())
        self.assertFalse(supervisor.job_path(supervisor.label('engine', other), self.jobs).exists())

    def test_ensure_lock_wait_has_a_deadline(self):
        (self.root / 'store.db').touch()
        with supervisor.lock(self.data / 'supervisor.lock'), \
             mock.patch.object(supervisor, 'ENSURE_LOCK_TIMEOUT', 0.1):
            with self.assertRaisesRegex(supervisor.ServiceError, 'timed out'):
                supervisor.ensure(self.root, jobs_dir=self.jobs)

    def test_plugin_upgrade_is_requested_without_stopping_a_pass_and_cannot_downgrade(self):
        base = self.root.parent / 'plugins/cache/market/chief-of-stuff'
        old, new = base / '1.0.0', base / '1.0.1'
        for release, version in ((old, '1.0.0'), (new, '1.0.1')):
            (release / '.claude-plugin').mkdir(parents=True)
            (release / '.claude-plugin/plugin.json').write_text(json.dumps({'version': version}))
            (release / 'requirements.txt').write_text('SQLAlchemy==2.0.54\n')
            (release / 'scripts').mkdir()
            (release / 'scripts/engine.py').touch()
            (release / 'chief_of_stuff.py').touch()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
            self.launchctl_mock.reset_mock()
            with mock.patch.object(supervisor, 'RELEASE', new):
                self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: upgrade pending')
            self.assertNotIn('bootout', self.verbs())
            with mock.patch.object(supervisor, 'RELEASE', old):
                self.assertEqual(supervisor.selected_release(self.root), new)
                supervisor.ensure(self.root, jobs_dir=self.jobs)
                self.assertEqual(supervisor.selected_release(self.root), new)
        (new / 'scripts/engine.py').unlink()
        with mock.patch.object(supervisor, 'RELEASE', old):
            self.assertEqual(supervisor.selected_release(self.root), old)
            (supervisor.state_dir(self.root) / supervisor.PLUGIN_RELEASE_MARKER).write_text('\0')
            self.assertEqual(supervisor.selected_release(self.root), old)

    def test_requested_plugin_handoff_occurs_only_after_the_pass(self):
        import threading
        import engine
        import store
        from test_engine_health import CLAUDE_MD, COS_TOML, _FakeForge
        base = self.root.parent / 'plugins/cache/market/chief-of-stuff'
        old, new = base / '1.0.0', base / '1.0.1'
        for release, version in ((old, '1.0.0'), (new, '1.0.1')):
            (release / '.claude-plugin').mkdir(parents=True)
            (release / '.claude-plugin/plugin.json').write_text(json.dumps({'version': version}))
            (release / 'requirements.txt').write_text('SQLAlchemy==2.0.54\n')
            (release / 'scripts').mkdir()
            (release / 'scripts/engine.py').touch()
        (self.root / 'CLAUDE.md').write_text(CLAUDE_MD)
        (self.root / 'cos.toml').write_text(COS_TOML)
        store.migrate(self.root / 'store.db')
        entered, finish = threading.Event(), threading.Event()
        def run_pass(**kwargs):
            entered.set()
            if not finish.wait(10):
                raise RuntimeError('pass timed out')
        with mock.patch.object(supervisor, 'RELEASE', old), \
             mock.patch.object(engine.forge, 'for_workspace', return_value=_FakeForge()), \
             mock.patch.object(engine, 'run_pass', side_effect=run_pass), \
             mock.patch('pages.supervise'), \
             mock.patch.object(engine.time, 'sleep'), \
             mock.patch.object(engine, '_handoff') as handoff:
            supervisor.ensure(self.root, jobs_dir=self.jobs)
            thread = threading.Thread(target=lambda: engine.main(['--root', str(self.root)]))
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                # Simulate a separate new-plugin hook while the old engine is blocked.
                with mock.patch.object(supervisor, 'RELEASE', new):
                    self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: upgrade pending')
                handoff.assert_not_called()
            finally:
                finish.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(handoff.call_args.args[0], new)
            self.assertFalse(workspace.lock_held(supervisor.state_dir(self.root) / 'engine.lock'))

    def test_explicit_retry_for_same_release_waits_until_pass_finishes(self):
        import threading
        import engine
        import store
        from test_engine_health import CLAUDE_MD, COS_TOML, _FakeForge
        (self.root / 'CLAUDE.md').write_text(CLAUDE_MD)
        (self.root / 'cos.toml').write_text(COS_TOML)
        store.migrate(self.root / 'store.db')
        entered, finish = threading.Event(), threading.Event()
        def run_pass(**kwargs):
            entered.set()
            if not finish.wait(10):
                raise RuntimeError('pass timed out')
        with mock.patch.object(engine.forge, 'for_workspace', return_value=_FakeForge()), \
             mock.patch.object(engine, 'run_pass', side_effect=run_pass), \
             mock.patch('pages.supervise'), mock.patch.object(engine.time, 'sleep'), \
             mock.patch.object(engine, '_prepare_handoff', return_value=Path(sys.executable)), \
             mock.patch.object(engine, '_persist_launchd_handoff', return_value=True) as handoff:
            supervisor.ensure(self.root, jobs_dir=self.jobs)
            thread = threading.Thread(target=lambda: engine.main(['--root', str(self.root)]))
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                with mock.patch.object(supervisor, 'load_agent', side_effect=SystemExit):
                    with self.assertRaises(SystemExit):
                        supervisor.install(self.root, jobs_dir=self.jobs)
                self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: upgrade pending')
                self.assertTrue(supervisor.status(self.root, self.jobs)['upgrade_pending'])
                self.assertNotIn('bootout', self.verbs())
                handoff.assert_not_called()
            finally:
                finish.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            handoff.assert_called_once_with(self.root, supervisor.RELEASE)

    def test_both_workspaces_install_after_lock_contention(self):
        import threading
        other = self.root.parent / 'other'
        other.mkdir()
        for root in (self.root, other):
            (root / 'store.db').touch()
        results = []
        with supervisor.lock(self.data / 'supervisor.lock'):
            threads = [threading.Thread(target=lambda r=root: results.append(supervisor.ensure(r, jobs_dir=self.jobs)))
                       for root in (self.root, other)]
            for thread in threads:
                thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        # The launchctl mock tracks one job globally; both independent plist files must exist.
        self.assertEqual(len(results), 2)
        for root in (self.root, other):
            self.assertTrue(supervisor.job_path(supervisor.label('engine', root), self.jobs).exists())

    def test_engine_install_preserves_existing_board_fallback(self):
        (self.root / 'store.db').touch()
        path = supervisor.job_path(supervisor.label('pages', self.root), self.jobs)
        path.parent.mkdir(parents=True)
        path.write_bytes(__import__('plistlib').dumps({'Label': supervisor.label('pages', self.root),
                                                       'WorkingDirectory': str(self.root)}))
        supervisor.ensure(self.root, jobs_dir=self.jobs)
        self.assertTrue(path.exists())

    def test_very_long_pass_warns_without_claiming_failure(self):
        now = datetime.now(timezone.utc)
        status = workspace.EngineStatus(running=True, present=True, started_at=now-timedelta(days=2),
                                        pass_started_at=now-timedelta(days=1, hours=1))
        health = workspace.engine_health(status, now, timezone.utc)
        self.assertIn('may be stuck', health.warning)
        self.assertNotEqual(health.state, 'failed')
