#!/usr/bin/env python3
"""Stand-in for `claude -p --input-format stream-json` in runner unit tests, and for `claude -p --output-format json`
behind the model adapter (`one_answer`).

Emits init, one assistant text, and a result per user line on stdin. Turn 2 writes `turn2.txt` in
cwd so tests can check per-turn fixture snapshots. A user message containing SLEEP hangs; one containing
BACKGROUND is followed by an unprompted notification turn; INLINE carries the notification inside its own turn; LINGER keeps the process alive after stdin closes.
"""

import json
import sys
import time
import uuid
from pathlib import Path


def one_answer(args):
    """`claude -p --output-format json ... -- <prompt>`, as the M0 spike measured it. A `claude-as run <profile>` lead is
    skipped. Sessions live in `.fake-claude-sessions/` in cwd. The prompt picks the outcome: QUOTA is a 429 quota stop on
    stderr, FAIL an error envelope with subtype success, SLEEP hangs; anything else is answered."""
    prompt = args[args.index("--") + 1]
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
    print(json.dumps(envelope))
    sys.exit(1 if failed else 0)


if "--output-format" in sys.argv and sys.argv[sys.argv.index("--output-format") + 1] == "json":
    one_answer(sys.argv[1:])


def turn(text):
    return (
        {"type": "system", "subtype": "init", "tools": ["Bash"], "agents": ["claude"], "mcp_servers": []},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "result": text, "num_turns": 1, "permission_denials": []},
    )


n = 0
linger = False
for line in sys.stdin:
    if not line.strip():
        continue
    n += 1
    text = json.loads(line)["message"]["content"]
    linger = linger or "LINGER" in text
    if "SLEEP" in text:
        time.sleep(30)
    if n == 2:
        Path("turn2.txt").write_text("written in turn 2\n")
    if "INLINE" in text:
        # The background task finishes before the assistant stops: the notification lands inside this turn.
        init, reply, result = turn(f"reply {n}: {text}")
        final = f"reply {n}: {text} — handled inline: DONE"
        for ev in (init, reply, {"type": "system", "subtype": "task_notification"}, {"type": "assistant", "message": {"content": [{"type": "text", "text": final}]}}, dict(result, result=final)):
            print(json.dumps(ev), flush=True)
        continue
    for ev in turn(f"reply {n}: {text}"):
        print(json.dumps(ev), flush=True)
    if "BACKGROUND" in text:
        # A background task finishing starts a turn with no user message, as `claude -p` does.
        time.sleep(0.5)
        print(json.dumps({"type": "system", "subtype": "task_notification"}), flush=True)
        for ev in turn("notified: DONE"):
            print(json.dumps(ev), flush=True)
if linger:
    # A background task still running after stdin closes keeps the process alive.
    time.sleep(60)
