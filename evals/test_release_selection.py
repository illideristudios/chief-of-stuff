"""Compatibility selection for releases running with the pre-selector supervisor."""
import json
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_selection
import supervisor


class ReleaseSelectionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name).resolve() / "data"
        self.env = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(self.data)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = Path(temp.name).resolve() / "workspace"
        self.root.mkdir()
        self.cache = Path(temp.name).resolve() / "plugins/cache/market/chief"
        self.old = self.make_release(self.cache / "1.0.0", "1.0.0")
        self.new = self.make_release(self.cache / "2.0.0", "2.0.0")
        self.marker = supervisor.state_dir(self.root) / "engine-plugin-release"
        self.marker.parent.mkdir(parents=True)

    def make_release(self, path, version):
        (path / "scripts").mkdir(parents=True)
        (path / "scripts/engine.py").touch()
        (path / "requirements.txt").touch()
        (path / ".claude-plugin").mkdir()
        (path / ".claude-plugin/plugin.json").write_text(json.dumps({"version": version}))
        return path

    def test_only_valid_newer_siblings_can_override_the_running_cache_release(self):
        older = self.make_release(self.cache / "0.9.0", "0.9.0")
        foreign = self.make_release(self.cache.parent / "other/3.0.0", "3.0.0")
        broken = self.make_release(self.cache / "3.0.0", "3.0.0")
        (broken / "scripts/engine.py").unlink()
        with mock.patch.object(supervisor, "RELEASE", self.old):
            for target, expected in ((self.new, self.new), (older, self.old), (foreign, self.old),
                                     (broken, self.old), (self.cache / "missing", self.old)):
                with self.subTest(target=target):
                    self.marker.write_text(str(target))
                    self.assertEqual(release_selection.selected_release(self.root, supervisor), expected)
            self.marker.write_text("\0")
            self.assertEqual(release_selection.selected_release(self.root, supervisor), self.old)

    def test_installed_pointer_takes_precedence_even_for_a_rollback(self):
        installed = self.root.parent / "install/versions/old"
        installed.mkdir(parents=True)
        with mock.patch.object(supervisor, "RELEASE", self.new), \
             mock.patch.object(supervisor, "current_release", return_value=installed):
            self.marker.write_text(str(self.new))
            self.assertEqual(release_selection.selected_release(self.root, supervisor), installed)

    def test_historical_selector_cannot_select_workspace_or_symlink_targets(self):
        fixture = Path(__file__).parent / "fixtures/a0f3a5a5_supervisor.py"
        spec = importlib.util.spec_from_file_location("historical_supervisor", fixture)
        historical = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(historical)
        historical.RELEASE = self.old
        malicious = self.make_release(self.root / "malicious", "9.0.0")
        (malicious / "scripts/pages.py").touch()
        record = {"release": str(malicious), "job": {"Label": historical.label("engine", self.root),
                                                     "WorkingDirectory": str(self.root)}}
        (self.marker.parent / "engine-explicit-install.json").write_text(json.dumps(record))
        self.assertEqual(historical.selected_release(self.root), malicious)
        self.assertEqual(release_selection.selected_release(self.root, historical), self.old)
        alias = self.cache / "9.0.0"
        alias.symlink_to(malicious, target_is_directory=True)
        self.marker.write_text(str(alias))
        self.assertEqual(release_selection.selected_release(self.root, historical), self.old)
        self.marker.write_text(str(self.new))
        (self.new / "requirements.txt").unlink()
        (self.new / "requirements.txt").symlink_to(malicious / "requirements.txt")
        self.assertEqual(release_selection.selected_release(self.root, historical), self.old)

    def test_private_install_intent_selects_pending_and_completed_targets(self):
        private = self.data / "engine-intents"
        private.mkdir(parents=True, mode=0o700)
        label = supervisor.label("engine", self.root)
        intent = {"operation": "install", "release": str(self.old),
                  "job": {"Label": label, "WorkingDirectory": str(self.root)}}
        with mock.patch.object(supervisor, "RELEASE", self.new):
            for pending in (True, False):
                intent["pending"] = pending
                (private / (label + ".json")).write_text(json.dumps(intent))
                self.assertEqual(release_selection.selected_release(self.root, supervisor), self.old)
                self.assertEqual(release_selection.explicit_release(self.root, supervisor), self.old)
            malicious = self.make_release(self.root / "malicious", "9.0.0")
            intent["release"] = str(malicious)
            (private / (label + ".json")).write_text(json.dumps(intent))
            with self.assertRaises(supervisor.ServiceError):
                release_selection.selected_release(self.root, supervisor)

    def test_cached_selector_is_never_called(self):
        with mock.patch.object(supervisor, "selected_release", side_effect=AssertionError("untrusted selector"), create=True), \
             mock.patch.object(supervisor, "RELEASE", self.old):
            self.marker.write_text(str(self.new))
            self.assertEqual(release_selection.selected_release(self.root, supervisor), self.new)
