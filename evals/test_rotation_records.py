"""Round-robin dispatch extends the configured rotation; records never decide routing."""

import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import limits
import performance
import settings
import store
import worker
from evals.test_engine import NOW

KEY = "github:example/repo#12"
ENTRIES = (settings.ModelEntry("claude", "one", "high"),
           settings.ModelEntry("codex", "two", "medium"), settings.ModelEntry("cursor", "three"))


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.db"
        store.migrate(self.path)
        self.db = store.Store(self.path)
        self.addCleanup(self.db.engine.dispose)

    def choose(self, key=KEY, stage="implement", attempt=1, **kwargs):
        self.db.upsert_item(key)
        return worker._model(self.db, key, stage, self.db.resume(key) or {},
                             SimpleNamespace(model="fallback", effort=""),
                             {"implement": ENTRIES, "review": ENTRIES}, NOW.isoformat(),
                             launchable=lambda runtime: True, attempt=attempt, **kwargs)

    def record(self, key, entry, stage="implement", attempt=1, outcome="failed"):
        self.db.record_segment(key, stage, attempt, NOW.isoformat(), NOW.isoformat(), outcome,
                               runtime=entry.runtime, model=entry.model, effort=entry.effort or "")

    def test_fan_out_wraps_and_resumes_after_reopening_store(self):
        picked = [self.choose(f"github:example/repo#{n}") for n in range(1, 3)]
        self.db.engine.dispose()
        self.db = store.Store(self.path)
        self.addCleanup(self.db.engine.dispose)
        picked += [self.choose(f"github:example/repo#{n}") for n in range(3, 5)]
        self.assertEqual(picked, [*ENTRIES, ENTRIES[0]])

    def test_recovered_selection_does_not_spend_another_turn(self):
        self.assertEqual(self.choose(), ENTRIES[0])
        self.assertEqual(self.choose(), ENTRIES[0])
        self.assertEqual(self.choose("github:example/repo#13"), ENTRIES[1])

    def test_retry_uses_failed_entry_not_current_cursor_under_every_strategy(self):
        for how in ("first", "random", "round_robin"):
            key = f"github:example/{how}#12"
            self.db.upsert_item(key)
            self.record(key, ENTRIES[-1])
            self.choose("github:example/repo#13")
            self.assertEqual(self.choose(key, attempt=2, how=how), ENTRIES[0])

    def test_fix_continues_implementation_rotation_and_classes_have_separate_cursors(self):
        first = self.choose()
        self.record(KEY, first, outcome="done")
        self.assertEqual(self.choose(KEY, stage="review"), ENTRIES[0])
        self.assertEqual(self.choose(KEY, stage="fix"), ENTRIES[1])

    def test_ineligible_entries_are_skipped_and_unknown_limits_remain_eligible(self):
        # Near-cap entries stay out; unknown-limit / otherwise eligible entries keep taking turns,
        # including after wrap-around would otherwise land on the capped entry.
        capped = {ENTRIES[0]}
        available = lambda entry: entry not in capped
        self.assertEqual(self.choose(available=available), ENTRIES[1])
        self.assertEqual(self.choose("github:example/repo#13", available=available), ENTRIES[2])
        self.assertEqual(self.choose("github:example/repo#14", available=available), ENTRIES[1])

    def test_all_entries_near_cap_refuses_instead_of_launching_persona(self):
        with self.assertRaisesRegex(RuntimeError, "limit"):
            self.choose(available=lambda entry: False)

    def test_explicit_first_and_random_are_still_configuration(self):
        self.assertEqual(self.choose(how="first"), ENTRIES[0])
        self.assertEqual(self.choose("github:example/repo#13", how="first"), ENTRIES[0])
        self.assertEqual(self.choose("github:example/repo#14", how="random", choose=lambda xs: xs[-1]), ENTRIES[-1])

    def test_concurrent_dispatches_take_distinct_turns(self):
        barrier = threading.Barrier(3)
        selected, errors = [], []
        def dispatch(n):
            db = store.Store(self.path)
            try:
                key = f"github:example/repo#{n}"
                db.upsert_item(key)
                barrier.wait(timeout=5)
                selected.append(worker._model(db, key, "implement", {},
                    SimpleNamespace(model="fallback", effort=""), {"implement": ENTRIES}, NOW.isoformat(),
                    launchable=lambda runtime: True))
            except BaseException as exc:
                errors.append(exc)
            finally:
                db.engine.dispose()
        threads = [threading.Thread(target=dispatch, args=(n,)) for n in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertCountEqual(selected, ENTRIES)

    def test_workflow_is_snapshotted_for_failed_and_completed_attempts(self):
        first = self.choose(workflow="delivery")
        self.record(KEY, first)
        second = self.choose(attempt=2, workflow="repair")
        self.record(KEY, second, attempt=2, outcome="done")
        rows = performance.table(self.db.segments(), NOW.date(), NOW.date())
        self.assertEqual([(r.model, r.workflow, r.completed, r.relaunched) for r in rows],
                         [("one", "delivery", 0, 0), ("two", "repair", 1, 1)])


class RotationSettingsTest(unittest.TestCase):
    def test_workers_default_to_round_robin_and_trio_keeps_its_independent_draw(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s.toml").write_text('[models.implement]\nrotation=["claude:one", "codex:two"]\n'
                                        '[models.trio]\nrotation=["claude:one"]\n')
            self.assertEqual(settings.load(root, "s.toml").model_picks,
                             {"implement": "round_robin", "trio": "first"})

    def test_known_limit_uses_configured_account_threshold_and_reset(self):
        reading = limits.Reading("claude", "secondary", "weekly", 85, 100, "%",
                                 NOW + timedelta(hours=1), NOW, "")
        with mock.patch.object(worker.limits_probe, "read", return_value=[reading]) as probe:
            self.assertFalse(worker.below_limit("claude", "secondary", NOW, .8))
            self.assertTrue(worker.below_limit("claude", "secondary", NOW, .9))
            self.assertTrue(worker.below_limit("claude", "secondary", NOW + timedelta(hours=2), .8))
        probe.assert_called_with("claude", "secondary")

    def test_unknown_limit_does_not_block_launch(self):
        reading = limits.Reading("codex", "", "limits", None, None, "", None, NOW, "unreadable")
        with mock.patch.object(worker.limits_probe, "read", return_value=[reading]):
            self.assertTrue(worker.below_limit("codex", "", NOW, .8))


class RawRecordsTest(unittest.TestCase):
    def test_records_keep_individual_durations_workflow_and_outcomes_without_defining_success(self):
        base = dict(issue_key=KEY, stage="implement", runtime="codex", model="two", effort="high",
                    agent_type="builder", workflow="delivery", started_at=NOW.isoformat())
        segments = [SimpleNamespace(**base, attempt=1, outcome="failed", required_human_review=True,
                                    ended_at=(NOW + timedelta(seconds=95)).isoformat()),
                    SimpleNamespace(**base, attempt=2, outcome="done", required_human_review=False,
                                    ended_at=(NOW + timedelta(minutes=4)).isoformat()),
                    SimpleNamespace(**base, attempt=3, outcome=None, required_human_review=False, ended_at=None)]
        records = performance.records(segments)
        self.assertEqual([(r.workflow, r.completed, r.human_review, r.relaunched, r.minutes) for r in records],
                         [("delivery", False, True, False, 1), ("delivery", True, False, True, 4),
                          ("delivery", False, False, True, None)])
        rows = performance.table(segments, NOW.date(), NOW.date())
        self.assertEqual([(r.workflow, r.runs, r.completed, r.human_review, r.relaunched, r.median_minutes)
                          for r in rows], [("delivery", 3, 1, 1, 2, 1)])

    def test_workflows_with_identical_entry_and_agent_type_stay_separate(self):
        base = dict(issue_key=KEY, stage="implement", runtime="codex", model="two", effort="high",
                    agent_type="builder", started_at=NOW.isoformat(), attempt=1, ended_at=None, outcome=None)
        rows = performance.table([SimpleNamespace(**base, workflow=workflow) for workflow in ("a", "b")],
                                 NOW.date(), NOW.date())
        self.assertEqual([(r.workflow, r.runs) for r in rows], [("a", 1), ("b", 1)])


if __name__ == "__main__":
    unittest.main()
