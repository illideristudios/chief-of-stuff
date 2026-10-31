"""Manual live proof that `datetime-inject@ai-additions` reaches a `claude -p` worker (1.0 plan, M3 item 8).

Run explicitly before tagging a release:
    CHIEF_CLOCK_LIVE=1 [CHIEF_CLOCK_PROFILE=<p>] python3 -m unittest evals.live.clock_delivery -v

Makes two billed `haiku` calls in separate worker sessions 1 second apart.

Spike finding (#636): `claude -p --output-format stream-json --verbose` emits `system/hook_started`
and `system/hook_response` events for `SessionStart` hooks only; `UserPromptSubmit` hook output is
injected into the model's prompt context without a stream-json hook event. This proof therefore asks
`haiku` to return the injected `[Current TimeStamp: ...]` line in a `--json-schema` response field and
checks that both timestamps parse and advance between calls.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import claude_adapter
import clock
import guard_binding
from model_port import Reply, Request

STAMP = re.compile(
    r"\[Current TimeStamp: [^|\]]+ \| (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) [^\]]+ \(UTC([+-]\d\d:\d\d)\)\]"
)
SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
    "required": ["lines"],
    "additionalProperties": False,
}
PROMPT = (
    "Return in `lines` every bracketed `[Current TimeStamp: ...]` line injected into this turn's context, "
    "character-for-character."
)


def _parse_stamp(lines: list[str]) -> datetime:
    """Exactly one `[Current TimeStamp: ...]` match across the returned lines; duplicates or none fail closed."""
    found = [m for line in lines for m in STAMP.finditer(line)]
    if len(found) != 1:
        raise AssertionError(f"want exactly one [Current TimeStamp: ...] line, got {len(found)} in {lines!r}")
    match = found[0]
    return datetime.fromisoformat(f"{match[1]}{match[2]}")


class ClockDeliveryLiveTest(unittest.TestCase):
    def test_worker_receives_advancing_datetime_inject_timestamps(self) -> None:
        if os.environ.get("CHIEF_CLOCK_LIVE") != "1":
            self.fail(
                "set CHIEF_CLOCK_LIVE=1 to run this billed live proof: "
                "CHIEF_CLOCK_LIVE=1 python3 -m unittest evals.live.clock_delivery -v"
            )
        profile = os.environ.get("CHIEF_CLOCK_PROFILE", "")
        guard = guard_binding.guard_plugin_dir()
        self.assertIsNotNone(guard)
        with tempfile.TemporaryDirectory() as tmp:
            claude_adapter.require_worker_clock(profile, cwd=tmp)
            stamps: list[datetime] = []
            for attempt in (1, 2):
                if attempt == 2:
                    time.sleep(1.1)
                session_id = str(uuid.uuid4())
                self.addCleanup(claude_adapter.forget_session, session_id, dict(os.environ))
                binding = guard_binding.Binding(
                    "github:example/repo#636",
                    "implement",
                    attempt,
                    session_id,
                    Path(tmp),
                    clock.system(),
                    guard_binding.DEFAULT_PROTECTED_BRANCHES,
                )
                before = clock.system().replace(microsecond=0) - timedelta(seconds=1)
                reply = claude_adapter.call(
                    Request(
                        prompt=PROMPT,
                        model="haiku",
                        session_id=session_id,
                        cwd=tmp,
                        worker=True,
                        plugin_dirs=(str(guard),),
                        schema=SCHEMA,
                        timeout_s=60,
                    ),
                    claude_as_profile=profile,
                    env={**os.environ, **guard_binding.guard_env(binding)},
                )
                after = clock.system() + timedelta(seconds=1)
                self.assertIsInstance(reply, Reply, f"worker call failed: {reply!r}")
                assert isinstance(reply, Reply)
                self.assertIsInstance(reply.data, dict, f"missing structured_output: {reply!r}")
                stamp = _parse_stamp(reply.data.get("lines", []))
                self.assertGreaterEqual(stamp, before)
                self.assertLessEqual(stamp, after)
                stamps.append(stamp)
            self.assertGreater(stamps[1], stamps[0])


if __name__ == "__main__":
    unittest.main()
