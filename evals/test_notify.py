"""Unit tests for scripts/notify.py: exception-only notifications from the store (M6 item 3, G1–G5, #714, #92)."""

import io
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import notify as nt  # noqa: E402
import store  # noqa: E402

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 23, 14, 0, tzinfo=CT)
QUEUE = "notes/NOTIFICATIONS.md"
KEY = "github:example/repo#12"

CLAUDE = """# Workspace

## Coordinator

- User: Robin
- Daily log dir: `daily/`
- Tracker: `daily/<date>-tracker.md`
- Timezone: America/Chicago
- Deadlines:
  - Early: 2026-09-23 23:59
  - Final: 2026-09-27 12:00
- Board: Artifact tool; URL https://claude.ai/artifact/board-7
- Settings: `chief-of-stuff.toml`
"""

TOML = f'[notify]\nadapter = "md-notify"\nqueue = "{QUEUE}"\n'


def md_notify_entries(text: str) -> list[tuple[str, str]]:
    """`NotificationSchedule.parse`, ported: (when, title) for every row md-notify would consider firing."""
    out, excluded = [], False
    for line in text.split("\n"):
        trimmed = line.strip(" ")
        if trimmed.startswith("#"):
            excluded = trimmed.lstrip("#").strip(" ").lower() in ("completed", "ignored")
            continue
        if excluded or not line.startswith("|") or "---" in line or "When (CT)" in line:
            continue
        body = line[1:]
        body = body[:-1] if body.endswith("|") else body
        cells = [c.strip(" ") for c in body.split("|")]
        when = cells[0] if cells else ""
        title = cells[1] if len(cells) > 1 else ""
        if not when:
            continue
        try:
            datetime.strptime(when, "%Y-%m-%d %H:%M")
            out.append((when, title))
            continue
        except ValueError:
            pass
        if re.fullmatch(r"(?i)(daily|mon|tue|wed|thu|fri|sat|sun)\s+([01]?\d|2[0-3]):[0-5]\d", when):
            out.append((when, title))
    return out


class Workspace:
    def __init__(self, claude: str = CLAUDE, toml: str | None = TOML):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "CLAUDE.md").write_text(claude)
        if toml is not None:
            (self.root / "chief-of-stuff.toml").write_text(toml)
        store.migrate(self.root / "store.db")
        self.db = store.Store(self.root / "store.db", clock=lambda: NOW)

    @property
    def queue(self) -> Path:
        return self.root / QUEUE

    def run(self, *argv: str, now: datetime = NOW) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = nt.main(["--root", str(self.root), *argv], now=now)
        return code, buf.getvalue()


class NotifyTest(unittest.TestCase):
    def ws(self, **kw) -> Workspace:
        w = Workspace(**kw)
        self.addCleanup(w.db.engine.dispose)
        self.addCleanup(w.tmp.cleanup)
        return w

    def test_notify_does_not_import_tracker_read(self):
        # Compliance G5: notify.py stops importing tracker_read.
        self.assertNotIn("tracker_read", nt.__dict__)

    def test_sync_emits_no_deadline_warnings_or_day_open_close_rows(self):
        # Compliance G3, G4: deadline warnings and day open/close recurring pings are removed.
        w = self.ws()
        code, out = w.run("sync")
        self.assertEqual(code, 0, out)
        text = w.queue.read_text()
        self.assertIn("md-notify", text)
        self.assertIn("| When (CT) | Title | Message | Open |", text)
        self.assertEqual(md_notify_entries(text), [])
        self.assertNotIn("⏰", text)
        self.assertNotIn("☀️", text)
        self.assertNotIn("🌙", text)

    def test_sync_emits_exception_pings_from_the_store_and_links_the_board_item(self):
        # Compliance G1, G2: notify.py reads the store (human_review stops, open decisions, unexpected failures)
        # and links to the board item in the Open cell.
        w = self.ws()
        w.db.upsert_item(KEY, title="Upload size limit", stage="review", forge_state="open", human_review=True)
        w.db.record_segment(
            KEY, "review", 1,
            started_at="2026-09-23T13:00:00-05:00",
            ended_at="2026-09-23T13:30:00-05:00",
            outcome="human_review",
            runner="rev-1",
        )
        key2 = "github:example/repo#15"
        w.db.upsert_item(key2, title="Cache warmup", stage="plan", forge_state="open")
        w.db.add_decision(key2, "plan", "Split cache pool?")
        key3 = "github:example/repo#18"
        w.db.upsert_item(key3, title="Auth token", stage="implement", forge_state="open")
        w.db.record_segment(
            key3, "implement", 1,
            started_at="2026-09-23T13:10:00-05:00",
            ended_at="2026-09-23T13:45:00-05:00",
            outcome="crashed",
            runner="impl-3",
        )
        w.db.log_event(key3, "stage write failed: label offline")

        code, out = w.run("sync")
        self.assertEqual(code, 0, out)
        text = w.queue.read_text()
        entries = md_notify_entries(text)
        titles = [t for _, t in entries]
        self.assertTrue(any("Upload size limit" in t or "#12" in t for t in titles), titles)
        self.assertTrue(any("Split cache pool?" in t or "#15" in t for t in titles), titles)
        self.assertTrue(any("impl-3" in t or "stage write failed" in t or "#18" in t for t in titles), titles)
        # Every store-backed exception row links to its board item in the Open cell:
        data_rows = [nt._cells(line) for line in text.split("\n") if nt._is_data(line)]
        self.assertTrue(data_rows)
        for cells in data_rows:
            self.assertTrue(cells[3].startswith("http"), f"expected board item URL in Open cell, got {cells!r}")
            self.assertIn("/issues/", cells[3])

    def test_second_sync_changes_nothing(self):
        w = self.ws()
        w.db.upsert_item(KEY, title="Upload size limit", stage="review", forge_state="open", human_review=True)
        w.run("sync")
        before = w.queue.read_bytes()
        code, out = w.run("sync")
        self.assertEqual((code, out.strip()), (0, "notify: nothing to change"))
        self.assertEqual(w.queue.read_bytes(), before)

    def test_refused_stage_or_board_write_emits_an_exception_ping(self):
        # #714: refused board or stage writes notify the user as an unexpected failure.
        w = self.ws()
        code, out = w.run("add", "--kind", "stopped", "--what", "stage write refused on #12",
                          "--message", "Kanban state changed outside the store", "--issue", KEY)
        self.assertEqual(code, 0, out)
        text = w.queue.read_text()
        entries = md_notify_entries(text)
        self.assertEqual(len(entries), 1)
        self.assertIn("stage write refused on #12", entries[0][1])
        data_rows = [nt._cells(line) for line in text.split("\n") if nt._is_data(line)]
        self.assertIn("/issues/", data_rows[0][3])

    def test_add_goes_in_the_active_table(self):
        w = self.ws()
        w.run("sync")
        code, out = w.run("add", "--kind", "awaiting", "--what", "CIMP 0.21.0")
        self.assertEqual(code, 0, out)
        lines = w.queue.read_text().split("\n")
        self.assertIn(("2026-09-23 14:00", "🙋 Awaiting you — CIMP 0.21.0 (14:00)"), md_notify_entries("\n".join(lines)))

    def test_kinds(self):
        for kind, head in (("awaiting", "🙋 Awaiting you"), ("stopped", "🛑 Session stopped"), ("gone", "👻 Session gone")):
            with self.subTest(kind=kind):
                self.assertEqual(nt.event_title(kind, "impl02", NOW), f"{head} — impl02 (14:00)")

    def test_a_title_anywhere_is_not_added_again(self):
        """Snooze rewrites When, and Complete and Ignore park a row: the Title is the only stable key."""
        title = "🙋 Awaiting you — CIMP 0.21.0 (14:00)"
        for section, row in (("## Completed", f"| 2026-09-23 14:00 | {title} | m | |"),
                             ("## Ignored", f"| 2026-09-23 14:00 | {title} | m | |"),
                             ("", f"| 2026-09-23 14:30 | {title} | m | | 1 |")):
            with self.subTest(section=section or "snoozed"):
                w = self.ws()
                w.queue.parent.mkdir(parents=True)
                header = "| When (CT) | Title | Message | Open | Snoozes |\n|---|---|---|---|---|\n"
                w.queue.write_text(f"# Q\n\n{header}{row if not section else ''}\n\n{section}\n{header if section else ''}{row if section else ''}\n")
                before = w.queue.read_bytes()
                code, out = w.run("add", "--kind", "awaiting", "--what", "CIMP 0.21.0")
                self.assertEqual(code, 0)
                self.assertIn("already there", out)
                self.assertEqual(w.queue.read_bytes(), before)

    def test_cells_are_made_safe_for_the_parser(self):
        w = self.ws()
        w.run("add", "--kind", "awaiting", "--what", "a | b\nc --- d When (CT) e", "--message", "x|y\n---z")
        entries = [t for _, t in md_notify_entries(w.queue.read_text()) if "Awaiting" in t]
        self.assertEqual(len(entries), 1, w.queue.read_text())
        self.assertNotIn("|", entries[0])
        self.assertNotIn("When (CT)", entries[0])

    def test_everything_else_is_preserved(self):
        w = self.ws()
        w.queue.parent.mkdir(parents=True)
        original = ("# Mine\n\nSome prose I wrote.\n\n| When (CT) | Title | Message | Open | Snoozes |\n|---|---|---|---|---|\n"
                    "| 2026-09-24 09:00 | Dentist | go | | 2 |\n\n## Completed\n| When (CT) | Title | Message | Open |\n|---|---|---|---|\n"
                    "| 2026-09-20 09:00 | Old | done | |\n")
        w.queue.write_text(original)
        w.run("add", "--kind", "gone", "--what", "impl02")
        after = w.queue.read_text().split("\n")
        added = [x for x in after if "Session gone" in x]
        self.assertEqual(len(added), 1)
        self.assertEqual([x for x in after if x not in added], original.split("\n"))
        self.assertEqual(after.index(added[0]), original.split("\n").index("| 2026-09-24 09:00 | Dentist | go | | 2 |") + 1)

    def test_a_later_when(self):
        w = self.ws()
        w.run("add", "--kind", "awaiting", "--what", "x", "--when", "2026-09-23 18:30")
        self.assertIn(("2026-09-23 18:30", "🙋 Awaiting you — x (14:00)"), md_notify_entries(w.queue.read_text()))

    def test_a_past_when_is_refused(self):
        w = self.ws()
        code, _ = w.run("add", "--kind", "awaiting", "--what", "x", "--when", "2026-09-23 09:00")
        self.assertEqual(code, 2)
        self.assertFalse(w.queue.exists())

    def test_off(self):
        for toml in (None, '[notify]\n', '[notify]\nadapter = "off"\n'):
            with self.subTest(toml=toml):
                w = self.ws(toml=toml)
                for argv in (("sync",), ("add", "--kind", "awaiting", "--what", "x")):
                    code, out = w.run(*argv)
                    self.assertEqual((code, out.strip()), (0, "notify: off"))
                self.assertFalse(w.queue.exists())

    def test_no_settings_line_is_off(self):
        w = self.ws(claude=CLAUDE.replace("- Settings: `chief-of-stuff.toml`\n", ""))
        self.assertEqual(w.run("sync")[1].strip(), "notify: off")

    def test_bad_settings_exit_two(self):
        w = self.ws(toml='[notify]\nday_open = "6am"\n')
        self.assertEqual(w.run("sync")[0], 2)

    def test_the_write_is_atomic(self):
        """One os.replace from a temp file beside the queue: md-notify never reads half a file."""
        w = self.ws()
        calls = []
        real = nt.os.replace

        def spy(src, dst):
            calls.append((Path(src).resolve().parent, Path(dst).resolve()))
            return real(src, dst)

        nt.os.replace = spy
        self.addCleanup(setattr, nt.os, "replace", real)
        w.run("sync")
        self.assertEqual(calls, [(w.queue.parent.resolve(), w.queue.resolve())])


if __name__ == "__main__":
    unittest.main()
