"""Regression coverage for release handoff and independent board recovery."""
import json
import subprocess
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from test_supervisor import SupervisorTest
import supervisor
import workspace
import pages


class ReleaseRegressions(SupervisorTest):
    def releases(self):
        base = self.root.parent / 'plugins/cache/market/chief-of-stuff'
        old, new = base / '1.0.0', base / '1.0.1'
        for release, version in ((old, '1.0.0'), (new, '1.0.1')):
            (release / '.claude-plugin').mkdir(parents=True)
            (release / '.claude-plugin/plugin.json').write_text(json.dumps({'version': version}))
            (release / 'requirements.txt').write_text('SQLAlchemy==2.0.54\n')
            (release / 'scripts').mkdir()
            (release / 'scripts/engine.py').touch()
        return old, new

    def test_boundary_replaces_loaded_job_for_restart(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
            with mock.patch.object(supervisor, 'RELEASE', new):
                supervisor.ensure(self.root, jobs_dir=self.jobs)
            self.assertEqual(self.job()['ProgramArguments'][1], str(old / 'scripts/engine.py'))
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.commit_handoff(self.root, new, self.jobs)
        self.assertEqual(self.job()['ProgramArguments'][1], str(new / 'scripts/engine.py'))
        self.assertIn('bootout', self.verbs())
        self.assertTrue(self.loaded)

    def test_explicit_downgrade_clears_prior_upgrade_request(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
            with mock.patch.object(supervisor, 'RELEASE', new):
                supervisor.ensure(self.root, jobs_dir=self.jobs)
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.install(self.root, jobs_dir=self.jobs)
            self.assertEqual(supervisor.selected_release(self.root), old)

    def test_failed_bootout_restores_old_registration(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
            with mock.patch.object(supervisor, 'RELEASE', new):
                supervisor.ensure(self.root, jobs_dir=self.jobs)
        before = self.job()
        def control(*args, **kwargs):
            if args[0] == 'bootout':
                raise supervisor.ServiceError('bootout failed')
            return self.launchctl(*args, **kwargs)
        with mock.patch.object(supervisor, 'RELEASE', old), mock.patch.object(supervisor, 'launchctl', side_effect=control):
            with self.assertRaisesRegex(supervisor.ServiceError, 'bootout failed'):
                supervisor.commit_handoff(self.root, new, self.jobs)
        self.assertEqual(self.job(), before)

    def test_helper_reloads_only_while_engine_lock_is_held(self):
        import engine
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        job_path = supervisor.job_path(supervisor.label('engine', self.root), self.jobs)
        def run(argv, **kwargs):
            self.assertTrue(workspace.lock_held(supervisor.state_dir(self.root) / 'engine.lock'))
            self.assertTrue(kwargs['start_new_session'])
            self.assertEqual(argv[-1], 'commit-handoff')
            return subprocess.CompletedProcess(argv, 0)
        with mock.patch.object(supervisor, 'RELEASE', old), \
             mock.patch.object(supervisor, 'job_path', return_value=job_path), \
             mock.patch.object(engine.subprocess, 'run', side_effect=run), \
             supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
            self.assertTrue(engine._persist_launchd_handoff(self.root, new))

    def test_unrelated_release_reports_unaccepted_handoff(self):
        (self.root / 'store.db').touch()
        supervisor.ensure(self.root, jobs_dir=self.jobs)
        other = self.root.parent / 'other-release'
        (other / '.claude-plugin').mkdir(parents=True)
        (other / '.claude-plugin/plugin.json').write_text('{"version":"2.0.0"}')
        (other / 'requirements.txt').write_text('pins')
        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'), mock.patch.object(supervisor, 'RELEASE', other):
            with self.assertRaisesRegex(supervisor.ServiceError, 'cannot.*handoff'):
                supervisor.ensure(self.root, jobs_dir=self.jobs)

    def test_unrelated_installed_pointer_cannot_claim_a_handoff(self):
        (self.root / 'store.db').touch()
        supervisor.ensure(self.root, jobs_dir=self.jobs)
        other = self.root.parent / 'install/versions/new'
        (other / '.claude-plugin').mkdir(parents=True)
        (other / '.claude-plugin/plugin.json').write_text('{"version":"2.0.0"}')
        (other / 'requirements.txt').write_text('pins')
        (other / 'chief_of_stuff.py').touch()
        (other.parent.parent / 'current').symlink_to(other)
        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'), mock.patch.object(supervisor, 'RELEASE', other):
            with self.assertRaisesRegex(supervisor.ServiceError, 'cannot.*handoff'):
                supervisor.ensure(self.root, jobs_dir=self.jobs)

    def test_background_retry_survives_lock_timeout(self):
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'ensure', side_effect=[supervisor.LockTimeout('timeout'), 'engine: started']) as ensure, \
             mock.patch.object(supervisor.time, 'sleep'):
            self.assertEqual(supervisor.ensure_retry(self.root), 'engine: started')
        self.assertEqual(ensure.call_count, 2)

    def test_error_and_long_pass_are_both_visible(self):
        now = datetime.now(timezone.utc)
        status = workspace.EngineStatus(running=True, present=True, last_error='previous failure', error_kind='pass',
                                         pass_started_at=now-timedelta(days=2))
        health = workspace.engine_health(status, now, timezone.utc)
        self.assertIn('previous failure', health.warning)
        self.assertIn('may be stuck', health.warning)

    def test_board_retry_continues_while_pass_is_running(self):
        stop = threading.Event()
        def attempt(root, event):
            if attempt.calls:
                event.set()
            attempt.calls += 1
        attempt.calls = 0
        with mock.patch.object(pages, 'supervise', side_effect=attempt), mock.patch.object(pages, 'POLL', 0):
            pages.supervise_with_retry(self.root, stop)
        self.assertEqual(attempt.calls, 2)
