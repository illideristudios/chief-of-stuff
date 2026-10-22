"""Unit tests for the mock calendar MCP server and the runner's calendar wiring.

The server is driven over stdio with canned newline-delimited JSON-RPC, the same transport
`claude -p --mcp-config` uses.
"""

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
MOCK = EVALS / "mock_calendar.py"
CT = ZoneInfo("America/Chicago")

EVENTS = {
    "cal-work": [
        {"id": "w1", "summary": "Standup", "start": {"dateTime": "2026-09-16T09:00:00-05:00", "timeZone": "America/Chicago"}, "end": {"dateTime": "2026-09-16T09:15:00-05:00", "timeZone": "America/Chicago"}},
        {"id": "w2", "summary": "Tomorrow sync", "start": {"dateTime": "2026-09-17T10:00:00-05:00"}, "end": {"dateTime": "2026-09-17T11:00:00-05:00"}},
    ],
    "cal-program": [
        {"id": "p1", "summary": "Week marker", "start": {"date": "2026-09-16"}, "end": {"date": "2026-09-17"}, "transparency": "transparent"},
    ],
}


def rpc(id_: int | None, method: str, params: dict | None = None) -> str:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


class MockServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.events = self.tmp / "events.json"
        self.events.write_text(json.dumps(EVENTS))
        self.log = self.tmp / "calls.jsonl"

    def converse(self, *lines: str) -> dict[int, dict]:
        proc = subprocess.run(
            [sys.executable, str(MOCK), "--events", str(self.events), "--log", str(self.log), "--tz", "America/Chicago"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        return {r["id"]: r for r in replies}

    def call(self, id_: int, **arguments: str) -> str:
        return rpc(id_, "tools/call", {"name": "list_events", "arguments": arguments})

    def test_initialize_echoes_protocol_and_declares_tools(self) -> None:
        r = self.converse(rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}))
        self.assertEqual(r[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r[1]["result"]["capabilities"])

    def test_notification_gets_no_reply(self) -> None:
        r = self.converse(rpc(None, "notifications/initialized"), rpc(2, "ping"))
        self.assertEqual(list(r), [2])
        self.assertEqual(r[2]["result"], {})

    def test_tools_list_names_list_events(self) -> None:
        r = self.converse(rpc(1, "tools/list"))
        tools = r[1]["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["list_events"])
        self.assertEqual(tools[0]["inputSchema"]["required"], ["calendarId"])

    def test_list_events_filters_by_window(self) -> None:
        r = self.converse(self.call(1, calendarId="cal-work", timeMin="2026-09-16T00:00:00-05:00", timeMax="2026-09-17T00:00:00-05:00"))
        items = json.loads(r[1]["result"]["content"][0]["text"])["items"]
        self.assertEqual([e["id"] for e in items], ["w1"])

    def test_all_day_event_overlaps_its_day(self) -> None:
        r = self.converse(self.call(1, calendarId="cal-program", timeMin="2026-09-16T12:00:00Z", timeMax="2026-09-16T13:00:00Z"))
        items = json.loads(r[1]["result"]["content"][0]["text"])["items"]
        self.assertEqual([e["id"] for e in items], ["p1"])

    def test_no_window_returns_everything(self) -> None:
        r = self.converse(self.call(1, calendarId="cal-work"))
        items = json.loads(r[1]["result"]["content"][0]["text"])["items"]
        self.assertEqual(len(items), 2)

    def test_unknown_calendar_is_tool_error(self) -> None:
        r = self.converse(self.call(1, calendarId="cal-nope"))
        self.assertTrue(r[1]["result"]["isError"])

    def test_unknown_method_is_rpc_error(self) -> None:
        r = self.converse(rpc(1, "resources/list"))
        self.assertEqual(r[1]["error"]["code"], -32601)

    def test_calls_are_logged(self) -> None:
        self.converse(self.call(1, calendarId="cal-work", timeMin="2026-09-16T00:00:00-05:00"), self.call(2, calendarId="cal-program"))
        logged = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual([c["arguments"]["calendarId"] for c in logged], ["cal-work", "cal-program"])
        self.assertEqual(logged[0]["tool"], "list_events")


class RunnerCalendarTest(unittest.TestCase):
    def test_mcp_config_holds_only_the_mock(self) -> None:
        cfg = run.calendar_mcp_config(Path("/r/events.json"), Path("/r/calls.jsonl"), "America/Chicago")
        self.assertEqual(list(cfg["mcpServers"]), ["calendar"])
        server = cfg["mcpServers"]["calendar"]
        self.assertEqual(server["args"][0], str(MOCK.resolve()))
        self.assertIn("/r/calls.jsonl", server["args"])

    def test_command_uses_given_mcp_config_and_allows_calendar_tool(self) -> None:
        case = run.Case("c", Path("/c"), {"prompt": "x"})
        cfg = run.calendar_mcp_config(Path("/r/events.json"), Path("/r/calls.jsonl"), "America/Chicago")
        cmd = run.claude_options(case.spec, mcp_config=cfg)
        self.assertEqual(json.loads(cmd[cmd.index("--mcp-config") + 1]), cfg)
        allowed = cmd[cmd.index("--allowedTools") + 1 : cmd.index("--disallowedTools")]
        self.assertIn(run.CALENDAR_TOOL, allowed)
        self.assertIn("--strict-mcp-config", cmd)

    def test_command_without_calendar_has_empty_mcp_config(self) -> None:
        cmd = run.claude_options({})
        self.assertEqual(json.loads(cmd[cmd.index("--mcp-config") + 1]), {"mcpServers": {}})
        self.assertNotIn(run.CALENDAR_TOOL, cmd)

    def test_arm_check_requires_connected_calendar(self) -> None:
        init = {"tools": ["Bash"], "agents": ["claude"], "mcp_servers": [{"name": "calendar", "status": "failed"}]}
        ok, detail = run.check_arm(init, "baseline", needs_calendar=True)
        self.assertFalse(ok)
        self.assertIn("calendar", detail)
        init["mcp_servers"] = [{"name": "calendar", "status": "connected"}]
        ok, _ = run.check_arm(init, "baseline", needs_calendar=True)
        self.assertTrue(ok)


class MockCallsGraderTest(unittest.TestCase):
    def rec(self, calls: list[dict]) -> run.RunRecord:
        t = datetime(2026, 9, 16, 16, 0, tzinfo=CT)
        return run.RunRecord(stream=run.Stream(), t_start=t, t_end=t, tz="America/Chicago", fixture_dir=Path("/x"), mock_calls=calls)

    @staticmethod
    def call(cal: str, **window: str) -> dict:
        return {"tool": "list_events", "arguments": {"calendarId": cal, **window}}

    G = {"type": "mock_calls", "calendar_ids": ["cal-work", "cal-program"], "covers": "2026-09-16"}

    def test_both_ids_covering_the_day_pass(self) -> None:
        day = {"timeMin": "2026-09-16T00:00:00-05:00", "timeMax": "2026-09-17T00:00:00-05:00"}
        ok, detail = run.grade(self.G, self.rec([self.call("cal-work", **day), self.call("cal-program", **day)]))
        self.assertTrue(ok, detail)

    def test_missing_id_fails(self) -> None:
        ok, detail = run.grade(self.G, self.rec([self.call("cal-work")]))
        self.assertFalse(ok)
        self.assertIn("cal-program", detail)

    def test_window_not_covering_day_fails(self) -> None:
        narrow = {"timeMin": "2026-09-16T12:00:00-05:00", "timeMax": "2026-09-17T00:00:00-05:00"}
        ok, _ = run.grade(self.G, self.rec([self.call("cal-work", **narrow), self.call("cal-program", **narrow)]))
        self.assertFalse(ok)

    def test_open_window_covers(self) -> None:
        ok, detail = run.grade(self.G, self.rec([self.call("cal-work"), self.call("cal-program", timeMin="2026-09-15T00:00:00Z")]))
        self.assertTrue(ok, detail)

    def test_covers_explicit_window(self) -> None:
        g = {"type": "mock_calls", "calendar_ids": ["cal-work"], "covers": {"from": "2026-09-16 17:21", "to": "2026-09-16 21:21"}}
        ok, detail = run.grade(g, self.rec([self.call("cal-work", timeMin="2026-09-16T00:00:00-05:00", timeMax="2026-09-16T21:21:00-05:00")]))
        self.assertTrue(ok, detail)
        ok, _ = run.grade(g, self.rec([self.call("cal-work", timeMin="2026-09-16T00:00:00-05:00", timeMax="2026-09-16T20:00:00-05:00")]))
        self.assertFalse(ok)

    def test_no_calls_fails(self) -> None:
        ok, _ = run.grade(self.G, self.rec([]))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
