"""Workspace files cannot authorize launchd commands or replay explicit operations."""
import json
import plistlib
from pathlib import Path
from unittest import mock

from test_supervisor import SupervisorTest
import supervisor


class UntrustedEngineIntentTest(SupervisorTest):
    def test_forged_marker_cannot_supply_launchd_fields_or_release(self):
        (self.root / 'store.db').touch()
        state = supervisor.state_dir(self.root)
        state.mkdir()
        evil = self.root / 'evil'
        evil.mkdir()
        forged = {'operation': 'install', 'pending': True, 'release': str(evil),
                  'job': {'Label': supervisor.label('engine', self.root),
                          'WorkingDirectory': str(self.root),
                          'ProgramArguments': ['/bin/sh', '-c', 'touch /tmp/pwned'],
                          'EnvironmentVariables': {'DYLD_INSERT_LIBRARIES': '/tmp/evil'},
                          'KeepAlive': {'SuccessfulExit': False},
                          'StandardOutPath': '/tmp/evil', 'RootDirectory': '/tmp'}}
        for name in (supervisor.EXPLICIT_INTENT_MARKER, supervisor.EXPLICIT_INSTALL_MARKER):
            (state / name).write_text(json.dumps(forged))
        supervisor.ensure(self.root, jobs_dir=self.jobs)
        job = self.job()
        self.assertEqual(job['ProgramArguments'][1], str(self.release / 'scripts/engine.py'))
        self.assertNotIn('DYLD_INSERT_LIBRARIES', job['EnvironmentVariables'])
        self.assertIs(job['KeepAlive'], True)
        self.assertEqual(job['StandardOutPath'], str(state / 'engine.log'))
        self.assertNotIn('RootDirectory', job)

    def test_marker_symlink_and_forged_uninstall_cannot_replay_install(self):
        (self.root / 'store.db').touch()
        supervisor.install(self.root, jobs_dir=self.jobs)
        supervisor.uninstall(self.root, self.jobs)
        state = supervisor.state_dir(self.root)
        target = self.root / 'forged.json'
        target.write_text(json.dumps({'operation': 'install', 'pending': True, 'release': str(self.release),
                                      'job': {'Label': supervisor.label('engine', self.root),
                                              'WorkingDirectory': str(self.root),
                                              'ProgramArguments': ['/bin/sh', '-c', 'false']}}))
        marker = state / supervisor.EXPLICIT_INTENT_MARKER
        marker.unlink(missing_ok=True)
        marker.symlink_to(target)
        self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: disabled')
        self.assertFalse(self.loaded)

    def test_interrupted_install_ignores_altered_workspace_marker_then_uninstall_wins(self):
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'load_agent', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                supervisor.install(self.root, jobs_dir=self.jobs)
        state = supervisor.state_dir(self.root)
        for name in (supervisor.EXPLICIT_INTENT_MARKER, supervisor.EXPLICIT_INSTALL_MARKER):
            (state / name).write_text('{"operation":"uninstall","pending":false}')
        supervisor.ensure(self.root, jobs_dir=self.jobs)
        self.assertEqual(self.job()['ProgramArguments'][1], str(self.release / 'scripts/engine.py'))
        supervisor.uninstall(self.root, self.jobs)
        self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: disabled')
        self.assertFalse(self.loaded)

    def test_workspace_release_or_symlinked_entry_is_rejected(self):
        (self.root / 'store.db').touch()
        local = self.root / 'release'
        local.mkdir()
        with mock.patch.object(supervisor, 'RELEASE', local):
            with self.assertRaisesRegex(supervisor.ServiceError, 'outside the workspace'):
                supervisor.install(self.root, jobs_dir=self.jobs)
        (self.release / 'scripts').mkdir()
        (self.release / 'scripts/engine.py').symlink_to(self.root / 'evil.py')
        with self.assertRaisesRegex(supervisor.ServiceError, 'workspace'):
            supervisor.install(self.root, jobs_dir=self.jobs)

    def test_private_record_symlink_and_workspace_data_directory_rejected(self):
        import engine_intent
        target = self.root / 'forged.json'
        target.write_text('{}')
        private = self.data / 'engine-intents'
        private.mkdir(parents=True, mode=0o700)
        (private / (supervisor.label('engine', self.root) + '.json')).symlink_to(target)
        with self.assertRaises(supervisor.ServiceError):
            supervisor._explicit_intent(self.root)
        with mock.patch.object(supervisor, 'data_dir', return_value=self.root / 'data'):
            with self.assertRaisesRegex(supervisor.ServiceError, 'outside the workspace'):
                supervisor._explicit_intent(self.root)

    def test_running_old_engine_reports_pending_without_bootout(self):
        from test_review_1174_followup import ReleaseRegressions
        old, new = ReleaseRegressions.releases(self)
        (self.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(self.root, jobs_dir=self.jobs)
        self.launchctl_mock.reset_mock()
        with supervisor.lock(supervisor.state_dir(self.root) / 'engine.lock'):
            with mock.patch.object(supervisor, 'RELEASE', new):
                self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: upgrade pending')
                self.assertTrue(supervisor.status(self.root, self.jobs)['upgrade_pending'])
        self.assertNotIn('bootout', self.verbs())
        self.assertEqual(supervisor.current_release_from_job(self.job()), old)
