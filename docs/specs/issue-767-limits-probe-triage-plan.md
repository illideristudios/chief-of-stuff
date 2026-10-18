# Issue #767: limits_probe review findings (post-triage)

Implementation plan for `github:illideristudios/chief-of-stuff#767`. The plan stage changes no runtime behavior. Line numbers are from `origin/develop` at `cd8e3441`. This worktree is `50091f80` and does not contain `scripts/limits_probe.py`, `scripts/limits_history.py`, `scripts/engine.py`, or `scripts/worker.py`; implementation must be cut from develop.

Frank Lloyd AIght triaged the round-7 panel list on 2026-10-17. Only the four **Fix** items below are in scope for this issue. Everything else is closed, covered by other issues, or returned to #750.

## In scope

### 1. `LimitsHistory.readings` must migrate like `record` (`scripts/limits_history.py:89-95`)

**Cause.** `record` always calls `_migrate()` before opening a session. `readings` only checks `self.path.exists()` and queries immediately. A file created but not yet upgraded, or a read overlapping another workspace’s first migration, can raise `no such table: limit_reading`.

**Change.** Route reads through the same migrated path as writes: call `_migrate()` before the `Session` query (same lock and Alembic upgrade as `record`). Keep the early `[]` only when the database file does not exist yet and migration has not created it—after `_migrate()`, an empty history still returns `[]`.

**Tests** (`evals/test_limits.py`, `History` class):

- **Unreadable pre-migrate file:** create `limits.sqlite` as an empty file (or a stub without `limit_reading`), call `readings`; assert no exception and `[]`.
- **Concurrent first writer:** extend `test_separate_migrations_account_queries_and_source_dedup` or add a sibling: thread A begins `record` on a fresh path while thread B calls `readings` on the same path; assert B does not raise and, after both finish, rows match today’s expectations.

No change to dedup, timezone, or XDG path tests.

### 2. Document native cap-hit parsing (`README.md` ~61, `scripts/limits_probe.py:72-119`)

**Cause.** README says native assistant `rate_limit` errors “establish 100% usage when they explicitly report a cap hit and a reset clock with a timezone.” `_claude_error` is narrower: one exact English sentence (`You've hit your … limit · resets … (…)`) with optional `session` / `weekly` / `Opus` / `Sonnet` prefix; `isApiErrorMessage` true; 12-hour `am`/`pm` clock without 24-hour form; parenthesized `ZoneInfo` name; DST interpreted relative to the event timestamp. Anything else becomes unknown (including “no supported reset clock/timezone”).

**Change.** Prefer documenting what exists (architect ruling). Add a short bullet list under **Runtime limits** naming the sentence shape, clock form, timezone requirement, and that other native error wordings stay unknown until parsed.

**Tests.** Optional doc-only; if a test is added, a one-line assertion in `evals/test_limits.py` that README mentions `ZoneInfo` or the middle-dot sentence is enough. No change to probe behavior unless product later broadens `_claude_error`.

### 3. Standing rules Unicode contract (`README.md` ~79, `scripts/settings.py:338-345`)

**Cause.** README says rules reject “control or Unicode format characters.” Validation also rejects `Zl` and `Zp` (and documents `Cc`/`Cf` in the error string). README omits `Zl`/`Zp` and does not name categories.

**Change.** Align README with `settings._workers`: reject Unicode categories `Cc`, `Cf`, `Zl`, and `Zp` (control, format, line/paragraph separators). Keep the existing aggregate cap sentence (64 rules, 6000 characters)—already on develop and matches #748.

**Tests** (`evals/test_settings.py`): add or extend a case that a rule containing a line-separator character (`\u2028`) is rejected with `[workers] rules` in the message.

### 4. One settings snapshot per engine pass for assignments (`scripts/worker.py:200-208`, `scripts/engine.py:563-576`)

**Cause.** Engine startup builds `functools.partial(worker.launch, …, rules=config.workers.rules, …)` once. Each `worker.launch` reloads `settings.load` for per-stage `skills` and `agent` defaults while still using startup-frozen `rules`, `require_green`, and `claude_as_profile`. Editing TOML between passes can mix old standing rules with new stage skills.

**Change.**

- Reload workspace settings once at the start of each `run_pass` (same `settings.load(root, settings_path(text))` pattern as `startup.workspace_forge`, without re-forging unless already required elsewhere).
- Build launch kwargs for that pass from the fresh `config.workers` and pass stage defaults from the same `config` into `worker.launch` (new parameter, e.g. `stages: settings` stage map or per-stage row).
- Remove the inner `load_settings` in `worker.launch`; callers supply stage defaults from the pass snapshot. Engine main loop either rebuilds the `partial` each pass or `run_pass` accepts `root` and constructs launch internally—pick the smaller diff; behavior is “one load per pass.”

**Tests:**

- `evals/test_worker.py`: launch uses stage skills from the snapshot passed in, not from a second load (mock `settings.load` to return different skills on second call; assert assignment uses first snapshot).
- `evals/test_engine.py`: extend `test_engine_passes_workspace_rules_to_its_worker_launch_port` or add a pass that reloads rules between two `run_pass` calls when TOML changes (mock or temp file rewrite); second dispatch sees new rules and matching stage skills from the same reload.

## Out of scope (recorded for implementers)

| Finding | Disposition |
|--------|-------------|
| Probe bypasses `session_logs.py` | C19 / #753 / #755 |
| C19 wrong engine line | False on develop; discard |
| `[workers] rules` aggregate cap | #748 on develop |
| `_nearby_timestamp` | Dropped with #750 window reading |
| `reconcile` split ask, trio backoff, ask options, `_clear_interrupt`, `pending_stage` rollback | #750 M5 code, not develop |

Do not edit `architecture/compliance.md` or `architecture/ARCHITECTURE.md` in this issue.

## Suggested implementation order

1. `limits_history.readings` + test (isolated, low risk).
2. README documentation (items 2–3) + settings test for `Zl`/`Zp`.
3. Engine/worker settings snapshot + tests (touch two modules; do last).

## Risks

- **Migration lock contention.** Extra `_migrate()` on every read adds one file lock; acceptable because reads are infrequent and `record` already migrates.
- **Per-pass settings reload.** Invalid TOML mid-run surfaces on the next pass instead of at engine start; match existing `validate_worker_rules` at startup and consider whether a failed reload should log and skip launches (today startup fails fast).
- **Worktree base.** Branches cut from this plan worktree lack limits/engine code; implement from `origin/develop`.
- **Doc drift.** If `_claude_error` is broadened later (#642 window work), README must move with it.

## Acceptance

- `readings` never raises on missing schema; concurrent first-record/read is safe.
- README accurately describes cap-hit parsing and all four rejected Unicode categories.
- A single `settings.load` per `run_pass` feeds both `[workers] rules` and stage skills/agent in every worker assignment that pass.
- `python3 -m unittest evals.test_limits evals.test_settings evals.test_worker evals.test_engine` passes on develop; full `discover` stays green.

## File ownership

Issue write list matches the four code areas above. No widening required unless implement stage discovers a shared helper in `scripts/settings.py` only used for reload (already writable).
