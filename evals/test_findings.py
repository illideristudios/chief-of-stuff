"""Unit tests for scripts/findings.py: the audit and its watcher read one list of finding words.

`audit_tasks` starts each finding line with one of these words, and `start_coordinator`'s watcher forwards the
lines that start with a watched one. A finding the audit gains without a word here, or a word the watcher drops,
fails here instead of going unforwarded without anyone noticing.
"""

import re
import sys
import typing
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import audit_tasks as al  # noqa: E402
import findings  # noqa: E402
import start_coordinator as start  # noqa: E402

# The watcher's pattern as it was typed before it was built from `findings.WATCHED`.
LITERAL = re.compile(r"(?i)^(orphaned work:|stopped:|reopen:|issue:|merged:|lane:|kanban:|decision queue:|next decision:|overdue:|waiting:)")

TRACKER = """## Decision queue

| # | decision | why it is next | who is blocked |
|---|---|---|---|
| 1 | Pick a name | the launch waits on it | impl |
| 1 | **Pick a date** | — | — |
"""


def reopen() -> al.Reopen:
    return al.Reopen("Build it", "impl", "tree (feat)", "not on main")


# One finding of every kind the audit's report carries, and the word its line starts with.
FINDINGS = {
    al.Reopen: (findings.REOPEN, reopen()),
    al.Orphan: (findings.ORPHANED, al.Orphan("tree (feat)", "impl", "1 uncommitted file(s)", "is not in ## Sessions")),
    al.Unclaimed: (findings.UNCLAIMED, al.Unclaimed("tree (feat)", "1 uncommitted file(s)")),
    al.Stop: (findings.STOPPED, al.Stop("Build it", "blocked", "coordinator", "tree (feat)", "running 10:00")),
    al.QueueFault: (findings.QUEUE, al.QueueFault("row 1", "carries the number 1 twice, so there is no next one")),
    al.IssueFault: (findings.ISSUE, al.IssueFault("Build it", "no issue")),
    al.MergedFault: (findings.MERGED, al.MergedFault("Build it", "!12 merged into main abc1234")),
    al.LaneFault: (findings.LANE, al.LaneFault("Build it", "lane review but no stage")),
    al.KanbanFault: (findings.KANBAN, al.KanbanFault("Build it", "expected 03, found 00")),
    al.OverBudget: (findings.OVER, al.OverBudget("Build it", timedelta(hours=3), "S", timedelta(hours=1))),
}


def printed() -> list[tuple[str, str]]:
    """Every finding line the audit can print, with the word it should start with."""
    lines = [(word, str(finding)) for word, finding in FINDINGS.values()]
    lines += [(findings.REOPEN, line) for line in al.group_reopens([reopen(), reopen()])]
    faults, queue, _ = al.queue_faults(TRACKER)
    lines += [(findings.NEXT, line) for line in queue]
    lines += [(findings.QUEUE, str(fault)) for fault in faults]
    return lines


class VocabularyTest(unittest.TestCase):
    def test_every_finding_kind_the_report_carries_has_a_sample(self):
        hints = typing.get_type_hints(al.Report)
        kinds = {typing.get_args(hint)[0] for name, hint in hints.items() if name != "lines"}
        self.assertEqual(kinds, set(FINDINGS))

    def test_every_finding_line_starts_with_its_word(self):
        lines = printed()
        self.assertIn(findings.NEXT, {word for word, _ in lines})
        for word, line in lines:
            with self.subTest(line=line):
                self.assertTrue(line.startswith(word + " "), line)

    def test_every_word_the_audit_prints_is_watched_or_unwatched(self):
        self.assertFalse(set(findings.WATCHED) & set(findings.UNWATCHED))
        for word, _ in printed():
            with self.subTest(word=word):
                self.assertIn(word, findings.WATCHED + findings.UNWATCHED)

    def test_the_gaps_are_pinned(self):
        # `over budget:` and `unclaimed work:` are printed and not forwarded; `overdue:` and `waiting:` are
        # forwarded and never printed. Closing either gap is a behaviour change, and changes this test with it.
        words = {word for word, _ in printed()}
        self.assertEqual(findings.UNWATCHED, (findings.OVER, findings.UNCLAIMED))
        self.assertEqual(set(findings.WATCHED) - words, {"overdue:", "waiting:"})


class WatcherPatternTest(unittest.TestCase):
    OTHER = [
        "", "tasks=1 trees=0 reopen=0 orphaned=0 stopped=0", "main abc1234 even with origin/main",
        "tree (feat): on main, committed", "sha: Build it — 'xyz' is not a commit id; read as it was before",
        "ambiguous: Build it — 2 File ownership rows for impl and none names it; no tree attributed",
        "Reopen: x", "ORPHANED WORK: x", "Next Decision: 1 — x", "Overdue: x", "WAITING:", "lane:x", "issue:",
        " issue: indented", "issues: x", "reopen x", "orphaned  work: x", "orphaned-work: x", "decision queue x",
        "stopped:\ttab", "x reopen: later in the line", "Kanban: a Kelvin sign folds to k",
        "Merged: x", "merged x", "merges: x", "tasks=1 trees=0 reopen=0 orphaned=0 stopped=0 issues=0 merged=1",
    ]

    def test_the_watched_words_are_the_typed_ones_in_their_order(self):
        self.assertEqual(list(findings.WATCHED), LITERAL.pattern.removeprefix("(?i)^(").removesuffix(")").split("|"))

    def test_the_built_pattern_matches_what_the_typed_one_did(self):
        lines = [line for _, line in printed()] + self.OTHER
        lines += [case(word) for word in findings.WATCHED for case in (str, str.upper, str.title)]
        accepted = 0
        for line in lines:
            with self.subTest(line=line):
                old, new = LITERAL.match(line), start.FINDING.match(line)
                self.assertEqual(old and old.group(0), new and new.group(0))
                accepted += bool(new)
        self.assertTrue(0 < accepted < len(lines))

    def test_over_budget_is_not_forwarded(self):
        self.assertIsNone(start.FINDING.match(str(FINDINGS[al.OverBudget][1])))


if __name__ == "__main__":
    unittest.main()
