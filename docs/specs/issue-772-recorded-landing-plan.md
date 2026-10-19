# Issue #772: stop rechecking a recorded landing

Implementation plan for `github:illideristudios/chief-of-stuff#772`. The plan stage changes no runtime behavior. Line numbers are from `origin/develop` at `8e566122`. This worktree is `50091f80` and does not contain `scripts/engine_facts.py` or `scripts/forge.py`. Implementation is cut from develop.

Frank Lloyd AIght filed this from #753's review. #773 stated the same cost and is closed. Develop already skips `forge.landings` when resume holds `verified_landing is True` (`scripts/engine_facts.py:231-257`, `scripts/engine.py:67-83`). That flag does not name the merge it proved, so the next time the issue is closed and merged the gather still reports the old landing and the old green CI. Open pull request #824 binds the memo to a commit and conflicts with develop; it also drops `verified_landing_head`. Leave that branch. Implement from current develop.

```
R := the memo equals this pass's main merge commit
O := the item is not closed-and-merged
W := the item is closed and merged, and the snapshot names a commit the memo does not equal
L := the memo is the legacy flag True and the snapshot names no commit
F := forge.landings includes this item
C := a stale memo is cleared before gather
S := the store keeps the memo

1. □(R → ¬F ∧ S)     [recorded completion leaves the per-pass recheck; the record stays]
2. □(O → C ∧ ¬F)     [open or unmerged: drop the memo; landings is only for closed merges]
3. □(W → C ∧ F)      [a different commit is cleared, then the forge proves this merge]
4. □(L → ¬F ∧ S)     [a flag outside the merged window stays recorded until a commit is named]
∴ a matching commit memo is not re-read; reopen and a new merge commit go back to the forge
```

A recorded completion for this merge commit is not asked of the forge again, and the store keeps it. An open item, or a closed merge whose commit is not the one on the memo, is still read from the forge.

## Cause

`gather` selects every store item with `forge_state == "closed"` and `merge_state == "merged"` (`scripts/engine_facts.py:231-232`). A resume dict whose `verified_landing` is exactly `True` is remembered: `on_main` and `suite_on_main` are appended as `True`, and the key is left out of `forge.landings` (`:237-257`). Anything else in that set is pending. `landings` (`scripts/forge.py:674`) then lists the bounded merged-request window (`:632-655`) and compares that pending key's newest main merge into current main.

`_remember_verified` (`scripts/engine.py:67-83`) writes the flag after a pass whose facts say on main and suite green. It also stores `verified_landing_head`, the source head `landings` returns, which `gather` copies back into `landing_heads` so `landed_trees` can recognize the recorded worktree (`scripts/engine_facts.py:239-240`, `scripts/reconcile.py:183-184`). `evals/test_engine.py:255` locks the skip. `evals/test_engine.py:569` locks the source head. Nothing clears the flag, and nothing compares it to a commit.

`snapshots` already reads that same merged window to set `merge_state` (`scripts/forge.py:801-841`) and does not carry the merge commit. `store.sync_from_forge` writes only `FORGE_COLUMNS` (`scripts/store.py:27`, `:319-322`), so a commit on the snapshot stays in the pass's memory and does not need a column. `engine_facts` only reads (`scripts/engine_facts.py:1-6`).

## Change

Leave `landings`' compare, its open-closer rule, and the `REQUEST_LIMIT` window as they are. An open follow-up still does not undo `merge_state` (`scripts/forge.py:836`). It also does not erase a memo whose commit is still the newest main merge. The next main merge commit does.

1. In `scripts/forge.py`, add one helper for the commit `landings` already proves (`:706-707`): GitHub `mergeCommit.oid`; GitLab `squash_commit_sha`, else `merge_commit_sha`, else `sha`; otherwise none. Use it from `landings` and from the snapshot index.
2. Replace `_merged_closes` with an index over the list `_merged_requests` already returned: the set of closed issue numbers, and each issue's newest main merge commit (highest request number, base `main`, commit from the helper). A merge onto another base stays in the closed set and has no commit. `snapshots` sets `landing_sha` on a closed merged row when that commit exists. No new forge call and no store column.
3. In `scripts/engine.py`, after `sync_from_forge` and before `gather` (`:631-657`), drop `verified_landing` and `verified_landing_head` together when the item is not closed-and-merged, or when this pass's snapshot names a `landing_sha` and the memo is not that string. A legacy `True` differs from any commit, so the first pass that can name the commit rechecks once and then stores the commit. A snapshot that names no commit leaves the memo in place, including an item that has fallen out of the merged window. Leave every other resume key.
4. In `scripts/engine_facts.py`, treat a memo of `True` or a non-empty string as remembered, and keep copying `verified_landing_head` into `landing_heads`. Pending keys are unchanged: no memo, or a memo this pass already dropped.
5. In `_remember_verified`, leave a memo that is already `True` or a non-empty string. When this pass proved on main and green CI and the memo is absent, store the snapshot's `landing_sha` when it is a non-empty string, otherwise `True`. Keep writing `verified_landing_head` from `facts.landing_heads` when that head is present.

`gather` still does not call `landings` when nothing is pending. Items still awaiting a proof stay pending, and the forge remains the read for them.

## Tests

On develop.

`evals/test_engine_facts.py`, on `GatherTest`:

- A closed merged item whose memo is the string `squashed`, with `verified_landing_head` set, does not call `landings`. Facts report that key on main, suite green, and `landing_heads` equal to that source head. A second closed merged item with no memo is the only key passed to `landings`.
- A legacy memo of `True` is remembered the same way when the resume still holds it.
- An open item whose resume still holds a memo is not passed to `landings` and is not forced on main by that memo.

`evals/test_forge.py`, next to `test_snapshots_include_closed_issues_and_merged_requests` (`:700`):

- Two main merges that close one issue, plus a merge onto `release`: the closed snapshot's `landing_sha` is the higher-numbered main merge's `mergeCommit.oid`.
- A GitLab row uses `squash_commit_sha`, then `merge_commit_sha`, then `sha`, the same order as `test_gitlab_uses_the_squash_sha_and_merge_base` (`:371`).

`evals/test_engine.py` (not on the issue's write list; the pass tests fail if the clear and the stored commit stay untested):

- Reopen clears `verified_landing` and `verified_landing_head` and keeps every other resume key.
- A snapshot `landing_sha` of `new-sha` over a memo of `True` or of `old-sha` calls `landings` with that key. A red result does not write a new memo, and the key is not completed.
- A proved landing is stored as that `landing_sha`. `verified_landing_head` stays the source head. The next pass does not call `landings`, and the key still completes.
- `test_a_verified_landing_is_not_reread_on_later_gathers` and `test_squash_landed_final_stage_clean_tree_completes_without_a_hold` stay green. The first still uses a boolean memo with no `landing_sha`. The second still expects `verified_landing_head`.

## Out of scope

- #912's GraphQL quota, backoff, and the closed issues that stay "on main is unknown" because they never received a memo. Those keys stay pending and keep calling `landings`.
- Paging merged history past `REQUEST_LIMIT`. The shortcut at `scripts/forge.py:653-655` stays.
- A store column, a schema change, or a write from `engine_facts`.
- Clearing a memo because a newer open request also closes the issue. The memo's commit is still the newest main merge until a new main merge commit replaces it.
- The architecture document. The architect owns it.

## Ownership to widen before implement

The issue's write list is `scripts/engine_facts.py`, `scripts/forge.py`, `evals/test_engine_facts.py`, and `evals/test_forge.py`. The memo is written in `scripts/engine.py`, and the clear has to run there before `gather`, beside `_remember_verified`. The implement stage needs write access to:

- `scripts/engine.py`
- `evals/test_engine.py`

This plan document is the plan stage's record, the same place as the #768 and #757 plans.

## Risks

- The first pass after deploy rechecks every in-window item whose memo is still `True`, once, because `True` is not the commit string. Items outside the window keep `True` and are not rechecked. A later merge updates the request, brings it back into the window, and the new commit clears the flag.
- Dropping `verified_landing_head` in the same write as the memo is required. A later proof with no source head would otherwise keep certifying `landed_trees` against the old head.
- `landing_sha` has to be the commit `landings` compares. A second spelling clears a good memo every pass and puts the key back on the forge read.
- `snapshots` and `landings` both already call `_merged_requests` when any key is pending. This change adds no third read. A pass with every closed merge remembered does not call `landings`.
- Branches cut from this plan worktree do not contain the engine. Implement from `origin/develop`.

## Acceptance

- A closed merged item whose resume memo is the snapshot's current main merge commit is absent from `forge.landings` on later passes, and the resume still holds that commit and `verified_landing_head`.
- An item that is open, or closed and merged under a different main merge commit, is included in that pass's `landings` keys. The forge result is what the facts report.
- A legacy `True` is still honored when the snapshot names no commit, and is replaced by the commit after the one recheck that names it.
- On develop: `python3 -m unittest evals.test_engine_facts evals.test_forge evals.test_engine` passes.
