"""The engine on its own repository installs each new origin/main as its release (the owner, 2026-10-27)."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claude_adapter
import self_update
from evals import make_repo

PLUGIN = {".claude-plugin/plugin.json": json.dumps({"name": "chief-of-stuff", "version": "1.0.0"}),
          "scripts/engine.py": "print('one')\n", "start_coordinator.py": "", "chief_of_stuff.py": ""}


class FollowMainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.install_dir = self.base / "share"
        (self.install_dir / "versions").mkdir(parents=True)
        self.seen = self.base / "state" / "main-release"
        self.upstream = str(self.base / "origin.git")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        # No test runs the real claude: the install's plugin step answers from here.
        patcher = mock.patch.object(claude_adapter, "update_plugin", return_value={"plugin": "1.0.0"})
        self.plugin = patcher.start()
        self.addCleanup(patcher.stop)

    def follow(self, root, upstream=None, lock=contextlib.nullcontext):
        # The fixture's origin is a local path, which same_repo rightly refuses; compare it as a plain string here.
        with mock.patch.object(self_update, "same_repo", lambda a, b: bool(b) and a.strip() == b):
            return self_update.follow_main(root, self.install_dir, self.seen,
                                           self.upstream if upstream is None else upstream, bin_dir=self.bin, lock=lock)

    def test_a_new_main_commit_is_installed_once_and_becomes_current(self):
        root = make_repo.build(self.base, {"files": PLUGIN})
        first = Path(self.follow(root)["release"])
        self.assertEqual((self.install_dir / "current").resolve(), first)
        self.assertEqual((first / "scripts/engine.py").read_text(), "print('one')\n")
        self.assertIsNone(self.follow(root))  # nothing new on main: no install
        (root / "scripts/engine.py").write_text("print('two')\n")
        make_repo.git(["commit", "-am", "two"], root)
        make_repo.git(["push", "origin", "main"], root)
        make_repo.git(["reset", "--hard", "HEAD~1"], root)  # the workspace checkout is not what is installed
        second = Path(self.follow(root)["release"])
        self.assertNotEqual(second, first)
        self.assertEqual((self.install_dir / "current").resolve(), second)
        self.assertEqual((second / "scripts/engine.py").read_text(), "print('two')\n")

    def test_an_install_takes_the_one_install_path_and_brings_the_plugin(self):
        # §14: every install gives I → Pl ∨ F, so self-update installs the way `chief-of-stuff install` does.
        root = make_repo.build(self.base, {"files": PLUGIN})
        result = self.follow(root)
        self.assertTrue((self.bin / "chief-of-stuff").is_file())
        self.plugin.assert_called_once_with(self.install_dir / "current", "1.0.0")
        self.assertEqual(result["plugin"], "1.0.0")

    def test_the_fetch_runs_before_the_lock_and_only_the_install_inside_it(self):
        # The machine-wide lock is never held across a network fetch.
        root = make_repo.build(self.base, {"files": PLUGIN})
        def held():
            raise RuntimeError("held")
        with self.assertRaises(RuntimeError):
            self.follow(root, lock=held)
        subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", self_update.MAIN], check=True, capture_output=True)
        self.assertFalse((self.install_dir / "current").exists())
        self.assertFalse(self.seen.exists())

    def test_an_origin_that_is_not_the_running_releases_upstream_installs_nothing(self):
        # A fork or any other clone carrying chief-of-stuff's files must not replace the release every workspace runs.
        root = make_repo.build(self.base, {"files": PLUGIN})
        self.assertIsNone(self.follow(root, upstream="https://github.com/example/other"))
        self.assertIsNone(self.follow(root, upstream=""))  # a release that names no upstream never self-updates
        self.assertFalse((self.install_dir / "current").exists())

    def test_a_workspace_with_no_origin_installs_nothing(self):
        (self.base / "plain").mkdir()
        self.assertIsNone(self.follow(self.base / "plain"))

    def test_a_refspec_that_maps_another_ref_onto_main_is_ignored(self):
        # Only the upstream's refs/heads/main is installed, whatever the workspace's fetch refspec says.
        root = make_repo.build(self.base, {"files": PLUGIN})
        make_repo.git(["checkout", "-b", "proposal"], root)
        (root / "scripts/engine.py").write_text("print('unreviewed')\n")
        make_repo.git(["commit", "-am", "unreviewed"], root)
        make_repo.git(["push", "origin", "proposal"], root)
        make_repo.git(["config", "remote.origin.fetch", "+refs/heads/proposal:refs/remotes/origin/main"], root)
        make_repo.git(["fetch", "origin"], root)  # origin/main now names the unreviewed branch
        release = Path(self.follow(root)["release"])
        self.assertEqual((release / "scripts/engine.py").read_text(), "print('one')\n")

    def test_ssh_and_https_urls_name_the_same_repository(self):
        upstream = "https://github.com/example/tool"
        for same in ("git@github.com:example/tool.git", "https://github.com/Example/tool.git", "ssh://git@github.com/example/tool"):
            self.assertTrue(self_update.same_repo(same, upstream), same)
        for other in ("git@github.com:fork/tool.git", "https://evil.test/example/tool", "github.com/example/tool",
                      "./github.com/example/tool", "/srv/github.com/example/tool", "file://github.com/example/tool",
                      "ext::sh -c x github.com/example/tool", "https://github.com/x/../example/tool", ""):
            self.assertFalse(self_update.same_repo(other, upstream), other)


class EngineFollowsMainTest(unittest.TestCase):
    def setUp(self):
        import engine  # deferred: the store's dependencies load only for the loop's wiring
        self.engine = engine
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def run_step(self, release, failure=None, result=None, selected=None, pending=False):
        (release / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (release / ".claude-plugin/plugin.json").write_text(json.dumps({"repository": "https://github.com/example/tool"}))
        with mock.patch.object(self.engine.supervisor, "RELEASE", release), \
                mock.patch.object(self.engine.release_selection, "selected_release", return_value=selected or release), \
                mock.patch.object(self.engine, "_pending_explicit_install", return_value=pending), \
                mock.patch.object(self.engine.supervisor, "lock") as locked, \
                mock.patch.object(self.engine.self_update, "follow_main", side_effect=failure, return_value=result) as follow, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.engine._follow_main(self.base / "ws")
            locked.assert_not_called()  # the engine holds no lock across the fetch
            if follow.called:
                with follow.call_args.kwargs["lock"]():
                    pass
        return follow, locked, err.getvalue()

    def test_a_failed_install_is_logged_and_the_loop_goes_on(self):
        follow, locked, err = self.run_step(self.base / "share/versions/1.0.0-abc", subprocess.CalledProcessError(128, "git"))
        follow.assert_called_once_with(self.base / "ws", self.base / "share", mock.ANY, "https://github.com/example/tool",
                                       lock=mock.ANY)
        self.assertTrue(str(locked.call_args.args[0]).endswith("supervisor.lock"))  # installs never race another workspace
        self.assertIn("engine: cannot install origin/main", err)

    def test_a_busy_supervisor_lock_waits_for_a_later_pass_quietly(self):
        _, _, err = self.run_step(self.base / "share/versions/1.0.0-abc", self.engine.supervisor.ServiceError("busy"))
        self.assertEqual(err, "")

    def test_an_install_logs_the_release_and_where_the_plugin_stands(self):
        report = {"release": str(self.base / "share/versions/1.0.0-def"), "plugin": "behind 1.0.0: user at 0.9.0"}
        _, _, err = self.run_step(self.base / "share/versions/1.0.0-abc", result=report)
        self.assertIn("engine: installed origin/main as 1.0.0-def; plugin behind 1.0.0: user at 0.9.0", err)

    def test_an_explicit_release_or_rollback_is_handed_off_before_fetching(self):
        release = self.base / "share/versions/1.0.0-abc"
        for chosen in (self.base / "share/versions/0.9.0-old", self.base / "plugins/cache/selected"):
            with self.subTest(chosen=chosen):
                follow, locked, _ = self.run_step(release, selected=chosen)
                follow.assert_not_called()
                locked.assert_not_called()

    def test_a_pending_explicit_install_of_the_running_release_defers_self_update(self):
        follow, locked, _ = self.run_step(self.base / "share/versions/1.0.0-abc", pending=True)
        follow.assert_not_called()
        locked.assert_not_called()

    def test_an_explicit_install_started_during_fetch_defers_self_update(self):
        release = self.base / "share/versions/1.0.0-abc"
        (release / ".claude-plugin").mkdir(parents=True)
        (release / ".claude-plugin/plugin.json").write_text(json.dumps({"repository": "https://github.com/example/tool"}))
        install = mock.Mock()

        def fetching(*args, lock, **kwargs):
            with lock():
                install()

        with mock.patch.object(self.engine.supervisor, "RELEASE", release), \
                mock.patch.object(self.engine.supervisor, "lock"), \
                mock.patch.object(self.engine.release_selection, "selected_release", return_value=release), \
                mock.patch.object(self.engine, "_pending_explicit_install", side_effect=[False, True]), \
                mock.patch.object(self.engine.self_update, "follow_main", side_effect=fetching):
            self.engine._follow_main(self.base / "ws")
        install.assert_not_called()

    def test_a_release_selection_changed_during_fetch_is_not_overwritten(self):
        release = self.base / "share/versions/1.0.0-abc"
        (release / ".claude-plugin").mkdir(parents=True)
        (release / ".claude-plugin/plugin.json").write_text(json.dumps({"repository": "https://github.com/example/tool"}))
        install = mock.Mock()

        def fetching(*args, lock, **kwargs):
            with lock():
                install()

        with mock.patch.object(self.engine.supervisor, "RELEASE", release), \
                mock.patch.object(self.engine.supervisor, "lock"), \
                mock.patch.object(self.engine.release_selection, "selected_release", side_effect=[
                    release, self.base / "share/versions/0.9.0-old"]), \
                mock.patch.object(self.engine.self_update, "follow_main", side_effect=fetching):
            self.engine._follow_main(self.base / "ws")
        install.assert_not_called()

    def test_an_engine_run_from_a_checkout_installs_nothing(self):
        follow, _, _ = self.run_step(self.base / "checkout")
        follow.assert_not_called()


if __name__ == "__main__":
    unittest.main()
