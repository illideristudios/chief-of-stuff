# Issue #767: limits_probe review findings to triage (from PR #750, for #642)

Implementation plan for `github:illideristudios/chief-of-stuff#767`. The plan stage changes no runtime code. Line references are from `origin/main` unless noted.

## Triage of the round-7 panel list

Frank Lloyd AIght triaged the review findings on 2026-10-17. **Four items are in scope for this issue.** The remaining items are already resolved on `origin/main`, owned by the architecture agent, or dropped with PR #750's window-reading delta.

| Finding | Disposition |
| --- | --- |
| `LimitsHistory.readings` skips `_migrate` (`scripts/limits_history.py:87-96`) | **Fix** (#767) |
| README understates `_claude_error` restrictions (`README.md:66`, `scripts/limits_probe.py:76-125`) | **Fix** (#767) |
| README omits `Zl`/`Zp` in standing-rules Unicode contract (`README.md:84`, `scripts/settings.py:345-351`) | **Fix** (#767) |
| Engine freezes `[workers] rules` at startup while `worker.launch` reloads stage settings (`scripts/worker.py:275-460`, `scripts/engine.py:787-852`, `:1066-1096`) | **Fix** (#767) |
| Probe bypasses `session_logs.py` | **Out of scope** — resolved on `origin/main` (#755): `scripts/session_logs.py` exists and `limits_probe.claude` reads transcripts through `session_logs.limit_events` (`scripts/limits_probe.py:19`, `:130`) |
| C19 cites wrong engine line in `architecture/compliance.md` | **Out of scope** — owned by the architecture agent; already updated on `origin/main` (`architecture/compliance.md:25`) |
| `[workers] rules` aggregate cap missing | **Out of scope** — resolved on `origin/main` (#748): `RULES_MAX = 64`, `RULES_TOTAL_CAP = 6000` (`scripts/settings.py:31-32`, `:352-353`) and `dispatch_prompt.validate_worker_rules` |
| `_nearby_timestamp` byte heuristic | **Out of scope** — dropped with #750 window reading; not on `origin/main` (#642 uses event timestamps) |
| `pending_stage` rollback on `slots.claim` failure, `reconcile` split ask, trio retry backoff/budget, ask options validation, `_clear_interrupt` scope matching | **Out of scope** — resolved on `origin/main` (`scripts/engine.py:206-278`, `:864-871`) |

Do not edit `architecture/compliance.md` or `architecture/ARCHITECTURE.md` in this issue.

## Worktree note

This plan worktree (`work/issue-767-plan-1`) was cut from an older commit (`50091f80`) that predates `scripts/limits_probe.py`, `scripts/limits_history.py`, `scripts/engine.py`, and `scripts/worker.py`. Implementation must rebase onto or branch from `origin/main`.

## In scope

### 1. `LimitsHistory.readings` migration and read coordination (`scripts/limits_history.py:48-96`)

**Problem.** `LimitsHistory.record` calls `self._migrate()` before opening a session, whereas `readings` checks only `if not self.path.exists(): return []` and queries `LimitReading` immediately. If `limits.sqlite` exists as an empty file from an interrupted open or while another workspace is running its first migration, `readings` can raise `OperationalError: no such table: limit_reading`. In addition, the `begin` event listener unconditionally issues `BEGIN IMMEDIATE`, which requires write access even for read queries and revision checks.

**Change.**

- In `_begin` (`scripts/limits_history.py:53-56`), emit `"BEGIN"` when `conn.get_execution_options().get("history_read")` is truthy and `"BEGIN IMMEDIATE"` otherwise.
- Add `_revision(self) -> str | None` opening a connection with `.execution_options(history_read=True)` and returning `MigrationContext.configure(conn).get_current_revision()`.
- In `_migrate(self)` (`scripts/limits_history.py:58-68`), resolve `head = scripts.get_current_head()` first and return early when `self.path.exists() and self._revision() == head` before `self.path.parent.mkdir(...)` or opening `.migrate.lock`, so an already-current database remains readable when the state directory is read-only. When acquiring `.migrate.lock` and `current != head`, verify `current is None or current in {r.revision for r in scripts.walk_revisions()}` (raising `ValueError(f"unknown limits history revision: {current}")` on an unknown revision) before calling `command.upgrade(cfg, "head")`.
- In `readings(self, runtime: str, account: str, since: datetime)` (`scripts/limits_history.py:87-96`), keep `since_stamp = _stamp(since)` and `if not self.path.exists(): return []` so reading a missing file creates no directory or lock file, then call `self._migrate()` and query inside `with Session(self.engine.execution_options(history_read=True)) as session:`.

**Tests** (`evals/test_limits.py`, `History`):

- `test_missing_history_read_creates_no_state`: `readings` on a missing path returns `[]` and leaves `path.parent` absent.
- `test_current_history_is_readable_without_a_writable_state_directory`: after `record`, unlink `.migrate.lock` and `chmod(0o555)` the parent directory; `readings` returns the stored row and does not recreate `.migrate.lock`.
- `test_unknown_history_revision_is_refused_without_attempting_upgrade`: update `alembic_version` to `'future_revision'`; `readings` raises `ValueError` matching `future_revision` without calling `command.upgrade`.
- `test_read_migrates_an_existing_empty_database`: `path.touch()` an empty `limits.sqlite`; `readings` migrates it and returns `[]`, and a subsequent `record`/`readings` round-trips.
- `test_read_waits_for_another_workspace_migration`: hold ` writer._migrate()` inside `command.upgrade` using ` threading.Event`; assert a concurrent `reader.readings()` blocks until upgrade finishes, then returns `[]` with `command.upgrade` called once.

### 2. Document `_claude_error` native cap-hit restrictions (`README.md:66`, `scripts/limits_probe.py:76-125`)

**Problem.** `README.md:66` states that native assistant `rate_limit` errors "also establish 100% usage when they explicitly report a cap hit and a reset clock with a timezone." `_claude_error` (`scripts/limits_probe.py:76-125`) is much narrower: it requires `type == "assistant"`, `error == "rate_limit"`, and `isApiErrorMessage is True`; the exact sentence `You've hit your (?:(session|weekly|Opus|Sonnet) )?limit\s*·\s*resets (.+) \(([^)]+)\)`; a valid event `timestamp`; a 12-hour clock (`1`–`12`, optional `:mm` minutes, lowercase `am`/`pm`); and a timezone name accepted by `ZoneInfo`. Other sentence forms are ignored and unsupported reset clocks/timezones produce an unknown reading.

**Change.** Update the `## Runtime limits` paragraph in `README.md:66` to state that native assistant `rate_limit` errors with `isApiErrorMessage: true` establish 100% usage for the text form `You've hit your limit · resets 1pm (America/Chicago)`, where the optional limit name before `limit` is `session`, `weekly`, `Opus`, or `Sonnet`, the reset clock uses a 12-hour hour (`1`–`12`), optional `:mm` minutes, and lowercase `am`/`pm` followed by a `ZoneInfo` timezone name, and the event carries a valid timestamp; other sentence forms, clocks, or timezones leave usage unknown. Do not broaden `_claude_error` in this issue.

**Tests.** Documentation accuracy check alongside `evals/test_limits.py`.

### 3. Standing-rules Unicode category contract (`README.md:84`, `scripts/settings.py:345-351`)

**Problem.** `scripts/settings.py:348` rejects characters in Unicode categories `Cc`, `Cf`, `Zl`, and `Zp`, but `README.md:84` mentions only "control or Unicode format characters" and omits line (`Zl`) and paragraph (`Zp`) separators.

**Change.** Update `README.md:84` to state that `[workers] rules` strings must not contain Unicode control (`Cc`), format (`Cf`), line separator (`Zl`), or paragraph separator (`Zp`) characters. Keep the existing 64-rule / 6000-character aggregate cap and stage assignment budget sentences.

**Tests** (`evals/test_settings.py`): verify that `[workers] rules` rejects `Cc`, `Cf`, `Zl` (`\u2028`), and `Zp` (`\u2029`) characters with `[workers] rules` in the `SettingsError` message.

### 4. One settings snapshot per engine pass (`scripts/worker.py:275-460`, `scripts/engine.py:787-852`, `scripts/engine.py:1066-1096`)

**Problem.** `engine.main` constructs `partial(worker.launch, ..., claude_as_profile=config.workers.claude_as_profile, rules=config.workers.rules, require_green=config.workers.require_green_tests, teardown_command=config.workers.teardown)` once at startup, while `worker.launch` (`scripts/worker.py:296`) calls `load_settings(root, settings_path(text))` on every launch to read `stages.get(stage)`, `models`, and `model_picks`, and `engine.run_pass` (`scripts/engine.py:803-804`) calls `worker.stage_defaults(root, claude_text, stage)` again. Editing the workspace TOML while the engine runs mixes startup-frozen `[workers]` settings with per-launch reloaded `[stages]` and `[models]` settings.

**Change.**

- In `scripts/worker.py`:
  - Update `launch` to accept `config: Settings | None = None` and `claude_as_profile: str | None = None` (loading `config = load_settings(root, settings_path(text))` only when `config is None`, and defaulting `claude_as_profile` to `config.workers.claude_as_profile` when `claude_as_profile is None` so an explicit `""` still selects the default login).
  - Read `defaults = config.stages.get(stage)`, `config.workers.rules`, `config.workers.require_green_tests`, `config.workers.teardown`, `config.models`, and `config.model_picks` from `config`.
  - Allow `stage_defaults` (or `run_pass`) to use the pass's `config` snapshot rather than re-reading TOML from disk.
- In `scripts/engine.py`:
  - Initialize `launch_config = config` after startup validation.
  - At the start of each loop iteration in `main`, reload `reloaded_config = settings.load(root, settings_path(text))`, check `reloaded_config.kanban is not None` and `settings.unmapped_stages(reloaded_config.kanban, policies)`, and run `dispatch_prompt.validate_worker_rules(root, reloaded_config, policies)`. If reload and validation succeed, set `launch_config = reloaded_config`; if any exception is raised, print `engine: settings reload failed; keeping last good snapshot: {exc}` to `sys.stderr` and keep the previous `launch_config`.
  - Build `launch = partial(worker.launch, store=db, root=root, trees=trees, policies=policies, forge=source, clone=root / selected if selected else root, config=launch_config)` for the pass and pass `workers=launch_config.workers`, `max_concurrency=launch_config.workers.max_concurrency`, `require_green=launch_config.workers.require_green_tests`, and `teardown_command=launch_config.workers.teardown` (plus `config=launch_config` if needed by `run_pass`) into `run_pass`.
- In `README.md:84`: document that engine workers share one snapshot of standing rules, stage settings, account profile, concurrency cap, suite policy, and teardown command per pass, edits take effect on the next pass, and a failed reload keeps the last good snapshot.

**Tests** (`evals/test_worker.py`, `evals/test_worker_teardown.py`, `evals/test_engine.py`):

- `evals/test_worker.py`:
  - `test_launch_uses_pass_snapshot_after_settings_change_on_disk`: pass a loaded `config` into `worker.launch` after rewriting `settings.toml` on disk; assert `claude_as_profile`, `agent`, `skills`, and `rules` all come from `config`.
  - `test_launch_without_a_snapshot_uses_the_configured_profile` and `test_launch_explicit_empty_profile_uses_default_login`.
- `evals/test_worker_teardown.py`: update `launch` calls that pass `teardown_command` to pass `config=settings.Settings(workers=settings.Workers(teardown=...))` (or keep backward-compatible kwarg handling if needed by callers) and verify teardown still runs when `claude_adapter.cancel` raises.
- `evals/test_engine.py`:
  - `test_each_pass_pins_rules_and_stage_defaults_and_refreshes_together`: verify two consecutive passes pick up updated `rules` and `stages["implement"]` together in one `config` object per pass.
  - `test_bad_reload_keeps_the_last_good_snapshot_and_still_runs_the_pass`: cover missing, empty, truncated, and unreadable `settings.toml` between passes; verify `settings reload failed` is logged to stderr and the prior `config` snapshot is reused.
  - `test_reload_failure_outside_settings_error_keeps_the_snapshot`, `test_reload_that_exceeds_a_stage_budget_keeps_the_last_good_snapshot`, and `test_once_bad_reload_runs_on_the_valid_startup_snapshot`.

## Suggested implementation order

1. `scripts/limits_history.py` + `evals/test_limits.py` (isolated to limit history migration and read locking).
2. `README.md` + `scripts/settings.py` + `evals/test_settings.py` (documentation and Unicode category tests).
3. `scripts/worker.py`, `scripts/engine.py`, `evals/test_worker.py`, `evals/test_worker_teardown.py`, and `evals/test_engine.py` (per-pass settings snapshot and reload fallback).

## Risks

- **Read-only state directories and lock contention in `LimitsHistory.readings`.** Checking `if not self.path.exists(): return []` and `self._revision() == head` before `mkdir` and `.migrate.lock`, and using `BEGIN` instead of `BEGIN IMMEDIATE` for `history_read` connections, avoids unnecessary write locks or permission errors when reading an already-migrated database.
- **Invalid mid-run settings edits.** A broken edit to `settings.toml` while the engine is running must not crash the loop or drop concurrency/rules back to empty defaults; catching reload/validation exceptions, logging to `stderr`, and retaining the last validated `launch_config` snapshot keeps the pass running safely.
- **Worktree base mismatch.** This worktree's base (`50091f80`) predates the target modules on `origin/main`; the implement stage must rebase onto `origin/main` before applying code changes.
- **Documentation drift.** When #642 expands local limit parsing, the `## Runtime limits` section in `README.md` must be updated in lockstep.

## Acceptance

- `LimitsHistory.readings` returns `[]` without creating state when the file is absent, migrates an uninitialized/older database under `.migrate.lock`, refuses unknown future revisions, and reads an up-to-date database without requiring a writable parent directory.
- `README.md` documents the exact `_claude_error` sentence, clock, timezone, and timestamp requirements, the four rejected Unicode categories (`Cc`, `Cf`, `Zl`, `Zp`), and the per-pass engine settings snapshot behavior.
- Each engine pass reloads and validates workspace settings once, falling back to the last good snapshot on failure, and supplies that single snapshot to both `run_pass` and `worker.launch`.
- `python3 -m unittest evals.test_limits evals.test_settings evals.test_worker evals.test_worker_teardown evals.test_engine` and full `unittest discover` pass with 0 failures, 0 skipped, and 0 xfailed.
