#!/usr/bin/env python3
"""Stand-in for completed Claude JSON or stream-json calls through the model adapter.

Sessions are stored in cwd for resume tests. SLEEP hangs; QUOTA and FAIL exercise failures.
"""

import json
import sys
import tempfile
import time
import uuid
from pathlib import Path


if sys.argv[-3:] == ["plugin", "list", "--json"]:
    if "--setting-sources" not in sys.argv or sys.argv[sys.argv.index("--setting-sources") + 1] != "user":
        print("plugin list requires --setting-sources user", file=sys.stderr)
        sys.exit(2)
    payload = Path(tempfile.mkdtemp(prefix="fake-clock-"))
    (payload / "hooks").mkdir()
    (payload / "hooks" / "hooks.json").write_text("{}")
    print(json.dumps([{"id": "datetime-inject@ai-additions", "version": "1.0.0",
                       "scope": "user", "enabled": True, "installPath": str(payload)}]))
    sys.exit(0)


def one_answer(args):
    """`claude -p --output-format json ... -- <prompt>`, as the M0 spike measured it. A `claude-as run <profile>` lead is
    skipped. Sessions live in `.fake-claude-sessions/` in cwd. The prompt picks the outcome: QUOTA is a 429 quota stop on
    stderr, FAIL an error envelope with subtype success, SLEEP hangs; anything else is answered."""
    tail = args[args.index("--") + 1:]
    # Stream input (engine workers, #1160): the prompt is the first stdin message.
    prompt = tail[0] if tail else json.loads(sys.stdin.readline())["message"]["content"]
    flag = {key: value for key, value in zip(args, args[1:]) if key.startswith("--")}
    sessions = Path(".fake-claude-sessions")
    session = flag.get("--resume") or flag.get("--session-id") or str(uuid.uuid4())
    if "SLEEP" in prompt:
        time.sleep(30)
    if "QUOTA" in prompt:
        print('API Error: 429 {"error":{"status":"RESOURCE_EXHAUSTED"}} resets at 5pm.', file=sys.stderr)
        sys.exit(1)
    if "--session-id" in flag:
        try:
            uuid.UUID(session)
        except ValueError:
            print("Error: Invalid session ID. Must be a valid UUID.", file=sys.stderr)
            sys.exit(1)
    if "--resume" in flag and not (sessions / session).exists():
        print(f"No conversation found with session ID: {session}", file=sys.stderr)
        sys.exit(1)
    text = f"reply: {prompt}"
    if "--resume" in flag:
        text += f" (recalls: {(sessions / session).read_text()})"
    elif "--session-id" in flag:
        sessions.mkdir(exist_ok=True)
        (sessions / session).write_text(prompt)
    failed = "FAIL" in prompt
    envelope = {"type": "result", "subtype": "success", "is_error": failed, "result": text, "session_id": session,
                "num_turns": 2 if "--json-schema" in flag else 1, "total_cost_usd": 0.0}
    if "--json-schema" in flag:
        envelope["structured_output"] = {"answer": prompt}
    if flag.get("--output-format") == "stream-json":
        for event in (
            {"type": "system", "subtype": "init", "session_id": session, "model": "claude-sonnet-4-6",
             "tools": ["Bash"], "agents": ["claude"], "mcp_servers": []},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "date"}},
                {"type": "text", "text": text}]}},
        ):
            print(json.dumps(event))
    print(json.dumps(envelope))
    sys.exit(1 if failed else 0)


one_answer(sys.argv[1:])
