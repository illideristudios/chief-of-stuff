"""A Gantt chart as an HTML fragment and its CSS: rows of timed segments on a window around now.

Generic by design: it knows segments, rows and a window, never what a row or a category means.
Colours come from the palette passed to `css`; kinds are CSS classes a host can restyle or add to.
No JavaScript: positions are baked in at render, and everything else is a class.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from fragment import colour as _colour, css_str as _css_str, esc as _esc, href as _href, kind as _kind

HOUR, DAY = timedelta(hours=1), timedelta(days=1)
STEPS = tuple(HOUR * h for h in (1, 2, 3, 4, 6, 12)) + tuple(DAY * d for d in (1, 2, 7))
MAX_TICKS = 13
MIN_WIDTH = 0.5
# Okabe-Ito. A tuple is (fill, edge), for a fill too pale to read on the background, as the yellow is.
PALETTE: dict[str, str | tuple[str, str]] = {
    "implement": "#0072B2", "pr": "#56B4E9", "review": "#009E73", "triage": "#E69F00",
    "fix": "#D55E00", "verify": "#CC79A7", "merge": "#000000",
}


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    category: str
    kind: str
    title: str = ""


@dataclass(frozen=True)
class Row:
    ref: str
    name: str
    note: str
    segments: tuple[Segment, ...]
    href: str = ""  # where the ref links, if anywhere
    ref_title: str = ""

    @property
    def label(self) -> str:
        return f"{self.ref} {self.name}".strip()


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    now: datetime


def window(now: datetime, before: timedelta, after: timedelta) -> Window:
    return Window(now - before, now + after, now)


def position(at: datetime, start: datetime, end: datetime) -> float:
    """Percent across the window, clamped to it."""
    span = (end - start).total_seconds() or 1.0
    return max(0.0, min(100.0, (at - start).total_seconds() / span * 100.0))


def place(a: datetime, b: datetime, start: datetime, end: datetime, min_width: float = MIN_WIDTH) -> str:
    """The inline style for a span: clamped left and width, never narrower than `min_width` percent."""
    left, right = position(a, start, end), position(b, start, end)
    return f"left:{left:.2f}%;width:{max(right - left, min_width):.2f}%"


def tick_step(span: timedelta, max_ticks: int = MAX_TICKS) -> timedelta:
    """The finest step that labels the span with at most `max_ticks` ticks."""
    return next((s for s in STEPS if span // s + 1 <= max_ticks), STEPS[-1])


def ticks(start: datetime, end: datetime, step: timedelta | None = None, fmt: str | None = None,
          origin: datetime | None = None) -> list[tuple[datetime, str]]:
    """Ticks from `start` to `end` inclusive, on multiples of `step` from `origin` (default: `start`'s midnight)."""
    step = step or tick_step(end - start)
    fmt = fmt or ("%H" if step < DAY else "%a %d")
    at = origin or start.replace(hour=0, minute=0, second=0, microsecond=0)
    at += step * math.ceil((start - at) / step)
    out = []
    while at <= end:
        out.append((at, at.strftime(fmt)))
        at += step
    return out


_ticks = ticks  # `render` takes a `ticks` argument of its own


def _bar(g: Segment, win: Window) -> str:
    def clock(at: datetime) -> str:
        return at.strftime("%a %H:%M" if win.end - win.start > DAY or at.date() != win.now.date() else "%H:%M")

    label = _esc(" · ".join(p for p in (f"{g.category} {clock(g.start)}–{clock(g.end)}", g.title, "needs input" if g.kind == "hold" else g.kind) if p))
    return (f'<span class="gantt-bar gantt-{_kind(g.kind)}" data-cat="{_esc(g.category)}" tabindex="0" role="img" '
            f'aria-label="{label}" title="{label}" style="{place(g.start, g.end, win.start, win.end)}"></span>')


def render(rows: list[Row], win: Window, ticks: list[tuple[datetime, str]] | None = None) -> str:
    """The chart as one `<figure>`. Segments are clipped to the window; one wholly outside it is dropped."""
    marks = [(at, label) for at, label in (_ticks(win.start, win.end) if ticks is None else ticks)
             if win.start <= at <= win.end]
    now = f"{position(win.now, win.start, win.end):.2f}%"
    on = win.start <= win.now <= win.end
    out = [f'<figure class="gantt" aria-label="{len(rows)} row{"" if len(rows) == 1 else "s"}, '
           f'{win.start:%a %H:%M} to {win.end:%a %H:%M}, now {win.now:%H:%M}.">',
           '<div class="gantt-axis" aria-hidden="true">']
    out += [f'<span class="gantt-tick" style="left:{position(at, win.start, win.end):.2f}%">{_esc(label)}</span>' for at, label in marks]
    if on:
        out.append(f'<span class="gantt-now" style="left:{now}">now {win.now:%H:%M}</span>')
    out += ["</div>", '<div class="gantt-rows">', f'<div class="gantt-over" aria-hidden="true"><span class="gantt-past" style="width:{now}"></span>']
    out += [f'<span class="gantt-grid" style="left:{position(at, win.start, win.end):.2f}%"></span>' for at, _ in marks]
    out.append((f'<span class="gantt-nowline" style="left:{now}"></span>' if on else "") + "</div>")
    for r in rows:
        segs = "".join(_bar(g, win) for g in r.segments if g.end > win.start and g.start < win.end)
        title = f' title="{_esc(r.ref_title)}"' if r.ref_title else ""
        ref = f'<a {_href(r.href)} class="gantt-ref"{title}>{_esc(r.ref)}</a>' if r.href else f'<span class="gantt-ref"{title}>{_esc(r.ref)}</span>'
        out.append(f'<div class="gantt-row"><span class="gantt-label">{ref}'
                   f'<span class="gantt-name">{_esc(r.name)}</span></span><span class="gantt-track">{segs}</span>'
                   f'<span class="gantt-note">{_esc(r.note)}</span></div>')
    out.append("</div></figure>")
    return "\n".join(out)


def legend(colors: dict[str, str | tuple[str, str]] = PALETTE, kinds: tuple[str, ...] = ("forecast", "hold")) -> str:
    """One swatch per category, then one per kind other than solid; `hold` displays as `needs input`."""
    keys = [f'<span class="gantt-bar gantt-done" data-cat="{_esc(c)}"></span>{_esc(c)}' for c in colors]
    keys += [f'<span class="gantt-bar gantt-{_kind(k)}"></span>{_esc("needs input" if k == "hold" else k)}' for k in kinds]
    return '<div class="gantt-legend">' + "".join(f'<span class="gantt-key">{k}</span>' for k in keys) + "</div>"


BASE_CSS = """\
.gantt,.gantt-legend{--gantt-label:236px;--gantt-note:124px;--gantt-ink:#2f2630;--gantt-muted:#6e6470;--gantt-bg:#f4efe1;--gantt-rule:#e6dcc6;--gantt-edge:#ddd3bd;--gantt-grid:#e2d8c1;--gantt-past:#e9e1cc;--gantt-now:#c9533a;--gantt-hold:#c9533a}
.gantt{margin:0;color:var(--gantt-ink)}
.gantt-axis{position:relative;height:15px;margin:0 var(--gantt-note) 0 var(--gantt-label);font:10.5px ui-monospace,monospace;color:var(--gantt-muted)}
.gantt-tick,.gantt-now{position:absolute;transform:translateX(-50%);white-space:nowrap}
.gantt-now{color:var(--gantt-now);font-weight:600;background:var(--gantt-bg);padding:0 3px}
.gantt-rows{position:relative;display:flex;flex-direction:column;gap:3px}
.gantt-over{position:absolute;top:0;bottom:0;left:var(--gantt-label);right:var(--gantt-note);pointer-events:none}
.gantt-past,.gantt-grid,.gantt-nowline{position:absolute;top:0;bottom:0}
.gantt-past{left:0;background:var(--gantt-past);opacity:.55}
.gantt-grid{width:1px;background:var(--gantt-grid)}
.gantt-nowline{width:2px;margin-left:-1px;background:var(--gantt-now)}
.gantt-row{display:flex;align-items:center;height:22px;border-top:1px solid var(--gantt-rule)}
.gantt-label{width:var(--gantt-label);flex:none;padding-right:10px;box-sizing:border-box;display:flex;gap:6px;align-items:baseline;overflow:hidden;white-space:nowrap}
.gantt-ref{font:11px ui-monospace,monospace;color:var(--gantt-muted)}
.gantt-name{font-size:12.5px;font-weight:500;overflow:hidden;text-overflow:ellipsis}
.gantt-track{position:relative;flex-grow:1;height:14px;border-left:1px solid var(--gantt-edge)}
.gantt-note{width:var(--gantt-note);flex:none;padding-left:10px;box-sizing:border-box;font-size:11px;color:var(--gantt-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gantt-bar{--c:var(--gantt-muted);--e:var(--c);position:absolute;top:0;height:14px;border-radius:2px;box-sizing:border-box}
.gantt-bar:focus-visible{outline:2px solid var(--gantt-ink);outline-offset:1px}
.gantt-bar.gantt-done{background:var(--c);box-shadow:inset 0 0 0 1px var(--e)}
.gantt-bar.gantt-forecast{background:repeating-linear-gradient(135deg,color-mix(in srgb,var(--c) 40%,transparent) 0 3px,transparent 3px 6px);box-shadow:inset 0 0 0 1px var(--c)}
.gantt-bar.gantt-hold{background:repeating-linear-gradient(135deg,var(--gantt-hold) 0 2px,var(--gantt-bg) 2px 5px);box-shadow:inset 0 0 0 1px var(--gantt-hold)}
.gantt-legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:11px;color:var(--gantt-muted)}
.gantt-key{display:inline-flex;align-items:center;gap:5px}
.gantt-legend .gantt-bar{position:static;width:18px;height:10px}
"""


def css(colors: dict[str, str | tuple[str, str]] = PALETTE) -> str:
    """The stylesheet: layout, kinds, and one rule per category. Override any `--gantt-*` token on `.gantt`."""
    rules = []
    for cat, value in colors.items():
        fill, edge = value if isinstance(value, tuple) else (value, value)
        rules.append(f'.gantt-bar[data-cat="{_css_str(cat)}"]{{--c:{_colour(fill)};--e:{_colour(edge)}}}')
    return BASE_CSS + "\n".join(rules) + "\n"
