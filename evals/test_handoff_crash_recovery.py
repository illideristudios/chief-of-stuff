"""Crash, signal and concurrent request regressions for engine supervision."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

from test_supervisor import SupervisorTest
import test_review_1174_followup
import supervisor


class HandoffCrashRecovery(SupervisorTest):
    releases = test_review_1174_followup.ReleaseRegressions.releases

    def test_interrupted_plist_write_is_replayed_even_if_job_is_loaded(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
            with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
                with mock.patch.object(supervisor, 'RELEASE', new):
                    supervisor.ensure(self.root, jobs_dir=self.jobs)
            original = supervisor.atomic_write
            def crash_after_plist(path, data, *args, **kwargs):
                original(path, data, *args, **kwargs)
                if path.suffix == '.plist':
                    raise SystemExit('power loss')
            with mock.patch.object(supervisor, 'atomic_write', side_effect=crash_after_plist):
                with self.assertRaises(SystemExit):
                    supervisor.commit_handoff(self.root, new, self.jobs)
            self.assertTrue(self.loaded)
            self.launchctl_mock.reset_mock()
            supervisor.commit_handoff(self.root, new, self.jobs)
            self.assertIn('bootout', self.verbs())
            self.assertTrue(self.loaded)
            self.assertEqual(supervisor.current_release_from_job(self.job()), new)

    def test_crashes_after_bootout_or_bootstrap_are_replayed(self):
        for verb in ('bootout', 'bootstrap'):
            with self.subTest(verb=verb):
                self.doCleanups(); self.setUp()
                old, new = self.releases()
                (self.root / 'store.db').touch()
                marker = supervisor.state_dir(self.root) / supervisor.PLUGIN_RELEASE_MARKER
                with mock.patch.object(supervisor, 'RELEASE', old):
                    supervisor.ensure(self.root, jobs_dir=self.jobs)
                    marker.write_text(str(new))
                    def crash(*args, **kwargs):
                        result = self.launchctl(*args, **kwargs)
                        if args[0] == verb:
                            raise SystemExit('power loss')
                        return result
                    with mock.patch.object(supervisor, 'launchctl', side_effect=crash):
                        with self.assertRaises(SystemExit):
                            supervisor.commit_handoff(self.root, new, self.jobs)
                    supervisor.commit_handoff(self.root, new, self.jobs)
                self.assertTrue(self.loaded)
                self.assertEqual(supervisor.current_release_from_job(self.job()), new)
                self.assertFalse((supervisor.state_dir(self.root) / supervisor.HANDOFF_MARKER).exists())

    def test_new_release_replays_crash_after_bootstrap(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
            (supervisor.state_dir(self.root) / supervisor.PLUGIN_RELEASE_MARKER).write_text(str(new))
            def crash(*args, **kwargs):
                result = self.launchctl(*args, **kwargs)
                if args[0] == 'bootstrap':
                    raise SystemExit('power loss')
                return result
            with mock.patch.object(supervisor, 'launchctl', side_effect=crash):
                with self.assertRaises(SystemExit):
                    supervisor.commit_handoff(self.root, new, self.jobs)
        with mock.patch.object(supervisor, 'RELEASE', new):
            supervisor.commit_handoff(self.root, new, self.jobs)
        self.assertTrue(self.loaded)
        self.assertFalse((supervisor.state_dir(self.root) / supervisor.HANDOFF_MARKER).exists())

    def test_explicit_downgrade_clears_request_before_load(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
            with mock.patch.object(supervisor, 'RELEASE', new):
                supervisor.ensure(self.root, jobs_dir=self.jobs)
        original = supervisor.load_agent
        def crash_after_load(*args):
            original(*args)
            raise SystemExit('power loss')
        with mock.patch.object(supervisor, 'RELEASE', old), mock.patch.object(supervisor, 'load_agent', side_effect=crash_after_load):
            with self.assertRaises(SystemExit):
                supervisor.install(self.root, jobs_dir=self.jobs)
            self.assertEqual(supervisor.selected_release(self.root), old)

    def test_explicit_downgrade_recovers_at_each_replacement_boundary(self):
        for stage in ('before_bootout', 'after_bootout', 'after_plist', 'after_bootstrap'):
            with self.subTest(stage=stage):
                self.doCleanups()
                self.setUp()
                old, new = self.releases()
                (self.root / 'store.db').touch()
                with mock.patch.object(supervisor, 'RELEASE', new):
                    supervisor.ensure(self.root, jobs_dir=self.jobs)
                marker = supervisor.state_dir(self.root) / supervisor.PLUGIN_RELEASE_MARKER
                marker.write_text(str(new))
                control = supervisor.launchctl
                write = supervisor.atomic_write
                loaded_job = [self.job()]
                def track(*args, **kwargs):
                    result = self.launchctl(*args, **kwargs)
                    if args[0] == 'bootout':
                        loaded_job[0] = None
                    elif args[0] == 'bootstrap':
                        loaded_job[0] = self.job()
                    return result
                self.launchctl_mock.side_effect = track
                def crash_control(*args, **kwargs):
                    if stage == 'before_bootout' and args[0] == 'bootout':
                        raise SystemExit('power loss')
                    result = control(*args, **kwargs)
                    if ((stage == 'after_bootout' and args[0] == 'bootout') or
                            (stage == 'after_bootstrap' and args[0] == 'bootstrap')):
                        raise SystemExit('power loss')
                    return result
                def crash_write(path, data, *args, **kwargs):
                    result = write(path, data, *args, **kwargs)
                    if stage == 'after_plist' and path.suffix == '.plist':
                        raise SystemExit('power loss')
                    return result
                with mock.patch.object(supervisor, 'RELEASE', old), \
                     mock.patch.object(supervisor, 'launchctl', side_effect=crash_control), \
                     mock.patch.object(supervisor, 'atomic_write', side_effect=crash_write):
                    with self.assertRaises(SystemExit):
                        supervisor.install(self.root, jobs_dir=self.jobs)
                with mock.patch.object(supervisor, 'RELEASE', new):
                    supervisor.ensure(self.root, jobs_dir=self.jobs)
                    if supervisor._explicit_install(self.root):
                        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
                            supervisor.commit_handoff(self.root, old, self.jobs)
                    self.assertEqual(supervisor.current_release_from_job(self.job()), old)
                    self.assertTrue(self.loaded)
                    self.assertEqual(supervisor.current_release_from_job(loaded_job[0]), old)
                with mock.patch.object(supervisor, 'RELEASE', old):
                    supervisor.ensure(self.root, jobs_dir=self.jobs)
                    self.assertEqual(supervisor.current_release_from_job(self.job()), old)
                self.assertFalse(marker.exists())

    def test_recovery_waits_for_pass_boundary_and_uninstall_wins(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', new):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        with mock.patch.object(supervisor, 'RELEASE', old), \
             mock.patch.object(supervisor, 'load_agent', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                supervisor.install(self.root, jobs_dir=self.jobs)
        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
            with mock.patch.object(supervisor, 'RELEASE', new):
                self.launchctl_mock.reset_mock()
                self.assertIn('engine: upgrade pending', supervisor.ensure(self.root, jobs_dir=self.jobs))
                self.assertNotIn('bootout', self.verbs())
                self.assertEqual(supervisor.selected_release(self.root), old)
                supervisor.commit_handoff(self.root, old, self.jobs)
        self.assertEqual(supervisor.current_release_from_job(self.job()), old)
        with mock.patch.object(supervisor, 'RELEASE', old), \
             mock.patch.object(supervisor, 'load_agent', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                supervisor.install(self.root, jobs_dir=self.jobs)
        supervisor.uninstall(self.root, self.jobs)
        with mock.patch.object(supervisor, 'RELEASE', new):
            self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: disabled')
            self.assertEqual(supervisor.commit_handoff(self.root, old, self.jobs), 'engine: handoff superseded')
        self.assertFalse(self.loaded)

    def test_failed_recovery_is_retried_and_new_explicit_install_supersedes_it(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', new):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        with mock.patch.object(supervisor, 'RELEASE', old), \
             mock.patch.object(supervisor, 'load_agent', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                supervisor.install(self.root, jobs_dir=self.jobs)
        def fail_bootout(*args, **kwargs):
            if args[0] == 'bootout':
                raise supervisor.ServiceError('bootout failed')
            return self.launchctl(*args, **kwargs)
        with mock.patch.object(supervisor, 'RELEASE', new), \
             mock.patch.object(supervisor, 'launchctl', side_effect=fail_bootout):
            with self.assertRaisesRegex(supervisor.ServiceError, 'bootout failed'):
                with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
                    supervisor.commit_handoff(self.root, old, self.jobs)
        self.assertEqual(supervisor.selected_release(self.root), old)
        with mock.patch.object(supervisor, 'RELEASE', new):
            supervisor.install(self.root, jobs_dir=self.jobs)
            self.assertEqual(supervisor.current_release_from_job(self.job()), new)
            self.assertFalse((supervisor.state_dir(self.root) / supervisor.EXPLICIT_INSTALL_MARKER).exists())

    def test_uninstall_between_ensure_checks_cancels_pending_install(self):
        import contextlib
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', new):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        with mock.patch.object(supervisor, 'RELEASE', old), \
             mock.patch.object(supervisor, 'load_agent', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                supervisor.install(self.root, jobs_dir=self.jobs)
        real_lock = supervisor.lock
        @contextlib.contextmanager
        def uninstall_before_lock(path, *args, **kwargs):
            with mock.patch.object(supervisor, 'lock', real_lock):
                supervisor.uninstall(self.root, self.jobs)
            with real_lock(path, *args, **kwargs):
                yield
        with mock.patch.object(supervisor, 'RELEASE', new), \
             mock.patch.object(supervisor, 'lock', side_effect=uninstall_before_lock):
            self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: disabled')
        self.assertFalse(self.loaded)
        self.assertFalse((supervisor.state_dir(self.root) / supervisor.EXPLICIT_INSTALL_MARKER).exists())

    def test_unrelated_registration_is_never_booted_out_by_recovery(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', new):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        with mock.patch.object(supervisor, 'RELEASE', old), \
             mock.patch.object(supervisor, 'load_agent', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                supervisor.install(self.root, jobs_dir=self.jobs)
        import plistlib
        path = supervisor.job_path(supervisor.label('engine', self.root), self.jobs)
        job = self.job()
        job['WorkingDirectory'] = '/unrelated'
        path.write_bytes(plistlib.dumps(job))
        self.launchctl_mock.reset_mock()
        with self.assertRaisesRegex(supervisor.ServiceError, 'unrelated LaunchAgent'):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        self.assertNotIn('bootout', self.verbs())
        self.assertTrue((supervisor.state_dir(self.root) / supervisor.EXPLICIT_INSTALL_MARKER).exists())

    def test_newer_request_supersedes_interrupted_handoff(self):
        old, new = self.releases()
        newest = old.parent / '1.0.2'
        (newest / '.claude-plugin').mkdir(parents=True)
        (newest / '.claude-plugin/plugin.json').write_text('{"version":"1.0.2"}')
        (newest / 'requirements.txt').write_text('SQLAlchemy==2.0.54\n')
        (newest / 'scripts').mkdir()
        (newest / 'scripts/engine.py').touch()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
            with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
                with mock.patch.object(supervisor, 'RELEASE', new):
                    supervisor.ensure(self.root, jobs_dir=self.jobs)
            original = supervisor.atomic_write
            def crash(path, data, *args, **kwargs):
                original(path, data, *args, **kwargs)
                if path.suffix == '.plist':
                    raise SystemExit()
            with mock.patch.object(supervisor, 'atomic_write', side_effect=crash):
                with self.assertRaises(SystemExit):
                    supervisor.commit_handoff(self.root, new, self.jobs)
            with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
                with mock.patch.object(supervisor, 'RELEASE', newest):
                    supervisor.ensure(self.root, jobs_dir=self.jobs)
            supervisor.commit_handoff(self.root, newest, self.jobs)
        self.assertEqual(supervisor.current_release_from_job(self.job()), newest)
        self.assertFalse((supervisor.state_dir(self.root) / supervisor.HANDOFF_MARKER).exists())

    def test_older_ensure_does_not_replace_newer_registered_plugin(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', new):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        self.assertEqual(supervisor.current_release_from_job(self.job()), new)

    def test_waiting_retry_preserves_each_callers_release(self):
        entered, release, calls = threading.Event(), threading.Event(), []
        def ensure(root):
            calls.append(threading.current_thread().name)
            if len(calls) == 1:
                entered.set()
                release.wait(5)
            return 'engine: serving'
        with mock.patch.object(supervisor, 'ensure', side_effect=ensure):
            first = threading.Thread(target=supervisor.ensure_retry, args=(self.root,), name='old')
            second = threading.Thread(target=supervisor.ensure_retry, args=(self.root,), name='new')
            first.start()
            self.assertTrue(entered.wait(5))
            second.start()
            time.sleep(0.1)
            release.set()
            first.join(5); second.join(5)
        self.assertEqual(calls, ['old', 'new'])

    def test_loaded_engine_with_unlinked_lock_defers_upgrade_to_boundary(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
            (supervisor.state_dir(self.root) / 'engine.lock').unlink()
            self.launchctl_mock.reset_mock()
            with mock.patch.object(supervisor, 'RELEASE', new):
                result = supervisor.ensure(self.root, jobs_dir=self.jobs)
            self.assertIn('upgrade pending', result)
            self.assertNotIn('bootout', self.verbs())
            self.assertEqual(supervisor.current_release_from_job(self.job()), old)
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.commit_handoff(self.root, new, self.jobs)
        self.assertEqual(supervisor.current_release_from_job(self.job()), new)

    def test_loaded_during_automatic_explicit_recovery_is_not_restarted(self):
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'load_agent', side_effect=SystemExit('interrupted')):
            with self.assertRaises(SystemExit):
                supervisor.install(self.root, jobs_dir=self.jobs)
        calls = []
        def launchctl(verb, *args, **kwargs):
            calls.append(verb)
            if verb == 'print':
                return calls.count('print') > 1
            return True
        with mock.patch.object(supervisor, 'launchctl', side_effect=launchctl):
            result = supervisor.ensure(self.root, jobs_dir=self.jobs)
        self.assertIn('upgrade pending', result)
        self.assertNotIn('bootout', calls)
        self.assertIsNotNone(supervisor._explicit_install(self.root))

    def test_job_loaded_during_ensure_is_not_restarted(self):
        (self.root / 'store.db').touch()
        calls = []
        def launchctl(verb, *args, **kwargs):
            calls.append(verb)
            if verb == 'print':
                return calls.count('print') > 1
            return True
        with mock.patch.object(supervisor, 'launchctl', side_effect=launchctl):
            result = supervisor.ensure(self.root, jobs_dir=self.jobs)
        self.assertIn('upgrade pending', result)
        self.assertNotIn('bootout', calls)

    def test_loaded_legacy_engine_reports_controlled_restart_guidance(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        self.launchctl_mock.reset_mock()
        with mock.patch.object(supervisor, 'RELEASE', new):
            for _ in range(2):
                result = supervisor.ensure(self.root, jobs_dir=self.jobs)
                self.assertTrue(supervisor.status(self.root, self.jobs)['upgrade_pending'])
                self.assertIn('upgrade pending', result)
                self.assertIn('pass', result)
                self.assertIn('install', result)
                self.assertEqual(supervisor.current_release_from_job(self.job()), old)
        self.assertNotIn('bootout', self.verbs())

    def test_detached_helper_survives_sigterm_to_parent(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        job_path = supervisor.job_path(supervisor.label('engine', self.root), self.jobs)
        helper = self.root / 'helper.py'
        result = self.root / 'finished'
        helper.write_text('import os, signal, time, pathlib\nos.kill(int(os.environ["TEST_PARENT"]), signal.SIGTERM)\ntime.sleep(0.4)\npathlib.Path(os.environ["TEST_RESULT"]).write_text("bootstrap")\n')
        driver = self.root / 'driver.py'
        driver.write_text('''import os, signal, sys, time
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.environ['TEST_SCRIPTS'])
import engine, supervisor
def interrupted(*_):
    Path(os.environ['TEST_SIGNAL']).write_text('received')
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, interrupted)
sys.platform = 'darwin'
with mock.patch.object(supervisor, 'RELEASE', Path(os.environ['TEST_OLD'])), mock.patch.object(supervisor, 'job_path', return_value=Path(os.environ['TEST_JOB'])), mock.patch.object(supervisor, 'launchctl', return_value=True), mock.patch.object(supervisor, '__file__', os.environ['TEST_HELPER']):
    try:
        engine._persist_launchd_handoff(Path(os.environ['TEST_ROOT']), Path(os.environ['TEST_NEW']))
        time.sleep(2)
    except KeyboardInterrupt:
        pass
''')
        env = dict(os.environ, TEST_SCRIPTS=str(Path(supervisor.__file__).parent), TEST_OLD=str(old), TEST_NEW=str(new), TEST_JOB=str(job_path), TEST_HELPER=str(helper), TEST_ROOT=str(self.root), TEST_RESULT=str(result), TEST_SIGNAL=str(self.root / 'signal'))
        driver.write_text(driver.read_text().replace("signal.signal(signal.SIGTERM", "os.environ['TEST_PARENT'] = str(os.getpid())\nsignal.signal(signal.SIGTERM"))
        child = subprocess.Popen([sys.executable, str(driver)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child.wait(timeout=5)
        deadline = time.monotonic() + 3
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue((self.root / 'signal').exists(), 'parent did not receive SIGTERM')
        self.assertTrue(result.exists(), 'helper died before bootstrap after parent received SIGTERM')

    def test_explicit_cache_choice_survives_later_newer_ensures_and_recovery(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                self.doCleanups(); self.setUp()
                old, new = self.releases()
                (self.root / 'store.db').touch()
                with mock.patch.object(supervisor, 'RELEASE', new):
                    supervisor.ensure(self.root, jobs_dir=self.jobs)
                with mock.patch.object(supervisor, 'RELEASE', old):
                    if interrupted:
                        with mock.patch.object(supervisor, 'load_agent', side_effect=SystemExit):
                            with self.assertRaises(SystemExit):
                                supervisor.install(self.root, jobs_dir=self.jobs)
                    else:
                        supervisor.install(self.root, jobs_dir=self.jobs)
                with mock.patch.object(supervisor, 'RELEASE', new):
                    supervisor.ensure(self.root, jobs_dir=self.jobs)
                    supervisor.ensure(self.root, jobs_dir=self.jobs)
                    if supervisor._explicit_install(self.root):
                        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
                            supervisor.commit_handoff(self.root, old, self.jobs)
                    self.assertEqual(supervisor.selected_release(self.root), old)
                    self.assertEqual(supervisor.current_release_from_job(self.job()), old)
                    with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
                        supervisor.ensure(self.root, jobs_dir=self.jobs)
                    self.assertEqual(supervisor.selected_release(self.root), old)
                with mock.patch.object(supervisor, 'RELEASE', new):
                    supervisor.install(self.root, jobs_dir=self.jobs)
                    self.assertEqual(supervisor.selected_release(self.root), new)

    def test_reinstall_after_uninstall_recovers_at_ensure_and_boundary(self):
        for boundary in (False, True):
            with self.subTest(boundary=boundary):
                self.doCleanups(); self.setUp()
                old, new = self.releases()
                (self.root / 'store.db').touch()
                with mock.patch.object(supervisor, 'RELEASE', new):
                    supervisor.ensure(self.root, jobs_dir=self.jobs)
                supervisor.uninstall(self.root, self.jobs)
                with mock.patch.object(supervisor, 'RELEASE', old), mock.patch.object(supervisor, 'load_agent', side_effect=SystemExit):
                    with self.assertRaises(SystemExit):
                        supervisor.install(self.root, jobs_dir=self.jobs)
                with mock.patch.object(supervisor, 'RELEASE', new):
                    if boundary:
                        supervisor.commit_handoff(self.root, old, self.jobs)
                    else:
                        supervisor.ensure(self.root, jobs_dir=self.jobs)
                    self.assertTrue(self.loaded)
                    self.assertEqual(supervisor.current_release_from_job(self.job()), old)
                supervisor.uninstall(self.root, self.jobs)
                with mock.patch.object(supervisor, 'RELEASE', new):
                    self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: disabled')
                    self.assertEqual(supervisor.commit_handoff(self.root, old, self.jobs), 'engine: handoff superseded')
                self.assertFalse(self.loaded)

    def test_uninstall_intent_wins_if_interrupted_before_cleanup(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old), mock.patch.object(supervisor, 'load_agent', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                supervisor.install(self.root, jobs_dir=self.jobs)
        with mock.patch.object(supervisor, 'unload_agent', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                supervisor.uninstall(self.root, self.jobs)
        with mock.patch.object(supervisor, 'RELEASE', new):
            self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: disabled')
            self.assertEqual(supervisor.commit_handoff(self.root, old, self.jobs), 'engine: handoff superseded')
        self.assertFalse(self.loaded)

    def test_install_intent_is_recoverable_before_legacy_pending_write(self):
        old, new = self.releases()
        (self.root / 'store.db').touch()
        supervisor.uninstall(self.root, self.jobs)
        write = supervisor.atomic_write
        def crash(path, data, *args, **kwargs):
            if path.name == supervisor.EXPLICIT_INSTALL_MARKER:
                raise SystemExit('power loss')
            return write(path, data, *args, **kwargs)
        with mock.patch.object(supervisor, 'RELEASE', old), mock.patch.object(supervisor, 'atomic_write', side_effect=crash):
            with self.assertRaises(SystemExit):
                supervisor.install(self.root, jobs_dir=self.jobs)
        with mock.patch.object(supervisor, 'RELEASE', new):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
            self.assertTrue(self.loaded)
            self.assertEqual(supervisor.current_release_from_job(self.job()), old)

    def test_interrupted_uninstall_removes_loaded_job_on_next_ensure(self):
        (self.root / 'store.db').touch()
        supervisor.ensure(self.root, jobs_dir=self.jobs)
        with mock.patch.object(supervisor, 'unload_agent', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                supervisor.uninstall(self.root, self.jobs)
        self.assertTrue(self.loaded)
        self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: disabled')
        self.assertFalse(self.loaded)
        self.assertFalse(supervisor.job_path(supervisor.label('engine', self.root), self.jobs).exists())

    def test_engine_sees_pending_intent_even_before_legacy_marker(self):
        import engine
        old, _ = self.releases()
        (self.root / 'store.db').touch()
        write = supervisor.atomic_write
        def crash(path, data, *args, **kwargs):
            if path.name == supervisor.EXPLICIT_INSTALL_MARKER:
                raise SystemExit('power loss')
            return write(path, data, *args, **kwargs)
        with mock.patch.object(supervisor, 'RELEASE', old), mock.patch.object(supervisor, 'atomic_write', side_effect=crash):
            with self.assertRaises(SystemExit):
                supervisor.install(self.root, jobs_dir=self.jobs)
        self.assertTrue(engine._pending_explicit_install(self.root))

    def test_explicit_installed_release_follows_later_current_pointer(self):
        base = self.root.parent / 'install/versions'
        old, new = base / 'old', base / 'new'
        for release in (old, new):
            (release / '.claude-plugin').mkdir(parents=True)
            (release / '.claude-plugin/plugin.json').write_text('{"version":"1.0.0"}')
            (release / 'requirements.txt').write_text('pins')
            (release / 'scripts').mkdir()
            (release / 'scripts/engine.py').touch()
            (release / 'chief_of_stuff.py').touch()
        pointer = base.parent / 'current'
        pointer.symlink_to(old)
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.install(self.root, jobs_dir=self.jobs)
            pointer.unlink()
            pointer.symlink_to(new)
            self.assertEqual(supervisor.selected_release(self.root), new)
