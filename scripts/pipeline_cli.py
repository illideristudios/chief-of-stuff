#!/usr/bin/env python3
"""Read one pipeline or one branch head's CI from the workspace Backlog: forge.

Use it when the question is that pipeline's status. A failed status is still a successful read.
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

EPILOG = """
When not to use:
  To decide whether an open pull or merge request is ready to merge, use
  chief-of-stuff merge-ready. That verdict already includes the head pipeline
  plus conflicts, draft, review threads, and a stale pipeline sha. This command
  does not.

Output:
  TOON on stdout, one object, fields in this order: id (int, or null), ref
  (string), sha (string), status (string), url (string, "" when there is no
  pipeline). Stderr is empty on success.

  No pipeline (--ref and the head has no runs): exit 0, id: null, status: none,
  url: "". A missing --id is not this result; it is a read error.

  GitHub status, for the single --id run and for the --ref aggregate: passed
  when every included check completed with success, neutral, or skipped;
  failed when any included check completed with another conclusion; running when
  none failed and at least one has not completed. On --ref, a truncated workflow
  list is a read error, not a partial aggregate.

  For --ref on GitHub, status is the aggregate of every workflow's latest run at
  that head; ID and URL identify the newest run, whose own status may differ
  from the aggregate. For --ref on GitLab, the latest pipeline at that head sets
  id, url, and status together.

  GitLab status, for --id and for the latest --ref pipeline: success maps to
  passed; failed and canceled map to failed; skipped, pending, created,
  waiting_for_resource, waiting_for_callback, preparing, scheduled, manual,
  draft, and canceling stay as those words; any other word maps to running.

  Stderr: pipeline: … on a read or config error, and argparse usage text on a
  usage fault. A usage fault prints no TOON.

Exit codes:
  0  The read succeeded, including status: failed and status: none; --help.
  1  Config or forge read failed (no Backlog: line, missing GitLab credential,
     HTTP error, unreadable payload, truncated GitHub run list, branch name
     that is blank, untrimmed, ".", or ".."). Stdout empty.
  2  Usage fault: missing --root, missing both selectors, both --id and --ref,
     a non-integer --id, or --id that is not positive.

Side effects:
  Reads only. No dry run and no --commit, because nothing is written.

  Reads CLAUDE.md (the Backlog: line), then the forge. GitHub uses gh api.
  GitLab uses REST on that line's host, including a self-hosted host, with the
  credential backlog.token already resolves (not a flag). --id reads
  actions/runs/<id> or pipelines/<id>. --ref reads the branch, then that head's
  runs.

  Writes: none. Not the store, not the board cache, not the worktree, not the
  forge.

Examples:
  chief-of-stuff pipeline --root ~/cedar-desk --id 71
  chief-of-stuff pipeline --root ~/cedar-desk --ref main
  chief-of-stuff pipeline --root ~/cedar-desk --ref release/ledger
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chief-of-stuff pipeline",
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help",
                        help="flag, default: off. Print this help text on stdout and exit 0.")
    parser.add_argument("--root", type=Path, required=True,
                        help="path, default: none (required). Workspace whose CLAUDE.md Backlog: line selects the forge and repository.")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--id", type=int,
                          help="int, default: none (required with --ref). One GitLab pipeline or one GitHub Actions run. Mutually exclusive with --ref; must be > 0. Does not aggregate.")
    selector.add_argument("--ref",
                          help="string, default: none (required with --id). Branch name. GitHub: status is the aggregate of every workflow's latest run at that head; ID and URL identify the newest run, whose own status may differ. GitLab: the latest pipeline at that head (one pipeline, so id, url, and status agree). Mutually exclusive with --id.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse argv and print Pipeline TOON; return 0 on reads, 1 on read/config errors; raises SystemExit(2) on invalid argv."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.id is not None and args.id <= 0:
        parser.error("--id must be a positive integer")
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
