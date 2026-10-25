"""Judge a calendar week of real workspace use against S1–S4 (M7.4)."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import decision_escalation
import reconcile
import supervisor
from workspace import ConfigError, read_config


@dataclass(frozen=True)
class BakeOutcome:
    status: str  # hold, fail, incomplete
    lines: tuple[str, ...] = ()


def _parse_at(value: str, zone: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def _in_window(at: str, start: datetime, end: datetime, zone: ZoneInfo) -> bool:
    t = _parse_at(at, zone)
    return start <= t <= end


def _window_days(start: datetime, end: datetime, zone: ZoneInfo) -> int:
    s = start.astimezone(zone).date()
    e = end.astimezone(zone).date()
    return (e - s).days


def _has_use(items: Sequence[Any], segments: Sequence[Any], decisions: Sequence[Any],
             logs: Sequence[Any], start: datetime, end: datetime, zone: ZoneInfo) -> bool:
    for seg in segments:
        if _in_window(seg.started_at, start, end, zone):
            return True
        if seg.ended_at and _in_window(seg.ended_at, start, end, zone):
            return True
    for row in decisions:
        if _in_window(row.created_at, start, end, zone):
            return True
    for entry in logs:
        if _in_window(entry.at, start, end, zone):
            return True
    return bool(items) and any(
        getattr(item, "updated_at", "") and _in_window(item.updated_at, start, end, zone) for item in items
    )


def _allowed_decision_record(record: dict[str, Any]) -> bool:
    if record.get("retry_exhausted"):
        return True
    if record.get("recommendation") == "hold":
        return True
    verdicts = record.get("verdicts")
    if record.get("unanimous") is False and verdicts:
        return True
    return False


def _allowed_interrupt_log(entry: Any) -> bool:
    msg = str(getattr(entry, "message", "") or "")
    return msg.startswith("limit:") or msg.startswith("exhausted:")


def _policy_human_review(segment: Any) -> bool:
    return bool(getattr(segment, "required_human_review", False))


def _routine_human_review(item: Any, segments: Sequence[Any], decisions: Sequence[Any],
                          start: datetime, end: datetime, zone: ZoneInfo) -> str | None:
    key = item.issue_key
    issue_decs = [d for d in decisions if d.issue_key == key]
    for seg in segments:
        if seg.issue_key != key or not _policy_human_review(seg):
            continue
        for stamp in (seg.ended_at, seg.started_at):
            if stamp and _in_window(stamp, start, end, zone):
                return None
    if item.human_review and item.updated_at and _in_window(item.updated_at, start, end, zone):
        if any(_allowed_decision_record(d.record if isinstance(d.record, dict) else {}) for d in issue_decs):
            return None
        if any(_allowed_decision_record(d.record if isinstance(d.record, dict) else {})
               for d in issue_decs if _in_window(d.created_at, start, end, zone)):
            return None
        return f"human_review on {key}"
    for seg in segments:
        if seg.issue_key != key or seg.outcome != "human_review" or _policy_human_review(seg):
            continue
        for stamp in (seg.ended_at, seg.started_at):
            if stamp and _in_window(stamp, start, end, zone):
                if not any(_allowed_decision_record(d.record if isinstance(d.record, dict) else {}) for d in issue_decs):
                    return f"segment human_review on {key}"
    return None


def _check_s1(decisions: Sequence[Any], items: Sequence[Any], segments: Sequence[Any], logs: Sequence[Any],
              start: datetime, end: datetime, zone: ZoneInfo) -> str | None:
    for row in decisions:
        record = row.record if isinstance(getattr(row, "record", None), dict) else {}
        created = _in_window(row.created_at, start, end, zone)
        if row.decided_by == "zach" and created:
            if not _allowed_decision_record(record):
                return f"zach decision {getattr(row, 'id', '')} at {row.created_at}"
        if decision_escalation.needs_owner([row]) is not None:
            if _in_window(row.created_at, start, end, zone) or created:
                if not _allowed_decision_record(record):
                    return f"owner wait on {row.issue_key} from {row.created_at}"
    for entry in logs:
        if _in_window(entry.at, start, end, zone) and _allowed_interrupt_log(entry):
            continue
    for item in items:
        hit = _routine_human_review(item, segments, decisions, start, end, zone)
        if hit:
            return hit
    return None


def _check_s2(items: Sequence[Any], flow_rows: Sequence[tuple[str, Any]]) -> str | None:
    keys = [item.issue_key for item in items]
    seen_fold: dict[str, str] = {}
    for key in keys:
        folded = key.casefold()
        if folded in seen_fold and seen_fold[folded] != key:
            return f"casefold duplicate {seen_fold[folded]} and {key}"
        seen_fold[folded] = key
    counts: dict[str, int] = {}
    for key, _row in flow_rows:
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > 1:
            return f"multiple gantt rows for {key}"
    for key, row in flow_rows:
        segs = getattr(row, "segments", ()) or ()
        stages = [getattr(s, "category", "") for s in segs]
        if len(stages) != len(set(stages)) and len(stages) > 1:
            pass  # rework may repeat stage names across attempts
    return None


def _merge_stage_decision(row: Any) -> bool:
    if row.topic == "merge":
        return True
    record = row.record if isinstance(getattr(row, "record", None), dict) else {}
    decision = record.get("decision") if isinstance(record.get("decision"), dict) else {}
    return decision.get("stage") == "merge"


def _check_s3(logs: Sequence[Any], decisions: Sequence[Any], kanban_hold_stages: Sequence[str],
              start: datetime, end: datetime, zone: ZoneInfo) -> str | None:
    if "merge" in kanban_hold_stages:
        return "kanban map holds merge for a human"
    for entry in logs:
        if getattr(entry, "kind", "") != reconcile.OP_MERGE:
            continue
        if not _in_window(entry.at, start, end, zone):
            continue
        merge_at = _parse_at(entry.at, zone)
        key = entry.issue_key
        for row in decisions:
            if row.issue_key != key:
                continue
            if row.decided_by != "zach" or not (row.answer or "").strip():
                continue
            if _parse_at(row.created_at, zone) > merge_at:
                continue
            if _merge_stage_decision(row):
                return f"prompted merge on {key} at {entry.at}"
    return None


def _issue_open_at(item: Any, segments: Sequence[Any], at: datetime, zone: ZoneInfo) -> bool:
    if getattr(item, "forge_state", "") == "closed":
        close = getattr(item, "updated_at", "")
        if close:
            return _parse_at(close, zone) > at
        return False
    return True


def _had_work_before(key: str, segments: Sequence[Any], logs: Sequence[Any], at: datetime, zone: ZoneInfo) -> bool:
    for seg in segments:
        if seg.issue_key != key:
            continue
        if _parse_at(seg.started_at, zone) < at:
            return True
    for entry in logs:
        if entry.issue_key != key:
            continue
        if _parse_at(entry.at, zone) < at:
            return True
    return False


def _autonomous_after(key: str, after: datetime, segments: Sequence[Any], logs: Sequence[Any],
                      zone: ZoneInfo) -> datetime | None:
    times: list[datetime] = []
    for seg in segments:
        if seg.issue_key != key:
            continue
        for stamp in (seg.started_at, seg.ended_at):
            if not stamp:
                continue
            t = _parse_at(stamp, zone)
            if t > after:
                times.append(t)
    for entry in logs:
        if entry.issue_key != key:
            continue
        t = _parse_at(entry.at, zone)
        if t <= after:
            continue
        msg = str(entry.message or "")
        if entry.kind == reconcile.OP_MERGE or msg.startswith("wait:"):
            times.append(t)
    return min(times) if times else None


def _owner_event_between(key: str, start: datetime, end: datetime, decisions: Sequence[Any],
                         items: Sequence[Any], segments: Sequence[Any], zone: ZoneInfo) -> bool:
    for row in decisions:
        if row.issue_key != key:
            continue
        t = _parse_at(row.created_at, zone)
        if not (start < t < end):
            continue
        record = row.record if isinstance(row.record, dict) else {}
        if row.decided_by == "zach" and not _allowed_decision_record(record):
            return True
        if decision_escalation.needs_owner([row]) and not _allowed_decision_record(record):
            return True
    item = next((i for i in items if i.issue_key == key), None)
    if item and _routine_human_review(item, segments, decisions, start, end, zone):
        return True
    return False


def _check_s4(engine_starts: Sequence[dict[str, Any]], items: Sequence[Any], segments: Sequence[Any],
              logs: Sequence[Any], decisions: Sequence[Any], start: datetime, end: datetime,
              zone: ZoneInfo) -> str | None:
    starts = []
    for raw in engine_starts:
        at = _parse_at(str(raw["at"]), zone)
        if start <= at <= end:
            starts.append(at)
    starts.sort()
    if len(starts) < 2:
        return None
    for restart in starts[1:]:
        for item in items:
            key = item.issue_key
            if not _issue_open_at(item, segments, restart, zone):
                continue
            if not _had_work_before(key, segments, logs, restart, zone):
                continue
            progress = _autonomous_after(key, restart, segments, logs, zone)
            if progress is None:
                return f"no autonomous progress after restart at {restart.isoformat()}"
            if _owner_event_between(key, restart, progress, decisions, items, segments, zone):
                return f"owner prompt between restart and progress on {key}"
    return None


def judge(
    *,
    zone: ZoneInfo,
    start: datetime,
    end: datetime,
    has_start_file: bool,
    items: Sequence[Any],
    segments: Sequence[Any],
    decisions: Sequence[Any],
    logs: Sequence[Any],
    engine_starts: Sequence[dict[str, Any]],
    flow_rows: Sequence[tuple[str, Any]],
    kanban_hold_stages: Sequence[str] = (),
) -> BakeOutcome:
    """Pure judgement over plain store-shaped records."""
    if _window_days(start, end, zone) < 7 or not _has_use(items, segments, decisions, logs, start, end, zone):
        return BakeOutcome("incomplete")
    if not has_start_file:
        return BakeOutcome("incomplete")
    lines: list[str] = []
    for name, check in (
        ("S1", lambda: _check_s1(decisions, items, segments, logs, start, end, zone)),
        ("S2", lambda: _check_s2(items, flow_rows)),
        ("S3", lambda: _check_s3(logs, decisions, kanban_hold_stages, start, end, zone)),
        ("S4", lambda: _check_s4(engine_starts, items, segments, logs, decisions, start, end, zone)),
    ):
        reason = check()
        if reason:
            lines.append(f"{name}: {reason}")
    if lines:
        return BakeOutcome("fail", tuple(lines))
    return BakeOutcome("hold")


def _load_starts(root) -> tuple[bool, list[dict[str, Any]]]:
    path = supervisor.state_dir(root) / "engine-starts.jsonl"
    if not path.is_file():
        return False, []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return True, rows


def _flow_rows(db, end: datetime) -> list[tuple[str, Any]]:
    import flow_chart

    built = flow_chart.build_from_store(db, end)
    items = db.items()
    by_key = {item.issue_key: item for item in items}
    order = [item.issue_key for item in items if item.issue_key in by_key]
    keys_in_build = []
    for item in items:
        keys_in_build.append(item.issue_key)
    if len(keys_in_build) != len(built):
        return [(item.issue_key, row) for (_, row), item in zip(built, items, strict=False)]
    return list(zip(keys_in_build, (row for _, row in built), strict=True))


def load_and_judge(root, start: datetime, end: datetime, zone: ZoneInfo) -> BakeOutcome:
    import settings
    import store_access

    has_file, starts = _load_starts(root)
    ws = read_config(root)
    cfg = settings.load(root, ws.settings_path)
    holds = cfg.kanban.hold_stages if cfg.kanban else ()
    db = store_access.migrated_store(root)
    try:
        items = db.items()
        segments = db.segments()
        decisions = db.decisions()
        logs = db.log()
        flow_rows = _flow_rows(db, end.astimezone(zone))
        return judge(
            zone=zone,
            start=start,
            end=end,
            has_start_file=has_file,
            items=items,
            segments=segments,
            decisions=decisions,
            logs=logs,
            engine_starts=starts,
            flow_rows=flow_rows,
            kanban_hold_stages=holds,
        )
    finally:
        db.engine.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path("."), help="workspace root")
    ap.add_argument("--start", required=True, help="window start (ISO8601)")
    ap.add_argument("--end", required=True, help="window end (ISO8601)")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    try:
        cfg = read_config(root)
        zone = cfg.zone
        start = _parse_at(args.start, zone)
        end = _parse_at(args.end, zone)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"bake: {exc}", file=sys.stderr)
        return 2
    outcome = load_and_judge(root, start, end, zone)
    print(outcome.status)
    for line in outcome.lines:
        print(line)
    return {"hold": 0, "fail": 1, "incomplete": 2}[outcome.status]


if __name__ == "__main__":
    raise SystemExit(main())
