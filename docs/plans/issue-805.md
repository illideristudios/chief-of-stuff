# Issue #805: read open closers before merged requests in `Forge.landings`

Implementation plan for `github:illideristudios/chief-of-stuff#805`. This plan stage changes no runtime behavior. Line numbers refer to `origin/develop` at `4417a8fe`. This worktree is branched from `50091f80` (`origin/main`) and does not contain `scripts/forge.py` or `evals/test_forge.py`; implementation must be cut from `origin/develop`.

The round-11 panel on PR #753 cited `scripts/forge.py:664` (`_merged_requests()`) and `scripts/forge.py:672` (`requests()`) at commit `e9b16e1c`. Later in PR #753 (`62f51a67`, `ce688513`), `Forge.landings` replaced `self.requests()` with `self._open_closers(pipeline.ref)` so landing checks do not read reviews, CI, or discussions, and folded the two loops into one comprehension (`scripts/forge.py:687-689` on `origin/develop`). The read order did not change: `self._merged_requests()` still runs before `self._open_closers(pipeline.ref)`.

```
O := the request is observed in _open_closers(pipeline.ref) at t₁
M := the request is observed in _merged_requests() at t₂
T := the request merges between the two reads
N := the request is in neither list (¬O ∧ ¬M)

1. □((t₁ < t₂ ∧ T) → (O ∧ M))     [open→merged is one-way: reading open at t₁ then merged at t₂ observes a mid-pass merge in open (and in merged once indexed)]
2. □((O ∨ M) → ¬N)                [present in at least one read means never dropped from both]
3. (t₁ < t₂ ∧ T) ⊢ ¬N             [MP: 1, 2]
∴ reading _open_closers before _merged_requests prevents a request that merges between the reads from disappearing in that pass
```

Because a pull or merge request transitions from open to merged and never back, reading open closers first and merged requests second guarantees that a request merging between the two calls is seen in at least the first read (and in both once the merge is indexed), never in neither.

## Cause

In `Forge.landings` (`scripts/forge.py:674-733`), lines `687-689` currently execute:

```python
        merged = self._merged_requests()
        newest = {}
        for row in [*merged, *self._open_closers(pipeline.ref)]:
```

1. `self._merged_requests()` runs first (`t₁`). A request `#22` that is still open at `t₁` is not in `merged`.
2. If `#22` merges before `self._open_closers(pipeline.ref)` runs (`t₂`), `#22` is no longer open at `t₂` and is not in `_open_closers(pipeline.ref)` either.
3. `#22` appears in neither list for that pass:
   - If an older merged request `#21` also closed the same issue and is already on `main`, `newest[key]` resolves to `#21` (`(21, old_head)`), and the second loop (`for row in merged:`) falsely certifies `#21`'s landing and records `old_head` in `heads[key]`.
   - If `#22` is the only request closing the issue, `newest` has no entry for `key` and `landings` reports `(None, None)` even if `pipeline.sha` already includes `#22`'s merge commit.

By contrast, `Forge.snapshots` (`scripts/forge.py:815-819`) already reads open requests (`self.requests()`) before merged requests (`self._merged_closes()`).

## Implementation

In `scripts/forge.py`, inside `Forge.landings` (`:687-689`), evaluate `self._open_closers(pipeline.ref)` before `self._merged_requests()`, while keeping the iteration order `[*merged, *opened]`:

```python
        opened = self._open_closers(pipeline.ref)
        merged = self._merged_requests()
        newest = {}
        for row in [*merged, *opened]:
```

Why `[*merged, *opened]` keeps its merge-before-open loop order:

- When request `#22` merges between `opened` (`t₁`) and `merged` (`t₂`) without changing its head SHA, both `merged` and `opened` carry the same `identity = (22, head_sha)`. `newest[key]` becomes `(22, head_sha)`, superseding any older merged request `#21`, and the second loop (`for row in merged:`) matches `newest.get(key) == identity` on `#22` and compares `#22`'s merge commit against `pipeline.sha`.
- If `_merged_requests()` has not yet indexed `#22`'s merge at `t₂`, `opened` still sets `newest[key] = (22, head_sha)`, preventing an older merged request `#21` from matching `newest.get(key) == identity` and leaving `found[key]` as `(None, None)` for that pass.
- Iterating `opened` after `merged` with `identity[0] >= newest[key][0]` preserves `test_a_newer_open_request_or_changed_head_cannot_use_an_old_landing` (`evals/test_forge.py:497-513`), where an open row with the same request number and a new head SHA must override a merged row with the old head SHA.

Make no other changes to `scripts/forge.py`.

## Tests

On `origin/develop`, in `evals/test_forge.py` on `LandingTest` (next to `test_a_newer_open_request_or_changed_head_cannot_use_an_old_landing` at `:497`), add `test_a_request_merging_between_the_two_reads_is_not_lost`:

1. Arrange an issue (`#3`, `key = adapter.key(3)`) with an older merged request `#21` (`headRefOid="old-head"`, `mergeCommit={"oid": "squashed"}`, closing `#3`) and a newer request `#22` (`headRefOid="new-head"`, `mergeCommit={"oid": "new-merge"}`, closing `#3`) that starts in the open list and moves from the open list into the merged list immediately after the first request-list read (`pr list` on GitHub; `merge_requests` on GitLab) returns.
2. Verify both cases across GitHub (`GH`) and GitLab (`GL`):
   - When `pipeline` is at `"squashed"` (before `"new-merge"` lands on `main`, with compare/merge-base returning `"squashed"`), `adapter.landings(pipeline, [key], heads=heads)` does not fall back to `#21`: it checks `#22` and returns `(False, False)` with `heads == {}`. (On unpatched `origin/develop`, `#22` is in neither read and `#21` falsely yields `(True, True)` with `heads == {key: "old-head"}`.)
   - When `pipeline` is at `"new-merge"` (so `#22`'s merge commit is on `main`), `adapter.landings(pipeline, [key], heads=heads)` observes `#22` in both reads and returns `(True, True)` with `heads == {key: "new-head"}`.
3. Run:
   - `python3 -m unittest evals.test_forge`
   - `python3 -m unittest discover -s evals -p 'test_*.py'`

## Risks

- **Branch base:** This plan worktree is at `50091f80` (`origin/main`), which predates `scripts/forge.py`. Cutting the implementation branch from `50091f80` instead of `origin/develop` will fail immediately.
- **Tie-breaker order in `newest`:** Reversing the loop list to `[*opened, *merged]` instead of keeping `[*merged, *opened]` would let `merged` overwrite `opened` when both have the same request number (`identity[0] >= newest[key][0]`), breaking `test_a_newer_open_request_or_changed_head_cannot_use_an_old_landing`.
- **Error order when both reads fail:** Because `_open_closers` now runs before `_merged_requests`, a transport failure affecting both endpoints raises `ForgeError("open closers: ...")` first rather than `ForgeError("merged requests: ...")`. Existing tests fail one endpoint at a time and do not depend on `merged requests` failing before `open closers`.

## Proposed file ownership

- write: `evals/test_forge.py`
- write: `scripts/forge.py`
- read: `docs/plans/issue-805.md`
