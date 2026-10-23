# Plan: Trio scope votes see issue requirements and the plan (#1006)

Stage: plan. Implementation wires richer context into scope consults and adds tests. No architecture doc edit (report to the architect if §8 should name the new helper or budget).

## What is wrong

`engine._settle_scopes` (`scripts/engine.py:311-355`) builds only:

```python
listed = "; ".join(f"{mode}: {path}" for path, mode in scopes)
```

and passes `context=listed` into `_choice` → `trio_gate.Decision`. `trio_gate.consult` embeds the full decision (including `context`) in each voter's prompt (`scripts/trio_gate.py:58-63`). Voters therefore see the proposed paths but not the issue title, issue body, or the plan file at `docs/plans/issue-<n>.md` that justified the proposal.

Store evidence (2026-10-22): many scope verdicts are `concern` with reasoning that requirements are missing (#927, #895, #909), which splits votes to the owner.

```
L := listed file paths only in context
R := voters can verify the write set against requirements
1. L → ¬R                    [observed split reasons]
2. acceptance requires R
∴ replace L with bounded issue + plan + paths
```

## Design

### 1. Pure builder in `scripts/ownership.py`

Add a small, testable API beside `plan_path` / `proposal_scopes`:

- **`SCOPE_CONTEXT_CAP`** (bytes, UTF-8): upper bound on `Decision.context` for topic `scope`. Pick a single constant here (suggested starting point: **12_000** bytes) so unit tests pin truncation without calling models. The cap is the trio **prompt** budget for this field only; persona rules and the rest of the decision JSON still ride in `trio_gate._request`, so keep the constant conservative and document that it may be tuned after measuring real prompts.

- **`scope_consult_context(*, title: str, body: str, plan_text: str, scopes: list[tuple[str, str]]) -> str`**
  - **Always include** the proposal paths first, in stable order (same as today: sort by path, then `write: path` / `read: path` lines or the existing `listed` join — keep one canonical form and use it everywhere).
  - **Then** labeled sections the voters can scan:
    - `Issue title: …`
    - `Issue body:\n…` (full forge body; do not strip `## File ownership` unless a later issue asks — acceptance says "issue body")
    - `Plan (docs/plans/issue-<n>.md):\n…`
  - **Bound** the whole string to `SCOPE_CONTEXT_CAP` with a deterministic shrink order:
    1. Never drop or truncate the paths block.
    2. Truncate `plan_text` from the end with a fixed suffix, e.g. `\n… [plan truncated]`.
    3. Truncate `body` from the end with `\n… [issue body truncated]`.
    4. Truncate `title` only as a last resort.
  - **Empty inputs**: missing plan file → `Plan: (not found in worktree)` or similar fixed phrase; empty title/body → explicit `(empty)` so context is still stable for the same inputs.

This keeps `trio_gate._settled` equality (`prior.get("context") != decision.context`, `scripts/trio_gate.py:202-204`) and `_choice`'s `retry_key` (`engine.py:186`) aligned: the same proposal, issue snapshot, and plan bytes produce the same context; any change re-consults.

### 2. `engine._settle_scopes` gathers inputs and calls the builder

Extend `_settle_scopes` signature minimally so it can read the plan from disk:

- Pass **`root: Path`** from `run_pass` (same workspace root worker launch uses).
- For each pending `scope_proposal`:
  - **Title**: `store.item(key).title` (forge-synced).
  - **Body**: prefer the snapshot body already on the forge row if `run_pass` still has it in scope; otherwise `forge.issue(number)` as on the approve path (`engine.py:339-345`). Do not consult without a readable body when the forge read fails — log and `continue` (same spirit as ownership read failures elsewhere).
  - **Plan text**: resolve the proposing segment's worktree from `store.ownership(key)` rows with `mode == "tree"` (absolute path written at launch, `scripts/worker.py:310-331`). Read `(tree / ownership.plan_path(key))` as UTF-8 when the file exists; otherwise `""` and let the builder emit the not-found line.
  - **Scopes**: from `proposal["scopes"]` as today.
  - Replace `context=listed` with `context=ownership.scope_consult_context(...)`.
  - Keep the **question** string as today (`Approve this file ownership for {key}? {listed}`) so board copy and existing tests that grep the question stay stable; only `context` grows.

No change to `scope_proposal` resume shape, `engine_mcp.report`, or approve/reject write paths.

### 3. Settled-decision reuse (acceptance box 3)

No change to `trio_gate._settled` logic. Verify with tests that:

- Two `trio_gate.decide` calls with the same `Decision` (including the new long `context`) reuse the first row (`len(model.requests)` unchanged on the second call — mirror `test_decide_reuses_matching_decision_…` in `evals/test_trio_gate.py:361-367`).
- Changing only the plan body or issue body changes `context` and forces a new consult (`len(model.requests)` increases).

## Tests (TDD order)

1. **`evals/test_ownership.py`** (new class or methods)
   - Paths block always present; title, body, and plan substrings appear when under the cap.
   - Truncation: oversized body/plan shrinks but paths remain intact; suffix markers present.
   - Determinism: same inputs → same string twice.

2. **`evals/test_engine.py`** (extend `proposal` / `_trio` helpers)
   - Set forge body to a distinctive string (e.g. `Acceptance: must carry context`).
   - Create a temp worktree path under `self.root`, register `store.set_ownership(KEY, str(tree), "tree")`, write `docs/plans/issue-1.md` with distinctive plan text.
   - Run `run_pass` with `_votes`; capture the first `model_port.Request` (pattern from `test_a_human_answer_during_a_trio_consult_wins_…`).
   - Assert `json.loads` segment of the prompt or inspect stored decision row: `record["decision"]["context"]` contains both distinctive issue and plan strings (acceptance box 2).
   - Add a reuse case: pre-seed a settled `scope` row with the exact `context` the builder would produce; second pass does not call the model again.

3. **Regression**: existing scope approval/reject/split tests (`test_an_approved_scope_proposal_is_written_…`, etc.) stay green without changing expectations on the question text.

Run:

```bash
python3 -B -m unittest evals.test_ownership evals.test_trio_gate evals.test_engine
```

## Risks

- **Oversized context** blows model limits or slows trio votes — mitigated by `SCOPE_CONTEXT_CAP` and paths-first truncation.
- **Stale context**: body read at consult time differs from approval-time `forge.set_body` — acceptable; voters judge the proposal against requirements at consult time, matching stored `decision.context` for reuse.
- **Missing worktree row** after plan ends — plan section empty; voters may still concern, but issue body should address most missing-requirement splits.
- **#1005** (split reruns plan with dissent): a replan changes plan text → new `context` → new consult; intentional.
- **#928** (failed votes are concerns): unchanged; this issue only enriches `context`.

## Out of scope

- Board/decision page rendering of context (e.g. #939) — may already expect rich `context`; no requirement to change `render_board.py` here.
- Changing scope **question** wording or trio options.
- Architecture doc updates — architect owns §8 if the budget or section labels become normative.

## Proposed implementation file ownership

- write: `scripts/ownership.py`
- write: `scripts/engine.py`
- write: `evals/test_ownership.py`
- write: `evals/test_engine.py`
- read: `scripts/trio_gate.py`
- read: `scripts/store.py`
- read: `scripts/worker.py`
- read: `docs/plans/issue-1006.md`
