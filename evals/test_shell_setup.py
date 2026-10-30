"""The configured login shell chooses runtime executables and receives exact argv."""

import sys
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

    def test_login_path_is_the_login_shells_path_and_ignores_rc_chatter(self):
        # #871: launchd's PATH is short; the engine takes this one instead.
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            (home / ".zshrc").write_text('echo "welcome back"\nexport PATH="/opt/extra/bin:$PATH"\n')
            got = shell.login_path({"SHELL": "/bin/zsh", "HOME": str(home), "PATH": "/usr/bin:/bin"})
            self.assertTrue(got.startswith("/opt/extra/bin:"), got)
            self.assertNotIn("welcome", got)

    def test_an_unreadable_login_path_is_empty(self):
        self.assertEqual(shell.login_path({"SHELL": "/missing/shell"}), "")

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
