"""The words each audit finding starts with, and which of them the watcher forwards.

`audit_tasks` starts every finding line with one of these, and `start_coordinator`'s watcher picks the lines
it forwards out of the audit's output by the same words, so a finding named here is named the same in both.
This module imports nothing from the plugin, so `start_coordinator` can load it as `scripts.findings` and the
scripts as `findings`.
"""

from __future__ import annotations

REOPEN = "reopen:"
ORPHANED = "orphaned work:"
UNCLAIMED = "unclaimed work:"
STOPPED = "stopped:"
QUEUE = "decision queue:"
NEXT = "next decision:"
ISSUE = "issue:"
MERGED = "merged:"
LANE = "lane:"
KANBAN = "kanban:"
OVER = "over budget:"

# What the watcher forwards, in the order its pattern tries them. No script prints `overdue:` or `waiting:`;
# dropping them is its own change.
WATCHED = (ORPHANED, STOPPED, REOPEN, ISSUE, MERGED, LANE, KANBAN, QUEUE, NEXT, "overdue:", "waiting:")
# Printed and not forwarded. The agent file asks the coordinator to act on `over budget:`; forwarding it, or
# `unclaimed work:`, is a behaviour change and starts as a failing case.
UNWATCHED = (OVER, UNCLAIMED)
