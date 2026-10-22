# Plan: #883 — Ownership backfill for GitLab workspaces

## Base and verdict

This planning worktree (`work/issue-883-plan-1`) is branched from `main` at `50091f80`, which predates `#820`, `#823`, and `#883` (`evals/test_ownership_backfill.py` is absent here, `scripts/migrate_backlog.py` has no `--file-ownership` mode, and `scripts/ownership.py` has no `derive_scopes`). Do not implement on `50091f80`.

On `origin/develop` (`4417a8fe`), PR `#914` (`fc297548`: red `20a518fd`, green `0b05c9cf`, docs `5b942434`) already implements `#883`. Subsequent commits on `origin/develop` must be preserved:
- `#915` (`4cf482ab`, `81ccd258`) added `plan_path` and `proposal_scopes` to `scripts/ownership.py` and landed `#820`.
- `#989` (`52e7d06e`) split `ownership.derive_scopes` into `_aliases`, `_is_read_task`, `_named`, `_line_mode`, and `_add_companion_tests`.

The implementation stage should cut from `origin/develop`, verify the existing tests pass without regressing `#915` or `#989`, and make no code changes unless a test fails.

## Logic

```
G := the workspace is a GitLab project (gitlab:<host>/<group>/<project>=<path>)
D := migrate_backlog --file-ownership --dry-run runs
A := migrate_backlog --file-ownership --apply runs
M := --approval-pr names a GitLab MR on that same project whose current head has Architect: APPROVED <sha>
     (unrevoked by a later Architect: CHANGES <sha>) and whose file at --plan-path matches the local plan byte-for-byte
H := --approval-pr names a GitHub PR or an MR on a different GitLab project
O := the issue is still open and its title/body digest still matches the dry-run snapshot

1. G ∧ D → issues are fetched via backlog.issues(home), derived via ownership.derive_scopes (never inferring "."),
           validated by issue_gate.ownership.issue_scopes, and written to <report>.md and <report>.json
2. G ∧ A ∧ M ∧ O → issue descriptions are written via backlog.set_description(home, number, candidate)
3. G ∧ A ∧ M ∧ ¬O → the issue is skipped (closed or changed since dry-run)
4. G ∧ A ∧ ¬M → apply refuses before any write
5. (G ∧ H) ∨ (¬G ∧ M) → apply refuses cross-forge and cross-project approvals
∴ GitLab workspaces share derivation, refusals, and plan format with GitHub, and write only under an approved MR on their own project
```

## Landed behavior on `origin/develop`

| Acceptance item | Implementation on `origin/develop` | Verification |
|---|---|---|
| `--file-ownership --dry-run` reads GitLab issues through the GitLab backlog client, with the same derivation, refusals (never inferring `.`), and plan file | `scripts/migrate_backlog.py`: `GITLAB_WORKSPACE`, `gitlab_home`, `issue_url`, and `ownership_main` (calls `bl.issues(home)` and passes `[dict(number=i.iid, title=i.title, body=i.body) for i in got.issues]` to `ownership_plan`). `scripts/backlog.py`: `_issue` sets `body=str(row.get("description") or "")`. `scripts/ownership.py`: `derive_scopes` is forge-independent and never infers `.`. | `evals/test_ownership_backfill.py`: `DerivationTest`, `PlanTest`, `GitLabTest.test_a_dry_run_reads_a_gitlab_workspace_through_the_backlog_client`, `GitLabTest.test_the_report_links_gitlab_issues_on_their_own_host` |
| Apply writes GitLab issue descriptions through the same client, gated on architect approval of the exact plan | `scripts/backlog.py`: `set_description` sends `PUT {cfg.issues_url}/{iid}` with `{"description": description}`. `scripts/migrate_backlog.py`: `_gitlab_ops` and `apply_ownership` verify `GITLAB_APPROVAL` on the same project, check `_verdict(notes, mr['sha']) == 'APPROVED'`, compare `/repository/files/<plan_path>?ref=<head>` against `plan_text`, re-read via `client.read_one`, re-check `approved() == head` before each write, and write via `client.set_description`. | `GitLabTest.test_an_approved_mr_on_the_same_project_writes_the_description_through_the_gitlab_client`, `test_an_unapproved_or_stale_mr_writes_nothing`, `test_a_closed_gitlab_issue_is_skipped`, `test_cross_forge_and_cross_project_approvals_are_refused` |
| Dry run and apply for GitLab workspaces, with plans committed for review | Per `CLAUDE.md` ("Fixtures are generic. No data from any workspace that uses this agent"), `architecture/ARCHITECTURE.md` §11, and the architect ruling on `#883` (items 1–3, documented in `docs/backlog.md`), no material derived from external GitLab workspaces enters this repository or a GitHub PR. Each GitLab workspace's plan is committed and reviewed in an MR on its own GitLab project (`https://<host>/<group>/<project>/-/merge_requests/<n>`) and applied there by the operator holding that host's token. | `GitLabTest.test_cross_forge_and_cross_project_approvals_are_refused`; `docs/backlog.md` |
| Red tests for GitLab reads and writes against mocked API responses | `evals/test_ownership_backfill.py` `GitLabTest` (added in `20a518fd` using the invented workspace `gitlab:labs.example.test/team/widgets`). | `python3 -B -m unittest evals.test_ownership_backfill` |

## Implementation stage steps

1. Cut or rebase the implementation worktree onto `origin/develop` (`4417a8fe` or later) and verify `fc297548` (`0b05c9cf`), `4cf482ab`, and `52e7d06e` are in history:
   ```sh
   git log --oneline origin/develop -- scripts/migrate_backlog.py scripts/ownership.py scripts/backlog.py evals/test_ownership_backfill.py docs/backlog.md
   ```
2. Run the backfill, ownership, backlog, and architecture test suites on `origin/develop`:
   ```sh
   python3 -B -m unittest evals.test_ownership_backfill evals.test_ownership evals.test_migrate_backlog evals.test_backlog evals.test_architecture
   ```
3. If all tests pass (`passed > 0`, `failed = 0`, `skipped = 0`, `xfailed = 0`), make no code edits and report `done` with the suite counts. Edit code only within the ownership scope below if a regression is found.
   - Optional coverage check (only if needed): `GitLabTest` injects a stub `client.set_description`; if a direct unit test of `backlog.set_description` against the local HTTP test server is desired, it belongs in `evals/test_ownership_backfill.py` (within this issue's write scope) or `evals/test_backlog.py`. None is required unless a gap is found.

## Tests

- On `origin/develop`: `evals/test_ownership_backfill.py` (`DerivationTest`, `PlanTest`, `ApplyTest`, `GitLabTest`), `evals/test_ownership.py`, `evals/test_migrate_backlog.py`, `evals/test_backlog.py`, and `evals/test_architecture.py`.
- Baseline run on this planning checkout (`50091f80`, where `evals/test_ownership_backfill.py` is not yet present):
  `python3 -B -m unittest evals.test_migrate_backlog evals.test_ownership evals.test_backlog evals.test_architecture` — 136 passed, 0 failed, 0 skipped, 0 xfailed.

## Risks

- **Stale worktree base (`50091f80`):** Re-implementing `#883` from `50091f80` would conflict with `fc297548` and overwrite `#915` (`plan_path`, `proposal_scopes`) and `#989` (the helper split in `scripts/ownership.py`). Step 1 prevents this by requiring `origin/develop` as the base.
- **Cross-host data leak (§11 / house rule):** Committing plans or issue text from real GitLab workspaces into `chief-of-stuff` would violate the generic-fixtures rule and the architect ruling on `#883`. `_github_ops` and `_gitlab_ops` enforce that each GitLab project's plan is reviewed and approved only on an MR in that same GitLab project, and tests use only the invented `gitlab:labs.example.test/team/widgets` workspace.
- **Concurrent edits or MR head movement during apply:** `apply_ownership` re-reads each issue via `read_one` to check its title/body digest and open state, and re-verifies `approved() == head` before each `set_description` call.

## Proposed file ownership

- write: `scripts/migrate_backlog.py`, `scripts/ownership.py`, `scripts/backlog.py`, `evals/test_ownership_backfill.py`, `docs/backlog.md`
- read: `scripts/backlog_ref.py`, `scripts/issue_gate.py`, `scripts/md.py`, `evals/test_ownership.py`, `evals/test_migrate_backlog.py`, `evals/test_backlog.py`, `evals/test_architecture.py`
