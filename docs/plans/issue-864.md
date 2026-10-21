# Issue 864 — merge with a method the repository allows

`merge_approved.main` merges a ready GitHub pull request with a hardcoded `--squash` (`scripts/merge_approved.py`, the `gh pr merge` call). There is no `merge_head`. This repository's settings, read 2026-10-21, are `allow_merge_commit: true`, `allow_squash_merge: false`, `allow_rebase_merge: true`, so that call is refused and the merge never lands.

GitHub's method is the first allowed of merge, squash, rebase. None allowed, or settings that cannot be read, refuses. GitLab stays a PUT of `{"sha": head}` and no strategy.

```
M := allow_merge_commit
S := allow_squash_merge
R := allow_rebase_merge
U := the three booleans were read

1. □(merge → U ∧ readable)          [no merge call unless the settings read succeeded]
2. U ∧ M → --merge                  [first in GitHub's order]
3. U ∧ ¬M ∧ S → --squash
4. U ∧ ¬M ∧ ¬S ∧ R → --rebase
5. U ∧ ¬M ∧ ¬S ∧ ¬R → refuse        [allows none]
6. ¬U → refuse                      [unreadable: failed call, bad JSON, missing key, non-bool]
7. GitLab → ¬strategy               [PUT body stays sha only]
∴  several allowed picks the earliest of (M, S, R); never a preferred flag
```

A missing key is unreadable, not false. A method is chosen only when all three values are booleans and at least one is true.

## Change

In `scripts/merge_approved.py` only:

- Add `github_merge_flag(repo, gh) -> (flag | None, why)`. One `gh(["api", f"repos/{repo}"])`. Map `allow_merge_commit` → `--merge`, `allow_squash_merge` → `--squash`, `allow_rebase_merge` → `--rebase`. Nonzero exit, non-object JSON, a missing key, or a non-bool yields `(None, why)`. All three false yields `(None, "repository allows no merge method")`.
- On the GitHub merge path, after the request is ready: if the flag is `None`, print `merge-approved: #N not merged: …` and return 1 without `gh pr merge`. Otherwise replace `--squash` with that flag and keep `--match-head-commit`.
- Delete the comment that squash is how this plugin merges and that a `[workflow] merge_method` can override it. One sentence in the module docstring states the order and the refusal. Do not add a setting.
- Do not edit the GitLab `PUT`. Do not read settings on list, on a blocked request, or when `merge_owner` is `worker` or `user`.

`evals/run.py` `GH_SHIM` blocks every `gh api` except graphql and comment replies. `merge-what-the-user-approved` runs the real script, so an unreadable shim answer makes the script refuse and the agent may call `gh pr merge` itself. Answer `gh api repos/<owner>/<name>` (exactly two slashes) with all three booleans true. Leave every other `api` path blocked.

## Tests

`evals/test_merge_approved.py`. `FakeGh` answers that `api` call. Default all three true, so a merge still happens.

- Existing ready merge still sends `--match-head-commit` and now sends `--merge`, not `--squash` or `--rebase`.
- One table: only squash → `--squash`; only rebase → `--rebase`; squash and rebase → `--squash`; merge and rebase → `--merge`.
- All false, nonzero `gh`, invalid JSON, a missing key, and a non-bool (`"true"`) each exit 1 with no `pr merge` call. The settings `api` call did happen.
- A peer-blocked merge makes neither call.
- GitLab: workspace backlog `GitLab; host https://labs.example.test; project team/app`, `CHIEF_OF_STUFF_GITLAB_TOKEN` set so `token()` does not touch the Keychain. The merge `PUT` payload is exactly `{"sha": <head>}`.

`evals/test_run.py`: `gh api repos/o/app` exits 0 with the three true booleans; `gh api repos/o/app/pulls/1` still exits 1.

## Out of scope

No `[workflow]` merge method. No GitLab `squash` parameter. No change to `evals/test_cases.py` or the case graders (`gh pr merge 12 --squash` stays a forbidden direct command). No per-case shim fixture. No read of a default-method field; the repo object has the three booleans and the order above.

## Risks

- Choosing `--merge` whenever merge commits are allowed changes this repository from the failing `--squash` to `--merge`. That is the fix.
- Treating a partial body as unreadable refuses rather than merging on the keys that arrived.
- Forgetting the shim branch leaves unit tests green and makes the approval eval's script refuse.

## File ownership

- write: `scripts/merge_approved.py`, `evals/test_merge_approved.py`, `evals/run.py`, `evals/test_run.py`
- read: `scripts/forge_review.py`, `scripts/backlog.py`, `evals/cases/merge-what-the-user-approved/case.json`, `evals/test_cases.py`
