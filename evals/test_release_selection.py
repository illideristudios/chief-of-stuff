"""Compatibility selection for releases running with the pre-selector supervisor."""
import json
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
        installed = self.root / "install/versions/old"
        with mock.patch.object(supervisor, "RELEASE", self.new), \
             mock.patch.object(supervisor, "current_release", return_value=installed):
            self.marker.write_text(str(self.new))
            self.assertEqual(release_selection.selected_release(self.root, supervisor), installed)

    def test_new_supervisor_owns_the_selection_when_present(self):
        with mock.patch.object(supervisor, "selected_release", return_value=self.new, create=True) as select:
            self.assertEqual(release_selection.selected_release(self.root, supervisor), self.new)
        select.assert_called_once_with(self.root)
