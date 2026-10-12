"""The one forge adapter (1.0 plan, M2): a workspace's GitHub or GitLab, read and written through one object.

The forge is the source of truth and the store a cache rebuilt from `snapshots`. It reads issues (backlog.py), open pull or merge
requests with their CI and review state (merge_ready.py over forge_review.py), and the user's review threads (review_threads.py);
it creates issues (backlog.py), writes stage back as labels or a GitHub Project Status (kanban.py), and merges verified heads
(merge_approved.py). Writes preview unless `commit=True`. A read that fails raises ForgeError: an empty answer would read as an
empty forge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import backlog
import forge_review
import kanban
import merge_approved
import merge_ready
import review_threads
from backlog_ref import Backlog, GitHubBacklog, IssueRef, backlog_from_config, home_of
from forge_port import ForgeError
from settings import Kanban


@dataclass(frozen=True)
class Forge:
    home: Backlog | GitHubBacklog
    approver: str = ""                 # the user's forge username: whose approval and review threads count
    kanban: Kanban | None = None       # the stage labels / Project Status; None leaves stage unread and unwritten
    gh: Callable = backlog.run_gh
    token: str | None = None           # GitLab only; None looks it up as backlog.token does

    @property
    def github(self) -> bool:
        return isinstance(self.home, GitHubBacklog)

    def key(self, number: int) -> str:
        """The store's issue key: `github:owner/repo#12`, `gitlab:host/group/project#5`."""
        host, repo = home_of(self.home)
        return f"github:{repo}#{number}" if self.github else f"gitlab:{host}/{repo}#{number}"

    def _token(self) -> str:
        return self.token if self.token is not None else backlog.token(self.home)

    # reads
    def issues(self, state: str = backlog.OPEN, *, limit: int | None = None) -> backlog.Fetch:
        return backlog.issues(self.home, token=self.token, state=state, gh=self.gh, limit=limit)

    def issue(self, number: int) -> backlog.Read:
        return backlog.read_one(self.home, number, token=self.token, gh=self.gh)

    def requests(self) -> list[merge_ready.Request]:
        """Open pull or merge requests with their merge verdict (#419) and the approver's approval."""
        try:
            return (merge_ready.github_ready(self.home.repo, self.gh, self.approver) if self.github else
                    merge_ready.gitlab_ready(self.home, self.home.project, self._token(), backlog._call, self.approver))
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ForgeError(f"requests: {exc}") from exc

    def _merged_closes(self) -> set[int]:
        """Issue numbers closed by a recently merged request (bounded by REQUEST_LIMIT)."""
        try:
            if self.github:
                code, out, err = self.gh(
                    ["pr", "list", "-R", self.home.repo, "--state", "merged",
                     "--search", "sort:updated-desc", "--limit", str(forge_review.REQUEST_LIMIT), "--json",
                     "number,body,closingIssuesReferences"])
                if code:
                    raise ForgeError(f"merged requests: {err.strip() or code}")
                rows = json.loads(out)
            else:
                listed, _, err = backlog._call(
                    "GET",
                    f"{self.home.api(self.home.project)}/merge_requests?"
                    + urlencode({"state": "merged", "order_by": "updated_at", "sort": "desc",
                                 "per_page": forge_review.REQUEST_LIMIT}),
                    self._token(), backlog.TIMEOUT)
                if err:
                    raise ForgeError(f"merged requests: {err}")
                rows = listed
            # shortcut: only the REQUEST_LIMIT most recently updated merged requests annotate closed issues (merging
            # updates a request; later edits can shift the window). Other closed issues keep their terminal stage mark
            # and read merge_state "". Page the list if a rebuild needs every merge.
            if not isinstance(rows, list):
                raise TypeError(f"expected a list, got {type(rows).__name__}")
            closed: set[int] = set()
            for row in rows:  # inside the guard: a malformed row is a forge read error, not a crash
                refs = (forge_review.github_closes(row, self.home.repo) if self.github else
                        forge_review.closes(row.get("description"), gitlab=True, project=self.home.project, host=self.home.host))
                for ref in refs:
                    closed.add(int(ref[1:]))
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ForgeError(f"merged requests: {exc}") from exc
        return closed

    def closing_request(self, issue_key: str, requests: list[merge_ready.Request] | None = None) -> merge_ready.Request:
        """The newest open request closing the issue, as snapshots report for rework."""
        found = self.requests() if requests is None else requests
        closing = [r for r in found if any(self.key(int(ref[1:])) == issue_key for ref in r.closes)]
        if not closing:
            raise ForgeError(f"no open request closes {issue_key}")
        return max(closing, key=lambda r: r.number)

    def threads(self) -> list[review_threads.Thread]:
        """The user's unresolved review threads: fix work for the finding burn-down."""
        if not self.approver:
            raise ForgeError("review threads need the approver, the user's forge username")
        try:
            if self.github:
                return review_threads.github_threads(self.home.repo, self.approver, self.gh)
            return review_threads.gitlab_threads(self.home, self.home.project, self.approver, self._token(), backlog._call)
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ForgeError(f"review threads: {exc}") from exc

    def snapshots(self) -> list[dict[str, Any]]:
        """One `store.Store.sync_from_forge` row per open or recently updated closed issue. Stage and human hold are left out
        without kanban, and review state without an approver, so a sync never blanks what was not read. Recent completed
        work is included so a dropped store can rebuild it (#627)."""
        got = self.issues()
        if not got.ok:
            raise ForgeError(f"issues: {got.error}")
        # shortcut: only the REQUEST_LIMIT most recently updated closed issues join the open operational set,
        # matching _merged_closes's bounded window. Page closed history if a rebuild needs every completed issue.
        closed = self.issues(state=backlog.CLOSED, limit=forge_review.REQUEST_LIMIT)
        if not closed.ok:
            raise ForgeError(f"closed issues: {closed.error}")
        latest: dict[int, merge_ready.Request] = {}
        # shortcut: several requests closing one issue (rework) report the newest; key by (issue, attempt) when M3 segments need each
        for r in sorted(self.requests(), key=lambda r: r.number):
            for ref in r.closes:
                latest[int(ref[1:])] = r
        merged = self._merged_closes()
        statuses: dict[str, str | None] = {}
        if self.kanban is not None and self.kanban.github_project is not None and self.github:
            statuses, error = kanban._project_items(self.gh, self.kanban.github_project)
            if error:
                raise ForgeError(f"project status: {error}")
        out = []
        for one in sorted((*got.issues, *closed.issues), key=lambda i: i.iid):
            r = latest.get(one.iid)
            snap = {"issue_key": self.key(one.iid), "title": one.title,
                    "forge_state": "open" if one.state == backlog.OPEN else "closed"}
            if self.kanban is not None:
                snap["stage"] = self._stage(one, statuses if self.kanban.github_project and self.github else None)
                snap["human_review"] = self.kanban.human_review_label in one.labels
            if self.approver:
                snap["review_state"] = r.approval if r else ""
            if one.state == backlog.CLOSED and one.iid in merged:  # an open follow-up does not undo a merge
                snap["merge_state"] = "merged"
            elif r:
                snap["merge_state"] = r.verdict
            else:
                snap["merge_state"] = ""
            out.append(snap)
        return out

    def _stage(self, one: backlog.Issue, statuses: dict[str, str | None] | None = None) -> str:
        assert self.kanban is not None
        try:
            if self.kanban.github_project is not None and self.github and statuses is not None:
                url = IssueRef(self.home.repo, one.iid).url
                status = statuses.get(url.casefold())
                if status is not None:
                    return self.kanban.stage_for_status(status)
                # Existing workspaces recorded their stages as labels before Project Status was honored.
            return self.kanban.stage_for_label(one.labels)
        except ValueError as exc:
            raise ForgeError(f"stage: {exc}") from exc

    # writes: preview unless commit
    def merge(self, number: int, commit: bool = False, issue_key: str | None = None) -> str:
        """Re-read readiness and the issue's closing request, then preview or report the verified head's merge outcome."""
        mark = "#" if self.github else "!"
        found = self.requests()
        one = next((r for r in found if r.number == number), None)
        if one is None:
            raise ForgeError(f"{mark}{number} is not an open request")
        if issue_key is not None:
            selected = self.closing_request(issue_key, found)
            if selected.number != number:
                raise ForgeError(f"{mark}{number} is no longer the newest request closing {issue_key}: {mark}{selected.number}")
        if one.verdict != "ready":
            raise ForgeError(f"not merged: {merge_ready.line(one, mark)}")
        evidence = f"{mark}{number} at {one.head} · pipeline {one.pipeline_id or '-'} on {one.pipeline_sha} · {one.url}"
        if not commit:
            return f"would merge {evidence}"
        _, repo = home_of(self.home)
        try:
            code, result = merge_approved.merge_head(one, self.home, self.github, repo, gh=self.gh,
                                                    call=backlog._call, token=None if self.github else self._token())
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ForgeError(f"{mark}{number} not merged at {one.head}: {exc}") from exc
        if code:
            raise ForgeError(f"{mark}{number} not merged at {one.head}: {result or f'merge exited {code}'}")
        outcome, _, error = result.partition(": ")
        return f"{outcome} {evidence}" + (f": {error}" if error else "")

    def create_issue(self, title: str, body: str = "", labels: tuple[str, ...] = (), parent: int | None = None,
                     blocked_by: tuple[int, ...] = (), commit: bool = False) -> backlog.Written:
        return backlog.create(self.home, title, body=body, labels=labels, token=self.token, commit=commit, gh=self.gh,
                              parent=parent, blocked_by=blocked_by)

    def write_stage(self, number: int, stage: str, expected: str | None = None, commit: bool = False) -> kanban.Sync:
        """Move the issue's stage (label, or GitHub Project Status when configured), leaving its other labels.
        `expected` is the stage the store last wrote; with none, initialize a blank issue or migrate its legacy label
        into Project Status. A card moved by hand is reported, never overwritten."""
        host, repo = home_of(self.home)
        ref = IssueRef(repo, number, host)
        if self.kanban is None:
            return kanban.Sync(ref, stage, error="no [kanban] stage labels configured")
        if self.github:
            return kanban.sync_github(ref, self.kanban, stage, gh=self.gh, commit=commit, expected_stage=expected,
                                      initialize=expected is None)
        return kanban.sync_gitlab(self.home, ref, self.kanban, stage, token=self.token, commit=commit, expected_stage=expected,
                                  initialize=expected is None)


def for_workspace(root: Path | str, **options: Any) -> Forge | None:
    """The forge the workspace's `Backlog:` line names, or None when it has none."""
    config = Path(root) / "CLAUDE.md"
    home = backlog_from_config(config) if config.is_file() else None
    return Forge(home, **options) if home else None
