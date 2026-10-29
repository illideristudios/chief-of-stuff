"""The `Backlog:` line in a workspace's `## Coordinator` block, and the issue a tracker cell points at.

The model only: which forge the line names and where a `#N` resolves. Talking to the forge is backlog.py's. This
module imports no HTTP client and nothing from the plugin, so the config layer and the PreToolUse hook can read a
`Backlog:` line without loading one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote, urlsplit

BACKLOG = re.compile(r"^\s*(?:[-*]\s*)?Backlog:\s*(.+?)\s*$", re.MULTILINE)
SCHEMES = ("http", "https")
ACCOUNT = "chief-of-stuff"
DEFAULT_ENV = "CHIEF_OF_STUFF_GITLAB_TOKEN"


class BacklogError(ValueError):
    """Invalid Backlog configuration."""


@dataclass(frozen=True)
class Backlog:
    """GitLab location and credential name; never the token value."""

    host: str
    project: str
    env: str = DEFAULT_ENV
    account: str = ACCOUNT

    @property
    def service(self) -> str:
        """Keychain service for this host."""
        return f"gitlab-{urlsplit(self.host).hostname or ''}"

    def api(self, project: str | None = None) -> str:
        """The REST base for `project` on this host; this backlog's own project when none is named."""
        return f"{self.host.rstrip('/')}/api/v4/projects/{quote(self.project if project is None else project, safe='')}"

    @property
    def issues_url(self) -> str:
        return f"{self.api()}/issues"

    @property
    def missing_token(self) -> str:
        """Why a GitLab call cannot be made: no credential for this host."""
        return f"no token: set ${self.env} or add it to the Keychain as {self.service}"

    def sibling(self, project: str) -> Backlog:
        """Another project on this host, read with this backlog's credential."""
        return replace(self, project=project)


@dataclass(frozen=True)
class GitHubBacklog:
    """GitHub repository; `gh` manages authentication."""

    repo: str


GH_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")


def _section(text: str, heading: str) -> list[str]:
    """The lines under `heading`, up to the next `## `. Empty when the heading is absent."""
    out: list[str] = []
    seen = False
    for line_ in text.splitlines():
        if line_.strip() == heading:
            seen = True
            continue
        if seen and line_.startswith("## "):
            break
        if seen:
            out.append(line_)
    return out


def parse_backlog(spec: str) -> Backlog:
    """`<tool>; host <url>; project <group/name>; token env <NAME>` — the shape `Board:` already uses.

    A `token` part that is not `env <NAME>` is refused, and the refusal does not quote what it found:
    house rule 6 forbids a secret in a committed file, and an error message is a committed file's
    next stop.
    """
    parts = [p.strip() for p in spec.split(";") if p.strip()]
    if parts and parts[0].lower().startswith("github"):
        return _parse_github(parts)
    host = project = ""
    env = DEFAULT_ENV
    for part in parts[1:]:
        key, _, value = part.partition(" ")
        key, value = key.lower(), value.strip().strip("`").strip()
        if key == "host":
            host = value
        elif key == "project":
            project = value
        elif key == "token":
            kind, _, rest = value.partition(" ")
            # An environment variable's name has no spaces, so the name ends at the first one and
            # whatever follows is prose. The live line carries a parenthetical after it.
            name = rest.split()[0] if rest.split() else ""
            if kind.lower() != "env" or not name:
                raise BacklogError(
                    "a `Backlog:` line names an environment variable, never a token: write "
                    "`token env NAME`"
                )
            env = name
    if not host:
        raise BacklogError(f"backlog needs a host: {spec!r}")
    if urlsplit(host).scheme not in SCHEMES:
        raise BacklogError(f"backlog host must be http or https: {host!r}")
    if not project:
        raise BacklogError(f"backlog needs a project path: {spec!r}")
    return Backlog(host=host, project=project, env=env)


def _parse_github(parts: list[str]) -> GitHubBacklog:
    """`GitHub…; repo <url|owner/name>`. The repo ends at the first space; a parenthetical is prose."""
    repo = ""
    for part in parts[1:]:
        key, _, value = part.partition(" ")
        if key.lower() == "repo":
            words = value.split()
            repo = words[0].strip("`") if words else ""
    repo = re.sub(r"^https?://(?:www\.)?github\.com/", "", repo).removesuffix(".git").strip("/")
    if not GH_REPO.match(repo):
        raise BacklogError(f"backlog needs a repo owner/name: {repo or 'none given'}")
    return GitHubBacklog(repo=repo)


def backlog_from_config(path: Path) -> Backlog | GitHubBacklog | None:
    """The `Backlog:` line inside `## Coordinator`, or None. No line is a workspace without a backlog."""
    body = "\n".join(_section(Path(path).read_text(), "## Coordinator"))
    m = BACKLOG.search(body)
    return parse_backlog(m.group(1)) if m else None


GITHUB = "github.com"


def home_of(cfg: Backlog | GitHubBacklog) -> tuple[str, str]:
    """(host, repo or project): where a bare `#N` in the tracker points."""
    if isinstance(cfg, GitHubBacklog):
        return GITHUB, cfg.repo
    return urlsplit(cfg.host).hostname or "", cfg.project


def file_with(cfg: Backlog | GitHubBacklog) -> str:
    """The command that files an issue in this backlog, for a fault line to name."""
    return "chief-of-stuff backlog --create"


@dataclass(frozen=True)
class IssueRef:
    """What a tracker's `issue` cell points at. `host` is github.com unless the cell names a GitLab."""

    repo: str
    number: int
    host: str = GITHUB

    @property
    def url(self) -> str:
        if self.host == GITHUB:
            return f"https://github.com/{self.repo}/issues/{self.number}"
        return f"https://{self.host}/{self.repo}/-/issues/{self.number}"

    def label(self, home: Backlog | GitHubBacklog | None) -> str:
        """`#N` in the Backlog, `owner/repo#N` on GitHub, `host/project#N` on any other GitLab: the key an issue is
        filed and read under, so a project path on two hosts is two keys."""
        if home is not None and (self.host, self.repo) == home_of(home):
            return f"#{self.number}"
        return f"{self.repo}#{self.number}" if self.host == GITHUB else f"{self.host}/{self.repo}#{self.number}"


_REF_STORE = re.compile(r"^(github|gitlab):(.+)#(\d+)$")


def store_label(key: str, home: Backlog | GitHubBacklog | None) -> str:
    """`IssueRef.label` for a store key (`github:owner/repo#N`, `gitlab:host/project#N`). Anything else is unchanged."""
    m = _REF_STORE.match(key)
    if not m:
        return key
    kind, body, number = m.group(1), m.group(2), int(m.group(3))
    if kind == "github":
        ref = IssueRef(body, number)
    else:
        host, _, repo = body.partition("/")
        ref = IssueRef(repo, number, host)
    return ref.label(home)


WORKFLOW = "workflow"  # an `issue` cell naming a step the pipeline performs: no issue, never looked up
_REF_SHORT = re.compile(r"^([\w.-]+/[\w.-]+)?#(\d+)$")
_REF_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)/?$")
_REF_GITLAB = re.compile(r"^https://([\w.-]+)/((?:[\w.-]+/)+[\w.-]+)/-/(?:issues|work_items)/(\d+)/?$")
_REF_LINK = re.compile(r"^\[[^\]]*\]\((https://[^)\s]+)\)$")


def issue_ref(cell: str, home: Backlog | GitHubBacklog | None, *, any_host: bool = False) -> IssueRef | None:
    """`#N` (in the Backlog), `owner/repo#N` (GitHub), a GitHub issue URL, a GitLab issue URL on the
    Backlog's own host, a store key, or a markdown link to one. Anything else is None, and a bare `#N` with no Backlog to resolve it against is None too.
    `any_host` admits a GitLab URL on any host: for naming an issue, never for fetching one."""
    text = cell.strip()
    store = _REF_STORE.match(text)
    if store:
        kind, body, number = store.groups()
        text = (f"https://github.com/{body}/issues/{number}" if kind == "github"
                else f"https://{body}/-/issues/{number}")
    link = _REF_LINK.match(text)
    if link:
        text = link.group(1)
    m = _REF_URL.match(text)
    if m:
        return IssueRef(m.group(1), int(m.group(2)))
    m = _REF_GITLAB.match(text)
    if m:
        # Only the Backlog's own host: a URL is read with the Backlog's token, and a cell naming any other
        # host would send it there (R1 on PR 14).
        own = any_host or (isinstance(home, Backlog) and m.group(1) == home_of(home)[0])
        return IssueRef(m.group(2), int(m.group(3)), m.group(1)) if own else None
    m = _REF_SHORT.match(text)
    if not m or not (m.group(1) or home):
        return None
    if m.group(1):
        return IssueRef(m.group(1), int(m.group(2)))
    host, repo = home_of(home)
    return IssueRef(repo, int(m.group(2)), host)


def parse_issue_arg(arg: str, home: Backlog | GitHubBacklog | None) -> IssueRef | None:
    """A CLI argument: `issue_ref`'s forms, plus a bare `N`, which is valid here and not as a tracker cell."""
    text = arg.strip()
    return issue_ref(f"#{text}" if text.isdigit() else text, home)
