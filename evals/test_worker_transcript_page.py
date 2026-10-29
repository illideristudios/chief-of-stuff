"""A worker session page uses the recorded attempt identity and never writes transcript HTML to the pages dir."""
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pages
import task_forge
import workers_page


class WorkerTranscriptPageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pages = self.root / "pages"
        self.pages.mkdir()
        self.trees = self.root / "trees"
        self.tree = self.trees / "issue-9-implement-1"
        self.tree.mkdir(parents=True)
        (self.root / "CLAUDE.md").write_text("## Coordinator\n- User: Robin\n- Daily log dir: `daily/`\n"
            "- Tracker: `daily/<date>-tracker.md`\n- Worktrees: `trees/`\n- Timezone: America/Chicago\n")
        self.sid = str(uuid.uuid4())
        self.item = SimpleNamespace(issue_key="github:example/repo#9", title="Test <task>", forge_state="open")
        self.segment = SimpleNamespace(id=14, issue_key=self.item.issue_key, stage="implement", attempt=1,
            runtime="claude", model="sonnet", ended_at=None, outcome="done", started_at=datetime.now(timezone.utc).isoformat(), runner="worker")
        self.run_dir = self.root / ".chief-of-stuff" / "runs" / "9-implement-1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "command.json").write_text(json.dumps({"session_id": self.sid, "cwd": str(self.tree),
            "env": {"CLAUDE_CONFIG_DIR": str(self.root / "config")}}))
        self.resume = {self.item.issue_key: {"runs": {"implement-1": {"session_id": self.sid,
            "tree": str(self.tree), "run_dir": str(self.run_dir)}}}}
        self.data = task_forge.PageData((self.item,), (self.segment,), (), (), self.resume)
        self.env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "config")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mock_data = patch.object(pages, "_page_data", return_value=self.data)
        self.mock_data.start()
        self.addCleanup(self.mock_data.stop)
        self.server = pages.make_server(self.pages, 0, root=self.root, refresh=lambda *_: None, every=3600)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(self.tree.resolve()))
        self.log = self.root / "config" / "projects" / slug / f"{self.sid}.jsonl"
        self.log.parent.mkdir(parents=True)

    def get(self, path, host=None):
        port = self.server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{port}"})
        response = conn.getresponse()
        body = response.read().decode()
        code = response.status
        headers = dict(response.getheaders())
        conn.close()
        return code, headers, body

    def line(self, text):
        return json.dumps({"type": "assistant", "timestamp": datetime.now(timezone.utc).isoformat(),
                           "message": {"content": [{"type": "text", "text": text}]}}) + "\n"

    def test_engine_session_route_reads_only_its_recorded_session_and_escapes_text(self):
        self.log.write_text(self.line('<script>alert("unsafe")</script>'))
        (self.log.parent / (str(uuid.uuid4()) + ".jsonl")).write_text(self.line("different session"))
        status, headers, body = self.get("/workers/sessions/14")
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn('<script>alert("unsafe")</script>', body)
        self.assertNotIn("different session", body)
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        self.assertEqual(list(self.pages.iterdir()), [])

    def test_second_request_observes_new_events_and_ended_attempt_keeps_transcript(self):
        self.log.write_text(self.line("first"))
        self.assertNotIn('class="session-text">second<', self.get("/workers/sessions/14")[2])
        with self.log.open("a") as f:
            f.write(self.line("second"))
        self.segment.ended_at = datetime.now(timezone.utc).isoformat()
        body = self.get("/workers/sessions/14")[2]
        self.assertIn("first", body)
        self.assertIn("second", body)
        self.assertIn("Ended", body)
        self.assertNotIn("Live", body)

    def test_invalid_tree_or_missing_and_unsupported_transcript_are_explained(self):
        self.assertIn("unavailable", self.get("/workers/sessions/14")[2].lower())
        self.segment.runtime = "codex"
        self.assertIn("not available", self.get("/workers/sessions/14")[2].lower())
        self.segment.runtime = "claude"
        self.resume[self.item.issue_key]["runs"]["implement-1"]["tree"] = str(self.root / "other-workspace" / "tree")
        self.assertIn("unavailable", self.get("/workers/sessions/14")[2].lower())
        self.assertEqual(self.get("/workers/sessions/15")[0], 404)
        self.assertEqual(self.get("/workers/sessions/14", host="evil.example")[0], 403)

    def test_launch_account_is_recorded_not_inferred_from_current_settings(self):
        self.log.write_text(self.line("original account"))
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "different-current-account")}):
            self.assertIn("original account", self.get("/workers/sessions/14")[2])
        (self.run_dir / "command.json").unlink()
        body = self.get("/workers/sessions/14")[2]
        self.assertIn("account used for this attempt could not be verified", body)
        self.assertNotIn("original account", body)

    def test_default_login_transcript_uses_recorded_directory_without_environment_override(self):
        self.log.write_text(self.line("default account"))
        command = json.loads((self.run_dir / "command.json").read_text())
        command["config_dir"] = command["env"].pop("CLAUDE_CONFIG_DIR")
        (self.run_dir / "command.json").write_text(json.dumps(command))
        self.assertIn("default account", self.get("/workers/sessions/14")[2])
        command["env"]["CLAUDE_CONFIG_DIR"] = str(self.root / "wrong")
        (self.run_dir / "command.json").write_text(json.dumps(command))
        self.assertNotIn("default account", self.get("/workers/sessions/14")[2])

    def test_launch_identity_mismatch_is_not_shown(self):
        self.log.write_text(self.line("sensitive"))
        (self.run_dir / "command.json").write_text(json.dumps({"session_id": str(uuid.uuid4()), "cwd": str(self.tree),
            "env": {"CLAUDE_CONFIG_DIR": str(self.root / "config")}}))
        body = self.get("/workers/sessions/14")[2]
        self.assertIn("account used for this attempt could not be verified", body)
        self.assertNotIn("sensitive", body)

    def test_paginated_transcript_reaches_the_newest_page_without_skipping_events(self):
        self.log.write_text("".join(self.line(f"entry-{i:03d}") for i in range(201)))
        first = self.get("/workers/sessions/14")[2]
        self.assertIn("entry-000", first)
        self.assertIn("entry-199", first)
        self.assertNotIn("entry-200", first)
        link = re.search(r'href="(/workers/sessions/14\?cursor=[^"]+)"', first).group(1)
        last = self.get(link)[2]
        self.assertIn("entry-200", last)
        self.assertIn('http-equiv="refresh"', last)
        self.assertEqual(self.get("/workers/sessions/14?cursor=bogus")[0], 400)

    def test_ended_worker_rows_link_to_the_same_attempt(self):
        self.segment.ended_at = datetime.now(timezone.utc).isoformat()
        html = workers_page.render("", workers_page.board_sources.EMPTY, datetime.now(timezone.utc), self.trees,
                                   None, db=self.data)
        self.assertIn('href="/workers/sessions/14"', html)

    def test_worker_rows_link_to_the_specific_attempt(self):
        html = workers_page.render("", workers_page.board_sources.EMPTY, datetime.now(timezone.utc), self.trees,
                                   None, db=self.data)
        self.assertIn('href="/workers/sessions/14"', html)


if __name__ == "__main__":
    unittest.main()
