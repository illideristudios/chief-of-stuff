# Plan: #928, trio votes from Cursor and agy fail, so almost no decision is unanimous

## Finding: where the code is, and what already landed

`scripts/trio_gate.py` and `scripts/host_adapter.py` do not exist on this branch's base (`main`, 0.128.x, `50091f80`). They live on `origin/develop` (`4417a8fe`, same as the #927 plan found). The implement stage must start from `origin/develop`, or every path below is missing. Line numbers below are `origin/develop`.

The issue's numbers are from 0.129.98. PRs #947 and #948 (0.129.99 and later) already shipped most of what §6 of the architecture asks for, so do not rebuild it:

- `trio_gate.TIMEOUT_S = 300` (was 60) and the 480 s pass budget (`trio_gate.py:14`, `_ask`'s `remaining`).
- `trio_gate._ask` replaces a member once on any non-quota failure by drawing another distinct entry (`trio_gate._ask`, `trio_gate.py:119`). A second failure is kept as the member's vote.
- `retryable_failure` makes a row whose failed vote is `quota` or `unknown` a pending outage (`decided_by=""`, retried at `retry_at`, capped at `MAX_TRIO_RETRIES = 8`), not a split.

## Why #928 still fails: one gap

`decision_escalation.retryable_failure` (`decision_escalation.py:16-24`) treats a failed vote as an outage only for `model_port.RETRYABLE = ("quota", "unknown")`. `host_adapter.judge` returns `Failure("error", ...)` for the two cases §6 names as an outage, "an answer with no structure":

- `host_adapter.py:244` `"{runtime} gave no readable answer: {exc}"` (the host's output did not parse).
- `host_adapter.py:246` `"{runtime} gave no structured answer"` (agy: `status != SUCCESS` or no `structured_output`; Cursor: `is_error` or no JSON object in `result`).

So when the replacement also fails with one of these, `consult` records `decided_by="zach"`, the pass never retries, and the decision waits on the owner. That is the issue's evidence: drago decision 82, agy `model failure (error): agy gave no structured answer`, `decided_by zach`, `unanimous false`. Timeouts (`host_adapter.py:235`) are `unknown`, so they already retry; `test_a_replacement_that_also_fails_keeps_the_failed_vote` covers only that kind.

`judge` is the trio's port for Codex, Cursor and agy only (`engine._judge`), so its failure kinds can change without touching workers. `RETRYABLE` itself must not widen: `worker.py`, `reconcile.py` and `completion.py` use it to decide worker retries.

## Change

`scripts/host_adapter.py`, `judge`

- The two `Failure("error", ...)` returns become `Failure("unknown", ...)`. A host that exits cleanly with output we cannot read is an outage the trio retries, not a model verdict. `missing_program` (no CLI, no read-only mode) stays `missing_program`; `classify` is unchanged (quota stays `quota`, a nonzero exit stays `unknown`).
- The no-structure message gains the last 300 characters of stdout, as `classify` already does for an unexplained exit: `f"{runtime} gave no structured answer: {stdout tail}"`. The failure text is stored in the verdict, so the next occurrence shows what agy actually returned. Nothing says today why agy returns no `structured_output` in 9 of 19 calls; the diagnosis comes from that tail.

`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`: patch version bump, as `5b596d3e` did for the first #928 release. The release install is what puts the fix in front of the live engine.

No change to `trio_gate.py`, `decision_escalation.py` or `model_port.py`. Nothing here conflicts with the #927 plan, which edits `trio_gate.py`, `engine.py` and their tests.

## Tests (write first, see them fail)

`evals/test_host_adapter.py`, `JudgeTest`:

- Rewrite `test_a_quota_line_is_a_quota_stop_and_no_answer_is_an_error`: a Cursor `is_error` envelope, an agy `{"status": "FAILED"}`, an agy `SUCCESS` with no `structured_output`, and unparseable stdout are all `kind == "unknown"` and `.retryable`; the message names the runtime and carries the stdout tail. Quota stays `quota`.

`evals/test_trio_gate.py`, `TrioModelsTest`, next to `test_a_replacement_that_also_fails_keeps_the_failed_vote`:

- The same replacement test run with the host's no-structure failure (`Failure("unknown", "agy gave no structured answer: ...")` on `cursor` and `agy`): `escalated`, four requests, `decided_by == ""`, `decision_escalation.retryable_failure(row.record)`. This pins the end state the issue wants, a retry and not an owner row, against the exact text.

Run `python3 -m unittest discover -s evals -p 'test_*.py'`.

## Verify after the release

Re-run the issue's repro query with `created_at` after the 0.129.99 install and the one for this release, on both stores, and also count `reasoning like 'malformed verdict%'`. Expected: Cursor timeouts far below 21 of 27, no `model failure (error)` rows from cursor or agy, and no `decided_by zach` rows whose newest verdict is a host failure. Anything left in the stdout tails is the agy diagnosis.

## Decision record

```
Glossary
F := a trio member's call failed and its replacement also failed
E := that failure is Failure kind "error"       U := that failure is Failure kind "unknown"
R := retryable_failure(row.record)               Z := the row is decided_by zach (waits on the owner)
O := the failure is "an answer with no structure", per §6 an outage
H := the failure comes from host_adapter.judge

Premises
1. R ↔ the failed vote's kind is in RETRYABLE = (quota, unknown)   [decision_escalation.py:16-24, model_port.py:46]
2. ¬R → Z; R → the row is retried at retry_at, up to 8 times       [trio_gate.consult, engine._choice]
3. host_adapter.judge returns kind "error" for no readable / no structured answer   [host_adapter.py:244,246]
4. F ∧ E → ¬R → Z                                                  [1, 2, 3]
5. O → the owner should not be asked                                [ARCHITECTURE.md §6, Outage]
6. RETRYABLE is also read by worker, reconcile and completion; widening it changes worker retries   [worker.py:527, reconcile.py:257, completion.py:73]
7. judge serves only the trio's Codex, Cursor and agy members       [engine._judge]

Derivation
8.  3 ∧ 4 → every no-structure failure on a host reaches the owner      [observed: drago decision 82]
9.  5 ∧ 8 → the current behaviour breaks §6
10. H ∧ O → reclassify to U inside host_adapter.judge                  [6, 7: widening RETRYABLE would leak to workers]
11. U → R                                                              [1]
12. F ∧ U → R → retried, not Z                                         [2, 11]
∴ judge returns "unknown" for an answer with no structure, so an outage is retried and the owner is asked only after 8 failed retries
```

## Risks

- **Base branch.** Implement from `origin/develop`, not this worktree's `main`.
- **Cursor `is_error` is now an outage.** A Cursor reply with `is_error: true` shares the no-structure path. A permanent one (a model name that does not exist) is retried until the cap of 8 (about 3 hours of backoff) before the owner sees it, instead of at once. The replace-once step moves the member to a different entry first, so a bad entry rarely reaches the cap.
- **Root cause of agy's missing `structured_output` is not established.** This plan stops it reaching the owner and records the evidence; it does not make agy answer. If the tails show the JSON sitting in a text field, the fix is to reuse `_structured`'s Cursor text scan for agy. That is a follow-up on evidence, not built on a guess.
- **Cost per retry.** Each retry re-asks all three members, including those that already voted. Carrying good votes across retries would cut calls, but §6 does not ask for it and it changes the record shape. Not planned.
- **A failed vote is still stored as `vote: "concern", choice: null`.** Unanimity is computed from adopt votes, so it is never counted as a real concern, and `retryable_failure` reads the `model failure (...)` prefix. Boards and the crawl that read the stored vote still see "concern". Not changed here.
- **`malformed verdict` is not an outage.** A reply whose data fails the schema (a choice outside the options) is still a settled concern. The issue's counts only matched `model failure%`, so the re-run above also counts malformed verdicts to show whether they matter.
- **Worst-case latency.** A 300 s timeout twice per member, three members, is up to 30 minutes of serial calls per consult, which the 480 s pass budget (#927) bounds per pass. Cursor is the slow one; the pass budget, not this change, is the limit.
- **Architecture documents.** §6 already describes this behaviour (commits `282b805a`, `c708fcbb`). The architecture agent owns `ARCHITECTURE.md` and `compliance.md`; nothing here edits them.

## Proposed file ownership

```
file_ownership:
- write scripts/host_adapter.py
- write evals/test_host_adapter.py
- write evals/test_trio_gate.py
- write .claude-plugin/plugin.json
- write .claude-plugin/marketplace.json
- read  scripts/trio_gate.py
- read  scripts/decision_escalation.py
- read  scripts/model_port.py
- read  scripts/engine.py
- read  architecture/ARCHITECTURE.md
```
