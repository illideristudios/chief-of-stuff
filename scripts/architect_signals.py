"""Architect signals: which unassigned compliance gaps still need a forge issue (#1180).

Use Cases; no I/O. The architect writes compliance rows; the coordinator's ledger of acted-on
ids is plain data handed in. A signal key `architect-signal:<gap-id>@<sha>` makes a repeated
signal file nothing twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Collection, Mapping, Sequence
from md import split_row

KEY_PREFIX = "architect-signal:"


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
        cells = [cell.strip() for cell in split_row(line)]
        if not line.strip().startswith("|") or len(cells) != 7 or not re.fullmatch(r"C\d+", cells[0]):
            continue
        gap_id, section, severity, file_line, gap, owner, status = cells
        status = status.lower()
        if owner.lower() != "unassigned" or status != "open":
            continue
        out.append(GapRow(
            id=gap_id,
            section=section,
            severity=severity,
            file_and_line=file_line,
            gap=gap,
            owner=owner,
            status=status,
        ))
    return out


def pending(rows: Sequence[GapRow], acted_ids: Collection[str] | Mapping[str, Any]) -> list[GapRow]:
    """Gaps that still need a signal: unassigned open rows whose ids are not yet acted on."""
    acted = set(acted_ids)
    return [row for row in rows if row.id not in acted]


def signal_key(gap_id: str, against: str) -> str:
    sha = against.strip()
    if not re.fullmatch(r"C\d+", gap_id) or not re.fullmatch(r"[0-9a-fA-F]{3,64}", sha):
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
        if needle in body.splitlines():
            number = row.get("number")
            if isinstance(number, int):
                return number
            try:
                return int(number)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
    return None
