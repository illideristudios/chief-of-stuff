"""Read-only pages for the owner to view and traverse the decision graph (§17).

`/decisions/graph` lists the current unreplaced leaves (full decision text and reason on each), each linked to
`/decisions/graph/<id>`. `/decisions/graph/<id>` shows one decision, its parents and children as links, and any
rejected alternatives with their reasons. Both render on request from detached store rows without writing, acting,
calling the history tool, or logging a grant.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from html import escape
from pathlib import Path

from backlog_ref import store_label
import decision_graph
from decision_page import Context, HEAD_OPEN, PAGE_CSS, context, write_if_changed
from fragment import issue_href, tab_bar, theme
from task_forge import PageData


def _section(heading: str, body: str) -> str:
    return f"  <section>\n    <h2>{escape(heading, quote=False)}</h2>\n{body}\n  </section>\n"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


def _prose_paragraphs(text: str) -> str:
    lines = [escape(part) for part in text.splitlines() if part.strip()] or [escape(text)]
    return "".join(f"<p>{line}</p>" for line in lines)


def _issue_link(issue_key: str, ctx: Context) -> str:
    if not issue_key:
        return ""
    label = store_label(issue_key, ctx.forge)
    return f'<a class="ref" href="{escape(issue_href(issue_key))}">{escape(label)}</a>'


def _graph_maps(
    nodes: Sequence[decision_graph.AncestorDecision],
) -> tuple[dict[int, decision_graph.AncestorDecision], dict[int, tuple[int, ...]], dict[int, tuple[int, ...]]]:
    """Build finite, deduplicated parent and child adjacency maps in O(V + E) even if raw edges contain a cycle."""
    by_id: dict[int, decision_graph.AncestorDecision] = {}
    for node in nodes:
        by_id[node.id] = node
    ordered_ids = sorted(by_id)
    parents_by_node: dict[int, tuple[int, ...]] = {}
    children_lists: dict[int, list[int]] = {nid: [] for nid in ordered_ids}
    for nid in ordered_ids:
        seen_parents: dict[int, None] = {}
        for pid in by_id[nid].parent_ids:
            if pid in by_id and pid not in seen_parents:
                seen_parents[pid] = None
                children_lists[pid].append(nid)
        parents_by_node[nid] = tuple(seen_parents)
    children_by_node = {nid: tuple(children_lists[nid]) for nid in ordered_ids}
    return by_id, parents_by_node, children_by_node


def _neighbour_list(neighbours: Sequence[decision_graph.AncestorDecision]) -> str:
    if not neighbours:
        return '    <div class="card"><span class="none">none</span></div>'
    rows = []
    for item in neighbours:
        topic = f'<span class="topic">{escape(item.topic)}</span>' if item.topic else ""
        rows.append(
            '      <div class="row">\n'
            '        <div class="stack">\n'
            f'          <span class="line"><a class="headline" href="/decisions/graph/{item.id}">Decision #{item.id}</a>{topic}</span>\n'
            f'          <div class="prose">{_prose_paragraphs(item.decision)}</div>\n'
            "        </div>\n"
            "      </div>"
        )
    return '    <div class="list">\n' + "\n".join(rows) + "\n    </div>"


def _rejected_list(alternatives: Sequence[decision_graph.RejectedAlternative]) -> str:
    if not alternatives:
        return '    <div class="card"><span class="none">none</span></div>'
    rows = []
    for alt in alternatives:
        rows.append(
            '      <div class="row">\n'
            '        <div class="stack">\n'
            '          <span class="cap">approach</span>\n'
            f'          <div class="prose">{_prose_paragraphs(alt.approach)}</div>\n'
            '          <span class="cap">reason</span>\n'
            f'          <div class="prose">{_prose_paragraphs(alt.reason)}</div>\n'
            "        </div>\n"
            "      </div>"
        )
    return '    <div class="list">\n' + "\n".join(rows) + "\n    </div>"


def index_page(
    nodes: Sequence[decision_graph.AncestorDecision],
    ctx: Context | None = None,
    navigation: dict | None = None,
) -> str:
    """Render `/decisions/graph`: current unreplaced leaves with full decision text, reason, and per-node links."""
    ctx = ctx or Context(datetime.now().astimezone())
    by_id, parents_by_node, _ = _graph_maps(nodes)
    leaves = decision_graph.current_leaves([by_id[nid] for nid in sorted(by_id)], parents_by_node)
    eyebrow = " · ".join([
        _plural(len(leaves), "current leaf", "current leaves"),
        _plural(len(by_id), "recorded decision"),
        '<a href="/decisions">decisions</a>',
        '<a href="/">board</a>',
    ])
    if leaves:
        cards = []
        for leaf in leaves:
            topic = f'<span class="topic">{escape(leaf.topic)}</span>' if leaf.topic else ""
            by = f'<span class="eyebrow">by {escape(leaf.decided_by)}</span>' if leaf.decided_by else ""
            issue = _issue_link(leaf.issue_key, ctx)
            parents_note = (
                f'<span class="eyebrow">{_plural(len(leaf.parent_ids), "parent")}</span>'
                if leaf.parent_ids else ""
            )
            meta = "".join(part for part in (topic, by, issue, parents_note) if part)
            cards.append(
                '    <div class="card ask">\n'
                f'      <span class="line"><a class="headline" href="/decisions/graph/{leaf.id}">Decision #{leaf.id}</a>{meta}</span>\n'
                '      <span class="cap">decision</span>\n'
                f'      <div class="prose">{_prose_paragraphs(leaf.decision)}</div>\n'
                '      <span class="cap">reason</span>\n'
                f'      <div class="prose">{_prose_paragraphs(leaf.reason)}</div>\n'
                "    </div>"
            )
        body = "\n".join(cards)
    else:
        body = '    <div class="card"><span class="none">none</span></div>'
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>Decision graph</title>\n{HEAD_OPEN}{theme()}{PAGE_CSS}</head>\n"
        '<body>\n<main class="decisions">\n'
        f"{tab_bar('Decisions', 0, True, **(navigation or {}))}"
        '  <header class="top">\n    <div>\n      <h1>Decision graph</h1>\n'
        f'      <span class="eyebrow">{eyebrow}</span>\n    </div>\n  </header>\n'
        + _section(f"Current leaves · {len(leaves)}", body)
        + "</main>\n</body>\n</html>\n"
    )


def node_page(
    nodes: Sequence[decision_graph.AncestorDecision],
    node_id: int,
    ctx: Context | None = None,
    navigation: dict | None = None,
) -> str | None:
    """Render `/decisions/graph/<id>`, or None when no node in the graph has `node_id`."""
    ctx = ctx or Context(datetime.now().astimezone())
    by_id, parents_by_node, children_by_node = _graph_maps(nodes)
    node = by_id.get(node_id)
    if node is None:
        return None
    leaves = decision_graph.leaf_ids(parents_by_node)
    is_leaf = node_id in leaves
    parents = [by_id[pid] for pid in parents_by_node[node_id]]
    children = [by_id[cid] for cid in children_by_node[node_id]]
    rejected = () if is_leaf else node.rejected_alternatives
    issue = _issue_link(node.issue_key, ctx)
    eyebrow = " · ".join([
        "Decision graph",
        f"#{node.id}",
        *([escape(node.topic)] if node.topic else []),
        *([f"by {escape(node.decided_by)}"] if node.decided_by else []),
        *([issue] if issue else []),
        *([escape(node.created_at)] if node.created_at else []),
        '<a href="/decisions/graph">current leaves</a>',
        '<a href="/decisions">decisions</a>',
        '<a href="/">board</a>',
    ])
    chip = '<span class="chip answered">CURRENT LEAF</span>' if is_leaf else '<span class="chip">SUPERSEDED</span>'
    heading = f"Decision #{node.id}" + (f" · {escape(node.topic)}" if node.topic else "")
    left = (
        _section(f"Parents · {len(parents)}", _neighbour_list(parents))
        + _section(f"Children · {len(children)}", _neighbour_list(children))
    )
    right = _section(f"Rejected alternatives · {len(rejected)}", _rejected_list(rejected))
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{heading}</title>\n{HEAD_OPEN}{theme()}{PAGE_CSS}</head>\n"
        '<body>\n<main class="decision">\n'
        f"{tab_bar('Decisions', 0, True, **(navigation or {}))}"
        '  <header class="top">\n    <div>\n'
        f'      <span class="eyebrow">{eyebrow}</span>\n'
        f"      <h1>{heading}</h1>\n    </div>\n    {chip}\n  </header>\n\n"
        '  <div class="card ask">\n'
        '    <span class="cap">decision</span>\n'
        f'    <div class="prose">{_prose_paragraphs(node.decision)}</div>\n'
        '    <span class="cap">reason</span>\n'
        f'    <div class="prose">{_prose_paragraphs(node.reason)}</div>\n'
        "  </div>\n\n"
        '  <div class="cols">\n  <div class="col">\n'
        + left
        + '  </div>\n  <div class="col">\n'
        + right
        + "  </div>\n  </div>\n</main>\n</body>\n</html>\n"
    )


def write_index(
    root: Path,
    pages_dir: Path,
    day: str,
    *,
    db: PageData | None = None,
    navigation: dict | None = None,
) -> Path:
    """Render `decisions-graph.html` from detached store rows; empty graph when no store rows exist."""
    out = pages_dir / "decisions-graph.html"
    ctx = context(root, day) or Context(datetime.now().astimezone())
    nodes = db.graph_nodes() if db is not None else ()
    write_if_changed(out, index_page(nodes, ctx, navigation))
    return out


def write_node(
    root: Path,
    pages_dir: Path,
    node_id: int,
    day: str,
    *,
    db: PageData | None = None,
    navigation: dict | None = None,
) -> Path | None:
    """Render `decisions-graph-<node_id>.html` from detached store rows; unlink and return None when unknown."""
    out = pages_dir / f"decisions-graph-{node_id}.html"
    ctx = context(root, day) or Context(datetime.now().astimezone())
    page = node_page(db.graph_nodes(), node_id, ctx, navigation) if db is not None else None
    if page is None:
        out.unlink(missing_ok=True)
        return None
    write_if_changed(out, page)
    return out
