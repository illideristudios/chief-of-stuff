"""The board's due clock, run in node against the script render_board actually emits.

A page left open keeps the deadline it was given and must switch from `in` to `passed … ago` when that
instant goes by. Missing node, a throw, or a hang fails the case: a skip would count as no regression.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import panels  # noqa: E402
import render_board as rb  # noqa: E402
from workspace import parse_coordinator  # noqa: E402

CT = ZoneInfo("America/Chicago")
PT = ZoneInfo("America/Los_Angeles")
NODE_TIMEOUT = 10

CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Timezone: America/Chicago
- Deadlines:
  - Launch: 2026-09-16 23:59
"""

TRACKER = """# Tracker 2026-10-18

## Tasks

| item | owner | state | since | due | checklist |
|---|---|---|---|---|---|
| Note | Robin | open | 09:00 |  | c |

## Log

- 09:00 opened
"""


def epoch_ms(dt: datetime) -> int:
    return round(dt.timestamp() * 1000)


def clock_script() -> str:
    """The one inline script on a rendered board. The driver never carries its own copy."""
    now = datetime(2026, 10, 18, 17, 42, tzinfo=CT)
    cfg = parse_coordinator(CLAUDE, today=now.date())
    html = rb.render(TRACKER, cfg, now)
    found = re.findall(r"<script>\n(.*?)\n</script>", html, re.S)
    if len(found) != 1:
        raise AssertionError(f"expected one clock script, found {len(found)}")
    return found[0]


class NodeFailed(Exception):
    """node did not run the clock to a result."""


# Reads {script, scenarios} on stdin. Each scenario is one page: a fake Date, the board dataset, and
# the two clocks. `steps` move the clock and fire the captured interval. The script's own first tick
# has already run by the time the first snap is taken.
DRIVER = r"""
const vm = require('vm');
const {script, scenarios} = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function el(text) {
  return {textContent: text, kids: {}, querySelector(sel) { return this.kids[sel] || null; }};
}
function run(scenario) {
  let nowMs = scenario.now;
  const RealDate = Date;
  function FakeDate(...args) {
    return args.length ? new RealDate(...args) : new RealDate(nowMs);
  }
  FakeDate.parse = RealDate.parse;
  FakeDate.UTC = RealDate.UTC;
  FakeDate.now = () => nowMs;
  const nowLabel = el('now');
  const dueLabel = el(scenario.stale ? scenario.stale.label : 'stale label');
  const nowTime = el('');
  const leftTime = el(scenario.stale ? scenario.stale.left : 'stale left');
  const dueClock = el('');
  dueClock.kids['.panels-clock-name'] = dueLabel;
  const root = {
    dataset: {tz: scenario.tz, deadline: scenario.deadline, deadlineName: scenario.name},
    querySelector(sel) {
      if (sel === '.panels-now') return nowTime;
      if (sel === '.panels-left') return leftTime;
      if (sel === '.panels-due .panels-clock-name') return dueLabel;
      if (sel === '.panels-due') return dueClock;
      if (sel === '.panels-clock-name') return nowLabel;
      return null;
    },
  };
  const document = {querySelector: sel => sel === '.board' ? root : null};
  const intervals = [];
  const sandbox = {
    document, Math, Intl, Date: FakeDate,
    setInterval: (f, delay) => { intervals.push({f, delay}); return intervals.length; },
  };
  new vm.Script(script).runInNewContext(sandbox, {timeout: 2000});
  const snap = () => ({
    label: dueLabel.textContent, left: leftTime.textContent,
    now: nowTime.textContent, plain: nowLabel.textContent,
  });
  const snaps = [snap()];
  for (const step of scenario.steps || []) {
    if ('now' in step) nowMs = step.now;
    if (step.tick) intervals.forEach(i => i.f());
    snaps.push(snap());
  }
  return {snaps, delays: intervals.map(i => i.delay)};
}
const out = scenarios.map(run);
console.log(JSON.stringify(out));
"""


def run_node(script: str, scenarios: list[dict], timeout: float = NODE_TIMEOUT) -> list[dict]:
    try:
        done = subprocess.run(
            ["node", "-e", DRIVER],
            input=json.dumps({"script": script, "scenarios": scenarios}),
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "TZ": "UTC"},
        )
    except subprocess.TimeoutExpired:
        raise NodeFailed(f"node did not finish in {timeout}s") from None
    except FileNotFoundError:
        raise NodeFailed("node is not on PATH: the board clock was not run") from None
    if done.returncode:
        raise NodeFailed(f"node exited {done.returncode}: {done.stderr.strip()[:500]}")
    try:
        got = json.loads(done.stdout)
    except ValueError:
        raise NodeFailed(f"node printed no result: {done.stdout[:200]!r} {done.stderr.strip()[:200]}") from None
    if not isinstance(got, list) or len(got) != len(scenarios):
        raise NodeFailed(f"node printed {got!r:.200} for {len(scenarios)} scenarios")
    return got


def _page(name: str, now: datetime, at: datetime, **extra) -> dict:
    return {"tz": str(now.tzinfo), "deadline": at.isoformat(), "name": name, "now": epoch_ms(now), **extra}


class BoardClockInNodeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = clock_script()

    def test_an_open_tab_crosses_into_passed(self) -> None:
        at = datetime(2026, 10, 18, 18, 0, tzinfo=CT)
        ms = epoch_ms(at)
        got = run_node(self.script, [_page("Review cutoff", at - timedelta(seconds=2), at, steps=[
            {"now": ms, "tick": True},
            {"now": ms + 1000, "tick": True},
        ])])[0]
        self.assertEqual(got["delays"], [1000])
        self.assertEqual([s["label"] for s in got["snaps"]], [
            "Review cutoff in", "Review cutoff in", "Review cutoff passed",
        ])
        self.assertEqual([s["left"] for s in got["snaps"]], ["0h00m02s", "0h00m00s", "0h00m01s ago"])
        self.assertTrue(all("-" not in s["left"] for s in got["snaps"]))
        self.assertEqual([s["now"] for s in got["snaps"]], ["17:59:58", "18:00:00", "18:00:01"])
        self.assertTrue(all(s["plain"] == "now" for s in got["snaps"]))

    def test_the_first_tick_corrects_a_stale_negative_clock(self) -> None:
        at = datetime(2026, 10, 18, 18, 0, tzinfo=CT)
        got = run_node(self.script, [_page(
            "Review cutoff", at + timedelta(milliseconds=400), at,
            stale={"label": "Review cutoff in", "left": "-173h42m00s"},
        )])[0]
        self.assertEqual(got["delays"], [1000])
        self.assertEqual(got["snaps"], [{
            "label": "Review cutoff passed", "left": "0h00m00s ago",
            "now": "18:00:00", "plain": "now",
        }])

    def test_a_literal_name_survives_expiry(self) -> None:
        name = 'A<B>&"C"'
        at = datetime(2026, 10, 18, 18, 0, tzinfo=CT)
        got = run_node(self.script, [_page(name, at + timedelta(seconds=5), at)])[0]
        self.assertEqual(got["snaps"][0]["label"], f"{name} passed")
        self.assertEqual(got["snaps"][0]["left"], "0h00m05s ago")
        self.assertNotIn("&lt;", got["snaps"][0]["label"])

    def test_ticks_agree_with_the_header_across_both_offset_changes(self) -> None:
        spring_now = datetime(2026, 3, 8, 1, 30, tzinfo=CT)
        spring_at = datetime(2026, 3, 8, 3, 30, tzinfo=CT)
        earlier = datetime(2026, 11, 1, 1, 30, tzinfo=CT)
        later = earlier.replace(fold=1)
        cases = (
            ("gap", spring_now, spring_at, "gap in", "1h00m00s", "01:30:00"),
            ("overlap", later, earlier, "overlap passed", "1h00m00s ago", "01:30:00"),
        )
        got = run_node(self.script, [_page(name, now, at) for name, now, at, *_ in cases])
        for (name, now, at, label, left, clock), result in zip(cases, got, strict=True):
            with self.subTest(name=name):
                html = panels.header(panels.Header("B", (), now, name, at))
                self.assertIn(f'<span class="panels-clock-name">{label}</span>', html)
                self.assertIn(f">{left}<", html)
                self.assertIn(f">{clock}<", html)
                self.assertEqual(result["snaps"][0]["label"], label)
                self.assertEqual(result["snaps"][0]["left"], left)
                self.assertEqual(result["snaps"][0]["now"], clock)
                self.assertEqual(result["snaps"][0]["plain"], "now")

    def test_the_current_clock_uses_the_workspace_zone(self) -> None:
        now = datetime(2026, 10, 18, 17, 42, tzinfo=PT)
        at = datetime(2026, 10, 18, 23, 59, tzinfo=PT)
        got = run_node(self.script, [_page("end of day", now, at)])[0]
        self.assertEqual(got["snaps"][0]["now"], "17:42:00")
        self.assertEqual(got["snaps"][0]["left"], "6h17m00s")
        self.assertEqual(got["snaps"][0]["label"], "end of day in")

    def test_a_throw_or_a_hang_or_a_missing_node_fails(self) -> None:
        with self.assertRaisesRegex(NodeFailed, "exited 1"):
            run_node("throw new Error('boom')", [_page("x", datetime(2026, 10, 18, 17, 42, tzinfo=CT),
                                                        datetime(2026, 10, 18, 23, 59, tzinfo=CT))])
        with self.assertRaisesRegex(NodeFailed, "did not finish"):
            run_node("for(;;){}", [_page("x", datetime(2026, 10, 18, 17, 42, tzinfo=CT),
                                          datetime(2026, 10, 18, 23, 59, tzinfo=CT))], timeout=1)
        saved = os.environ.get("PATH")
        os.environ["PATH"] = "/nonexistent"
        try:
            with self.assertRaisesRegex(NodeFailed, "not on PATH"):
                run_node("1", [])
        finally:
            if saved is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = saved

    def test_the_ci_image_installs_node(self) -> None:
        text = (Path(__file__).resolve().parents[1] / ".github/ci/Dockerfile").read_text()
        self.assertRegex(text, r"apt-get install[\s\S]*\bnodejs\b")


class ClockScriptShapeTest(unittest.TestCase):
    """The rendered page carries one script. This does not need node."""

    def test_render_emits_one_script(self) -> None:
        script = clock_script()
        self.assertIn("setInterval", script)
        self.assertIn("dataset.deadline", script)


if __name__ == "__main__":
    unittest.main()
