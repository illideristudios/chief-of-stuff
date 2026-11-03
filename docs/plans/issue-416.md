# Issue 416 — card and audit tag link to the other project's issue

A board card whose issue is a GitLab issue in another project on the home host already shows that project's key (`IssueRef.label`: `host/project#N`). The link still opens `/issues/N`, the home task page for the same number. The audit's close line has the same split: the tag is the long key, and the command is `backlog --close N`, which closes the home issue.

Home issues stay on `/issues/N`. Flow rows stay on `#N` and `/issues/N` (#459). Kanban already updates a same-host sibling through `Backlog.sibling` and does not change.

## Cause

`render_board.build_columns` builds the visible key with `issue_key`, then `page()` links whatever ends in a number to `/issues/<n>`:

```171:174:scripts/render_board.py
    def page(cell: str, other: str) -> str:
        """A card name's link: its task page when the issue ends in a number, as Flow rows link, else `other` (#194)."""
        m = TRAILING_NUMBER.search(cell)
        return f"/issues/{int(m[1])}" if m else other
```

The card name calls `page(task.issue, …)`. The ref chip calls `page` on the display key. Both cells end in the issue number, so both land on the home page. `issue_url` already has the forge URL (the cached issue, else the cell when it is `http(s)`). `fragment.href` opens an `https://` URL in a new tab.

`audit_tasks.issue_faults` tags with `ref.label(home)` and, when a done task's issue is still open, prints `chief-of-stuff backlog --close {ref.number}`. `backlog --close` accepts only the home project (`evals/test_backlog.py` refuses `other/repo#5`). A same-host GitLab URL parses (`issue_ref`) and is then refused for not being the home project, so the printed command closes the wrong issue.

`issue_ref` does not parse the display key `host/group/project#N`. Decide from the tracker cell (a GitLab URL) or from `IssueRef`, and pass the already resolved URL through as `other`. Do not teach the parser the display key.

## Behavior

In-app `/issues/N` only when the cell is the home issue, or when no home is configured and the cell is the kind of reference that links there today (`#N`, a home URL, a non-URL that merely ends in a number).

When `sources.home` is set and the cell's `IssueRef` is a different `(host, repo)`:

- The card name and the issue-ref chip keep today's label (`#N` at home, `host/project#N` or `owner/repo#N` otherwise).
- Both hrefs are the other project's issue URL: `sources` issue URL, else the cell when it is already `http(s)`, else `IssueRef.url`.
- A change chip on that card follows the same `page()` (#209: no route for the change itself), so it opens that issue URL rather than `/issues/N`.
- An external URL keeps `target="_blank" rel="noopener"`.

Audit, when a done task's issue is still open:

- The tag stays `ref.label(home)`.
- Home: `chief-of-stuff backlog --close N --commit`, unchanged.
- Another project on the home GitLab host: `chief-of-stuff backlog --close <ref.url> --commit`. `--close` of that URL closes the sibling with the home token (`Backlog.sibling`, as kanban already does). Another host stays "not an issue reference" and is never requested.
- A GitHub issue outside the backlog: do not print `--close N`. Print `ref.url` as the issue to close. `backlog --close other/repo#N` stays refused (`test_close_refuses_an_issue_outside_the_backlog`).

## Tests first

One failing case per behavior, then the code that turns it green. Run `python3 -m unittest discover -s evals -p 'test_*.py'` for the modules touched, and do not stop at the new cases.

1. `evals/test_flow_board.py`, beside `HomeProjectUrlTest`. Home `https://example.com/o/app`. Cell `https://example.com/acme/web/-/issues/8`, with and without a cached issue. The card ref text is `example.com/acme/web#8`. Name href and ref href are `https://example.com/acme/web/-/issues/8`, not `/issues/8`. The existing home URL case still expects `(("#8", "/issues/8"),)`.
2. `evals/test_audit_tasks.py`, beside `SameHostProjectTest`. A done task whose open issue is `https://gl.example/g/other/-/issues/2` prints `--close` of that URL, and does not print `--close 2`. A done task on a GitHub issue outside the backlog (`x/y#2`) does not print `--close 2`; the line names `https://github.com/x/y/issues/2`. The home `#7` line in `GitLabIssueAuditTest` stays `--close 7`.
3. `evals/test_backlog.py`. `--close` of a same-host sibling issue URL closes that project's issue. `--close other/repo#5` and another host stay refused.

## Edits

- `scripts/render_board.py` — `page()` takes the foreign branch above. Call it with the tracker cell for both the name and the ref chip; keep using `issue_key` for the chip text. Leave `flow_ref` alone.
- `scripts/audit_tasks.py` — home ref: keep `--close {number}`. Same-host GitLab sibling: `--close {ref.url}`. Any other non-home ref: name `ref.url`, no `--close` of the home number.
- `scripts/backlog.py` — `--close` of a GitLab ref on the home host and a different project calls `close(cfg.sibling(ref.repo), ref.number)`. Home refs stay `close(cfg, ref.number)`.

## Risks

- Flow (`flow_ref`) and the workers page still link by trailing number. Opening those still shows every tracker row whose issue cell ends in that number (`issue_page` matches on `issue_number` only). This issue does not add a project-qualified route.
- A foreign key with no URL and no parseable cell must render as plain text, not `/issues/N`.
- Sibling close must use the home host's token only. A host mismatch is a refusal, as in `ForeignHostTest`.
- GitHub issues outside the backlog stay unclosable. The card still uses the forge URL. The audit line names that URL and does not offer `--close N`.
