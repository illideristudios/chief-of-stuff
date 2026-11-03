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
import board_sources
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
        line = json.dumps(dict(limit_event(), message={"id": "m", "usage": {}, "content": "secret"}))
        path = self.log([line])
        limits_probe.read("claude", "")
        workers_page.activity(self.tree)
        self.assertTrue(all(key[1] == path for key in session_logs._scans))
        self.assertNotIn("secret", repr(session_logs._scans))
        for name, read in (("limits", lambda: limits_probe.read("claude", "")),
                           ("activity", lambda: workers_page.activity(self.tree))):
            with self.subTest(reader=name):
                session_logs._scans.clear()
                path.write_text(line + "\n")
                read()
                self.assertTrue(session_logs._scans)
                path.unlink()
                read()
                self.assertEqual(session_logs._scans, {})

    def test_a_workers_render_drops_activity_for_trees_that_are_not_running(self):
        line = json.dumps({"timestamp": STAMP.isoformat(), "message": {"id": "m", "usage": {"input_tokens": 1}}})
        running = self.log([line])
        ended = self.home / "ended"
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(ended.resolve()))
        ended_log = self.home / "config" / "projects" / slug / "session.jsonl"
        ended_log.parent.mkdir(parents=True)
        ended_log.write_text(line + "\n")
        workers_page.activity(self.tree)
        workers_page.activity(ended)
        worker = board_sources.Worker("impl", "one-shot", "claude", "opus", "Cut", self.tree.name, None, None)
        sources = board_sources.Sources({}, {}, {}, (worker,), {})
        workers_page.render("# Tracker\n\n## Log\n", sources, STAMP, self.tree.parent, None)
        self.assertEqual([k for k in session_logs._scans if k[0] == "activity"], [("activity", running)])
        workers_page.render("# Tracker\n\n## Log\n", board_sources.EMPTY, STAMP, self.tree.parent, None)
        self.assertFalse(any(k[0] == "activity" for k in session_logs._scans))
