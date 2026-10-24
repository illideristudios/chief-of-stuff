# Plan: Remove engine file ownership (#1039)

## Goal

Restore engine flow by eliminating declared file scopes, trio `scope` decisions, and ownership write gates. Overlapping edits between issues are handled at merge/review time (separate worktrees). Target behaviour is already stated in `architecture/ARCHITECTURE.md` (2026-10-23, #1039); this issue changes **code only**—architecture docs stay with frank-lloyd-aight.

## Acceptance mapping

| Acceptance criterion | Implementation |
| --- | --- |
| Plans propose no scope; no `scope` decision | Remove `file_ownership` from MCP `report`, `scope_proposal` resume data, `_settle_scopes`, and plan-stage dispatch text that asks for a proposal |
| No write refusal for paths outside a list | Drop readiness holds for `needs file ownership`, file-scope claims, and overlap-based dispatch refusal; keep worktree-only guard (paths inside the worker tree) |
| Delete `ownership.py`, `scope_proposal`, dispatch ownership block | See file list below |
| Drop `ownership` table | New migration: move `mode=tree` rows to a dedicated `worktree` table, then `op.drop_table("ownership")` |
| Templates, skills, agent, README | Remove `## File ownership` / tracker File ownership workflow |
| Evals green | Delete or rewrite ownership tests; update agent eval fixtures that still require File ownership rows |

## Phased delivery

Issue notes recommend unblocking flow before the large deletion.

### Phase A — Stop scope decisions (small PR, merge first)

Unblocks issues waiting on trio `scope` / `scope_proposal` without finishing the full removal.

1. **`scripts/engine_mcp.py`**: Stop accepting `file_ownership` on `report`; do not write `scope_proposal` to resume.
2. **`scripts/engine.py`**: Remove `_settle_scopes` and its call site; remove `scope_pending` from facts assembly; stop syncing `ownership.issue_scopes` from forge bodies on each pass (or no-op if Phase B follows immediately).
3. **`scripts/dispatch_prompt.py`**: Plan stage delivery line — keep `Plan file: write … docs/plans/issue-<n>.md` but drop the `file_ownership` proposal sentence (use a tiny `plan_path()` helper, initially still imported from `ownership.py`).
4. **`scripts/reconcile.py`**: Remove `scope_pending` on `Facts`, `SCOPE_REPLANS`, and `_replan_for_scope`.
5. **Tests**: Adjust `evals/test_engine_mcp.py` and scope-related tests in `evals/test_engine.py` (delete scope trio cases or expect no `scope` decisions).

After Phase A, plan runs complete without trio scope approval; issues should no longer stall on `scope`.

### Phase B — Full removal (this issue’s main body)

Complete acceptance criteria and delete dead code.

## Store and migration

Today `store.Ownership` holds both **file scopes** (`write` / `read`, repo-relative) and **worktree locks** (`mode=tree`, absolute tree path). Acceptance requires dropping the `ownership` table; tree locks may remain if they never gate writes (#1039 notes).

1. **`scripts/migrations/versions/0007_drop_file_ownership.py`**:
   - Migrate `mode = 'tree'` rows (and absolute paths treated as tree per `replace_file_ownership`) into `resume.data["trees"]` (or a dedicated `worktree` table if preferred; keeping tree paths in `Resume.data["trees"]` avoids adding a new ORM table under §1 YAGNI since `Resume` already tracks `worktree`, `review_trees`, and `runs`).
   - Strip `"scope_proposal"` from `resume.data` on upgrade so issues with pending proposals do not carry dead keys.
   - `op.drop_table("ownership")`.
2. **`scripts/store.py`**: Remove `Ownership`, `set_ownership`, `ownership`, and `replace_file_ownership`. Expose `set_worktree(issue_key, path)` and `worktrees(issue_key) -> list[str]` for cleanup tree retention.
3. **`scripts/worker.py`**: Call `store.set_worktree(issue_key, ...)` instead of `store.set_ownership(issue_key, ..., "tree")`.
4. **`scripts/cleanup.py`**: Enumerate kept trees in `owned_items` from `db.worktrees(item.issue_key)` instead of `db.ownership(item.issue_key)`.
5. **`scripts/engine_facts.py`**: Stop building `readiness.Claim` list from file-scope ownership rows; stop populating `read_only` from `store.ownership` (leave `completion.py` and `reconcile.Facts.read_only` intact per `ARCHITECTURE.md` §4/§8).

## Delete `scripts/ownership.py`

Move surviving helpers before deletion:

- `plan_path` / `PLAN_DIR` → move directly into `scripts/dispatch_prompt.py` (do **not** create a separate `scripts/plans.py`, because `ARCHITECTURE.md` §4 removed `ownership.py` from `Entities` without adding `plans.py`, and `evals/test_architecture.py` enforces the exact §4 circle inventory).
- Remove: `issue_scopes`, `proposal_scopes`, `with_issue_scopes`, tracker `parse` / `cell` / `overlaps`, `read_only`, and migration helpers used only by `migrate_backlog.py --file-ownership`.

## Engine path

| Module | Change |
| --- | --- |
| `engine.py` | Remove forge body scope sync (`scopes` dict, `replace_file_ownership` loops), implicit plan `Claim`s, `_retire_ownership` naming cleanup (launcher hold logic stays; rename if misleading) |
| `engine_mcp.py` | Remove `ScopeEntry`, `file_ownership` field, `ownership` import |
| `issue_gate.py` | Register/start without `issue_scopes`; no `needs file ownership` refusal; no `replace_file_ownership` on intake |
| `readiness.py` | Dispatch when not blocked by asks/reports/deps; remove `needs file ownership` and file overlap `_conflict` (tree/process claims stay in `engine_facts` / orphans) |
| `reconcile.py` | Facts no longer carry file `claims` for readiness; update `why_not` callers |

## Coordinator / tracker path

Interactive dispatch still uses tracker **File ownership** today (`flow.py`, `task_forge.py`, `dispatch_prompt.refuse_overlap`, `one_shot.py`, `audit_tasks.parse_ownership`, `spawn_session` help text). Acceptance requires removing ownership from **agents**, **dispatch skill**, and **README**—treat tracker File ownership as deprecated in the same change set:

| Module | Change |
| --- | --- |
| `dispatch_prompt.py` | Remove `Owns:` from assignments, `refuse_overlap`, tracker ownership lookup in `compose` / `worker --check`; soften worker bootstrap text (`ownership` / `scope` stop reasons → coordinator without path lists) |
| `flow.py`, `task_forge.py` | Ready-task logic: drop path overlap filtering via `ownership.cell` / `overlaps` |
| `one_shot.py` | Drop requirement for File ownership row naming the tree before launch (tree still recorded via worktree table / launcher) |
| `audit_tasks.py` | Remove or narrow `parse_ownership` / ownership-driven orphan and unlanded checks; keep worktree/registry claims via `tree_claims` |
| `tree_claims.py`, `orphans.py` | Update docstrings; stop referring to tracker File ownership as a claim source |
| `spawn_session.py` | Help text: Tasks row checks only |
| `migrate_backlog.py` | Remove `--file-ownership` subcommand and `ownership_*` helpers (or entire path if unused) |
| `tracker_log.py` | Docstring only if it references File ownership for tree naming |

## User-facing docs and templates

- **`.github/ISSUE_TEMPLATE/task.yml`** and **`bug.yml`**: Remove `file_ownership` textarea.
- **`skills/dispatch/SKILL.md`**: Dispatch = Tasks row (+ issue); no File ownership row, no `Owns:` in proposal block, no overlap narrative.
- **`skills/coordinator-sessions/SKILL.md`**: Remove “file a session owns” routing where it means path ownership (keep “do not touch another session’s task” without path lists).
- **`agents/chief-of-stuff.md`**: Remove File ownership from ready definition, morning reads, tracker schema, dispatch rules, and orphaned-task path lists (point at worktree / issue instead).
- **`README.md`**: Remove “Engine issue ownership” section and worker-check wording about File ownership.

Do **not** edit `architecture/ARCHITECTURE.md` or `architecture/compliance.md` in this issue.

## Tests and evals

### Unit tests (high touch)

| File | Action |
| --- | --- |
| `evals/test_ownership.py` | **Delete** (module removed) |
| `evals/test_ownership_backfill.py` | **Delete** or reduce to any non-ownership backlog tests |
| `evals/test_architecture.py` | Remove `"ownership"` from `DRAWS_NOTHING` and `CIRCLES["entities"]`; add `"migrations/versions/0007_drop_file_ownership"` to `CIRCLES["adapters"]` |
| `evals/test_engine.py` | Remove scope proposal / `needs file ownership` / ownership sync cases; add “plan completes without scope decision” |
| `evals/test_engine_mcp.py` | `report` without `file_ownership` |
| `evals/test_engine_facts.py` | Remove `set_ownership` calls; update or remove `read_only` scope-driven tests |
| `evals/test_readiness.py` | No `needs file ownership`; no overlap conflicts on paths |
| `evals/test_reconcile.py` | Remove `_replan_for_scope` / `needs file ownership` / `scope_pending` tests |
| `evals/test_issue_gate.py` | Filing without ownership section succeeds; no `store.ownership` assertions |
| `evals/test_dispatch_prompt.py` | No refusal for missing File ownership row; plan text without proposal |
| `evals/test_flow.py`, `test_spawn_session.py`, `test_one_shot.py`, `test_audit_tasks.py` | Align with tracker/engine behaviour |
| `evals/test_store.py`, `test_cleanup.py`, `test_worker.py` | Use `set_worktree` / `worktrees` API instead of mixed `ownership`; test `0007_drop_file_ownership` migration |

### Agent eval fixtures

Many `evals/cases/*/fixture/CLAUDE.md` embed coordinator File ownership rules copied from the agent. After `agents/chief-of-stuff.md` changes, refresh fixtures (template sync or targeted case updates). Cases explicitly about ownership (`owned-item-not-redispatched`, `orphaned-work-has-no-owner`, `worker-check-validates-a-new-row`, etc.) need **rewrite or retire**.

### Verification command

Use the engine venv Python (which includes `sqlalchemy`, `alembic`, and `mcp`):

```bash
~/.claude/plugins/data/chief-of-stuff-chief-of-stuff/venv-*.current/bin/python3 -m unittest discover -s evals -p 'test_*.py'
```

Run after each phase; aim for `failed=0`, `skipped=0`, `xfailed=0` before implement stage marks done.

## Implementation order (Phase B)

1. Migration `0007_drop_file_ownership.py` + `store` worktree API (keep tests compiling).
2. Inline `plan_path` in `dispatch_prompt.py` + drop implicit plan claims in `engine.py`.
3. Engine MCP + engine pass (no scope sync/settle).
4. `readiness` + `reconcile` + `issue_gate` + `engine_facts`.
5. Coordinator scripts (`dispatch_prompt`, `flow`, `task_forge`, `one_shot`, `audit_tasks`, `migrate_backlog`).
6. Delete `ownership.py` and update `evals/test_architecture.py`.
7. Templates, skills, agent, README.
8. Eval suite sweep.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| **Concurrent path edits** without overlap gate | Accepted: merge conflict + existing review/CI gate; document in PR |
| **Losing tree lock** when dropping `ownership` | Migrate `tree` rows first; run `evals/test_worker.py` / cleanup tests |
| **Open issues with `## File ownership` bodies** | Harmless prose after sync removed; optional follow-up bulk edit not required for engine |
| **Stuck `scope_proposal` in resume** | One-time engine pass or migration snippet to `pop("scope_proposal")` on upgrade |
| **Large eval fixture churn** | Batch agent fixture updates; run `test_cases.py` subset early |
| **Coordinator vs engine divergence** | Single PR for both paths per acceptance on skills/agent/README |
| **Baseline suite not green in worktree** | Fix environment-specific failures separately; implement stage must report green counts after changes |

## Out of scope

- Architecture document updates (frank-lloyd-aight).
- Stripping historical `## File ownership` sections from every open GitHub issue.
- Changing worktree guard rules in `guard_commands.py` (worktree boundary stays).

## Commits

Small commits per phase step (migration, store, engine, coordinator, docs, evals) for reviewability; do not merge from the worker worktree.
