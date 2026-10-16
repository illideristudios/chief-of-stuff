"""A column board as an HTML fragment and its CSS: cards under column heads, with an overflow fold.

Generic by design: it knows columns, cards, tags and marks, never what a column or a state means.
Colours come from the palette passed to `css`; kinds are CSS classes a host can restyle or add to.
No JavaScript: overflow is a native `<details>`, everything else is a class.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from fragment import colour as _colour, css_str as _css_str, esc as _esc, href as _href, kind as _kind

LIMIT = 3
# Okabe-Ito on parchment. (fill, ink, border): a tag's background, its text, and its border shorthand.
PALETTE: dict[str, tuple[str, str, str]] = {
    "running": ("#0072B2", "#fbf8ef", "1px solid #0072B2"),
    "waiting": ("#f4efe1", "#8a6c30", "1px solid #a7843e"),
    "open": ("#f4efe1", "#2f2630", "1px solid #ddd3bd"),
    "orphaned": ("#fbf8ef", "#c9533a", "1px dashed #c9533a"),
    "done": ("#4f6b3a", "#fbf8ef", "1px solid #4f6b3a"),
    "hold": ("#c9533a", "#fbf8ef", "1px solid #c9533a"),
}


@dataclass(frozen=True)
class Mark:
    text: str
    category: str


@dataclass(frozen=True)
class Card:
    name: str
    kind: str  # the chip's palette key, and what overflow tallies
    refs: tuple[tuple[str, str], ...] = ()  # (text, url); a blank url draws plain text
    owner: str = ""
    state: str = ""  # the chip's text; blank draws no chip
    marks: tuple[Mark, ...] = ()
    flag: Mark | None = None  # the card's whole condition: an outline and a bar across its foot, in its colour
    href: str = ""  # links the name
    owner_href: str = ""  # a worker owner's `/workers` link; blank keeps a person or `unassigned` plain (#209)


@dataclass(frozen=True)
class Column:
    name: str
    cards: tuple[Card, ...]
    note: str = ""  # a word beside the name
    kind: str = ""  # a class, `columns-<kind>`; the base CSS styles "gate"


def _tag(role: str, category: str, text: str) -> str:
    return f'<span class="columns-tag columns-{role}" data-cat="{_esc(category)}">{_esc(text)}</span>'


def _link(text: str, url: str, cls: str = "", tag: str = "span", prose: bool = False, inline: Callable[[str], str] = _esc) -> str:
    c = f' class="{cls}"' if cls else ""
    body = inline(text) if prose else _esc(text)
    # Inline Markdown can have its own links; never nest those in the card's navigation link.
    return f'<a{c} {_href(url)}>{body}</a>' if url and '<a ' not in body else f"<{tag}{c}>{body}</{tag}>"


def _card(c: Card, inline: Callable[[str], str] = _esc) -> str:
    chip = _tag("chip", c.kind, c.state) if c.state else ""
    marks = f'<div class="columns-marks">{"".join(_tag("mark", m.category, m.text) for m in c.marks)}</div>' if c.marks else ""
    head = f'<div class="columns-card columns-flagged" data-cat="{_esc(c.flag.category)}">' if c.flag else '<div class="columns-card">'
    flag = f'<div class="columns-tag columns-flag" data-cat="{_esc(c.flag.category)}">{_esc(c.flag.text)}</div>' if c.flag else ""
    return (f'{head}{_link(c.name, c.href, "columns-card-name", "div", True, inline)}'
            + (f'<div class="columns-refs">{"".join(_link(t, u) for t, u in c.refs)}</div>' if c.refs else "")
            + f'<div class="columns-who">{_link(c.owner, c.owner_href, "columns-owner")}{chip}</div>{marks}{flag}</div>')


def tally(cards: tuple[Card, ...]) -> str:
    """One span per kind, in order of first appearance: `2 open`, `1 done`."""
    return "".join(f"<span>{n} {_esc(k)}</span>" for k, n in Counter(c.kind for c in cards).items())


def _column(col: Column, limit: int, inline: Callable[[str], str] = _esc) -> str:
    kind = f" columns-{_kind(col.kind)}" if col.kind else ""
    note = f'<span class="columns-note">{_esc(col.note)}</span>' if col.note else ""
    shown, rest = col.cards[:limit], col.cards[limit:]
    more = (f'<details class="columns-more"><summary>+ {tally(rest)}</summary>{"".join(_card(c, inline) for c in rest)}</details>'
            if rest else "")
    return (f'<section class="columns-col{kind}" aria-label="{_esc(col.name)}, {len(col.cards)}">'
            f'<div class="columns-head"><h3 class="columns-name">{_esc(col.name)}</h3>{note}'
            f'<span class="columns-count">{len(col.cards)}</span></div>{"".join(_card(c, inline) for c in shown)}{more}</section>')


def render(cols: list[Column], limit: int = LIMIT, inline: Callable[[str], str] = _esc) -> str:
    """The board as one `<figure>`: at most `limit` cards a column, the rest folded."""
    n = sum(len(c.cards) for c in cols)
    out = [f'<figure class="columns" aria-label="{len(cols)} column{"" if len(cols) == 1 else "s"}, {n} card{"" if n == 1 else "s"}">',
           '<div class="columns-grid">', *(_column(c, limit, inline) for c in cols), "</div>", "</figure>"]
    return "\n".join(out)


BASE_CSS = """\
.columns{--columns-ink:#2f2630;--columns-muted:#6e6470;--columns-link:#8a6c30;--columns-edge:#a7843e;--columns-bg:#ebe4d2;--columns-gate:#efe6cf;--columns-gate-ink:#8a6c30;--columns-card:#fbf8ef;--columns-rule:#ddd3bd;margin:0;color:var(--columns-ink)}
.columns-grid{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(120px,1fr);gap:8px;overflow-x:auto}
.columns-col{background:var(--columns-bg);border-radius:5px;padding:7px 6px;display:flex;flex-direction:column;gap:6px;min-width:0}
.columns-gate{background:var(--columns-gate)}
.columns-head{display:flex;align-items:baseline;gap:6px;border-bottom:1px dotted var(--columns-rule);padding-bottom:5px}
.columns-name{margin:0;font:600 13px "Cormorant SC",Georgia,serif;letter-spacing:.08em;text-transform:uppercase;border:0;padding:0}
.columns-gate .columns-name{color:var(--columns-gate-ink)}
.columns-note{font-size:11px;color:var(--columns-muted)}
.columns-count{font-size:14px;font-weight:700;margin-left:auto;font-variant-numeric:tabular-nums}
.columns-card{background:var(--columns-card);border:1px solid var(--columns-rule);border-radius:4px;padding:6px 7px;display:flex;flex-direction:column;gap:3px;overflow-wrap:anywhere}
.columns-card-name{font-size:12.5px;font-weight:500;line-height:1.25;color:inherit;text-decoration:none}
.columns-refs{display:flex;gap:5px;flex-wrap:wrap;font:10.5px ui-monospace,monospace;color:var(--columns-muted)}
.columns-refs a{color:var(--columns-link);text-decoration:none;border-bottom:1px dotted var(--columns-edge)}
.columns a:focus-visible{outline:2px solid var(--columns-ink);outline-offset:1px}
.columns-who,.columns-marks{display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.columns-owner{font-size:11px;color:var(--columns-muted);text-decoration:none}
.columns-tag{--c:var(--columns-card);--i:var(--columns-ink);--e:1px solid var(--columns-rule);background:var(--c);color:var(--i);border:var(--e)}
.columns-chip{font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;padding:1px 6px;border-radius:9px}
.columns-mark{font-size:10px;padding:0 5px;border-radius:3px}
.columns-flagged{border:2px solid var(--c);padding-bottom:0;overflow:hidden}
.columns-flag{margin:3px -7px 0;padding:2px 0;border:0;text-align:center;font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase}
.columns-more{font-size:11px;color:var(--columns-muted)}
.columns-more>summary{cursor:pointer;display:flex;gap:6px;flex-wrap:wrap}
.columns-more[open]{display:flex;flex-direction:column;gap:6px}
.columns summary:focus-visible{outline:2px solid var(--columns-ink);outline-offset:1px}
"""


def css(colors: dict[str, tuple[str, str, str]] = PALETTE) -> str:
    """The stylesheet: layout, the gate kind, and one rule per tag category. Override any `--columns-*` token on `.columns`."""
    rules = [f'.columns-tag[data-cat="{_css_str(cat)}"],.columns-card[data-cat="{_css_str(cat)}"]{{--c:{_colour(fill)};--i:{_colour(ink)};--e:{_colour(edge)}}}'
             for cat, (fill, ink, edge) in colors.items()]
    return BASE_CSS + "\n".join(rules) + "\n"
