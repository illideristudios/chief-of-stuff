"""Live operator messages through the real detached Claude runner and a fake stream-json host (#1160)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import claude_adapter
import process_status
import store
import worker
from model_port import Reply, Request

KEY = "github:example/project#42"

# Echoes each stdin line at once, as `--replay-user-messages` does mid-turn. The first turn ends when GATE exists; a
# later turn answers the follow-ups the CLI read, after stdin closes. NOECHO models a write the CLI never acknowledged.
HOST = '''
import json, os, sys, threading, time, uuid
from pathlib import Path
gate = Path(sys.argv[1])
seen, first = [], threading.Event()
def out(event):
    print(json.dumps(event), flush=True)
def read():
    for line in sys.stdin:
        message = json.loads(line)
        seen.append(message)
        if not os.environ.get("NOECHO") or len(seen) == 1:
            out({"type": "user", "isReplay": True, "uuid": message.get("uuid") or str(uuid.uuid4()),
                 "message": message["message"]})
        first.set()
reader = threading.Thread(target=read)
reader.start()
first.wait()
while not gate.exists():
    time.sleep(0.02)
out({"type": "result", "subtype": "success", "is_error": False, "result": "first turn", "num_turns": 1})
reader.join()
if len(seen) > 1:
    out({"type": "result", "subtype": "success", "is_error": False,
         "result": f"followed up with {len(seen) - 1}", "num_turns": 2})
'''


def wait_for(condition, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = condition()
        if got:
            return got
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


class LiveMessagesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.path = self.root / "store.db"
        store.migrate(self.path)
        self.db = self.open_store()
        self.db.upsert_item(KEY, title="A task")
        self.db.record_segment(KEY, "implement", 1, "start")
        self.run_dir = worker._run_dir(self.root, KEY, "implement", 1)
        self.run_dir.mkdir(parents=True)
        self.gate = self.root / "gate"
        self.host = self.root / "host.py"
        self.host.write_text(HOST)
        self.session = str(uuid.uuid4())

    def open_store(self) -> store.Store:
        db = store.Store(self.path)
        self.addCleanup(db.engine.dispose)
        return db

    def start(self, noecho: bool = False) -> subprocess.Popen:
        env = {"NOECHO": "1"} if noecho else {}
        runner = claude_adapter.spawn([sys.executable, str(self.host), str(self.gate), self.session],
                                      Request("the assignment", session_id=self.session, cwd=str(self.root)),
                                      self.run_dir, env, stdin_prompt="the assignment")
        self.addCleanup(self.stop, runner)
        started = wait_for(lambda: (self.run_dir / "started.json").exists()
                           and json.loads((self.run_dir / "started.json").read_text()))
        self.host_pid = started["pid"]
        wait_for(lambda: claude_adapter.intake_open(self.run_dir))
        self.save_record(runner.pid, process_status.identity(runner.pid, self.session))
        return runner

    def stop(self, runner: subprocess.Popen) -> None:
        self.gate.touch()
        for pid in (runner.pid, getattr(self, "host_pid", None)):
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        runner.wait(timeout=5)

    def save_record(self, runner_pid: int, runner_identity: str | None, **extra) -> None:
        self.db.set_resume(KEY, {"runs": {"implement-1": {
            "session_id": self.session, "run_dir": str(self.run_dir), "runner_pid": runner_pid,
            "runner_identity": runner_identity, **extra}}})

    def queue(self, body: str = "Please check the timeout.", request_id: str | None = None):
        return self.db.queue_worker_message(KEY, "implement", 1, self.session, body,
                                            request_id=request_id or str(uuid.uuid4()))

    def status(self, message, db: store.Store | None = None):
        return next(m for m in (db or self.db).worker_messages(KEY) if m.id == message.id)

    def written(self) -> list[str]:
        """Every uuid the host echoed, i.e. every message the runner wrote that the CLI read."""
        events = [json.loads(line) for line in (self.run_dir / "stream.jsonl").read_text().splitlines() if line]
        return [e["uuid"] for e in events if e.get("type") == "user"]

    def test_a_mid_turn_message_is_delivered_across_an_engine_restart_and_the_result_still_collects(self):
        runner = self.start()
        message = self.queue()
        worker.deliver_messages(self.db, self.root)
        claim = self.status(message)
        self.assertEqual(claim.status, "sending")
        wait_for(lambda: (self.run_dir / claude_adapter.MAILBOX / "acks" / f"{claim.delivery_id}.json").exists())
        restarted = self.open_store()  # a new engine settles what the old one handed off
        worker.deliver_messages(restarted, self.root)
        delivered = self.status(message, restarted)
        self.assertEqual(delivered.status, "delivered")
        self.assertIn(claim.delivery_id, delivered.evidence)
        self.gate.touch()
        self.assertEqual(runner.wait(timeout=10), 0)
        self.assertEqual(self.written().count(claim.delivery_id), 1)
        reply = claude_adapter.classify(json.loads((self.run_dir / "exit.json").read_text())["exit_code"],
                                        (self.run_dir / "stdout").read_text(), "")
        self.assertIsInstance(reply, Reply)
        self.assertEqual(reply.text, "followed up with 1")

    def test_ended_unsupported_and_legacy_workers_fail_without_any_write(self):
        cases = {"codex": "codex workers cannot", "legacy": "without live messaging", "ended": "attempt ended"}
        for case, reason in cases.items():
            with self.subTest(case):
                launch = {"argv": [], "cwd": "", "session_id": self.session, "env": {}}
                if case != "legacy":
                    launch["stdin_prompt"] = "p"
                (self.run_dir / "command.json").write_text(json.dumps(launch))
                self.save_record(1, "x", **({"runtime": "codex"} if case == "codex" else {}))
                message = self.queue()
                if case == "ended":
                    self.db.end_segment(KEY, "implement", 1, "end", "done")
                worker.deliver_messages(self.db, self.root)
                row = self.status(message)
                self.assertEqual(row.status, "failed")
                self.assertIn(reason, row.evidence)
                self.assertIsNone(row.delivery_id)
        self.assertFalse((self.run_dir / claude_adapter.MAILBOX).exists())

    def test_the_sweep_waits_for_a_handoff_in_progress_and_fails_only_a_crashed_one(self):
        self.start()
        message = self.queue()
        identity = (message.id, KEY, "implement", 1, self.session)
        swept = threading.Event()

        def sweep() -> None:
            worker.deliver_messages(self.open_store(), self.root)
            swept.set()

        with claude_adapter.mailbox_lock(self.run_dir):  # a handoff between its claim and its inbox write
            claim = self.db.begin_worker_message_delivery(*identity)
            sweeper = threading.Thread(target=sweep)
            sweeper.start()
            self.assertFalse(swept.wait(0.5))  # blocked on the lock: it cannot see "claimed, no file" yet
            self.assertEqual(self.status(message).status, "sending")
            claude_adapter.post(self.run_dir, {"delivery_id": claim.delivery_id, "session_id": self.session,
                                               "body": message.body})
        sweeper.join(timeout=5)
        self.assertNotEqual(self.status(message).status, "failed")
        wait_for(lambda: worker.deliver_messages(self.db, self.root) or self.status(message).status == "delivered")

        crashed = self.queue("second")
        with claude_adapter.mailbox_lock(self.run_dir):
            self.db.begin_worker_message_delivery(crashed.id, KEY, "implement", 1, self.session)
        # The handoff died holding the lock (the OS released it) before any write.
        worker.deliver_messages(self.db, self.root)
        row = self.status(crashed)
        self.assertEqual(row.status, "failed")
        self.assertIn("interrupted", row.evidence)
        self.assertNotIn(row.delivery_id, self.written())

    def test_concurrent_handoff_and_close_never_lose_or_duplicate_a_message(self):
        runner = self.start()
        messages = [self.queue(f"message {n}") for n in range(12)]
        handing = threading.Thread(target=lambda: [worker.deliver_messages(self.open_store(), self.root)
                                                   for _ in range(30)])
        handing.start()
        time.sleep(0.05)
        self.gate.touch()  # the turn ends and the runner closes its intake while handoffs continue
        handing.join(timeout=30)
        self.assertEqual(runner.wait(timeout=60), 0)
        worker.deliver_messages(self.db, self.root)
        written = self.written()
        for message in messages:
            row = self.status(message)
            with self.subTest(row.body):
                self.assertIn(row.status, ("delivered", "failed"))
                self.assertLessEqual(written.count(row.delivery_id or "-"), 1)
                self.assertEqual(row.status == "delivered", row.delivery_id in written)

    def test_a_runner_crash_after_writing_leaves_the_message_unconfirmed_and_never_resends_it(self):
        runner = self.start(noecho=True)
        message = self.queue()
        worker.deliver_messages(self.db, self.root)
        claim = self.status(message)
        wait_for(lambda: (self.run_dir / claude_adapter.MAILBOX / "taken" / f"{claim.delivery_id}.json").exists())
        os.kill(runner.pid, signal.SIGKILL)
        runner.wait(timeout=5)
        for _ in range(3):
            worker.deliver_messages(self.open_store(), self.root)
        self.assertEqual(self.status(message).status, "sending")
        with self.assertRaises(ValueError):
            self.db.begin_worker_message_delivery(message.id, KEY, "implement", 1, self.session)
        later = self.queue("after the crash")
        worker.deliver_messages(self.db, self.root)
        self.assertIn("session ended", self.status(later).evidence)
        self.assertEqual(os.listdir(self.run_dir / claude_adapter.MAILBOX / "taken"), [f"{claim.delivery_id}.json"])

    def test_an_untaken_message_of_a_dead_runner_is_revoked_and_failed(self):
        launch = {"argv": [], "cwd": "", "session_id": self.session, "env": {}, "stdin_prompt": "p"}
        (self.run_dir / "command.json").write_text(json.dumps(launch))
        for name in claude_adapter.BOXES:
            (self.run_dir / claude_adapter.MAILBOX / name).mkdir(parents=True)
        (self.run_dir / claude_adapter.MAILBOX / "open").touch()
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.save_record(gone.pid, "an earlier start")
        message = self.queue()
        claim = self.db.begin_worker_message_delivery(message.id, KEY, "implement", 1, self.session)
        with claude_adapter.mailbox_lock(self.run_dir):
            claude_adapter.post(self.run_dir, {"delivery_id": claim.delivery_id, "session_id": self.session,
                                               "body": message.body})
        worker.deliver_messages(self.db, self.root)
        row = self.status(message)
        self.assertEqual(row.status, "failed")
        self.assertIn("closed its intake", row.evidence)
        self.assertTrue((self.run_dir / claude_adapter.MAILBOX / "rejected" / f"{claim.delivery_id}.json").exists())


if __name__ == "__main__":
    unittest.main()
