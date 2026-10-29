"""Private worker messaging and store files, including upgrades from older releases."""
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import claude_adapter
import store
import store_access


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


class PrivateStateTest(unittest.TestCase):
    def test_existing_messaging_run_is_secured_on_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            box = run / 'mailbox'
            for name in claude_adapter.BOXES:
                (box / name).mkdir(parents=True)
                (box / name).chmod(0o755)
            run.chmod(0o755); box.chmod(0o755)
            (run / 'command.json').write_text(json.dumps({'stdin_prompt': 'secret'}))
            (run / 'stream.jsonl').write_text('secret')
            (run / 'stdout').write_text('secret')
            message = box / 'inbox' / 'message.json'
            message.write_text('secret')
            for path in (run / 'command.json', run / 'stream.jsonl', run / 'stdout', message):
                path.chmod(0o644)
            self.assertTrue(claude_adapter.messaging(run))
            for path in (run, box, *(box / name for name in claude_adapter.BOXES)):
                self.assertEqual(mode(path), 0o700)
            for path in (run / 'command.json', run / 'stream.jsonl', run / 'stdout', message):
                self.assertEqual(mode(path), 0o600)
            self.assertEqual(message.read_text(), 'secret')

    def test_existing_store_and_live_wal_are_secured_without_data_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'store.db'
            store.migrate(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute('CREATE TABLE private_test (body TEXT)')
                connection.execute('INSERT INTO private_test VALUES (?)', ('secret',))
                connection.commit()
                for file in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
                    file.chmod(0o644)
                db = store.Store(path)
                db.engine.dispose()
                for file in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
                    self.assertEqual(mode(file), 0o600)
                self.assertEqual(connection.execute('SELECT body FROM private_test').fetchone(), ('secret',))
            finally:
                connection.close()

    def test_store_refuses_symlink_and_hardlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'other'; target.write_text('do not touch'); target.chmod(0o644)
            path = root / 'store.db'
            for link in ('symlink', 'hardlink'):
                (path.symlink_to(target) if link == 'symlink' else os.link(target, path))
                with self.assertRaises(OSError):
                    store.Store(path)
                self.assertEqual(mode(target), 0o644)
                self.assertEqual(target.read_text(), 'do not touch')
                path.unlink()

    def test_store_rejects_linked_sidecar_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); path = root / 'store.db'
            store.migrate(path)
            target = root / 'other'; target.write_text('untouched'); target.chmod(0o644)
            sidecar = Path(str(path) + '-wal')
            sidecar.symlink_to(target)
            with self.assertRaises(OSError):
                store.Store(path)
            self.assertEqual(mode(target), 0o644)
            self.assertEqual(target.read_text(), 'untouched')

    def test_engine_restart_secures_old_runs_but_leaves_non_messaging_runs_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / '.chief-of-stuff' / 'runs'
            old = runs / '1-implement-1'; other = runs / '2-implement-1'
            for run in (old, other):
                run.mkdir(parents=True); run.chmod(0o755)
            (old / 'command.json').write_text(json.dumps({'stdin_prompt': 'secret'}))
            (old / 'stream.jsonl').write_text('secret')
            (old / 'stream.jsonl').chmod(0o644)
            (other / 'command.json').write_text(json.dumps({'argv': []}))
            db = store_access.migrated_store(root, engine_owned=True)
            db.engine.dispose()
            self.assertEqual(mode(old), 0o700)
            self.assertEqual(mode(old / 'stream.jsonl'), 0o600)
            self.assertEqual(mode(other), 0o755)
            self.assertEqual((old / 'stream.jsonl').read_text(), 'secret')

    def test_multiple_processes_open_existing_wal_and_keep_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'store.db'
            store.migrate(path)
            code = """import sys
sys.path.insert(0, sys.argv[1])
import store
s = store.Store(sys.argv[2])
for n in range(10): s.upsert_item('github:example/repo#' + sys.argv[3], title=str(n))
s.engine.dispose()
"""
            processes = [subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'scripts'), str(path), str(n)])
                         for n in range(4)]
            for process in processes:
                self.assertEqual(process.wait(timeout=30), 0)
            db = store.Store(path)
            try:
                self.assertEqual(len(db.items()), 4)
                self.assertEqual(mode(path), 0o600)
            finally:
                db.engine.dispose()

    def test_messaging_run_rejects_symlinked_mailbox_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); run = root / 'run'; run.mkdir()
            target = root / 'other'; target.mkdir(); target.chmod(0o755)
            (run / 'command.json').write_text(json.dumps({'stdin_prompt': 'secret'}))
            (run / 'mailbox').symlink_to(target, target_is_directory=True)
            with self.assertRaises(OSError):
                claude_adapter.messaging(run)
            self.assertEqual(mode(target), 0o755)

    def test_message_temporary_file_is_private_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'; run.mkdir()
            (run / 'command.json').write_text(json.dumps({'stdin_prompt': 'secret'}))
            self.assertTrue(claude_adapter.messaging(run))
            temporary = run / 'mailbox' / 'inbox' / 'delivery.tmp'
            observed = []
            def sync(fd):
                observed.append(stat.S_IMODE(os.fstat(fd).st_mode))
                self.assertTrue(temporary.exists())
            with patch.object(claude_adapter.os, 'fsync', side_effect=sync):
                claude_adapter.post(run, {'delivery_id': 'delivery', 'body': 'secret'})
            self.assertEqual(observed, [0o600])
            self.assertEqual(mode(temporary.with_suffix('.json')), 0o600)

    def test_mailbox_handoffs_from_multiple_processes_remain_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'; run.mkdir()
            (run / 'command.json').write_text(json.dumps({'stdin_prompt': 'secret'}))
            self.assertTrue(claude_adapter.messaging(run))
            code = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import claude_adapter
run = Path(sys.argv[2])
with claude_adapter.mailbox_lock(run):
    claude_adapter.post(run, {'delivery_id': sys.argv[3], 'body': 'secret'})
"""
            processes = [subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'scripts'), str(run), str(n)])
                         for n in range(6)]
            for process in processes:
                self.assertEqual(process.wait(timeout=30), 0)
            messages = list((run / 'mailbox' / 'inbox').glob('*.json'))
            self.assertEqual(len(messages), 6)
            self.assertTrue(all(mode(path) == 0o600 for path in messages))
            self.assertTrue(all(json.loads(path.read_text())['body'] == 'secret' for path in messages))

    def test_child_exit_is_observed_without_reaping(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.2)'])
        try:
            self.assertFalse(claude_adapter._exited_without_reaping(process.pid))
            import time
            deadline = time.monotonic() + 5
            while not claude_adapter._exited_without_reaping(process.pid):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            self.assertEqual(process.wait(timeout=2), 0)
        finally:
            if process.poll() is None:
                process.kill(); process.wait()
