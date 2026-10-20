# Issue #767: limits_probe review findings to triage (from PR #750, for #642)

Implementation plan for `github:illideristudios/chief-of-stuff#767`. The plan stage changes no runtime code. Line references are from `origin/develop` unless noted.

## Triage of the round-7 panel list

Frank Lloyd AIght triaged the review findings on 2026-10-17. **Four items are in scope for this issue.** The rest are closed here, owned elsewhere, or already fixed on develop.

| Finding | Disposition |
|--------|-------------|
| `LimitsHistory.readings` skips `_migrate` | **Fix** (#767) |
| README understates `_claude_error` restrictions | **Fix** (#767) |
| README omits `Zl`/`Zp` in standing-rules Unicode contract | **Fix** (#767) |
| Engine freezes `[workers] rules` at startup while `worker.launch` reloads stage settings | **Fix** (#767) |
| Probe bypasses `session_logs.py` | **Out of scope** — on develop, `limits_probe.claude` already calls `session_logs.limit_events`; C19 / #753 / #755 |
| C19 cites wrong engine line in `architecture/compliance.md` | **Out of scope** — architecture agent; false on develop |
| `[workers] rules` aggregate cap missing | **Out of scope** — #748; develop enforces `RULES_MAX` / `RULES_TOTAL_CAP` |
| `_nearby_timestamp` byte heuristic | **Out of scope** — dropped with #750 window reading; #642 must use event timestamps |
| `reconcile` split ask, trio backoff, ask options, `_clear_interrupt`, `pending_stage` rollback | **Out of scope** — #750 M5 / #775 / #774 territory, not this triage slice |

Do not edit `architecture/compliance.md` or `architecture/ARCHITECTURE.md` in this issue.

## Worktree note

This plan worktree (`work/issue-767-plan-1`) was cut from an older base and does not ship `scripts/limits_probe.py`, `scripts/limits_history.py`, `scripts/engine.py`, or `scripts/worker.py`. Implementation and tests must land on a branch cut from `origin/develop`.

## In scope

### 1. `LimitsHistory.readings` must migrate like `record` (`scripts/limits_history.py:89-98`)

**Problem.** `record` always calls `_migrate()` before opening a session. `readings` only checks `self.path.exists()` and queries immediately. A file created but not yet upgraded, or a read overlapping another workspace’s first migration, can raise `no such table: limit_reading`.

**Change.** Call `_migrate()` before the `Session` query (same lock and Alembic upgrade as `record`). Keep returning `[]` when the database file does not exist and migration has not created it; after `_migrate()`, an empty history still returns `[]`.

**Tests** (`evals/test_limits.py`, `History`):

- **Pre-migrate stub file:** create `limits.sqlite` as an empty file (or without `limit_reading`), call `readings`; assert no exception and `[]`.
- **Concurrent first writer:** extend `test_separate_migrations_account_queries_and_source_dedup` or add a sibling: thread A begins `record` on a fresh path while thread B calls `readings` on the same path; assert B does not raise and, after both finish, rows match expectations.

### 2. Document native cap-hit parsing (`README.md` ~63, `scripts/limits_probe.py:72-119`)

**Problem.** README says native assistant `rate_limit` errors “establish 100% usage when they explicitly report a cap hit and a reset clock with a timezone.” `_claude_error` is narrower: `type == assistant`, `error == rate_limit`, `isApiErrorMessage is True`; one exact English sentence (`You've hit your … limit · resets … (…)`) with optional `session` / `weekly` / `Opus` / `Sonnet` prefix; 12-hour `am`/`pm` clock (no 24-hour form); parenthesized `ZoneInfo` name; DST interpreted relative to the event's `timestamp`. Other wordings become unknown (including “no supported reset clock/timezone”).

**Change.** Under **Runtime limits**, add a short bullet list naming the sentence shape, clock form, timezone requirement, and that other native error wordings stay unknown until parsed. Do not broaden probe parsing in this issue.

**Tests.** Doc-only is acceptable. Optional: one assertion in `evals/test_limits.py` that README mentions `ZoneInfo` or the middle-dot sentence.

### 3. Standing rules Unicode contract (`README.md` ~81, `scripts/settings.py:338-345`)

**Problem.** README says rules reject “control or Unicode format characters.” Validation also rejects `Zl` and `Zp` (categories `Cc`, `Cf`, `Zl`, `Zp` in code). README omits line/paragraph separators and does not name categories.

**Change.** Align README with `_workers`: reject Unicode categories `Cc`, `Cf`, `Zl`, and `Zp`. Keep the existing aggregate cap sentence (64 rules, 6000 characters) — already on develop.

**Tests** (`evals/test_settings.py`): add or extend a case that a rule containing a line-separator character (`\u2028`) is rejected with `[workers] rules` in the message.

### 4. One settings snapshot per engine pass for assignments (`scripts/worker.py:285-293`, `scripts/engine.py:1129-1146`)

**Problem.** Engine startup builds `functools.partial(worker.launch, …, rules=config.workers.rules, …)` once. Each `worker.launch` calls `load_settings` again for per-stage `skills` and `agent` defaults while still using startup-frozen `rules`, `require_green`, and `claude_as_profile`. Editing TOML between passes can mix old standing rules with new stage skills.

**Change.**

- Reload workspace settings once at the start of each `run_pass` (same `settings.load(root, settings_path(text))` pattern as startup, without re-forging unless already required).
- Build launch kwargs for that pass from the fresh `config.workers` and pass stage defaults from the same `config` into `worker.launch` (e.g. `stages` map or per-stage row passed by caller).
- Remove the inner `load_settings` in `worker.launch`; callers supply stage defaults from the pass snapshot. Engine main loop either rebuilds the `partial` each pass or `run_pass` constructs launch internally — pick the smaller diff; behavior is “one load per pass.”

**Tests:**

- `evals/test_worker.py`: launch uses stage skills from the snapshot passed in, not from a second load (mock `settings.load` to return different skills on second call; assert assignment uses first snapshot).
- `evals/test_engine.py`: extend `test_engine_passes_workspace_rules_to_its_worker_launch_port` or add a pass that reloads rules between two `run_pass` calls when TOML changes; second dispatch sees new rules and matching stage skills from the same reload.

## Suggested implementation order

1. `limits_history.readings` + test (isolated, low risk).
2. README documentation (items 2–3) + settings test for `Zl`/`Zp`.
3. Engine/worker settings snapshot + tests (two modules; do last).

## Risks

- **Migration lock contention.** Extra `_migrate()` on every read adds one file lock; acceptable because reads are infrequent and `record` already migrates.
- **Per-pass settings reload.** Invalid TOML mid-run surfaces on the next pass instead of at engine start; match existing startup validation and decide whether a failed reload should log and skip launches (today startup fails fast).
- **Doc drift.** If `_claude_error` is broadened later (#642 window work), README must move with it.

## Acceptance

- `readings` never raises on missing schema; concurrent first-record/read is safe.
- README accurately describes cap-hit parsing and all four rejected Unicode categories.
- A single `settings.load` per `run_pass` feeds both `[workers] rules` and stage skills/agent in every worker assignment that pass.
- `python3 -m unittest evals.test_limits evals.test_settings evals.test_worker evals.test_engine` passes on develop; full `discover` stays green.

## File ownership (implement stage)

| Path | Mode |
|------|------|
| `README.md` | read |
| `architecture/compliance.md` | read |
| `evals/test_engine.py` | write |
| `evals/test_limits.py` | write |
| `evals/test_reconcile.py` | read |
| `evals/test_session_logs.py` | read |
| `evals/test_settings.py` | write |
| `evals/test_worker.py` | write |
| `scripts/engine.py` | write |
| `scripts/limits_history.py` | write |
| `scripts/limits_probe.py` | read |
| `scripts/reconcile.py` | read |
| `scripts/session_logs.py` | read |
| `scripts/settings.py` | read |
| `scripts/worker.py` | write |

Narrow writes to the four code areas above unless a shared helper change is required (e.g. only `settings.py` if reload extraction is shared).
