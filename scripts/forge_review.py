"""How a forge's review state reads: whether the approver approved a pull or merge request's head, and how its
pipeline stands, and the request that reading yields. merge_approved.py merges on it, merge_ready.py prints its
verdict, review_threads.py replies on it, and board_sources.py caches it for the pages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import backlog
import backlog_ref
from settings import Workflow, load as load_settings
from workspace import ConfigError, read_config

PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}  # a completed check that concluded anything else (STALE, "") is not a pass
GITLAB_PIPELINE = {"success": "passed", "failed": "failed", "canceled": "failed"}
# Not-ready forge statuses keep their own word so the line can name them (#425); anything else unknown is running.
# skipped ran no jobs: nothing passed, nothing failed.
GITLAB_WAITING = frozenset({"skipped", "pending", "created", "waiting_for_resource", "waiting_for_callback", "preparing",
                            "scheduled", "manual", "draft", "canceling"})
# Forge mergeability still being computed — not a settled blocker (#430).
SETTLING = frozenset({"checking", "unchecked", "ci_still_running", "unknown", "approvals_syncing", "preparing"})
REQUEST_LIMIT = backlog.PER_PAGE  # one page; readers page until short, and refuse a full MAX_PAGES read as whole
CLOSES = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(#\d+)")


def gitlab_pipeline_status(status: str | None) -> str:
    if status is None:
        return "none"
    if status in GITLAB_PIPELINE:
        return GITLAB_PIPELINE[status]
    return status if status in GITLAB_WAITING else "running"


def scrub(text: str) -> str:
    """Forge-supplied text with control characters removed so a hostile value cannot forge a printed line (#426)."""
    return "".join(c for c in text if c.isprintable())


def closes(body: str | None) -> tuple[str, ...]:
    """The issue refs a request's body closes (`Closes #3`), in order, once each."""
    return tuple(dict.fromkeys(CLOSES.findall(body or "")))


def github_approval(reviews: list[dict], approver: str, head: str) -> str:
    # A comment neither grants nor withdraws; the approver's latest approve or request-changes decides.
    mine = sorted((r for r in reviews if (r.get("author") or {}).get("login") == approver
                   and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")),
                  key=lambda r: r.get("submittedAt") or "")
    if not mine or mine[-1]["state"] != "APPROVED":
        return "changes_requested" if mine and mine[-1]["state"] == "CHANGES_REQUESTED" else "none"
    return "approved" if (mine[-1].get("commit") or {}).get("oid") == head else "stale"


def github_pipeline(checks: list[dict]) -> str:
    if not checks:
        return "none"
    def running(c: dict) -> bool:
        return (c.get("status") != "COMPLETED") if c.get("__typename") == "CheckRun" else c.get("state") in ("PENDING", "EXPECTED")
    if any(not running(c) and (c.get("conclusion") or c.get("state") or "").upper() not in PASSED for c in checks):
        return "failed"
    return "running" if any(running(c) for c in checks) else "passed"


def forge(root: Path, repo: str | None) -> tuple[Workflow, backlog_ref.Backlog, bool, str]:
    """The workspace's workflow settings, its forge, whether that is GitHub, and the repo to act on."""
    cfg = read_config(root)
    workflow = load_settings(root, cfg.settings_path).workflow
    home = cfg.backlog
    if home is None:
        raise ConfigError("no Backlog: line names the forge")
    github = isinstance(home, backlog_ref.GitHubBacklog)
    return workflow, home, github, repo or (home.repo if github else home.project)


NO_PIPELINE = "no pipeline"


@dataclass(frozen=True)
class Request:
    number: int
    title: str
    url: str
    head: str
    approval: str   # approved | stale | changes_requested | none
    pipeline: str   # passed | failed | running | none
    mergeable: str  # yes | conflict | the forge's own word
    approver: str = ""
    pipeline_id: str = ""   # the head pipeline's id; "" when none or unread
    pipeline_sha: str | None = None  # the commit that pipeline ran on; None when unread, which is never "not stale"
    threads: int | None = None       # unresolved review threads; None when unread, which is never zero
    threads_cut_at: int = 0          # the count read when more threads lay behind it (a cut-off read is not a whole one)
    draft: bool = False
    merge_state: str = ""            # GitHub's mergeStateStatus; "" (GitLab) has none
    closes: tuple[str, ...] = ()     # the issue refs its body closes, e.g. ("#3",)

    @property
    def unverified(self) -> str:
        """Why the pipeline's sha or the thread count cannot be trusted; "" when both were read in full."""
        if self.pipeline_sha is None:
            return "pipeline sha unread"
        if self.threads is None:
            return "threads unread"
        return f"threads cut off at {self.threads_cut_at}" if self.threads_cut_at else ""

    @property
    def verdict(self) -> str:
        """The one place that decides ready (#419), from the head pipeline and never the forge's own merge word.
        Precedence: conflicts > draft > red > stale pipeline > running > no pipeline > unverified > findings > blocked > ready."""
        if self.mergeable == "conflict":
            return "conflicts"
        if self.draft:
            return "draft"
        if self.pipeline == "failed":
            return "red"
        if self.pipeline != "none" and self.pipeline_sha is not None and self.pipeline_sha != self.head:
            return "stale pipeline"
        if self.pipeline not in ("passed", "none"):
            return "running"
        if self.mergeable in SETTLING:
            return "running"
        if self.pipeline == "none":
            return NO_PIPELINE
        if self.unverified:
            return "unverified"
        if self.threads:
            return "findings open"
        if self.merge_state not in ("", "CLEAN"):
            return "blocked"
        if self.mergeable != "yes":
            return "blocked"
        return "ready"

    @property
    def why(self) -> str:
        """The one clause the line adds to `unverified`, `blocked`, and named waiting pipelines."""
        if self.verdict == "unverified":
            return self.unverified
        if self.verdict == "blocked":
            return self.merge_state if self.merge_state not in ("", "CLEAN") else self.mergeable
        if self.verdict == "running" and self.pipeline not in ("running", "passed", "failed", "none"):
            return self.pipeline
        if self.verdict == "running" and self.mergeable in SETTLING:
            return self.mergeable
        return ""

    @property
    def blockers(self) -> list[str]:
        out = {"none": [f"no approval from {self.approver}"],
               "changes_requested": [f"no approval from {self.approver}"],
               "stale": [f"{self.approver} approved an earlier commit"]}.get(self.approval, [])
        if self.pipeline not in ("passed", "none"):
            out.append(f"pipeline {self.pipeline}")
        if self.mergeable != "yes":
            out.append("conflict" if self.mergeable == "conflict" else f"not mergeable: {self.mergeable}")
        if self.draft:
            out.append("draft")
        if self.merge_state not in ("", "CLEAN"):
            out.append(f"not mergeable: {self.merge_state}")
        for verdict, why in (("stale pipeline", "pipeline ran on an earlier commit"), (NO_PIPELINE, NO_PIPELINE),
                             ("findings open", f"{self.threads} unresolved threads")):
            if self.verdict == verdict:
                out.append(why)
        return out


def github_requests(repo: str, approver: str, gh=backlog.run_gh) -> list[Request]:
    # gh paginates under --limit; refuse a full GH_LIMIT answer as a partial list (#425).
    code, out, err = gh(["pr", "list", "-R", repo, "--state", "open", "--limit", str(backlog.GH_LIMIT), "--json",
                         "number,title,url,headRefOid,reviews,statusCheckRollup,mergeable,isDraft,mergeStateStatus,body"])
    if code != 0:
        raise RuntimeError(f"gh pr list: {err.strip() or code}")
    rows = json.loads(out)
    if len(rows) >= backlog.GH_LIMIT:
        raise RuntimeError(f"{backlog.GH_LIMIT} open or more; refusing to read a partial list as whole")
    return [Request(p["number"], p["title"], p["url"], p["headRefOid"],
                    github_approval(p.get("reviews") or [], approver, p["headRefOid"]),
                    github_pipeline(p.get("statusCheckRollup") or []),
                    {"MERGEABLE": "yes", "CONFLICTING": "conflict"}.get(p.get("mergeable"), "unknown"), approver,
                    draft=bool(p.get("isDraft")), merge_state=p.get("mergeStateStatus") or "UNKNOWN",
                    closes=closes(p.get("body")))
            for p in rows]


def gitlab_requests(home: backlog.Backlog, project: str, approver: str, token: str, call=backlog._call) -> list[Request]:
    # ponytail: GitLab's approvals API names no commit, so an approval counts for the head only when the
    # project resets approvals on push. Check that project setting once; a per-note sha check if it is off.
    base = home.api(project)
    listed: list[dict] = []
    for page in range(1, backlog.MAX_PAGES + 1):
        batch, _, err = call("GET", f"{base}/merge_requests?" + urlencode(
            {"state": "opened", "per_page": backlog.PER_PAGE, "page": page}), token, backlog.TIMEOUT)
        if err:
            raise RuntimeError(f"merge requests: {err}")
        listed.extend(batch)
        if len(batch) < backlog.PER_PAGE:
            break
    else:
        raise RuntimeError(f"more than {backlog.MAX_PAGES} pages of merge requests; refusing to read a partial list as whole")
    out = []
    for row in listed:
        iid = int(row["iid"])
        mr, _, err = call("GET", f"{base}/merge_requests/{iid}", token, backlog.TIMEOUT)
        approvals, _, err2 = call("GET", f"{base}/merge_requests/{iid}/approvals", token, backlog.TIMEOUT)
        if err or err2:
            raise RuntimeError(f"!{iid}: {err or err2}")
        names = {(a.get("user") or {}).get("username") for a in approvals.get("approved_by") or []}
        head = mr.get("head_pipeline") or {}
        status = mr.get("detailed_merge_status") or ""
        head_sha = mr.get("sha", "")
        # Merged-results pipelines run on a temporary merge ref; opened MRs have no merge_commit_sha (#430).
        pipe_sha = (head.get("sha") or None) if head else ""
        if pipe_sha and head.get("ref") == f"refs/merge-requests/{iid}/merge":
            pipe_sha = head_sha
        out.append(Request(iid, mr.get("title", ""), mr.get("web_url", ""), head_sha,
                           "approved" if approver in names else "none",
                           gitlab_pipeline_status(None if not head else head.get("status")),
                           "conflict" if mr.get("has_conflicts") else ("yes" if status == "mergeable" else status),
                           approver, str(head.get("id") or ""), pipe_sha,
                           draft=bool(mr.get("draft")), closes=closes(mr.get("description"))))
    return out
