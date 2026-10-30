# Plan: issue #874, the worker guard judges permission requests (High)

## Finding: the work is already on `develop`

This worktree is on `work/issue-874-plan-1`, cut from `main` (0.128.67), and `main` has none of the guard files (`scripts/guard_rules.py`, `guard_commands.py`, `worker_guard.py`, `evals/test_guard*.py`), so the issue's file ownership cannot be edited from this base.

`origin/develop` (4417a8fe) already holds the fix: PR #875 (`loop/874`, "Permission requests are judged, never sent to the owner"), hardened by the architect audit (`b6711197`, `3a6e7014`), extended by #890 (`756e63ed`) and #954 (a retry takes the trio's answer). I read it with `git show origin/develop:<path>` and changed nothing there.

## Acceptance against develop

| Acceptance item | Where it is met on develop | Test |
|---|---|---|
| Bash judged by `guard_commands`, the PreToolUse gate's own `denial_reason` | `guard_commands.judgement` calls `denial_reason` first; `worker_guard.handle` calls `permission_judgement` | `test_guard.py` permission cases |
| allow answered automatically | `judgement` returns `("allow", "")`; `handle` emits `behavior: allow` | `test_the_143_read_is_allowed_without_anyone`, `test_routine_reads_and_own_tree_git_writes_are_allowed_without_anyone` |
| refused command denied with the reason | `("deny", OWNED/UNPARSED)`; `_deny` carries it as `message` | `test_a_merge_chained_after_a_read_is_denied_with_the_reason` |
| only the unjudgeable goes to the trio | `("trio", why)` records an allow/deny ask; `permission_ask` no longer decides | `test_an_unjudgeable_permission_is_an_ask_the_trio_is_consulted_on_first`, `test_unparseable_commands_and_secret_reads_go_to_the_trio` |
| compound commands parse: `&&`, `\|\|`, `;`, pipes | `_segments` splits on `SEPARATORS` | the #143 command; `sed ...; echo ...; rtk grep ...` |
| redirects to `/dev/null` and fds are not writes; a file inside the worktree is a write in scope | `judgement` checks each redirect target with `inside` and `_in_git_dir`; `/dev/null` and digit targets pass | `2>/dev/null` in #143; `ls > .git/config` goes to the trio |
| non-Bash tools follow the same rule | `permission_judgement`: Read/Glob/Grep/LS and Edit/Write/MultiEdit/NotebookEdit allowed inside the worktree, the rest to the trio | `test_reads_and_edits_are_allowed_inside_the_worktree_only` |
| red tests: #143 allowed, `&&` merge denied, unparseable goes to the trio with no owner decision | the tests above; the last asserts `db.decisions(KEY) == []` | present |

Residual gap: there is no permission-level test that `ls 2>&1 | head` is allowed, or that `ls > notes.txt` (a real in-tree file) is allowed, which is what `judgement` does by its docstring. If the owner wants in-tree redirects judged as writes that go to the trio, that is a rule change and needs a decision, not a test.

## Steps (implement stage)

1. Base: merge `origin/develop` into the stage branch (never force; the engine owns merges). Without it nothing below applies.
2. Add two cases to `evals/test_guard.py` beside the #143 test: `ls 2>&1 | head` is allowed, and `ls > notes.txt` is allowed with no ask; keep `ls > .git/config` and `echo x > /etc/hosts` going to the trio.
3. Run the guard evals (`evals/test_guard.py`, `test_guard_commands.py`, `test_guard_rules.py`) and the full suite. Nothing in `scripts/` changes.
4. If step 1 shows `develop` already satisfies every row, report done with the test additions only, or close the issue as delivered by #875.

## Risks

- Base drift: planning against `main` would produce edits to files that do not exist, so the implementer confirms `scripts/guard_commands.py` exists before editing.
- Auto-allow is a security surface: the architect audit found four holes (`VAR=` prefixes, git `-c`/`--ext-diff`, brace expansion, `.git` writes), all closed and tested, and any new entry in `READERS` needs the same scrutiny.
- The guard is a rail, not a sandbox (`guard_commands` docstring, design 9): engine merge verification stays the authority.
- This session's own PreToolUse gate refused a `for` loop and `git merge-base` as unparseable or outside the allowlist; that is the fail-closed behaviour and out of scope here.

## Logic

```
G := guard_commands judges a PermissionRequest (allow | deny | trio)
D := the fix for #874 is on origin/develop
B := the stage branch contains the guard files
A := every acceptance row is met by existing code and tests
W := the implementation has anything left to write
E := the implementation edits the issue's owned files

1. D                           [git log origin/develop: f20a4c52, 56da9591, b6711197; PR #875]
2. ¬B                          [HEAD tree has no scripts/guard_*.py]
3. D ∧ (acceptance table) → A  [tests named in the table]
4. A → (W ↔ residual test gaps only)
5. E → B                       [files must exist to be edited; Steps 1]
∴  merge develop first (B); then the work is two tests and a verification run, not a rewrite of G
```
