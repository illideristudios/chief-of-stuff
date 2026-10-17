"""Limits and worker activity read one account's transcripts through one gateway, decoding only what each needs."""
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import limits_probe
import session_logs
import workers_page

STAMP = datetime.now(timezone.utc)


def limit_event(used=.4):
    return {"type": "rate_limit_event", "timestamp": STAMP.isoformat(), "rate_limit_info": {
        "rateLimitType": "weekly", "utilization": used, "resetsAt": (STAMP + timedelta(days=2)).timestamp()}}


class SharedLogsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.tree = self.home / "tree"
        self.project = re.sub(r"[^A-Za-z0-9]", "-", str(self.tree.resolve()))
        env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / "config"),
                                      "CLAUDE_AS_PROFILES_DIR": str(self.home / "profiles")})
        env.start()
        self.addCleanup(env.stop)
        session_logs._scans.clear()

    def log(self, lines, account=""):
        base = self.home / "config" if not account else self.home / "profiles" / account
        path = base / "projects" / self.project / "session.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        return path

    def test_both_consumers_read_the_configured_account(self):
        for account in ("", "secondary"):
            with self.subTest(account=account):
                message = {"id": "message-1", "usage": {"input_tokens": 123}}
                self.log([json.dumps(dict(limit_event(), message=message)), "{partial"], account)
                self.assertEqual(limits_probe.read("claude", account)[0].used, 40)
                self.assertEqual(workers_page.activity(self.tree, account=account), (STAMP, 123))

    def test_limits_decode_only_lines_that_can_hold_a_limit_event(self):
        plain = [json.dumps({"type": "user", "timestamp": STAMP.isoformat(), "message": {"content": "x" * 50}})] * 200
        self.log(plain + [json.dumps(limit_event())])
        with patch("session_logs.json.loads", wraps=json.loads) as decoded:
            self.assertEqual(limits_probe.read("claude", "")[0].used, 40)
        self.assertEqual(decoded.call_count, 1)

    def test_decoding_does_not_hold_the_cache_lock(self):
        self.log([json.dumps(limit_event())])
        held, real = [], json.loads

        def decode(line):
            free = session_logs._scan_lock.acquire(blocking=False)
            if free:
                session_logs._scan_lock.release()
            held.append(not free)
            return real(line)

        with patch("session_logs.json.loads", side_effect=decode):
            limits_probe.read("claude", "")
        self.assertEqual(held, [False])

    def test_cache_forgets_deleted_transcripts_and_keeps_only_compact_records(self):
        path = self.log([json.dumps(dict(limit_event(), message={"id": "m", "usage": {}, "content": "secret"}))])
        limits_probe.read("claude", "")
        workers_page.activity(self.tree)
        self.assertTrue(all(key[1] == path for key in session_logs._scans))
        self.assertNotIn("secret", repr(session_logs._scans))
        path.unlink()
        limits_probe.read("claude", "")
        workers_page.activity(self.tree)
        self.assertEqual(session_logs._scans, {})
