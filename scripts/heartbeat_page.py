"""The report page, `/report` (#1179): done, done on assumptions and parked, each item linked to its worker's thread or its
issue page, under the engine's last heartbeat. It renders what `heartbeat_store.FileSink` wrote and never acts."""

from __future__ import annotations

from html import escape
from pathlib import Path

import heartbeat_store
from decision_page import HEAD, write_if_changed
from fragment import issue_href, tab_bar

SECTIONS = (("done", "Done"), ("assumed", "Done on assumptions"), ("parked", "Parked"))


def _row(entry: dict) -> str:
    link = entry.get("link", "")
    href = link if link.startswith(heartbeat_store.THREAD_LINK) else issue_href(entry["key"])
    why = f' <span class="meta">· {escape(entry["why"])}</span>' if entry.get("why") else ""
    return f'<li><a href="{escape(href, quote=True)}">{escape(entry.get("title") or entry["key"])}</a>{why}</li>'


def render(saved: dict | None) -> str:
    if saved is None:
        body = '<p class="meta">No heartbeat yet: the engine has not delivered one.</p>'
    else:
        entries = saved.get("entries", [])
        sections = "".join(
            f'<section><h2>{heading} ({len(rows)})</h2>'
            + ("<ul>" + "".join(_row(e) for e in rows) + "</ul>" if rows else '<p class="meta">None.</p>') + "</section>"
            for state, heading in SECTIONS for rows in [[e for e in entries if e.get("state") == state]])
        body = (f'<p class="meta">{escape(saved.get("line", ""))}</p>'
                f'<pre class="meta">{escape(saved.get("summary", ""))}</pre>{sections}')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>Report</title>{HEAD}</head>'
            f'<body><main>{tab_bar("Board", 0, parent=True)}<header class="top"><div><h1>Report</h1></div></header>'
            f'{body}</main></body></html>')


def write(root: Path, pages_dir: Path) -> Path:
    """Render `report.html`; the page server calls this in process on every request."""
    out = pages_dir / "report.html"
    write_if_changed(out, render(heartbeat_store.read(root)))
    return out
