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


def file_signal(root: Path, gap_id: str, *, against: str, cfg, token=None, fetch=None, create=None) -> str:
    """Search all open and closed issues, file once, and record the coordinator ledger.

    --root must name the shared absolute coordinator root, even when the filing worker
    has its own worker tree. Its compliance.md is the source; its ledger is
    .chief-of-stuff/architect-signals.json. Search, create and record run under one lock.

    A durable filing intent retains the original architect-signal:<id>@<sha> key
    across crashes, even if the compliance commit changes. Dedupe requires that key
    on its own line. An acted gap is not pending.

    The forge has no idempotency token. Unconfirmed creates are reconciled with the
    forge and never blindly retried: only a positive dedupe hit permits recovery.
    An empty search or failed read must never authorize a second POST. Report an
    unconfirmed create and stop; never bypass it or choose a new key.
    This command never edits architecture/compliance.md; only the architect may.
    """
    import backlog
    from backlog_ref import home_of, IssueRef
    fetch, create = fetch or backlog.issues, create or backlog.create
    with store.exclusive(root):
        data = store.load_ledger(root)
        if gap_id in data["acted"]:
            return data["acted"][gap_id]["issue_key"]
        inflight = data.setdefault("filing", {})
        intent = inflight.get(gap_id)
        if intent is None:
            rows = {row.id: row for row in signals.unassigned_gaps(store.compliance_text(root))}
            gap = rows[gap_id]
            intent = {"signal_key": signals.signal_key(gap_id, against),
                      "title": f"{gap.id}: {gap.gap}",
                      "body": signals.issue_body(gap=gap, against=against, source="architecture/compliance.md"),
                      "creating": False}
            inflight[gap_id] = intent
            store.save_ledger(root, data)
        got = fetch(cfg, token=token, state="all")
        if got.error:
            raise RuntimeError(f"cannot dedupe architect signal: {got.error}")
        number = signals.find_existing([{"number": i.iid, "body": i.body} for i in got.issues], intent["signal_key"])
        if number is None:
            if intent["creating"]:
                raise RuntimeError(f"{gap_id} creation is unconfirmed; preserving its key, no second issue filed")
            intent["creating"] = True
            store.save_ledger(root, data)  # durable before the remote side effect
            made = create(cfg, intent["title"], body=intent["body"], token=token, commit=True)
            if made.error or not made.done or not made.iid:
                raise RuntimeError(f"{gap_id} creation is unconfirmed: {made.error}")
            number = made.iid
        host, repo = home_of(cfg)
        key = IssueRef(repo, number, host).store_key
        data["acted"][gap_id] = {"issue_key": key, "signal_key": intent["signal_key"]}
        del inflight[gap_id]
        store.save_ledger(root, data)
        return key


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

    file_p = sub.add_parser("file", help="dedupe, file and record under the coordinator's ledger lock",
                            description=file_signal.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    file_p.add_argument("gap_id")
    file_p.add_argument("--against", required=True)

    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.cmd == "file":
            from startup import workspace_forge
            _, _, forge = workspace_forge(root)
            if forge is None:
                raise ValueError("workspace has no configured backlog")
            print(file_signal(root, args.gap_id, against=args.against, cfg=forge.home, token=forge.token))
            return 0
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
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"architect-signals: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
