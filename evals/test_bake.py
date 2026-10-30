"""M7.4 bake judge: S1–S4 over fixture records (no live workspace data)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bake
import reconcile
import store
import supervisor
from gantt import Row, Segment
from test_m6_store_board import make_store_workspace

ZONE = ZoneInfo("America/Chicago")
KEY = "github:example/project#1"
OTHER = "github:other/project#1"
CLOSED = "github:example/project#99"


def _dt(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, 0, tzinfo=ZONE)


def _seg(stage: str, attempt: int, started: datetime, ended: datetime | None = None, **extra):
    return SimpleNamespace(
        issue_key=KEY,
        stage=stage,
        attempt=attempt,
        started_at=started.isoformat(),
        ended_at=ended.isoformat() if ended else None,
        outcome=extra.get("outcome"),
        required_human_review=extra.get("required_human_review", False),
    )


def _item(**extra):
    defaults = {"issue_key": KEY, "stage": "implement", "forge_state": "open", "human_review": False,
                "updated_at": ""}
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def _dec(created: datetime, *, decided_by: str = "user", answer: str = "", topic: str = "ask", record=None):
    return SimpleNamespace(
        issue_key=KEY,
        topic=topic,
        question="?",
        answer=answer,
        decided_by=decided_by,
        created_at=created.isoformat(),
        record=record or {},
    )


def _log(at: datetime, message: str = "", kind: str = "", issue_key: str = KEY):
    return SimpleNamespace(issue_key=issue_key, at=at.isoformat(), message=message, kind=kind)


def _row(n_segments: int) -> Row:
    start = _dt(1)
    segs = tuple(
        Segment(start + timedelta(hours=i), start + timedelta(hours=i + 1), f"stage{i}", "done", "")
        for i in range(n_segments)
    )
    return Row(ref="#1", name=KEY, note="", segments=segs)


def _kwargs(start: datetime, end: datetime, **overrides):
    base = {
        "zone": ZONE,
        "start": start,
        "end": end,
        "has_start_file": True,
        "items": (_item(),),
        "segments": (_seg("implement", 1, _dt(2)),),
        "decisions": (),
        "logs": (),
        "engine_starts": ({"at": _dt(1, 6).isoformat(), "pid": 1},),
        "flow_rows": ((KEY, _row(1)),),
        "kanban_hold_stages": (),
    }
    base.update(overrides)
    return base


class JudgeTest(unittest.TestCase):
    def test_hold_over_seven_days_with_use_restart_and_wait(self):
        start, end = _dt(1), _dt(8)
        outcome = bake.judge(**_kwargs(
            start, end,
            segments=(
                _seg("implement", 1, _dt(2), _dt(3)),
                _seg("review", 1, _dt(4)),
            ),
            engine_starts=(
                {"at": _dt(1, 6).isoformat(), "pid": 1},
                {"at": _dt(5, 9).isoformat(), "pid": 2},
            ),
            logs=(_log(_dt(5, 9) + timedelta(minutes=5), "wait: quota", ""),),
            flow_rows=((KEY, _row(2)),),
        ))
        self.assertEqual(outcome.status, "hold")

    def test_owner_row_without_verdicts_fails_s1(self):
        outcome = bake.judge(**_kwargs(
            _dt(1), _dt(8),
            decisions=(_dec(_dt(3), record={"unanimous": False}),),
        ))
        self.assertEqual(outcome.status, "fail")
        self.assertTrue(any(line.startswith("S1:") for line in outcome.lines))

    def test_unanimous_non_hold_reaching_owner_fails_s1(self):
        outcome = bake.judge(**_kwargs(
            _dt(1), _dt(8),
            decisions=(_dec(_dt(3), record={"unanimous": True, "recommendation": "approve",
                                            "verdicts": [{"vote": "adopt", "choice": "approve"}]}),),
        ))
        self.assertEqual(outcome.status, "fail")
        self.assertTrue(any("S1:" in line for line in outcome.lines))

    def test_s1_limit_interrupt_reaching_owner_holds(self):
        outcome = bake.judge(**_kwargs(
            _dt(1), _dt(8),
            decisions=(_dec(_dt(3), record={}),),
            logs=(_log(_dt(3), "limit: reached spend_usd limit"),),
        ))
        self.assertEqual(outcome.status, "hold")
        self.assertFalse(any(line.startswith("S1:") for line in outcome.lines))

    def test_s2_one_item_three_segments_one_row(self):
        outcome = bake.judge(**_kwargs(
            _dt(1), _dt(8),
            segments=(
                _seg("implement", 1, _dt(2), _dt(3)),
                _seg("implement", 2, _dt(3), _dt(4)),
                _seg("review", 1, _dt(4)),
            ),
            flow_rows=((KEY, _row(3)),),
        ))
        self.assertNotIn("S2:", "\n".join(outcome.lines))

    def test_s2_two_rows_for_one_key_fails(self):
        outcome = bake.judge(**_kwargs(
            _dt(1), _dt(8),
            flow_rows=((KEY, _row(1)), (KEY, _row(1))),
        ))
        self.assertEqual(outcome.status, "fail")
        self.assertTrue(any(line.startswith("S2:") for line in outcome.lines))

    def test_s3_owner_merge_answer_before_merge_fails(self):
        merge_at = _dt(6)
        outcome = bake.judge(**_kwargs(
            _dt(1), _dt(8),
            decisions=(_dec(_dt(5), topic="merge", answer="yes"),),
            logs=(_log(merge_at, "merged", reconcile.OP_MERGE),),
        ))
        self.assertEqual(outcome.status, "fail")
        self.assertTrue(any(line.startswith("S3:") for line in outcome.lines))

    def test_s3_implement_ambiguity_before_merge_holds(self):
        merge_at = _dt(6)
        outcome = bake.judge(**_kwargs(
            _dt(1), _dt(8),
            decisions=(_dec(_dt(4), topic="scope", answer="pick A"),),
            logs=(_log(merge_at, "merged", reconcile.OP_MERGE),),
        ))
        self.assertNotIn("S3:", "\n".join(outcome.lines))

    def test_s3_cleared_merge_human_review_fails(self):
        merge_at = _dt(6)
        outcome = bake.judge(**_kwargs(
            _dt(1), _dt(8),
            segments=(
                _seg("merge", 1, _dt(4), _dt(5), outcome="human_review"),
                _seg("implement", 1, _dt(2), _dt(3)),
            ),
            logs=(_log(merge_at, "merged", reconcile.OP_MERGE),),
        ))
        self.assertEqual(outcome.status, "fail")
        self.assertTrue(any(line.startswith("S3:") for line in outcome.lines))

    def test_s4_progress_before_new_ask_holds(self):
        restart = _dt(5, 9)
        outcome = bake.judge(**_kwargs(
            _dt(1), _dt(8),
            segments=(_seg("implement", 1, _dt(2)),),
            engine_starts=(
                {"at": _dt(1, 6).isoformat(), "pid": 1},
                {"at": restart.isoformat(), "pid": 2},
            ),
            logs=(_log(restart + timedelta(minutes=1), "wait: quota", ""),),
        ))
        self.assertNotIn("S4:", "\n".join(outcome.lines))

    def test_s4_ask_between_start_and_progress_fails(self):
        restart = _dt(5, 9)
        outcome = bake.judge(**_kwargs(
            _dt(1), _dt(8),
            segments=(_seg("implement", 1, _dt(2)),),
            engine_starts=(
                {"at": _dt(1, 6).isoformat(), "pid": 1},
                {"at": restart.isoformat(), "pid": 2},
            ),
            decisions=(_dec(restart + timedelta(minutes=1), record={"unanimous": False}),),
            logs=(_log(restart + timedelta(hours=2), "wait: quota", ""),),
        ))
        self.assertEqual(outcome.status, "fail")
        self.assertTrue(any(line.startswith("S4:") for line in outcome.lines))

    def test_six_day_window_is_incomplete(self):
        outcome = bake.judge(**_kwargs(_dt(1), _dt(7)))
        self.assertEqual(outcome.status, "incomplete")

    def test_seven_days_without_store_activity_is_incomplete(self):
        outcome = bake.judge(**_kwargs(
            _dt(1), _dt(8),
            items=(),
            segments=(),
            flow_rows=(),
        ))
        self.assertEqual(outcome.status, "incomplete")

    def test_missing_start_file_is_incomplete(self):
        outcome = bake.judge(**_kwargs(_dt(1), _dt(8), has_start_file=False, engine_starts=()))
        self.assertEqual(outcome.status, "incomplete")


class FlowRowsLoadTest(unittest.TestCase):
    def test_flow_rows_keeps_keys_when_closed_segmentless_skipped(self):
        root, db = make_store_workspace()
        self.addCleanup(db.engine.dispose)
        db.upsert_item(CLOSED, workflow="build", stage="merge", forge_state="closed", title="Gone")
        db.upsert_item(KEY, workflow="build", stage="implement", forge_state="open", title="Live")
        started = datetime(2026, 10, 9, 10, 0, tzinfo=ZONE)
        db.record_segment(KEY, "implement", 1, started_at=started.isoformat(),
                          ended_at=(started + timedelta(hours=1)).isoformat(), outcome="done")
        rows = bake._flow_rows(db, datetime(2026, 10, 9, 14, 0, tzinfo=ZONE))
        keys = [key for key, _ in rows]
        self.assertEqual(keys, [KEY])
        self.assertNotIn(CLOSED, keys)


class RecordEngineStartTest(unittest.TestCase):
    def test_appends_one_json_line_without_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).isoformat()
            supervisor.record_engine_start(root, at, 99)
            path = supervisor.state_dir(root) / "engine-starts.jsonl"
            first = path.read_text(encoding="utf-8")
            self.assertEqual(json.loads(first.strip()), {"at": at, "pid": 99})
            supervisor.record_engine_start(root, at, 100)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0]), {"at": at, "pid": 99})
            self.assertEqual(json.loads(lines[1]), {"at": at, "pid": 100})


if __name__ == "__main__":
    unittest.main()
