"""Print the agent performance table from the store's stage segments (architecture §9)."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import store_access
from performance import table
from workspace import ConfigError, read_config


def _cell(value: object) -> str:
    return "-" if value in (None, "") else str(value)


def _print_table(rows: list) -> None:
    headers = ("period", "runtime", "model", "effort", "agent_type",
               "runs", "completed", "human_review", "relaunched", "median_minutes")
    cells = [headers] + [
        (_cell(r.period), _cell(r.runtime), _cell(r.model), _cell(r.effort), _cell(r.agent_type),
         _cell(r.runs), _cell(r.completed), _cell(r.human_review), _cell(r.relaunched),
         _cell(r.median_minutes))
        for r in rows
    ]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    for row in cells:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path("."), help="workspace root holding store.db and CLAUDE.md")
    ap.add_argument("--since", required=True, metavar="DATE", help="inclusive YYYY-MM-DD in the workspace zone")
    ap.add_argument("--until", metavar="DATE", help="inclusive YYYY-MM-DD; default today in the workspace zone")
    ap.add_argument("--by", choices=("day", "week"), default="day")
    args = ap.parse_args(argv)
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
        _print_table(table(db.segments(), since=since, until=until, by=args.by, zone=cfg.zone))
    finally:
        db.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
