# Plan: Issue #949 — Excuse unrelated pull-request merge commits in `moved_outside_engine` (§13)

Stage: `plan`. Only `docs/plans/issue-949.md` is written in this stage. Line references are against `HEAD` (`dc388cb5`).

## Finding

When a non-Claude worker (`codex`, `cursor`, `agy`) finishes, `worker.collect` (`scripts/worker.py:554-558`) calls `host_adapter.moved_outside_engine(store, record, now.isoformat())` (`scripts/host_adapter.py:273-300`).

For each protected branch recorded in `record["branch_heads"]` at launch, `moved_outside_engine` fetches `refs/heads/<branch>` from `origin`, lists `rev-list --first-parent <before>..<after>` (`scripts/host_adapter.py:290`), and holds the run for human review whenever any commit in that first-parent range is not in `engine_merges(store)` (`scripts/host_adapter.py:294-297`).

`engine_merges(store)` (`scripts/host_adapter.py:262-270`) only contains merges executed by `engine_merge.execute` (`scripts/engine_merge.py:56-60`). Whenever a person or interactive session merges any pull request into a protected branch while a non-Claude worker is running, that merge commit is absent from `engine_merges(store)`, so every concurrent non-Claude worker is held for human review when it exits. On the `chief-of-stuff` store, all 38 commits named across 31 such holds are ordinary GitHub pull-request merge commits (`Merge pull request #<N> from <owner>/<branch>`) for unrelated branches.

Meanwhile, `worker.launch` already records the worker's own branch in `record["branch"]` (`scripts/worker.py:413`: `work/issue-<N>-<stage>-<attempt>`, or the existing work/PR branch on review/fix/retry at `:304, :316, :327`).

## Logic

```
P_branches := protected branches recorded in record["branch_heads"] at launch
V(b)       := branch b is fetched from origin, before(b) and after(b) are readable, and rev-list --first-parent before(b)..after(b) succeeds
Rm(c)      := commit c is in engine_merges(store)
M2(c)      := commit c has at least two parents in Git (len(parents(c)) >= 2)
Pr(c, h)   := subject(c) matches /^Merge pull request #\d+ from [^/\s]+\/(\S+)$/ with head branch h
B_own      := record.get("branch") is a non-empty string
P(c)       := M2(c) ∧ B_own ∧ ∃h: Pr(c, h) ∧ h != record["branch"]
W(c)       := the run's worker may have made commit c outside the engine
Ok         := moved_outside_engine(store, record, now) == ""

1. Rm(c) → ¬W(c)                                                             [the engine merged c itself]
2. P(c) → ¬W(c)                                                              [a PR merge of another branch is not this worker's doing]
3. ¬Rm(c) ∧ ¬P(c) → W(c)                                                     [ledger F3: direct push, local merge, own-branch PR merge, or unknown branch holds]
4. Ok ↔ (P_branches ≠ ∅ ∧ ∀b ∈ P_branches: V(b) ∧ ∀c ∈ first_parent(before(b)..after(b)): Rm(c) ∨ P(c))
∴ a non-Claude run holds iff a protected branch is unreadable or gained a first-parent commit that is neither an engine-recorded merge nor a pull-request merge of another branch
```

## Change

All production changes are in `scripts/host_adapter.py`:

1. **Match pull-request merge commits whose head is not the run's branch (`scripts/host_adapter.py`)**:
   - Import `re` and define:
     ```python
     PR_MERGE = re.compile(r"^Merge pull request #\d+ from [^/\s]+/(\S+)$")
     ```
   - Add a helper `_unrelated_pr_merge(parents: list[str], subject: str, own_branch: str) -> bool`:
     ```python
     def _unrelated_pr_merge(parents: list[str], subject: str, own_branch: str) -> bool:
         """Whether a commit is a pull-request merge whose head is not the run's own branch (#949)."""
         match = PR_MERGE.match(subject) if len(parents) >= 2 and own_branch else None
         return match is not None and match.group(1) != own_branch
     ```
     Requiring `len(parents) >= 2` ensures a single-parent direct push with a spoofed `"Merge pull request ..."` commit message still holds. Requiring a non-empty `own_branch` preserves ledger F3 (unknown is never safe) if `record` has no `"branch"`.

2. **Read parents and subject in `moved_outside_engine`'s existing `rev-list` call (`scripts/host_adapter.py:273-300`)**:
   - Read `own_branch = record.get("branch") if isinstance(record.get("branch"), str) else ""`.
   - Pass `"--no-commit-header"` and `"--format=%H %P\t%s"` to the existing `rev-list` call, keeping `f"{before}..{after}"` at `args[2]`:
     ```python
     code, out = make_worktree.git_trusted(
         ["rev-list", "--first-parent", f"{before}..{after}", "--no-commit-header", "--format=%H %P\t%s"], tree)
     ```
     Each line in real Git output is `<commit> <parent1>[ <parent2> ...]\t<subject>`. Lines from existing unit-test mocks that return bare SHAs (`<commit>`) parse as `commit = <commit>, parents = [], subject = ""`, so every existing test in `evals/test_host_adapter.py` continues to work without extra subprocess calls.
   - Parse each non-empty line of `out.splitlines()`:
     ```python
     unknown = []
     for line in out.splitlines():
         shas, _, subject = line.partition("\t")
         if not shas.strip():
             continue
         commit, *parents = shas.split()
         if commit not in recorded and not _unrelated_pr_merge(parents, subject, own_branch):
             unknown.append(commit)
     ```
   - When `unknown` is non-empty, append the existing message `f"{branch} gained commits the engine did not merge: {', '.join(c[:12] for c in unknown)}"`.

No changes are needed in `scripts/worker.py` (`record["branch"]` is already populated for all stages at `scripts/worker.py:413`).

## Tests (TDD: red first)

In `evals/test_host_adapter.py`:

1. Update `HostAdapterTest.check` (`:180-196`) to accept `branch: str = "work/issue-1-plan-1"` and include `"branch": branch` in `record`.
2. Add failing tests before changing `scripts/host_adapter.py`:
   - `test_a_pr_merge_of_another_branch_does_not_hold_the_run`: `self.check` with a 2-parent merge line `f"{'p' * 40} {'a' * 40} {'b' * 40}\tMerge pull request #946 from illideristudios/docs/decision-pages-945"` and `branch="work/issue-949-plan-1"` returns `""`. Mix it with an engine-recorded merge and verify it still returns `""`.
   - `test_a_pr_merge_of_the_runs_own_branch_holds_and_is_named`: `self.check` with `f"{'w' * 40} {'a' * 40} {'b' * 40}\tMerge pull request #949 from illideristudios/work/issue-949-plan-1"` and `branch="work/issue-949-plan-1"` holds for human review and names `"w" * 12`.
   - `test_a_direct_push_or_local_merge_holds_even_with_a_pr_subject`:
     - single-parent `f"{'d' * 40} {'a' * 40}\tMerge pull request #946 from illideristudios/other"` holds (`len(parents) < 2`);
     - 2-parent local merge `f"{'l' * 40} {'a' * 40} {'b' * 40}\tMerge branch 'other' into main"` holds;
     - 2-parent PR merge when `branch=""` holds (missing own branch fails closed).
   - `test_moved_outside_engine_with_a_real_git_remote_excuses_unrelated_pr_merges`: create a real bare `origin` and worker clone via `self.git_tree()`, advance `origin`'s `develop` with a 2-parent commit titled `Merge pull request #946 from illideristudios/docs/decision-pages-945`, verify `moved_outside_engine` returns `""`; then advance `origin`'s `develop` with a 2-parent commit titled `Merge pull request #949 from illideristudios/work/issue-949-plan-1`, verify `moved_outside_engine` holds and names only the second merge commit.

Run with `"$CHIEF_PYTHON" -m unittest evals/test_host_adapter.py evals/test_worker.py`.

## Risks

- **Architecture documentation (`architecture/ARCHITECTURE.md` §13)**: `:320` and `:332` currently state `Ok ↔ ∀b ∈ P, ∀c ∈ first-parent(start(b)..end(b)): Rm(c)`. Per `CLAUDE.md`, only the architecture agent (`frank-lloyd-aight`) edits `architecture/ARCHITECTURE.md` and `architecture/compliance.md`; the implementation leaves `architecture/` untouched.
- **Squash or rebase merges made outside the engine**: A manual squash or rebase merge on GitHub lands as a single-parent commit without a `Merge pull request #<N> from <owner>/<branch>` subject and cannot be distinguished from a direct push using git history alone. It will still hold for human review (safe fail-closed behavior). `chief-of-stuff` uses merge commits for PRs, matching all 38 observed commits.
- **Custom merge commit subject when merging manually**: If a person manually edits the default GitHub merge commit title (`Merge pull request #N from owner/branch`) before clicking merge, the subject will not match `PR_MERGE` and will fail closed by holding for human review.
- **Already-held runs in `store.db`**: Existing `human_review` flags in `store.db` are not retroactively cleared by `moved_outside_engine`; this change prevents false-positive holds on future collects.

## File ownership

- write: `scripts/host_adapter.py`
- write: `evals/test_host_adapter.py`
- read: `scripts/worker.py`
- read: `scripts/engine_merge.py`
- read: `scripts/make_worktree.py`
- read: `evals/test_worker.py`
- read: `architecture/ARCHITECTURE.md`
- read: `docs/plans/issue-949.md`
