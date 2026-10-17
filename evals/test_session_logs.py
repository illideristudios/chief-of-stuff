"""Limits and worker activity share account discovery and a single transcript decode."""
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
import workers_page


class SharedLogsTest(unittest.TestCase):
    def test_both_consumers_share_the_configured_account_and_cached_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            tree = home / "tree"
            stamp = datetime.now(timezone.utc)
            project = re.sub(r"[^A-Za-z0-9]", "-", str(tree.resolve()))
            for account, base in (("", home / "config"), ("secondary", home / "profiles/secondary")):
                with self.subTest(account=account):
                    path = base / "projects" / project / "session.jsonl"
                    path.parent.mkdir(parents=True)
                    event = {"type": "rate_limit_event", "timestamp": stamp.isoformat(), "rate_limit_info": {
                        "rateLimitType": "weekly", "utilization": .4,
                        "resetsAt": (stamp + timedelta(days=2)).timestamp()},
                        "message": {"id": "message-1", "usage": {"input_tokens": 123}}}
                    path.write_text(json.dumps(event) + "\n{partial")
                    with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home / "config"),
                                                 "CLAUDE_AS_PROFILES_DIR": str(home / "profiles")}), \
                         patch("json.loads", wraps=json.loads) as decode:
                        self.assertEqual(limits_probe.read("claude", account)[0].used, 40)
                        calls = decode.call_count
                        self.assertEqual(workers_page.activity(tree, account=account), (stamp, 123))
                        self.assertEqual(decode.call_count, calls, "activity must reuse decoded transcript records")
                        event["message"]["usage"]["input_tokens"] = 456
                        path.write_text(json.dumps(event) + "\n")
                        self.assertEqual(workers_page.activity(tree, account=account), (stamp, 456))
                        calls = decode.call_count
                        self.assertEqual(limits_probe.read("claude", account)[0].used, 40)
                        self.assertEqual(decode.call_count, calls, "limits must also reuse activity's refreshed records")
