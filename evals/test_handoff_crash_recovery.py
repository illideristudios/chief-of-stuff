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
                old, new = self.releases() if verb == 'bootout' else (old, new)
                (self.root / 'store.db').touch()
                marker = supervisor.state_dir(self.root) / supervisor.PLUGIN_RELEASE_MARKER
                with mock.patch.object(supervisor, 'RELEASE', old):
                    supervisor.install(self.root, jobs_dir=self.jobs)
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
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
with mock.patch.object(supervisor, 'RELEASE', Path(os.environ['TEST_OLD'])), mock.patch.object(supervisor, 'job_path', return_value=Path(os.environ['TEST_JOB'])), mock.patch.object(supervisor, 'launchctl', return_value=True), mock.patch.object(supervisor, '__file__', os.environ['TEST_HELPER']):
    try:
        engine._persist_launchd_handoff(Path(os.environ['TEST_ROOT']), Path(os.environ['TEST_NEW']))
        time.sleep(2)
    except KeyboardInterrupt:
        pass
''')
        env = dict(os.environ, TEST_SCRIPTS=str(Path(supervisor.__file__).parent), TEST_OLD=str(old), TEST_NEW=str(new), TEST_JOB=str(job_path), TEST_HELPER=str(helper), TEST_ROOT=str(self.root), TEST_RESULT=str(result))
        driver.write_text(driver.read_text().replace("signal.signal(signal.SIGTERM", "os.environ['TEST_PARENT'] = str(os.getpid())\nsignal.signal(signal.SIGTERM"))
        child = subprocess.Popen([sys.executable, str(driver)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child.wait(timeout=5)
        deadline = time.monotonic() + 3
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(result.exists(), 'helper died before bootstrap after parent received SIGTERM')
