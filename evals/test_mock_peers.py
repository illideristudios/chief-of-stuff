"""Unit tests for the mock peers MCP server, the runner's peer wiring, and the coordination graders."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import run

EVALS = Path(__file__).parent
FIXTURES = EVALS / "fixtures"
MOCK = EVALS / "mock_peers.py"
CT = ZoneInfo("America/Chicago")

SESSIONS = [
    {"ref": "a1b2c3", "name": "4821-audit", "state": "busy", "started_hours_ago": 2},
    {"ref": "d4e5f6", "name": "7719-notes", "state": "idle", "started_hours_ago": 1},
    {"ref": "9f2a41", "name": "wt-audit", "state": "busy", "started_minutes_ago": 3},
]


def rpc(id_: int | None, method: str, params: dict | None = None) -> str:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def call(id_: int, name: str, **args) -> str:
    return rpc(id_, "tools/call", {"name": name, "arguments": args})


class MockPeersServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.sessions = self.tmp / "sessions.json"
        self.sessions.write_text(json.dumps(SESSIONS))
        self.log = self.tmp / "calls.jsonl"

    def converse(self, *lines: str) -> dict[int, dict]:
        proc = subprocess.run(
            [sys.executable, str(MOCK), "--sessions", str(self.sessions), "--log", str(self.log), "--tz", "America/Chicago"],
            input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return {r["id"]: r for r in (json.loads(l) for l in proc.stdout.splitlines() if l.strip())}

    def text(self, reply: dict) -> str:
        return reply["result"]["content"][0]["text"]

    def test_tools_and_listing_shape(self) -> None:
        r = self.converse(rpc(1, "initialize", {"protocolVersion": "2025-06-18"}), rpc(2, "tools/list"), call(3, "list_sessions"))
        self.assertEqual(r[1]["result"]["serverInfo"]["name"], "mock-peers")
        self.assertEqual([t["name"] for t in r[2]["result"]["tools"]], ["list_sessions", "send"])
        rows = self.text(r[3]).splitlines()
        self.assertIn("4821-audit [a1b2c3]  ·  interactive  ·  busy  ·  started 2h ago", rows)
        self.assertIn("7719-notes [d4e5f6]  ·  interactive  ·  idle  ·  started 1h ago", rows)
        # `registration-fills-the-ref` sets minutes: a session that came up moments ago is the whole premise,
        # and the mock had been listing it as an hour old.
        self.assertIn("wt-audit [9f2a41]  ·  interactive  ·  busy  ·  started 3m ago", rows)
        entries = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual(entries[0]["tool"], "list_sessions")
        self.assertIn("at", entries[0])

    def test_send_by_name_and_by_name_with_ref(self) -> None:
        r = self.converse(call(1, "send", to="4821-audit", text="Reply in 3 lines: task; waiting; free"), call(2, "send", to="7719-notes [d4e5f6]", text="hi"))
        self.assertEqual(json.loads(self.text(r[1]))["to"], "4821-audit [a1b2c3]")
        self.assertEqual(json.loads(self.text(r[2]))["to"], "7719-notes [d4e5f6]")
        entries = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual([(e["tool"], e["to_ref"], e["ok"]) for e in entries], [("send", "a1b2c3", True), ("send", "d4e5f6", True)])
        self.assertEqual(entries[0]["text"], "Reply in 3 lines: task; waiting; free")

    def test_send_to_unknown_name_fails_and_is_logged(self) -> None:
        r = self.converse(call(1, "send", to="old-name", text="x"), call(2, "send", to="a1b2c3", text="bare ref"))
        self.assertTrue(r[1]["result"].get("isError"))
        self.assertIn("no agent reachable", self.text(r[1]))
        self.assertTrue(r[2]["result"].get("isError"))
        entries = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual([(e["to_ref"], e["ok"]) for e in entries], [(None, False), (None, False)])

    def test_sessions_file_reread_per_call(self) -> None:
        # A rename between calls: the old name stops resolving, the ref still does.
        renamed = [dict(SESSIONS[0], name="4821-audit-v2"), SESSIONS[1]]
        proc = subprocess.Popen(
            [sys.executable, str(MOCK), "--sessions", str(self.sessions), "--log", str(self.log), "--tz", "America/Chicago"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        proc.stdin.write(call(1, "send", to="4821-audit", text="a") + "\n"); proc.stdin.flush()
        first = json.loads(proc.stdout.readline())
        self.sessions.write_text(json.dumps(renamed))
        proc.stdin.write(call(2, "send", to="4821-audit", text="b") + "\n" + call(3, "send", to="4821-audit-v2 [a1b2c3]", text="c") + "\n")
        proc.stdin.close()
        rest = [json.loads(l) for l in proc.stdout.read().splitlines() if l.strip()]
        proc.wait(timeout=10)
        self.assertNotIn("isError", first["result"])
        self.assertTrue(rest[0]["result"].get("isError"))
        self.assertNotIn("isError", rest[1]["result"])


class RunnerPeerWiringTest(unittest.TestCase):
    def test_mock_tool_names_are_not_the_real_peer_tools(self) -> None:
        for name in run.PEER_MOCK_TOOLS:
            self.assertTrue(name.startswith("mcp__peers__"))
            for real in run.PEER_TOOLS:
                self.assertNotIn(real, name)

    def test_peers_config_and_allowlist(self) -> None:
        cfg = run.peers_mcp_config(Path("/r/sessions.json"), Path("/r/calls.jsonl"), "America/Chicago")
        self.assertIn(str(run.MOCK_PEERS.resolve()), cfg["mcpServers"]["peers"]["args"])
        cmd = run.claude_options({}, mcp_config=cfg)
        allowed = cmd[cmd.index("--allowedTools") + 1 :]
        for name in run.PEER_MOCK_TOOLS:
            self.assertIn(name, allowed)
        self.assertNotIn(run.PEER_MOCK_TOOLS[0], run.claude_options({}))

    def test_check_arm_requires_peers_when_needed(self) -> None:
        init = {"tools": [], "agents": [], "mcp_servers": []}
        ok, detail = run.check_arm(init, "baseline", needs_peers=True)
        self.assertFalse(ok)
        self.assertIn("peers", detail)
        init["mcp_servers"].append({"name": "peers", "status": "connected"})
        self.assertTrue(run.check_arm(init, "baseline", needs_peers=True)[0])

    def test_peers_are_rewritten_before_each_adapter_call(self) -> None:
        from unittest import mock
        seen = []
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "first.json").write_text("first")
            (base / "second.json").write_text("second")
            def call(request, **kwargs):
                seen.append((base / "peers-sessions.json").read_text())
                return run.Reply("done", None, "session", 0, 1, events=[{"type": "result"}])
            with mock.patch.object(run.claude_adapter, "call", side_effect=call):
                run.run_turns({"turns": [{"prompt": "one", "peers": "first.json"},
                                          {"prompt": "two", "peers": "second.json"}]},
                    "baseline", "sonnet", base, base, {}, None, base / "calls", "America/Chicago",
                    case_root=base, ctx={})
        self.assertEqual(seen, ["first", "second"])


TRACKER_BEFORE = """# Tracker

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Audit | 4821-audit | running 09:30 | 09:00 |  | x |

## Sessions

| ref | name | doing | waiting on | free at | constraints | last reply |
|---|---|---|---|---|---|---|
| a1b2c3 | 4821-audit | audit | none | 11:00 | none | 09:30 |

## Log

- 09:00 opened
- 09:30 audit dispatched
"""


class CoordinationGraderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.before = Path(tempfile.mkdtemp())
        self.after = Path(tempfile.mkdtemp())
        for root in (self.before, self.after):
            (root / "daily").mkdir()
            (root / "daily" / "t.md").write_text(TRACKER_BEFORE)
        stream = run.load_stream(FIXTURES / "stream-denied-commit.jsonl")
        self.rec = run.RunRecord(
            stream=stream,
            t_start=datetime(2026, 9, 16, 16, 0, tzinfo=CT),
            t_end=datetime(2026, 9, 16, 16, 5, tzinfo=CT),
            tz="America/Chicago",
            fixture_dir=self.after,
            mock_calls=[
                {"tool": "list_sessions", "at": "2026-09-16T16:01:00-05:00"},
                {"tool": "send", "to": "4821-audit", "to_ref": "a1b2c3", "text": "Reply in 3 lines: current task and state; waiting on what; when free.", "ok": True, "at": "2026-09-16T16:02:00-05:00"},
                {"tool": "send", "to": "gone", "to_ref": None, "text": "x", "ok": False, "at": "2026-09-16T16:02:30-05:00"},
            ],
            before_dir=self.before,
        )

    def grade(self, g: dict) -> tuple[bool, str]:
        return run.grade(g, self.rec)

    def test_peer_calls(self) -> None:
        self.assertEqual(len(self.rec.peer_calls), 3)
        self.assertTrue(self.grade({"type": "peer_calls", "tool": "list_sessions", "min": 1})[0])
        self.assertTrue(self.grade({"type": "peer_calls", "tool": "send", "to_ref": "a1b2c3", "ok": True, "text_match": "(?i)3 lines", "min": 1})[0])
        self.assertTrue(self.grade({"type": "peer_calls", "tool": "send", "to_ref": "a1b2c3", "text_not_match": "(?i)human-only", "min": 1})[0])
        ok, detail = self.grade({"type": "peer_calls", "tool": "send", "to_ref": "a1b2c3", "text_not_match": "(?i)3 lines", "min": 1})
        self.assertFalse(ok)
        ok, detail = self.grade({"type": "peer_calls", "tool": "send", "ok": False, "max": 0})
        self.assertFalse(ok)
        self.assertIn("1 call", detail)
        self.assertTrue(self.grade({"type": "peer_calls", "tool": "send", "text_match": "(?m)^Tracker:", "max": 0})[0])

    def test_timestamp_tolerance_leading(self) -> None:
        g = {"type": "timestamp_tolerance", "path": "daily/t.md", "section": "## Log"}
        (self.after / "daily" / "t.md").write_text(TRACKER_BEFORE + "- 16:03 polled 4821-audit (it reported done 16:58)\n")
        self.assertTrue(self.grade(g)[0], self.grade(g)[1])
        (self.after / "daily" / "t.md").write_text(TRACKER_BEFORE + "- 16:58 polled\n")
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("16:58", detail)
        (self.after / "daily" / "t.md").write_text(TRACKER_BEFORE + "- polled without a time\n")
        self.assertFalse(self.grade(g)[0])
        (self.after / "daily" / "t.md").write_text(TRACKER_BEFORE)
        self.assertTrue(self.grade(g)[0])  # nothing added: nothing to check

    def test_timestamp_tolerance_column(self) -> None:
        g = {"type": "timestamp_tolerance", "path": "daily/t.md", "section": "## Sessions", "column": 7}
        updated = TRACKER_BEFORE.replace("| a1b2c3 | 4821-audit | audit | none | 11:00 | none | 09:30 |", "| a1b2c3 | 4821-audit | audit, reported done 16:58 | none | 11:00 | none | 16:04 |")
        (self.after / "daily" / "t.md").write_text(updated)
        self.assertTrue(self.grade(g)[0], self.grade(g)[1])
        bad = updated.replace("| 16:04 |", "| 16:58 |")
        (self.after / "daily" / "t.md").write_text(bad)
        self.assertFalse(self.grade(g)[0])
        blank = updated.replace("| 16:04 |", "|  |")
        (self.after / "daily" / "t.md").write_text(blank)
        self.assertTrue(self.grade(g)[0])  # a blank cell is not a wrong time

    def test_reply_lines(self) -> None:
        self.rec.stream.last_text = "4821-audit blocked on the agent redeploy, allow?"
        self.assertTrue(self.grade({"type": "reply_lines", "max": 2})[0])
        self.rec.stream.last_text = "a\n\nb\nc\n"
        ok, detail = self.grade({"type": "reply_lines", "max": 2})
        self.assertFalse(ok)
        self.assertIn("3", detail)

    def test_file_matches_count(self) -> None:
        g = {"type": "file_matches", "path": "daily/t.md", "pattern": "(?m)^\\| a1b2c3 \\|", "count": 1}
        self.assertTrue(self.grade(g)[0])
        (self.after / "daily" / "t.md").write_text(TRACKER_BEFORE.replace("## Log", "| a1b2c3 | 4821-audit-v2 | audit | none | 11:00 | none | 16:04 |\n\n## Log"))
        ok, detail = self.grade(g)
        self.assertFalse(ok)
        self.assertIn("2", detail)


if __name__ == "__main__":
    unittest.main()
