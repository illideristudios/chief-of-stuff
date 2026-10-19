# Issue 412: identify issue pages by host, project, and number

## Outcome and acceptance

Two tracked issues with the same number must have distinct issue pages and source pages when their projects or hosts differ. Every link into those pages must preserve the same identity, and each page must select only that identity's tasks, Flow rows, cached issue, and associated changes. A missing identity returns 404 even when another tracked issue has the requested number.

This stage changes only this plan. Implementation follows the steps below using failing Python regression tests first, as required by `CLAUDE.md`.

## Evidence in the current code

- `scripts/pages.py`: `ISSUE` accepts only `/issues/(number)[/source]`; `Handler._allowed` maps it to `issue-(number).html` or `issue-(number)-source.html`. `fresh` recovers only an integer from that filename. Locks, freshness records, stale content, and route-only file protection consequently share the numeric namespace.
- `scripts/issue_page.py`: `render` selects tasks and Flow rows by `issue_number(...) == number`, reads `sources.issues['#N']`, and selects changes containing `#N`. Its Flow build omits `home=sources.home`. `_files` and `write` construct numeric links and filenames.
- `scripts/source_page.py`: `render` matches the trailing number, reads `#N`, and links back to a numeric task URL. `write` independently selects the change using `#N` before fetching notes.
- `scripts/render_board.py`: `build_columns.page` drops identity using `TRAILING_NUMBER`; this affects task names, issue references, change references, and merged-today cards. `flow_ref` also builds a numeric URL even though Flow rows already carry project-aware keys.
- `scripts/workers_page.py`: `_task` repeats the trailing-number link generation for running and ended workers.
- `scripts/backlog_ref.py`: `IssueRef(repo, number, host)` already provides the identity model. `issue_ref(..., any_host=True)` parses supported issue cells without authorizing forge access. `IssueRef.label(home)` provides exactly the home-relative keys used by `board_sources` and `tracker.issue_key`.
- `issue_ref` does not parse the stored GitLab label `host/project#N`; routing must handle that canonical label explicitly rather than pass it through `tracker.issue_key`'s trailing-number fallback. Qualified GitHub labels have two project components; GitLab labels include the host followed by at least two project components.

## Route and identity contract

Use `/issues/<host>/<project path>/<number>` and the same route with `/source` appended. For example:

- `/issues/github.com/team/app/8`
- `/issues/github.com/team/other/8`
- `/issues/labs.example.test/group/subgroup/app/8/source`

The host and the complete project path remain present for the home project as well. Parse the final numeric component after removing the optional `/source`; allow nested GitLab groups. Build and decode routes with one shared helper, with segment validation and canonical integer formatting. Preserve existing host/project semantics from `IssueRef`; do not add unrelated normalization of case, ports, or URL schemes in this issue.

Retain `/issues/N[/source]` as a compatibility alias for the configured home project only, using a temporary redirect to its qualified URL for GET and HEAD and preserving any query string. Never search all projects for the first or only issue numbered N. When there is no home configuration, retain numeric routes only for explicitly bare `#N` tracker cells; they belong to a local unresolved namespace and must never match qualified references. Explicit URLs and project keys still produce qualified routes without a configured home. If neither a supported reference nor an explicitly bare number can be resolved, keep the existing external link/text fallback.

Renderers accept `IssueRef` targets, keeping integer inputs as a compatibility wrapper: an integer resolves in the configured home, or to the strictly bare namespace when no home exists. This keeps existing direct renderer callers usable without retaining number-based matching. Compare all resolved task identities and Flow keys exactly. Use `target.label(home)` only for source-cache lookup and change membership; keep complete identity for navigation and storage.

## Implementation sequence

1. **Add identity and routing regressions.** Extend the existing issue/source/board/workers/server tests and add focused helper tests. Start with two same-number issues in different projects and then the same project path on two hosts. Give them different titles, states, changes, acceptance bullets, file lists, and worker histories so assertions detect mixed content, not merely different URLs.
2. **Introduce `scripts/issue_routes.py`.** Keep this a small, pure presentation adapter with no filesystem, network, credential, server, or renderer dependencies. Reuse `backlog_ref.IssueRef` and `issue_ref`; provide reference/key-to-target resolution, route formatting/parsing, and rendered filename generation. Parse source-cache GitLab keys explicitly using their host/project/number structure. Reject malformed references rather than invoke a trailing-number fallback. Resolve home configuration consistently: persisted `sources.home` when available, with the workspace backlog as fallback at write/render boundaries when the cache has not been populated.
3. **Separate storage identities.** Generate bounded filenames such as `issue-q-<sha256>.html` and `issue-q-<sha256>-source.html`, hashing an unambiguous serialization of `(host, full project, number)`. Use the full digest, not a truncated hash or lossy slug; the `q-` marker separates qualified filenames from numeric legacy names even for an all-digit digest. Keep `issue-N*.html` only for the unresolved bare namespace. Do not recover identity from a hash: pass the parsed target explicitly through `Handler._allowed` to `fresh` and the writers; verify that target and expected basename agree. Include the page kind in the filename as today. Register the new helper in the architecture document's presenter list and the existing classification test without adding dependency exemptions or making model modules import page code.
4. **Update the server.** Replace numeric-only route parsing with the shared helper, implement the home aliases, and thread targets through lazy render dispatch. Extend `RENDERED` to recognize qualified filenames while continuing to protect old numeric filenames from direct requests and `/all` listings. Preserve checked-path containment, dotfile refusal, per-file locks, render slots, byte-digest freshness, HEAD/reload behavior, and last-good-page error handling. Those mechanisms should operate on distinct target filenames. Unknown or removed identities must remove/404 only their own file. Old numeric cached pages must never satisfy a qualified request or a home alias through a stale-file shortcut.
5. **Update issue rendering.** Normalize the target once and select `mine` by full identity. Pass the effective home to `flow_chart.build`, compare each Flow reference by full identity, and use the matching key for the issue and changes. Continue building Flow from the full tracker context so workflow/change-only passes retain their existing association behavior; filter the result by identity. Derive log and worker history from the matching task set, preserving existing name-based log semantics. Pass the target to `_files` and the writer so source links, file anchors, and output paths preserve identity. Escape qualified labels wherever they enter HTML. Display a qualified label for non-home issues so two `#N` pages are distinguishable.
6. **Update source rendering.** Normalize the target before both `render` and the writer's `_change`/notes selection. Match tracker tasks and cache entries by the same full identity as the issue page. Build its task backlink and output filename through the helper. Preserve current change ordering and empty/unavailable-diff behavior. A route target does not authorize a request to that host: notes still use an already associated change and existing configured credentials. The current notes function queries the configured home change project; do not make it fetch an unrelated same-number change for a foreign change. Where a selected change is outside that configuration, show notes unavailable without a forge call. Broadening forge collection or supporting foreign clones is outside this fix.
7. **Update every navigation producer.** Use shared resolution/formatting for board card names, issue refs, change refs through their first associated issue, merged-today cards, board Flow links, and both workers tables. Feed full task cells or canonical row/change keys into the helper before abbreviating visible labels. Pass the effective home through these paths; use the workspace backlog fallback when a cold source cache lacks `home`. Preserve external links for changes with no issue and unsupported/nonissue cells. Update README route documentation and affected module/test contract comments.

## Tests and verification

Run meaningful regressions against the old implementation first; confirm that the identity-isolation assertions fail. No live forge access or browser automation is needed. Use the existing temporary-workspace server fixtures and mocked forge calls.

| Area | Required coverage |
|---|---|
| Helper (`evals/test_issue_routes.py`) | Round trips for GitHub short keys/URLs, GitLab URLs including work items and nested groups, markdown links, home `#N`, canonical GitLab cache keys, and absent-home bare refs. Same number across projects/hosts gives different routes and filenames; equivalent forms of one identity agree. Unrelated text ending in a digit does not become an issue. |
| Issue page (`evals/test_issue_page.py`) | Both colliding targets show only their own title, issue state, acceptance, changes, Flow rows, history, workers, and references. Cover home bare vs equivalent full URL, foreign-only tracking, exact unknown target, no cached issue, and disappearance after tracker updates. A page cannot borrow another project's closed state or done task. |
| Source page (`evals/test_source_page.py`, `evals/test_source_notes.py`) | Different associated changes/file lists for colliding targets; correct task backlink, source links and file anchors; empty-cache/no-change output; notes are selected for the same change shown on the page. Foreign changes cannot trigger home-project same-number notes or send credentials to a route-specified host. |
| Navigation (`evals/test_flow_board.py`, `evals/test_issue_page.py`, `evals/test_workers_page.py`) | Distinct hrefs for cards, refs, merged cards, Flow rows, running workers, and ended workers. Cover home URLs, canonical foreign keys, cold caches, and changes without an associated issue. Update old assertions that intentionally encode number-only behavior. |
| HTTP/cache (`evals/test_pages.py` and existing issue/source route tests) | GET and HEAD of each qualified issue/source route; home numeric redirects; strict absent-home bare compatibility; unknown identities/malformed paths 404. Repeated requests preserve separate content, cache files, freshness records, locks, and stale-error responses. Same-number targets can render independently while concurrent requests for the same target serialize. Changing/removing one target never serves or deletes the other's cache. |
| Route protection | Direct requests for both new and legacy rendered filenames remain 404 and absent from `/all`. Reject dot/traversal segments, encoded separators, invalid host/project segments, extra suffixes, malformed or nonnumeric IDs, and double-decoding attempts. Query strings and source file anchors preserve route identity. Hash filenames stay bounded for long nested project paths. |
| Architecture | New helper is classified; no model imports presentation code, new import cycles, or expanded dependency exemptions. |

Suggested focused command after implementation:

```text
python3 -B -m unittest discover -s evals -p 'test_issue_routes.py'
PYTHONPATH=evals python3 -B -m unittest test_issue_page test_source_page test_source_notes test_source_split test_flow_board test_flow_chart test_workers_page test_pages test_closed_issue_stop test_architecture
python3 -B -m unittest discover -s evals -p 'test_*.py'
```

Report actual final suite counts for passed, failed (including errors), skipped, and expected failures; do not infer new implementation coverage from this planning run. The engine's green gate requires passed > 0 and all three other counts zero. Any unrelated baseline failure must be documented, not repaired outside this issue's approved ownership.

Planning-stage baseline: `PYTHONPATH=evals python3 -B -m unittest test_issue_page test_source_page test_flow_board test_workers_page` completed with **155 passed, 0 failed, 0 skipped, 0 xfailed**. These tests validate existing behavior only; the implementation's new isolation regressions remain to be written. An earlier full discovery run was interrupted before completion and supplies no final suite counts. `git diff --check` also passed.

## Risks and boundaries

- **Incomplete propagation:** fixing hrefs alone leaves rendering and cache collisions. Acceptance requires the complete route-to-render-to-source round trip and all navigation producers.
- **Cache keys are relative to home:** comparing an absolute target directly to `#N`, or using a stale/absent cache home without the workspace fallback, silently loses content. Resolve full identities first, then derive the home-relative lookup key.
- **Compatibility:** existing numeric bookmarks become home aliases. Foreign issues previously reached by number must use qualified URLs; no safe number-based inference exists. Keep old generated-file protection during this transition, without deleting unrelated files or migrating forge/tracker data.
- **Path and HTML boundaries:** project paths can be nested and route inputs are untrusted. Segment validation, canonical serialization, bounded cache basenames, escaping, and the existing containment checks must agree.
- **Foreign forge data:** board source collection and the configured graph clone have existing limits. Render available cached data or the existing unavailable state; do not invent missing changes or redirect home credentials based on the route.
- **Existing task-name association:** tracker Log lines identify tasks by name/item, not issue identity. Tests should use distinct task names across projects. Changing that log schema or repairing same-name task collisions is separate work.
- **Scope:** do not refactor shared page utilities, rewrite Flow grouping, change issue-state storage, change forge authentication, or broaden source fetching. Existing architecture gaps unrelated to the new routing adapter remain outside this fix.

## Proposed implementation ownership

The planning stage owns only `docs/plans/issue-412.md`. The implementation stage should request these exact paths:

| Mode | Repository-relative path |
|---|---|
| write | `scripts/issue_routes.py` |
| write | `scripts/pages.py` |
| write | `scripts/issue_page.py` |
| write | `scripts/source_page.py` |
| write | `scripts/render_board.py` |
| write | `scripts/workers_page.py` |
| write | `evals/test_issue_routes.py` |
| write | `evals/test_pages.py` |
| write | `evals/test_issue_page.py` |
| write | `evals/test_source_page.py` |
| write | `evals/test_source_notes.py` |
| write | `evals/test_flow_board.py` |
| write | `evals/test_workers_page.py` |
| write | `evals/test_architecture.py` |
| write | `architecture/ARCHITECTURE.md` |
| write | `README.md` |
| read | `CLAUDE.md` |
| read | `docs/plans/issue-412.md` |
| read | `scripts/backlog_ref.py` |
| read | `scripts/backlog.py` |
| read | `scripts/tracker.py` |
| read | `scripts/tracker_log.py` |
| read | `scripts/board_sources.py` |
| read | `scripts/flow_chart.py` |
| read | `scripts/task_forge.py` |
| read | `scripts/workspace.py` |
| read | `scripts/settings.py` |
| read | `scripts/decision_page.py` |
| read | `scripts/fragment.py` |
| read | `scripts/page_reload.py` |
| read | `scripts/module_graph.py` |
| read | `evals/test_flow_chart.py` |
| read | `evals/test_closed_issue_stop.py` |
| read | `evals/test_source_split.py` |
| read | `.github/workflows/tests.yml` |
| read | `.github/ci/Dockerfile` |
