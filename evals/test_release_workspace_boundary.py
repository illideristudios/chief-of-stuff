"""A workspace marker cannot promote workspace-controlled code into a release."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import release_selection
import supervisor


class WorkspaceReleaseBoundary(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.cache = self.base / 'plugins/cache/market/chief'
        self.old = self.release(self.cache / '1.0.0', '1.0.0')
        self.root = self.release(self.cache / '9.0.0', '9.0.0')
        self.child = self.release(self.root / 'nested', '10.0.0')
        self.data = self.base / 'data'
        self.marker = supervisor.state_dir(self.root) / supervisor.PLUGIN_RELEASE_MARKER
        self.marker.parent.mkdir()
        self.legacy = SimpleNamespace(RELEASE=self.old, current_release=lambda: self.old,
                                      data_dir=lambda: self.data, state_dir=supervisor.state_dir,
                                      ServiceError=supervisor.ServiceError)

    def release(self, path, version):
        (path / 'scripts').mkdir(parents=True)
        (path / '.claude-plugin').mkdir()
        (path / '.claude-plugin/plugin.json').write_text(json.dumps({'version': version}))
        for name in ('requirements.txt', 'scripts/engine.py', 'scripts/pages.py'):
            (path / name).write_text('pins\n' if name == 'requirements.txt' else 'malicious code\n')
        return path

    def test_workspace_marker_and_alias_cannot_select_workspace_code(self):
        alias = self.cache / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with mock.patch.object(supervisor, 'RELEASE', self.old), mock.patch.object(supervisor, 'data_dir', return_value=self.data):
            for target in (self.root, self.child, alias):
                with self.subTest(target=target):
                    self.marker.write_text(str(target))
                    self.assertEqual(supervisor.selected_release(self.root), self.old)
                    self.assertEqual(release_selection.selected_release(self.root, supervisor), self.old)
                    self.assertEqual(release_selection.selected_release(self.root, self.legacy), self.old)
            legitimate = self.release(self.cache / '2.0.0', '2.0.0')
            self.marker.write_text(str(legitimate))
            self.assertEqual(supervisor.selected_release(self.root), legitimate)
            self.assertEqual(release_selection.selected_release(self.root, self.legacy), legitimate)

    def test_release_with_workspace_linked_code_is_rejected(self):
        candidate = self.release(self.cache / '2.0.0', '2.0.0')
        for name in ('requirements.txt', 'scripts/engine.py', 'scripts/pages.py', 'scripts/helper.py'):
            with self.subTest(name=name):
                path = candidate / name
                path.unlink(missing_ok=True)
                path.symlink_to(self.root / 'requirements.txt')
                self.marker.write_text(str(candidate))
                with mock.patch.object(supervisor, 'RELEASE', self.old), mock.patch.object(supervisor, 'data_dir', return_value=self.data):
                    self.assertEqual(supervisor.selected_release(self.root), self.old)
                    self.assertEqual(release_selection.selected_release(self.root, self.legacy), self.old)
                path.unlink()
                path.write_text('trusted')

    def test_unsafe_current_or_legacy_selector_cannot_bypass_validation(self):
        with mock.patch.object(supervisor, 'RELEASE', self.old), mock.patch.object(supervisor, 'current_release', return_value=self.root), \
             mock.patch.object(supervisor, 'data_dir', return_value=self.data):
            with self.assertRaises(supervisor.ServiceError):
                supervisor.selected_release(self.root)
        self.legacy.current_release = lambda: self.root
        with self.assertRaises(supervisor.ServiceError):
            release_selection.selected_release(self.root, self.legacy)

    def test_engine_and_board_do_not_prepare_or_execute_workspace_marker(self):
        import engine
        import pages
        import threading
        self.marker.write_text(str(self.root))
        stop = threading.Event()
        select = pages._selected_release
        def selected(root):
            result = select(root)
            stop.set()
            return result
        with mock.patch.object(supervisor, 'RELEASE', self.old), \
             mock.patch.object(supervisor, 'data_dir', return_value=self.data), \
             mock.patch.object(engine, 'adopt_login_path'), \
             mock.patch.object(engine.startup, 'workspace_forge', side_effect=ValueError('fixture stops startup')), \
             mock.patch.object(engine, '_prepare_handoff') as prepare_engine, \
             mock.patch.object(engine, '_handoff') as handoff_engine, \
             mock.patch.object(supervisor, 'ensure_venv') as prepare_board, \
             mock.patch.object(pages.subprocess, 'Popen') as start_board, \
             mock.patch.object(pages, 'supervise_with_retry', side_effect=lambda root, done, startup: done.wait()), \
             mock.patch.object(pages, '_selected_release', side_effect=selected):
            self.assertEqual(engine.main(['--root', str(self.root), '--once']), 2)
            pages.supervise_standalone(self.root, stop)
        prepare_engine.assert_not_called()
        handoff_engine.assert_not_called()
        prepare_board.assert_not_called()
        start_board.assert_not_called()

    def test_version_one_selector_is_not_trusted(self):
        self.legacy.TRUSTED_INTENT_VERSION = 1
        self.legacy.selected_release = mock.Mock(return_value=self.root)
        self.marker.write_text(str(self.root))
        self.assertEqual(release_selection.selected_release(self.root, self.legacy), self.old)
        self.legacy.selected_release.assert_not_called()

class MarkerBaselineTest(unittest.TestCase):
    def test_workspace_marker_does_not_block_a_legitimate_upgrade(self):
        from test_supervisor import SupervisorTest
        from test_review_1174_followup import ReleaseRegressions
        case = SupervisorTest(methodName='runTest')
        case.setUp()
        self.addCleanup(case.doCleanups)
        old, new = ReleaseRegressions.releases(case)
        case.root = old.parent / '9.0.0'
        (case.root / 'scripts').mkdir(parents=True)
        (case.root / '.claude-plugin').mkdir()
        (case.root / '.claude-plugin/plugin.json').write_text('{"version":"9.0.0"}')
        (case.root / 'scripts/engine.py').touch()
        (case.root / 'requirements.txt').touch()
        (case.root / 'store.db').touch()
        with mock.patch.object(supervisor, 'RELEASE', old):
            supervisor.ensure(case.root, jobs_dir=case.jobs)
        marker = supervisor.state_dir(case.root) / supervisor.PLUGIN_RELEASE_MARKER
        marker.write_text(str(case.root))
        with supervisor.lock(supervisor.state_dir(case.root) / 'engine.lock'), \
             mock.patch.object(supervisor, 'RELEASE', new):
            self.assertEqual(supervisor.ensure(case.root, jobs_dir=case.jobs), 'engine: upgrade pending; if the running engine cannot hand off, stop new work, '
                                 'verify the current pass has completed and no new pass can start, then run '
                                 'chief-of-stuff supervisor --root /path/to/workspace install from the desired release during a maintenance window')
            self.assertEqual(marker.read_text(), str(new))
