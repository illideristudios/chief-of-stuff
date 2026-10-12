#!/usr/bin/env python3
"""Read and write the configured GitHub or GitLab backlog.

GitHub uses `gh`; GitLab uses its API. Writes preview by default. Failed reads report an unknown
count rather than zero. Credentials come from the configured environment variable or Keychain.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlencode, urlsplit

# The `Backlog:` line's model lives in backlog_ref.py, which loads no HTTP client. It is imported here as well,
# so `backlog.Backlog`, `backlog.issue_ref` and the rest still name it.
from backlog_ref import (  # noqa: F401
    GITHUB, WORKFLOW, Backlog, BacklogError, GitHubBacklog, IssueRef, backlog_from_config, file_with, home_of, issue_ref,
    parse_backlog, parse_issue_arg,
)

TIMEOUT = 5.0
PER_PAGE = 100
MAX_PAGES = 50
OPEN, CLOSED = "opened", "closed"


# A full result may be truncated; reject it rather than treating it as complete.
GH_LIMIT = PER_PAGE * MAX_PAGES
GH_FIELDS = "number,title,state,labels,url,updatedAt,closedAt"
GH_STATE = {OPEN: "open", CLOSED: "closed", "all": "all"}


def run_gh(args: list[str], input_text: str | None = None) -> tuple[int, str, str]:
    """Run `gh`, allowing an absolute-path eval override."""
    binary = os.environ.get("CHIEF_OF_STUFF_GH", "gh")
    if binary != "gh" and not Path(binary).is_absolute():
        return 127, "", "CHIEF_OF_STUFF_GH must be an absolute executable path"
    try:
        done = subprocess.run([binary, *args], input=input_text, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 127, "", "gh not found"
    return done.returncode, done.stdout, done.stderr


@dataclass(frozen=True)
class Issue:
    iid: int
    title: str
    state: str
    labels: tuple[str, ...] = ()
    web_url: str = ""
    updated_at: str = ""
    closed_at: str = ""
    body: str = ""

    @property
    def is_open(self) -> bool:
        return self.state == OPEN


@dataclass(frozen=True)
class Note:
    """One issue comment: who said it, when, and what. System notes never become a Note."""

    author: str
    time: str = ""
    body: str = ""


@dataclass(frozen=True)
class Read:
    """One issue, or the reason there is none. `error` empty means the issue is the whole answer."""

    issue: Issue | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True)
class Notes:
    """A comment list, or the reason there is none. `error` empty means the list is the whole answer."""

    notes: tuple[Note, ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True)
class Fetch:
    """Issues, or the reason there are none. `error` empty means the list is the whole answer."""

    issues: tuple[Issue, ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True)
class Counts:
    """`None` is not zero. A count that could not be taken stays None all the way to the line."""

    open: int | None = None
    closed: int | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def keychain_token(account: str, service: str) -> str:
    """The macOS Keychain entry, or "". Not found is the common case, not an incident."""
    found = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True, text=True, check=False,
    )
    return found.stdout.strip() if found.returncode == 0 else ""


def glab_token(host: str) -> str:
    """The token `glab auth login` already keeps for the host, or "". No second copy of it is made."""
    try:
        found = subprocess.run(["glab", "config", "get", "token", "--host", host],
                               capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return ""
    return found.stdout.strip() if found.returncode == 0 else ""


def token(cfg: Backlog, environ: dict | None = None, keychain=keychain_token, glab=glab_token) -> str:
    """Environment, then Keychain, then glab's login, then absent. Absent is "" — a state to report, not an exception."""
    env = os.environ if environ is None else environ
    from_env = (env.get(cfg.env) or "").strip()
    if from_env:
        return from_env
    for ask in (lambda: keychain(cfg.account, cfg.service), lambda: glab(urlsplit(cfg.host).hostname or "")):
        try:
            got = (ask() or "").strip()
        except Exception:  # a Mac without `security`, a locked Keychain, a denied prompt
            got = ""
        if got:
            return got
    return ""


def _call(method: str, url: str, private_token: str, timeout: float,
          payload: dict | None = None) -> tuple[object, dict, str]:
    """One request. Returns (parsed body, headers, error) and raises nothing the caller has to catch."""
    headers = {"PRIVATE-TOKEN": private_token, "User-Agent": "chief-of-stuff/backlog",
               "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (scheme checked in parse_backlog)
            raw = response.read()
            got = {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return None, {}, f"HTTP {exc.code} from GitLab"
    except Exception as exc:  # timeout, refused, DNS, TLS
        return None, {}, f"{type(exc).__name__}: {exc}"
    try:
        return json.loads(raw.decode("utf-8", "replace")), got, ""
    except ValueError:
        return None, got, "GitLab did not answer with JSON"


def _get(url: str, private_token: str, timeout: float) -> tuple[object, dict, str]:
    return _call("GET", url, private_token, timeout)


def _issue(row: dict) -> Issue:
    return Issue(
        iid=int(row.get("iid", 0)),
        title=str(row.get("title", "")),
        state=str(row.get("state", "")),
        labels=tuple(str(x) for x in row.get("labels", ())),
        web_url=str(row.get("web_url", "")),
        updated_at=str(row.get("updated_at", "")),
        closed_at=str(row.get("closed_at", "")),
    )


def _gh_list(cfg: GitHubBacklog, state: str, fields: str, labels: tuple[str, ...], gh) -> tuple[list, str]:
    args = ["issue", "list", "-R", cfg.repo, "--state", GH_STATE[state], "--json", fields, "--limit", str(GH_LIMIT)]
    for label in labels:
        args += ["--label", label]
    rc, out, err = gh(args)
    if rc != 0:
        return [], f"gh: {(err.strip().splitlines() or ['exit ' + str(rc)])[-1]}"
    try:
        rows = json.loads(out)
    except ValueError:
        return [], "GitHub did not answer with JSON"
    if not isinstance(rows, list):
        return [], "GitHub answered with something that is not a list of issues"
    if len(rows) >= GH_LIMIT:
        return [], f"{GH_LIMIT} issues or more; refusing to read a partial backlog as whole"
    return rows, ""


def _gh_issue(row: dict) -> Issue:
    return Issue(
        iid=int(row.get("number", 0)),
        title=str(row.get("title", "")),
        state=CLOSED if str(row.get("state", "")).upper() == "CLOSED" else OPEN,
        labels=tuple(str(x.get("name", "")) for x in row.get("labels", ()) if isinstance(x, dict)),
        web_url=str(row.get("url", "")),
        updated_at=str(row.get("updatedAt", "")),
        closed_at=str(row.get("closedAt") or ""),
    )


def issues(cfg: Backlog | GitHubBacklog, token: str | None = None, state: str = OPEN,
           labels: tuple[str, ...] = (), per_page: int = PER_PAGE, timeout: float = TIMEOUT, gh=None) -> Fetch:
    """Every page of `state` issues, or the reason there are none. Never a partial list read as whole."""
    if isinstance(cfg, GitHubBacklog):
        rows, error = _gh_list(cfg, state, GH_FIELDS, labels, gh or run_gh)
        return Fetch(error=error) if error else Fetch(issues=tuple(_gh_issue(r) for r in rows if isinstance(r, dict)))
    secret = token if token is not None else globals()["token"](cfg)
    if not secret:
        return Fetch(error=cfg.missing_token)
    out: list[Issue] = []
    page = "1"
    for _ in range(MAX_PAGES):
        query = {"state": state, "per_page": per_page, "page": page}
        if labels:
            query["labels"] = ",".join(labels)
        body, headers, error = _get(f"{cfg.issues_url}?{urlencode(query)}", secret, timeout)
        if error:
            return Fetch(error=error)
        if not isinstance(body, list):
            return Fetch(error="GitLab answered with something that is not a list of issues")
        out.extend(_issue(row) for row in body if isinstance(row, dict))
        page = (headers.get("x-next-page") or "").strip()
        if not page:
            return Fetch(issues=tuple(out))
    return Fetch(error=f"more than {MAX_PAGES} pages of issues; refusing to read a partial backlog as whole")


def issue_states(cfg: Backlog | GitHubBacklog, gh=None) -> dict[int, Issue]:
    """Every issue in the repo, open and closed, by number: one list call, not one per tracker row.
    A failed or partial read raises. An empty map would read as "none of these issues exist"."""
    got = issues(cfg, state="all", gh=gh)
    if not got.ok:
        raise BacklogError(got.error)
    return {i.iid: i for i in got.issues}


def read_one(cfg: Backlog | GitHubBacklog, iid: int, token: str | None = None,
             timeout: float = TIMEOUT, gh=None) -> Read:
    """One issue with its body (#63), the shapes the list omits. A failed read says why; it never raises."""
    if isinstance(cfg, GitHubBacklog):
        rc, out, err = (gh or run_gh)(["issue", "view", str(iid), "-R", cfg.repo, "--json", f"{GH_FIELDS},body"])
        if rc != 0:
            return Read(error=f"gh: {(err.strip().splitlines() or [f'exit {rc}'])[-1]}")
        try:
            row = json.loads(out)
        except ValueError:
            return Read(error="GitHub did not answer with JSON")
        if not isinstance(row, dict):
            return Read(error="GitHub answered with something that is not an issue")
        one = _gh_issue(row)
        return Read(issue=replace(one, body=str(row.get("body") or "")))
    secret = token if token is not None else globals()["token"](cfg)
    if not secret:
        return Read(error=cfg.missing_token)
    body, _headers, error = _get(f"{cfg.issues_url}/{iid}", secret, timeout)
    if error:
        return Read(error=error)
    if not isinstance(body, dict):
        return Read(error="GitLab answered with something that is not an issue")
    return Read(issue=replace(_issue(body), body=str(body.get("description") or "")))  # GitLab's issue text is `description`


def comments(cfg: Backlog | GitHubBacklog, iid: int, token: str | None = None,
             timeout: float = TIMEOUT, gh=None) -> Notes:
    """An issue's comments in order, each with author and time (#63). GitLab's system notes record
    label and state changes, not a person speaking, and are not comments. A failed read says why."""
    if isinstance(cfg, GitHubBacklog):
        rc, out, err = (gh or run_gh)(["api", "--paginate", f"repos/{cfg.repo}/issues/{iid}/comments?per_page={PER_PAGE}"])
        if rc != 0:
            return Notes(error=f"gh: {(err.strip().splitlines() or [f'exit {rc}'])[-1]}")
        try:
            rows = json.loads(out)
        except ValueError:
            return Notes(error="GitHub did not answer with JSON")
        if not isinstance(rows, list):
            return Notes(error="GitHub answered with something that is not a list of comments")
        return Notes(notes=tuple(_note(r) for r in rows if isinstance(r, dict)))
    secret = token if token is not None else globals()["token"](cfg)
    if not secret:
        return Notes(error=cfg.missing_token)
    out_notes: list[Note] = []
    page = "1"
    for _ in range(MAX_PAGES):
        query = {"per_page": PER_PAGE, "sort": "asc", "page": page}
        body, headers, error = _get(f"{cfg.issues_url}/{iid}/notes?{urlencode(query)}", secret, timeout)
        if error:
            return Notes(error=error)
        if not isinstance(body, list):
            return Notes(error="GitLab answered with something that is not a list of comments")
        out_notes += [n for n in (_note(row) for row in body if isinstance(row, dict)) if n.author]
        page = (headers.get("x-next-page") or "").strip()
        if not page:
            return Notes(notes=tuple(out_notes))
    return Notes(error=f"more than {MAX_PAGES} pages of comments; refusing to read a partial answer as whole")


def _note(row: dict) -> Note:
    """A forge comment row as a Note, or an empty one for a system note: its author is the forge itself."""
    if row.get("system") is True or row.get("system") == "true":
        return Note("", "", "")
    return Note(
        author=str(((row.get("user") or {}).get("login")) or ((row.get("author") or {}).get("username")) or ""),
        time=str(row.get("created_at") or ""),
        body=str(row.get("body") or ""),
    )


def view_text(one: Issue, notes: tuple[Note, ...] = ()) -> str:
    """The text `--view` prints: the issue's own line, its state, its body, then its comments in order."""
    lines = [issue_line(one), f"state: {one.state}", one.body]
    lines += [f"{n.time} {n.author}: {n.body}" for n in notes]
    return "\n".join(lines)


def _total(cfg: Backlog, secret: str, state: str, timeout: float) -> tuple[int | None, str]:
    """GitLab's `X-Total` for one state, or a full count when it withholds the header."""
    body, headers, error = _get(f"{cfg.issues_url}?{urlencode({'state': state, 'per_page': 1, 'page': 1})}",
                                secret, timeout)
    if error:
        return None, error
    total = (headers.get("x-total") or "").strip()
    if total.isdigit():
        return int(total), ""
    fetched = issues(cfg, token=secret, state=state, timeout=timeout)
    return (len(fetched.issues), "") if fetched.ok else (None, fetched.error)


def counts(cfg: Backlog | GitHubBacklog, token: str | None = None, timeout: float = TIMEOUT, gh=None) -> Counts:
    """Open and closed. One failure makes both unknown: half a count is a count nobody can act on."""
    if isinstance(cfg, GitHubBacklog):
        got = {}
        for state in (OPEN, CLOSED):
            rows, error = _gh_list(cfg, state, "number", (), gh or run_gh)
            if error:
                return Counts(error=error)
            got[state] = len(rows)
        return Counts(open=got[OPEN], closed=got[CLOSED])
    secret = token if token is not None else globals()["token"](cfg)
    if not secret:
        return Counts(error=cfg.missing_token)
    opened, error = _total(cfg, secret, OPEN, timeout)
    if error:
        return Counts(error=error)
    closed, error = _total(cfg, secret, CLOSED, timeout)
    if error:
        return Counts(error=error)
    return Counts(open=opened, closed=closed)


# ---- the write side -------------------------------------------------------------------------
#
# `--dry-run` is the default and `--commit` is required, because a write is outward-facing and the
# GitLab *web UI* is on the coordinator's human-only list. The API is not the web UI, but the
# caution transfers: the default has to be the mode that cannot do damage.

CREATE, CLOSE, COMMENT, LABEL = "create", "close", "comment", "label"
LABEL_COLOR = "#428bca"
FUTURE = {CREATE: "create", CLOSE: "close", COMMENT: "comment", LABEL: "create label"}
PAST = {CREATE: "created", CLOSE: "closed", COMMENT: "commented on", LABEL: "created label"}


@dataclass(frozen=True)
class Written:
    """What a write did, or would do, or could not do. `done` is the only thing that means it happened."""

    action: str
    what: str
    done: bool = False
    iid: int | None = None
    url: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def write_line(one: Written) -> str:
    """A refusal never borrows a success's wording — 116(a), on the write side."""
    if one.error:
        return f"{one.action} failed: {one.what} — {one.error}"
    if not one.done:
        return f"would {FUTURE[one.action]}: {one.what}"
    if one.what == f"#{one.iid}":   # close and comment name the issue and nothing else
        return f"{PAST[one.action]} {one.what}"
    where = f" #{one.iid}" if one.iid else ""
    return f"{PAST[one.action]}{where}: {one.what}" if where else f"{PAST[one.action]} {one.what}"


def _secret(cfg: Backlog, token: str | None) -> str:
    return token if token is not None else globals()["token"](cfg)


def existing_labels(cfg: Backlog, token: str | None = None, timeout: float = TIMEOUT) -> tuple[set[str], str]:
    """Every label the project has, or the reason we do not know. Empty-and-unknown are different."""
    secret = _secret(cfg, token)
    if not secret:
        return set(), cfg.missing_token
    names: set[str] = set()
    page = "1"
    for _ in range(MAX_PAGES):
        url = f"{cfg.api()}/labels"
        body, headers, error = _get(f"{url}?{urlencode({'per_page': PER_PAGE, 'page': page})}", secret, timeout)
        if error:
            return set(), error
        if not isinstance(body, list):
            return set(), "GitLab answered with something that is not a list of labels"
        names.update(str(row.get("name", "")) for row in body if isinstance(row, dict))
        page = (headers.get("x-next-page") or "").strip()
        if not page:
            return names, ""
    return set(), f"more than {MAX_PAGES} pages of labels"


def _gh_label_names(cfg: GitHubBacklog, gh) -> tuple[set[str], str]:
    """Every label name the repository has, or the reason we do not know."""
    rc, out, err = gh(["label", "list", "-R", cfg.repo, "--json", "name", "--limit", str(GH_LIMIT)])
    if rc != 0:
        return set(), f"gh: {(err.strip().splitlines() or ['exit ' + str(rc)])[-1]}"
    try:
        rows = json.loads(out)
    except ValueError:
        return set(), "GitHub did not answer with JSON"
    if not isinstance(rows, list):
        return set(), "GitHub answered with something that is not a list of labels"
    return {str(row.get("name", "")) for row in rows if isinstance(row, dict)}, ""


def _ensure_github_labels(cfg: GitHubBacklog, wanted: tuple[str, ...], *, commit: bool,
                          gh) -> tuple[Written, ...]:
    have, error = _gh_label_names(cfg, gh)
    if error:
        return (Written(LABEL, ", ".join(wanted), error=error),)
    out = []
    for name in wanted:
        if name in have:
            continue
        if not commit:
            out.append(Written(LABEL, name))
            continue
        rc, _out, err = gh(["label", "create", name, "-R", cfg.repo, "--color", LABEL_COLOR.lstrip("#")])
        out.append(Written(LABEL, name, done=rc == 0,
                           error="" if rc == 0 else f"gh: {(err.strip().splitlines() or ['exit ' + str(rc)])[-1]}"))
    return tuple(out)


def ensure_labels(cfg: Backlog | GitHubBacklog, names: tuple[str, ...], token: str | None = None,
                  commit: bool = False, timeout: float = TIMEOUT, gh=None) -> tuple[Written, ...]:
    """Create the labels a write needs and no others.

    No vocabulary is baked in. Zach, 2026-09-19 22:47 — "tasks are also going to be changeable over
    time" — so the caller names the labels and this creates whichever of them the forge has not seen.
    """
    wanted = tuple(n for n in dict.fromkeys(n.strip() for n in names) if n)
    if not wanted:
        return ()
    if isinstance(cfg, GitHubBacklog):
        return _ensure_github_labels(cfg, wanted, commit=commit, gh=gh or run_gh)
    secret = _secret(cfg, token)
    have, error = existing_labels(cfg, token=secret, timeout=timeout)
    if error:
        return (Written(LABEL, ", ".join(wanted), error=error),)
    out: list[Written] = []
    url = f"{cfg.api()}/labels"
    for name in wanted:
        if name in have:
            continue
        if not commit:
            out.append(Written(LABEL, name))
            continue
        _body, _headers, failed = _call("POST", url, secret, timeout, {"name": name, "color": LABEL_COLOR})
        out.append(Written(LABEL, name, done=not failed, error=failed))
    return tuple(out)


def create(cfg: Backlog | GitHubBacklog, title: str, body: str = "", labels: tuple[str, ...] = (),
           token: str | None = None, commit: bool = False, timeout: float = TIMEOUT, gh=None,
           parent: int | None = None, blocked_by: tuple[int, ...] = ()) -> Written:
    """One issue. Its labels are made first: GitLab drops an unknown label rather than refusing it."""
    if isinstance(cfg, GitHubBacklog):
        if not commit:
            return Written(CREATE, title)
        args = ["issue", "create", "-R", cfg.repo, "--title", title, "--body-file", "-"]
        for label in labels:
            args += ["--label", label]
        if parent is not None:
            args += ["--parent", str(parent)]
        if blocked_by:
            args += ["--blocked-by", ",".join(str(i) for i in blocked_by)]
        rc, out, err = (gh or run_gh)(args, body)
        if rc != 0:
            return Written(CREATE, title, error=f"gh: {err.strip() or 'exit ' + str(rc)}")
        match = re.search(rf"^https://github\.com/{re.escape(cfg.repo)}/issues/(\d+)/?$", out.strip())
        if not match:
            return Written(CREATE, title, error="gh created no recognizable issue URL")
        return Written(CREATE, title, done=True, iid=int(match.group(1)), url=out.strip())
    if parent is not None or blocked_by:
        return Written(CREATE, title, error="native issue relationships require a GitHub backlog")
    secret = _secret(cfg, token)
    if not secret:
        return Written(CREATE, title, error=cfg.missing_token)
    if labels:
        for made in ensure_labels(cfg, labels, token=secret, commit=commit, timeout=timeout):
            if made.error:
                return Written(CREATE, title, error=f"label {made.what}: {made.error}")
    if not commit:
        return Written(CREATE, title)
    payload = {"title": title}
    if body:
        payload["description"] = body
    if labels:
        payload["labels"] = ",".join(labels)
    got, _headers, error = _call("POST", cfg.issues_url, secret, timeout, payload)
    if error:
        return Written(CREATE, title, error=error)
    row = got if isinstance(got, dict) else {}
    return Written(CREATE, title, done=True, iid=int(row.get("iid", 0)) or None, url=str(row.get("web_url", "")))


def close(cfg: Backlog | GitHubBacklog, iid: int, token: str | None = None, commit: bool = False,
          timeout: float = TIMEOUT, gh=None) -> Written:
    what = f"#{iid}"
    if isinstance(cfg, GitHubBacklog):
        if not commit:
            return Written(CLOSE, what)
        rc, _out, err = (gh or run_gh)(["issue", "close", str(iid), "-R", cfg.repo])
        return Written(CLOSE, what, done=rc == 0, iid=iid, error="" if rc == 0 else f"gh: {err.strip()}")
    secret = _secret(cfg, token)
    if not secret:
        return Written(CLOSE, what, error=cfg.missing_token)
    if not commit:
        return Written(CLOSE, what)
    _got, _headers, error = _call("PUT", f"{cfg.issues_url}/{iid}", secret, timeout, {"state_event": "close"})
    return Written(CLOSE, what, done=not error, iid=iid, error=error)


def comment(cfg: Backlog | GitHubBacklog, iid: int, body: str, token: str | None = None, commit: bool = False,
            timeout: float = TIMEOUT, gh=None) -> Written:
    what = f"#{iid}"
    if isinstance(cfg, GitHubBacklog):
        if not commit:
            return Written(COMMENT, what)
        rc, _out, err = (gh or run_gh)(["issue", "comment", str(iid), "-R", cfg.repo, "--body-file", "-"], body)
        return Written(COMMENT, what, done=rc == 0, iid=iid, error="" if rc == 0 else f"gh: {err.strip() or 'exit ' + str(rc)}")
    secret = _secret(cfg, token)
    if not secret:
        return Written(COMMENT, what, error=cfg.missing_token)
    if not commit:
        return Written(COMMENT, what)
    _got, _headers, error = _call("POST", f"{cfg.issues_url}/{iid}/notes", secret, timeout, {"body": body})
    return Written(COMMENT, what, done=not error, iid=iid, error=error)


def line(got: Counts) -> str:
    """One line for the board. Unknown says unknown — it never borrows zero's wording."""
    if not got.ok or got.open is None:
        return f"backlog: unknown — {got.error or 'no answer'}"
    return f"backlog: {got.open} open, {got.closed} closed"


def issue_line(one: Issue) -> str:
    labels = f" [{', '.join(one.labels)}]" if one.labels else ""
    return f"#{one.iid} {one.title}{labels}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read the GitLab or GitHub backlog named in a ## Coordinator block.")
    parser.add_argument("--config", default="CLAUDE.md", help="workspace CLAUDE.md holding the block")
    parser.add_argument("--list", action="store_true", help="one line per issue instead of a count")
    parser.add_argument("--state", default=OPEN, choices=(OPEN, CLOSED, "all"), help="which issues")
    parser.add_argument("--labels", default="", help="comma-separated labels to narrow the ask")
    parser.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds per request")
    parser.add_argument("--create", metavar="TITLE", help="file one issue")
    parser.add_argument("--body", default="", help="the issue body, or the comment's text")
    parser.add_argument("--parent", type=int, help="GitHub parent issue number for --create")
    parser.add_argument("--blocked-by", type=int, action="append", default=[], metavar="IID",
                        help="GitHub blocker issue number for --create; repeat for multiple blockers")
    parser.add_argument("--close", metavar="ISSUE", help="close one issue: N, #N, or a reference in the Backlog")
    parser.add_argument("--comment", type=int, metavar="IID", help="comment on one issue")
    parser.add_argument("--view", type=int, metavar="N", help="print one issue's title, state, body and labels (#63)")
    parser.add_argument("--comments", action="store_true", help="with --view, add the issue's comments in order")
    # No `--token`: argv is readable by `ps`, so the token comes from the environment or the
    # Keychain and from nowhere a shell history can keep it.
    parser.add_argument("--commit", action="store_true",
                        help="actually write. Without it every write is printed and not made.")
    args = parser.parse_args(argv)

    config = Path(args.config)
    if not config.is_file():
        print(f"no config at {config}", file=sys.stderr)
        return 2
    try:
        cfg = backlog_from_config(config)
    except BacklogError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if cfg is None:
        print("backlog: none configured", file=sys.stderr)
        return 2

    labels = tuple(p.strip() for p in args.labels.split(",") if p.strip())
    writes: list[Written] = []
    if args.create:
        writes.append(create(cfg, args.create, body=args.body, labels=labels,
                             commit=args.commit, timeout=args.timeout,
                             parent=args.parent, blocked_by=tuple(args.blocked_by)))
    if args.close is not None:
        ref = parse_issue_arg(args.close, cfg)
        if ref is None or (ref.host, ref.repo) != home_of(cfg):
            print(f"backlog: --close {args.close!r} is not an issue in this Backlog", file=sys.stderr)
            return 2
        writes.append(close(cfg, ref.number, commit=args.commit, timeout=args.timeout))
    if args.comment:
        writes.append(comment(cfg, args.comment, args.body, commit=args.commit, timeout=args.timeout))
    if args.view is not None:
        ref = parse_issue_arg(str(args.view), cfg)
        if ref is None or (ref.host, ref.repo) != home_of(cfg):
            print(f"backlog: --view {args.view!r} is not an issue in this Backlog", file=sys.stderr)
            return 2
        got = read_one(cfg, ref.number, timeout=args.timeout)
        notes: tuple[Note, ...] = ()
        if got.ok and args.comments:
            found = comments(cfg, ref.number, timeout=args.timeout)
            if not found.ok:
                print(f"backlog: unknown — {found.error}", file=sys.stderr)
                return 1
            notes = found.notes
        if not got.ok:
            print(f"backlog: unknown — {got.error}", file=sys.stderr)
            return 1
        print(view_text(got.issue, notes))
        return 0
    if writes:
        for one in writes:
            print(write_line(one))
        if not args.commit:
            print("nothing was written: add --commit to make these real", file=sys.stderr)
        return 0 if all(w.ok for w in writes) else 1

    if not args.list:
        got = counts(cfg, timeout=args.timeout)
        print(line(got))
        return 0 if got.ok else 1

    fetched = issues(cfg, state=args.state, labels=labels, timeout=args.timeout)
    if not fetched.ok:
        print(f"backlog: unknown — {fetched.error}", file=sys.stderr)
        return 1
    for one in fetched.issues:
        print(issue_line(one))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
