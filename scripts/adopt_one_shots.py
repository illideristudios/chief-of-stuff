#!/usr/bin/env python3
"""Reconcile the one-shot runs whose launcher died before reconciling them (#99).

After a coordinator restart, a tree can hold a finished run - its pid file's worker gone, its task
row still `running`, no launcher report copy - that nothing will ever answer for. This sweep
reconciles each such run from its own evidence (the result file, the changed files, the work
already in the tree), writes the report, and moves the row. One TOON list of the adopted reports;
`[]` when there is nothing to adopt. A worker still alive is left to finish; the next sweep adopts
it. Run it after a restart, before deciding what the orphaned rows need.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _vendor.toon_format import encode as toon_encode
from one_shot import adopt


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="workspace whose trees hold the runs")
    args = ap.parse_args(argv)
    try:
        adopted = adopt(args.root.resolve())
    except (OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(toon_encode(adopted) if adopted else "[]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
