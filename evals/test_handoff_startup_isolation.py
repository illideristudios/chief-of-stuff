"""Real handoff subprocesses must not import workspace Python startup code."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RELEASE = Path(__file__).resolve().parents[1]


class HandoffStartupIsolation(unittest.TestCase):
    def test_detached_commit_handoff_ignores_poisoned_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / 'workspace'
            root.mkdir()
            marker = base / 'executed'
            (root / 'sitecustomize.py').write_text(f'open({str(marker)!r}, "w").write("executed")\n')
            driver = '''import os, plistlib, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import engine
root = Path(sys.argv[2]); os.chdir(root)
job = root / "job.plist"; job.write_bytes(plistlib.dumps({}))
os.environ["PYTHONPATH"] = str(root)
with mock.patch.object(engine.sys, "platform", "darwin"), \\
     mock.patch.object(engine.supervisor, "job_path", return_value=job), \\
     mock.patch.object(engine.supervisor, "current_release_from_job", return_value=engine.supervisor.RELEASE), \\
     mock.patch.object(engine.supervisor, "launchctl", return_value=True):
    try:
        engine._persist_launchd_handoff(root, engine.supervisor.RELEASE)
    except engine.supervisor.ServiceError:
        pass
'''
            result = subprocess.run([sys.executable, '-I', '-c', driver, str(RELEASE / 'scripts'), str(root)],
                                    cwd=root, env=dict(os.environ, PYTHONPATH=str(root), CLAUDE_PLUGIN_DATA=str(base / "data")),
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists(), result.stderr)
