# Issue 662 — Project status when the Project has more than 5,000 items

## Problem

`kanban._project_items` (the issue text says `_project_rows`; the function is `_project_items`) loads the whole Project with `gh project item-list --limit 5000`. When `totalCount` is greater than the number of items returned, it returns `GitHub Project item list was truncated` and no rows.

Both callers fail closed on that error, even if the tracked issue is on the Project:

- `sync_github` reads status before a stage write and again to verify it (`_project_item`).
- `audit_tasks.kanban_faults` reads status once for the open tracked issues.

A Project whose `totalCount` equals the returned rows (including exactly 5,000) already counts as complete. That case stays correct by disappearing: this read no longer uses `item-list`.

## Decision

Read each tracked issue's `projectItems` with one `gh api graphql` call. Do not page `item-list`.

A higher `--limit` or a cursor walk still makes a stage write depend on Project size, and the next ceiling fails the same way. The callers only need two facts about the issues they already have: is this issue on the configured Project, and what is its single-select value.

```
P := the Project has more than 5,000 items
I := this issue is a member of the configured Project
R := the status read succeeds
A := the issue is reported absent
T := this issue's projectItems page has a next page
E := the GraphQL read failed

1. P ∧ I → R                 [today: P → ¬R]
2. A ↔ (¬I ∧ ¬E ∧ ¬T)        [absence is a finished membership read]
3. T ∨ E → ¬A ∧ ¬R           [a short or failed read is not absence]
∴ a large Project does not block an issue that is on it
```

## Behavior

`_project_items(gh, project, refs)` returns `(dict, error)`.

- The dict maps `IssueRef.url` to the single-select name, or to `None` when the issue is on the Project and that field is unset. A missing key means absent.
- `refs` is the open GitHub issues this call cares about. Empty `refs` returns `({}, "")` and does not call `gh`.
- Sync passes `(ref,)`. Audit passes every open GitHub ref it is about to check, in one call (chunks of 25 aliases, so two issues stay one call). Closed issues stay off this read, as they do now.
- Match a node when `project.number` equals `GitHubProject.number` and the owner login `casefold`s equal to `GitHubProject.owner`. Any other project is ignored.
- `fieldValueByName` uses `GitHubProject.field`. A `ProjectV2ItemFieldSingleSelectValue` contributes `name`. A null value is `None` (present, unset). Any other `__typename` is an error.
- Two nodes for the same Project, a null `issue`, a node whose owner login cannot be read, `pageInfo.hasNextPage`, a GraphQL `errors` entry, or a non-zero `gh` exit: return an error and no dict. Do not treat those as absent, and do not `item-add`.
- `item-add`, `item-edit`, and `field-list` stay as they are. `_github_labels_exist` still uses `--limit 5000`; that is a different read and out of scope.

Query shape, built like `board_sources.github` (inline `json.dumps` for owner, name, and field; do not import `board_sources`):

```graphql
query {
  r0: repository(owner: "OWNER", name: "REPO") {
    n7: issue(number: 7) {
      projectItems(first: 20) {
        pageInfo { hasNextPage }
        nodes {
          project {
            number
            owner { ... on User { login } ... on Organization { login } }
          }
          fieldValueByName(name: "FIELD") {
            __typename
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
      }
    }
  }
}
```

`shortcut: 20 memberships and chunks of 25, page or raise the chunk when a tracker or an issue exceeds that.`

## Tests

TDD in `evals/test_kanban.py`. Red first, then the production change. The `GitHubUpdaterTest.gh` fake answers `api graphql` and stops answering `project item-list`.

1. An `item-list` body with `totalCount` 5001 that omits the issue does not matter: sync preview returns the status from `projectItems` and never calls `item-list`.
2. Audit of two open issues on a Project that would truncate `item-list` reports no truncation and no absence, and issues one graphql call.
3. Memberships that are empty or only other projects: audit says `issue is absent from the configured GitHub Project`; sync with `--initialize` still `item-add`s.
4. Unset field (`fieldValueByName` null): the issue is present and its status is `None`.
5. `hasNextPage`, a null issue node, two nodes for this Project, a non-single-select value, or a failed `gh` call: the error is returned, the issue is not called absent, and sync does not `item-edit` or `item-add`.
6. `test_configured_status_field_is_requested` asserts the query carries the configured field name.
7. `test_github_project_audit_lists_items_once_for_multiple_tasks` still expects one `_project_items` call and asserts the open refs were the ones passed.

Run `python3 -m unittest evals.test_kanban` while iterating, then `python3 -m unittest discover -s evals -p 'test_*.py'`. The implementation report needs passed, failed, skipped, and xfailed from that full run.

## Risks

- A mistyped field name makes `fieldValueByName` null, so a preview or audit sees an unset status. The commit path still refuses a missing field via `field-list`. No extra field read on this path.
- The verify re-read can still race a slow Project update. Same race as today's second `item-list`. No retry.
- Confirm the fragment against GitHub's public ProjectV2 schema. Do not point a fixture at this repo's Project.
- `docs/kanban.md` does not mention the 5,000 cap. Leave it.

## File ownership

| path | mode |
|---|---|
| `scripts/kanban.py` | write |
| `scripts/audit_tasks.py` | write |
| `evals/test_kanban.py` | write |
| `scripts/board_sources.py` | read |
| `scripts/settings.py` | read |
| `scripts/backlog_ref.py` | read |
| `docs/kanban.md` | read |
