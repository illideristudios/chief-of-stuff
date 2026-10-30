"""A worker transcript is scoped to its own Claude session and can be read while it is still being written."""
import json
import os
import re
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import session_logs as worker_transcript


class ClaudeTranscriptTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tree = self.root / "worker"
        self.sid = str(uuid.uuid4())
        environment = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "config"),
                                              "CLAUDE_AS_PROFILES_DIR": str(self.root / "profiles")})
        environment.start()
        self.addCleanup(environment.stop)

    def path(self, session=None, account=""):
        base = self.root / "config" if not account else self.root / "profiles" / account
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(self.tree.resolve()))
        path = base / "projects" / slug / f"{session or self.sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def line(role, content):
        return json.dumps({"type": role, "timestamp": datetime.now(timezone.utc).isoformat(),
                           "message": {"role": role, "content": content}}).encode() + b"\n"

    def test_exact_session_and_account_with_text_and_tools(self):
        other = self.path(str(uuid.uuid4()))
        other.write_bytes(self.line("assistant", [{"type": "text", "text": "another worker"}]))
        path = self.path(account="secondary")
        path.write_bytes(self.line("user", "Fix this") + self.line("assistant", [
            {"type": "text", "text": "Checking"},
            {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "app.py"}},
        ]) + self.line("user", [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}]))
        page = worker_transcript.read_claude(self.tree, self.sid, account="secondary")
        self.assertEqual(page.status, "ok")
        self.assertEqual([x["kind"] for x in page.events], ["user", "assistant", "tool_use", "tool_result"])
        self.assertEqual(page.events[0]["text"], "Fix this")
        self.assertEqual(page.events[2]["input"], {"file_path": "app.py"})
        self.assertNotIn("another worker", repr(page.events))
        self.assertEqual(worker_transcript.read_claude(self.tree, self.sid).status, "missing")

    def test_partial_line_is_read_once_only_after_completed(self):
        path = self.path()
        first = self.line("assistant", [{"type": "text", "text": "first"}])
        second = self.line("assistant", [{"type": "text", "text": "second"}])
        path.write_bytes(first + second[:15])
        page = worker_transcript.read_claude(self.tree, self.sid)
        self.assertEqual([e["text"] for e in page.events], ["first"])
        self.assertEqual(page.cursor.offset, len(first))
        with path.open("ab") as out:
            out.write(second[15:])
        next_page = worker_transcript.read_claude(self.tree, self.sid, cursor=page.cursor)
        self.assertEqual([e["text"] for e in next_page.events], ["second"])
        self.assertEqual(worker_transcript.read_claude(self.tree, self.sid, cursor=next_page.cursor).events, ())

    def test_replaced_or_truncated_file_resets_the_cursor(self):
        path = self.path()
        path.write_bytes(self.line("assistant", [{"type": "text", "text": "old, longer content"}]))
        page = worker_transcript.read_claude(self.tree, self.sid)
        replacement = path.with_suffix(".new")
        replacement.write_bytes(self.line("assistant", [{"type": "text", "text": "new"}]))
        replacement.replace(path)
        read = worker_transcript.read_claude(self.tree, self.sid, cursor=page.cursor)
        self.assertTrue(read.reset)
        self.assertEqual([e["text"] for e in read.events], ["new"])

    def test_in_place_rewrite_is_detected_when_size_is_not_smaller(self):
        path = self.path()
        path.write_bytes(self.line("assistant", [{"type": "text", "text": "old"}]))
        page = worker_transcript.read_claude(self.tree, self.sid)
        path.write_bytes(self.line("assistant", [{"type": "text", "text": "different and longer"}]))
        read = worker_transcript.read_claude(self.tree, self.sid, cursor=page.cursor)
        self.assertTrue(read.reset)
        self.assertEqual([e["text"] for e in read.events], ["different and longer"])

    def test_page_limit_does_not_skip_any_complete_record(self):
        path = self.path()
        total = worker_transcript.MAX_PAGE_RECORDS + 1
        path.write_bytes(b"".join(self.line("user", str(i)) for i in range(total)))
        first = worker_transcript.read_claude(self.tree, self.sid)
        second = worker_transcript.read_claude(self.tree, self.sid, cursor=first.cursor)
        self.assertEqual(len(first.events), worker_transcript.MAX_PAGE_RECORDS)
        self.assertEqual([e["text"] for e in second.events], [str(total - 1)])

    def test_invalid_identity_and_bad_cursor_rejected(self):
        with self.assertRaises(ValueError):
            worker_transcript.read_claude(self.tree, "../../private")
        with self.assertRaises(ValueError):
            worker_transcript.read_claude(self.tree, self.sid, account="../private")
        with self.assertRaises(ValueError):
            worker_transcript.Cursor(device=0, inode=0, offset=-1)

    def test_corrupt_record_is_skipped_and_symlink_is_not_followed(self):
        path = self.path()
        path.write_bytes(b"not json\n" + self.line("assistant", [{"type": "text", "text": "valid"}]))
        self.assertEqual([e["text"] for e in worker_transcript.read_claude(self.tree, self.sid).events], ["valid"])
        path.unlink()
        private = self.root / "private"
        private.write_bytes(self.line("user", "not a transcript"))
        path.symlink_to(private)
        self.assertEqual(worker_transcript.read_claude(self.tree, self.sid).status, "unreadable")

    def test_oversized_record_is_explicit_and_does_not_consume_it(self):
        path = self.path()
        path.write_bytes(b"x" * (worker_transcript.MAX_RECORD_BYTES + 1) + b"\n")
        page = worker_transcript.read_claude(self.tree, self.sid)
        self.assertEqual(page.status, "record_too_large")
        self.assertEqual(page.cursor.offset, 0)


if __name__ == "__main__":
    unittest.main()
