"""Take and record one unbilled reading of every runtime's account limits."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from limits_history import LimitsHistory
from limits_probe import read_all
from settings import load
from workspace import settings_path

DESCRIPTION = (
    "Take and record one unbilled reading of every runtime's account limits.\n\n"
    "Use this to inspect locally observed quota before dispatch. These are readings from\n"
    "local session transcripts and logs, not a live provider query."
)

EPILOG = """\
When not to use:
  For workspace settings, runtime PATH, Health: target probes, and (advice) lines,
  use chief-of-stuff health. limits is not a connectivity or configuration health
  check and does not probe HTTP targets.

Output:
  Plain text on stdout (not TOON and not JSON). Normal completion leaves stderr
  empty. Command failures print "limits: <reason>" on stderr. Argparse usage
  faults print usage and errors on stderr. Help is text on stdout.

  After a header row, the command prints one line per limit reading. One runtime
  can yield multiple rows. Unknown readings keep exit code 0; the reason appears
  inline in the used/cap column as "unknown: <reason>", never as zero usage.

  Columns (space-separated):
  - runtime: host identifier (claude, codex, cursor, or agy).
  - account: profile or login name selected for that runtime; the internally empty
    default login is printed as default.
  - limit: window, model, or cap identifier from the local source, or limits when
    unavailable.
  - used/cap: numeric usage and cap with unit, e.g. 85/100%; or unknown: <reason>.
  - reset: reset time in ISO 8601, or unknown when absent.
  - observed: observation time in ISO 8601 (read_at). A known local event keeps its
    event time, so rerunning does not make old evidence fresh.

Exit codes:
  0  Readings recorded and printed, including when some or all are unknown; also
     successful -h/--help. Stderr is empty.
  1  Command failure such as unreadable or invalid settings, or shared-history
     creation, migration, or write failure; diagnosed on stderr as limits: <reason>.
  2  Argparse usage fault (unknown option, missing --root value, etc.); usage on
     stderr. Quota usage itself is never a nonzero status.

Side effects:
  No preview, dry run, or --commit gate: a normal invocation records immediately.
  Help performs no collection and writes no history.

  Reads: the workspace CLAUDE.md under --root; the Settings TOML named there when
  present (missing configuration uses defaults); local session logs for the launch
  account through session_logs.py. Claude reads structured events under
  $CLAUDE_CONFIG_DIR or ~/.claude, or $CLAUDE_AS_PROFILES_DIR/<profile> or
  ~/.claude-profiles/<profile> when [workers] claude_as_profile selects a profile.
  Other runtimes use their default login. This pass reads each runtime once under
  that account, not every account on the machine.

  Writes: creates, migrates, and updates the account-wide SQLite history at
  $XDG_STATE_HOME/chief-of-stuff/limits.sqlite (default
  ~/.local/state/chief-of-stuff/limits.sqlite). --root chooses account and
  configuration, not a workspace-local history file. The database is separate
  from the issue store. Repeated identical observations and unchanged unknown
  reasons may be deduplicated; recording does not promise a new row every time.

  Does not touch the issue store or forge, launch runtimes, or make billed model
  calls. The installed chief-of-stuff router may bootstrap its dependency venv
  before execing this script; that is separate from collection.

  [limits] warn_at in the Settings TOML is a finite fraction from 0 through 1,
  default 0.8. Downstream near-cap judgments compare observed used/cap or
  projected usage at reset with this threshold. This command validates settings,
  records, and prints readings; it does not print a threshold verdict, enforce a
  cap, or change exit status when usage is high. Unknown readings cannot
  establish a near-cap warning. Currently codex, cursor, and agy report
  unknown: read in 1.1 when no local numeric evidence exists.

Examples:
  chief-of-stuff limits --help
  chief-of-stuff limits
  chief-of-stuff limits --root /tmp/cedar-workspace
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chief-of-stuff limits",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help",
        help="flag, default: off. Print this help text on stdout and exit 0 without collecting or recording.",
    )
    parser.add_argument(
        "--root", type=Path, default=Path("."),
        help=("filesystem path, default: . (current working directory). Selects the workspace whose "
              "CLAUDE.md and named Settings TOML determine the launch account and [limits] warn_at."),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
