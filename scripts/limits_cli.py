"""Take and record one unbilled reading of every runtime's account limits."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from limits_history import LimitsHistory
from limits_probe import read_all
from settings import load
from workspace import settings_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        claude = args.root / "CLAUDE.md"
        settings = load(args.root, settings_path(claude.read_text() if claude.is_file() else ""))
        rows = read_all(settings.workers)
        history = LimitsHistory()
        try:
            history.record(rows)
        finally:
            history.engine.dispose()
    except Exception as exc:
        print(f"limits: {exc}", file=sys.stderr)
        return 1
    print("runtime  account  limit  used/cap  reset  observed")
    for r in rows:
        value = f"unknown: {r.unknown}" if r.unknown else f"{r.used:g}/{r.cap:g}{r.unit}"
        print(f"{r.runtime}  {r.account or 'default'}  {r.limit}  {value}  "
              f"{r.resets_at.isoformat() if r.resets_at else 'unknown'}  {r.read_at.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
