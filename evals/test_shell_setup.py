"""The configured login shell chooses runtime executables and receives exact argv."""

import sys
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import shell_setup as shell  # noqa: E402


class ShellSetupTest(unittest.TestCase):
    def test_login_shell_resolves_a_binary_from_zshrc(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "home"
            home.mkdir()
            binary = root / "bin" / "fakecli"
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            (home / ".zshrc").write_text(f'export PATH="{binary.parent}:$PATH"\n')
            source = {"SHELL": "/bin/zsh", "HOME": str(home), "PATH": "/usr/bin:/bin"}
            self.assertEqual(shell.resolve("fakecli", source=source), str(binary))

    def test_shell_input_cannot_be_command_text(self):
        with self.assertRaises(shell.ShellError):
            shell.resolve("codex; echo leaked")

    def test_login_command_keeps_worker_arguments_separate(self):
        source = {"SHELL": "/bin/zsh"}
        self.assertEqual(shell.login_argv(["/bin/echo", "two words"])[-2:], ["/bin/echo", "two words"])
        self.assertEqual(shell.clean_env(dict(source, CODEX_SESSION_ID="secret")), source)
        with mock.patch.object(shell, "login_shell", return_value="/bin/fish"):
            self.assertEqual(shell.login_argv(["/bin/echo", "two words"]),
                             ["/bin/fish", "-lic", "exec $argv", "/bin/echo", "two words"])

    def test_clean_env_keeps_every_chief_key(self):
        """#658: the worker guard binds from CHIEF_* variables; stripping any one leaves it unbound."""
        source = {"CHIEF_ROOT": "/r", "CHIEF_ISSUE_KEY": "k", "CHIEF_PROTECTED_BRANCHES": "[]", "OTHER": "x"}
        self.assertEqual(shell.clean_env(source), {k: v for k, v in source.items() if k != "OTHER"})

    def test_bad_configured_shell_is_reported(self):
        with self.assertRaises(shell.ShellError):
            shell.login_shell({"SHELL": "/missing/shell"})

    def test_command_cleanup_is_bounded_and_preserves_the_original_failure(self):
        """#798: even SIGKILL cannot guarantee that a child can be reaped promptly."""
        for failure_type in (subprocess.TimeoutExpired, KeyboardInterrupt):
            for stuck in (False, True):
                failure = (subprocess.TimeoutExpired("workspace command", 0.1)
                           if failure_type is subprocess.TimeoutExpired else KeyboardInterrupt())
                with self.subTest(failure=failure_type.__name__, stuck=stuck):
                    child = mock.MagicMock(pid=123, returncode=None)
                    child.__enter__.return_value = child
                    # Popen.__exit__ waits implicitly, even when an explicit wait was bounded.
                    child.__exit__.side_effect = lambda *args: child.wait()
                    child.poll.return_value = None
                    waits = []

                    def wait(timeout=None):
                        self.assertIsNotNone(timeout, "cleanup must never wait without a timeout")
                        self.assertGreater(timeout, 0)
                        waits.append(timeout)
                        if len(waits) == 1:
                            raise failure
                        if stuck:
                            raise subprocess.TimeoutExpired("unreapable child", timeout)
                        child.returncode = -shell.signal.SIGKILL
                        return child.returncode

                    child.wait.side_effect = wait
                    with tempfile.TemporaryDirectory() as directory, \
                         mock.patch.object(shell, "login_shell", return_value="/bin/sh"), \
                         mock.patch.object(shell.subprocess, "Popen", return_value=child), \
                         mock.patch.object(shell, "_unreaped_children", [], create=True), \
                         mock.patch.object(shell.os, "killpg") as kill:
                        root = Path(directory)
                        with self.assertRaises(type(failure)) as raised:
                            shell.run_command("exit 0", root, workspace=root, timeout=0.1)
                    self.assertIs(raised.exception, failure)
                    kill.assert_called_once_with(child.pid, shell.signal.SIGKILL)
                    self.assertEqual(len(waits), 2)
                    self.assertLessEqual(waits[1], 1, "cleanup promises at most one additional second")
                    if stuck:
                        self.assertIn(f"pid {child.pid}", str(raised.exception))
                    else:
                        self.assertNotIn(f"pid {child.pid}", str(raised.exception))

    def test_teardown_warning_distinguishes_an_unreaped_child(self):
        for stuck in (False, True):
            with self.subTest(stuck=stuck):
                child = mock.Mock(pid=798, returncode=None)
                failure = subprocess.TimeoutExpired("workspace command", 120)
                child.wait.side_effect = [failure, subprocess.TimeoutExpired("cleanup", 1) if stuck else 0]
                with tempfile.TemporaryDirectory() as directory, \
                     mock.patch.object(shell, "login_shell", return_value="/bin/sh"), \
                     mock.patch.object(shell.subprocess, "Popen", return_value=child), \
                     mock.patch.object(shell, "_unreaped_children", [], create=True), \
                     mock.patch.object(shell.os, "killpg"):
                    root = Path(directory)
                    warnings = shell.teardown(root, root / "tree", "worker", "cleanup")
                self.assertEqual(len(warnings), 1)
                self.assertIn("timed out", warnings[0])
                if stuck:
                    self.assertIn("pid 798", warnings[0])
                    self.assertIn("not reaped", warnings[0])
                else:
                    self.assertNotIn("pid 798", warnings[0])

    def test_unreaped_child_is_retained_and_polled_by_later_commands(self):
        child = mock.Mock(pid=798, returncode=None)
        child.wait.side_effect = [subprocess.TimeoutExpired("command", 0.1),
                                  subprocess.TimeoutExpired("cleanup", 1)]
        child.poll.side_effect = [None, -shell.signal.SIGKILL]
        finished = mock.Mock(returncode=0)
        pending = []
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(shell, "login_shell", return_value="/bin/sh"), \
             mock.patch.object(shell.subprocess, "Popen", side_effect=[child, finished, finished]), \
             mock.patch.object(shell, "_unreaped_children", pending, create=True), \
             mock.patch.object(shell.os, "killpg"):
            root = Path(directory)
            with self.assertRaises(subprocess.TimeoutExpired):
                shell.run_command("cleanup", root, workspace=root, timeout=0.1)
            self.assertEqual(pending, [child], "retain Popen without relying on its destructor")
            child.poll.assert_not_called()
            for status in (None, -shell.signal.SIGKILL):
                result = shell.run_command("exit 0", root, workspace=root)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(pending, [child] if status is None else [])
            self.assertEqual(child.poll.call_args_list, [mock.call(), mock.call()])
            self.assertEqual(child.wait.call_args_list, [mock.call(timeout=0.1), mock.call(timeout=1)])

    def standin(self, path: Path, knows: Path) -> Path:
        """An executable at `path` that answers `-lic "command -v <knows' name>"` with `knows`, and nothing else."""
        path.write_text(f"#!{sys.executable}\nimport sys\n"
                        f"if sys.argv[1:] != ['-lic', 'command -v {knows.name}']:\n    sys.exit(1)\n"
                        f"print({str(knows)!r})\n")
        path.chmod(0o755)
        return path

    def test_chief_of_stuff_shell_stands_in_for_lookups_only(self):
        """#48: an eval case has to make a runtime absent, and the harness cannot take a real binary off PATH.
        With `CHIEF_OF_STUFF_SHELL` set, `resolve` asks that program and not `$SHELL`. The stand-in is the only
        thing that knows where `fakecli48` is: no PATH and no rc file names its directory. It answers lookups and
        nothing else: the shell a launch goes through is still `$SHELL`."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "home").mkdir()
            binary = root / "opt" / "fakecli48"
            binary.parent.mkdir()
            binary.write_text(f"#!{sys.executable}\n")
            binary.chmod(0o755)
            source = {"SHELL": "/bin/zsh", "HOME": str(root / "home"), "PATH": "/usr/bin:/bin"}
            self.assertIsNone(shell.resolve("fakecli48", source=source), "$SHELL alone does not find it")
            source["CHIEF_OF_STUFF_SHELL"] = str(self.standin(root / "standin.py", binary))
            self.assertEqual(shell.resolve("fakecli48", source=source), str(binary))
            self.assertIsNone(shell.resolve("othercli48", source=source), "the stand-in knows no other program")
            self.assertEqual(shell.login_shell(source), "/bin/zsh", "the login shell a launch uses is $SHELL")
            # No `source`: production and the eval harness set the variable in the process environment.
            with mock.patch.dict(shell.os.environ, source, clear=True):
                self.assertEqual(shell.resolve("fakecli48"), str(binary))
                self.assertIsNone(shell.resolve("othercli48"))

    def test_a_launch_goes_through_the_login_shell_not_the_lookup_stand_in(self):
        """The stand-in is named as a shell `login_argv` supports, so only its path tells it from `$SHELL`: a worker
        launched through it would run with no login setup at all, and nothing would say so."""
        with tempfile.TemporaryDirectory() as d:
            standin = self.standin(Path(d) / "sh", Path(d) / "fakecli48")
            env = {"SHELL": "/bin/sh", "CHIEF_OF_STUFF_SHELL": str(standin), "PATH": "/usr/bin:/bin"}
            with mock.patch.dict(shell.os.environ, env, clear=True):
                self.assertEqual(shell.login_argv(["/bin/echo", "two words"]),
                                 ["/bin/sh", "-lic", 'exec "$@"', "chief-of-stuff", "/bin/echo", "two words"])
                with mock.patch.object(shell.os, "execve") as execve:
                    shell.exec_login(["/bin/echo", "two words"])
            path, argv, _ = execve.call_args.args
            self.assertEqual((path, argv[0]), ("/bin/sh", "/bin/sh"))
