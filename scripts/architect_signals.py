"""Architect signals: unassigned compliance gaps become forge issues once (#1180).

Use Cases. The architect writes `architecture/compliance.md`; the coordinator keeps
`.chief-of-stuff/architect-signals.json` and never edits the compliance rows. A signal key
`architect-signal:<gap-id>@<sha>` in an issue body makes a repeated signal file nothing twice.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

LEDGER_NAME = "architect-signals.json"
KEY_PREFIX = "architect-signal:"
ROW = re.compile(
    r"^\|\s*(?P<id>C\d+)\s*\|\s*(?P<section>[^|]+)\|\s*(?P<severity>[^|]+)\|\s*"
    r"(?P<file>[^|]+)\|\s*(?P<gap>[^|]+)\|\s*(?P<owner>[^|]+)\|\s*(?P<status>[^|]+)\|?\s*$"
)


@dataclass(frozen=True)
class GapRow:
    id: str
    section: str
    severity: str
    file_and_line: str
    gap: str
    owner: str
    status: str


def unassigned_gaps(text: str) -> list[GapRow]:
    """Open compliance rows whose owner is still `unassigned` — the coordinator's intake."""
    out: list[GapRow] = []
    for line in text.splitlines():
        m = ROW.match(line.strip())
        if not m:
            continue
        owner = m.group("owner").strip()
        status = m.group("status").strip().lower()
        if owner.lower() != "unassigned" or status != "open":
            continue
        out.append(GapRow(
            id=m.group("id").strip(),
            section=m.group("section").strip(),
            severity=m.group("severity").strip(),
            file_and_line=m.group("file").strip(),
            gap=m.group("gap").strip(),
            owner=owner,
            status=status,
        ))
    return out


def ledger_path(root: Path) -> Path:
    return root / ".chief-of-stuff" / LEDGER_NAME


def load_ledger(root: Path) -> dict[str, Any]:
    path = ledger_path(root)
    if not path.is_file():
        return {"acted": {}}
    data = json.loads(path.read_text())
    acted = data.get("acted") if isinstance(data, dict) else None
    return {"acted": dict(acted) if isinstance(acted, dict) else {}}


def record_acted(root: Path, gap_id: str, *, issue_key: str, signal_key: str) -> None:
    """Record that this gap row has been acted on. Never touches compliance.md. First write wins."""
    data = load_ledger(root)
    if gap_id in data["acted"]:
        return
    data["acted"][gap_id] = {"issue_key": issue_key, "signal_key": signal_key}
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def pending(root: Path, compliance_text: str) -> list[GapRow]:
    acted = load_ledger(root)["acted"]
    return [row for row in unassigned_gaps(compliance_text) if row.id not in acted]


def signal_key(gap_id: str, against: str) -> str:
    sha = against.strip()
    if not gap_id or not sha:
        raise ValueError("signal key needs a gap id and a commit")
    return f"{KEY_PREFIX}{gap_id}@{sha}"


def issue_body(*, gap: GapRow, against: str, source: str) -> str:
    key = signal_key(gap.id, against)
    return (
        f"## Outcome\n\n"
        f"Close the architecture compliance gap {gap.id} (§{gap.section}): {gap.gap}\n\n"
        f"## Why\n\n"
        f"Architect signal from `{source}` against `{against.strip()}`.\n"
        f"File and line: {gap.file_and_line}. Severity: {gap.severity}.\n\n"
        f"## Acceptance\n\n"
        f"- [ ] The gap {gap.id} is closed in `architecture/compliance.md` by the architect after the fix lands.\n\n"
        f"{key}\n"
    )


def find_existing(issues: Sequence[Mapping[str, Any]], key: str) -> int | None:
    """Return the issue number whose body already carries this signal key, if any."""
    needle = key.strip()
    if not needle:
        return None
    for row in issues:
        body = str(row.get("body") or "")
        if needle in body.splitlines() or needle in body:
            number = row.get("number")
            if isinstance(number, int):
                return number
            try:
                return int(number)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
    return None


def compliance_text(root: Path) -> str:
    path = root / "architecture" / "compliance.md"
    if not path.is_file():
        raise FileNotFoundError(f"no compliance record at {path}")
    return path.read_text()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
            rows = pending(root, compliance_text(root))
            if args.json:
                print(json.dumps([row.__dict__ for row in rows], indent=2))
            else:
                for row in rows:
                    print(f"{row.id}\t{row.section}\t{row.severity}\t{row.gap}")
            return 0
        if args.cmd == "mark":
            record_acted(root, args.gap_id, issue_key=args.issue_key, signal_key=args.signal_key)
            print(f"marked {args.gap_id}")
            return 0
        if args.cmd == "key":
            print(signal_key(args.gap_id, args.against))
            return 0
        if args.cmd == "body":
            rows = {row.id: row for row in unassigned_gaps(compliance_text(root))}
            if args.gap_id not in rows:
                raise KeyError(f"no unassigned open gap {args.gap_id}")
            print(issue_body(gap=rows[args.gap_id], against=args.against,
                             source="architecture/compliance.md"), end="")
            return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"architect-signals: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
