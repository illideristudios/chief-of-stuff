# Plan: Delete the compliance.md wording check from the retired-provider test (§11) (#1004)

Stage: plan. Implementation deletes three statements in `evals/test_cases.py` and edits nothing else. No architecture doc edit. No new dependency.

## What is wrong

`CaseLintTest.test_retired_deploy_provider_is_absent_outside_archives` does two jobs.

1. The repository scan (`evals/test_cases.py:173`) enforces §11. `retired_provider_files` walks tracked source, and the assertion allows only `architecture/compliance.md` among the hits. That stays.
2. The ledger read (`:174-176`) then opens `architecture/compliance.md` and fails when any line other than `| C20 |` contains `RETIRED_PROVIDER`. That grades the wording of a document the architect owns. The comment at `:172` says so. §11 does not govern that document: it covers code, agent rules, skills, and eval fixtures (`architecture/ARCHITECTURE.md:281`). A new compliance row that cites the closed gap by name turns CI red even though no code changed.

Today the second check is green only because the closed C20 row is the only ledger line that names the provider. The failure is latent.

```
S := the scan at :173 enforces §11
R := :174-176 asserts on compliance.md's wording
G := §11 governs compliance.md
V(x) := x adds value (tests the interface, stable, the owner checks its own output)

1. S → V(S)                 [ARCHITECTURE.md:281]
2. R ∧ ¬G → ¬V(R)           [test_cases.py:172; §11 lists code, rules, skills, fixtures only]
3. S ∧ R ∧ ¬G               [the method as written]
∴ keep :173, delete :174-176
```

The scan is the §11 test. The extra read of the architect's ledger is not, so a new compliance row must not fail this test.

## Change

In `test_retired_deploy_provider_is_absent_outside_archives`, delete these three statements and nothing around them:

```python
        ledger = (EVALS.parent / "architecture/compliance.md").read_text().splitlines()
        stray = [line[:60] for line in ledger if RETIRED_PROVIDER in line.lower() and not line.startswith("| C20 |")]
        self.assertEqual(stray, [])
```

Leave this assertion, and the comment on the line above it, exactly as they are:

```python
        # C20 names the open gap. Only the architect edits architecture/compliance.md.
        self.assertLessEqual(set(retired_provider_files(EVALS.parent)), {"architecture/compliance.md"})
```

Match the statements, not the line numbers. A shift that deletes the `assertLessEqual` instead of the ledger read removes the §11 check this issue keeps.

Leave `test_retired_provider_scan_reads_source_and_skips_generated_state` (`:178-189`), `retired_provider_files`, `SOURCE_DIRS`, and `RETIRED_PROVIDER` unchanged. `RETIRED_PROVIDER` stays split (`"rail" + "way"` at `:72`) so this file is not itself a scan hit.

Do not rewrite the comment. Its first sentence will sit above an assertion that no longer reads the ledger. That leftover is accepted: the filed change is the three statements, and a rewritten comment is how this scanned file would name the provider and fail the assertion that remains. The second sentence still says why `compliance.md` is the allowed hit.

## Tests

No new test and no edit to any other test. The issue is a deletion, and it forbids other test changes, so there is no new red case to write first.

The acceptance that the scan still fails is the assertion that stays. `retired_provider_files` returns every scanned path that names the provider. `assertLessEqual(..., {"architecture/compliance.md"})` fails when that set contains any other path. The scanner's own test already proves which paths the walk returns, including a planted name in source and the skips for archive, run output, and generated trees. It stays byte-for-byte.

After the deletion, run:

```
python3 -B -m unittest evals.test_cases
```

`evals.test_cases` must pass. Before this plan was added, that command was 73 passed, 0 failed, 0 skipped, 0 xfailed. Re-run it after the deletion; the count stays green and may grow only if new tests landed on the branch independently. Do not change `assertLessEqual` to `assertEqual`: an empty hit list is a pass, and the test does not require `compliance.md` to name the provider.

## Out of scope

- `architecture/ARCHITECTURE.md` and `architecture/compliance.md`. The architect owns both. Report a disagreement; do not edit them.
- The scanner's directory list and skip rules. `architecture/` stays scanned. This issue does not narrow the walk to §11's four categories, and it does not add `ARCHITECTURE.md` to the allowlist. A scanned file other than `compliance.md` that names the provider still fails.
- Any other test, fixture, or dependency.

## Risks

- Deleting the allowlist assertion, or "cleaning up" `retired_provider_files` while deleting the ledger read. The diff should be three removed lines inside one method.
- Writing the provider's name as contiguous bytes in the comment, in this plan, or anywhere under the scanned tree except `architecture/compliance.md`. `docs/` is scanned (`SOURCE_DIRS` includes `docs`; only `docs/archive` is skipped). This plan is under `docs/plans/` and must not be a hit.
- Treating a future red scan as a reason to widen the allowlist. A hit outside `compliance.md` is the check working.
- A new compliance row that names the provider will pass. That is the point of the deletion. C20's wording is no longer this test's job.

## Baseline

`python3 -B -m unittest evals.test_cases` on this worktree, with this plan file present: 73 passed, 0 failed, 0 skipped, 0 xfailed. The two methods this issue names were green before the plan file existed (2 passed). The suite run after the plan file was written is the one that counts: the plan is a scanned `docs/` file and did not become a hit.

## Proposed implementation file ownership

- write: `evals/test_cases.py`
- read: `architecture/ARCHITECTURE.md`
- read: `architecture/compliance.md`
- read: `docs/plans/issue-1004.md`
