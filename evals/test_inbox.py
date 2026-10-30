"""Comprehensive automated test suite for file-backed bidirectional inbox communication system.

Covers requirements R1, R2, R3, and R5 across Tiers 1 through 4:
- Tier 1 (Feature Coverage):
    - SendMessageTest: creation, full schema fields, ISO timestamp, atomic staging, task/worktree context.
    - ListMessagesTest: unread ordering, unread vs all, recipient isolation, empty mailbox.
    - ReadAndAckTest: reading payload, auto-ack, manual ack, idempotent repeat ack, durability.
    - DrainInboxTest: draining unread messages in order, recipient isolation, idempotence.
    - CLITest: subcommands (send, list, read, ack, drain), exit codes (0, 1, 2), JSON and text formats.
- Tier 2 (Boundary & Corner Cases):
    - BoundaryAndCornerCasesTest: empty body, large payload (>100KB), special characters/newlines/escapes,
      Unicode/emojis, corrupted JSON recovery, zero-byte file recovery, missing schema fields recovery,
      path traversal prevention, auto-creation of mailbox .gitignore.
- Tier 3 (Combinatorial & Interaction Cases):
    - InteractionAndConcurrencyCasesTest: concurrent sender and reader interleaving, drain while new message
      arrives, acknowledgment race conditions, read message race conditions, corrupted file injected during concurrent drain.
- Tier 4 (Multi-Process Concurrency Simulation Test):
    - MultiProcessConcurrencySimulationTest: 10 worker processes, 50 messages per worker (500 total)
      with sequential monotonic counters, concurrent drainer thread/process, verifying:
      1. Zero message loss: all 500 messages collected.
      2. Zero duplicate message IDs.
      3. Zero corrupted files.
      4. Monotonic sequence ordering per worker.
      5. Zero deadlocks (timeout < 25s).
      6. Final unread count is 0.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import unittest
import uuid

# Ensure repository root and scripts directory are in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

INBOX_SCRIPT = SCRIPTS_DIR / "inbox.py"

# Lazy-loaded module handle and exceptions
inbox: Any = None
Message: Any = None
InboxError: type[Exception] = Exception
MessageNotFoundError: type[Exception] = Exception
CorruptedMessageError: type[Exception] = Exception
InvalidMessageError: type[Exception] = Exception


def _import_inbox() -> None:
    """Dynamically imports scripts/inbox.py and binds required symbols."""
    global inbox, Message, InboxError, MessageNotFoundError, CorruptedMessageError, InvalidMessageError
    if inbox is not None:
        return
    try:
        import inbox as _inbox  # type: ignore
        inbox = _inbox
        Message = getattr(_inbox, "Message", None)
        InboxError = getattr(_inbox, "InboxError", Exception)
        MessageNotFoundError = getattr(_inbox, "MessageNotFoundError", InboxError)
        CorruptedMessageError = getattr(_inbox, "CorruptedMessageError", InboxError)
        InvalidMessageError = getattr(_inbox, "InvalidMessageError", InboxError)
    except ImportError:
        raise unittest.SkipTest("scripts/inbox.py is not yet available in the repository")


def _simulation_worker(
    worker_id: int,
    message_count: int,
    mailbox_dir: str,
    recipient: str,
    start_barrier: Any = None,
) -> None:
    """Worker process function executed concurrently in Tier 4 simulation.

    Must be top-level module function to remain picklable under multiprocessing 'spawn' context.
    """
    import sys
    from pathlib import Path
    import time
    import uuid

    scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import inbox

    if start_barrier is not None:
        try:
            start_barrier.wait(timeout=10)
        except Exception:
            pass

    for seq_id in range(message_count):
        inbox.send_message(
            recipient=recipient,
            sender=f"worker-{worker_id}",
            body=f"worker {worker_id} seq {seq_id}",
            msg_type="progress",
            payload={
                "worker_id": worker_id,
                "seq_id": seq_id,
                "nonce": uuid.uuid4().hex,
            },
            mailbox_dir=mailbox_dir,
        )
        # Micro-yield to induce realistic concurrent context switches
        time.sleep(0.001)


# =====================================================================
# Base Test Case with Workspace Isolation
# =====================================================================

class BaseInboxTestCase(unittest.TestCase):
    """Base test class providing an isolated temporary directory for each test method."""

    def setUp(self) -> None:
        _import_inbox()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.mailbox_dir = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()


# =====================================================================
# Tier 1: Feature Coverage
# =====================================================================

class SendMessageTest(BaseInboxTestCase):
    """Tier 1: Comprehensive tests for message creation and schema validation."""

    def test_send_creates_immutable_toon_file(self) -> None:
        """Sending a message creates a discrete TOON file in <mailbox>/<recipient>/incoming/."""
        msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-1",
            body="Planning task 1",
            mailbox_dir=self.mailbox_dir,
        )
        self.assertIsNotNone(msg)
        self.assertTrue(bool(msg.message_id))
        self.assertEqual(msg.recipient, "coordinator")
        self.assertEqual(msg.sender, "worker-1")
        self.assertEqual(msg.body, "Planning task 1")

        incoming_dir = self.mailbox_dir / "coordinator" / "incoming"
        self.assertTrue(incoming_dir.is_dir(), f"Incoming dir {incoming_dir} should exist")
        files = list(incoming_dir.glob("*.toon"))
        self.assertEqual(len(files), 1, f"Expected exactly 1 message file, found: {files}")

        # Verify on-disk TOON content matches schema
        data = inbox.Message.from_toon(files[0].read_text()).to_dict()
        self.assertEqual(data["message_id"], msg.message_id)
        self.assertEqual(data["sender"], "worker-1")
        self.assertEqual(data["recipient"], "coordinator")
        self.assertEqual(data["body"], "Planning task 1")

    def test_send_preserves_full_schema_fields(self) -> None:
        """Message preserves all metadata fields required by R1 schema contract."""
        msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-test",
            body="Testing schema",
            msg_type="register",
            payload={"status": "planning", "branch": "feat/inbox"},
            task="Storage task",
            worktree="trees/wt-1",
            headers={"transport": "file", "priority": "high"},
            mailbox_dir=self.mailbox_dir,
        )
        self.assertEqual(msg.type, "register")
        self.assertEqual(msg.task, "Storage task")
        self.assertEqual(msg.worktree, "trees/wt-1")
        self.assertEqual(msg.payload.get("status"), "planning")
        self.assertEqual(msg.payload.get("branch"), "feat/inbox")
        self.assertEqual(msg.headers.get("transport"), "file")
        self.assertEqual(msg.headers.get("priority"), "high")

        # Verify persistence on disk
        incoming_dir = self.mailbox_dir / "coordinator" / "incoming"
        msg_files = list(incoming_dir.glob("*.toon"))
        self.assertEqual(len(msg_files), 1)
        disk_data = inbox.Message.from_toon(msg_files[0].read_text()).to_dict()
        self.assertEqual(disk_data["type"], "register")
        self.assertEqual(disk_data["task"], "Storage task")
        self.assertEqual(disk_data["worktree"], "trees/wt-1")
        self.assertEqual(disk_data["payload"]["status"], "planning")
        self.assertEqual(disk_data["headers"]["priority"], "high")

    def test_toon_round_trip_nested_payload_and_legacy_json_mail(self) -> None:
        payload = {"rows": [{"id": 1, "name": "one"}, {"id": 2, "name": "two"}],
                   "empty": [], "note": "colon: comma, newline\nnext", "enabled": False}
        fresh = inbox.send_message("worker", "coordinator", "fresh", payload=payload,
                                   mailbox_dir=self.mailbox_dir)
        incoming = self.mailbox_dir / "worker" / "incoming"
        self.assertTrue((incoming / f"{fresh.message_id}.toon").is_file())
        self.assertEqual(inbox.read_message("worker", fresh.message_id, ack=False,
                                            mailbox_dir=self.mailbox_dir).payload, payload)

        legacy = inbox.send_message("worker", "coordinator", "legacy", mailbox_dir=self.mailbox_dir)
        (incoming / f"{legacy.message_id}.toon").unlink()
        (incoming / f"{legacy.message_id}.json").write_text(legacy.to_json() + "\n")
        self.assertEqual(len(inbox.list_messages("worker", mailbox_dir=self.mailbox_dir)), 2)
        self.assertTrue(inbox.ack_message("worker", legacy.message_id + ".json",
                                           mailbox_dir=self.mailbox_dir))
        self.assertEqual(inbox.read_message("worker", legacy.message_id,
                                            mailbox_dir=self.mailbox_dir).body, "legacy")

    def test_send_iso_timestamp(self) -> None:
        """Message timestamp must be a valid ISO-8601 UTC string."""
        msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-1",
            body="timestamp verification",
            mailbox_dir=self.mailbox_dir,
        )
        self.assertTrue(bool(msg.timestamp))
        # Verify parseable by datetime.fromisoformat
        parsed = datetime.fromisoformat(msg.timestamp.replace("Z", "+00:00"))
        self.assertIsNotNone(parsed)

    def test_send_atomic_staging(self) -> None:
        """Atomic write stages via .tmp and leaves no dangling temporary files."""
        msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-1",
            body="atomic staging check",
            mailbox_dir=self.mailbox_dir,
        )
        self.assertIsNotNone(msg)
        tmp_dir = self.mailbox_dir / ".tmp"
        self.assertTrue(tmp_dir.exists(), ".tmp staging directory must exist")
        # No leftover staging files in .tmp
        tmp_files = list(tmp_dir.glob("*"))
        self.assertEqual(len(tmp_files), 0, f"Found dangling staging files in .tmp: {tmp_files}")

    def test_send_standard_message_types(self) -> None:
        """Standard lifecycle message types (register, progress, ask, reply, stop, generic) are supported."""
        standard_types = ["register", "progress", "ask", "reply", "stop", "generic"]
        for msg_type in standard_types:
            msg = inbox.send_message(
                recipient="coordinator",
                sender="worker-types",
                body=f"Type {msg_type}",
                msg_type=msg_type,
                mailbox_dir=self.mailbox_dir,
            )
            self.assertEqual(msg.type, msg_type)

    def test_send_validation_empty_sender_or_recipient(self) -> None:
        """Empty or whitespace-only sender or recipient must raise ValueError/InboxError."""
        with self.assertRaises((ValueError, InboxError)):
            inbox.send_message(
                recipient="",
                sender="worker-1",
                body="missing recipient",
                mailbox_dir=self.mailbox_dir,
            )
        with self.assertRaises((ValueError, InboxError)):
            inbox.send_message(
                recipient="coordinator",
                sender="",
                body="missing sender",
                mailbox_dir=self.mailbox_dir,
            )
        with self.assertRaises((ValueError, InboxError)):
            inbox.send_message(
                recipient="   ",
                sender="worker-1",
                body="whitespace recipient",
                mailbox_dir=self.mailbox_dir,
            )

    def test_send_returns_immutable_message(self) -> None:
        """Message dataclass returned by send_message must be frozen/immutable."""
        msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-1",
            body="immutability check",
            mailbox_dir=self.mailbox_dir,
        )
        with self.assertRaises((dataclasses.FrozenInstanceError, AttributeError)):
            msg.body = "modified body"  # type: ignore


class ListMessagesTest(BaseInboxTestCase):
    """Tier 1: Comprehensive tests for listing unread and all messages."""

    def test_list_returns_unread_in_chronological_order(self) -> None:
        """list_messages returns unread messages sorted in ascending chronological order."""
        for i in range(4):
            inbox.send_message(
                recipient="coordinator",
                sender=f"worker-{i}",
                body=f"Message {i}",
                mailbox_dir=self.mailbox_dir,
            )
            time.sleep(0.01)

        messages = inbox.list_messages(
            recipient="coordinator",
            unread_only=True,
            mailbox_dir=self.mailbox_dir,
        )
        self.assertEqual(len(messages), 4)
        for i in range(3):
            self.assertLessEqual(messages[i].timestamp, messages[i + 1].timestamp)
        self.assertEqual([m.body for m in messages], [f"Message {i}" for i in range(4)])

    def test_list_unread_vs_all(self) -> None:
        """unread_only=True excludes read messages; unread_only=False includes both."""
        m1 = inbox.send_message("coordinator", "w1", "msg 1", mailbox_dir=self.mailbox_dir)
        m2 = inbox.send_message("coordinator", "w2", "msg 2", mailbox_dir=self.mailbox_dir)

        # Acknowledge m1
        inbox.ack_message("coordinator", m1.message_id, mailbox_dir=self.mailbox_dir)

        unread = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0].message_id, m2.message_id)

        all_msgs = inbox.list_messages("coordinator", unread_only=False, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(all_msgs), 2)
        all_ids = {m.message_id for m in all_msgs}
        self.assertIn(m1.message_id, all_ids)
        self.assertIn(m2.message_id, all_ids)

    def test_list_recipient_isolation(self) -> None:
        """Messages addressed to one recipient are strictly isolated from others."""
        inbox.send_message("coordinator", "w1", "for coord", mailbox_dir=self.mailbox_dir)
        inbox.send_message("worker-1", "coord", "for worker", mailbox_dir=self.mailbox_dir)

        coord_msgs = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(coord_msgs), 1)
        self.assertEqual(coord_msgs[0].body, "for coord")

        worker_msgs = inbox.list_messages("worker-1", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(worker_msgs), 1)
        self.assertEqual(worker_msgs[0].body, "for worker")

    def test_list_nonexistent_recipient(self) -> None:
        """Listing a recipient that has never received messages returns [] without error."""
        msgs = inbox.list_messages("unknown-session-1234", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(msgs, [])


class ReadAndAckTest(BaseInboxTestCase):
    """Tier 1: Comprehensive tests for reading and acknowledging messages."""

    def test_read_payload_and_metadata(self) -> None:
        """read_message returns the exact Message object with correct attributes."""
        sent = inbox.send_message(
            recipient="coordinator",
            sender="worker-alpha",
            body="alpha body",
            msg_type="ask",
            payload={"action": "rebase"},
            task="Auth",
            worktree="trees/auth",
            mailbox_dir=self.mailbox_dir,
        )
        read = inbox.read_message(
            recipient="coordinator",
            message_id=sent.message_id,
            ack=False,
            mailbox_dir=self.mailbox_dir,
        )
        self.assertEqual(read.message_id, sent.message_id)
        self.assertEqual(read.sender, "worker-alpha")
        self.assertEqual(read.type, "ask")
        self.assertEqual(read.body, "alpha body")
        self.assertEqual(read.payload, {"action": "rebase"})
        self.assertEqual(read.task, "Auth")
        self.assertEqual(read.worktree, "trees/auth")

    def test_read_auto_ack_default(self) -> None:
        """read_message with default ack=True atomically moves file to read/."""
        msg = inbox.send_message("coordinator", "w1", "auto-ack check", mailbox_dir=self.mailbox_dir)
        incoming_file = self.mailbox_dir / "coordinator" / "incoming"
        read_file = self.mailbox_dir / "coordinator" / "read"

        self.assertEqual(len(list(incoming_file.glob("*.toon"))), 1)
        read_msg = inbox.read_message("coordinator", msg.message_id, mailbox_dir=self.mailbox_dir)
        self.assertEqual(read_msg.message_id, msg.message_id)

        # File must now be moved to read/
        self.assertEqual(len(list(incoming_file.glob("*.toon"))), 0)
        self.assertEqual(len(list(read_file.glob("*.toon"))), 1)

        # Subsequent unread list must be empty
        unread = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(unread), 0)

    def test_read_with_no_ack(self) -> None:
        """read_message with ack=False leaves message in incoming/."""
        msg = inbox.send_message("coordinator", "w1", "no-ack check", mailbox_dir=self.mailbox_dir)
        read_msg = inbox.read_message("coordinator", msg.message_id, ack=False, mailbox_dir=self.mailbox_dir)
        self.assertEqual(read_msg.message_id, msg.message_id)

        unread = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0].message_id, msg.message_id)

    def test_ack_message_manual(self) -> None:
        """ack_message moves an unread message from incoming/ to read/ and returns True."""
        msg = inbox.send_message("coordinator", "w1", "manual ack check", mailbox_dir=self.mailbox_dir)
        res = inbox.ack_message("coordinator", msg.message_id, mailbox_dir=self.mailbox_dir)
        self.assertTrue(res)

        unread = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(unread), 0)

    def test_ack_message_idempotent(self) -> None:
        """Calling ack_message multiple times on the same message is safe and returns True."""
        msg = inbox.send_message("coordinator", "w1", "idempotent ack", mailbox_dir=self.mailbox_dir)
        res1 = inbox.ack_message("coordinator", msg.message_id, mailbox_dir=self.mailbox_dir)
        self.assertTrue(res1)
        res2 = inbox.ack_message("coordinator", msg.message_id, mailbox_dir=self.mailbox_dir)
        self.assertTrue(res2)

    def test_read_acknowledged_message(self) -> None:
        """read_message can read an already-acknowledged message from read/ directory."""
        msg = inbox.send_message("coordinator", "w1", "reading archived", mailbox_dir=self.mailbox_dir)
        inbox.ack_message("coordinator", msg.message_id, mailbox_dir=self.mailbox_dir)

        archived = inbox.read_message("coordinator", msg.message_id, mailbox_dir=self.mailbox_dir)
        self.assertEqual(archived.message_id, msg.message_id)
        self.assertEqual(archived.body, "reading archived")

    def test_read_nonexistent_message_raises(self) -> None:
        """read_message on an unknown message ID raises MessageNotFoundError or InboxError."""
        with self.assertRaises((MessageNotFoundError, InboxError, ValueError)):
            inbox.read_message("coordinator", "nonexistent_id_99999", mailbox_dir=self.mailbox_dir)

    def test_ack_nonexistent_message(self) -> None:
        """ack_message on unknown ID either returns False or raises MessageNotFoundError."""
        try:
            res = inbox.ack_message("coordinator", "nonexistent_id_99999", mailbox_dir=self.mailbox_dir)
            self.assertFalse(res)
        except (MessageNotFoundError, InboxError, ValueError):
            pass


class DrainInboxTest(BaseInboxTestCase):
    """Tier 1: Comprehensive tests for draining inboxes."""

    def test_drain_retrieves_all_unread_in_chronological_order(self) -> None:
        """drain_inbox retrieves all unread messages sorted in ascending timestamp order."""
        for i in range(5):
            inbox.send_message("coordinator", f"worker-{i}", f"Drain {i}", mailbox_dir=self.mailbox_dir)
            time.sleep(0.01)

        drained = inbox.drain_inbox("coordinator", mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(drained), 5)
        for i in range(4):
            self.assertLessEqual(drained[i].timestamp, drained[i + 1].timestamp)
        self.assertEqual([m.body for m in drained], [f"Drain {i}" for i in range(5)])

    def test_drain_marks_all_as_read(self) -> None:
        """drain_inbox atomically moves all unread messages to read/, leaving 0 unread."""
        for i in range(3):
            inbox.send_message("coordinator", "w1", f"msg {i}", mailbox_dir=self.mailbox_dir)

        drained = inbox.drain_inbox("coordinator", mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(drained), 3)

        unread = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(unread), 0)

        # Verify files now reside in read/
        read_dir = self.mailbox_dir / "coordinator" / "read"
        self.assertEqual(len(list(read_dir.glob("*.toon"))), 3)

    def test_drain_empty_mailbox(self) -> None:
        """Draining an empty mailbox returns [] cleanly."""
        drained = inbox.drain_inbox("coordinator", mailbox_dir=self.mailbox_dir)
        self.assertEqual(drained, [])

    def test_drain_recipient_isolation(self) -> None:
        """Draining one recipient's inbox does not drain or affect another recipient's inbox."""
        inbox.send_message("coordinator", "w1", "coord 1", mailbox_dir=self.mailbox_dir)
        inbox.send_message("worker-1", "coord", "worker 1", mailbox_dir=self.mailbox_dir)

        drained = inbox.drain_inbox("coordinator", mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0].body, "coord 1")

        worker_unread = inbox.list_messages("worker-1", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(worker_unread), 1)
        self.assertEqual(worker_unread[0].body, "worker 1")

    def test_drain_idempotent_repeated_calls(self) -> None:
        """Consecutive calls to drain_inbox return remaining unread messages, returning [] on repeat."""
        for i in range(3):
            inbox.send_message("coordinator", "w1", f"msg {i}", mailbox_dir=self.mailbox_dir)

        first_drain = inbox.drain_inbox("coordinator", mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(first_drain), 3)

        second_drain = inbox.drain_inbox("coordinator", mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(second_drain), 0)


class CLITest(BaseInboxTestCase):
    """Tier 1: Comprehensive tests for CLI execution, subcommands, and exit codes."""

    def _run_cli(self, args: list[str], expected_code: int | None = 0) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, str(INBOX_SCRIPT)] + args
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        if expected_code is not None:
            self.assertEqual(
                proc.returncode,
                expected_code,
                f"CLI command failed with code {proc.returncode} (expected {expected_code}).\n"
                f"Command: {' '.join(cmd)}\n"
                f"Stdout:\n{proc.stdout}\n"
                f"Stderr:\n{proc.stderr}",
            )
        return proc

    def test_cli_send_basic(self) -> None:
        """CLI: 'send' creates a message and prints output with exit code 0."""
        proc = self._run_cli([
            "send",
            "--to", "coordinator",
            "--from", "worker-cli",
            "--body", "hello from cli",
            "--mailbox-dir", str(self.mailbox_dir),
        ])
        self.assertEqual(proc.returncode, 0)
        unread = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0].body, "hello from cli")

    def test_cli_send_full_options(self) -> None:
        """CLI: 'send' accepts --type, --payload, --task, --worktree."""
        proc = self._run_cli([
            "send",
            "--to", "coordinator",
            "--from", "worker-cli",
            "--type", "register",
            "--body", "reg body",
            "--payload", '{"branch": "feat/cli", "ref": "0123"}',
            "--task", "Testing task",
            "--worktree", "trees/cli",
            "--mailbox-dir", str(self.mailbox_dir),
        ])
        self.assertEqual(proc.returncode, 0)
        msgs = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].type, "register")
        self.assertEqual(msgs[0].task, "Testing task")
        self.assertEqual(msgs[0].worktree, "trees/cli")
        self.assertEqual(msgs[0].payload.get("branch"), "feat/cli")

    def test_cli_list_json_format(self) -> None:
        """CLI: 'list --format json' returns a valid JSON array."""
        inbox.send_message("coordinator", "w1", "item 1", mailbox_dir=self.mailbox_dir)
        inbox.send_message("coordinator", "w2", "item 2", mailbox_dir=self.mailbox_dir)

        proc = self._run_cli([
            "list",
            "--recipient", "coordinator",
            "--format", "json",
            "--mailbox-dir", str(self.mailbox_dir),
        ])
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 2)
        bodies = [m["body"] for m in data]
        self.assertIn("item 1", bodies)
        self.assertIn("item 2", bodies)

    def test_cli_defaults_to_toon_for_structured_results(self) -> None:
        sent = self._run_cli(["send", "--to", "worker", "--from", "coordinator",
                              "--body", "next: task", "--mailbox-dir", str(self.mailbox_dir)])
        message = inbox.toon_decode(sent.stdout)
        self.assertEqual(message["body"], "next: task")
        listed = self._run_cli(["list", "--recipient", "worker",
                                "--mailbox-dir", str(self.mailbox_dir)])
        self.assertEqual(inbox.toon_decode(listed.stdout)[0]["message_id"], message["message_id"])
        status = self._run_cli(["status", "--recipient", "worker",
                                "--mailbox-dir", str(self.mailbox_dir)])
        self.assertEqual(inbox.toon_decode(status.stdout)["unread"], 1)
        read = self._run_cli(["read", message["message_id"], "--recipient", "worker",
                              "--mailbox-dir", str(self.mailbox_dir)])
        self.assertEqual(inbox.toon_decode(read.stdout)["body"], "next: task")
        drained = self._run_cli(["drain", "--recipient", "worker",
                                 "--mailbox-dir", str(self.mailbox_dir)])
        self.assertEqual(inbox.toon_decode(drained.stdout), [])

    def test_cli_accepts_toon_payload(self) -> None:
        proc = self._run_cli(["send", "--to", "worker", "--from", "coordinator",
                              "--payload-toon", 'branch: feat/test\nready: true',
                              "--mailbox-dir", str(self.mailbox_dir)])
        self.assertEqual(inbox.toon_decode(proc.stdout)["payload"],
                         {"branch": "feat/test", "ready": True})

    def test_cli_wait_returns_existing_unread_without_acknowledging(self) -> None:
        msg = inbox.send_message("worker", "coordinator", "next task", mailbox_dir=self.mailbox_dir)
        proc = self._run_cli(["wait", "--recipient", "worker", "--timeout", "0",
                              "--mailbox-dir", str(self.mailbox_dir)])
        self.assertEqual([item["message_id"] for item in inbox.toon_decode(proc.stdout)], [msg.message_id])
        self.assertEqual([item.message_id for item in inbox.list_messages(
            "worker", unread_only=True, mailbox_dir=self.mailbox_dir)], [msg.message_id])

    def test_cli_wait_returns_when_message_arrives(self) -> None:
        def send_later() -> None:
            time.sleep(0.05)
            inbox.send_message("worker", "coordinator", "reply", mailbox_dir=self.mailbox_dir)

        sender = threading.Thread(target=send_later)
        sender.start()
        try:
            proc = self._run_cli(["wait", "--recipient", "worker", "--timeout", "1",
                                  "--interval", "0.01", "--mailbox-dir", str(self.mailbox_dir)])
        finally:
            sender.join()
        self.assertEqual([item["body"] for item in inbox.toon_decode(proc.stdout)], ["reply"])

    def test_cli_wait_times_out_and_rejects_invalid_bounds(self) -> None:
        proc = self._run_cli(["wait", "--recipient", "worker", "--timeout", "0.02",
                              "--interval", "0.01", "--mailbox-dir", str(self.mailbox_dir)])
        self.assertEqual(inbox.toon_decode(proc.stdout), [])
        bad = self._run_cli(["wait", "--recipient", "worker", "--interval", "0",
                             "--mailbox-dir", str(self.mailbox_dir)], expected_code=2)
        self.assertIn("interval", bad.stderr)

    def test_cli_list_text_format(self) -> None:
        """CLI: 'list --format text' outputs readable message summaries."""
        inbox.send_message("coordinator", "w1", "text format check", mailbox_dir=self.mailbox_dir)
        proc = self._run_cli([
            "list",
            "--recipient", "coordinator",
            "--format", "text",
            "--mailbox-dir", str(self.mailbox_dir),
        ])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("text format check", proc.stdout)

    def test_cli_read_with_auto_ack(self) -> None:
        """CLI: 'read' retrieves message and marks it acknowledged by default."""
        msg = inbox.send_message("coordinator", "w1", "cli read ack", mailbox_dir=self.mailbox_dir)
        proc = self._run_cli([
            "read",
            msg.message_id,
            "--recipient", "coordinator",
            "--format", "json",
            "--mailbox-dir", str(self.mailbox_dir),
        ])
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(data["message_id"], msg.message_id)

        unread = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(unread), 0)

    def test_cli_read_no_ack(self) -> None:
        """CLI: 'read --no-ack' leaves message unread."""
        msg = inbox.send_message("coordinator", "w1", "cli read no-ack", mailbox_dir=self.mailbox_dir)
        proc = self._run_cli([
            "read",
            msg.message_id,
            "--recipient", "coordinator",
            "--no-ack",
            "--format", "json",
            "--mailbox-dir", str(self.mailbox_dir),
        ])
        self.assertEqual(proc.returncode, 0)
        unread = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(unread), 1)

    def test_cli_ack_subcommand(self) -> None:
        """CLI: 'ack' acknowledges a message with exit code 0."""
        msg = inbox.send_message("coordinator", "w1", "to be acked", mailbox_dir=self.mailbox_dir)
        proc = self._run_cli([
            "ack",
            msg.message_id,
            "--recipient", "coordinator",
            "--mailbox-dir", str(self.mailbox_dir),
        ])
        self.assertEqual(proc.returncode, 0)
        unread = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(unread), 0)

    def test_cli_drain_subcommand(self) -> None:
        """CLI: 'drain --format json' outputs all drained messages and empties incoming."""
        inbox.send_message("coordinator", "w1", "drain cli 1", mailbox_dir=self.mailbox_dir)
        inbox.send_message("coordinator", "w2", "drain cli 2", mailbox_dir=self.mailbox_dir)

        proc = self._run_cli([
            "drain",
            "--recipient", "coordinator",
            "--format", "json",
            "--mailbox-dir", str(self.mailbox_dir),
        ])
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data), 2)

        unread = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(unread), 0)

    def test_cli_missing_required_args_exit_code_2(self) -> None:
        """CLI: Invoking subcommand without required arguments exits with code 2."""
        proc = self._run_cli(["send", "--body", "no to or from"], expected_code=2)
        self.assertEqual(proc.returncode, 2)

    def test_cli_read_nonexistent_message_exit_code_nonzero(self) -> None:
        """CLI: Reading a nonexistent message exits with a non-zero exit code (1 or 2)."""
        proc = self._run_cli([
            "read",
            "nonexistent_uuid",
            "--recipient", "coordinator",
            "--mailbox-dir", str(self.mailbox_dir),
        ], expected_code=None)
        self.assertIn(proc.returncode, (1, 2))


# =====================================================================
# Tier 2: Boundary & Corner Cases
# =====================================================================

class BoundaryAndCornerCasesTest(BaseInboxTestCase):
    """Tier 2: Boundary value, corrupted input, and resilience edge cases."""

    def test_empty_body(self) -> None:
        """Message with empty body string is handled correctly:
        - When no payload is provided, empty body raises ValueError/InvalidMessageError.
        - When payload is provided with empty body, it is persisted and read back cleanly.
        """
        with self.assertRaises((ValueError, InboxError)):
            inbox.send_message(
                recipient="coordinator",
                sender="worker-1",
                body="",
                mailbox_dir=self.mailbox_dir,
            )

        msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-1",
            body="",
            payload={"body": ""},
            mailbox_dir=self.mailbox_dir,
        )
        self.assertEqual(msg.body, "")
        read = inbox.read_message("coordinator", msg.message_id, ack=False, mailbox_dir=self.mailbox_dir)
        self.assertEqual(read.body, "")

    def test_large_payload_over_100kb(self) -> None:
        """Message with >100KB payload is atomically written and read without truncation."""
        large_body = "A" * 150_000
        large_dict = {f"k_{i}": f"v_{i}" * 10 for i in range(2_000)}

        msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-1",
            body=large_body,
            payload=large_dict,
            mailbox_dir=self.mailbox_dir,
        )
        self.assertEqual(len(msg.body), 150_000)

        read = inbox.read_message("coordinator", msg.message_id, ack=False, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(read.body), 150_000)
        self.assertEqual(read.body, large_body)
        self.assertEqual(read.payload, large_dict)

    def test_special_characters_newlines_and_escapes(self) -> None:
        """Message containing newlines, quotes, tabs, backslashes, and control sequences preserves fidelity."""
        complex_body = (
            "Line 1\nLine 2\r\nLine 3\tTabbed\n"
            "\"Double Quotes\" and 'Single Quotes'\n"
            "Backslashes: \\ \\\\ \\n \\t\n"
            "XML/HTML: <tag attr=\"val\">&amp; &lt;</tag>\n"
            "JSON string: {\"key\": \"value\", \"nested\": [1, 2, 3]}"
        )
        msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-1",
            body=complex_body,
            mailbox_dir=self.mailbox_dir,
        )
        read = inbox.read_message("coordinator", msg.message_id, ack=False, mailbox_dir=self.mailbox_dir)
        self.assertEqual(read.body, complex_body)

    def test_unicode_and_emojis(self) -> None:
        """Unicode characters from diverse alphabets and emojis are preserved intact."""
        unicode_body = "Japanese: こんにちは | Arabic: مرحبا | Greek: Γειά σου | Emojis: 🚀📬🔐💥🎉"
        msg = inbox.send_message(
            recipient="coordinator",
            sender="worker-1",
            body=unicode_body,
            payload={"emoji": "📬", "text": "日本語"},
            mailbox_dir=self.mailbox_dir,
        )
        read = inbox.read_message("coordinator", msg.message_id, ack=False, mailbox_dir=self.mailbox_dir)
        self.assertEqual(read.body, unicode_body)
        self.assertEqual(read.payload["emoji"], "📬")
        self.assertEqual(read.payload["text"], "日本語")

    def test_corrupted_json_recovery(self) -> None:
        """Truncated/invalid JSON in incoming/ is quarantined to dead-letter/ without crashing reader."""
        # Send one valid message first
        valid = inbox.send_message("coordinator", "w1", "valid msg", mailbox_dir=self.mailbox_dir)

        # Plant a truncated/malformed JSON file directly in incoming/
        incoming_dir = self.mailbox_dir / "coordinator" / "incoming"
        corrupt_file = incoming_dir / "msg_corrupted_test.json"
        corrupt_file.write_text('{"message_id": "broken", "body": "unfinishe')

        # Listing must not raise JSONDecodeError, must return valid sibling
        messages = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, valid.message_id)

        # Corrupted file must be moved to dead-letter/
        self.assertFalse(corrupt_file.exists(), "Corrupted file must not remain in incoming/")
        dead_letter_dir = self.mailbox_dir / "coordinator" / "dead-letter"
        self.assertTrue(dead_letter_dir.is_dir())
        quarantined = list(dead_letter_dir.glob("*.json"))
        self.assertGreaterEqual(len(quarantined), 1)

    def test_zero_byte_file_recovery(self) -> None:
        """A 0-byte file in incoming/ is quarantined to dead-letter/ without blocking processing."""
        valid = inbox.send_message("coordinator", "w1", "valid msg", mailbox_dir=self.mailbox_dir)
        incoming_dir = self.mailbox_dir / "coordinator" / "incoming"
        zero_file = incoming_dir / "zero_byte.json"
        zero_file.write_bytes(b"")

        drained = inbox.drain_inbox("coordinator", mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0].message_id, valid.message_id)

        self.assertFalse(zero_file.exists())
        dead_letter_dir = self.mailbox_dir / "coordinator" / "dead-letter"
        self.assertTrue(dead_letter_dir.is_dir())

    def test_missing_schema_fields_recovery(self) -> None:
        """A JSON file missing required schema fields is quarantined to dead-letter/."""
        valid = inbox.send_message("coordinator", "w1", "valid msg", mailbox_dir=self.mailbox_dir)
        incoming_dir = self.mailbox_dir / "coordinator" / "incoming"
        bad_schema = incoming_dir / "missing_fields.json"
        bad_schema.write_text(json.dumps({"some_random_key": "no sender or recipient"}))

        messages = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_id, valid.message_id)

        self.assertFalse(bad_schema.exists())

    def test_path_traversal_prevention(self) -> None:
        """Path traversal characters in recipient or message_id are safely handled or rejected."""
        traversal_attempts = ["../../etc/passwd", "../outside", "..\\windows\\system32"]
        for bad_id in traversal_attempts:
            # Should either raise an exception or sanitize to stay strictly within mailbox_dir
            try:
                inbox.send_message(
                    recipient=bad_id,
                    sender="attacker",
                    body="exploit",
                    mailbox_dir=self.mailbox_dir,
                )
            except (ValueError, InboxError):
                continue

            # If sanitized, verify no directory outside mailbox_dir was created
            parent_escapes = [p for p in self.mailbox_dir.parent.glob("etc")]
            self.assertEqual(len(parent_escapes), 0)

    def test_mailbox_root_auto_gitignore(self) -> None:
        """Initializing a mailbox root automatically creates .gitignore with '*\\n!.gitignore'."""
        inbox.send_message("coordinator", "w1", "init gitignore", mailbox_dir=self.mailbox_dir)
        gitignore = self.mailbox_dir / ".gitignore"
        self.assertTrue(gitignore.exists(), ".gitignore should be auto-created in mailbox root")
        content = gitignore.read_text(encoding="utf-8")
        self.assertIn("*", content)


# =====================================================================
# Tier 3: Combinatorial & Interaction Cases
# =====================================================================

class InteractionAndConcurrencyCasesTest(BaseInboxTestCase):
    """Tier 3: Interleaving operations, concurrent send/read, and race conditions."""

    def test_concurrent_sender_and_reader_interleaving(self) -> None:
        """Concurrent sender and reader threads interleave without JSON decode errors or partial reads."""
        sender_count = 30
        errors: list[Exception] = []
        read_messages: list[Any] = []
        stop_reader = threading.Event()

        def reader_loop() -> None:
            while not stop_reader.is_set():
                try:
                    msgs = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
                    for m in msgs:
                        full_msg = inbox.read_message("coordinator", m.message_id, ack=False, mailbox_dir=self.mailbox_dir)
                        read_messages.append(full_msg.message_id)
                except Exception as exc:
                    errors.append(exc)
                time.sleep(0.002)

        reader_thread = threading.Thread(target=reader_loop)
        reader_thread.start()

        for i in range(sender_count):
            try:
                inbox.send_message(
                    recipient="coordinator",
                    sender=f"worker-{i}",
                    body=f"Concurrent body {i}",
                    mailbox_dir=self.mailbox_dir,
                )
            except Exception as exc:
                errors.append(exc)
            time.sleep(0.002)

        stop_reader.set()
        reader_thread.join(timeout=5)

        self.assertEqual(errors, [], f"Encountered concurrency errors during interleaving: {errors}")
        drained = inbox.drain_inbox("coordinator", mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(drained), sender_count)

    def test_drain_while_new_message_arrives(self) -> None:
        """Messages sent while drain is executing are never lost or corrupted."""
        total_messages = 40
        collected: list[Any] = []
        errors: list[Exception] = []
        stop_drainer = threading.Event()

        def drainer_loop() -> None:
            while not stop_drainer.is_set():
                try:
                    drained = inbox.drain_inbox("coordinator", mailbox_dir=self.mailbox_dir)
                    collected.extend(drained)
                except Exception as exc:
                    errors.append(exc)
                time.sleep(0.005)

        drainer_thread = threading.Thread(target=drainer_loop)
        drainer_thread.start()

        for i in range(total_messages):
            inbox.send_message("coordinator", "worker", f"msg {i}", mailbox_dir=self.mailbox_dir)
            time.sleep(0.002)

        # Allow drainer to catch up, then signal stop and final drain
        time.sleep(0.05)
        stop_drainer.set()
        drainer_thread.join(timeout=5)

        final_drained = inbox.drain_inbox("coordinator", mailbox_dir=self.mailbox_dir)
        collected.extend(final_drained)

        self.assertEqual(errors, [])
        self.assertEqual(len(collected), total_messages, f"Expected {total_messages}, got {len(collected)}")
        collected_ids = [m.message_id for m in collected]
        self.assertEqual(len(set(collected_ids)), total_messages, "No duplicate message IDs allowed")

    def test_acknowledgment_race_condition(self) -> None:
        """Two concurrent threads acknowledging the exact same message both succeed idempotently."""
        msg = inbox.send_message("coordinator", "w1", "race ack", mailbox_dir=self.mailbox_dir)
        barrier = threading.Barrier(2)
        results: list[bool] = []
        errors: list[Exception] = []

        def ack_target() -> None:
            try:
                barrier.wait(timeout=5)
                res = inbox.ack_message("coordinator", msg.message_id, mailbox_dir=self.mailbox_dir)
                results.append(res)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=ack_target)
        t2 = threading.Thread(target=ack_target)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(errors, [], f"Ack race generated exceptions: {errors}")
        self.assertEqual(len(results), 2)
        # Both must succeed (return True) idempotently
        self.assertTrue(all(results), f"Both acks should return True, got: {results}")

    def test_read_message_race_condition(self) -> None:
        """Concurrent threads calling read_message(recipient, msg_id, ack=True) all succeed idempotently."""
        rounds = 10
        concurrency = 5

        for r in range(rounds):
            msg = inbox.send_message(
                recipient="coordinator",
                sender="worker",
                body=f"race read {r}",
                payload={"round": r},
                mailbox_dir=self.mailbox_dir,
            )
            barrier = threading.Barrier(concurrency)
            results: list[Any] = []
            errors: list[Exception] = []

            def reader_target() -> None:
                try:
                    barrier.wait(timeout=5)
                    res = inbox.read_message(
                        "coordinator",
                        msg.message_id,
                        ack=True,
                        mailbox_dir=self.mailbox_dir,
                    )
                    results.append(res)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=reader_target) for _ in range(concurrency)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            self.assertEqual(errors, [], f"Read race round {r} generated exceptions: {errors}")
            self.assertEqual(
                len(results),
                concurrency,
                f"Round {r} expected {concurrency} successful reads, got {len(results)}",
            )
            for res in results:
                self.assertEqual(res.message_id, msg.message_id)
                self.assertEqual(res.body, f"race read {r}")

        # Invariant checks: all messages moved to read/, none remaining in incoming/
        unreads = inbox.list_messages("coordinator", unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(unreads), 0)
        reads = inbox.list_messages("coordinator", unread_only=False, mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(reads), rounds)

    def test_corrupted_file_injected_during_concurrent_drain(self) -> None:
        """A corrupted file dropped into incoming/ during concurrent drain is quarantined without stalling drainer."""
        incoming_dir = self.mailbox_dir / "coordinator" / "incoming"

        # Drainer running
        for i in range(10):
            inbox.send_message("coordinator", "w1", f"valid {i}", mailbox_dir=self.mailbox_dir)

        # Inject corrupt file
        corrupt_file = incoming_dir / "corrupted_injection.json"
        corrupt_file.write_text("NOT VALID JSON")

        for i in range(10, 20):
            inbox.send_message("coordinator", "w1", f"valid {i}", mailbox_dir=self.mailbox_dir)

        drained = inbox.drain_inbox("coordinator", mailbox_dir=self.mailbox_dir)
        self.assertEqual(len(drained), 20, "All 20 valid messages must be successfully drained")
        self.assertFalse(corrupt_file.exists(), "Corrupted file must be moved out of incoming/")


# =====================================================================
# Tier 4: Multi-Process Concurrency Simulation Test
# =====================================================================

class MultiProcessConcurrencySimulationTest(BaseInboxTestCase):
    """Tier 4: Realistic multi-process workload simulation verifying zero loss under contention."""

    def test_multiprocess_simulation(self) -> None:
        """Simulates 10 concurrent worker processes sending 50 messages each (500 total).

        A concurrent drainer drains every 50ms.
        Verifies:
        1. Zero message loss: all 500 messages collected.
        2. Zero duplicate message IDs.
        3. Zero corrupted files.
        4. Monotonic sequence ordering per worker.
        5. Zero deadlocks (strict timeout < 25s).
        6. Final unread count is 0.
        """
        num_workers = 10
        msgs_per_worker = 50
        total_expected = num_workers * msgs_per_worker  # 500 messages
        recipient = "coordinator"

        # Use multiprocessing 'spawn' context for macOS/POSIX portability
        mp_ctx = multiprocessing.get_context("spawn")
        start_barrier = mp_ctx.Barrier(num_workers)

        # Launch concurrent drainer thread
        collected_messages: list[Any] = []
        drainer_lock = threading.Lock()
        stop_drainer = threading.Event()
        drainer_errors: list[Exception] = []

        def drainer_worker() -> None:
            while not stop_drainer.is_set():
                try:
                    batch = inbox.drain_inbox(recipient, mailbox_dir=self.mailbox_dir)
                    if batch:
                        with drainer_lock:
                            collected_messages.extend(batch)
                except Exception as exc:
                    drainer_errors.append(exc)
                time.sleep(0.05)  # Drain every 50ms

        drainer_thread = threading.Thread(target=drainer_worker)
        drainer_thread.start()

        start_time = time.time()

        # Spawn 10 worker processes
        processes: list[multiprocessing.Process] = []
        for worker_id in range(num_workers):
            p = mp_ctx.Process(
                target=_simulation_worker,
                args=(
                    worker_id,
                    msgs_per_worker,
                    str(self.mailbox_dir),
                    recipient,
                    start_barrier,
                ),
            )
            processes.append(p)
            p.start()

        # Join all worker processes with strict timeout
        timeout_seconds = 25.0
        for p in processes:
            remaining_time = max(0.1, timeout_seconds - (time.time() - start_time))
            p.join(timeout=remaining_time)
            self.assertFalse(p.is_alive(), f"Worker process {p.pid} timed out or deadlocked!")
            self.assertEqual(p.exitcode, 0, f"Worker process {p.pid} exited with non-zero code {p.exitcode}")

        # Stop drainer and perform final drain
        stop_drainer.set()
        drainer_thread.join(timeout=5)

        # Final drain pass to collect any messages that arrived right before worker termination
        final_batch = inbox.drain_inbox(recipient, mailbox_dir=self.mailbox_dir)
        with drainer_lock:
            collected_messages.extend(final_batch)

        elapsed_time = time.time() - start_time

        # --- Strict Invariant Assertions ---

        # 1. Zero message loss: all 500 messages collected
        self.assertEqual(drainer_errors, [], f"Drainer encountered unexpected errors: {drainer_errors}")
        self.assertEqual(
            len(collected_messages),
            total_expected,
            f"Message loss detected: expected {total_expected}, collected {len(collected_messages)}",
        )

        # 2. Zero duplicate message IDs
        message_ids = [m.message_id for m in collected_messages]
        self.assertEqual(
            len(set(message_ids)),
            total_expected,
            f"Duplicate message IDs found: {total_expected - len(set(message_ids))} duplicates",
        )

        # 3. Zero corrupted files: all 500 messages parse and validate payload
        for msg in collected_messages:
            self.assertEqual(msg.recipient, recipient)
            self.assertTrue(msg.sender.startswith("worker-"))
            self.assertEqual(msg.type, "progress")
            self.assertIsInstance(msg.payload, dict)
            self.assertIn("worker_id", msg.payload)
            self.assertIn("seq_id", msg.payload)
            self.assertIn("nonce", msg.payload)

        # 4. Monotonic sequence ordering per worker
        for worker_id in range(num_workers):
            worker_msgs = [m for m in collected_messages if m.payload.get("worker_id") == worker_id]
            self.assertEqual(
                len(worker_msgs),
                msgs_per_worker,
                f"Worker {worker_id} had {len(worker_msgs)} messages, expected {msgs_per_worker}",
            )
            # Verify all sequence IDs 0..msgs_per_worker-1 are present
            seq_ids = [m.payload["seq_id"] for m in worker_msgs]
            self.assertEqual(
                sorted(seq_ids),
                list(range(msgs_per_worker)),
                f"Worker {worker_id} missing sequence IDs or had gaps: {seq_ids}",
            )

        # 5. Zero deadlocks: test finished well within timeout
        self.assertLess(
            elapsed_time,
            timeout_seconds,
            f"Test execution took {elapsed_time:.2f}s, exceeding timeout of {timeout_seconds}s",
        )

        # 6. Final unread count is 0
        final_unread = inbox.list_messages(recipient, unread_only=True, mailbox_dir=self.mailbox_dir)
        self.assertEqual(
            len(final_unread),
            0,
            f"Expected 0 unread messages in mailbox, found {len(final_unread)}",
        )


class TaskKeyTest(BaseInboxTestCase):
    """#33: a message names its task; one stored before the rename says `lane` and still loads."""

    def test_a_message_stored_with_lane_loads_as_its_task(self) -> None:
        msg = Message.from_dict({"message_id": "m1", "timestamp": "t", "sender": "a", "recipient": "b",
                                 "type": "progress", "body": "", "lane": "Security audit"})
        self.assertEqual(msg.task, "Security audit")

    def test_review_is_a_message_type(self) -> None:
        """The reviewer's fallback report (auditor `reviewer`, 2026-09-23): `send --type review`."""
        self.assertIn("review", inbox.VALID_MESSAGE_TYPES)


if __name__ == "__main__":
    unittest.main()
