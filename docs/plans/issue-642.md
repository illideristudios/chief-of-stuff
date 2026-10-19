# Issue #642: 1.0 M5 — Decision gate in the engine: every ask through the trio; interrupts and limits

Implementation plan for `github:illideristudios/chief-of-stuff#642` (`docs/specs/2026-10-09-chief-of-stuff-1.0-plan.md` M5; design decisions D5a, D6, D12, D13; `architecture/ARCHITECTURE.md` §§4, 6, 10). The plan stage changes no runtime code.

## Status on `origin/develop`

This plan worktree (`work/issue-642-plan-1`) was cut from `origin/main` (`50091f80`), which predates the 1.0 engine modules. On `origin/develop`, M5 (`#642`) has already landed in PR `#750` (`ad8afce2` / `5bad1375`, *Consult the trio before an ask (#642)*) and been extended by:

- PR `#758` (`fe8b76e7`, `#643`): append-only board decision intents (`answer`, `reverse`, `re-route`), forge comment delivery, and exception pings;
- PR `#880` (`a9becdb2`): architecture §6 rules for append-only decisions, human-answer precedence, and `[models.trio]`;
- PR `#881` (`a9d66bb7`, `#827`): workspace `policy_overlay` merging over `assets/policy.toml`;
- PR `#891` (`0b49a8e1`, `#879`): trio model rotation drawing three distinct read-only entries from `[models.trio]` and recording `runtime` and `model` per vote;
- PR `#892` and PR `#900` (`76344fcd`, `99c6c8b0`, `#890`): permission asks settled by guard rule, the trio, or deny (never an owner interrupt);
- PR `#917` (`8c5b6776`, `#915`): plan-stage `file_ownership` proposals settled through the trio (`topic="scope"`).

The owner reopened and confirmed M5 scope on 2026-10-11 (`docs/specs/2026-10-09-chief-of-stuff-1.0-plan.md` M5: *"The owner, 2026-10-11: proceed with this scope as written."*). Commit `459be1e4` settled the shipped default trio (`["implementer", "senior-architect", "senior-staff-engineer"]`, all Sonnet, `architect` excluded) and marked `trios = {}` topic tags as explicit placeholders until the owner defines tag mappings.

```
Glossary
  A     := a decision or ask arises (worker ask, workflow zach_gate without standing approval, or scope proposal)
  S     := an unreversed, non-retryable settled decision row already matches (topic, question, instance, stage, attempt, context, options)
  B     := measured issue usage breaches a configured limit in resolved policy (spend_usd, quota_percent, time_minutes)
  T     := the engine consults the 3-persona trio through the metered model port (trio_gate.consult)
  U     := all 3 trio verdicts vote "adopt" for the same option (and, for a workflow gate, that option is not "hold")
  H     := a human answer (decided_by = "user") is recorded for that decision identity
  Ad    := the choice is adopted, persisted (decided_by = "trio"), and applied (ask answered, gate waived, or scope written)
  R     := a trio verdict failed with a retryable model error under MAX_TRIO_RETRIES (8) and with launchable rotation entries remaining
  Bk    := the engine records exponential backoff in resume.trio_retries and waits without pinging the owner
  Ia    := a deduplicated ambiguity interrupt is derived in resume.decision_interrupts for the owner (decided_by = "zach")
  Ie    := a deduplicated exhausted interrupt is persisted in resume.interrupt when stage or hold-release retries exceed retry_limit
  Il    := a deduplicated limit interrupt is persisted in resume.interrupt and blocks new dispatches, reviews, and consults
  Rev   := a board reversal is recorded on an adopted decision

Premises
  1. A ∧ ¬S ∧ ¬B → T                                  [plan M5.1; engine._choice, _consultation_blocked]
  2. A ∧ B → Il ∧ ¬T                                  [plan M5.3, M5.4; policy.breach stops consults and launches]
  3. T ∧ H → human answer wins (¬overwrite)            [§6; trio_gate.record with decision_actions.human_answer]
  4. T ∧ ¬H ∧ U → Ad ∧ ¬Ia                            [plan M5.1, M5 exit S1; unanimous trio resolves routine asks silently]
  5. T ∧ ¬H ∧ ¬U ∧ R → Bk ∧ ¬Ia                       [decision_actions.retryable_failure; transient outages back off]
  6. T ∧ ¬H ∧ ¬U ∧ ¬R → Ia                            [plan M5.1, M5.3; split, gate hold, or capped outage escalates once]
  7. Rev → open_reversal routes back to target stage, then replacements re-consult T on completion   [plan M5.2; §6]

Derivation
  8. from 1, 4: A ∧ ¬S ∧ ¬B ∧ ¬H ∧ U → Ad ∧ ¬Ia      (every routine ask with a unanimous trio proceeds with no ping)
  9. from 1, 6: A ∧ ¬S ∧ ¬B ∧ ¬H ∧ ¬U ∧ ¬R → Ia      (any concern or exhausted trio retry escalates to the owner once)
 10. from 2:    B → Il                                 (configured spend, quota, or time breach interrupts once and halts new work)

∴ every ask goes through the trio first, unanimous choices are applied without pinging, splits and capped failures escalate once, reversals re-route and re-consult, and configured limits halt and ping on breach.
```

## Architecture and Design on `origin/develop`

| Circle (§4) | Module | M5 Responsibility |
|---|---|---|
| Entities | `scripts/policy.py`, `assets/policy.toml` | `Policy` fields `trio`, `trios`, `spend_usd`, `quota_percent`, `time_minutes`, `retry_limit`, `zach_gates`, `approvals`; `_trio` validation (3 distinct known personas, never `architect`); `_limit` validation; `breach(resolved, usage)`; `overlay(shipped, over)` |
| Use Cases | `scripts/trio_gate.py` | `Decision`, `Outcome`, `select(policy, personas, tags)`, `consult(...)`, `decide(...)`, `record(store, key, update)` with `human_answer` lock guard |
| Use Cases | `scripts/decision_actions.py` | Append-only decision identity and state (`latest`, `needs_owner`, `owner_waits`, `pending_decisions`), retry cap (`MAX_TRIO_RETRIES = 8`, `retryable_failure`, `exhaust_retry`, `stamped_failures`), reversal and replacement lifecycle (`reverse`, `reverse_intent`, `restarted`, `open_reversal`, `replacements`, `preserve_ask_link`, `committed_ask_answer`), board intents (`current_intent`, `supersede`, `advance`, `block`, `unblock`), and forge comment extraction (`pending_comments`) |
| Use Cases | `scripts/reconcile.py` | Emits `Limit` on `policy.breach`, `Ask` on unwaived `zach_gates` without an active standing approval, `Escalate` when stage or hold-release retries exceed `retry_limit`, and routes `open_reversal` back to `workflow.start` |
| Use Cases | `scripts/model_port.py` | `Port`, `Request`, `Reply`, `Failure`, `RETRYABLE`, and `draw` for `[models.trio]` distinct read-only model selection |
| Interface Adapters, gateways | `scripts/store.py`, `scripts/settings.py` | Append-only `add_decision(..., unless=...)`, worker `asks` table (`add_ask`, `answer_ask`), `resume` state, and `settings.load_policy` / `workspace_policy` |
| Frameworks and Drivers | `scripts/engine.py`, `scripts/worker.py`, `scripts/claude_adapter.py`, `scripts/worker_guard.py` | `_choice`, `_metered` spend accounting, `_route_asks`, `_settle_asks`, `_settle_permission`, `_settle_scopes`, `_apply_intents`, `_deliver_comments`, `_interrupt` / `_clear_interrupt`, `_sync_decision_interrupts`, `_check_quota_limits`, and `worker._record_usage` |

### 1. M5.1 — Trio gate before any ask, with adopted choice applied and persisted

- **Validation and reuse (`scripts/trio_gate.py:16-148`)**:
  - `Decision.validate()` enforces at least two distinct non-empty option strings.
  - `decide()` checks `_settled()` against the newest row for `(topic, question, instance)`. A row is reused only when its `options`, `(stage, attempt)`, and `context` match and it is neither reversed/restarted nor a retryable model failure.
- **Consultation and model diversity (`scripts/trio_gate.py:98-181`, `#879`)**:
  - `_members()` draws three distinct launchable entries from `[models.trio]` via `model_port.draw` (preferring distinct runtimes, falling back to persona models when no rotation is configured).
  - `_ask()` invokes the model port in read-only mode with a strict JSON schema (`vote: "adopt" | "concern"`, `choice` in `options`, non-empty `reasoning`). On a `quota` failure it rotates that member to the next unused entry in `[models.trio]`.
  - Every verdict records `persona`, `vote`, `choice`, `reasoning`, `runtime`, and `model`.
- **Adoption vs. escalation (`scripts/trio_gate.py:140-181`, `scripts/engine.py:167-201`)**:
  - When all three verdicts vote `"adopt"` for the same `choice` (and not `"hold"` on a workflow `gate`), `consult()` records `answer=recommendation, decided_by="trio"`.
  - Engine application:
    - Worker `ask` rows (`_route_asks`, `_settle_asks`): `store.answer_ask(key, ask.id, answer)` persists the chosen option on the ask row and unblocks readiness.
    - Workflow gate `Ask` (`reconcile.Ask` in `run_pass`): adds `action.gate` to `resume["waived_gates"]`, allowing the stage transition on the next pass (`_clear_waived_gates` clears it when the stage moves).
    - Permission asks (`_settle_permission`, `#890`): settled by `guard_commands.permission_judgement`, the trio, or `"deny"` on split/exhaustion.
    - Plan scope proposals (`_settle_scopes`, `#915`): settled by the trio under `topic="scope"`, options `("approve", "reject")`.
  - Any `concern` vote, split `choice`, malformed verdict, or `gate` `"hold"` records `answer=None, decided_by="zach"`.
  - Transient model outages (`retryable_failure`) record `"failures": failures + 1` with `decided_by=""` and schedule exponential backoff (`60 * 2**min(failures, 6)` seconds, capped at 3600s) in `resume["trio_retries"]`. At `MAX_TRIO_RETRIES = 8` (or when every `[models.trio]` entry stops on quota), `exhaust_retry` records `decided_by="zach"` and `"retry_exhausted": failures`.

### 2. M5.2 — Shipped topic selection, forge record, and board reversal

- **Shipped topic selection (`assets/policy.toml:20-35`, `scripts/policy.py:173-198`, `scripts/trio_gate.py:46-55`)**:
  - `assets/policy.toml` ships `trio = ["implementer", "senior-architect", "senior-staff-engineer"]` (all Sonnet) and `trios = {}` (placeholder map for topic tags).
  - `policy._trio` validates that `trio` and every entry in `trios` name three distinct known personas and never `"architect"`.
  - `trio_gate.select(policy, personas, tags)` selects `policy.trios[tag]` for the first matching tag in `tags`, or `policy.trio` when none matches.
- **Append-only record and forge comment delivery (`scripts/trio_gate.py:90-100`, `scripts/engine.py:467-481`)**:
  - `trio_gate.record` is the single writer to `store.add_decision`, passing `unless=person_first` so a human answer (`decided_by == "user"`) is never overwritten by a late trio write.
  - `engine._deliver_comments` posts unsent `record["comment"]` bodies from `decision_actions.pending_comments` to the forge issue via `forge.comment(number, body, commit=True)` (`OP_DECISION_COMMENT`) and records sent comments in `resume["decision_comments"]`.
- **Board reversal (`scripts/decision_actions.py:205-263, 470-620`, `scripts/reconcile.py:415-416`, `scripts/engine.py:344-465`)**:
  - Reversing an adopted decision (`decision_actions.reverse` or board `respond(..., action="reverse", ...)`) appends a row with `reversed: True` and `answer=None`.
  - `reconcile.plan` (`open_reversal`) routes the item back to `workflow.start` (or `_apply_intents` moves it to `_reverse_target` and advances the intent from `"write"` to `"run"` and then `"restarted"` with `restart_attempt`).
  - Once the restarted stage segment completes with `outcome == "done"`, `decision_actions.replacements` pairs the reversal origin with the completed segment, `_link_replacement_asks` creates or links the replacement ask, and `_route_asks` consults the trio fresh without reusing pre-reversal answers.

### 3. M5.3 — Deduplicated interrupts: ambiguity, exhausted retries, approaching limit

- **Ambiguity (`scripts/engine.py:250-274`, `scripts/decision_actions.py:166-183`)**:
  - `_sync_decision_interrupts` derives `resume["decision_interrupts"]` from `decision_actions.owner_waits(decisions, segments, asks, workflow.start, passed)`.
  - Each active decision attempt awaiting the owner gets one entry `{"kind": "ambiguity", "reason": pending.reason, "decision": {"id": [...], "stage": ..., "attempt": ...}}`, logging `ambiguity: <reason>` only when a new decision key enters `decision_interrupts`. Answering the decision, passing its gate via a standing approval, or superseding its attempt via a reversal restart removes the entry.
- **Exhausted retries (`scripts/reconcile.py:429-430, 480-496`, `scripts/engine.py:119-127, 942-952`)**:
  - When failed stage attempts or hold-release failures exceed `resolved.retry_limit`, `reconcile.plan` returns `Escalate(key, reason, stage=...)`.
  - `engine.run_pass` calls `_interrupt(store, key, "exhausted", action.reason, at, scope={"stage": action.stage})`, which writes `resume["interrupt"]` and logs `exhausted: <reason>` only when `data.get("interrupt") != record`, blocks any active `"run"` intent, and retires engine stage ownership.
- **Approaching or breached limit (`scripts/reconcile.py:490-510`, `scripts/engine.py:211-220, 745-780, 942-945`)**:
  - When `policy.breach(resolved, usage)` returns `"spend"`, `"quota"`, or `"time"`, `reconcile.plan` returns `Limit(key, kind, f"reached {kind} limit", stage=...)` and `_consultation_blocked` blocks trio consultations, each calling `_interrupt(store, key, "limit", f"reached {kind} limit", at, scope={"stage": stage})`.
  - `_check_quota_limits` also checks account-wide runtime readings from `limits_history` through `limits.near_cap` (`[limits] warn_at`, §10) and records a deduplicated `"limit"` interrupt when a rolling window is at or near cap.

### 4. M5.4 — Limit config with workspace override and per-issue spend and time recording, no defaults

- **Policy limits and workspace override (`scripts/policy.py:48-64`, `scripts/settings.py`, `assets/policy.toml`)**:
  - `Policy` defines `spend_usd: float | None = None`, `quota_percent: float | None = None`, and `time_minutes: float | None = None`. `assets/policy.toml` ships no default values for them (`None` = unset).
  - Limits are overridable per persona, stage, and room in policy tables, and per workspace via `policy_overlay` in `chief-of-stuff.toml` (`settings.workspace_policy`, `#827`).
- **Per-issue spend and time recording (`scripts/worker.py:66-100, 566-568`, `scripts/engine.py:129-146`)**:
  - `worker._record_usage` accumulates `reply.cost_usd` (for both `Reply` and `Failure`) into `resume["spend_usd"]` and the segment's elapsed minutes (`(ended - started).total_seconds() / 60.0`, using `exited_at` from `exit.json`) into `resume["time_minutes"]`, guarded by `resume["recorded_usage"]` so each `(stage, attempt)` is counted once. An observed quota stop also records `quota_percent = 100.0` while the quota backoff is active.
  - `engine._metered` wraps the model port passed to `trio_gate.decide` so every trio consultation call (`Reply` or `Failure`) adds `got.cost_usd` to `resume["spend_usd"]`.

## Implementation Steps

Because M5 (`#642`) is already implemented and verified on `origin/develop` (`ad8afce2` / `#750` and subsequent commits through `956a5ef6`), the `implement` stage must not rebuild M5 from `origin/main` (`50091f80`):

1. **Base on `origin/develop`.** Ensure the implementation worktree is at or fast-forwarded to `origin/develop`, where `scripts/trio_gate.py`, `scripts/decision_actions.py`, `scripts/policy.py`, `scripts/reconcile.py`, `scripts/engine.py`, `scripts/worker.py`, `assets/policy.toml`, and their `evals/test_*.py` suites already live.
2. **Run the M5 and full verification suites on `origin/develop`.** Confirm all M5.1–M5.4 and M5 exit tests pass with zero failures, skips, or xfails:
   ```text
   python3 -m unittest evals.test_trio_gate evals.test_decision_actions evals.test_policy evals.test_reconcile evals.test_worker evals.test_engine evals.test_guard evals.test_store evals.test_architecture
   python3 -m unittest discover -s evals -p 'test_*.py'
   ```
3. **Avoid duplicate changes.** If all suites are green on `origin/develop`, make no duplicate code changes. Only if a regression is found on `origin/develop` should a minimal red-then-green fix and plugin version bump (`.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`) be committed.

## Tests

On `origin/develop`, the M5 requirements and exit criteria (S1) are covered by:

| Test file | Coverage |
|---|---|
| `evals/test_trio_gate.py` | Topic tag selection (`select`), fallback to default trio, `architect` refusal, unanimous adoption, split escalation, malformed/failed model replies, `decide` reuse vs. re-consultation on changed options/context/stage/attempt or reversal, `[models.trio]` rotation and quota fallback (`#879`), and `human_answer` precedence |
| `evals/test_decision_actions.py` | `needs_owner`, `owner_waits`, `pending_decisions`, restart supersession across `plan` and `implement`, standing approvals releasing gates, naive timestamp tolerance, rescoped return-edge gates, `reverse` / `restarted` / `replacements`, and board intents (`current_intent`, `supersede`, `advance`, `block`, `unblock`, `pending_comments`) |
| `evals/test_policy.py` | Shipped default trio (`implementer`, `senior-architect`, `senior-staff-engineer` on Sonnet), placeholder markers on unsettled personas/tags, `architect` refused in `trio` and `trios` at every scope, `spend_usd` / `quota_percent` / `time_minutes` defaults (`None`) and finite validation, `breach`, and workspace `policy.overlay` (`#827`) |
| `evals/test_reconcile.py` | Limit breach (`Limit`) halting new dispatches and reviews, room/stage limit overrides, `Ask` on `zach_gates` without standing approval, `Escalate` on `retry_limit`, and `open_reversal` routing back to `workflow.start` |
| `evals/test_worker.py` | `collect` recording host `spend_usd` and elapsed `time_minutes` from `exited_at`, idempotent `recorded_usage` on retry, and `quota_percent = 100.0` on quota stop |
| `evals/test_engine.py` | M5 exit (S1): `test_a_unanimous_trio_adopts_an_ask_with_no_ping`, `test_a_split_trio_escalates_once`, `test_a_reached_limit_pings_once_and_does_not_launch`, `test_a_reversed_decision_restarts_once_and_reconsults`, gate approvals and holds, exponential trio backoff and durable retry cap (`MAX_TRIO_RETRIES`), crash recovery of decision interrupts and reversals, metered trio spend on replies and failures, permission ask settlement (`#890`), and plan scope proposal settlement (`#915`) |
| `evals/test_guard.py` | Unjudgeable worker permission requests recorded as asks and routed through the trio (`#874`, `#890`) |
| `evals/test_architecture.py` | Circle placement and inward-only dependency enforcement across `policy`, `trio_gate`, `decision_actions`, `reconcile`, `model_port`, `store`, `worker`, and `engine` |

In this plan worktree (`50091f80`), the pre-cutover architecture, settings, and ownership suites pass (`python3 -m unittest evals/test_architecture.py evals/test_settings.py evals/test_ownership.py`: 78 passed, 0 failed, 0 skipped, 0 xfailed).

## Risks

- **Branch base skew (`origin/main` vs `origin/develop`).** `origin/main` (`50091f80`) does not contain `scripts/engine.py`, `scripts/trio_gate.py`, `scripts/decision_actions.py`, or `scripts/policy.py`. Attempting to implement M5 from `origin/main` instead of `origin/develop` would collide with PR `#750` (`ad8afce2`) and subsequent merges.
- **Placeholder policy entries (`C22`, `#638`).** `assets/policy.toml` intentionally keeps `trios = {}` and reviewer persona placeholders until `#638` and owner tag definitions land; `evals/test_policy.py` pins both the three Sonnet trio personas and the `PLACEHOLDER` markers. Do not invent topic tags or remove placeholder markers in `#642`.
- **Trio re-consultation loops.** A split or capped trio consultation records `answer=None, decided_by="zach"`. `_settled` must continue returning `Outcome(None, False, ...)` for non-retryable unanswered rows so subsequent engine passes wait on the owner rather than calling the model on every pass.
- **Human answer vs. in-flight trio race.** A person can answer on the board while a trio consultation is running; `trio_gate.record` must keep using `store.add_decision(..., unless=person_first)` so a model verdict never overwrites a human answer (`§6`).
- **Quota stop vs. configured limit breach.** A transient host quota stop waits until `retry_at` and rotates `[models.trio]` / worker models; only reaching a configured `quota_percent`, `spend_usd`, or `time_minutes` limit (or `limits.near_cap` at `warn_at`) emits the `limit` interrupt.

## Proposed File Ownership

| Mode | Path |
|---|---|
| `write` | `assets/policy.toml` |
| `write` | `scripts/claude_adapter.py` |
| `write` | `scripts/decision_actions.py` |
| `write` | `scripts/engine.py` |
| `write` | `scripts/model_port.py` |
| `write` | `scripts/policy.py` |
| `write` | `scripts/reconcile.py` |
| `write` | `scripts/store.py` |
| `write` | `scripts/trio_gate.py` |
| `write` | `scripts/worker.py` |
| `write` | `scripts/worker_guard.py` |
| `write` | `evals/test_architecture.py` |
| `write` | `evals/test_decision_actions.py` |
| `write` | `evals/test_engine.py` |
| `write` | `evals/test_guard.py` |
| `write` | `evals/test_policy.py` |
| `write` | `evals/test_reconcile.py` |
| `write` | `evals/test_store.py` |
| `write` | `evals/test_trio_gate.py` |
| `write` | `evals/test_worker.py` |
| `read` | `architecture/ARCHITECTURE.md` |
| `read` | `docs/specs/2026-10-09-chief-of-stuff-1.0-design.md` |
| `read` | `docs/specs/2026-10-09-chief-of-stuff-1.0-plan.md` |
| `read` | `docs/plans/issue-642.md` |
