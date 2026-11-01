# Plan: #636, 1.0 M3.8: declare datetime-inject and prove the clock reaches workers

Stage: plan. Scope of this stage: this file only.

## What is true today

- `origin/develop` (the PR base) has the engine: `scripts/worker.py`, `scripts/claude_adapter.py`, `guard/`. This worktree's HEAD (`50091f80`, `main`) does not. **The implement stage must branch from `origin/develop`, not from `main`.**
- Neither `.claude-plugin/plugin.json` nor `.claude-plugin/marketplace.json` on `origin/develop` has `dependencies` or `allowCrossMarketplaceDependenciesOn`. No test touches the clock plugin.
- Decisions are already made (issue comment, Architect, 2026-10-16; spike note addendum): depend on it (Zach, 2026-10-10); declare it in **both** manifests, because a `plugin.json`-only declaration installs without the dependency; add `ai-additions` to `allowCrossMarketplaceDependenciesOn`; ship no clock hook of our own; range `^1.0` (Zach, 2026-10-16; the plugin is at 1.0.0). The "version range" owner item in the issue body is closed.
- M0 spike: `UserPromptSubmit` command hooks run under `claude -p` and their stdout reaches the model. Hooks fail open, so a missing plugin means a worker with no clock and no error.
- Prior art: PR #749 (`loop/636`, open, base `develop`, 145 files, Red/Green commits `58e5a9ec`..`31dd1a15`). It was cut from an older develop and `claude_adapter` has since been rewritten (`start`/`spawn`, detached runner). Do **not** merge or rebase it. Port from it by hand: both manifest hunks, `evals/fixtures/claude-plugin-list-user.json`, and the shape of its `ClockDependencyTest`. Leave the rest.

```
d := declare datetime-inject and prove delivery (#636)
M := both manifests declare datetime-inject@ai-additions "^1.0"
A := marketplace.json lists ai-additions in allowCrossMarketplaceDependenciesOn
H := a claude worker launch refuses when datetime-inject is not installed, enabled and user-scoped
P := a worker launched with the production profile gets one bracketed timestamp line per prompt
Q := ship no clock hook of our own
K := the install actually carries the dependency
S1 := the declaration must be in the marketplace entry, not plugin.json alone   [spike note, 2026-10-10]
S2 := Zach: depend on it; range ^1.0                                            [issue comment, 2026-10-10, 2026-10-16]
S3 := hooks fail open                                                           [spike note, observed]
S4 := cross-marketplace deps install only if allowed                            [spike note, from the docs, not run]
S5 := plan M3 item 8: test that a launched worker receives one bracketed line per prompt   [1.0 plan]

S1 ∧ S4 → (M ∧ A) → K
S3 → ¬H → silent loss of the clock        ∴ H is needed, not optional
S2 → ¬Q
P is the only premise no unit test can discharge (it needs a real `claude -p`)   [S5, S3]
∴ ship M ∧ A ∧ H, a static test for M ∧ A, a fake-host test for H, and one billed, explicitly-run live test for P
```

## Plan, smallest complete change

Order is Red then Green per step; commit each small.

1. **Manifests (the declaration).** Add to `.claude-plugin/plugin.json` and to the `chief-of-stuff` entry of `.claude-plugin/marketplace.json`: `"dependencies": [{"name": "datetime-inject", "marketplace": "ai-additions", "version": "^1.0"}]`. Add top-level `"allowCrossMarketplaceDependenciesOn": ["ai-additions"]` to `marketplace.json`. Bump the version in both files as the repo's release convention requires (the two must match; see `evals/test_runtime_launch.py:57`).
   - Test (Red first), `evals/test_clock.py` (exists on develop; add a class): both manifests carry the identical dependency dict, and `ai-additions` is in the allow list. Pure JSON read, no process.
2. **Launch check (H).** One small function that turns `claude plugin list --json` output into a verdict, and one subprocess call that gets it.
   - Pure part in `scripts/clock.py`: `user_clock(plugins)` returns the enabled, user-scoped `datetime-inject@ai-additions` record or `None` (port the 6 lines from `loop/636:scripts/clock.py`).
   - Process part in `scripts/claude_adapter.py`: run the registry host's `plugin list --json` with `--setting-sources user`, the worker's `claude_as_profile` and `shell_setup.login_argv`, a hard timeout, stdin devnull, stdout and stderr to files (not pipes). Raise `RuntimeError` with the command's own stderr when the call fails, the JSON is unreadable, or `user_clock` finds nothing. Reuse `runtimes.program` and `shell_setup` exactly as `command()` does; do not add a second way to find `claude`.
   - Call site: `scripts/worker.py`, in the `chosen.runtime == "claude"` branch immediately before `claude_adapter.start` (develop line ~381). Non-claude hosts have no `datetime-inject` and are out of scope. Adopting an existing detached run (`existing is not None`, top of `launch`) runs no preflight: that run already launched.
   - A refusal here leaves the run directory behind, but `launch` already preserves a refused start's evidence under a `-refused-` sibling and the tree under `resume.worktree`, so a relaunch reuses it. Test that a refused clock followed by a good one launches without a second tree. Moving the check earlier is the implementer's call if that test fails; `loop/636` moved it before the tree and run dir.
   - **Version range is not re-checked by the engine.** Claude Code's own resolver enforces `^1.0` on install and enable (spike note, from the docs). `loop/636` added a hand-written caret parser (`version_ok`) to re-check it; skip it. It is a second implementation of semver for a case the host already owns, and a semver library would be a new dependency needing Zach's yes. Residual risk: a user who installs `datetime-inject@2.x` by hand and sidesteps the resolver. State this in the PR; revisit only if Zach wants it.
   - Tests (fake host, no model), `evals/test_model_adapter.py` and `evals/test_worker.py`: a captured user-scope inventory passes (`evals/fixtures/claude-plugin-list-user.json`, port it; replace `installPath`); missing, disabled, project-only, wrong-marketplace each refuse; failing command, timeout, non-JSON, and a non-list refuse with a message that names `datetime-inject@ai-additions`; a non-claude chosen runtime does not run the preflight; an adopted run does not run it. `evals/fixtures/fake_claude.py` answers `plugin list --json` only when `--setting-sources user` is present.
3. **Delivery proof (P).** `evals/live/clock_delivery.py` (new directory, not named `test_*.py`), run only as `CHIEF_CLOCK_LIVE=1 [CHIEF_CLOCK_PROFILE=<p>] python3 -m unittest evals.live.clock_delivery -v`. It launches `haiku` through `claude_adapter` with the production worker profile (guard plugin dir, `worker=True`, `--setting-sources user`) twice, in separate sessions, one second apart, and asserts each prompt got exactly one line matching the plugin's bracketed format (`[Current TimeStamp: ... | YYYY-MM-DD HH:MM:SS ... (UTC±hh:mm)]`), inside the call's wall-clock window, strictly later the second time.
   - **First, in a scratch spike, check whether stream-json emits the hook's stdout** (`--output-format stream-json --verbose`, hook events). If it does, assert on the hook event: deterministic, and the model reads nothing. If it does not, ask the model to echo the line into a `--json-schema` field as `loop/636` did. Record which in the module docstring.
   - Why a separate module: `require_green_tests` holds a report whose skipped count is not zero. A `skipUnless` live test would count as skipped on every default run and block green. The default suite must have passed > 0 and skipped = 0, failed = 0, xfailed = 0.
   - Check `evals/run.py` discovery before choosing the path, so the directory is neither picked up nor flagged by `evals/test_architecture.py`.
4. **Docs.** The architect owns `architecture/*` and the 1.0 spec docs (memory: architect-owns-docs). Do not edit them. Put in the PR body: the one-line how-to for the live run, the version-range residual risk, and a request that the architect record M3.8 as done and close the "version range" open item in the plan.

## Risks

- `plugin list --json` field names (`id`, `scope`, `enabled`, `installPath`) are taken from one captured sample (`loop/636`, CLI 2.1.296). Re-capture on the installed CLI before writing the fixture and keep the capture, not a hand-edited copy.
- Hook fail-open means H guards presence only. It cannot prove a hook ran. Only the live test proves that, and only when a human runs it. Say so; do not claim P is in CI.
- The live test is billed (two haiku calls). It must never be reachable from the default suite or CI.
- Under `--setting-sources user`, the plugin must be user-scoped. A project-scoped install loads nowhere in a worker. The check and its message must say "user".
- `loop/636` and develop disagree about `claude_adapter.call` (pipes then, pipes still in `call`, detached in `start`). The preflight uses neither: keep it a small separate function so a future change to either path does not touch it.
- Plan stage only wrote this file. No tests were run in this stage, so no pass/fail counts are reported.

## File ownership (proposed for implement)

Write:
- `.claude-plugin/plugin.json`
- `.claude-plugin/marketplace.json`
- `scripts/clock.py`
- `scripts/claude_adapter.py`
- `scripts/worker.py`
- `evals/test_clock.py`
- `evals/test_model_adapter.py`
- `evals/test_worker.py`
- `evals/fixtures/fake_claude.py`
- `evals/fixtures/claude-plugin-list-user.json` (new)
- `evals/live/clock_delivery.py` (new)

Read: `scripts/runtimes.py`, `scripts/shell_setup.py`, `scripts/guard_binding.py`, `guard/`, `evals/run.py`, `evals/test_architecture.py`, `evals/test_runtime_launch.py`, `docs/specs/2026-10-09-chief-of-stuff-1.0-spike-claude-p.md`, `docs/specs/2026-10-09-chief-of-stuff-1.0-plan.md`.
