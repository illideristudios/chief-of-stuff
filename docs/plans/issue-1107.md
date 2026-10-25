# Issue #1107: one parser for store keys

## Outcome and scope

One function, `backlog_ref.store_ref(key) -> IssueRef | None`, parses a store key. `store_label` and `task_forge.source_key` both call that function. `source_key` stops building a URL and reparsing it. The label of another host's GitLab key is `IssueRef.label`: `host/project#N`, the same string `#903` already shows on a card.

This stage changes only this plan. Implementation does not loosen `issue_ref`'s refusal of another host's GitLab URL, does not add a second store-key grammar, and does not reformat keys that `forge.key` writes.

```
K := the string is a store key `github:owner/repo#N` or `gitlab:host/project#N`
F := it names a GitLab project on a host other than the workspace backlog
R := the label is `IssueRef.label` (`host/project#N` when F)
N := `issue_ref` of the rebuilt GitLab URL returns None

1. K ∧ F → R                         [IssueRef.label; #903 store_label test at 3f27617c]
2. N is required for a fetched URL   [issue_ref, any_host false; R1 on PR 14]
3. N ↛ ¬R                            [naming a key is not fetching it]
4. □(one parser of K)                [store_ref; store_label and source_key call it]
∴   K is parsed only by store_ref, and F still labels as R while a foreign URL still does not fetch
```

A foreign URL staying unreadable does not make the store key's label `#N` or the raw `gitlab:` string.

## Source baseline and implementation prerequisite

This worktree is `17552bbdb697b84345579a3048f03efa083b64f3`. It has `source_key` and `issue_ref`. It does not have `store_label` or `_REF_STORE`.

`store_label` exists only on the unmerged `#903` head. Pull request [#1106](https://github.com/illideristudios/chief-of-stuff/pull/1106) (`work/issue-903-plan-1`, head `3f27617caf9a09abd05ea4cae50a55c9f180c552`) was open and unmerged when this plan was written. The architect approved that head on 2026-10-25 and filed this issue as finding A1. Implementation starts after that pull request merges, on a base that already contains `store_label`. Do not copy `store_label` onto this plan branch, and do not merge #1106 from this stage.

Read-only inspection of `3f27617c` (no fetch, checkout, rebase, or merge):

- `scripts/backlog_ref.py`: `_REF_STORE` is `^(github|gitlab):(.+)#(\d+)$`. `store_label` matches it, builds an `IssueRef` (GitHub body is the repo; GitLab splits on the first `/` into host and project), and returns `ref.label(home)`. A non-match returns the original string.
- At that head, line 219 is `return IssueRef(...) if own else None` inside `issue_ref`. `own` is false for a GitLab URL on another host when `any_host` is false. On this plan branch the same return is line 202.
- `scripts/task_forge.py` `source_key` (lines 27–36) splits on the last `#`, rebuilds `https://github.com/.../issues/N` or `https://{host}/{project}/-/issues/N`, and calls `tracker.issue_key`.
- `tracker.issue_key` calls `issue_ref(..., any_host=True)`. A well-formed other-host key therefore does **not** hit the None return. Measured on this branch, with home `Backlog("https://gl.example", "g/p")`:

  | Store key | `source_key` today | `store_label` at `3f27617c` |
  | --- | --- | --- |
  | `gitlab:gl2.example/g/p#5` | `gl2.example/g/p#5` | `gl2.example/g/p#5` |
  | `gitlab:gl.example/g/p#5` | `#5` | `#5` |
  | `gitlab:gl.example/acme/web#5` | `gl.example/acme/web#5` | `gl.example/acme/web#5` |
  | `gitlab:gl2.example/only#5` | `#5` | `gl2.example/only#5` |

- The live disagreement is the last row. `_REF_GITLAB` requires a slash in the project path, so the rebuilt URL does not parse. `issue_key` then takes the trailing number and returns `#5`. `store_label` keeps `gl2.example/only#5`. That is the red case. The group/project row already agrees only because `issue_key` passes `any_host=True`; `issue_ref` of the same URL without that flag returns None, which is the split A1 names.
- `scripts/render_board.py` at `3f27617c` calls `store_label` for a card ref and a decision-row link. It does not parse the key itself. After `store_label` delegates, those call sites stay correct and stay out of this change.

## Implementation

1. **Add `store_ref` by extracting the match `store_label` already owns.** In `scripts/backlog_ref.py`, `store_ref(key: str) -> IssueRef | None` uses the existing `_REF_STORE`. GitHub yields `IssueRef(body, number)`. GitLab splits `body` on the first `/` into host and project, project keeping any further slashes, and yields `IssueRef(project, number, host)`. No match returns None. No `home` argument: the caller applies `label`. Do not strip, and do not add a second pattern. Do not reject a project path that `_REF_STORE` accepts today, including a single segment; `store_label` must stay a pure delegation so the `#903` assertions stay green.

   `store_label` becomes: `ref = store_ref(key); return key if ref is None else ref.label(home)`.

2. **Point `source_key` at `store_ref` and delete the URL rebuild.** A store key returns `store_ref(key).label(home)`. Anything `store_ref` rejects, including `#N`, a forge URL, `workflow`, and a `gitlab:` string that is not `prefix#digits`, still goes to `issue_key`. `source_key` no longer interpolates `https://` or calls `issue_key` on a URL it just built.

   `task_forge` is a use case and `backlog_ref` is an adapter (`architecture/ARCHITECTURE.md` §4). `evals/test_architecture.py` `OUTWARD_IMPORTS` may only shrink, and `task_forge→backlog_ref` is not in that set. A direct import fails `test_inward_to_outward_imports_match_the_known_list`. `tracker` already imports `backlog_ref` (`tracker→backlog_ref` is on the list). Add `store_ref` to that existing import and call `tracker.store_ref` from `source_key`. That is the same function object as `backlog_ref.store_ref`. Do not add an allowlist entry and do not edit `architecture/`.

3. **Leave the fetch parser strict.** `issue_ref` still returns None for a GitLab URL on another host unless `any_host` is set. `issue_key(..., any_host=True)`, `parse_issue_arg`, and the R1 regression `test_a_gitlab_url_on_another_host_is_no_reference` stay as they are. Store-key naming and URL fetching remain different gates.

4. **Do not move the other `#` splits.** `engine`, `worker`, `reconcile`, `readiness`, and `source_page` read a trailing number from a key the store already holds. `forge.key` writes `github:owner/repo#N` and `gitlab:host/project#N`. `migrate_backlog` parses a workspace id with no issue number. Those are not readers of the store-key grammar. One writer remaining beside one parser is a follow-on, not this issue.

## Tests and acceptance

Red tests first, on the post-#1106 base, then the production edits in small commits. Use the generic hosts already in `evals/test_backlog.py` (`gl.example`, `gl2.example`, project `g/p`). No live forge, no network, no workspace files.

| Test | Required evidence |
| --- | --- |
| `evals/test_backlog.py` | `store_ref("gitlab:gl2.example/g/p#5")` is `IssueRef("g/p", 5, "gl2.example")` and `.label(GL)` is `gl2.example/g/p#5`. The same for a nested project (`team/nested/app`), the home project (`#5`), another project on the home host (`gl.example/acme/web#5`), a GitHub home repo (`#N`) and another GitHub repo (`owner/repo#N`), and None home (full label). Non-keys (`""`, `workflow`, `#5`, a URL, `gitlab:gl2.example/g/p#5/extra`) are None. `store_label` of those keys stays equal to the `#903` results, including the unchanged original string for a non-key. `issue_ref("https://gl2.example/g/p/-/issues/5", GL)` is still None. |
| `evals/test_task_forge.py` (new) | The red assertion, committed before `source_key` changes: `source_key("gitlab:gl2.example/only#5", GL)` is `gl2.example/only#5`. Today it is `#5`. Also pin `source_key("gitlab:gl2.example/g/p#5", GL)` as `gl2.example/g/p#5` so the foreign-host label cannot fall back to None or the raw store key. Home, same-host other project, and GitHub keys match `store_ref(key).label(home)`. A non-key still follows `issue_key` (`#5` stays `#5`, `workflow` stays `workflow`). |

The group/project `source_key` assertion is already green on this branch. It is not the red test. The red test is the single-segment other-host key, whose URL reparse drops the host, plus `store_ref` itself, which does not exist until step 1. Run the new tests and confirm failure before editing `source_key`. Then make them pass.

After the focused modules (`evals.test_backlog`, `evals.test_task_forge`, `evals.test_architecture`, `evals.test_flow_board`, `evals.test_render_board`), run `python3 -m unittest discover -s evals -p 'test_*.py'` with `CHIEF_OF_STUFF_WORKSPACE` unset. Acceptance of the implementation is passed > 0 and failed, skipped, and xfailed all zero. `test_architecture` must stay green without a new outward-import line.

## Risks and mitigations

- **#1106 has not merged.** `store_label` is absent here. Starting on `17552bbd` would invent a second copy. Wait for the merge and recheck the `store_label` body against this plan before editing.
- **The obvious other-host key is not red.** `gitlab:gl2.example/g/p#5` already labels correctly through `any_host=True`. A test of only that key would go green without `source_key` changing. The single-segment key is the assertion that fails today.
- **A direct import fails §5.** Call `tracker.store_ref`. Do not grow `OUTWARD_IMPORTS` and do not change `compliance.md` or `ARCHITECTURE.md`.
- **Delegation changes a label.** `store_label` must return `ref.label(home)` for a match and the original string otherwise. Reuse `_REF_STORE` and the same split; do not strip or tighten the match in this issue.
- **Foreign-host fetch regresses.** `store_ref` must not set `any_host` or call `issue_ref`. Keep the R1 test green.
- **Cache lookups move.** `source_key` is how `render_board`, `issue_page`, and `source_page` turn a store key into the cache key `board_sources` files. For every key the two parsers already agree on, the label stays the same. The only intended change is a key the URL grammar currently collapses to `#N`.

## Proposed implementation file ownership

Paths are repository-relative to the post-#1106 tree.

| Mode | Path | Purpose |
| --- | --- | --- |
| write | `scripts/backlog_ref.py` | `store_ref`, and `store_label` calling it. |
| write | `scripts/tracker.py` | Re-export `store_ref` on the existing `backlog_ref` import. |
| write | `scripts/task_forge.py` | `source_key` calls `store_ref` and stops building URLs. |
| write | `evals/test_backlog.py` | `store_ref` and `store_label` label coverage, foreign URL still None. |
| write | `evals/test_task_forge.py` | Red-then-green `source_key` label for another host. |
| read | `scripts/render_board.py` | `store_label` call sites introduced by #1106. |
| read | `scripts/issue_page.py` | `source_key` cache lookup. |
| read | `scripts/source_page.py` | `source_key` cache lookup. |
| read | `scripts/forge.py` | Store-key writer `Forge.key`; not edited. |
| read | `evals/test_architecture.py` | Outward-import equality. |
| read | `evals/test_flow_board.py` | Hold and card behavior that calls `source_key`. |
| read | `evals/test_render_board.py` | Board rendering fixtures. |
| read | `architecture/ARCHITECTURE.md` | §4 circles and §5 dependency rule. |
| read | `CLAUDE.md` | TDD and the eval command. |

## Planning-stage validation

Only this Markdown file is changed. No production or test module was edited. The baseline below is the current checkout, which does not contain `store_ref`, `store_label`, or `evals/test_task_forge.py`. System Python lacks `alembic`, `sqlalchemy`, and `mcp`, so twelve architecture import probes fail there before any assertion about circles. The supported engine interpreter is the one that has `requirements.txt` installed.

```text
env -u CHIEF_OF_STUFF_WORKSPACE PYTHONDONTWRITEBYTECODE=1 \
  ~/.claude/plugins/data/chief-of-stuff-chief-of-stuff/venv-cc2edd13350e11bd.current/bin/python \
  -m unittest evals.test_backlog evals.test_architecture evals.test_flow_board evals.test_render_board
Ran 364 tests — OK
passed=364, failed=0, skipped=0, xfailed=0
```
