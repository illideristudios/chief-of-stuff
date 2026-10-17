"""The one forge adapter (1.0 plan, M2): a workspace's GitHub or GitLab, read and written through one object.

The forge is the source of truth and the store a cache rebuilt from `snapshots`. It reads issues (backlog.py), open pull or merge
requests with their CI and review state (merge_ready.py over forge_review.py), and the user's review threads (review_threads.py);
it creates issues (backlog.py), writes stage back as labels or a GitHub Project Status (kanban.py), and merges verified heads
(merge_approved.py). Writes preview unless `commit=True`. A read that fails raises ForgeError: an empty answer would read as an
empty forge. Pipeline and branch-head reads also use this adapter.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode

import backlog
import forge_review
import kanban
import merge_approved
import merge_ready
import review_threads
from backlog_ref import Backlog, GitHubBacklog, IssueRef, backlog_from_config, home_of
from forge_port import ForgeError
from settings import Kanban

_LOCAL_CLOSE = re.compile(r"#\d+")  # a closing ref for this repository; anything else is qualified or foreign


def _gh_json(gh, args: list[str]):
    code, stdout, stderr = gh(args)
    if code:
        return None, stderr.strip() or f"gh exited {code}"
    try:
        return json.loads(stdout), ""
    except ValueError:
        return None, "gh did not answer with JSON"


def _github_issue(gh, ref: backlog.IssueRef):
    row, error = _gh_json(gh, ["issue", "view", str(ref.number), "-R", ref.repo,
                               "--json", "labels,state,url"])
    if error:
        return (), error
    if not isinstance(row, dict) or not isinstance(row.get("labels"), list):
        return (), "GitHub issue has no readable labels"
    return tuple(str(x["name"]) for x in row["labels"] if isinstance(x, dict) and "name" in x), ""


def _project_rows(gh, project):
    # item-list serializes values using at most 100 field definitions, even with a larger item limit.
    fields, error = _project_fields(gh, project)
    if error:
        return {}, error
    # item-list omits an unknown field's key, which would read as every item having no stage.
    if not any(isinstance(f, dict) and f.get("name") == project.field for f in fields):
        return {}, f"GitHub Project has no field named {project.field}"
    row, error = _gh_json(gh, ["project", "item-list", str(project.number), "--owner", project.owner,
                               "--format", "json", "--limit", "5000"])
    if error:
        return {}, error
    if not isinstance(row, dict) or not isinstance(row.get("items"), list):
        return {}, "GitHub Project items could not be parsed"
    # totalCount says whether a full page is the whole Project. shortcut: a Project over 5,000 items is refused, not
    # paged; query the issue's projectItems instead when one exists (#662).
    total = row.get("totalCount")
    if total > len(row["items"]) if isinstance(total, int) else len(row["items"]) >= 5000:
        return {}, "GitHub Project item list was truncated"
    items = {}
    for item in row["items"]:
        if isinstance(item, dict) and isinstance(item.get("content"), dict):
            url = item["content"].get("url")
            if isinstance(url, str):
                items[url.casefold()] = item
    return items, ""


def _project_status(item, project):
    # gh's JSON serializer lowercases the first character of the field name.
    key = project.field[:1].lower() + project.field[1:]
    value = item.get(key, item.get(project.field.lower()))
    return str(value) if value is not None else None


def project_items(gh, project):
    """URL → Status option for every issue item in the Project (empty Status is None)."""
    items, error = _project_rows(gh, project)
    return {url: _project_status(item, project) for url, item in items.items()}, error


def _project_item(gh, project, ref: backlog.IssueRef):
    items, error = _project_rows(gh, project)
    item = items.get(ref.url.casefold())
    return item, _project_status(item, project) if item is not None else None, error


def _node_id(row):
    value = row.get("id") if isinstance(row, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def _project_fields(gh, project):
    row, error = _gh_json(gh, ["project", "field-list", str(project.number), "--owner", project.owner,
                               "--format", "json", "--limit", "100"])
    if error:
        return [], error
    fields = row.get("fields") if isinstance(row, dict) else None
    if not isinstance(fields, list):
        return [], "GitHub Project fields could not be parsed"
    total = row.get("totalCount")  # as for items: a full page is whole only when totalCount says so
    if total > len(fields) if isinstance(total, int) else len(fields) >= 100:
        return [], "GitHub Project field list was truncated"
    return fields, ""


def project_option_ids(gh, project, value: str | None):
    fields, error = _project_fields(gh, project)
    if error:
        return None, None, error
    for field in fields:
        if isinstance(field, dict) and field.get("name") == project.field:
            field_id = _node_id(field)
            if field_id is None:
                return None, None, "GitHub Project field has no readable node ID"
            if value is None:
                return field_id, None, ""
            options = field.get("options")
            if isinstance(options, list):
                for option in options:
                    if isinstance(option, dict) and option.get("name") == value:
                        option_id = _node_id(option)
                        if option_id is None:
                            return None, None, "GitHub Project option has no readable node ID"
                        return field_id, option_id, ""
            return None, None, f"missing GitHub Project option: {value}; create the {project.field} option {value} before syncing"
    return None, None, f"missing GitHub Project field: {project.field}"


def _github_labels_exist(gh, ref: backlog.IssueRef, names: tuple[str, ...]) -> str:
    if not names:
        return ""
    row, error = _gh_json(gh, ["label", "list", "-R", ref.repo, "--json", "name", "--limit", "5000"])
    if error:
        return error
    if not isinstance(row, list):
        return "GitHub labels could not be parsed"
    have = {x["name"].casefold() for x in row if isinstance(x, dict) and isinstance(x.get("name"), str)}
    missing = {name for name in names if name.casefold() not in have}
    return (f"missing label in GitHub: {', '.join(sorted(missing))}; "
            f"create the label(s) {', '.join(sorted(missing))} before syncing") if missing else ""


def _sync_github(ref: backlog.IssueRef, config: Kanban, stage: str, *, gh=None,
                commit: bool = False, expected_stage: str | None = None,
                initialize: bool = False) -> kanban.Sync:
    """Update issue labels, or a GitHub Project Status plus the issue's hold label."""
    if ref.host != backlog.GITHUB:
        return kanban.Sync(ref, stage, error="issue is not on GitHub")
    try:
        config.label_for(stage)
    except KeyError:
        return kanban.Sync(ref, stage, error=f"unmapped tracker stage: {stage}")
    if expected_stage is not None:
        try:
            config.label_for(expected_stage)
        except KeyError:
            return kanban.Sync(ref, stage, error=f"unmapped prior stage: {expected_stage}")
    gh = gh or backlog.run_gh
    current, error = _github_issue(gh, ref)
    if error:
        return kanban.Sync(ref, stage, error=error)
    current = kanban._canonical(config, current)
    delta = kanban._github_label_delta(config, current, stage, keep_hold=kanban.extra_hold(config, current, expected_stage))
    managed = kanban._managed(config)
    current_labels = frozenset(current) & managed
    desired_labels = set(kanban.after(current, delta)) & managed
    prior_labels = frozenset(kanban._github_label_delta(config, (), expected_stage).add) if expected_stage else frozenset()
    hold = frozenset({config.human_review_label})
    project = config.github_project
    legacy = False
    present, old_status, wanted = False, None, None
    item_id, field_id, option_id, project_id = None, None, None, None
    if project:
        item, old_status, error = _project_item(gh, project, ref)
        if error:
            return kanban.Sync(ref, stage, delta, error=error)
        present = item is not None
        item_id = _node_id(item)
        # Forge used stage labels before it honored github_project. Migrate only a single stage mark.
        single_mark = len(current_labels & (set(config.stages) | set(config.terminal_stages))) == 1
        legacy = old_status is None and single_mark
        wanted = config.status_for(stage)
        if wanted:
            field_id, option_id, error = project_option_ids(gh, project, wanted)
            if error:
                return kanban.Sync(ref, stage, delta, error=error)
    project_add = bool(project and not present and wanted is not None)
    changed = delta.changed or (project is not None and (project_add or (present and old_status != wanted)))
    result = kanban.Sync(ref, stage, delta, current_status=old_status, project_status=wanted,
                  project_add=project_add)
    if not changed:
        return kanban.Sync(ref, stage, delta, done=True, current_status=old_status, project_status=wanted)
    if not commit:
        return result
    if project:
        prior_status = config.status_for(expected_stage) if expected_stage else None
        current_state = (old_status if present else None, frozenset(current_labels))
        desired_state = (wanted, frozenset(desired_labels))
        prior_state = (prior_status, frozenset(prior_labels))
        # Status is written before labels when leaving a hold or migrating; a retry after a failed label edit
        # finds that half-written state.
        priors = (prior_state, (prior_status, prior_labels | hold), (wanted, prior_labels), (wanted, prior_labels | hold))
        blanks = ((None, frozenset()), (None, hold))
        if legacy or (single_mark and present and old_status == wanted):
            legacy_prior = frozenset(kanban.label_delta(config, (), expected_stage).add) if expected_stage else frozenset()
            priors += tuple((status, labels) for status in (None, wanted) for labels in (legacy_prior, legacy_prior | hold))
            if expected_stage is None:
                blanks += (current_state,)
        error = kanban._guard(current_state, desired_state, priors,
                       expected_stage=expected_stage, initialize=initialize, blanks=blanks)
    else:
        error = kanban._guard(current_labels, desired_labels, (prior_labels, prior_labels | hold),
                       expected_stage=expected_stage, initialize=initialize, blanks=(frozenset(), hold))
    if error:
        return kanban.Sync(ref, stage, delta, error=error, current_status=old_status, project_status=wanted)
    error = _github_labels_exist(gh, ref, delta.add)
    if error:
        return kanban.Sync(ref, stage, delta, error=error, current_status=old_status, project_status=wanted)

    project_changed = project is not None and (project_add or (present and old_status != wanted))
    if project_changed:
        if present and item_id is None:
            return kanban.Sync(ref, stage, delta, error="GitHub Project item has no readable node ID")
        if field_id is None:
            field_id, option_id, error = project_option_ids(gh, project, wanted)
            if error:
                return kanban.Sync(ref, stage, delta, error=error)
        row, error = _gh_json(gh, ["project", "view", str(project.number), "--owner", project.owner,
                                  "--format", "json"])
        project_id = _node_id(row)
        if error or project_id is None:
            return kanban.Sync(ref, stage, delta, error=error or "GitHub Project has no readable node ID")

    def edit_labels():
        if not delta.changed:
            return ""
        cmd = ["issue", "edit", str(ref.number), "-R", ref.repo]
        if delta.add:
            cmd += ["--add-label", ",".join(delta.add)]
        if delta.remove:
            cmd += ["--remove-label", ",".join(delta.remove)]
        code, _, stderr = gh(cmd)
        return stderr.strip() or f"gh exited {code}" if code else ""

    def edit_project():
        if not project_changed:
            return ""
        edit_id = item_id
        if not present:
            row, error = _gh_json(gh, ["project", "item-add", str(project.number), "--owner", project.owner,
                                      "--url", ref.url, "--format", "json"])
            edit_id = _node_id(row)
            if error or edit_id is None:
                return error or "added GitHub Project item has no readable node ID"
        cmd = ["project", "item-edit", "--id", edit_id, "--project-id", project_id,
               "--field-id", field_id]
        cmd += ["--single-select-option-id", option_id] if wanted is not None else ["--clear"]
        code, _, stderr = gh(cmd)
        return stderr.strip() or f"gh exited {code}" if code else ""

    # Enter a hold before moving the card. During migration, retain the legacy stage until Status is written.
    # Leave a hold only after the destination is set.
    steps = (edit_labels, edit_project) if config.holds(stage) and not legacy else (edit_project, edit_labels)
    for step in steps:
        error = step()
        if error:
            return kanban.Sync(ref, stage, delta, error=error, current_status=old_status,
                        project_status=wanted, project_add=project_add)
    verified_labels, error = _github_issue(gh, ref)
    if error or set(kanban._canonical(config, verified_labels)) != set(kanban.after(current, delta)):
        return kanban.Sync(ref, stage, delta, error=error or "GitHub labels differ after update")
    if project:
        item, status, error = _project_item(gh, project, ref)
        present = item is not None
        if error or (wanted is not None and not present) or (present and status != wanted):
            return kanban.Sync(ref, stage, delta, error=error or "GitHub Project status differs after update")
    return kanban.Sync(ref, stage, delta, done=True, current_status=old_status,
                project_status=wanted, project_add=project_add)


def _sync_gitlab(cfg: backlog.Backlog, ref: backlog.IssueRef, config: Kanban, stage: str,
                 *, token: str | None = None, commit: bool = False,
                 expected_stage: str | None = None, initialize: bool = False,
                 timeout: float = backlog.TIMEOUT) -> kanban.Sync:
    """GitLab's add/remove update leaves unrelated issue labels untouched."""
    if ref.host != backlog.home_of(cfg)[0]:
        return kanban.Sync(ref, stage, error="issue host does not match the configured GitLab host")
    try:
        config.label_for(stage)
    except KeyError:
        return kanban.Sync(ref, stage, error=f"unmapped tracker stage: {stage}")
    if expected_stage is not None:
        try:
            config.label_for(expected_stage)
        except KeyError:
            return kanban.Sync(ref, stage, error=f"unmapped prior stage: {expected_stage}")
    project = cfg if cfg.project == ref.repo else cfg.sibling(ref.repo)
    secret = token if token is not None else backlog.token(project)
    if not secret:
        return kanban.Sync(ref, stage, error=cfg.missing_token)
    url = f"{project.issues_url}/{ref.number}"
    row, _, error = backlog._get(url, secret, timeout)
    if error:
        return kanban.Sync(ref, stage, error=error)
    if not isinstance(row, dict) or not isinstance(row.get("labels"), list):
        return kanban.Sync(ref, stage, error="GitLab issue has no readable labels")
    current = tuple(str(label) for label in row["labels"])
    held = kanban.extra_hold(config, current, expected_stage)
    delta = kanban.label_delta(config, current, stage, keep_hold=held)
    managed = kanban._managed(config)
    prior = frozenset(kanban.label_delta(config, (), expected_stage).add) if expected_stage else frozenset()
    hold = frozenset({config.human_review_label})
    desired = set(kanban.after(current, delta)) & managed
    if commit:
        error = kanban._guard(frozenset(current) & managed, desired, (prior, prior | hold),
                              expected_stage=expected_stage, initialize=initialize, blanks=(frozenset(), hold))
        if error:
            return kanban.Sync(ref, stage, delta, error=error)
    if not delta.changed:
        return kanban.Sync(ref, stage, delta, done=True)
    if not commit:
        return kanban.Sync(ref, stage, delta)
    have, error = backlog.existing_labels(project, token=secret, timeout=timeout)
    if error:
        return kanban.Sync(ref, stage, delta, error=error)
    missing = set(delta.add) - have
    if missing:
        return kanban.Sync(ref, stage, delta, error=f"missing label in GitLab: {', '.join(sorted(missing))}; "
                    f"create the label(s) {', '.join(sorted(missing))} before syncing")
    payload = {}
    if delta.add:
        payload["add_labels"] = ",".join(delta.add)
    if delta.remove:
        payload["remove_labels"] = ",".join(delta.remove)
    _, _, error = backlog._call("PUT", url, secret, timeout, payload)
    if error:
        return kanban.Sync(ref, stage, delta, error=error)
    verified, _, error = backlog._get(url, secret, timeout)
    if error or not isinstance(verified, dict) or not isinstance(verified.get("labels"), list):
        return kanban.Sync(ref, stage, delta, error=error or "could not verify GitLab labels after update")
    if set(verified["labels"]) != set(kanban.after(current, delta)):
        return kanban.Sync(ref, stage, delta, error="GitLab labels differ after update; reconcile before continuing")
    return kanban.Sync(ref, stage, delta, done=True)


@dataclass(frozen=True)
class Pipeline:
    """Status follows forge_review; a head without CI has id=None, status='none', url=''."""
    id: int | None  # None when the branch head has no pipeline
    ref: str
    sha: str
    status: str
    url: str


def _github_check(row: dict) -> dict:
    # Adapt REST workflow runs to the CheckRun shape understood by the shared status aggregator.
    return {"__typename": "CheckRun", "status": row["status"].upper(),
            "conclusion": (row.get("conclusion") or "").upper()}


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

    def mark(self, number: int) -> str:
        """The forge's request mark: `#21` on GitHub, `!5` on GitLab."""
        return f"#{number}" if self.github else f"!{number}"

    def _token(self) -> str:
        return self.token if self.token is not None else backlog.token(self.home)

    def mark_for_stage(self, stage: str) -> str | None:
        """The label merge verifies for a stage; '' when the stage has none; None without kanban."""
        if self.kanban is None:
            return None
        if stage in self.kanban.stage_map or stage in self.kanban.terminal_stages:
            return self.kanban.label_for(stage)
        return ""

    # reads
    def issues(self, state: str = backlog.OPEN, *, limit: int | None = None) -> backlog.Fetch:
        return backlog.issues(self.home, token=self.token, state=state, gh=self.gh, limit=limit)

    def issue(self, number: int) -> backlog.Read:
        return backlog.read_one(self.home, number, token=self.token, gh=self.gh)

    def _pipeline_read(self, path: str):
        if self.github:
            row, error = _gh_json(self.gh, ["api", f"repos/{self.home.repo}/{path}"])
        else:
            token = self._token()
            if not token:
                raise ForgeError(self.home.missing_token)
            row, _, error = backlog._call("GET", f"{self.home.api()}/{path}", token, backlog.TIMEOUT)
        if error:
            raise ForgeError(error)
        return row

    def _pipeline(self, row: dict) -> Pipeline:
        if not isinstance(row, dict):
            raise ValueError("pipeline has unreadable id, ref, SHA, status or URL")
        ref, sha, url = (row.get(k) for k in (
            ("head_branch", "head_sha", "html_url") if self.github else ("ref", "sha", "web_url")))
        status, number = row.get("status"), row.get("id")
        if (type(number) is not int or number <= 0
                or any(not isinstance(v, str) or not v.strip() for v in (ref, sha, url, status))
                or (self.github and row.get("conclusion") is not None
                    and not isinstance(row.get("conclusion"), str))):
            raise ValueError("pipeline has unreadable id, ref, SHA, status or URL")
        if self.github:
            status = forge_review.github_pipeline([_github_check(row)])
        else:
            status = forge_review.gitlab_pipeline_status(status)
        return Pipeline(number, ref, sha, status, url)

    def pipeline(self, pipeline_id: int) -> Pipeline:
        """Return one normalized pipeline/run in this repository; unreadable responses raise ForgeError."""
        try:
            if type(pipeline_id) is not int or pipeline_id <= 0:
                raise ValueError("pipeline id must be a positive integer")
            path = "actions/runs" if self.github else "pipelines"
            return self._pipeline(self._pipeline_read(f"{path}/{pipeline_id}"))
        except ForgeError:
            raise
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ForgeError(f"pipeline: {exc}") from exc

    def branch_head(self, ref: str) -> Pipeline:
        """Return head CI (all GitHub workflows), or status='none'; unreadable heads raise ForgeError."""
        try:
            if not isinstance(ref, str) or not ref.strip() or ref != ref.strip() or ref in (".", ".."):
                raise ValueError("branch ref must be a nonblank, trimmed name other than '.' or '..'")
            path = "branches" if self.github else "repository/branches"
            branch = self._pipeline_read(f"{path}/{quote(ref, safe='')}")
            commit = branch.get("commit") if isinstance(branch, dict) else None
            sha = commit.get("sha" if self.github else "id") if isinstance(commit, dict) else None
            if not isinstance(sha, str) or not sha.strip():
                raise ValueError("branch has no readable head SHA")
            if branch.get("name") != ref:
                raise ValueError("branch name does not match the requested ref")
            if self.github:
                query = urlencode({"branch": ref, "head_sha": sha, "per_page": backlog.PER_PAGE})
                payload = self._pipeline_read(f"actions/runs?{query}")
                rows = payload.get("workflow_runs") if isinstance(payload, dict) else None
            else:
                query = urlencode({"ref": ref, "sha": sha, "order_by": "id", "sort": "desc", "per_page": 1})
                rows = self._pipeline_read(f"pipelines?{query}")
            if not isinstance(rows, list):
                raise ValueError("pipeline list is unreadable")
            if self.github:
                # Single-page reads trust total_count, else fail closed at PER_PAGE; only each workflow's highest-ID run attests to its current head state.
                total = payload.get("total_count")
                truncated = total > len(rows) if type(total) is int else len(rows) >= backlog.PER_PAGE
                if truncated:
                    raise ValueError("pipeline list was truncated")
            if not rows:
                return Pipeline(None, ref, sha, "none", "")
            if self.github:
                workflows = {}
                for row in rows:
                    run = self._pipeline(row)
                    if run.sha != sha or run.ref != ref:
                        raise ValueError("latest pipeline does not match the requested branch head")
                    workflow = row.get("workflow_id")
                    if type(workflow) is not int or workflow <= 0:
                        raise ValueError("pipeline has no readable workflow id")
                    if workflow not in workflows or run.id > workflows[workflow][0].id:
                        workflows[workflow] = (run, row)
                latest = max((run for run, row in workflows.values()), key=lambda run: run.id)
                status = forge_review.github_pipeline([_github_check(row) for run, row in workflows.values()])
                return Pipeline(latest.id, ref, sha, status, latest.url)
            latest = self._pipeline(rows[0])
            # The transport's filters cannot attest to this head unless the returned pipeline agrees.
            if latest.sha != sha or latest.ref != ref:
                raise ValueError("latest pipeline does not match the requested branch head")
            return latest
        except ForgeError:
            raise
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ForgeError(f"branch head: {exc}") from exc

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
                closed.update(int(ref[1:]) for ref in refs if _LOCAL_CLOSE.fullmatch(ref))
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ForgeError(f"merged requests: {exc}") from exc
        return closed

    def closing_request(self, issue_key: str, requests: list[merge_ready.Request] | None = None) -> merge_ready.Request:
        """The newest open request closing only the assigned issue; extra or cross-repo closing refs are unsafe to merge."""
        found = self.requests() if requests is None else requests
        closing = [r for r in found if r.closes and all(
            _LOCAL_CLOSE.fullmatch(ref) and self.key(int(ref[1:])) == issue_key for ref in r.closes)]
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

    def snapshot(self, issue_key: str, *, issue: Any | None = None,
                 request: merge_ready.Request | None = None) -> dict[str, Any]:
        """One issue's forge row (as `snapshots` builds), plus the observed stage label merge can verify.

        A blank `stage_mark` means no mapped label; None means stage was not read (no kanban).
        """
        if issue is None:
            got = self.issue(int(issue_key.rsplit("#", 1)[1]))
            if not got.ok or got.issue is None:
                raise ForgeError(f"issue: {got.error or 'not found'}")
            issue = got.issue
        if request is None:
            latest: dict[int, merge_ready.Request] = {}
            # shortcut: several requests closing one issue (rework) report the newest; key by (issue, attempt) when M3 segments need each
            for r in sorted(self.requests(), key=lambda r: r.number):
                for ref in r.closes:
                    latest[int(ref[1:])] = r
            request = latest.get(issue.iid)
        snap: dict[str, Any] = {"issue_key": issue_key, "title": issue.title,
                                "forge_state": "open" if issue.state == backlog.OPEN else "closed",
                                "stage_mark": None}
        if self.kanban is not None:
            statuses: dict[str, str | None] | None = None
            if self.kanban.github_project is not None and self.github:
                statuses, error = project_items(self.gh, self.kanban.github_project)
                if error:
                    raise ForgeError(f"project status: {error}")
            snap["stage"] = self._stage(issue, statuses)
            snap["human_review"] = self.kanban.human_review_label in issue.labels
            snap["stage_mark"] = self.mark_for_stage(snap["stage"])
        if self.approver:
            snap["review_state"] = request.approval if request else ""
        snap["merge_state"] = request.verdict if request else ""
        return snap

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
                if _LOCAL_CLOSE.fullmatch(ref):
                    latest[int(ref[1:])] = r
        merged = self._merged_closes()
        statuses: dict[str, str | None] = {}
        if self.kanban is not None and self.kanban.github_project is not None and self.github:
            statuses, error = project_items(self.gh, self.kanban.github_project)
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
                snap["stage_mark"] = self.mark_for_stage(snap["stage"])
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

    def protected_branches(self) -> tuple[str, ...]:
        """The repository's default branch, then its protected branches, for the worker guard. Unreadable raises."""
        _, repo = home_of(self.home)
        try:
            if self.github:
                # shortcut: first 100 protected branches, branch rulesets not listed; paginate or read rulesets when a repo outgrows it
                code, out, err = self.gh(["api", f"repos/{repo}", "--jq", ".default_branch"])
                if code:
                    raise RuntimeError(err.strip() or f"gh exited {code}")
                names = [out.strip()]
                code, out, err = self.gh(["api", f"repos/{repo}/branches?protected=true&per_page=100", "--jq", ".[].name"])
                if code:
                    raise RuntimeError(err.strip() or f"gh exited {code}")
                names += out.split()
            else:
                base, token = self.home.api(), self._token()
                project, _, err = backlog._call("GET", base, token, backlog.TIMEOUT)
                if err:
                    raise RuntimeError(err)
                names = [project["default_branch"]]
                rows, _, err = backlog._call("GET", f"{base}/protected_branches?per_page=100", token, backlog.TIMEOUT)
                if err:
                    raise RuntimeError(err)
                names += [row["name"] for row in rows]
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ForgeError(f"protected branches: {exc}") from exc
        found = tuple(dict.fromkeys(n for n in names if isinstance(n, str) and n.strip()))
        if not found or not names[0].strip():
            raise ForgeError("protected branches: the forge reported no default branch")
        return found

    # writes: preview unless commit
    def merge(self, number: int, commit: bool = False, issue_key: str | None = None, approval: str | None = None,
              head: str | None = None, stage_mark: str | None = None) -> str:
        """Re-read readiness and the issue's closing request, then preview or report the verified head's merge outcome.
        `approval`, `head` and the issue's `stage_mark` are what a policy check relied on; the final read must still
        report them, and the issue must still be open. A blank `stage_mark` expects no mapped stage label;
        None leaves the stage unchecked."""
        mark = self.mark(number)
        found = self.requests()
        one = next((r for r in found if r.number == number), None)
        if one is None:
            raise ForgeError(f"{mark} is not an open request")
        if issue_key is not None:
            selected = self.closing_request(issue_key, found)
            if selected.number != number:
                raise ForgeError(f"{mark} is no longer the newest request closing {issue_key}: {self.mark(selected.number)}")
        if one.verdict != "ready":
            raise ForgeError(f"not merged: {merge_ready.line(one, mark[:1])}")
        if approval is not None and one.approval != approval:  # verdict leaves approval out
            raise ForgeError(f"{mark} approval changed since the policy check: {approval} -> {one.approval}")
        if head is not None and one.head != head:  # a push after the check is unreviewed code
            raise ForgeError(f"{mark} head changed since the policy check: {head[:7]} -> {one.head[:7]}")
        if issue_key is not None:  # a close or stage move during the policy check is a forge-side hold
            got = self.issue(int(issue_key.rsplit("#", 1)[1]))
            if not got.ok or got.issue is None:
                raise ForgeError(f"issue: {got.error or 'not found'}")
            if not got.issue.is_open:
                raise ForgeError(f"{issue_key} is no longer open on the forge")
            if self.kanban and self.kanban.human_review_label in got.issue.labels:
                raise ForgeError(f"{issue_key} needs input on the forge")
            if stage_mark is not None and self.kanban:
                try:  # same decode as snapshot: Project Status when configured, else labels
                    statuses = None
                    if self.kanban.github_project is not None and self.github:
                        statuses, error = project_items(self.gh, self.kanban.github_project)
                        if error:
                            raise ForgeError(f"project status: {error}")
                    observed = self._stage(got.issue, statuses)
                except ValueError as exc:
                    raise ForgeError(f"{issue_key} left its stage since the policy check: {exc}") from exc
                if stage_mark == "" and observed:
                    raise ForgeError(f"{issue_key} left its stage since the policy check: a mapped stage label appeared")
                if stage_mark and (self.mark_for_stage(observed) or "").casefold() != stage_mark.casefold():
                    raise ForgeError(f"{issue_key} left its stage since the policy check: {stage_mark} is gone")
        evidence = f"{mark} at {one.head} · pipeline {one.pipeline_id or '-'} on {one.pipeline_sha} · {one.url}"
        if not commit:
            return f"would merge {evidence}"
        _, repo = home_of(self.home)
        try:
            code, result = merge_approved.merge_head(one, self.home, self.github, repo, gh=self.gh,
                                                    call=backlog._call, token=None if self.github else self._token())
        except (RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ForgeError(f"{mark} not merged at {one.head}: {exc}") from exc
        if code:
            raise ForgeError(f"{mark} not merged at {one.head}: {result or f'merge exited {code}'}")
        outcome, _, error = result.partition(": ")
        return f"{outcome} {evidence}" + (f": {error}" if error else "")

    def comment(self, number: int, body: str, commit: bool = False) -> str:
        """Post or preview a comment on an issue."""
        if not commit:
            return f"would comment on #{number}: {body}"
        return backlog.comment(self.home, number, body, token=None if self.github else self._token(),
                               commit=True, gh=self.gh).error

    def create_issue(self, title: str, body: str = "", labels: tuple[str, ...] = (), parent: int | None = None,
                     blocked_by: tuple[int, ...] = (), commit: bool = False) -> backlog.Written:
        return backlog.create(self.home, title, body=body, labels=labels, token=self.token, commit=commit, gh=self.gh,
                              parent=parent, blocked_by=blocked_by)

    def write_stage(self, number: int, stage: str, expected: str | None = None, commit: bool = False,
                    initialize: bool = False) -> kanban.Sync:
        """Move the issue's stage (label, or GitHub Project Status when configured), leaving its other labels.
        `expected` is the stage the store last wrote; `initialize` starts a blank issue or migrates its legacy label into
        Project Status, and is never inferred from a missing `expected`. A card moved by hand is reported, never overwritten."""
        host, repo = home_of(self.home)
        ref = IssueRef(repo, number, host)
        if self.kanban is None:
            return kanban.Sync(ref, stage, error="no [kanban] stage labels configured")
        if self.github:
            return _sync_github(ref, self.kanban, stage, gh=self.gh, commit=commit, expected_stage=expected,
                                initialize=initialize)
        return _sync_gitlab(self.home, ref, self.kanban, stage, token=self.token, commit=commit, expected_stage=expected,
                            initialize=initialize)

    def add_human_hold(self, number: int) -> str:
        """Add only the review hold; leave the numbered stage and Project Status untouched."""
        if self.kanban is None:
            return "no [kanban] stage labels configured"
        host, repo = home_of(self.home)
        return add_human_hold(IssueRef(repo, number, host), self.home, self.kanban, gh=self.gh, token=self.token)


def add_human_hold(ref: backlog.IssueRef, home: backlog.Backlog | backlog.GitHubBacklog,
                   config: Kanban, *, gh=None, token: str | None = None) -> str:
    """Add only the review hold; leave the numbered stage and Project Status untouched.

    Return an error string, or empty string when the hold is verified. This is used when a
    one-shot worker exits unfinished, including after partial edits.
    """
    label = config.human_review_label
    if ref.host == backlog.GITHUB:
        gh = gh or backlog.run_gh
        current, error = _github_issue(gh, ref)
        if error or label in kanban._canonical(config, current):
            return error
        # #566: the hold is the point — a label the repo has never seen is created, not refused.
        for made in backlog.ensure_labels(backlog.GitHubBacklog(ref.repo), (label,), commit=True, gh=gh):
            if made.error:
                return made.error
        code, _, stderr = gh(["issue", "edit", str(ref.number), "-R", ref.repo, "--add-label", label])
        if code:
            return stderr.strip() or f"gh exited {code}"
        updated, error = _github_issue(gh, ref)
        return error or ("GitHub review hold was not applied" if label not in kanban._canonical(config, updated) else "")
    if not isinstance(home, backlog.Backlog) or ref.host != backlog.home_of(home)[0]:
        return "issue host does not match the configured backlog"
    project = home if home.project == ref.repo else home.sibling(ref.repo)
    secret = token if token is not None else backlog.token(project)
    if not secret:
        return home.missing_token
    url = f"{project.issues_url}/{ref.number}"
    row, _, error = backlog._get(url, secret, backlog.TIMEOUT)
    if error:
        return error
    if not isinstance(row, dict) or not isinstance(row.get("labels"), list):
        return "GitLab issue has no readable labels"
    if label in row["labels"]:
        return ""
    # #566: the hold is the point — a label the project has never seen is created, not refused.
    for made in backlog.ensure_labels(project, (label,), token=secret, commit=True):
        if made.error:
            return made.error
    _, _, error = backlog._call("PUT", url, secret, backlog.TIMEOUT, {"add_labels": label})
    if error:
        return error
    updated, _, error = backlog._get(url, secret, backlog.TIMEOUT)
    return error or ("GitLab review hold was not applied" if not isinstance(updated, dict) or
                     label not in updated.get("labels", []) else "")


def for_workspace(root: Path | str, **options: Any) -> Forge | None:
    """The forge the workspace's `Backlog:` line names, or None when it has none."""
    config = Path(root) / "CLAUDE.md"
    home = backlog_from_config(config) if config.is_file() else None
    return Forge(home, **options) if home else None
