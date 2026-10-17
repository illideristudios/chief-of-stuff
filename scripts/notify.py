#!/usr/bin/env python3
"""The coordinator's exception notifications, written where the configured notifier reads them (M6, #714, #92).

The md-notify adapter is optional and writes only when explicitly selected in workspace settings.

    python3 notify.py --root . sync
    python3 notify.py --root . add --kind awaiting --what "PR 0.23.0" [--message M] [--issue KEY] [--when now|YYYY-MM-DD HH:MM]

Only exception causes emit pings:
- a `human_review` stop (needs input)
- an open decision / gate timeout
- an unexpected failure (crashed/failed/timed_out worker or refused board/stage write)
Each store-backed ping links to its board item in the `Open` cell.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfoNotFoundError
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import store_access  # noqa: E402
except ModuleNotFoundError:
    store_access = None  # type: ignore[assignment]
from settings import Settings, SettingsError, load  # noqa: E402
from workspace import ConfigError, read_config  # noqa: E402

WHEN = "%Y-%m-%d %H:%M"
KINDS = {"awaiting": "🙋 Awaiting you", "stopped": "🛑 Session stopped", "gone": "👻 Session gone"}
TEMPLATE = """# chief-of-stuff notifications

Written by chief-of-stuff's `notify.py` for md-notify. Tap Complete or Ignore on a banner to park its row. Keep other notification tools from rewriting this queue.

| When (CT) | Title | Message | Open |
|---|---|---|---|
"""
PARKED = ("completed", "ignored")
_BOARD_URL_RE = re.compile(r"^\s*-\s*Board:.*?(https?://[^\s)]+)", re.MULTILINE)


def clean(text: str) -> str:
    """One line, safe for md-notify's row filter: no `|`, no `---`, no `When (CT)`."""
    t = " ".join(str(text).split()).replace("|", "/")
    return re.sub(r"-{3,}", "—", t).replace("When (CT)", "When CT")


def event_title(kind: str, what: str, now: datetime) -> str:
    """The clock read is in the title, so one event notifies once and the same ask later notifies again."""
    return f"{KINDS[kind]} — {clean(what)} ({now:%H:%M})"


@dataclass(frozen=True)
class Row:
    when: str
    title: str
    message: str = ""
    open_url: str = ""
    legacy_title: str = ""

    def line(self) -> str:
        open_cell = f" {clean(self.open_url)} " if self.open_url else "  "
        return f"| {self.when} | {clean(self.title)} | {clean(self.message)} |{open_cell}|"


@dataclass
class Changes:
    lines: list[str] = field(default_factory=list)


def _cells(line: str) -> list[str]:
    body = line[1:]
    body = body[:-1] if body.endswith("|") else body
    return [c.strip() for c in body.split("|")]


def _is_data(line: str) -> bool:
    if not line.startswith("|") or "---" in line or "When (CT)" in line:
        return False
    cells = _cells(line)
    return len(cells) > 1 and cells[0] not in ("", "Every")


def _regions(lines: list[str]) -> list[str]:
    """Per line: `active`, `recurring` or `parked`, by the heading above it, the way md-notify reads it."""
    out, current = [], "active"
    for line in lines:
        if line.strip().startswith("#"):
            name = line.strip().lstrip("#").strip().lower()
            current = "parked" if name in PARKED else "recurring" if name == "recurring" else "active"
        out.append(current)
    return out


def board_base_url(root: Path, settings: Settings) -> str:
    claude_md = root / "CLAUDE.md"
    if claude_md.is_file():
        m = _BOARD_URL_RE.search(claude_md.read_text(encoding="utf-8"))
        if m:
            return m.group(1).rstrip("/")
    port = getattr(getattr(settings, "pages", None), "port", 7700) or 7700
    return f"http://127.0.0.1:{port}"


def board_item_url(root: Path, settings: Settings, issue_key: str) -> str:
    if not issue_key:
        return ""
    return f"{board_base_url(root, settings)}/issues/{quote(issue_key, safe="")}"


def _short_key(issue_key: str) -> str:
    return f"#{issue_key.rpartition('#')[2]}" if "#" in issue_key else issue_key


def _parse_stamp(raw: str | None, zone, fallback: datetime) -> datetime:
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=zone)
            return dt.astimezone(zone)
        except ValueError:
            pass
    return fallback


def _recovered(message: str, issue_key: str, stamp: datetime, db, segments: list, zone, now: datetime) -> bool:
    """Whether the thing a log line reports has since come right. Only the failed operation's own state says so."""
    state = db.resume(issue_key)
    state = state if isinstance(state, dict) else {}
    lowered = message.casefold()
    if lowered.startswith("stage write"):
        return not state.get("pending_stage")
    if "comment failed" in lowered:
        return not state.get("decision_comments")
    # Anything else stopped a worker run, which a later successful run of the issue recovers.
    return any(s.outcome == "done" and _parse_stamp(s.ended_at or s.started_at, zone, now) > stamp for s in segments)


def store_rows(root: Path, cfg, settings: Settings, now: datetime) -> list[Row]:
    """Build exception notification rows from the store: human_review stops, open decisions, unexpected failures."""
    db_path = root / "store.db"
    if not db_path.is_file():
        return []
    access = store_access
    if access is None:
        import store_access as access  # noqa: E402
    db = access.migrated_store(root)
    try:
        rows: list[Row] = []
        items = db.items()
        segments = db.segments()
        decisions = db.decisions()
        opened = {i.issue_key for i in items if i.forge_state != "closed"}
        logs = db.log()
        segs_by_key: dict[str, list] = {}
        for seg in segments:
            segs_by_key.setdefault(seg.issue_key, []).append(seg)

        for item in items:
            if item.forge_state == "closed":
                continue
            item_segs = segs_by_key.get(item.issue_key, [])
            latest = item_segs[-1] if item_segs else None
            hr_segs = [latest] if latest is not None and latest.outcome == "human_review" else []
            if item.human_review or hr_segs:
                stamp_raw = ((hr_segs[-1].ended_at or hr_segs[-1].started_at) if hr_segs
                             else (db.resume(item.issue_key) or {}).get("human_review_since"))
                stamp = _parse_stamp(stamp_raw, cfg.zone, now)
                short = _short_key(item.issue_key)
                what = f"{short} {item.title}".strip()
                rows.append(
                    Row(
                        now.strftime(WHEN),
                        event_title("awaiting", what, stamp) if stamp_raw else f"{KINDS["awaiting"]} — {what} (hold)",
                        f"Needs input on {what}",
                        open_url=board_item_url(root, settings, item.issue_key),
                    )
                )

        for dec in decisions:
            if dec.issue_key not in opened or dec.status not in ("open", "blocked"):
                continue
            stamp = _parse_stamp(dec.created_at, cfg.zone, now)
            short = _short_key(dec.issue_key)
            what = f"{short} {dec.question}".strip()
            rows.append(
                Row(
                    now.strftime(WHEN),
                    event_title("awaiting", what, stamp),
                    f"Decision needed ({dec.topic}): {dec.question}",
                    open_url=board_item_url(root, settings, dec.issue_key),
                )
            )

        for seg in segments:
            if seg.issue_key not in opened or seg.outcome not in ("failed", "crashed", "timed_out"):
                continue
            stamp = _parse_stamp(seg.ended_at or seg.started_at, cfg.zone, now)
            if any(s.outcome == "done" and _parse_stamp(s.ended_at or s.started_at, cfg.zone, now) > stamp
                   for s in segs_by_key.get(seg.issue_key, [])):
                continue
            short = _short_key(seg.issue_key)
            resume = db.resume(seg.issue_key)
            runner = ""
            if isinstance(resume, dict):
                runner = str((resume.get("runs") or {}).get(f"{seg.stage}-{seg.attempt}", {}).get("runner") or "")
            stamp = _parse_stamp(seg.ended_at or seg.started_at, cfg.zone, now)
            what = f"{runner or short} ({seg.outcome})"
            rows.append(
                Row(
                    now.strftime(WHEN),
                    event_title("stopped", what, stamp),
                    f"{short} {seg.stage} attempt {seg.attempt} {seg.outcome}",
                    open_url=board_item_url(root, settings, seg.issue_key),
                )
            )

        for entry in logs:
            lowered = entry.message.casefold()
            if entry.issue_key not in opened or not any(word in lowered for word in ("failed", "refused", "escalate: retry limit")):
                continue
            stamp = _parse_stamp(entry.at, cfg.zone, now)
            if _recovered(entry.message, entry.issue_key, stamp, db, segs_by_key.get(entry.issue_key, []), cfg.zone, now):
                continue
            short = _short_key(entry.issue_key)
            what = f"{short} {entry.message}"
            rows.append(
                Row(
                    now.strftime(WHEN),
                    event_title("stopped", what, stamp),
                    entry.message,
                    open_url=board_item_url(root, settings, entry.issue_key),
                )
            )
        return rows
    finally:
        db.engine.dispose()


class Notifier(Protocol):
    def add(self, row: Row) -> Changes: ...
    def sync(self, one_shot: list[Row], recurring: list[Row], now: datetime) -> Changes: ...


class Off:
    def add(self, row: Row) -> Changes:
        return Changes()

    def sync(self, one_shot: list[Row], recurring: list[Row], now: datetime) -> Changes:
        return Changes()


class MdNotifyQueue:
    """md-notify's queue file, selected explicitly by workspace settings."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    @contextmanager
    def lock(self, timeout: float = 5):
        """Shared with md-notify: flock the persistent sibling before reading or replacing the queue."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(f".{self.path.name}.lock").open("a") as handle:
            until = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= until:
                        raise TimeoutError(f"notification queue is locked: {self.path}") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self) -> list[str]:
        return (self.path.read_text() if self.path.is_file() else TEMPLATE).split("\n")

    def _write(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = self.path.stat().st_mode & 0o777 if self.path.exists() else 0o644
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(lines))
            os.chmod(tmp, mode)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @staticmethod
    def _titles(lines: list[str]) -> set[str]:
        return {_cells(x)[1] for x in lines if _is_data(x)}

    @staticmethod
    def _table_end(lines: list[str], region: str) -> int | None:
        """The index after the last row of the first table in `region`, or None when it has none."""
        regions, start = _regions(lines), None
        for i, line in enumerate(lines):
            if regions[i] == region and line.startswith("|") and not line.strip().startswith("#"):
                start = i
                break
        if start is None:
            return None
        end = start
        while end + 1 < len(lines) and lines[end + 1].startswith("|"):
            end += 1
        return end + 1

    def _insert(self, lines: list[str], row: Row, region: str) -> None:
        at = self._table_end(lines, region)
        if at is None:
            first_heading = next((i for i, x in enumerate(lines) if x.startswith("## ")), len(lines))
            lines[first_heading:first_heading] = ["| When (CT) | Title | Message | Open |", "|---|---|---|---|", ""]
            at = first_heading + 2
        lines.insert(at, row.line())

    def add(self, row: Row) -> Changes:
        with self.lock():
            return self._add(row)

    def _add(self, row: Row) -> Changes:
        lines = self._read()
        if clean(row.title) in self._titles(lines):
            return Changes([f"notify: already there {clean(row.title)}"])
        self._insert(lines, row, "active")
        self._write(lines)
        return Changes([f"notify: added {clean(row.title)}"])

    def sync(self, one_shot: list[Row], recurring: list[Row] | None = None, now: datetime | None = None) -> Changes:
        with self.lock():
            return self._sync(one_shot)

    def _sync(self, one_shot: list[Row]) -> Changes:
        lines = self._read()
        said: list[str] = []
        created = not self.path.is_file()
        for row in one_shot:
            if clean(row.title) not in self._titles(lines):
                self._insert(lines, row, "active")
                said.append(f"notify: added {clean(row.title)}")
        if said or created:
            self._write(lines)
        return Changes(said)


def adapter_for(settings: Settings, root: Path) -> Notifier:
    if settings.notify.adapter == "md-notify":
        return MdNotifyQueue(settings.notify.queue_path(root))
    return Off()


def configuration(root: Path):
    try:
        cfg = read_config(root)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"invalid workspace timezone: {exc}") from exc
    return cfg, load(root, cfg.settings_path)


def sync_workspace(root: Path, now: datetime | None = None) -> Changes:
    """One reconciliation, shared by the CLI and independent service. Off writes nothing."""
    cfg, settings = configuration(root)
    if settings.notify.adapter == "off":
        return Changes(["notify: off"])
    now = (now or datetime.now(cfg.zone)).astimezone(cfg.zone).replace(second=0, microsecond=0)
    rows = store_rows(root, cfg, settings, now)
    return adapter_for(settings, root).sync(rows, [], now)


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="workspace root holding CLAUDE.md")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="one event notification")
    add.add_argument("--kind", choices=sorted(KINDS), required=True)
    add.add_argument("--what", required=True, help="what it is about; goes in the title")
    add.add_argument("--message", default="", help="the banner's body; defaults to --what")
    add.add_argument("--issue", default="", help="optional issue key to link in the Open cell")
    add.add_argument("--when", default="now", help="now, or YYYY-MM-DD HH:MM in the workspace timezone")
    sub.add_parser("sync", help="exception notifications from the store")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    if args.cmd == "sync":
        try:
            got = sync_workspace(root, now)
        except (OSError, ValueError, ConfigError, SettingsError) as e:
            print(f"notify: {e}", file=sys.stderr)
            return 2
        print("\n".join(got.lines) or "notify: nothing to change")
        return 0
    try:
        cfg, settings = configuration(root)
    except (OSError, ConfigError, SettingsError) as e:
        print(f"notify: {e}", file=sys.stderr)
        return 2
    if settings.notify.adapter == "off":
        print("notify: off")
        return 0
    now = (now or datetime.now(cfg.zone)).astimezone(cfg.zone).replace(second=0, microsecond=0)
    queue = adapter_for(settings, root)
    try:
        when = now
        if args.when != "now":
            try:
                when = datetime.strptime(args.when, WHEN).replace(tzinfo=cfg.zone)
            except ValueError:
                print(f"notify: --when {args.when!r} is not now or YYYY-MM-DD HH:MM", file=sys.stderr)
                return 2
            if when < now:
                print(f"notify: --when {args.when} has passed; a past row resurfaces until tapped, so use --when now",
                      file=sys.stderr)
                return 2
        open_url = board_item_url(root, settings, args.issue) if args.issue else ""
        got = queue.add(Row(when.strftime(WHEN), event_title(args.kind, args.what, now),
                            args.message or args.what, open_url=open_url))
    except OSError as e:
        print(f"notify: {e}", file=sys.stderr)
        return 2
    print("\n".join(got.lines) or "notify: nothing to change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
