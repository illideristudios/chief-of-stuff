"""MODULE GRAPH, the section the decision and issue pages share: the branch-graph CLI's drawing of a change's head
against its merge base, into `<pages dir>/.graphs/<comparison hash>/`, from the clone the settings' `[graph]` table
names. The source & diff page fetches a change the same way, with `fetch` and `run`.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from html import escape
from pathlib import Path

import board_sources
import git_trees
from fragment import href
from settings import Graph

# Mermaid draws in the page's own tokens: nodes on surface, lines and text in fg, borders in line.
# ponytail: read once at load, so a theme switch after load keeps the old colours until a reload.
MERMAID = ('<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>'
           "<script>{const v=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();"
           "mermaid.initialize({startOnLoad:true,theme:'base',themeVariables:{primaryColor:v('--surface'),"
           "background:v('--surface'),primaryTextColor:v('--fg'),textColor:v('--fg'),lineColor:v('--fg'),"
           "primaryBorderColor:v('--line')}})}</script>")
# branch-graph styles drift edges by width only (older ones in its own orange); they take the legend's DRIFT colour.
DRIFT = (re.compile(r"^(\s*linkStyle [\d,]+ )(?:stroke:#\w+,)?", re.M), r"\1stroke:#E69F00,")
LEGEND = (("new", "NEW"), ("changed", "CHANGED"), ("removed", "REMOVED"), ("drift", "DRIFT"))


def _run(cmd: list[str], env: dict[str, str], timeout: int) -> str:
    """stdout of `cmd`, or ValueError with the last line of stderr."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        raise ValueError(f"{cmd[0]} timed out after {timeout}s") from None
    if out.returncode:
        raise ValueError((out.stderr.strip().splitlines() or [f"{cmd[0]} exited {out.returncode}"])[-1])
    return out.stdout


def run(cmd: list[str], timeout: int = 120) -> str:
    """stdout of a plain call in the caller's environment, minus the repository-selecting variables: the clone the
    path names must win over the launcher's."""
    return _run(cmd, git_trees.user_env(), timeout)


def git(args: list[str], timeout: int = 120) -> str:
    """stdout of a pinned git call in the clone (#556): the fetch writes into a clone whose config and hooks a worker
    can write, so it carries `git_trees.GIT_WRITE_PINS` and runs on `git_trees.write_env()` — its hooks and helpers
    pinned dark, the caller's credential variables riding along."""
    return _run(["git", *git_trees.GIT_WRITE_PINS, *args], git_trees.write_env(), timeout)


def validate_source(change: board_sources.Change) -> tuple[str, str]:
    """Reject sources the configured clone cannot safely fetch or serve from cache."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", change.head):
        raise ValueError("the forge gave no head commit")
    if re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", change.ref):
        raise ValueError("the configured clone has no authorized source for a sibling pull request")
    match = re.fullmatch(r"([#!])(\d+)", change.ref)
    if not match:
        raise ValueError("invalid change reference")
    return match.groups()


def fetch(change: board_sources.Change, clone: Path) -> str:
    """Fetch the change's head into the clone; return its merge base with `origin/<base>`."""
    kind, n = validate_source(change)
    ref = f"refs/merge-requests/{n}/head" if kind == "!" else f"pull/{n}/head"
    # ponytail: the fetch runs synchronously on the GET that renders the page (up to minutes on a big clone); a
    # background job if that bites.
    git(["-C", str(clone), "fetch", "-q", "--end-of-options", "origin", ref, *([change.base] if change.base else [])])
    return run(["git", "-C", str(clone), "merge-base", "--end-of-options", f"origin/{change.base}", change.head]).strip()


def cache_key(change: board_sources.Change, clone: Path, base: str, *inputs) -> str:
    """Identify the actual comparison, source and all caller-specific output inputs."""
    identity = [change.head, change.base, change.ref, change.url, str(clone.resolve()), base, *inputs]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def comparison(change: board_sources.Change, clone: Path, index: Path, *inputs) -> tuple[str, bool]:
    """Fetch the comparison, or identify the last verified one as stale when offline."""
    validate_source(change)
    identity = cache_key(change, clone, '', *inputs)
    try:
        return fetch(change, clone), False
    except (OSError, ValueError) as error:
        try:
            saved = json.loads((index / identity).read_text())
            base = saved['base']
            if not isinstance(base, str) or not base or saved['key'] != cache_key(change, clone, base, *inputs):
                raise ValueError('invalid cache identity')
            return base, True
        except (OSError, ValueError, KeyError, TypeError):
            raise error


def remember_comparison(change: board_sources.Change, clone: Path, index: Path, base: str, *inputs) -> None:
    index.mkdir(parents=True, exist_ok=True)
    identity = cache_key(change, clone, '', *inputs)
    (index / identity).write_text(json.dumps({'base': base, 'key': cache_key(change, clone, base, *inputs)}))


def draw_result(change: board_sources.Change, g: Graph, clone: Path, pages: Path) -> tuple[list[str], str, bool]:
    """(mermaid views, summary) from branch-graph for the change's head against its merge base, cached per comparison
    and settings."""
    rules = clone / g.rules if g.rules else None
    try:
        rules_hash = hashlib.sha256(rules.read_bytes()).hexdigest() if rules else None
    except OSError:
        rules_hash = None  # branch-graph will report an unreadable rules file
    inputs = (g.root, g.depth, g.rules, rules_hash, g.exclude, g.max_nodes)
    index = pages / '.graphs' / '.comparisons'
    base, stale = comparison(change, clone, index, *inputs)
    key = cache_key(change, clone, base, *inputs)
    out = pages / ".graphs" / key
    summary = out / "summary.txt"
    if not summary.is_file():
        if stale:
            raise ValueError('cached module graph is unavailable while offline')
        out.mkdir(parents=True, exist_ok=True)
        said = run(["branch-graph", "--repo", str(clone), "--base", base, "--head", change.head, "--root", g.root,
                    "--depth", str(g.depth), *(["--rules", str(clone / g.rules)] if g.rules else []),
                    *(a for x in g.exclude for a in ("--exclude", x)), "--max-nodes", str(g.max_nodes),
                    "--out", str(out / "page.html")])
        summary.write_text((said.splitlines() or [""])[0])  # written last: it marks the drawing done
    said = summary.read_text()
    if re.search(r"\bviews=0\b", said):  # every changed module is excluded: nothing to draw
        if not stale:
            remember_comparison(change, clone, index, base, *inputs)
        return [], said, stale
    # An older branch-graph ignores --max-nodes, writes one page.mmd, and says no views=.
    views = sorted(out.glob("page-*.mmd"), key=lambda f: int(f.stem[5:])) or [out / "page.mmd"]
    result = [DRIFT[0].sub(DRIFT[1], f.read_text()) for f in views]
    if not stale:
        remember_comparison(change, clone, index, base, *inputs)
    return result, said, stale


def draw(change: board_sources.Change, g: Graph, clone: Path, pages: Path) -> tuple[list[str], str]:
    views, summary, stale = draw_result(change, g, clone, pages)
    if stale:
        raise ValueError('module graph is stale: the current base could not be verified')
    return views, summary


def section(change: board_sources.Change | None, graph: Graph | None, pages: Path | None, root: Path | None,
            head: str = "MODULE GRAPH", none: str = "This decision names no change to graph.") -> str:
    """The module graph section: branch-graph's drawing of `change`, or one line saying why there is none. The
    decision and issue pages share it."""
    if change:
        kind = "MR" if change.ref.startswith("!") else "PR"
        head += f' · <a class="ref" data-k="{kind}" {href(change.url)}>{escape(change.ref)}</a>'

    def wrap(body: str) -> str:
        return f"  <section>\n    <h2>{head}</h2>\n{body}\n  </section>\n"

    if not change:
        return wrap(f'    <p class="card">{escape(none)}</p>')
    if not (graph and pages and root):
        return wrap('    <p class="card">The module graph is not configured: the settings have no <code>[graph]</code> table.</p>')
    try:
        views, summary, stale = draw_result(change, graph, root / graph.clone, pages)
    except (OSError, ValueError) as e:
        return wrap(f'    <p class="card">The module graph is unavailable: {escape(str(e))}</p>')
    warning = '    <p class="card">Stale cached graph: the current base could not be verified.</p>\n' if stale else ''
    if not views:
        return wrap(warning + '    <p class="card">No module outside the excluded set changed.</p>')
    head += (f' <span class="cap">{len(views)} view{"s" * (len(views) != 1)} · '
             f"at most {graph.max_nodes} modules each</span>")
    figures = []
    for n, mmd in enumerate(views, 1):
        title = re.match(r"%% view: (.*)", mmd)
        figures.append(f'      <figure class="card fig view"><div class="cap">{n}{" · " + escape(title[1]) if title else ""}</div>'
                       f'<div class="scroll"><pre class="mermaid">{escape(mmd)}</pre></div></figure>')
    chips = "".join(f'<span class="chip {k}">{label}</span>' for k, label in LEGEND)
    rest = sum(int(x) for x in re.findall(r"(\d+) more modules untouched", "".join(views)))
    rest = f"<span>Not drawn: {rest} modules.</span>" if rest else ""
    # Decision.dc.html's caption: "nodes +1 −1 ~1 · edges +2 −1 · drift 1".
    caption = re.sub(r" (?:excluded|views)=\S*", "", summary).replace(" edges", " · edges").replace(" drift=", " · drift ")
    return wrap(warning + '    <figure class="graph"><div class="views">\n' + "\n".join(figures) + "\n    </div>\n"
                f'    <figcaption>{chips}<span class="sum">{escape(caption)}</span>{rest}</figcaption></figure>\n    {MERMAID}')


def reference_path(root: Path, path: str, identity: tuple[str, str] | None, at: str | None,
                   need: str, cache: dict) -> str | None:
    """The pages' read-only Git gateway for forge file references, alongside their change comparisons."""
    return git_trees.reference_path(root, path, identity, at, need, cache)
