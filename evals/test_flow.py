"""Unit tests for the deterministic flow engine."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "scripts"))

import flow
import tracker
import ownership
from settings import load as load_settings


SAMPLE_CLAUDE_MD = """# Workspace

## Coordinator

- User: Zach
- Timezone: America/Chicago
- Daily log dir: daily/
- Tracker: `daily/<date>-tracker.md`
- Worktrees: worktrees/
- Settings: chief-of-stuff.toml
"""

SAMPLE_TOML = """[workers]
mode = "one-shot"
max_concurrency = 2
"""


def make_workspace(root: Path, tracker_text: str, toml_text: str = SAMPLE_TOML) -> Path:
    (root / "CLAUDE.md").write_text(SAMPLE_CLAUDE_MD)
    (root / "chief-of-stuff.toml").write_text(toml_text)
    (root / "daily").mkdir(parents=True, exist_ok=True)
    (root / "worktrees").mkdir(parents=True, exist_ok=True)
    (root / "daily" / "2026-10-08-tracker.md").write_text(tracker_text)
    return root


class FlowEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tz = ZoneInfo("America/Chicago")

    def test_ready_one_shot_tasks_filters_overlap_and_workflow(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-a | Fix bug in module A | unassigned | open | 2026-10-08 | | S | build | build | #1 | |
| task-b | Add test for module A | unassigned | open | 2026-10-08 | | S | build | build | #2 | |
| task-c | Refactor module C | unassigned | open | 2026-10-08 | | S | build | build | #3 | |
| task-d | Improve module D | worker-d | running 10:00 | 2026-10-08 | | S | build | build | #4 | |
| task-e | Fix bug in module D | unassigned | open | 2026-10-08 | | S | build | build | #5 | |
| task-w | Routine workflow step | unassigned | open | 2026-10-08 | | S | | | workflow | |

## File ownership

| context | paths |
|---|---|
| task-a | `src/a.py` |
| task-b | `src/a.py` |
| task-c | `src/c.py` |
| task-d | `src/d.py` |
| task-e | `src/d.py` |
| task-w | none |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        settings = load_settings(self.root, "chief-of-stuff.toml")
        ready = flow.ready_one_shot_tasks(self.root, tracker_text, settings)

        # task-d is running (takes 1 slot). max_concurrency is 2. So 1 slot available!
        # Candidates: task-a, task-c.
        # task-b overlaps with task-a (so task-b excluded).
        # task-e overlaps with task-d (which is running, so task-e excluded).
        # task-w is workflow (excluded).
        # Since 1 slot is available, only the first non-conflicting ready task (task-a) is returned!
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].name, "task-a")

    def test_ready_one_shot_tasks_respects_max_concurrency_when_free(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-a | Fix bug in module A | unassigned | open | 2026-10-08 | | S | build | build | #1 | |
| task-c | Refactor module C | unassigned | open | 2026-10-08 | | S | build | build | #3 | |
| task-f | Fix module F | unassigned | open | 2026-10-08 | | S | build | build | #6 | |

## File ownership

| context | paths |
|---|---|
| task-a | `src/a.py` |
| task-c | `src/c.py` |
| task-f | `src/f.py` |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        settings = load_settings(self.root, "chief-of-stuff.toml")  # max_concurrency = 2
        ready = flow.ready_one_shot_tasks(self.root, tracker_text, settings)
        self.assertEqual(len(ready), 2)
        self.assertEqual([t.name for t in ready], ["task-a", "task-c"])

    def test_reconcile_dead_workers_marks_orphaned_when_no_report(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-d | Improve module D | worker-d | running 10:00 | 2026-10-08 | | S | build | build | #4 | |

## File ownership

| context | paths |
|---|---|
| task-d | `src/d.py` |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        tracker_path = self.root / "daily" / "2026-10-08-tracker.md"

        # worker-d has no running PID and no report file
        with mock.patch("flow.live_workers", return_value={}):
            reconciled = flow.reconcile_dead_workers(self.root, tracker_path, "2026-10-08")

        self.assertTrue(any("orphaned" in r for r in reconciled))
        updated = tracker_path.read_text()
        self.assertIn("| task-d | Improve module D | worker-d | orphaned |", updated)

    def test_reconcile_keeps_a_live_one_shot_whose_pid_file_holds_the_item(self):
        """#570: the pid file records the item text, not the row's name; a live launcher still holds its row."""
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-d | Improve module D | app-4-implementer | running 10:00 | 2026-10-08 | | S | build | build | #4 | |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        pid_file = self.root / "worktrees" / "tree-d" / ".chief-of-stuff" / "one-shot.pid"
        pid_file.parent.mkdir(parents=True)
        pid_file.write_text(f"{os.getpid()} Improve module D\n")
        tracker_path = self.root / "daily" / "2026-10-08-tracker.md"

        self.assertEqual(flow.reconcile_dead_workers(self.root, tracker_path, "2026-10-08"), [])
        self.assertIn("| app-4-implementer | running 10:00 |", tracker_path.read_text())

    def test_a_live_session_named_like_a_task_item_does_not_hold_its_row(self):
        """#571 review: only a one-shot's pid text names its row through the launcher; a registered session that happens
        to share a task's item is not that task's worker, so the dead worker's row is still orphaned."""
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-d | deploy | worker-d | running 10:00 | 2026-10-08 | | S | build | build | #4 | |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        tracker_path = self.root / "daily" / "2026-10-08-tracker.md"
        with mock.patch("process_status.registrations", return_value={os.getpid(): {"name": "deploy"}}):
            reconciled = flow.reconcile_dead_workers(self.root, tracker_path, "2026-10-08")
        self.assertTrue(any("orphaned" in r for r in reconciled), reconciled)

    def test_a_live_session_named_as_the_rows_name_or_owner_still_holds_it(self):
        """#575 review: a registered session joins its row by the row's name or owner, so its row stays running."""
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-d | deploy | worker-d | running 10:00 | 2026-10-08 | | S | build | build | #4 | |

## Log

- 10:00 started
"""
        tracker_path = self.root / "daily" / "2026-10-08-tracker.md"
        for session in ("task-d", "worker-d"):
            with self.subTest(session=session):
                make_workspace(self.root, tracker_text)
                with mock.patch("process_status.registrations", return_value={os.getpid(): {"name": session}}):
                    self.assertEqual(flow.reconcile_dead_workers(self.root, tracker_path, "2026-10-08"), [])
                self.assertIn("| worker-d | running 10:00 |", tracker_path.read_text())

    def test_check_stagnant_tasks_detects_prolonged_running_without_commits(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-d | Improve module D | worker-d | running 10:00 | 2026-10-08 | | S | build | build | #4 | |

## File ownership

| context | paths |
|---|---|
| task-d | `src/d.py`; worktree `tree-d` (branch-d) |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        settings = load_settings(self.root, "chief-of-stuff.toml")
        tree_dir = self.root / "worktrees" / "tree-d"
        tree_dir.mkdir(parents=True, exist_ok=True)

        now = datetime(2026, 10, 8, 11, 30, tzinfo=self.tz)  # 1h30m after 10:00
        with mock.patch("flow.last_worktree_commit_epoch", return_value=datetime(2026, 10, 8, 10, 5, tzinfo=self.tz).timestamp()):
            stagnant = flow.check_stagnant_tasks(self.root, tracker_text, now, "2026-10-08", settings)

        self.assertTrue(any("task-d" in s and "stale" in s for s in stagnant))

    def test_dispatch_ready_tasks_cuts_worktree_and_spawns_process(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-a | Fix bug in module A | unassigned | open | 2026-10-08 | | S | build | build | #1 | |

## File ownership

| context | paths |
|---|---|
| task-a | `src/a.py` |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        tracker_path = self.root / "daily" / "2026-10-08-tracker.md"
        settings = load_settings(self.root, "chief-of-stuff.toml")

        with mock.patch("make_worktree.build") as mock_build, \
             mock.patch("subprocess.Popen") as mock_popen:
            dispatched = flow.dispatch_ready_tasks(self.root, "2026-10-08", tracker_path, settings)

        self.assertEqual(len(dispatched), 1)
        self.assertIn("dispatched one-shot worker for 'task-a'", dispatched[0])
        mock_build.assert_called_once()
        self.assertEqual(mock_build.call_args.kwargs["agent_type"], "")
        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        self.assertIn("worker", cmd)
        self.assertIn("--one-shot", cmd)
        self.assertIn("task-a", cmd)
        self.assertEqual(cmd[cmd.index("--name") + 1], "flow-task-a")
        self.assertNotIn("--type", cmd)
        import spawn_session
        import one_shot
        argv = cmd[cmd.index("worker") + 1:]
        with mock.patch.object(one_shot, "run", return_value=0) as run:
            self.assertEqual(spawn_session.main(argv), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["name"], "flow-task-a")
        self.assertIsNone(run.call_args.kwargs["agent_type"])

    def test_dispatch_stage_agent_matches_cut_and_actual_launch(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-a | Review module A | unassigned | open | 2026-10-08 | | S | code | review | | |

## File ownership

| context | paths |
|---|---|
| task-a | `src/a.py` |
"""
        import spawn_session
        import one_shot
        for declarations, expected in (("- Agent: implementer task\n- Agent: reviewer task\n", "reviewer"),
                                       ("- Agent: reviewer task\n", "reviewer"),
                                       ("- Agent: reviewer standing\n", "reviewer"),
                                       ("- Agent: implementer task\n", ""), ("", "")):
            with self.subTest(declarations=declarations):
                make_workspace(self.root, tracker_text, SAMPLE_TOML +
                               '[lanes.code]\nstages = ["review"]\n[stages.review]\nagent = "reviewer"\n')
                claude = self.root / "CLAUDE.md"
                claude.write_text(claude.read_text() + declarations)
                settings = load_settings(self.root, "chief-of-stuff.toml")

                def cut(*args, **kwargs):
                    flow.make_worktree.check_type(claude.read_text(), kwargs["agent_type"])

                with mock.patch("make_worktree.build", side_effect=cut) as build, \
                     mock.patch("subprocess.Popen") as popen:
                    flow.dispatch_ready_tasks(self.root, "2026-10-08",
                                              self.root / "daily/2026-10-08-tracker.md", settings)
                self.assertEqual(build.call_args.kwargs["agent_type"], expected)
                cmd = popen.call_args.args[0]
                with mock.patch.object(one_shot, "run", return_value=0) as run:
                    self.assertEqual(spawn_session.main(cmd[cmd.index("worker") + 1:]), 0)
                self.assertEqual(run.call_args.kwargs["agent_type"] or "", expected)

    def test_dispatch_passes_type_only_when_agent_lines_declare_it(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-a | Fix bug in module A | unassigned | open | 2026-10-08 | | S | build | build | #1 | |

## File ownership

| context | paths |
|---|---|
| task-a | `src/a.py` |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        claude = self.root / "CLAUDE.md"
        claude.write_text(claude.read_text() + "- Agent: implementer task\n")
        tracker_path = self.root / "daily" / "2026-10-08-tracker.md"
        settings = load_settings(self.root, "chief-of-stuff.toml")
        with mock.patch("make_worktree.build") as mock_build, mock.patch("subprocess.Popen") as mock_popen:
            flow.dispatch_ready_tasks(self.root, "2026-10-08", tracker_path, settings)
        self.assertEqual(mock_build.call_args.kwargs["agent_type"], "implementer")
        cmd = mock_popen.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--type") + 1], "implementer")

    def test_dispatch_omits_type_when_implementer_is_not_declared(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-a | Fix bug in module A | unassigned | open | 2026-10-08 | | S | build | build | #1 | |

## File ownership

| context | paths |
|---|---|
| task-a | `src/a.py` |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        claude = self.root / "CLAUDE.md"
        claude.write_text(claude.read_text() + "- Agent: fixer standing\n")
        tracker_path = self.root / "daily" / "2026-10-08-tracker.md"
        settings = load_settings(self.root, "chief-of-stuff.toml")
        with mock.patch("make_worktree.build") as mock_build, mock.patch("subprocess.Popen") as mock_popen:
            flow.dispatch_ready_tasks(self.root, "2026-10-08", tracker_path, settings)
        self.assertEqual(mock_build.call_args.kwargs["agent_type"], "")
        self.assertNotIn("--type", mock_popen.call_args.args[0])

    def test_a_standing_implementer_is_not_the_one_shot_type(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-a | Fix bug in module A | unassigned | open | 2026-10-08 | | S | build | build | #1 | |

## File ownership

| context | paths |
|---|---|
| task-a | `src/a.py` |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        claude = self.root / "CLAUDE.md"
        claude.write_text(claude.read_text() + "- Agent: implementer standing\n")
        tracker_path = self.root / "daily" / "2026-10-08-tracker.md"
        settings = load_settings(self.root, "chief-of-stuff.toml")
        with mock.patch("make_worktree.build") as mock_build, mock.patch("subprocess.Popen") as mock_popen:
            flow.dispatch_ready_tasks(self.root, "2026-10-08", tracker_path, settings)
        self.assertEqual(mock_build.call_args.kwargs["agent_type"], "")
        self.assertNotIn("--type", mock_popen.call_args.args[0])

    def test_a_bad_agent_lifetime_refuses_that_task_and_does_not_abort_the_tick(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-a | Fix bug in module A | unassigned | open | 2026-10-08 | | S | build | build | #1 | |

## File ownership

| context | paths |
|---|---|
| task-a | `src/a.py` |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        claude = self.root / "CLAUDE.md"
        claude.write_text(claude.read_text() + "- Agent: ghost immortal\n")
        tracker_path = self.root / "daily" / "2026-10-08-tracker.md"
        settings = load_settings(self.root, "chief-of-stuff.toml")
        with mock.patch("subprocess.Popen") as mock_popen:
            dispatched = flow.dispatch_ready_tasks(self.root, "2026-10-08", tracker_path, settings)
        self.assertTrue(any("refused worktree" in line for line in dispatched))
        mock_popen.assert_not_called()

    def test_an_unassigned_standing_placeholder_is_not_dispatched(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| impl02 — standing implementer | impl02: standing implementer. Wait idle. | unassigned | open | 2026-10-08 | | S | build | build | #1 | |

## File ownership

| context | paths |
|---|---|
| impl02: standing implementer. Wait idle. | `src/a.py` |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        settings = load_settings(self.root, "chief-of-stuff.toml")
        ready = flow.ready_one_shot_tasks(self.root, tracker_text, settings)
        self.assertEqual(ready, [])

    def test_a_bold_unassigned_standing_item_is_not_dispatched(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| impl03 | **impl03**: standing implementer. Wait idle. | unassigned | open | 2026-10-08 | | S | build | build | #1 | |

## File ownership

| context | paths |
|---|---|
| **impl03**: standing implementer. Wait idle. | `src/a.py` |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        settings = load_settings(self.root, "chief-of-stuff.toml")
        ready = flow.ready_one_shot_tasks(self.root, tracker_text, settings)
        self.assertEqual(ready, [])

    def test_tick_runs_full_cycle(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-a | Fix bug in module A | unassigned | open | 2026-10-08 | | S | build | build | #1 | |

## File ownership

| context | paths |
|---|---|
| task-a | `src/a.py` |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        with mock.patch("make_worktree.build"), mock.patch("subprocess.Popen"):
            events = flow.tick(self.root, now=datetime(2026, 10, 8, 10, 30, tzinfo=self.tz), day="2026-10-08")
        self.assertTrue(any("dispatched" in e for e in events))

    def test_flow_cli_runs_tick(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-a | Fix bug in module A | unassigned | open | 2026-10-08 | | S | build | build | #1 | |

## File ownership

| context | paths |
|---|---|
| task-a | `src/a.py` |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        with mock.patch("make_worktree.build"), mock.patch("subprocess.Popen"), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = flow.main(["--root", str(self.root), "--date", "2026-10-08"])
        self.assertEqual(code, 0)
        self.assertIn("dispatched", out.getvalue())

    def test_main_exits_zero_cleanly_when_not_a_workspace(self):
        empty_dir = self.root / "empty"
        empty_dir.mkdir()
        code = flow.main(["--root", str(empty_dir)])
        self.assertEqual(code, 0)

    def test_main_exits_zero_cleanly_when_claude_md_has_no_coordinator_block(self):
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "CLAUDE.md").write_text("# Some other project\n\nNo coordinator here.\n")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = flow.main(["--root", str(plain)])
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")

    def test_reconcile_dead_workers_selects_latest_report(self):
        tracker_text = """## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| task-d | Improve module D | worker-d | running 10:00 | 2026-10-08 | | S | build | build | #4 | |

## File ownership

| context | paths |
|---|---|
| task-d | `src/d.py` |

## Log

- 10:00 started
"""
        make_workspace(self.root, tracker_text)
        tracker_path = self.root / "daily" / "2026-10-08-tracker.md"
        reports_dir = self.root / ".chief-of-stuff" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        # Create older report (done) and newer report (relaunch)
        old_report = reports_dir / "tree-1.toon"
        old_report.write_text("task: task-d\nworker: worker-d\nstatus: done\nreason: finished\n")
        os.utime(old_report, (1000, 1000))

        new_report = reports_dir / "tree-2.toon"
        new_report.write_text("task: task-d\nworker: worker-d\nstatus: relaunch\nreason: branch moved\n")
        os.utime(new_report, (2000, 2000))

        with mock.patch("flow.live_workers", return_value={}):
            reconciled = flow.reconcile_dead_workers(self.root, tracker_path, "2026-10-08")

        self.assertTrue(any("relaunch" in r for r in reconciled))
        updated = tracker_path.read_text()
        self.assertIn("| unassigned | open |", updated)

    def test_resolve_chief_bin_prefers_pinned_release(self):
        fake_release = self.root / "fake_release"
        fake_release.mkdir()
        fake_bin = fake_release / "chief_of_stuff.py"
        fake_bin.write_text("#!/usr/bin/env python3\n")
        with mock.patch.dict(os.environ, {"CHIEF_OF_STUFF_RELEASE": str(fake_release)}):
            resolved = flow.resolve_chief_bin(self.root)
            self.assertEqual(resolved, fake_bin.resolve())


if __name__ == "__main__":
    unittest.main()

