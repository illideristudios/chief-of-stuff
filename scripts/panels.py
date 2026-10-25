"""A dashboard's header, count tiles and list panels as HTML fragments and their CSS.

Generic by design: it knows titles, counts and rows of facts, never what a tile counts or what a row is.
Colours come from the palette and tokens passed to `css`; kinds are CSS classes a host can restyle or add to.
No JavaScript: the clocks carry `datetime` attributes a host script may keep ticking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fragment import colour as _colour, css_str as _css_str, esc as _esc, href as _href, kind as _kind

# Okabe-Ito on parchment: a kind's accent, its top rule and its count.
PALETTE: dict[str, str] = {
    "blocked": "#c9533a", "decisions": "#a7843e", "approved": "#009E73", "running": "#0072B2",
    "drift": "#c9533a", "orphaned": "#6e6470", "merge": "#009E73", "workers": "#0072B2",
}
TOKENS: dict[str, str] = {
    "ink": "#2f2630", "muted": "#6e6470", "card": "#fbf8ef", "rule": "#ddd3bd", "edge": "#a7843e", "link": "#8a6c30",
    "hot": "#c9533a", "hot-ink": "#fbf8ef",
}


@dataclass(frozen=True)
class Header:
    title: str
    meta: tuple[str, ...]  # the line under the title, joined with " · "
    now: datetime
    deadline: str
    deadline_at: datetime
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tile:
    label: str
    count: int
    anchor: str  # the id it links to
    kind: str


@dataclass(frozen=True)
class Row:
    name: str
    facts: str = ""
    num: str = ""
    ref: str = ""
    href: str = ""  # links the ref, or the name when there is no ref
    side: str = ""  # a short value right of the name


@dataclass(frozen=True)
class Panel:
    id: str
    title: str
    kind: str
    count: int
    rows: tuple[Row, ...]
    note: str = ""
    link: tuple[str, str] | None = None  # (text, href)
    foot: str = ""
    href: str = ""  # links the title


def hm(start: datetime, end: datetime) -> str:
    """`end - start` as `8h50m`, negative once `end` has passed."""
    m = int((end - start).total_seconds() / 60)  # toward zero, as the board's ticking clock counts
    return f"{'-' if m < 0 else ''}{abs(m) // 60}h{abs(m) % 60:02d}m"


def hms(start: datetime, end: datetime) -> str:
    """Absolute whole seconds between the instants as `8h49m23s`.

    Timestamps, not `end - start`: aware datetimes that share a zone subtract as wall clocks, and a
    DST fold then disagrees with the UTC instant the browser clock uses.
    """
    s = int(abs(end.timestamp() - start.timestamp()))
    return f"{s // 3600}h{s // 60 % 60:02d}m{s % 60:02d}s"


def header(h: Header) -> str:
    warn = "".join(f'<div class="panels-warn">{_esc(w)}</div>' for w in h.warnings)
    # Strictly before now, even by a fraction of a second. Truncating first would hide that as zero.
    expired = h.deadline_at.timestamp() < h.now.timestamp()
    word = "passed" if expired else "in"
    left = hms(h.now, h.deadline_at) + (" ago" if expired else "")
    return (f'<header class="panels-head"><div class="panels-heading"><h1>{_esc(h.title)}</h1>'
            f'<div class="panels-meta">{_esc(" · ".join(h.meta))}</div>{warn}</div>'
            f'<div class="panels-clocks"><div class="panels-clock"><span class="panels-clock-name">now</span>'
            f'<time class="panels-now" datetime="{h.now.isoformat(timespec="seconds")}">{h.now:%H:%M:%S}</time></div>'
            f'<div class="panels-clock panels-due"><span class="panels-clock-name">{_esc(h.deadline)} {word}</span>'
            f'<time class="panels-left" datetime="{h.deadline_at.isoformat(timespec="seconds")}">{left}</time>'
            "</div></div></header>")


def tiles(ts: list[Tile]) -> str:
    return ('<nav class="panels-tiles" aria-label="counts">'
            + "".join(f'<a class="panels-tile" data-kind="{_kind(t.kind)}" href="#{_kind(t.anchor)}">'
                      f'<span class="panels-label">{_esc(t.label)}</span><b class="panels-count">{t.count}</b></a>' for t in ts)
            + "</nav>")


def _row(r: Row) -> str:
    num = f'<span class="panels-num">{_esc(r.num)}</span>' if r.num else ""
    ref = ((f'<a class="panels-ref" {_href(r.href)}>{_esc(r.ref)}</a>' if r.href else f'<span class="panels-ref">{_esc(r.ref)}</span>')
           if r.ref else "")
    name = (f'<a class="panels-name" {_href(r.href)}>{_esc(r.name)}</a>' if r.href and not r.ref
            else f'<span class="panels-name">{_esc(r.name)}</span>')
    side = f'<span class="panels-side">{_esc(r.side)}</span>' if r.side else ""
    facts = f'<span class="panels-facts">{_esc(r.facts)}</span>' if r.facts else ""
    return f'<div class="panels-row">{num}{ref}{name}{side}{facts}</div>'


def _panel(p: Panel) -> str:
    note = f'<span class="panels-note">{_esc(p.note)}</span>' if p.note else ""
    link = f'<a class="panels-link" {_href(p.link[1])}>{_esc(p.link[0])}</a>' if p.link else ""
    foot = f'<div class="panels-foot">{_esc(p.foot)}</div>' if p.foot else ""
    rows = "".join(map(_row, p.rows)) or '<div class="panels-row panels-empty">none</div>'
    return (f'<section class="panels-panel" id="{_kind(p.id)}" data-kind="{_kind(p.kind)}">'
            f'<div class="panels-panel-head"><h3 class="panels-title">{f'<a {_href(p.href)}>{_esc(p.title)}</a>' if p.href else _esc(p.title)}</h3>{note}{link}'
            f'<b class="panels-count">{p.count}</b></div>{rows}{foot}</section>')


def panels(ps: list[Panel]) -> str:
    """The panels as one row that stacks on a narrow screen."""
    return '<div class="panels">' + "".join(map(_panel, ps)) + "</div>"


BASE_CSS = """\
.panels-head{display:flex;align-items:flex-end;gap:12px 20px;flex-wrap:wrap;border-bottom:1px solid var(--panels-edge);padding-bottom:10px;color:var(--panels-ink)}
.panels-heading{flex:1 1 320px;min-width:0}
.panels-head h1{margin:0}
.panels-meta{font-size:12.5px;color:var(--panels-muted);overflow-wrap:anywhere}
.panels-warn{font-size:12px;color:var(--panels-hot)}
.panels-clocks{display:flex;gap:8px;align-items:flex-end}
.panels-clock{background:var(--panels-card);border:1px solid var(--panels-rule);border-radius:5px;padding:6px 12px;text-align:right;display:flex;flex-direction:column}
.panels-due{background:var(--panels-hot);border-color:var(--panels-hot);color:var(--panels-hot-ink)}
.panels-clock-name{font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;opacity:.85}
.panels-clock time{font:18px ui-monospace,monospace;font-variant-numeric:tabular-nums}
.panels-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:14px 0}
.panels-tile{display:flex;align-items:baseline;gap:6px;text-decoration:none;color:var(--panels-ink);background:var(--panels-card);border:1px solid var(--panels-rule);border-top:3px solid var(--k,var(--panels-rule));border-radius:5px;padding:7px 11px}
.panels-tile:focus-visible{outline:2px solid var(--panels-ink);outline-offset:1px}
.panels-label{font:14px "Cormorant SC",Georgia,serif;letter-spacing:.08em}
.panels-tile .panels-count{font-size:24px;margin-left:auto}
.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px;margin:14px 0;color:var(--panels-ink)}
.panels-panel{background:var(--panels-card);border:1px solid var(--panels-rule);border-top:3px solid var(--k,var(--panels-rule));border-radius:5px;padding:8px 11px;display:flex;flex-direction:column;gap:5px;min-width:0}
.panels-panel-head{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.panels-title{margin:0;font:600 13.5px "Cormorant SC",Georgia,serif;letter-spacing:.08em;border:0;padding:0}
.panels-note,.panels-link,.panels-facts,.panels-foot{font-size:11px}
.panels-note,.panels-facts,.panels-foot,.panels-num,.panels-empty{color:var(--panels-muted)}
.panels-count{font-size:16px;font-weight:700;margin-left:auto;font-variant-numeric:tabular-nums;color:var(--k,var(--panels-ink))}
.panels-row{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 6px;font-size:12.5px;border-bottom:1px dotted var(--panels-rule);padding-bottom:4px}
.panels-num,.panels-ref,.panels-side{font:12px ui-monospace,monospace;font-variant-numeric:tabular-nums;flex:none}
.panels-name{flex:1 1 0;min-width:0;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--panels-ink)}
.panels-facts{flex:1 0 100%;overflow-wrap:anywhere}
.panels a{color:var(--panels-link)} .panels a.panels-name{color:var(--panels-ink)}
"""


def css(colors: dict[str, str] = PALETTE, tokens: dict[str, str] = TOKENS) -> str:
    """The stylesheet: layout, the `--panels-*` tokens and one accent per kind."""
    root = ";".join(f"--panels-{_kind(k)}:{_colour(v)}" for k, v in tokens.items())
    rules = [f'.panels-tile[data-kind="{_css_str(k)}"],.panels-panel[data-kind="{_css_str(k)}"]{{--k:{_colour(v)}}}'
             for k, v in colors.items()]
    return BASE_CSS + f".panels-head,.panels-tiles,.panels{{{root}}}\n" + "\n".join(rules) + "\n"
