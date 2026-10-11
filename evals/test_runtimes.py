"""Unit tests for scripts/runtimes.py: one record per agent CLI, and every launcher covering every record.

Adding a runtime is a row in the table. These tests list what else the row needs: a worker template, a one-shot
command, a settings entry, an eval backend, and the launch checks its capabilities imply. A launcher that misses
one fails here instead of at someone's launch.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "evals"))
import one_shot  # noqa: E402
import run  # noqa: E402
import runtimes  # noqa: E402
import settings  # noqa: E402
import spawn_session  # noqa: E402
from test_spawn_session import run_main  # noqa: E402


class TableTest(unittest.TestCase):
    def test_the_runtimes_and_their_order(self):
        # `start --runtime`, `evals/run.py --runtime` and the settings refusal list them in this order;
        # `worker --runtime` sorts them.
        self.assertEqual(runtimes.NAMES, ("claude", "codex", "cursor", "agy"))

    def test_cursor_installs_as_agent(self):
        self.assertEqual([runtimes.binary(name) for name in runtimes.NAMES], ["claude", "codex", "agent", "agy"])

    def test_a_registration_from_an_unknown_runtime_names_its_own_executable(self):
        # A session registered by a release that knew a runtime this one does not is still checked by name.
        self.assertEqual(runtimes.binary("someday"), "someday")

    def test_an_unknown_runtime_has_no_record(self):
        with self.assertRaises(KeyError):
            runtimes.get("someday")

    def test_every_record_has_the_shape_an_adapter_needs(self):
        # Ledger E13: codex, cursor and agy stay as data in 1.0, checked for shape only; 1.1 builds their adapters.
        for r in runtimes.RUNTIMES:
            with self.subTest(runtime=r.name):
                self.assertTrue(all(isinstance(v, str) and v for v in (r.name, r.binary, r.display)))
                self.assertEqual({type(r.needs_model), type(r.schedules)}, {bool})
                self.assertEqual(sum("{effort}" in token for token in r.effort_argv), 1 if r.effort_argv else 0)

    def test_a_record_cannot_be_changed(self):
        with self.assertRaises(AttributeError):
            runtimes.get("claude").binary = "other"  # type: ignore[misc]


class EveryLauncherCoversEveryRuntimeTest(unittest.TestCase):
    def test_every_runtime_has_a_worker_template_that_starts_its_executable(self):
        self.assertEqual(set(spawn_session.TEMPLATES), set(runtimes.NAMES))
        for name in runtimes.NAMES:
            with self.subTest(runtime=name):
                self.assertEqual(spawn_session.TEMPLATES[name][0], runtimes.binary(name))

    def test_every_runtime_has_a_one_shot_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            for name in runtimes.NAMES:
                with self.subTest(runtime=name):
                    argv = one_shot.command(name, "/bin/cli", cwd, cwd / "dispatch.md", agent_type=None,
                                            model="m", effort="")
                    self.assertEqual(argv[0], "/bin/cli")
                    self.assertIn(str(cwd / "dispatch.md"), argv[-1])

    def test_every_runtime_that_takes_an_effort_passes_it_before_the_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            for runtime in (r for r in runtimes.RUNTIMES if r.takes_effort):
                with self.subTest(runtime=runtime.name):
                    def windows(effort):  # the argv, as every run of consecutive tokens (`-c` alone is not the effort)
                        got = one_shot.command(runtime.name, "/bin/cli", cwd, cwd / "dispatch.md", agent_type=None,
                                               model="m", effort=effort)
                        return got, [got[i:i + len(tokens)] for i in range(len(got))]

                    tokens = runtime.effort_tokens("high")
                    got, runs = windows("high")
                    self.assertIn(str(cwd / "dispatch.md"), got[-1])
                    self.assertIn(tokens, runs)
                    self.assertNotIn(tokens, windows("")[1])

    def test_every_runtime_is_a_settings_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in runtimes.NAMES:
                with self.subTest(runtime=name):
                    (root / "s.toml").write_text(f'[models.any]\nrotation = ["{name}:model-id"]\n')
                    self.assertEqual(settings.load(root, "s.toml").models["any"][0].runtime, name)

    def test_the_settings_refusal_lists_the_runtimes_in_table_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s.toml").write_text('[models.any]\nrotation = ["someday:model-id"]\n')
            with self.assertRaisesRegex(settings.SettingsError, "runtime one of claude, codex, cursor, agy$"):
                settings.load(root, "s.toml")

    def test_every_other_runtime_has_an_eval_backend_on_its_executable(self):
        with mock.patch.object(run.shutil, "which", side_effect=lambda exe: "/bin/" + exe):
            for name in runtimes.NAMES:
                if name == "claude":
                    continue  # Claude runs through `command`, the harness's own backend.
                with self.subTest(runtime=name):
                    argv = run.host_command(name, "m", "hi", Path("/tmp/work"), Path("/tmp/plugin"))
                    self.assertEqual(argv[0], "/bin/" + runtimes.binary(name))


class CapabilityTest(unittest.TestCase):
    """What a record says a launch takes, the worker launcher enforces."""

    def test_the_runtimes_that_take_an_effort(self):
        # agy's CLI lists `--effort`; cursor's does not.
        self.assertEqual([r.name for r in runtimes.RUNTIMES if r.takes_effort], ["claude", "codex", "agy"])

    def test_a_runtime_without_effort_refuses_it(self):
        for runtime in runtimes.RUNTIMES:
            if runtime.takes_effort:
                continue
            with self.subTest(runtime=runtime.name):
                out = run_main("--runtime", runtime.name, "--model", "model-id", "--effort", "high")
                self.assertEqual(out.returncode, 1)
                self.assertIn("--effort", out.stderr)

    def test_a_runtime_that_needs_a_model_refuses_to_start_without_one(self):
        for runtime in runtimes.RUNTIMES:
            if not runtime.needs_model:
                continue
            with self.subTest(runtime=runtime.name):
                out = run_main("--runtime", runtime.name)
                self.assertEqual(out.returncode, 1)
                self.assertIn("--model", out.stderr)


if __name__ == "__main__":
    unittest.main()
