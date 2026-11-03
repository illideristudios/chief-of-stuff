"""Print the agent performance table from the store's stage segments (architecture §9)."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from performance import records, table
import store_access
from workspace import ConfigError, read_config

DESCRIPTION = (
    "Print the agent performance table from the store's stage segments (architecture §9).\n\n"
    "Use this to aggregate one-shot worker run counts, outcomes, and median duration over\n"
    "a date window by day or week and by runtime, model, effort, and agent type."
)

EPILOG = """\
When not to use:
  For live board, worker, or decision pages, use chief-of-stuff board or
  chief-of-stuff pages. To read a day's human-readable tracker prose or sections,
  use chief-of-stuff tracker. For runtime account quota readings, use
  chief-of-stuff limits; for workspace settings, runtime PATH, and Health: target
  probes, use chief-of-stuff health.

  This command reads persisted store stage segments only (store.db via
  store_access.migrated_store). Daily tracker ## Log lines, one-shot report files,
  and interactive sessions are not sources (architecture §9).

Output:
  Without --raw, a plain text table on stdout (two-space-separated, left-padded
  columns; not TOON and not JSON). With --raw, one JSON array on stdout of attempt
  records (one per stage segment started in the window, in the workspace
  timezone, with its outcome and individual duration) instead of the table; the
  columns and grouping below do not apply. Normal completion prints the header row followed by zero or more
  sorted data rows on stdout (an empty window prints the header row only) and
  leaves stderr empty. Help prints text on stdout with stderr empty. Workspace,
  date, and missing-store errors print "performance: <reason>" on stderr with
  stdout empty. Argparse usage faults print usage and errors on stderr.

  Windowing, grouping, and keys (§9):
  Each segment's started_at timestamp is converted to the workspace timezone
  (Timezone: in CLAUDE.md). Segments whose start date falls in [--since, --until]
  inclusive are grouped by (period, runtime, model, effort, agent_type, workflow) sorted
  ascending; a run that ends after midnight stays in the period of its start.
  With --by day, period is the start date (YYYY-MM-DD); with --by week, a week
  starts on Monday and period is that week's Monday (YYYY-MM-DD). A segment with
  no runtime, model, effort, agent_type, or workflow is keyed by what is recorded, and an
  empty key field prints as "-".

  Columns (in order):
  - period: first day of the bucket (YYYY-MM-DD): the start day itself (--by day)
    or that week's Monday (--by week).
  - runtime: host runtime identifier recorded on the segment (claude, codex,
    cursor, or agy), or "-" when empty.
  - model: model identifier recorded on the segment, or "-" when empty.
  - effort: reasoning effort recorded on the segment, or "-" when empty.
  - agent_type: declared workspace agent type recorded on the segment, or "-"
    when empty.
  - workflow: workflow recorded on the segment, or "-" when empty (an older
    segment's unknown workflow stays empty).
  - runs: total stage segments started in the bucket for that key, including
    open (unended) segments.
  - completed: segments with outcome == "done".
  - human_review: segments with outcome == "human_review" or
    required_human_review set.
  - relaunched: segments with attempt > 1.
  - median_minutes: median whole minutes (int((ended_at - started_at) // 60s))
    across ended segments in the group, taking the lower middle value when the
    count is even, or "-" when no segment in the group has ended. Open segments
    count in runs but not in median_minutes.

Exit codes:
  0  Table printed on stdout (including when no segments match the window), or
     successful -h/--help. Stderr is empty.
  1  Refusal from the top-level chief-of-stuff router before launching the script
     if the store-backed engine virtualenv cannot be prepared ("refused: <reason>"
     on stderr), or an uncaught store migration/lock ServiceError (for example,
     when store.db is at an older revision while a live engine holds engine.lock).
  2  Usage or workspace fault: argparse error (missing --since, invalid --by
     choice, unknown option), invalid --since or --until ISO date, unreadable or
     invalid CLAUDE.md / ## Coordinator block, or missing <root>/store.db
     ("performance: no store at <path>" on stderr).

Side effects:
  No preview or --dry-run mode and no --commit gate exist, because the command
  writes no application, tracker, or forge records. Help (-h/--help) performs no
  workspace read, lock acquisition, or store access.

  Reads: <root>/CLAUDE.md (## Coordinator block and Timezone) via
  workspace.read_config, and <root>/store.db stage segments (segment and resume
  tables) via store_access.migrated_store and db.segments().

  Writes and locks: does not write segment rows, work items, decisions, daily
  tracker files, board pages, or forge issues, and starts no service or model
  session. Opening store.db via store_access.migrated_store acquires
  <root>/.chief-of-stuff/migrate.lock; when store.db is already at the current
  Alembic head it only reads, and when store.db is at an older revision while no
  engine holds <root>/.chief-of-stuff/engine.lock it migrates store.db to head
  before reading. When invoked through the top-level chief-of-stuff performance
  router, the router acquires supervisor.lock in the chief-of-stuff data
  directory to ensure the store-backed virtualenv before execing this script.

Examples:
  # Daily rows from 2026-10-01 through today in the current workspace
  chief-of-stuff performance --since 2026-10-01

  # Daily rows from 2026-10-01 through today in an explicit workspace
  chief-of-stuff performance --root /tmp/maple-workspace --since 2026-10-01

  # Weekly Monday-anchored rows for a closed date range
  chief-of-stuff performance --root /tmp/maple-workspace --since 2026-10-01 --until 2026-10-07 --by week

  # JSON attempt records for the same range, ungrouped
  chief-of-stuff performance --root /tmp/maple-workspace --since 2026-10-01 --until 2026-10-07 --raw
"""


def _cell(value: object) -> str:
    return "-" if value in (None, "") else str(value)


def _print_table(rows: list) -> None:
    headers = ("period", "runtime", "model", "effort", "agent_type", "workflow",
               "runs", "completed", "human_review", "relaunched", "median_minutes")
    cells = [headers] + [
        (_cell(r.period), _cell(r.runtime), _cell(r.model), _cell(r.effort), _cell(r.agent_type), _cell(r.workflow),
         _cell(r.runs), _cell(r.completed), _cell(r.human_review), _cell(r.relaunched),
         _cell(r.median_minutes))
        for r in rows
    ]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    for row in cells:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="chief-of-stuff performance",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    ap.add_argument(
        "-h", "--help", action="help",
        help="flag, default: off. Print this help text on stdout and exit 0 without reading CLAUDE.md or opening store.db.",
    )
    ap.add_argument(
        "--root", type=Path, default=Path("."),
        help=("filesystem path, default: . (current working directory). Selects the workspace root holding "
              "CLAUDE.md (for the Coordinator timezone) and store.db."),
    )
    ap.add_argument(
        "--since", required=True, metavar="DATE",
        help=("ISO date YYYY-MM-DD, default: none (required). Sets the inclusive start date of the reporting "
              "window in the workspace timezone, matched against each segment's started_at date."),
    )
    ap.add_argument(
        "--until", metavar="DATE",
        help=("ISO date YYYY-MM-DD, default: today in the workspace timezone. Sets the inclusive end date of "
              "the reporting window in the workspace timezone, matched against each segment's started_at date."),
    )
    ap.add_argument(
        "--by", choices=("day", "week"), default="day",
        help=("grouping period {day,week}, default: day. Groups segments into daily buckets (period is the "
              "segment's start date) or Monday-anchored weekly buckets (period is that week's Monday)."),
    )
    ap.add_argument(
        "--raw", action="store_true",
        help=("flag, default: off. Print JSON attempt records, preserving each outcome and individual duration, "
              "instead of the grouped table."),
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        cfg = read_config(root)
        since = date.fromisoformat(args.since)
        until = date.fromisoformat(args.until) if args.until else datetime.now(cfg.zone).date()
    except (OSError, ConfigError, ValueError) as exc:
        print(f"performance: {exc}", file=sys.stderr)
        return 2
    path = root / "store.db"
    if not path.is_file():
        print(f"performance: no store at {path}", file=sys.stderr)
        return 2
    db = store_access.migrated_store(root)
    try:
        segments = db.segments()
        if args.raw:
            print(json.dumps([asdict(row) for row in records(segments)
                              if since <= datetime.fromisoformat(row.started_at).astimezone(cfg.zone).date() <= until]))
        else:
            _print_table(table(segments, since=since, until=until, by=args.by, zone=cfg.zone))
    finally:
        db.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
