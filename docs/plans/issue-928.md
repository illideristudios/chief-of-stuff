# Implementation plan for #928: retry unstructured trio answers as outages

## Scope and verified baseline

This stage changes only this plan. Implement the remaining #928 gap; do not change repeated-consult identity (#927), rotation selection (#879), worker retries, or live decision stores.

The assigned worktree starts at `50091f80` plus the earlier plan commit `59f7c030`; it has no engine modules or engine evals. The earlier plan's instruction to use `origin/develop` is stale: that ref no longer exists locally. The engine code is present in the inspected `origin/main` snapshot, `17552bbd` (plugin version `0.129.114`). Implementation must use an engine-bearing base selected by the engine and recheck the functions below before editing. This plan stage does not switch branches or merge anything.

Already present in that snapshot:

- `scripts/trio_gate.py:TIMEOUT_S` is 300 seconds (commit `6c9046f4`), addressing Cursor answers that took longer than the issue's original 60-second limit.
- `trio_gate._ask` replaces a member once after a non-quota `Failure`, using another rotation entry when available. Quota stops advance through the available entries.
- `scripts/engine.py:TRIO_PASS_BUDGET_S` is 480 seconds. Each call receives the lesser of 300 seconds and the remaining pass budget; no call starts after the budget expires.
- `decision_escalation.retryable_failure`, `exhaust_retry`, and `engine._choice` keep retryable outages pending, apply exponential backoff, and escalate after `MAX_TRIO_RETRIES = 8`. A settled owner decision is reused rather than consulted again.

Do not recreate these mechanisms or raise their limits. The architecture already specifies the intended behavior in `architecture/ARCHITECTURE.md` §6: a timeout or answer without structure gets one replacement, and a second failure is retried as an outage rather than treated as disagreement.

## Remaining defect and intended result

`host_adapter.judge` is the trio's read-only adapter for Codex, Cursor, and agy. It currently returns:

| Host result | Current failure kind | Decision behavior after replacement also fails |
| --- | --- | --- |
| Subprocess timeout | `unknown` | Pending outage with backoff |
| Quota stop | `quota` | Rotation/backoff or exhausted-quota escalation |
| Unreadable answer (`_structured` raises a caught parsing/read exception) | `error` | Immediately waits on owner |
| Missing structured answer (`_structured` returns `None`) | `error` | Immediately waits on owner |

The latter path includes agy `status != SUCCESS`, absent/null `structured_output`, Cursor `is_error`, Cursor result text without a JSON object, and a missing Codex answer file. It matches the issue's `agy gave no structured answer` evidence.

`model_port.RETRYABLE` contains only `quota` and `unknown`. `_ask` replaces an `error` once, but `retryable_failure` does not recognize a second `error` as an outage. `consult` therefore stores `decided_by="zach"`; `decide` reuses that row as settled. The problem is the host classification, not the existing replacement or backoff logic.

After the change, an unreadable or absent structured answer follows the timeout path. One failed host can be replaced and still yield three valid, matching adopt votes. If its replacement also fails, the decision stays unanswered and pending until its retry deadline. Recovery can then adopt normally; persistent failure becomes an explicitly exhausted outage at the existing cap.

## Implementation sequence

1. **Write failing adapter evals.** In `evals/test_host_adapter.py:JudgeTest`, split the existing quota/no-answer test and add parameterized failure cases. Assert the results are `Failure(kind="unknown")`, `.retryable` is true, and the message still names the runtime and whether the answer was unreadable or unstructured. These assertions must fail against the inspected implementation.
2. **Make the narrow production change.** In `scripts/host_adapter.py:judge`, change the two `Failure("error", ...)` returns for unreadable and missing structured answers to `Failure("unknown", ...)`. Retain the existing diagnostic messages. Do not add raw stdout tails to persisted verdicts: they are unnecessary for the classification fix and may contain arbitrary model output. Keep `classify`, `_structured` extraction rules, and `model_port.RETRYABLE` unchanged. This keeps worker failure classification unchanged.
3. **Pin the decision behavior through the adapter.** Extend `evals/test_trio_gate.py:TrioModelsTest` with a model port that calls the real `host_adapter.judge` for failing host entries, mocking CLI resolution, login argv, and subprocess responses. Successful entries can return the existing valid reply fixture. A test that merely constructs `Failure("unknown", ...)` would pass before this fix and cannot catch the classification regression.
4. **Pin retry scheduling and recovery.** Extend `evals/test_engine.py` using its existing fake clock/store fixtures and the same adapter-produced failure path. Verify no repeated call before the retry deadline, successful adoption after recovery, and owner escalation only at the existing cap. Reuse current retry/crash coverage rather than introducing a new scheduler or retry counter.
5. **Release metadata.** Increment the patch version consistently in `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`, choosing the next version from the implementation base rather than pinning a number here. Commit the tests/fix and metadata in small meaningful commits; never merge. Installation and live-engine operation remain outside this implementation change.

## Test matrix and acceptance

Adapter tests use local fakes only; no real model calls, credentials, or timed sleeps:

- Cursor error envelope; successful envelope whose result has no JSON object; malformed/empty JSON stdout.
- agy failed status; SUCCESS with absent or null `structured_output`; malformed JSON stdout.
- Codex absent answer file and invalid JSON in that file. Extend the test helper to write invalid answer text when necessary.
- Existing valid host outputs still produce `Reply.data`; quota stays `quota`; missing CLI/read-only mode stays `missing_program`; a simulated `TimeoutExpired` stays retryable and receives `request.timeout_s`.

Decision tests exercise these observable outcomes:

1. Cursor produces no structured answer; a distinct replacement returns a valid adopt vote. All three final votes adopt the same option, so the decision is unanimous with the expected recommendation and final runtime/model identities.
2. Cursor and its agy replacement both return actual unstructured host envelopes. With the existing deterministic rotation, requests are Claude, Cursor, agy, Codex. Assert `answer is None`, `decided_by == ""`, `failures == 1`, no recommendation/unanimity, and `retryable_failure(row.record)` is true. Cover unreadable output as well as missing structure. Also cover a rotation with no spare replacement: it must remain a retryable outage, without extra calls.
3. Through the engine, that outage records a future retry deadline and no owner ambiguity interrupt. Reopening the store and running before the deadline makes no model calls. At the deadline, healthy replies settle the decision and clear its retry state.
4. Persistent adapter-produced outages reach the existing eight-consultation cap, produce one owner escalation identifying unavailability, and make no further calls on later passes. A genuine well-formed concern vote still settles as disagreement and waits on the owner immediately. A human answer that arrives during consultation remains authoritative (existing regression coverage).

A failure placeholder is still persisted with `vote="concern"`, `choice=null`, and `model failure (unknown): ...` for record compatibility. Acceptance is that this placeholder routes through outage handling and cannot settle the decision as a substantive concern before exhaustion. Do not falsely infer unanimous adoption from the two successful votes, change the stored verdict schema, or alter board rendering in this issue.

Run the focused evals first on the implementation base:

```text
python3 -m unittest discover -s evals -p 'test_host_adapter.py'
python3 -m unittest discover -s evals -p 'test_trio_gate.py'
python3 -m unittest discover -s evals -p 'test_engine.py'
```

Then run the repository suite:

```text
python3 -m unittest discover -s evals -p 'test_*.py'
```

Record actual passed, failed, skipped, and xfailed counts. The plan-stage suite runs against this assigned older worktree; it validates the plan branch baseline, not the proposed engine change. The implementation must run its own focused and full suites on the engine-bearing base. Inspect the diff to confirm production edits are limited to the two failure classifications and synchronized version metadata.

## Risks and verification after release

- **Base mismatch:** implementation cannot apply this plan to the assigned older base without the engine modules. Revalidate the current engine base and ownership before starting; do not copy modules from another branch into this plan worktree.
- **Persistent host misconfiguration:** Cursor error envelopes and agy failed status can represent permanent problems. They will now use the existing bounded replacement/backoff path before owner escalation. Missing executables remain immediately identifiable. This change does not establish why agy omits its structured output or make a slow host faster.
- **Existing owner rows:** historical `model failure (error)` rows remain settled under current logic. This code change prevents new misclassification; it does not silently rewrite/reopen old decisions, rerun their possible side effects, or clear the issue's existing backlog. Any historical repair requires separate, explicit lifecycle handling.
- **Malformed verdicts:** validly decoded data that fails the vote schema still follows `malformed verdict` handling. This change covers missing/unreadable answers reported as `model failure`, matching the issue's evidence. Count malformed verdicts separately during rollout rather than assuming this fix covers them.
- **Cost and latency:** retries reconsult all members; successful votes are not carried between consultations. Existing 300-second call limits, the shared 480-second pass budget, and capped backoff bound the extra work. Exhausted outage escalation remains intentional.
- **Record consumers:** stored failure placeholders still say `concern`. Outage status depends on `choice=null` and the existing failure prefix/kind. Preserve those formats and verify actual owner routing, rather than relying only on a count of concern labels.

After installation by the release process, rerun the issue's read-only aggregation on both stores with `created_at` filtered to the installation timestamp. Count failures by runtime/kind, pending retryable outages, exhausted retries, malformed verdicts, and owner escalations. New host no-readable/no-structured failures must use `model failure (unknown)`, never `model failure (error)`. They must remain pending until recovery or explicit retry exhaustion; complete valid matching votes must adopt. Compare timeout rates separately: this patch changes outage routing, and does not promise a lower underlying host failure rate or unanimity for genuine disagreements. No live-store reads or writes are needed for deterministic implementation tests.

## Proposed implementation file ownership

| Path | Mode | Purpose |
| --- | --- | --- |
| `scripts/host_adapter.py` | write | Classify absent/unreadable judgement answers as retryable outages |
| `evals/test_host_adapter.py` | write | Host-envelope and timeout regression cases |
| `evals/test_trio_gate.py` | write | Adapter-to-decision replacement and pending-outage coverage |
| `evals/test_engine.py` | write | Deadline, recovery, and capped outage integration coverage |
| `.claude-plugin/plugin.json` | write | Release patch version |
| `.claude-plugin/marketplace.json` | write | Matching release patch version |
| `scripts/trio_gate.py` | read | Existing selection, replacement, recording, and reuse |
| `scripts/decision_escalation.py` | read | Existing outage detection, owner routing, retry cap |
| `scripts/model_port.py` | read | Failure kinds and shared retry semantics |
| `scripts/engine.py` | read | Judge dispatch, pass budget, persisted retry scheduling |
| `scripts/runtimes.py` | read | Host read-only judgement argv |
| `architecture/ARCHITECTURE.md` | read | Canonical §6 outage requirements |
| `CLAUDE.md` | read | Implementation and test conventions |
