#!/usr/bin/env python3
"""Read a pipeline's status or branch CI from the configured forge.

For GitHub branches, status is an aggregate of every workflow's latest run at the head;
ID and URL identify the newest run, whose status may differ from the aggregate.
GitLab branches report the latest pipeline at the head.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys

import forge
from _vendor.toon_format import encode as toon_encode
from forge_port import ForgeError
from workspace import ConfigError, read_config


def main(argv: list[str] | None = None) -> int:
    """Parse argv and print Pipeline TOON; return 0 on reads, 1 on read/config errors; raises SystemExit(2) on invalid argv."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True, help="workspace holding CLAUDE.md and its Backlog: line")
    selector = ap.add_mutually_exclusive_group(required=True)
    selector.add_argument("--id", type=int, help="GitLab pipeline ID or GitHub Actions run ID")
    selector.add_argument("--ref", help="branch head and CI: GitHub aggregates every workflow; "
                         "ID and URL identify the newest run; GitLab reads the latest pipeline")
    args = ap.parse_args(argv)
    if args.id is not None and args.id <= 0:
        ap.error("--id must be a positive integer")
    try:
        cfg = read_config(args.root)
        if cfg.backlog is None:
            raise ConfigError("no Backlog: line names the forge")
        adapter = forge.Forge(cfg.backlog)
        result = adapter.pipeline(args.id) if args.id is not None else adapter.branch_head(args.ref)
    except (OSError, ConfigError, ForgeError) as exc:
        print(f"pipeline: {exc}", file=sys.stderr)
        return 1
    print(toon_encode(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
