"""Command line for architect signals (#1180).

Frameworks and Drivers. Composes the use-case judgements with the compliance and ledger gateway.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import architect_signal_store as store
import architect_signals as signals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Unassigned compliance gaps become forge issues once; the coordinator ledger never edits compliance.md.")
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="cmd", required=True)

    pending_p = sub.add_parser("pending", help="list unassigned open gaps not yet in the ledger")
    pending_p.add_argument("--json", action="store_true")

    mark = sub.add_parser("mark", help="record a gap id as acted on (does not edit compliance.md)")
    mark.add_argument("gap_id")
    mark.add_argument("--issue-key", required=True)
    mark.add_argument("--signal-key", required=True)

    key_p = sub.add_parser("key", help="print the signal key for a gap id and commit")
    key_p.add_argument("gap_id")
    key_p.add_argument("--against", required=True)

    body_p = sub.add_parser("body", help="print an issue body for a pending gap")
    body_p.add_argument("gap_id")
    body_p.add_argument("--against", required=True)

    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.cmd == "pending":
            rows = signals.pending(
                signals.unassigned_gaps(store.compliance_text(root)),
                store.load_ledger(root)["acted"],
            )
            if args.json:
                print(json.dumps([row.__dict__ for row in rows], indent=2))
            else:
                for row in rows:
                    print(f"{row.id}\t{row.section}\t{row.severity}\t{row.gap}")
            return 0
        if args.cmd == "mark":
            store.record_acted(root, args.gap_id, issue_key=args.issue_key, signal_key=args.signal_key)
            print(f"marked {args.gap_id}")
            return 0
        if args.cmd == "key":
            print(signals.signal_key(args.gap_id, args.against))
            return 0
        if args.cmd == "body":
            rows = {row.id: row for row in signals.unassigned_gaps(store.compliance_text(root))}
            if args.gap_id not in rows:
                raise KeyError(f"no unassigned open gap {args.gap_id}")
            print(signals.issue_body(gap=rows[args.gap_id], against=args.against,
                                     source="architecture/compliance.md"), end="")
            return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"architect-signals: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
