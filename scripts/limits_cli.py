"""Take and record one unbilled reading of every runtime's account limits."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from limits_history import LimitsHistory
from limits_probe import read
from runtimes import NAMES
from settings import load
from workspace import read_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        cfg = read_config(args.root)
        settings = load(args.root, cfg.settings_path)
        rows = [r for runtime in NAMES for r in read(runtime, settings.workers.claude_as_profile if runtime == "claude" else "")]
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
