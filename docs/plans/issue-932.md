# Issue #932: preserve the identity of refused worker starts

## Outcome and scope

A failed start must retain the runtime, model, and effort selected for that attempt. The issue page must show, for example, `cursor composer-2.5 · plan · attempt 3` with `failed to start · 0m`. Performance aggregation must attribute that failed attempt to the same runtime/model pair. Successful starts, retry accounting, concurrency, hold restoration, and process cleanup keep their existing behavior.

This stage changes only this plan. Implementation must not repair production databases, infer historical models from today's rotation, change model selection policy, or expand into #901's process-lifecycle work.

## Source baseline and implementation prerequisite

The assigned worktree is based on `50091f80bed51d55e16666f830fa37b9b0a4c1b5` (plugin version 0.128.67). It contains the older tracker-based flow, without `scripts/engine.py`, `scripts/worker.py`, `scripts/slots.py`, or `scripts/store.py`. The reported bug cannot be implemented or reproduced against this base.

For planning, the engine sources were inspected read-only through the existing local Git ref `origin/develop`, at `4417a8fedba45a1460e172913c1e73a26bfabe00`. No ref was fetched, checked out, merged, or rebased. The implementation must start from the engine-bearing development line selected by the engine/maintainer and recheck these source locations. Do not recreate the newer engine in this old checkout or change the assigned plan branch's base as part of this stage.

Observed in that development snapshot:

- `scripts/engine.py`, `run_pass`: the Dispatch/Review path passes `runtime="claude"` to `slots.claim`. Its failed-launch handler recreates a zero-duration `failed` segment with `runtime="claude"` and no model or effort.
- `scripts/worker.py`, `launch`: `_model` selects a `ModelEntry` after preflight and worktree preparation. Adapter startup and identity verification can then raise. `_save_run(..., model=str(chosen))` and the returned model/effort/runtime become available only after verification succeeds. Earlier resume data may describe a previous attempt.
- `scripts/slots.py`, `claim`: reserves an open segment under the lock, launches outside the lock, deletes the reservation on any launch exception, and stamps successful identity from a two-element Claude return tuple or a three-element other-host tuple.
- `scripts/engine.py`, `_fail_start`: restores stage/hold state and may itself raise a `ForgeError`. Failure identity must survive that second error.
- `scripts/store.py`, `Segment` and `record_segment`: runtime, model, effort, and agent type already exist; omitted fields preserve prior values. A schema migration is unnecessary.
- `scripts/issue_page.py`: the store-backed Workers rows render segment runtime/model and outcome directly. `scripts/performance.py` groups directly by runtime/model/effort/agent type.
- `scripts/reconcile.py`, `_scope_replans`: already treats `outcome == "failed"` with equal start/end timestamps as a failed launch. Preserve that representation rather than adding a new persisted outcome.

## Implementation

1. **Carry the selected entry out of a failed launch.** Add a small explicit exception contract in `scripts/worker.py`, such as `StartFailed`, containing the immutable selected `ModelEntry` and the original exception. Once `_model` returns, cover request construction, branch checks, adapter startup, verification, and durable run recording with that contract. Preserve the original message and exception cause for logs. Retain the existing inner cleanup behavior: a spawn refusal has no runner to cancel; a runner that fails verification still receives cancellation, session cleanup, host cleanup, and teardown. Do not wrap `SystemExit` or `KeyboardInterrupt` as ordinary failed launches.

   Select only once. Do not reconstruct the entry from the rotation's first element, call `_model` again on failure, or read the previous stage-level resume model as the current attempt. Failures before selection have unknown runtime/model, not an assumed Claude identity. Preserve durable-run adoption using the entry that actually ran; never reroll an adopted attempt. Update direct launch tests that assert the original exception type to assert the explicit failure contract and its cause instead.

2. **Persist the attempt's selected identity through engine failure handling.** In `run_pass`'s `start` closure, capture `StartFailed`'s runtime/model/effort before invoking `_fail_start`, alongside the existing `launch_failed` flag. In the outer claim failure handler, recreate the same single `(issue_key, stage, attempt)` failed segment with that captured identity, the existing agent type, and equal start/end timestamps. This must work even when hold restoration raises a replacement error. A generic error before selection records unknown identity. Keep the original failure reason, stage move, retry budget, and hold-restoration logging.

   Remove the unconditional Claude identity from the initial `slots.claim` call. A provisional reservation may have unknown identity until launch establishes it. A rejected claim, failed hold release before launch, or unsupported session policy must not become a fabricated failed host attempt.

3. **Keep successful identity recording correct.** In `scripts/slots.py`, explicitly map the existing successful `(model, effort)` launch result to runtime `claude`; keep `(model, effort, runtime)` authoritative for other hosts. Otherwise removing the engine's provisional Claude value would blank successful Claude records. Preserve explicitly supplied identity when a test/custom launcher returns `None`. Keep the lock boundary, duplicate/cap checks, reservation deletion, and successful timestamp behavior unchanged. Slots alone must still drop a failed reservation; the engine owns recording the durable failed attempt.

4. **Render the startup status without changing stored outcomes.** In the store-backed Workers branch of `scripts/issue_page.py`, display `failed to start` for the existing failed-launch sentinel (`outcome == "failed"` and nonempty equal start/end timestamps). Render the stored runtime/model. Keep ordinary running and completed/failed run statuses intact. Use exact timestamps, not a displayed duration of `0m`, so a genuine subminute failed run remains `failed`. Avoid introducing a new outcome that would alter reconciliation or performance scoring. Keep existing HTML escaping and the tracker-backed rendering path.

5. **Prove attribution through consumers.** Pass the engine-recorded failed segment into `performance.table` and render the issue page in regression coverage. The existing grouping logic should need no production change. Historical rows remain as recorded; their original selection cannot be recovered reliably from their empty model field.

## Tests and acceptance

Start with failing behavioral regressions on the engine-bearing base, then implement the changes in small commits. Use temporary stores, generic issue fixtures, fake adapters, deterministic choices, and a fake clock; no live Cursor/Claude invocation, forge write, or production-store access is needed.

| Test location | Required evidence |
| --- | --- |
| `evals/test_worker.py` | Select Cursor `composer-2.5` and refuse because `.cursor/mcp.json` exists; assert selected runtime/model/effort and unchanged refusal reason in `StartFailed`. Separately fail identity verification and assert the same metadata plus existing cleanup. Cover a Claude spawn failure, random selection called once, cross-runtime quota fallback, and failure before selection without borrowed identity. Retain successful and durable-adoption coverage. |
| `evals/test_engine.py` | Drive actual `worker.launch` through the engine/slots boundary with fake host startup, rather than only fabricating an exception. Both reported Cursor failures produce exactly one closed failed segment with the selected entry, correct stage/attempt/agent type, zero running slots, and expected hold/retry state. Include Dispatch and Review paths, a previous Claude attempt followed by a refused Cursor attempt, and hold restoration failing after the selected launch fails. Keep existing slot-race, failed-release, restart/reversal, and retry-exhaustion regressions green. |
| `evals/test_slots.py` | Unknown provisional identity does not imply Claude; two-element successful return records Claude, three-element return records the returned host, and a `None` return preserves supplied identity. A failed launch still deletes its reservation and frees the slot. Existing concurrency and duplicate tests continue to pass. |
| `evals/test_m6_store_board.py` | Store-backed issue route renders the failed segment as Cursor plus `composer-2.5`, correct plan attempt, and `failed to start · 0m`. Reopen the store before rendering to prove persistence. Include a successful row and a genuine subminute failed run with unequal timestamps to prevent status misclassification. |
| `evals/test_performance.py` | The recorded failed attempt contributes one run, zero completed runs, and zero minutes to Cursor/composer-2.5 (including selected effort), without contaminating the previous Claude group. Preserve current scoring formulas. |

After focused regressions, run the development line's documented full suite (`python3 -m unittest discover -s evals -p 'test_*.py'`) using its supported dependency environment. Report all four final counts: passed, failed, skipped, xfailed. Acceptance requires passed > 0 and all other counts zero. Recheck the implementation diff for unintended schema, policy, architecture, or production-data edits.

## Risks and mitigations

- **Wrong starting revision:** this plan branch predates the engine. Implementation on an engine-bearing base is a prerequisite; baseline tests below do not validate the engine fix.
- **Identity differs from the attempted host:** random picks, quota fallback, skipped unavailable hosts, and stale resume data defeat reconstruction. Carry the actual selected entry from the same call that builds the host request.
- **Cleanup or hold failures mask identity:** capture metadata before `_fail_start` and preserve the original cause. Keep #901 cleanup and both error messages; test a secondary restoration failure.
- **Successful Claude identity regresses:** dropping the provisional default requires explicitly recording Claude for successful two-element results. Cover both result shapes and adoption.
- **State-machine regression:** retain `failed`, exact zero-duration launch timestamps, reservation rollback, and attempt keys. Do not invent failed attempts for capacity/duplicate or hold-release refusals.
- **Misleading startup labels:** use the existing exact failed-launch sentinel, not rounded duration or missing model. Leave legacy attribution untouched rather than guessing.

## Proposed implementation file ownership

All paths are repository-relative and refer to the engine-bearing implementation base.

| Mode | Path | Purpose |
| --- | --- | --- |
| write | `scripts/worker.py` | Selected-entry failure contract and launch propagation. |
| write | `scripts/engine.py` | Capture and persist failed-start identity. |
| write | `scripts/slots.py` | Neutral reservation and explicit successful runtime handling. |
| write | `scripts/issue_page.py` | Failed-start display. |
| write | `evals/test_worker.py` | Adapter refusal, verification, selection, and adoption regressions. |
| write | `evals/test_engine.py` | Durable attribution through engine/worker/slots and hold behavior. |
| write | `evals/test_slots.py` | Successful identity and reservation lifecycle coverage. |
| write | `evals/test_m6_store_board.py` | Persisted segment and issue-route rendering coverage. |
| write | `evals/test_performance.py` | Attribution grouping regression. |
| read | `scripts/store.py` | Existing segment schema and upsert semantics. |
| read | `scripts/settings.py` | `ModelEntry`, rotations, and pick modes. |
| read | `scripts/host_adapter.py` | Cursor refusal and other-host start/cleanup contracts. |
| read | `scripts/claude_adapter.py` | Claude start and cancellation contracts. |
| read | `scripts/reconcile.py` | Retry accounting and failed-launch sentinel. |
| read | `scripts/performance.py` | Existing grouping and scoring semantics. |
| read | `architecture/ARCHITECTURE.md` | Engine and performance requirements. |
| read | `CLAUDE.md` | TDD and validation instructions. |
| read | `requirements.txt` | Supported engine test dependencies. |

## Planning-stage validation

Only this Markdown file is changed. The final baseline run in the assigned old checkout was:

```text
env -u CHIEF_OF_STUFF_WORKSPACE PYTHONDONTWRITEBYTECODE=1 python3 -m unittest evals.test_runtime_backends evals.test_runtime_launch evals.test_issue_page evals.test_workers_page evals.test_flow
Ran 127 tests — OK
passed=127, failed=0, skipped=0, xfailed=0
```

An initial run of the same suite inherited `CHIEF_OF_STUFF_WORKSPACE`: the empty-workspace flow test followed that variable into a live workspace and rejected its newer model-config format (126 passed, 1 failed). Removing only that inherited variable isolated the fixtures and made the same suite green. No source or test changes were made to obtain that result. These are baseline results, not implementation regression results; the newer engine tests are absent from this checkout.
