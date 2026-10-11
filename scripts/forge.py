"""The one forge adapter (1.0 plan, M2): a workspace's GitHub or GitLab, read and written through one object.

The forge is the source of truth and the store a cache rebuilt from `snapshots`. It reads issues (backlog.py), open pull or merge
requests with their CI and review state (merge_ready.py over forge_review.py), and the user's review threads (review_threads.py);
it creates issues (backlog.py) and writes stage back as labels (kanban.py). Writes preview unless `commit=True`. A read that fails
raises ForgeError: an empty answer would read as an empty forge.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import backlog
import forge_review
import kanban
import merge_ready
import review_threads
from backlog_ref import Backlog, GitHubBacklog, IssueRef, backlog_from_config, home_of
from settings import Kanban


class ForgeError(RuntimeError):
    """The forge did not answer in full, so nothing it said can stand for the whole."""


@dataclass(frozen=True)
class Forge:
    home: Backlog | GitHubBacklog
    approver: str = ""                 # the user's forge username: whose approval and review threads count
    kanban: Kanban | None = None       # the stage labels; None leaves stage unread and unwritten
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
    def issues(self, state: str = backlog.OPEN) -> backlog.Fetch:
        return backlog.issues(self.home, token=self.token, state=state, gh=self.gh)

    def issue(self, number: int) -> backlog.Read:
        return backlog.read_one(self.home, number, token=self.token, gh=self.gh)

    def requests(self) -> list[merge_ready.Request]:
        """Open pull or merge requests with their merge verdict (#419) and the approver's approval."""
        try:
            got = (merge_ready.github_ready(self.home.repo, self.gh, self.approver) if self.github else
                   merge_ready.gitlab_ready(self.home, self.home.project, self._token(), backlog._call, self.approver))
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ForgeError(f"requests: {exc}") from exc
        if len(got) >= forge_review.REQUEST_LIMIT:
            # shortcut: refuse rather than page; page the list and the graphql read when a repo keeps this many open
            raise ForgeError(f"requests: {forge_review.REQUEST_LIMIT} open or more; refusing to read a partial list as whole")
        return got

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
        """One `store.Store.sync_from_forge` row per open issue. Stage is left out without stage labels, and review state
        without an approver, so a sync never blanks what was not read."""
        got = self.issues()
        if not got.ok:
            raise ForgeError(f"issues: {got.error}")
        latest: dict[int, merge_ready.Request] = {}
        # shortcut: several requests closing one issue (rework) report the newest; key by (issue, attempt) when M3 segments need each
        for r in sorted(self.requests(), key=lambda r: r.number):
            for ref in r.closes:
                latest[int(ref[1:])] = r
        out = []
        for one in sorted(got.issues, key=lambda i: i.iid):
            r = latest.get(one.iid)
            snap = {"issue_key": self.key(one.iid), "title": one.title,
                    "forge_state": "open" if one.state == backlog.OPEN else "closed"}
            if self.kanban is not None:
                snap["stage"] = self._stage(one.labels)
            if self.approver:
                snap["review_state"] = r.approval if r else ""
            snap["merge_state"] = r.verdict if r else ""
            out.append(snap)
        return out

    def _stage(self, labels: tuple[str, ...]) -> str:
        # shortcut: a label several stages share reads back as the first of them; give each stage its own label for an exact rebuild
        return next((stage for stage in self.kanban.stage_map if self.kanban.label_for(stage) in labels), "")

    # writes: preview unless commit
    def create_issue(self, title: str, body: str = "", labels: tuple[str, ...] = (), parent: int | None = None,
                     blocked_by: tuple[int, ...] = (), commit: bool = False) -> backlog.Written:
        return backlog.create(self.home, title, body=body, labels=labels, token=self.token, commit=commit, gh=self.gh,
                              parent=parent, blocked_by=blocked_by)

    def write_stage(self, number: int, stage: str, expected: str | None = None, commit: bool = False) -> kanban.Sync:
        """Move the issue's stage label to `stage`, leaving its other labels. `expected` is the stage the store last wrote; with
        none, only an issue carrying no stage label is written. A label moved by hand is reported, never overwritten."""
        host, repo = home_of(self.home)
        ref = IssueRef(repo, number, host)
        if self.kanban is None:
            return kanban.Sync(ref, stage, error="no [kanban] stage labels configured")
        # Labels, not Project Status: both forges have them (design decision 1 leaves the choice open).
        labels = dataclasses.replace(self.kanban, github_project=None)
        if self.github:
            return kanban.sync_github(ref, labels, stage, gh=self.gh, commit=commit, expected_stage=expected,
                                      initialize=expected is None)
        return kanban.sync_gitlab(self.home, ref, labels, stage, token=self.token, commit=commit, expected_stage=expected,
                                  initialize=expected is None)


def for_workspace(root: Path | str, **options: Any) -> Forge | None:
    """The forge the workspace's `Backlog:` line names, or None when it has none."""
    config = Path(root) / "CLAUDE.md"
    home = backlog_from_config(config) if config.is_file() else None
    return Forge(home, **options) if home else None
