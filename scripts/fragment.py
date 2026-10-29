"""Shared HTML escaping, links, theme and tab bar.

Charts use this helper without importing page, workspace or third-party modules.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import urlsplit

KIND = re.compile(r"[a-z][a-z0-9-]*")
COLOUR = re.compile(r"[#\w(),.%/ -]+")  # a colour, var() or border shorthand; nothing that ends a declaration or a rule


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def external_url(url: str) -> str:
    """`url` stripped when it is a clean http(s) URL with a host, else `""`."""
    clean = (url or "").strip()
    if not clean or any(ord(c) <= 32 or ord(c) == 127 for c in clean):
        return ""
    parts = urlsplit(clean)
    return clean if parts.scheme.lower() in ("http", "https") and bool(parts.netloc) else ""


def href(url: str) -> str:
    """`href="url"`, opening in a new tab when `url` is external (http/https); in-app routes stay in the tab."""
    if ext := external_url(url):
        return f'href="{esc(ext)}" target="_blank" rel="noopener"'
    scheme = urlsplit((url or "").strip()).scheme.lower()
    safe = "#" if scheme in ("javascript", "data", "vbscript") else url
    return f'href="{esc(safe)}"'


def kind(name: str) -> str:
    """`name`, if it can stand as a CSS class."""
    if not KIND.fullmatch(name):
        raise ValueError(f"kind {name!r} is not a class name")
    return name


def css_str(text: str) -> str:
    """A CSS string body that cannot end the string, the rule or the `<style>` element."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\3c ").replace("\n", "\\a ")


def colour(value: str) -> str:
    """`value`, if it cannot end its declaration or rule."""
    if not COLOUR.fullmatch(value):
        raise ValueError(f"colour {value!r} is not a colour")
    return value


# The Board sections nav (Flow.dc.html) atop every page: (name, route); a tab with no page yet has no route.
TABS = (("Board", "/"), ("Workers", "/workers"), ("Dispatch", ""), ("Since you last looked", ""), ("Epics", ""),
        ("Decisions", "/decisions"), ("Limits", ""))
# On the pages' tokens (--fg, --muted, --line, --link); wraps at phone width rather than widening the page.
# The pages' type: Alegreya Sans for text, Cormorant SC for heads.
FONTS = "https://fonts.googleapis.com/css2?family=Alegreya+Sans:wght@400;500;600&family=Cormorant+SC:wght@600&display=swap"
# The canonical page theme (frank-lloyd-aight docs/page-theme.css), vendored at assets/page-theme.css by
# scripts/sync_page_theme.py; read at render time, so a re-sync reaches the served pages without a restart.
THEME_PATH = Path(__file__).resolve().parent.parent / "assets" / "page-theme.css"
TAB_CSS = """nav.tabs{display:flex;flex-wrap:wrap;align-items:flex-end;column-gap:28px;margin:-8px 0 0;border-bottom:1px solid var(--line)}
nav.tabs>*{display:inline-flex;align-items:baseline;gap:7px;min-height:34px;box-sizing:border-box;padding:8px 0 7px;margin-bottom:-1px;font:600 15.5px "Cormorant SC",Georgia,serif;letter-spacing:.08em;text-decoration:none;color:var(--muted)}
nav.tabs>[aria-current=page]{color:var(--fg);box-shadow:inset 0 -3px 0 var(--fg)}
nav.tabs>.tab-parent{color:var(--fg);box-shadow:inset 0 -1px 0 var(--fg)}
nav.tabs>[data-tone=hot] .badge{color:var(--dl)}
nav.tabs>[data-tone=ready] .badge{color:var(--stage-review)}
nav.tabs>[data-tone=pending] .badge{color:var(--brass)}
nav.tabs>[data-tone=risk] .badge{color:var(--stage-triage)}
nav.tabs .badge{font:700 11.5px ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:0;color:var(--link,var(--brass))}
nav.tabs>span[title]{opacity:.55;cursor:default}
nav.tabs>span[title] .badge{color:var(--muted)}
"""


PROSE_CSS = """
.prose{min-width:0;overflow-wrap:anywhere}
.prose>:first-child{margin-top:0}.prose>:last-child{margin-bottom:0}
.prose p+p{margin-top:6px}
.prose pre{overflow-x:auto;padding:10px;background:var(--bg);border-radius:4px}
.prose pre code{white-space:pre;background:none;padding:0}
.prose table{display:block;overflow-x:auto;border-collapse:collapse;max-width:100%}
.prose th,.prose td{border:1px solid var(--line);padding:4px 8px}
.prose img{max-width:100%}
"""


def theme() -> str:
    """The vendored theme's CSS, to sit at the top of a page's own `<style>` block, ahead of the page's rules —
    cascade order (theme first, page rules second) is all that decides what wins while the pages keep their own look.
    One block per page, as the tests that slice the pages' CSS expect; the comment marks where the theme ends."""
    return THEME_PATH.read_text() + PROSE_CSS + "\n/* the canonical theme ends here; the page's own rules follow */\n"


def tab_bar(tab: str, pending: int, parent: bool = False, since: tuple[int, str] = (0, ""),
            epics: tuple[int, int] = (0, 0), workers: int = 0, dispatch: int = 0, decisions: int | None = None) -> str:
    """The nav, `tab` marked as this page's (`aria-current`), or as its parent's on a detail page (`parent`).
    Counts come from the pages' shared context; zero badges are omitted. Unbuilt pages still carry their counts.
    Epics preserves its at-risk/late breakdown (#230); `since` preserves the last-look time (#229)."""
    pending = pending if decisions is None else decisions
    mark = ' class="tab-parent"' if parent else ' aria-current="page"'
    at_risk = " · ".join(f"{n} {what}" for n, what in zip(epics, ("at risk", "late")) if n)
    badges = {"Workers": (str(workers) if workers else "", f"{workers} need input", "hot"),
              "Dispatch": (str(dispatch) if dispatch else "", f"{dispatch} ready to dispatch", "ready"),
              "Decisions": (str(pending) if pending else "", f"{pending} pending", "pending"),
              "Since you last looked": (str(since[0]) if since[0] else "", f"{since[0]} went needs input since {since[1]}", "hot"),
              "Epics": (at_risk, f"milestones: {at_risk}", "hot" if epics[1] else "risk")}
    out = []
    for name, url in TABS:
        text, title, tone = badges.get(name, ("", "", ""))
        badge = f'<span class="badge" title="{esc(title)}">{esc(text)}</span>' if text else ""
        attrs = (mark if name == tab else "") + (f' data-tone="{tone}"' if text and url else "")
        out.append(f"<a {href(url)}{attrs}>{name}{badge}</a>" if url else f'<span{attrs} title="not available yet">{name}{badge}</span>')
    return '<nav class="tabs" aria-label="Board sections">' + "".join(out) + "</nav>\n"


def issue_href(key: str | int) -> str:
    """An issue identity as one encoded route component, including its forge namespace."""
    from urllib.parse import quote
    return "/issues/" + quote(str(key), safe="")


def issue_page_name(key: str | int, source: bool = False) -> str:
    raw = str(key)
    token = raw if raw.isdecimal() else "k" + raw.encode().hex()
    return f"issue-{token}{'-source' if source else ''}.html"
