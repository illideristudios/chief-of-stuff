"""Regressions for workspace-only adversaries at release and recovery boundaries."""
import os
import threading
from pathlib import Path
from unittest import mock

from test_release_workspace_boundary import WorkspaceReleaseBoundary
from test_supervisor import SupervisorTest
import engine_intent
import pages
import supervisor


class ReleaseTraversal(WorkspaceReleaseBoundary):
    def test_workspace_alias_swapped_after_validation_never_reaches_preparation(self):
        candidate = self.release(self.cache / '2.0.0', '2.0.0')
        trusted = candidate / 'scripts/trusted.py'
        trusted.write_text('trusted')
        alias = self.root / 'alias'
        alias.symlink_to(trusted)
        entry = candidate / 'scripts/pages.py'
        entry.unlink()
        entry.symlink_to(alias)
        self.marker.write_text(str(candidate))
        validate = engine_intent.validate_release
        swapped = []
        def validate_then_swap(*args):
            try:
                return validate(*args)
            finally:
                if args[1] == candidate and not swapped:
                    alias.unlink()
                    alias.symlink_to(self.root / 'scripts/pages.py')
                    swapped.append(True)
        stop = threading.Event()
        select = pages._selected_release
        def selected(root):
            result = select(root)
            stop.set()
            return result
        with mock.patch.object(supervisor, 'RELEASE', self.old), \
             mock.patch.object(supervisor, 'data_dir', return_value=self.data), \
             mock.patch.object(engine_intent, 'validate_release', side_effect=validate_then_swap), \
             mock.patch.object(supervisor, 'ensure_venv') as prepare, \
             mock.patch.object(pages.subprocess, 'Popen') as execute, \
             mock.patch.object(pages, '_selected_release', side_effect=selected), \
             mock.patch.object(pages, 'supervise_with_retry', side_effect=lambda root, done, startup: done.wait()):
            pages.supervise_standalone(self.root, stop)
            self.assertTrue(swapped)
            prepare.assert_not_called()
            execute.assert_not_called()

    def test_requirements_cannot_reference_indirect_workspace_inputs(self):
        candidate = self.release(self.cache / '2.0.0', '2.0.0')
        self.marker.write_text(str(candidate))
        for directive in ('-r ' + str(self.root / 'requirements.txt'),
                          '-c ' + str(self.root / 'requirements.txt'),
                          str(self.root), 'pkg @ file://' + str(self.root)):
            with self.subTest(directive=directive):
                (candidate / 'requirements.txt').write_text(directive + '\n')
                with mock.patch.object(supervisor, 'RELEASE', self.old), \
                     mock.patch.object(supervisor, 'data_dir', return_value=self.data):
                    self.assertEqual(supervisor.selected_release(self.root), self.old)

    def test_version_two_cached_selector_is_revalidated(self):
        candidate = self.release(self.cache / '2.0.0', '2.0.0')
        (candidate / 'scripts/pages.py').unlink()
        (candidate / 'scripts/pages.py').symlink_to(self.root / 'scripts/pages.py')
        self.legacy.TRUSTED_INTENT_VERSION = 2
        self.legacy.selected_release = lambda root: candidate
        with self.assertRaises(supervisor.ServiceError):
            __import__('release_selection').selected_release(self.root, self.legacy)


class RecoveryAuthority(SupervisorTest):
    def test_unlinked_held_engine_lock_does_not_authorize_automatic_bootout(self):
        (self.root / 'store.db').touch()
        supervisor.ensure(self.root, jobs_dir=self.jobs)
        with mock.patch.object(supervisor, 'load_agent', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                supervisor.install(self.root, jobs_dir=self.jobs)
        lock = supervisor.state_dir(self.root) / 'engine.lock'
        with supervisor.lock(lock):
            os.unlink(lock)
            self.launchctl_mock.reset_mock()
            self.assertEqual(supervisor.ensure(self.root, jobs_dir=self.jobs), 'engine: upgrade pending; if the running engine cannot hand off, stop new work, '
                                 'verify the current pass has completed and no new pass can start, then run '
                                 'chief-of-stuff supervisor --root /path/to/workspace install from the desired release during a maintenance window')
            self.assertNotIn('bootout', self.verbs())
            self.assertTrue(supervisor._explicit_install(self.root))

    def test_forged_old_plugin_marker_is_not_rollback_authority(self):
        base = self.root.parent / 'plugins/cache/market/chief/1.0.0'
        (base / 'scripts').mkdir(parents=True)
        (base / 'scripts/engine.py').touch()
        (base / 'requirements.txt').touch()
        (base / '.claude-plugin').mkdir()
        (base / '.claude-plugin/plugin.json').write_text('{"version":"1.0.0"}')
        marker = supervisor.state_dir(self.root) / supervisor.PLUGIN_RELEASE_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_text(str(base))
        with mock.patch.object(supervisor, 'RELEASE', base), \
             mock.patch.object(pages, 'VERSION', '1.0.0'), \
             mock.patch.object(pages, '_who', return_value=f'{pages.SERVER}/2.0.0'), \
             mock.patch.object(pages, '_stop', side_effect=AssertionError('must preserve server')) as stop:
            self.assertEqual(pages.ensure(self.root / 'pages', 12345, self.root / 'pages.log', self.root), 'pages: serving')
            stop.assert_not_called()
