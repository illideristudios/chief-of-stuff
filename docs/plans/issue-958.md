# Issue 958

A deny the trio voted for stays denied for that exact command. A deny the engine wrote because the trio split or was unavailable stays denied only for the attempt that received it. The next attempt asks again.

```
V := the latest linked decision is a trio verdict (decided_by = trio)
U := the deny is not a trio verdict (split, unavailable, or no decision row)
S := same stage and attempt as the ask that was denied
R := the same tool, input and worktree is asked again

1. V → ¬R                         [#957: an answered verdict is honoured]
2. U ∧ S → ¬R                     [this attempt stops, as it does today]
3. U ∧ ¬S → R                     [the open case: ask on the next attempt]
```

A person reversing a verdict is the same predicate. The reversal is the latest row, so it is not `decided_by = trio`, and the restarted attempt asks again. Today that reverse does not lift the deny: `_earlier` falls back to `ask.answer`, which stays `deny`.

## Behavior

`scripts/engine.py` stays as it is. A split still appends `deny` via `_settle_permission` (`decided_by` `guard`, reason "the trio did not adopt an answer"). An unavailable trio still answers the ask `deny`, and writes a decision row only when one already exists. A unanimous trio deny stays `decided_by` `trio`. Decisions stay append-only. Nothing is deleted or rewritten.

The change is which of those answers the hook honours. In `scripts/guard_rules.py`, one pure judgement: a deny binds this request only when the latest row `ask_decision` returns is `decided_by == "trio"`, or the ask's stage and attempt are the binding's. Any other deny does not bind. `scripts/worker_guard.py` asks `_earlier` for that row's `decided_by` (none when there is no row) and, when the judgement says the deny does not bind, falls through and records a new ask. The new ask is a new decision instance (`ask-{id}`), so the next pass consults the trio. The same attempt still gets the existing "was denied, stop" reply and does not record a second ask.

Match the command the way `_earlier` already does: same issue, `tool_name`, `tool_input` and `cwd`. An allow, and an ask that is still unanswered, keep today's behavior on every attempt.

## Tests

Red cases in `evals/test_guard.py` before the production change. The binding in that file is implement attempt 2. Seed a decision `ask_decision` can see, or the row is invisible and the case is not a verdict: topic `ask`, question and options equal to the ask, `record["decision"]["instance"] == f"ask-{id}"`, and `record["decision"]["context"]` equal to the ask context.

- A `guard` deny on attempt 2, asked again on attempt 2: deny, still one ask.
- That same command on attempt 3: a new unanswered ask.
- An ask answered `deny` with no decision row (the unavailable trio wrote nothing to reverse): same two outcomes.
- A `trio` deny on attempt 3: deny, still one ask.
- A `trio` deny followed by a later reversal row for the same identity (`answer` empty, `reversed` true): attempt 3 records a new ask.
- The existing same-attempt deny test, which writes no decision row, stays green.

## Out of scope

- No new backoff and no permission-specific cap. The stage retry limit already bounds how often a fresh attempt consults.
- No owner prompt for a permission split. #890 stands.
- No change to `_settle_permission`, `_settled`, or the ask's stored answer.
- No edit under `architecture/`. The architect records the doc gap.

## Risks

- A command the trio keeps splitting is consulted once per later attempt, three model calls each, until the stage retry limit. That limit is the cap.
- The signal is `decided_by == "trio"` on the latest linked row, not the `settled_by` wording. Reason text changes; the writer does not.
- A verdict whose row `ask_decision` cannot match looks like an unavailable deny and is asked again on the next attempt. Do not add a second matcher. The tests seed the record the existing matcher requires.
- A guard-rule deny never reaches this path: `permission_judgement` returns first. An old ask the rule settled as deny is not a trio verdict, so a later attempt asks again only when the command is still unjudgeable.

## Architect

`architecture/ARCHITECTURE.md` says a split is not put to the trio again, and the #957 note treated this sticky deny as a consequence of append-only decisions. After this change the old decision stays denied and is not consulted again. The hook records a new ask, and that new instance is what the trio sees. Append-only holds. The architect records that a permission deny which is not a trio verdict binds only the attempt that received it.

## File ownership

- write: `scripts/guard_rules.py`
- write: `scripts/worker_guard.py`
- write: `evals/test_guard.py`
- read: `scripts/engine.py`
- read: `scripts/decision_escalation.py`
- read: `scripts/guard_binding.py`
- read: `evals/test_engine.py`
