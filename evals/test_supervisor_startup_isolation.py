"""Automatic supervisor entry points must ignore workspace Python startup hooks."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

RELEASE = Path(__file__).resolve().parents[1]


class SupervisorStartupIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="supervisor ' launch ")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.marker = self.workspace / "poisoned"
        (self.workspace / "sitecustomize.py").write_text(f"open({str(self.marker)!r}, 'a').write('site\\n')\n")
        (self.workspace / "json.py").write_text(f"open({str(self.marker)!r}, 'a').write('json\\n')\nraise RuntimeError('workspace json loaded')\n")
        plugin = self.base / "plugin ' $(touch injected)"
        plugin.symlink_to(RELEASE, target_is_directory=True)
        self.env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(plugin),
                        CLAUDE_PLUGIN_DATA=str(self.base / 'data'), PYTHONPATH=':')

    def run_driver(self, code):
        return subprocess.run([sys.executable, '-I', '-c', code, str(RELEASE), str(self.workspace)],
                              cwd=self.workspace, env=self.env, capture_output=True, text=True, timeout=30)

    def await_helper(self):
        log = self.workspace / '.chief-of-stuff/engine-ensure.log'
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log.exists() and log.read_text().strip():
                return log.read_text()
            time.sleep(0.05)
        self.fail('detached helper did not finish')

    def test_session_start_hook_runs_isolated_with_poisoned_pythonpath(self):
        hook = json.loads((RELEASE / 'hooks/hooks.json').read_text())['hooks']['SessionStart'][0]['hooks'][0]['command']
        for value in (':', str(self.workspace)):
            with self.subTest(pythonpath=value):
                result = subprocess.run(hook, shell=True, cwd=self.workspace,
                                        env=dict(self.env, PYTHONPATH=value), capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('engine: not configured', result.stdout)
                self.assertFalse(self.marker.exists())
                self.assertFalse((self.workspace / 'injected').exists())

    def test_hook_preserves_auth_environment_and_strips_python_configuration(self):
        binary_dir = self.base / 'bin'
        binary_dir.mkdir()
        observed = self.base / 'observed'
        python = binary_dir / 'python3'
        import shlex
        python.write_text('#!/bin/sh\n'
                          'printf "%s\\n%s\\n%s\\n" "${PYTHONCUSTOM-unset}" '
                          '"${PYTHONPATH-unset}" "$SSH_AUTH_SOCK" > ' + shlex.quote(str(observed)) + '\n'
                          'exec ' + shlex.quote(sys.executable) + ' "$@"\n')
        python.chmod(0o755)
        hook = json.loads((RELEASE / 'hooks/hooks.json').read_text())['hooks']['SessionStart'][0]['hooks'][0]['command']
        env = dict(self.env, PATH=str(binary_dir) + os.pathsep + os.environ['PATH'],
                   PYTHONCUSTOM='unsafe', SSH_AUTH_SOCK='auth-socket')
        result = subprocess.run(hook, shell=True, cwd=self.workspace, env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(observed.read_text().splitlines(), ['unset', 'unset', 'auth-socket'])

    def test_detached_helper_starts_isolated_before_importing(self):
        (self.workspace / 'store.db').touch()
        (self.workspace / '.chief-of-stuff').mkdir()
        (self.workspace / '.chief-of-stuff/engine.disabled').touch()
        driver = """
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'scripts'))
import supervisor
os.environ['PYTHONPATH'] = ':'
print(supervisor.ensure_background(Path(sys.argv[2])))
"""
        result = self.run_driver(driver)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('engine: ensuring', result.stdout)
        log = self.await_helper()
        self.assertTrue('engine: disabled' in log or 'requires macOS' in log, log)
        self.assertFalse(self.marker.exists())

    def test_coordinator_launches_real_supervisor_isolated(self):
        (self.workspace / 'store.db').touch()
        (self.workspace / '.chief-of-stuff').mkdir()
        (self.workspace / '.chief-of-stuff/engine.disabled').touch()
        driver = """
import os, subprocess, sys
from pathlib import Path
from unittest import mock
release, root = map(Path, sys.argv[1:])
sys.path.insert(0, str(release))
import start_coordinator as start
real_run = subprocess.run
def run(argv, **kwargs):
    if 'notify_service.py' in str(argv[1]):
        return subprocess.CompletedProcess(argv, 0, '', '')
    return real_run(argv, **kwargs)
os.environ['PYTHONPATH'] = str(root)
with mock.patch.object(start, 'resolve', return_value='/bin/true'), \\
     mock.patch.object(start, 'login_argv', side_effect=lambda command: command), \\
     mock.patch.object(start, 'install', return_value=release), \\
     mock.patch.object(start.subprocess, 'run', side_effect=run), \\
     mock.patch.object(start.subprocess, 'call', return_value=0):
    raise SystemExit(start.main(['--runtime', 'claude', '--root', str(root)]))
"""
        result = self.run_driver(driver)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('engine: service setup failed', result.stderr)
        log = self.await_helper()
        self.assertTrue('engine: disabled' in log or 'requires macOS' in log, log)
        self.assertFalse(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
