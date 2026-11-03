# Plan: forge-only workflows merge without worker findings (#1025)

## Problem

A workflow that omits `review` treats forge approval as its review. `reconcile._merge` accepts `review_state == "approved"` (`scripts/reconcile.py:311-312`) and then returns `Wait("review findings are unknown")` whenever `counts is None` (`scripts/reconcile.py:313-314`).

`counts` is supplied only by `reconcile.review_findings`, and the engine omits the issue from `open_findings` when that returns `None` (`scripts/engine.py:103-105`). `review_findings` returns `None` unless a completed review segment has a usable report (`scripts/reconcile.py:100-112`). A workflow with no review stage never produces that segment, so an approved, ready request waits forever.

`engine_merge.execute` re-plans with the same inputs (`scripts/engine_merge.py:43-48`), so the wait also refuses the merge at execution time.

Confirmed on this tree, with findings omitted (`open_findings` has no key):

- `implement → merge`, `review_state=approved`, `merge_state=ready` → `Wait("review findings are unknown")`.
- The same item with `counts == {}` → `Merge`. That is why `test_workflow_without_review_keeps_its_forge_review_requirement` stays green: it passes `{}`.
- `review_state=none` → `Wait("review has not run")`.
- A workflow that includes `review`, with a done review segment and unknown counts → `Wait("review findings are unknown")`.
- `start=merge` and no edges, approved and ready, unknown counts → the same forever wait. `test_start_merge_without_edges_merges` hides it by passing `{}`.

## Goal

When the workflow has no `review` stage, an approved, ready request merges even though worker findings are unknown. Every later merge gate still applies.

```
R := "review" ∈ {workflow.start} ∪ {edge targets}
D := a done review covers the completed work
A := review_state == "approved"
H := review_state == "changes_requested"
C := counts is not None
K := C ∧ counts contain a blocking severity
M := the later merge gates pass (ready, architect, decisions, incoming edge, Zach gate)

1. H → Wait("changes requested on the forge")                 [checked before findings]
2. R ∧ ¬D → dispatch review                                   [the review stage still runs]
3. R ∧ D ∧ ¬C → Wait("review findings are unknown")           [a worker report can still arrive]
4. ¬R ∧ ¬A ∧ ¬H → Wait("review has not run")                 [forge approval stays required]
5. K → Wait("blocking review findings")                       [known counts still block]
6. ¬R ∧ A ∧ ¬C ∧ M → Merge                                    [unknown counts cannot arrive]
∴ an approved forge-only request merges; a workflow that has review still waits on its report
```

## Non-goals

- Inventing severity counts from forge approval or review threads. `review_findings` already records that forge approval is not a worker report of zero findings (`scripts/reconcile.py:107-108`).
- Changing `engine._planning_inputs` or `engine_merge.execute`. Both already pass unknown counts through; the hold is in `_merge`.
- The wait at `scripts/reconcile.py:467-469`. That branch runs only while the item's stage is `review`, which requires `review` to be in the workflow.
- Architecture docs. Nothing in `architecture/ARCHITECTURE.md` requires worker findings when the workflow omits `review`.

## Design

`stages` in `_merge` is already `{workflow.start} ∪ {edge targets}`. Keep that as the meaning of "the workflow has a review stage". A review name that is only an edge source is outside that set today, and the forge-approval branch uses the same set.

Replace the unconditional unknown-counts return with a return only when `"review" in stages`. Call `_blocking` only when `counts is not None`, because `_blocking` reads `counts.get` and a forge-only item now falls through with `counts is None`.

```python
    if counts is None and "review" in stages:
        return Wait(key, "review findings are unknown")
    review_stage = "review" if "review" in stages else "merge"
    resolved = _policy(policies, review_stage, room)
    if counts is not None and _blocking(counts, resolved):
        return Wait(key, "blocking review findings")
```

Known counts still block, including on a forge-only workflow. `review_findings` counts a historical review segment even when the current workflow omits `review`, and those findings stay a hold. Unknown counts are the case that cannot change.

Falling through reaches the architect check, the ready check, decision holds, and the incoming-edge or Zach gate. A forge-only item whose merge policy requires an architect consult waits on that consult once findings no longer hide it.

## Implementation steps (TDD)

1. **Red** in `evals/test_reconcile.py`, next to `test_workflow_without_review_keeps_its_forge_review_requirement`. Call `self.plan(...)` with the default `findings=None` so `open_findings` has no key.
   - `implement → merge`, stage `merge`, `review_state=approved`, `merge_state=ready`, history `[Segment("implement")]` → `Merge`.
   - `start=merge`, no edges, same approval and ready state, empty history → `Merge`.
   - `review_state=none` on the `implement → merge` workflow → `Wait("review has not run")`.
   - `review_state=changes_requested` → `Wait("changes requested on the forge")`.
   - `counts={"critical": 1}` on the forge-only workflow → `Wait("blocking review findings")`.
   - Standard workflow, done review segment, findings omitted → `Wait("review findings are unknown")`.
   - Forge-only workflow whose `merge` place sets `architect=True`, findings omitted, no architect segment → `Wait("architect consultation is required")`.
2. Run that test and confirm the two `Merge` assertions fail with `Wait("review findings are unknown")`.
3. **Green** in `scripts/reconcile.py`: apply the `_merge` change above. Leave `review_findings` and the stage-`review` branch at line 467 unchanged.
4. Re-run the new test and the preserved tests below.

## Tests to preserve

- `test_workflow_without_review_keeps_its_forge_review_requirement` — approved plus `{}` still merges; `none` still waits.
- `test_start_merge_without_edges_merges` — `{}` still merges.
- `test_merge_requires_ready_state_completed_review_and_known_findings` — a workflow that has `review` still waits on unknown counts.
- `test_unknown_or_stale_review_findings_do_not_merge` in `evals/test_engine.py`.
- `test_findings_missing_at_execution_are_not_assumed_clean` and `test_issue_type_without_engine_review_requires_current_forge_approval` in `evals/test_engine_merge.py`. The second refuses with `review has not run` because approval was withdrawn, which stays in front of the findings check.

## Risks

| Risk | Mitigation |
|------|------------|
| Skipping the unknown-counts wait also skips a later gate | The new architect assertion, plus the existing ready, decision, and incoming-edge tests, which run after this check. |
| A forge-only history that contains a done review with a broken report (`counts is None`) merges | The workflow cannot produce a repaired report. A usable report still blocks via `_blocking`. |
| Treating approval as `{}` would look like zero findings | Do not change `review_findings` or `_planning_inputs`. The comment at `reconcile.py:107-108` stays. |
| `merge` missing from `policies.stages` makes `_policy` raise once the early return is gone | Fixtures and shipped workflows that reach merge already define the `merge` place. `_policy` is the existing lookup used for the architect check. |

## Verification

```bash
python3 -m unittest evals.test_reconcile.ReconcileTest evals.test_engine_merge.ExecutorTest
```

The implementer runs the full `python3 -m unittest discover -s evals -p 'test_*.py'` before a done report. This workspace requires passed > 0 and failed, skipped, and xfailed all zero.

## File ownership (implementation stage)

| Path | Mode |
|------|------|
| `scripts/reconcile.py` | write |
| `evals/test_reconcile.py` | write |
| `scripts/engine.py` | read |
| `scripts/engine_merge.py` | read |
| `evals/test_engine.py` | read |
| `evals/test_engine_merge.py` | read |
